"""Борт, который говорит по EGTS, а не пишет в шину напрямую.

Раньше симулятор клал телеметрию сразу в Kafka. Так не бывает: на площадке
между машиной и шиной стоит бортовой терминал и протокол. Здесь тот же парк
шлёт координаты и датчики по EGTS в шлюз, а шлюз уже кладёт их в Kafka.

Что взято от настоящего терминала:

* запись хранится, пока сервер не подтвердил её приём. Нет подтверждения -
  запись остаётся в памяти и уедет позже;
* при обрыве связи борт копит данные и досылает их пачками, когда связь
  вернулась. Именно так теряются или не теряются рейсы на реальном карьере;
* соединение восстанавливается само, с представлением терминала заново.

⚠️ События смены (рейс закрыт, простой начался) по EGTS не идут: на площадке
их формирует система, а не терминал. Они по-прежнему уходят в шину напрямую.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from collections import deque

import skhema
from protocol import (
    PT_APPDATA,
    SERVICE_AUTH,
    SERVICE_TELEDATA,
    SR_AD_SENSORS_DATA,
    SR_POS_DATA,
    SR_TERM_IDENTITY,
    Datchiki,
    EgtsError,
    NeedMoreData,
    Podzapis,
    Poziciya,
    Predstavlenie,
    Zapis,
    razobrat_paket,
    sobrat_paket,
)


class EgtsUplink:
    """Канал борта: телеметрия по EGTS, события по-прежнему в шину."""

    def __init__(self, producer, host: str, port: int, park: str,
                 bufer_max: int = 60000, v_pachke: int = 60):
        self.producer = producer
        self.host = host
        self.port = port
        self.park = park
        self.bufer_max = bufer_max
        self.v_pachke = v_pachke

        self.buffer = deque()      # записи EGTS, ждущие отправки
        self.kafka_buffer = deque()  # события, ждущие отправки в шину
        self.online = True
        self.dropped = 0
        self.lock = threading.Lock()

        self.sock: socket.socket | None = None
        self.nomer_paketa = 0
        self.nomer_zapisi = 0
        self.ostatok = b""
        self.sleduyushchaya_popytka = 0.0

    # ------------------------------------------------------------- отправка

    def send(self, topic: str, payload: dict):
        payload["t_wall"] = time.time()
        if payload.get("kind") in ("truck", "excavator"):
            zapis = self._v_zapis(payload)
            with self.lock:
                self.buffer.append(zapis)
                while len(self.buffer) > self.bufer_max:
                    self.buffer.popleft()
                    self.dropped += 1
            return
        # Событие смены: его формирует система карьера, борт такого не шлёт.
        import json
        telo = (topic, json.dumps(payload, ensure_ascii=False).encode())
        with self.lock:
            self.kafka_buffer.append(telo)
        self._slit_kafka()

    def pump(self, wall_dt: float):
        """Догон: пачками, как терминал после возвращения связи."""
        self._slit_kafka()
        with self.lock:
            if not self.online or not self.buffer:
                return
        for _ in range(4):
            if not self._otpravit_pachku():
                break

    def set_online(self, value: bool):
        with self.lock:
            self.online = value
            if not value and self.sock is not None:
                # Обрыв канала это не закрытие приложения: сокет рвётся молча,
                # и терминал узнаёт об этом только по отсутствию подтверждений.
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
                self.ostatok = b""

    # ------------------------------------------------------------- внутреннее

    def _slit_kafka(self):
        with self.lock:
            ochered = list(self.kafka_buffer)
            self.kafka_buffer.clear()
        for topic, telo in ochered:
            try:
                self.producer.produce(topic, telo)
            except BufferError:
                self.producer.poll(0.2)
                try:
                    self.producer.produce(topic, telo)
                except BufferError:
                    self.dropped += 1

    def _v_zapis(self, payload: dict) -> bytes:
        tip = payload["kind"]
        oid = skhema.nomer_borta(self.park, tip, payload["id"])
        t_wall = payload.get("t_wall", time.time())
        analogovye = {
            skhema.ADS_T_SIM: int(payload.get("t_sim", 0)),
            skhema.ADS_MS: int((t_wall - int(t_wall)) * 1000),
        }
        din = 0
        if tip == "truck":
            analogovye[skhema.ADS_TRUCK_CYCLES] = int(payload.get("cycles", 0))
            analogovye[skhema.ADS_TRUCK_STATE] = skhema.SOSTOYANIYA_TRUCK.get(payload.get("state"), 0)
            analogovye[skhema.ADS_TRUCK_FACE] = skhema.ZABOI.get(payload.get("face"), 0)
            if payload.get("loaded"):
                din |= skhema.DIN_LOADED
            shirota, dolgota = skhema.v_gradusy(payload.get("x", 0.0), payload.get("y", 0.0))
        else:
            analogovye[skhema.ADS_EXC_QUEUE] = int(payload.get("queue", 0))
            analogovye[skhema.ADS_EXC_STATE] = skhema.SOSTOYANIYA_EXC.get(payload.get("state"), 0)
            analogovye[skhema.ADS_EXC_TONS] = int(round(payload.get("tons", 0.0) * 10))
            analogovye[skhema.ADS_EXC_IDLE] = int(payload.get("idle_sec", 0))
            analogovye[skhema.ADS_EXC_BUSY] = int(payload.get("busy_sec", 0))
            # У экскаватора координаты постоянные: он стоит в забое.
            shirota, dolgota = skhema.v_gradusy(0.0, 0.0)

        poziciya = Poziciya(
            vremya=int(t_wall),
            shirota=shirota,
            dolgota=dolgota,
            skorost=0.0,
            din=din,
        )
        self.nomer_zapisi = (self.nomer_zapisi + 1) & 0xFFFF
        return Zapis(
            nomer=self.nomer_zapisi,
            obyekt=oid,
            vremya=int(t_wall),
            podzapisi=[
                Podzapis(SR_POS_DATA, poziciya.v_bayty()),
                Podzapis(SR_AD_SENSORS_DATA, Datchiki(analogovye=analogovye, diskretnye=din).v_bayty()),
            ],
        ).v_bayty()

    def _soedinit(self) -> bool:
        if self.sock is not None:
            return True
        if time.monotonic() < self.sleduyushchaya_popytka:
            return False
        try:
            sock = socket.create_connection((self.host, self.port), timeout=5)
            sock.settimeout(5)
            # Терминал при подключении представляется: сервер должен знать,
            # чьи данные принимает, ещё до первой координаты.
            predstavlenie = Zapis(
                nomer=0,
                obyekt=None,
                sluzhba=SERVICE_AUTH,
                podzapisi=[Podzapis(SR_TERM_IDENTITY,
                                    Predstavlenie(skhema.PARKI.get(self.park, 9)).v_bayty())],
            ).v_bayty()
            self.nomer_paketa = (self.nomer_paketa + 1) & 0xFFFF
            sock.sendall(sobrat_paket(PT_APPDATA, self.nomer_paketa, predstavlenie))
            self.sock = sock
            self.ostatok = b""
            return True
        except OSError:
            # Частые попытки переподключения на площадке только мешают:
            # канал и так узкий.
            self.sleduyushchaya_popytka = time.monotonic() + 2.0
            return False

    def _otpravit_pachku(self) -> bool:
        if not self._soedinit():
            return False
        with self.lock:
            if not self.buffer:
                return False
            pachka = [self.buffer.popleft() for _ in range(min(self.v_pachke, len(self.buffer)))]

        telo = b"".join(pachka)
        self.nomer_paketa = (self.nomer_paketa + 1) & 0xFFFF
        try:
            self.sock.sendall(sobrat_paket(PT_APPDATA, self.nomer_paketa, telo))
            self._zhdat_podtverzhdeniya()
            return True
        except (OSError, EgtsError):
            # Не подтверждено - значит не доставлено. Возвращаем записи в
            # начало буфера: порядок для отчёта по смене важен.
            with self.lock:
                self.buffer.extendleft(reversed(pachka))
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
            self.sleduyushchaya_popytka = time.monotonic() + 2.0
            return False

    def _zhdat_podtverzhdeniya(self):
        """Ждём ответ сервера. Нет ответа - считаем пачку недоставленной."""
        self.sock.settimeout(5)
        while True:
            try:
                tip, nomer, telo, dlina = razobrat_paket(self.ostatok)
            except NeedMoreData:
                kusok = self.sock.recv(65536)
                if not kusok:
                    raise OSError("сервер закрыл соединение")
                self.ostatok += kusok
                continue
            self.ostatok = self.ostatok[dlina:]
            return
