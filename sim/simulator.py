"""Симулятор горнотранспортного комплекса небольшого открытого карьера.

Модель: два забоя с экскаваторами, дробилка, отвал, парк самосвалов.
Поток телеметрии уходит в Kafka через "канал связи", который умеет падать
и потом догонять накопленный буфер. Стратегия назначения самосвалов
задаётся переменной DISPATCH: fixed (закреплены за забоем) или balanced.
"""

import json
import math
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

from confluent_kafka import Producer

# ---------------------------------------------------------------- настройки

BROKER = os.getenv("KAFKA_BROKER", "kafka:9092")
TOPIC_TELEMETRY = os.getenv("TOPIC_TELEMETRY", "quarry.telemetry")
TOPIC_EVENTS = os.getenv("TOPIC_EVENTS", "quarry.events")

STRATEGY = os.getenv("DISPATCH", "balanced")       # fixed | balanced
FLEET = os.getenv("FLEET", STRATEGY)               # метка парка в сообщениях
TRUCKS = int(os.getenv("TRUCKS", "8"))
TRUCK_PAYLOAD_T = float(os.getenv("TRUCK_PAYLOAD_T", "90"))

SPEED = float(os.getenv("SIM_SPEED", "60"))        # во сколько раз быстрее реального времени
TICK_SEC = float(os.getenv("SIM_TICK_SEC", "1"))   # шаг модели в секундах карьера
TELEMETRY_SEC = float(os.getenv("TELEMETRY_SEC", "5"))  # период отправки координат

BREAKDOWN_PER_HOUR = float(os.getenv("BREAKDOWN_PER_HOUR", "0.012"))
REPAIR_MIN = (float(os.getenv("REPAIR_MIN_LO", "20")), float(os.getenv("REPAIR_MIN_HI", "45")))

OUTAGE_EVERY_MIN = float(os.getenv("OUTAGE_EVERY_MIN", "0"))   # период обрыва канала, минуты карьера
OUTAGE_DUR_MIN = float(os.getenv("OUTAGE_DUR_MIN", "8"))
BUFFER_MAX = int(os.getenv("BUFFER_MAX", "60000"))
FLUSH_PER_SEC = int(os.getenv("FLUSH_PER_SEC", "1500"))        # скорость догона после обрыва

HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))
SEED = int(os.getenv("SEED", "42"))

rnd = random.Random(SEED + (0 if STRATEGY == "fixed" else 1))

# ------------------------------------------------------------------- канал


class Uplink:
    """Канал с площадки наружу. Пока оборван, всё копится в буфере."""

    def __init__(self, producer):
        self.producer = producer
        self.buffer = deque()
        self.online = True
        self.dropped = 0
        self.lock = threading.Lock()

    def send(self, topic, payload):
        payload["t_wall"] = time.time()
        msg = (topic, json.dumps(payload, ensure_ascii=False).encode())
        with self.lock:
            if self.online and not self.buffer:
                self._produce(msg)
                return
            self.buffer.append(msg)
            while len(self.buffer) > BUFFER_MAX:
                self.buffer.popleft()
                self.dropped += 1

    def pump(self, wall_dt):
        """Догон буфера ограниченной скоростью, как узкий канал на площадке."""
        with self.lock:
            if not self.online or not self.buffer:
                return
            quota = max(1, int(FLUSH_PER_SEC * wall_dt))
            for _ in range(min(quota, len(self.buffer))):
                self._produce(self.buffer.popleft())

    def _produce(self, msg):
        topic, body = msg
        try:
            self.producer.produce(topic, body)
        except BufferError:
            self.producer.poll(0.2)
            try:
                self.producer.produce(topic, body)
            except BufferError:
                self.dropped += 1

    def set_online(self, value):
        with self.lock:
            self.online = value


# ------------------------------------------------------------- карта карьера


@dataclass
class Point:
    name: str
    x: float
    y: float


FACES = {
    "EX-01": Point("Забой 1, уголь", -820, 430),
    "EX-02": Point("Забой 2, вскрыша", -960, -310),
}
DEST = {
    "CR-01": Point("Дробилка", 900, 150),
    "DP-01": Point("Отвал", 620, -700),
}


def dist_km(a, b):
    return math.hypot(a.x - b.x, a.y - b.y) / 1000.0 * 1.35  # 1.35 на извилистость дороги


@dataclass
class Excavator:
    id: str
    point: Point
    material: str
    dest_id: str
    bucket_t: float = 20.0
    bucket_sec: float = 38.0
    spot_sec: float = 45.0
    serving: str = None
    serving_until: float = 0.0
    queue: list = field(default_factory=list)
    idle_sec: float = 0.0
    busy_sec: float = 0.0
    tons: float = 0.0

    def load_seconds(self, payload_t):
        buckets = max(1, round(payload_t / self.bucket_t))
        return self.spot_sec + buckets * self.bucket_sec * rnd.uniform(0.9, 1.15)

    def wait_forecast(self, now):
        """Сколько ждать, если приехать прямо сейчас."""
        ahead = max(0.0, self.serving_until - now)
        return ahead + len(self.queue) * (self.spot_sec + 4.5 * self.bucket_sec)


@dataclass
class Truck:
    id: str
    payload_t: float
    state: str = "to_face"
    state_start: float = 0.0
    state_end: float = 0.0
    frm: Point = None
    to: Point = None
    face: str = "EX-01"
    wait_sec: float = 0.0
    load_start: float = None
    prev_load_start: float = None
    cycles: int = 0

    def progress(self, now):
        if self.state_end <= self.state_start:
            return 1.0
        return min(1.0, max(0.0, (now - self.state_start) / (self.state_end - self.state_start)))

    def position(self, now):
        if not self.frm or not self.to:
            return (0.0, 0.0)
        p = self.progress(now)
        return (self.frm.x + (self.to.x - self.frm.x) * p,
                self.frm.y + (self.to.y - self.frm.y) * p)


# ------------------------------------------------------------------- модель


class Quarry:
    def __init__(self, uplink):
        self.up = uplink
        self.now = 0.0
        self.excavators = {
            "EX-01": Excavator("EX-01", FACES["EX-01"], "уголь", "CR-01"),
            "EX-02": Excavator("EX-02", FACES["EX-02"], "вскрыша", "DP-01"),
        }
        self.trucks = []
        faces = list(self.excavators)
        for i in range(TRUCKS):
            face = faces[i % len(faces)]
            t = Truck(id="{}-{:02d}".format(FLEET[:3].upper(), i + 1),
                      payload_t=TRUCK_PAYLOAD_T * rnd.uniform(0.94, 1.0),
                      face=face)
            start = DEST[self.excavators[face].dest_id]
            self._go(t, start, self.excavators[face].point, loaded=False)
            t.state = "to_face"
            self.trucks.append(t)
        self.last_telemetry = 0.0
        self.next_outage = OUTAGE_EVERY_MIN * 60 if OUTAGE_EVERY_MIN > 0 else math.inf
        self.outage_until = -1.0

    # ---- перемещения

    def _travel_sec(self, a, b, loaded):
        kmh = (20.0 if loaded else 31.0) * rnd.uniform(0.85, 1.12)
        return dist_km(a, b) / kmh * 3600.0

    def _go(self, t, a, b, loaded):
        t.frm, t.to = a, b
        t.state_start = self.now
        t.state_end = self.now + self._travel_sec(a, b, loaded)

    # ---- выбор забоя

    def _pick_face(self, t, frm):
        if STRATEGY == "fixed":
            return t.face
        best, best_cost = t.face, math.inf
        for eid, ex in self.excavators.items():
            travel = dist_km(frm, ex.point) / 31.0 * 3600.0
            cost = travel + ex.wait_forecast(self.now)
            if cost < best_cost:
                best, best_cost = eid, cost
        return best

    # ---- шаг модели

    def step(self, dt):
        self.now += dt
        self._link_schedule()

        for ex in self.excavators.values():
            if ex.serving:
                ex.busy_sec += dt
            else:
                ex.idle_sec += dt

        for t in self.trucks:
            self._step_truck(t, dt)

        for ex in self.excavators.values():
            if ex.serving is None and ex.queue:
                self._start_loading(ex)

        if self.now - self.last_telemetry >= TELEMETRY_SEC:
            self.last_telemetry = self.now
            self._emit_telemetry()

    def _step_truck(self, t, dt):
        if t.state != "down" and rnd.random() < BREAKDOWN_PER_HOUR * dt / 3600.0:
            self._break_down(t)
            return

        if t.state == "queue":
            t.wait_sec += dt
            return
        if self.now < t.state_end:
            return

        if t.state == "to_face":
            ex = self.excavators[t.face]
            t.state = "queue"
            t.state_start = self.now
            ex.queue.append(t.id)
        elif t.state == "loading":
            ex = self.excavators[t.face]
            ex.serving = None
            ex.tons += t.payload_t
            t.state = "to_dump"
            self._go(t, ex.point, DEST[ex.dest_id], loaded=True)
        elif t.state == "to_dump":
            t.state = "dumping"
            t.state_start = self.now
            t.state_end = self.now + rnd.uniform(70, 110)
        elif t.state == "dumping":
            ex_old = self.excavators[t.face]
            self._emit_event("dump_completed", truck=t.id, excavator=t.face,
                             material=ex_old.material, tons=round(t.payload_t, 1))
            here = DEST[ex_old.dest_id]
            t.face = self._pick_face(t, here)
            t.state = "to_face"
            self._go(t, here, self.excavators[t.face].point, loaded=False)
        elif t.state == "down":
            t.state = "to_face"
            x, y = t.position(self.now)
            self._go(t, Point("ремонт", x, y), self.excavators[t.face].point, loaded=False)
            self._emit_event("repair_done", truck=t.id)

    def _break_down(self, t):
        if t.state == "queue":
            ex = self.excavators[t.face]
            if t.id in ex.queue:
                ex.queue.remove(t.id)
        if t.state == "loading":
            self.excavators[t.face].serving = None
        x, y = t.position(self.now)
        here = Point("на месте", x, y)
        t.frm = t.to = here
        t.state = "down"
        t.state_start = self.now
        t.state_end = self.now + rnd.uniform(*REPAIR_MIN) * 60
        self._emit_event("breakdown", truck=t.id,
                         minutes=round((t.state_end - t.state_start) / 60, 1))

    def _start_loading(self, ex):
        truck_id = ex.queue.pop(0)
        t = next(x for x in self.trucks if x.id == truck_id)
        wait = self.now - t.state_start
        t.state = "loading"
        t.state_start = self.now
        t.state_end = self.now + ex.load_seconds(t.payload_t)
        t.frm = t.to = ex.point
        ex.serving = t.id
        ex.serving_until = t.state_end

        t.prev_load_start, t.load_start = t.load_start, self.now
        if t.prev_load_start is not None:
            t.cycles += 1
            self._emit_event("cycle_completed", truck=t.id, excavator=ex.id,
                             material=ex.material,
                             cycle_sec=round(self.now - t.prev_load_start, 1),
                             wait_sec=round(wait, 1),
                             tons=round(t.payload_t, 1))
        else:
            self._emit_event("first_load", truck=t.id, excavator=ex.id,
                             wait_sec=round(wait, 1))

    # ---- канал

    def _link_schedule(self):
        if self.now >= self.next_outage and self.outage_until < 0:
            self.outage_until = self.now + OUTAGE_DUR_MIN * 60
            self._emit_event("link_down", minutes=OUTAGE_DUR_MIN)
            self.up.set_online(False)
        if 0 <= self.outage_until <= self.now:
            self.outage_until = -1.0
            self.next_outage = self.now + OUTAGE_EVERY_MIN * 60 if OUTAGE_EVERY_MIN > 0 else math.inf
            self.up.set_online(True)
            self._emit_event("link_up", buffered=len(self.up.buffer))

    # ---- отправка

    def _base(self):
        return {"fleet": FLEET, "strategy": STRATEGY, "t_sim": round(self.now, 1)}

    def _emit_event(self, kind, **fields):
        msg = self._base()
        msg.update({"event": kind})
        msg.update(fields)
        self.up.send(TOPIC_EVENTS, msg)

    def _emit_telemetry(self):
        for t in self.trucks:
            x, y = t.position(self.now)
            msg = self._base()
            msg.update({
                "kind": "truck", "id": t.id, "state": t.state,
                "x": round(x, 1), "y": round(y, 1),
                "face": t.face, "cycles": t.cycles,
                "loaded": t.state in ("to_dump", "dumping"),
            })
            self.up.send(TOPIC_TELEMETRY, msg)
        for ex in self.excavators.values():
            msg = self._base()
            msg.update({
                "kind": "excavator", "id": ex.id,
                "state": "loading" if ex.serving else "idle",
                "queue": len(ex.queue),
                "idle_sec": round(ex.idle_sec, 1),
                "busy_sec": round(ex.busy_sec, 1),
                "tons": round(ex.tons, 1),
            })
            self.up.send(TOPIC_TELEMETRY, msg)


# --------------------------------------------------------------- управление


def http_server(uplink, quarry_ref):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            if self.path.startswith("/outage/start"):
                uplink.set_online(False)
                self._ok({"link": "down"})
            elif self.path.startswith("/outage/stop"):
                uplink.set_online(True)
                self._ok({"link": "up"})
            else:
                self.send_error(404)

        def do_GET(self):  # noqa: N802
            q = quarry_ref.get("q")
            self._ok({
                "fleet": FLEET, "strategy": STRATEGY,
                "sim_hours": round(q.now / 3600, 2) if q else 0,
                "link": "up" if uplink.online else "down",
                "buffered": len(uplink.buffer),
                "dropped": uplink.dropped,
            })

        def _ok(self, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    HTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


def main():
    producer = Producer({
        "bootstrap.servers": BROKER,
        "linger.ms": 50,
        "queue.buffering.max.messages": 400000,
        "compression.type": "lz4",
    })
    uplink = Uplink(producer)
    quarry = Quarry(uplink)
    ref = {"q": quarry}
    threading.Thread(target=http_server, args=(uplink, ref), daemon=True).start()
    print("[sim] парк {}, стратегия {}, самосвалов {}, ускорение {}x, брокер {}".format(
        FLEET, STRATEGY, TRUCKS, SPEED, BROKER), flush=True)

    wall_step = TICK_SEC / SPEED
    next_wall = time.monotonic()
    last_pump = time.monotonic()
    while True:
        quarry.step(TICK_SEC)
        now = time.monotonic()
        uplink.pump(now - last_pump)
        last_pump = now
        producer.poll(0)
        next_wall += wall_step
        sleep = next_wall - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_wall = time.monotonic()


if __name__ == "__main__":
    main()
