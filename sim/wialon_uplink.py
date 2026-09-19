"""Борт, который говорит по Wialon IPS.

Тот же парк, что и в `egts_uplink`, но другой протокол. Наружу класс выглядит
одинаково (`send`, `pump`, `set_online`, `buffer`, `dropped`), поэтому
симулятор про разницу не знает: он переключается переменной окружения.

🔴 Главное отличие от EGTS, и оно не в синтаксисе. В EGTS один канал несёт
записи многих объектов: номер борта едет в каждой записи. В Wialon IPS
объект называется ОДИН раз при входе, и дальше всё соединение принадлежит
ему. Значит парк на этом протоколе держит столько соединений, сколько машин,
и каждое живёт своей жизнью: своя очередь неотправленного, свой вход после
обрыва. На площадке так и есть, там каждый терминал сам себе клиент.

Что взято от настоящего терминала:

* запись хранится, пока сервер не подтвердил её приём;
* после обрыва накопленное уходит пачкой в пакете чёрного ящика `#B#`,
  это штатный механизм протокола, а не самодеятельность;
* соединение восстанавливается само, со входом заново.

⚠️ События смены (рейс закрыт, простой начался) по Wialon IPS не идут, как
не идут и по EGTS: их формирует система, а не терминал.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque

import ips
import skhema

# Сколько записей уходит в одном пакете чёрного ящика. Слишком большая пачка
# упирается в размер буфера приёмника, слишком мелкая догоняет вечность.
V_PACHKE = 40
PAUZA_POVTORA = 3.0


class _Terminal:
    """Один бортовой терминал: одно соединение, один объект, свой поток.

    🔴 Поток у каждого терминала свой, и это не расточительство. Сначала все
    терминалы обслуживал один поток по кругу, и каждому приходилось ждать
    ответа сервера по очереди: очередь записей копилась волнами, а задержка
    доставки держалась вдвое выше, чем у соседнего парка на EGTS. На площадке
    терминалы тем более не ждут друг друга, это отдельные устройства.
    """

    def __init__(self, host: str, port: int, imei: str, bufer_max: int, zhivo):
        self.host = host
        self.port = port
        self.imei = imei
        self.bufer_max = bufer_max
        self.zhivo = zhivo          # канал парка поднят: общий обрыв связи
        self.buffer: deque[str] = deque()
        self.dropped = 0
        self.sock: socket.socket | None = None
        self.ostatok = ""
        self.sleduyushchaya_popytka = 0.0
        self.potok = threading.Thread(target=self._rabotat, daemon=True)
        self.potok.start()

    def _rabotat(self):
        while True:
            if self.zhivo() and self.buffer:
                for _ in range(4):
                    if not self.otpravit_pachku():
                        break
            # Темп терминала. Чаще нет смысла, реже растёт задержка доставки,
            # по которой стенд меряет обещание заказчику.
            time.sleep(0.02)

    def polozhit(self, telo: str):
        self.buffer.append(telo)
        while len(self.buffer) > self.bufer_max:
            self.buffer.popleft()
            self.dropped += 1

    def otklyuchit(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.ostatok = ""

    def _soedinit(self) -> bool:
        if self.sock is not None:
            return True
        if time.time() < self.sleduyushchaya_popytka:
            return False
        try:
            sock = socket.create_connection((self.host, self.port), timeout=5)
            sock.settimeout(5)
            # ⚠️ Без этого ядро придерживает мелкие пакеты, ожидая, что к ним
            # добавится ещё данных (алгоритм Нагла). Протокол здесь строго
            # «запрос, ответ», добавлять нечего, и каждая пачка получает
            # лишние десятки миллисекунд. На стенде это подняло задержку
            # приёма у парка вдвое против соседнего парка на EGTS.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.sendall(ips.paket_vhoda(self.imei).encode())
            otvet = self._prochitat(sock)
            # Сервер, ответивший «0», отказал во входе: слать данные в такое
            # соединение бессмысленно, и молчаливая досылка в никуда это
            # худший вид потери телеметрии.
            if otvet is None or not otvet.startswith("#AL#1"):
                sock.close()
                self.sleduyushchaya_popytka = time.time() + PAUZA_POVTORA
                return False
            self.sock = sock
            return True
        except OSError:
            self.sleduyushchaya_popytka = time.time() + PAUZA_POVTORA
            return False

    def _prochitat(self, sock: socket.socket) -> str | None:
        try:
            while "\r\n" not in self.ostatok:
                kusok = sock.recv(4096)
                if not kusok:
                    return None
                self.ostatok += kusok.decode("ascii", "replace")
            stroka, self.ostatok = self.ostatok.split("\r\n", 1)
            return stroka + "\r\n"
        except OSError:
            return None

    def otpravit_pachku(self) -> bool:
        """Одна пачка. Возвращает True, если есть смысл продолжать."""
        if not self.buffer or not self._soedinit():
            return False
        pachka = [self.buffer[i] for i in range(min(V_PACHKE, len(self.buffer)))]
        try:
            if len(pachka) == 1:
                self.sock.sendall(ips.paket_dannyh(pachka[0]).encode())
                zhdem = "#AD#"
            else:
                self.sock.sendall(ips.paket_chernogo_yashchika(pachka).encode())
                zhdem = "#AB#"
            otvet = self._prochitat(self.sock)
        except OSError:
            self.otklyuchit()
            return False

        if otvet is None or not otvet.startswith(zhdem):
            self.otklyuchit()
            return False

        # Сколько записей сервер подтвердил. На #AB# он отвечает числом, и
        # оно может быть меньше отправленного: неподтверждённое остаётся в
        # буфере и уедет следующей пачкой.
        prinyato = len(pachka)
        hvost = otvet.strip().split("#")[-1]
        if zhdem == "#AB#" and hvost.isdigit():
            prinyato = min(int(hvost), len(pachka))
        elif zhdem == "#AD#" and hvost != "1":
            prinyato = 0
        for _ in range(prinyato):
            self.buffer.popleft()
        return prinyato > 0


class WialonUplink:
    """Парк терминалов Wialon IPS с общим интерфейсом канала."""

    def __init__(self, producer, host: str, port: int, park: str,
                 bufer_max: int = 60000):
        self.producer = producer
        self.host = host
        self.port = port
        self.park = park
        # Буфер задан на парк, а делится по машинам: иначе одна застрявшая
        # машина съела бы память за всех.
        self.na_terminal = max(200, bufer_max // 12)
        self.terminaly: dict[str, _Terminal] = {}
        self.kafka_buffer: deque = deque()
        self.online = True
        self.lock = threading.Lock()

        # 🔴 Отправка вынесена из такта модели, и это не оптимизация, а
        # исправление найденной на стенде ошибки. Сначала терминалы слались
        # прямо в такте, как в версии для EGTS. Но у EGTS одно соединение на
        # парк и один обмен с сервером за такт, а здесь их столько, сколько
        # машин: десять ожиданий ответа шестьдесят раз в секунду. Модель
        # отстала ВТРОЕ, при этом буфер оставался пустым и ни одна метрика
        # канала не жаловалась: часы карьера просто шли медленнее.
        # Дальше отправкой занимается сам терминал, каждый в своём потоке.

    # -------------------------------------------------------- как у EGTS

    @property
    def buffer(self) -> list:
        """Общий размер очереди парка: столько же, сколько показывает EGTS."""
        with self.lock:
            return [z for t in self.terminaly.values() for z in t.buffer]

    @property
    def dropped(self) -> int:
        with self.lock:
            return sum(t.dropped for t in self.terminaly.values())

    def send(self, topic: str, payload: dict):
        payload["t_wall"] = time.time()
        if payload.get("kind") in ("truck", "excavator"):
            imei, telo = self._v_zapis(payload)
            with self.lock:
                terminal = self.terminaly.get(imei)
                if terminal is None:
                    terminal = _Terminal(self.host, self.port, imei,
                                         self.na_terminal, lambda: self.online)
                    self.terminaly[imei] = terminal
                terminal.polozhit(telo)
            return
        telo = (topic, json.dumps(payload, ensure_ascii=False).encode())
        with self.lock:
            self.kafka_buffer.append(telo)
        self._slit_kafka()

    def pump(self, wall_dt: float):
        """События смены в шину. Телеметрию шлёт каждый терминал сам."""
        self._slit_kafka()

    def set_online(self, value: bool):
        with self.lock:
            self.online = value
            if not value:
                # Обрыв канала это не закрытие приложения: сокеты рвутся
                # молча, и терминалы узнают об этом по отсутствию ответов.
                for terminal in self.terminaly.values():
                    terminal.otklyuchit()

    # ------------------------------------------------------------ внутреннее

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
                    pass

    def _v_zapis(self, payload: dict) -> tuple[str, str]:
        tip = payload["kind"]
        oid = skhema.nomer_borta(self.park, tip, payload["id"])
        shirota, dolgota = skhema.v_gradusy(payload.get("x", 0.0), payload.get("y", 0.0))
        vremya = payload["t_wall"]

        params: dict[str, float] = {
            ips.P_T_SIM: float(payload.get("t_sim", 0.0)),
            # Миллисекунды отдельным параметром: поле времени в протоколе
            # секундное, а задержку доставки стенд меряет в долях секунды.
            ips.P_MS: int(round((vremya - int(vremya)) * 1000)),
        }
        if tip == "truck":
            params.update({
                ips.P_STATE: skhema.SOSTOYANIYA_TRUCK.get(payload.get("state", ""), 0),
                ips.P_FACE: skhema.ZABOI.get(payload.get("face") or "", 0),
                ips.P_CYCLES: int(payload.get("cycles", 0)),
                ips.P_LOADED: 1 if payload.get("loaded") else 0,
            })
        else:
            params.update({
                ips.P_STATE: skhema.SOSTOYANIYA_EXC.get(payload.get("state", ""), 0),
                ips.P_QUEUE: int(payload.get("queue", 0)),
                ips.P_TONS: int(round(float(payload.get("tons", 0.0)) * 10)),
                ips.P_IDLE: int(payload.get("idle_sec", 0)),
                ips.P_BUSY: int(payload.get("busy_sec", 0)),
            })

        telo = ips.telo_dannyh(shirota, dolgota, vremya, params=params)
        return str(oid), telo
