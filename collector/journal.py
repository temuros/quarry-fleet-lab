"""Журнал событий карьера в PostgreSQL.

Prometheus хранит числа во времени, а отчёт по смене состоит из событий:
что случилось, с кем, сколько длилось и по какой причине. Это разные
хранилища, и смешивать их не надо.

Запись асинхронная и необязательная: если базы нет, приёмник продолжает
считать показатели. Потеря журнала не должна ронять поток телеметрии.
"""

import os
import queue
import threading
import time

DSN = os.getenv("PG_DSN", "")
QUEUE_MAX = int(os.getenv("JOURNAL_QUEUE_MAX", "20000"))
BATCH = int(os.getenv("JOURNAL_BATCH", "200"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        BIGSERIAL PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL,
    t_sim     DOUBLE PRECISION NOT NULL,
    shift_no  INTEGER NOT NULL,
    fleet     TEXT NOT NULL,
    strategy  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    who       TEXT,
    minutes   DOUBLE PRECISION,
    reason    TEXT,
    tons      DOUBLE PRECISION,
    material  TEXT
);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts DESC);
CREATE INDEX IF NOT EXISTS events_shift_idx ON events (strategy, shift_no, kind);
"""

INSERT = """
INSERT INTO events (ts, t_sim, shift_no, fleet, strategy, kind, who, minutes, reason, tons, material)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

# Сырые названия событий переводятся в то, что человек прочтёт в отчёте.
KIND_RU = {
    "breakdown": ("машина встала", "ремонт"),
    "repair_done": ("машина вышла из ремонта", None),
    "excavator_down": ("забой встал", "ремонт или перегон"),
    "excavator_up": ("забой заработал", None),
    "face_starved": ("забой без машин", "нет подачи самосвалов"),
    "link_down": ("оборвалась связь", None),
    "link_up": ("связь восстановилась", None),
    "cycle_completed": ("рейс", None),
}


class Journal:
    """Очередь в памяти плюс поток, который сливает её в базу."""

    def __init__(self, dsn=DSN):
        self.dsn = dsn
        self.queue = queue.Queue(maxsize=QUEUE_MAX)
        self.dropped = 0
        self.written = 0
        self.enabled = bool(dsn)
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True).start()
            print("[journal] пишу события в базу", flush=True)
        else:
            print("[journal] база не задана, журнал выключен", flush=True)

    def record(self, msg, shift_no):
        """Положить событие в очередь. Никогда не блокирует приёмник."""
        if not self.enabled:
            return
        kind = msg.get("event")
        if kind not in KIND_RU:
            return
        name, reason = KIND_RU[kind]
        who = msg.get("truck") or msg.get("excavator") or ("канал" if "link" in kind else None)
        minutes = msg.get("minutes")
        if minutes is None and kind == "cycle_completed":
            minutes = round(float(msg.get("cycle_sec", 0)) / 60, 1)
        row = (
            time.time(),
            float(msg.get("t_sim", 0)),
            shift_no,
            msg.get("fleet", ""),
            msg.get("strategy", ""),
            name,
            who,
            minutes,
            reason,
            msg.get("tons"),
            msg.get("material"),
        )
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    # ---- фоновая часть

    def _connect(self):
        import psycopg2

        conn = psycopg2.connect(self.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        return conn

    def _worker(self):
        import datetime

        conn = None
        while True:
            try:
                if conn is None:
                    conn = self._connect()
                    print("[journal] подключился к базе", flush=True)

                rows = [self.queue.get()]
                while len(rows) < BATCH:
                    try:
                        rows.append(self.queue.get_nowait())
                    except queue.Empty:
                        break

                prepared = [
                    (datetime.datetime.fromtimestamp(r[0], datetime.timezone.utc),) + r[1:]
                    for r in rows
                ]
                with conn.cursor() as cur:
                    cur.executemany(INSERT, prepared)
                self.written += len(prepared)
            except Exception as exc:
                # База может быть недоступна: ждём и пробуем снова, поток
                # телеметрии от этого не страдает.
                print("[journal] запись не удалась: {}".format(exc), flush=True)
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
                time.sleep(5)
