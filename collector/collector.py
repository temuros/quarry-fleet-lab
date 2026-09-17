"""Приёмник телеметрии карьера: читает Kafka, считает показатели, отдаёт Prometheus.

Показатели специально считаются здесь, а не в PromQL: модель идёт в ускоренном
времени карьера, и делить одно на другое в запросах было бы источником ошибок.
"""

import json
import os
import threading
import time
from collections import defaultdict, deque

from confluent_kafka import Consumer
from prometheus_client import Counter, Gauge, start_http_server

BROKER = os.getenv("KAFKA_BROKER", "kafka:9092")
GROUP = os.getenv("GROUP_ID", "quarry-collector")
TOPIC_TELEMETRY = os.getenv("TOPIC_TELEMETRY", "quarry.telemetry")
TOPIC_EVENTS = os.getenv("TOPIC_EVENTS", "quarry.events")

PORT = int(os.getenv("METRICS_PORT", "8000"))
WINDOW_SIM_H = float(os.getenv("WINDOW_SIM_HOURS", "2"))       # окно скользящих средних
SLA_LAG_SEC = float(os.getenv("SLA_LAG_SEC", "5"))             # порог задержки по требованиям площадки
UPTIME_WINDOW_SEC = int(os.getenv("UPTIME_WINDOW_SEC", "300"))  # окно доступности потока
STALE_SIM_SEC = float(os.getenv("STALE_SIM_SEC", "120"))       # когда забыть машину

# ------------------------------------------------------------------ метрики

m_tons = Counter("quarry_tons_total", "Вывезено горной массы, тонн",
                 ["strategy", "material"])
m_cycles = Counter("quarry_cycles_total", "Завершённых рейсов", ["strategy"])
m_breakdowns = Counter("quarry_breakdowns_total", "Поломок самосвалов", ["strategy"])
m_link_down = Counter("quarry_link_down_total", "Обрывов канала связи", ["fleet"])
m_messages = Counter("quarry_messages_total", "Принято сообщений", ["topic"])

g_tph = Gauge("quarry_tons_per_hour", "Тонн в час, скользящее окно", ["strategy"])
g_cycle = Gauge("quarry_cycle_seconds_avg", "Среднее время рейса, секунды", ["strategy"])
g_wait = Gauge("quarry_wait_seconds_avg", "Среднее ожидание под погрузку, секунды", ["strategy"])
g_idle = Gauge("quarry_excavator_idle_ratio", "Доля простоя экскаватора",
               ["strategy", "excavator"])
g_queue = Gauge("quarry_queue_len", "Самосвалов в очереди у забоя", ["strategy", "excavator"])
g_state = Gauge("quarry_trucks", "Самосвалов в состоянии", ["strategy", "state"])
g_simh = Gauge("quarry_sim_hours", "Отработано часов карьера", ["strategy"])
g_uptime = Gauge("quarry_stream_uptime_ratio", "Доступность потока данных", ["fleet"])
g_lag = Gauge("quarry_ingest_lag_seconds", "Задержка приёма телеметрии", ["fleet"])
g_sla_lag = Gauge("quarry_sla_lag_seconds", "Порог задержки по требованиям площадки")
g_sla_uptime = Gauge("quarry_sla_uptime_ratio", "Порог доступности по требованиям площадки")

g_sla_lag.set(SLA_LAG_SEC)
g_sla_uptime.set(0.95)

# --------------------------------------------------------------- состояние

lock = threading.Lock()
win_tons = defaultdict(deque)     # strategy -> (t_sim, tons)
win_cycle = defaultdict(deque)    # strategy -> (t_sim, cycle_sec)
win_wait = defaultdict(deque)     # strategy -> (t_sim, wait_sec)
win_exc = defaultdict(deque)      # (strategy, exc) -> (t_sim, idle_sec, busy_sec)
truck_state = {}                  # (strategy, truck) -> (state, t_sim)
sim_clock = defaultdict(float)    # strategy -> последнее t_sim
good_seconds = defaultdict(set)   # fleet -> секунды реального времени с живым потоком
lag_window = defaultdict(deque)   # fleet -> (wall, lag)
STARTED_AT = time.time()

WINDOW_SIM_SEC = WINDOW_SIM_H * 3600


def trim(dq, now_sim, span=WINDOW_SIM_SEC):
    while dq and now_sim - dq[0][0] > span:
        dq.popleft()


def handle_telemetry(msg):
    strategy = msg.get("strategy", "?")
    fleet = msg.get("fleet", strategy)
    t_sim = float(msg.get("t_sim", 0))
    sim_clock[strategy] = max(sim_clock[strategy], t_sim)

    lag = max(0.0, time.time() - float(msg.get("t_wall", time.time())))
    wall = time.time()
    lag_window[fleet].append((wall, lag))
    while lag_window[fleet] and wall - lag_window[fleet][0][0] > 10:
        lag_window[fleet].popleft()
    if lag < SLA_LAG_SEC:
        good_seconds[fleet].add(int(wall))

    if msg.get("kind") == "truck":
        truck_state[(strategy, msg["id"])] = (msg.get("state", "?"), t_sim)
    elif msg.get("kind") == "excavator":
        key = (strategy, msg["id"])
        win_exc[key].append((t_sim, float(msg.get("idle_sec", 0)), float(msg.get("busy_sec", 0))))
        trim(win_exc[key], t_sim)
        g_queue.labels(strategy, msg["id"]).set(msg.get("queue", 0))


def handle_event(msg):
    strategy = msg.get("strategy", "?")
    fleet = msg.get("fleet", strategy)
    t_sim = float(msg.get("t_sim", 0))
    sim_clock[strategy] = max(sim_clock[strategy], t_sim)
    kind = msg.get("event")

    if kind == "dump_completed":
        tons = float(msg.get("tons", 0))
        m_tons.labels(strategy, msg.get("material", "?")).inc(tons)
        win_tons[strategy].append((t_sim, tons))
        trim(win_tons[strategy], t_sim)
    elif kind == "cycle_completed":
        m_cycles.labels(strategy).inc()
        win_cycle[strategy].append((t_sim, float(msg.get("cycle_sec", 0))))
        win_wait[strategy].append((t_sim, float(msg.get("wait_sec", 0))))
        trim(win_cycle[strategy], t_sim)
        trim(win_wait[strategy], t_sim)
    elif kind == "breakdown":
        m_breakdowns.labels(strategy).inc()
    elif kind == "link_down":
        m_link_down.labels(fleet).inc()


def recompute():
    """Пересчёт скользящих показателей, раз в секунду реального времени."""
    with lock:
        for strategy, dq in win_tons.items():
            now_sim = sim_clock[strategy]
            trim(dq, now_sim)
            span_h = max(0.25, min(WINDOW_SIM_H, (now_sim - dq[0][0]) / 3600 if dq else 0.25))
            g_tph.labels(strategy).set(sum(v for _, v in dq) / span_h)

        for strategy, dq in win_cycle.items():
            trim(dq, sim_clock[strategy])
            g_cycle.labels(strategy).set(sum(v for _, v in dq) / len(dq) if dq else 0)
        for strategy, dq in win_wait.items():
            trim(dq, sim_clock[strategy])
            g_wait.labels(strategy).set(sum(v for _, v in dq) / len(dq) if dq else 0)

        for (strategy, exc), dq in win_exc.items():
            trim(dq, sim_clock[strategy])
            if len(dq) >= 2:
                d_idle = dq[-1][1] - dq[0][1]
                d_busy = dq[-1][2] - dq[0][2]
                total = d_idle + d_busy
                g_idle.labels(strategy, exc).set(d_idle / total if total > 0 else 0)

        counts = defaultdict(int)
        seen = set()
        for (strategy, truck), (state, t_sim) in list(truck_state.items()):
            if sim_clock[strategy] - t_sim > STALE_SIM_SEC:
                continue
            counts[(strategy, state)] += 1
            seen.add(strategy)
        for strategy in seen:
            for state in ("to_face", "queue", "loading", "to_dump", "dumping", "down"):
                g_state.labels(strategy, state).set(counts.get((strategy, state), 0))

        for strategy, t in sim_clock.items():
            g_simh.labels(strategy).set(t / 3600)

        now = int(time.time())
        # знаменатель: окно, но не больше того, сколько приёмник вообще работает
        span = max(5.0, min(float(UPTIME_WINDOW_SEC), time.time() - STARTED_AT))
        for fleet, secs in good_seconds.items():
            fresh = {s for s in secs if now - s < UPTIME_WINDOW_SEC}
            good_seconds[fleet] = fresh
            g_uptime.labels(fleet).set(min(1.0, len(fresh) / span))
        for fleet, dq in lag_window.items():
            g_lag.labels(fleet).set(max((v for _, v in dq), default=0.0))


def ticker():
    while True:
        time.sleep(1)
        try:
            recompute()
        except Exception as exc:  # показатели не должны ронять приёмник
            print("[collector] пересчёт не удался: {}".format(exc), flush=True)


def main():
    start_http_server(PORT)
    threading.Thread(target=ticker, daemon=True).start()
    consumer = Consumer({
        "bootstrap.servers": BROKER,
        "group.id": GROUP,
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
        # топик, созданный уже после подписки, иначе ждал бы обновления метаданных 5 минут
        "topic.metadata.refresh.interval.ms": 20000,
        "allow.auto.create.topics": True,
    })
    consumer.subscribe([TOPIC_TELEMETRY, TOPIC_EVENTS])
    print("[collector] метрики на :{}, брокер {}".format(PORT, BROKER), flush=True)

    while True:
        # короткий таймаут: иначе ожидание пачки само добавляло бы секунду
        # к измеряемой задержке приёма
        batch = consumer.consume(num_messages=500, timeout=0.2)
        if not batch:
            continue
        with lock:
            for raw in batch:
                if raw.error():
                    continue
                m_messages.labels(raw.topic()).inc()
                try:
                    msg = json.loads(raw.value())
                except Exception:
                    continue
                if raw.topic() == TOPIC_TELEMETRY:
                    handle_telemetry(msg)
                else:
                    handle_event(msg)


if __name__ == "__main__":
    main()
