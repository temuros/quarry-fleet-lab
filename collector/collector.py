"""Приёмник телеметрии карьера: читает Kafka, считает показатели, отдаёт Prometheus.

Показатели специально считаются здесь, а не в PromQL: модель идёт в ускоренном
времени карьера, и делить одно на другое в запросах было бы источником ошибок.

Два набора показателей и два дашборда под них:
  - инженерный: тонн в час, задержка приёма, доступность потока;
  - диспетчерский: наряд смены, состояние каждой машины и забоя, события.
"""

import json
import os
import signal
import threading
import time
from collections import defaultdict, deque

from confluent_kafka import Consumer
from prometheus_client import Counter, Gauge, start_http_server

from journal import Journal
import alerts
import podpisi

BROKER = os.getenv("KAFKA_BROKER", "kafka:9092")
GROUP = os.getenv("GROUP_ID", "quarry-collector")
TOPIC_TELEMETRY = os.getenv("TOPIC_TELEMETRY", "quarry.telemetry")
TOPIC_EVENTS = os.getenv("TOPIC_EVENTS", "quarry.events")

PORT = int(os.getenv("METRICS_PORT", "8000"))
ALERTS_PORT = int(os.getenv("ALERTS_PORT", "8001"))
WINDOW_SIM_H = float(os.getenv("WINDOW_SIM_HOURS", "2"))       # окно скользящих средних
SLA_LAG_SEC = float(os.getenv("SLA_LAG_SEC", "5"))             # порог задержки по требованиям площадки
UPTIME_WINDOW_SEC = int(os.getenv("UPTIME_WINDOW_SEC", "300"))  # окно доступности потока
STALE_SIM_SEC = float(os.getenv("STALE_SIM_SEC", "120"))       # когда забыть машину

SHIFT_HOURS = float(os.getenv("SHIFT_HOURS", "12"))            # смена на карьере
PLAN_TONS = float(os.getenv("PLAN_TONS", "20000"))             # сменный наряд, тонн

# Коды состояний: в Grafana они превращаются в слова, а не в цифры.
TRUCK_STATE_CODE = {
    "to_face": 1,    # едет порожняком
    "queue": 2,      # ждёт погрузки
    "loading": 3,    # грузится
    "to_dump": 4,    # едет гружёным
    "dumping": 5,    # разгружается
    "down": 6,       # в ремонте
}
FACE_STATE_CODE = {"loading": 1, "idle": 2, "down": 3}

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

# ---- то, что нужно диспетчеру

g_truck_state = Gauge("quarry_truck_state_code", "Что делает машина", ["strategy", "truck"])
g_truck_face = Gauge("quarry_truck_face_code", "К какому забою приписана машина",
                     ["strategy", "truck"])
g_truck_trips = Gauge("quarry_truck_trips", "Рейсов машины за смену", ["strategy", "truck"])
g_truck_tons = Gauge("quarry_truck_tons", "Тонн вывезено машиной за смену", ["strategy", "truck"])
g_truck_wait_min = Gauge("quarry_truck_wait_minutes", "Ожидание под погрузку за смену, минуты",
                         ["strategy", "truck"])
g_truck_down_min = Gauge("quarry_truck_down_minutes", "В ремонте за смену, минуты",
                         ["strategy", "truck"])

g_face_state = Gauge("quarry_face_state_code", "Что происходит в забое", ["strategy", "excavator"])

g_shift_tons = Gauge("quarry_shift_tons", "Вывезено за смену по материалу",
                     ["strategy", "material"])
g_shift_total = Gauge("quarry_shift_tons_total", "Вывезено за смену всего", ["strategy"])
g_shift_hours = Gauge("quarry_shift_hours", "Сколько часов идёт смена", ["strategy"])
g_shift_plan = Gauge("quarry_shift_plan_tons", "Сменный наряд, тонн")
g_shift_expected = Gauge("quarry_shift_expected_tons", "Сколько должно быть к этому часу",
                         ["strategy"])
g_shift_gap = Gauge("quarry_shift_gap_tons", "Отставание от наряда, тонн", ["strategy"])
g_shift_done = Gauge("quarry_shift_done_ratio", "Доля выполнения наряда", ["strategy"])

g_event_time = Gauge("quarry_last_event_time", "Когда случилось последнее такое событие",
                     ["strategy", "kind", "who"])
g_journal_written = Gauge("quarry_journal_rows_written", "Записано событий в журнал")
g_journal_dropped = Gauge("quarry_journal_rows_dropped", "Потеряно событий журнала")

g_sla_lag.set(SLA_LAG_SEC)
g_sla_uptime.set(0.95)
g_shift_plan.set(PLAN_TONS)

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

truck_seen = {}                   # (strategy, truck) -> последнее t_sim, чтобы мерить длительности
shift_idx = {}                    # strategy -> номер смены
shift_from = {}                   # strategy -> с какого t_sim мы наблюдаем эту смену
shift_tons = defaultdict(float)   # (strategy, material) -> тонн за смену
shift_trucks = defaultdict(lambda: defaultdict(float))  # strategy -> {(машина, что): значение}

WINDOW_SIM_SEC = WINDOW_SIM_H * 3600
SHIFT_SEC = SHIFT_HOURS * 3600
journal = Journal()
# Сообщения идут из трёх разделов Kafka и приходят не строго по порядку:
# часы карьера нормально скачут назад на минуты. Перезапуском считаем только
# настоящий откат к началу, а не эту чересполосицу.
RESTART_TOLERANCE_SEC = 300.0


def reset_shift(strategy, t_sim):
    """Новая смена: наряд считается с нуля, прошлая не подмешивается.

    Отдельно запоминаем, с какого момента мы эту смену видим. Если приёмник
    подняли в середине смены, сравнивать вывезенное с полным нарядом нечестно:
    получится отставание, которого не было.
    """
    for key in [k for k in shift_tons if k[0] == strategy]:
        shift_tons.pop(key, None)
    shift_trucks.pop(strategy, None)
    shift_from[strategy] = t_sim


def reset_strategy(strategy):
    """Забыть всё про парк: он начал заново."""
    win_tons.pop(strategy, None)
    win_cycle.pop(strategy, None)
    win_wait.pop(strategy, None)
    for key in [k for k in win_exc if k[0] == strategy]:
        win_exc.pop(key, None)
    for key in [k for k in truck_state if k[0] == strategy]:
        truck_state.pop(key, None)
    for key in [k for k in truck_seen if k[0] == strategy]:
        truck_seen.pop(key, None)


def note_clock(strategy, t_sim):
    """Часы карьера у парка. Если они ушли назад, симулятор перезапустили.

    Без этого приёмник смешивал бы два прогона: окна считались бы по меткам
    старого, а показатели ползли бы неизвестно куда.
    """
    previous = sim_clock.get(strategy, 0.0)
    restarted = t_sim < previous * 0.5 and previous - t_sim > RESTART_TOLERANCE_SEC
    if restarted:
        print("[collector] парк {} начал заново, состояние сброшено".format(strategy), flush=True)
        reset_strategy(strategy)
        reset_shift(strategy, t_sim)
        sim_clock[strategy] = t_sim
        shift_idx[strategy] = int(t_sim // SHIFT_SEC)
        return
    sim_clock[strategy] = max(previous, t_sim)

    current_shift = int(t_sim // SHIFT_SEC)
    if strategy not in shift_idx:
        shift_idx[strategy] = current_shift
        reset_shift(strategy, t_sim)
    elif current_shift != shift_idx[strategy]:
        print("[collector] парк {}: началась смена {}".format(strategy, current_shift), flush=True)
        shift_idx[strategy] = current_shift
        # Новая смена началась на границе, а не в момент прихода сообщения.
        reset_shift(strategy, current_shift * SHIFT_SEC)


def trim(dq, now_sim, span=WINDOW_SIM_SEC):
    while dq and now_sim - dq[0][0] > span:
        dq.popleft()


def handle_telemetry(msg):
    strategy = msg.get("strategy", "?")
    fleet = msg.get("fleet", strategy)
    t_sim = float(msg.get("t_sim", 0))
    note_clock(strategy, t_sim)

    lag = max(0.0, time.time() - float(msg.get("t_wall", time.time())))
    wall = time.time()
    lag_window[fleet].append((wall, lag))
    while lag_window[fleet] and wall - lag_window[fleet][0][0] > 10:
        lag_window[fleet].popleft()
    if lag < SLA_LAG_SEC:
        good_seconds[fleet].add(int(wall))

    if msg.get("kind") == "truck":
        truck = msg["id"]
        state = msg.get("state", "?")
        truck_state[(strategy, truck)] = (state, t_sim)

        # Длительность состояния берётся из разницы отметок времени: так
        # приёмнику не нужно знать, с каким периодом шлёт симулятор.
        key = (strategy, truck)
        previous = truck_seen.get(key)
        truck_seen[key] = t_sim
        if previous is not None and 0 < t_sim - previous < 600:
            dt = t_sim - previous
            if state == "queue":
                shift_trucks[strategy][(truck, "wait")] += dt
            elif state == "down":
                shift_trucks[strategy][(truck, "down")] += dt

        g_truck_state.labels(strategy, truck).set(TRUCK_STATE_CODE.get(state, 0))
        face = msg.get("face", "")
        if "-" in face:
            g_truck_face.labels(strategy, truck).set(int(face.split("-")[-1]))
        g_truck_wait_min.labels(strategy, truck).set(shift_trucks[strategy][(truck, "wait")] / 60)
        g_truck_down_min.labels(strategy, truck).set(shift_trucks[strategy][(truck, "down")] / 60)
        g_truck_trips.labels(strategy, truck).set(shift_trucks[strategy][(truck, "trips")])
        g_truck_tons.labels(strategy, truck).set(shift_trucks[strategy][(truck, "tons")])

    elif msg.get("kind") == "excavator":
        key = (strategy, msg["id"])
        win_exc[key].append((t_sim, float(msg.get("idle_sec", 0)), float(msg.get("busy_sec", 0))))
        trim(win_exc[key], t_sim)
        g_queue.labels(strategy, msg["id"]).set(msg.get("queue", 0))
        g_face_state.labels(strategy, msg["id"]).set(FACE_STATE_CODE.get(msg.get("state", ""), 0))


def handle_event(msg):
    strategy = msg.get("strategy", "?")
    fleet = msg.get("fleet", strategy)
    t_sim = float(msg.get("t_sim", 0))
    note_clock(strategy, t_sim)
    kind = msg.get("event")
    journal.record(msg, shift_idx.get(strategy, 0))

    if kind == "dump_completed":
        tons = float(msg.get("tons", 0))
        material = msg.get("material", "?")
        m_tons.labels(strategy, material).inc(tons)
        win_tons[strategy].append((t_sim, tons))
        trim(win_tons[strategy], t_sim)
        shift_tons[(strategy, material)] += tons
        truck = msg.get("truck")
        if truck:
            shift_trucks[strategy][(truck, "tons")] += tons
    elif kind == "cycle_completed":
        m_cycles.labels(strategy).inc()
        win_cycle[strategy].append((t_sim, float(msg.get("cycle_sec", 0))))
        win_wait[strategy].append((t_sim, float(msg.get("wait_sec", 0))))
        trim(win_cycle[strategy], t_sim)
        trim(win_wait[strategy], t_sim)
        truck = msg.get("truck")
        if truck:
            shift_trucks[strategy][(truck, "trips")] += 1
    elif kind == "breakdown":
        m_breakdowns.labels(strategy).inc()
        g_event_time.labels(strategy, "машина встала", msg.get("truck", "?")).set(time.time())
    elif kind == "repair_done":
        g_event_time.labels(strategy, "машина из ремонта", msg.get("truck", "?")).set(time.time())
    elif kind == "excavator_down":
        g_event_time.labels(strategy, "забой встал", msg.get("excavator", "?")).set(time.time())
    elif kind == "excavator_up":
        g_event_time.labels(strategy, "забой заработал", msg.get("excavator", "?")).set(time.time())
    elif kind == "link_down":
        m_link_down.labels(fleet).inc()
        g_event_time.labels(strategy, "оборвалась связь", "канал").set(time.time())
    elif kind == "link_up":
        g_event_time.labels(strategy, "связь восстановлена", "канал").set(time.time())
    elif kind == "face_starved":
        g_event_time.labels(strategy, "забой стоял без машин",
                            msg.get("excavator", "?")).set(time.time())


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
            # Наряд: сколько уже вывезли и сколько должны были к этому часу.
            # Считаем от момента, с которого смену вообще видим.
            elapsed_h = max(0.0, (t - shift_from.get(strategy, t)) / 3600)
            total = 0.0
            for (st, material), tons in shift_tons.items():
                if st != strategy:
                    continue
                g_shift_tons.labels(strategy, material).set(tons)
                total += tons
            expected = PLAN_TONS * min(1.0, elapsed_h / SHIFT_HOURS)
            g_shift_total.labels(strategy).set(total)
            g_shift_hours.labels(strategy).set(elapsed_h)
            g_shift_expected.labels(strategy).set(expected)
            g_shift_gap.labels(strategy).set(total - expected)
            g_shift_done.labels(strategy).set(total / PLAN_TONS if PLAN_TONS else 0)

        now = int(time.time())
        # знаменатель: окно, но не больше того, сколько приёмник вообще работает
        span = max(5.0, min(float(UPTIME_WINDOW_SEC), time.time() - STARTED_AT))
        for fleet, secs in good_seconds.items():
            fresh = {s for s in secs if now - s < UPTIME_WINDOW_SEC}
            good_seconds[fleet] = fresh
            g_uptime.labels(fleet).set(min(1.0, len(fresh) / span))
        for fleet, dq in lag_window.items():
            g_lag.labels(fleet).set(max((v for _, v in dq), default=0.0))

        g_journal_written.set(journal.written)
        g_journal_dropped.set(journal.dropped)


def ticker():
    while True:
        time.sleep(1)
        try:
            recompute()
        except Exception as exc:  # показатели не должны ронять приёмник
            print("[collector] пересчёт не удался: {}".format(exc), flush=True)


def main():
    start_http_server(PORT)
    # Реестр открытых ключей бортов. Поднимается до подписки: приёмник,
    # который начал считать показатели раньше, чем узнал, с кого требовать
    # подпись, засчитал бы первые отсчёты без проверки.
    proverka = podpisi.Proverka.podnyat()
    # Приём тревог от Alertmanager: они попадают в ту же ленту событий, что и
    # происшествия на карьере, потому что в разборе смены это один вопрос.
    alerts.zapustit(journal, ALERTS_PORT)
    threading.Thread(target=ticker, daemon=True).start()
    consumer = Consumer({
        "bootstrap.servers": BROKER,
        "group.id": GROUP,
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
        # топик, созданный уже после подписки, иначе ждал бы обновления метаданных 5 минут
        "topic.metadata.refresh.interval.ms": 20000,
        "allow.auto.create.topics": True,
        # Быстрее выселять мёртвого участника группы: контейнер при
        # перезапуске убивают, и без этого чтение встаёт на минуты.
        "session.timeout.ms": 15000,
        "heartbeat.interval.ms": 5000,
    })
    consumer.subscribe([TOPIC_TELEMETRY, TOPIC_EVENTS])

    # Выход из группы по сигналу: иначе Kafka ждёт истечения сессии, и
    # после перезапуска показатели молчат несколько минут.
    stopping = threading.Event()

    def on_signal(signum, frame):
        print("[collector] получен сигнал {}, выхожу из группы".format(signum), flush=True)
        stopping.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    print("[collector] метрики на :{}, брокер {}, наряд {:.0f} т за {:.0f} ч".format(
        PORT, BROKER, PLAN_TONS, SHIFT_HOURS), flush=True)

    while not stopping.is_set():
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
                    # Отсчёт, не прошедший проверку подписи, дальше не идёт:
                    # ни в показатели, ни в журнал, ни в отчёт по смене.
                    if not proverka.prinyat(msg):
                        continue
                    handle_telemetry(msg)
                else:
                    handle_event(msg)

    consumer.close()
    print("[collector] остановлен", flush=True)


if __name__ == "__main__":
    main()
