"""Борт, который подписывает каждый отсчёт и не гарантирует доставку.

Третий протокол рядом с EGTS и Wialon IPS. Наружу класс выглядит так же
(`send`, `pump`, `set_online`, `buffer`, `dropped`), поэтому симулятор про
разницу не знает: протокол выбирается переменной окружения.

Чем ведёт себя иначе и почему это осмысленно:

* **подтверждений нет.** Одно заявление это одна дейтаграмма UDP. Борт не
  ждёт ответа, не держит соединение и не знает, дошло ли. Взамен он не может
  и «зависнуть на подтверждении», как это делает терминал с гарантией;
* **буфер ограничен не размером, а временем.** У отсчёта есть срок годности,
  и протухшее не досылается никогда. Это не экономия, а отказ делать вред:
  координата получасовой давности на диспетчерском экране выглядит как
  текущая, и по ней принимают решения;
* **каждый отсчёт подписан ключом борта.** Подпись едет вместе с данными, и
  проверяют её не здесь и не на шлюзе, а в приёмнике показателей, за шиной.

🔴 Отсюда главное свойство, ради которого протокол и добавлен: после
восстановления канала подписанный парк НЕ заливает очередь. Терминал с
гарантией доставки после получасового обрыва вываливает получасовой буфер
разом, и приёмник разбирает его вместе с текущими данными. Здесь уезжает
только то, что ещё годно, а остальное честно посчитано как потерянное.

⚠️ События смены по этому протоколу не идут, как не идут по EGTS и Wialon:
их формирует система карьера, а не борт.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque

import kluchi
import podpis
import skhema
import zayavlenie


class StatementUplink:
    """Канал борта на подписанных заявлениях."""

    def __init__(self, producer, host: str, port: int, park: str,
                 bufer_max: int = 60000, zhizn_sek: float = zayavlenie.ZHIZN_SEK):
        self.producer = producer
        self.host = host
        self.port = port
        self.park = park
        self.bufer_max = bufer_max
        self.zhizn_sek = zhizn_sek
        self.tema = zayavlenie.tema_parka(park)

        self.buffer: deque = deque()        # (годно до, номер борта, байты заявления)
        self.kafka_buffer: deque = deque()
        self.online = True
        self.dropped = 0        # не поместилось в буфер
        self.ustarelo = 0       # протухло раньше, чем нашёлся канал
        self.otpravleno = 0
        # Выброшенное считается по бортам: отчёт подписывает та машина, чьи
        # отсчёты пропали, и подписать его за неё некому.
        self.ustarelo_borta: dict[int, int] = {}
        self.otchitano: dict[int, int] = {}
        self.lock = threading.Lock()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.adres: tuple[str, int] | None = None
        self.adres_do = 0.0          # когда перечитать имя шлюза заново
        self.sleduyushchaya_popytka = 0.0
        # Ключ борта рождается при первом отсчёте машины и дальше живёт в
        # памяти: на площадке он лежит в самом терминале. Открытая часть
        # считается один раз вместе с закрытой: вывод её из семени стоит
        # столько же, сколько сама подпись, а меняться ей не с чего.
        self.kluchi_borta: dict[int, tuple[bytes, bytes]] = {}

    # ------------------------------------------------------------- отправка

    def send(self, topic: str, payload: dict):
        payload["t_wall"] = time.time()
        if payload.get("kind") in ("truck", "excavator"):
            godno_do = int(payload["t_wall"] + self.zhizn_sek)
            oid = skhema.nomer_borta(self.park, payload["kind"], payload["id"])
            syroye = self._v_zayavlenie(payload, godno_do, oid)
            with self.lock:
                self.buffer.append((godno_do, oid, syroye))
                while len(self.buffer) > self.bufer_max:
                    self.buffer.popleft()
                    self.dropped += 1
            return
        telo = (topic, json.dumps(payload, ensure_ascii=False).encode())
        with self.lock:
            self.kafka_buffer.append(telo)
        self._slit_kafka()

    def pump(self, wall_dt: float):
        self._slit_kafka()
        seychas = time.time()
        while True:
            with self.lock:
                if not self.buffer:
                    return
                godno_do, oid, syroye = self.buffer[0]
                # Протухшее выбрасывается независимо от того, поднят канал или
                # нет: иначе после обрыва парк выплюнул бы весь накопленный
                # буфер, а протокол ровно этого и не должен делать.
                if godno_do < seychas:
                    self.buffer.popleft()
                    self.ustarelo += 1
                    self.ustarelo_borta[oid] = self.ustarelo_borta.get(oid, 0) + 1
                    continue
                if not self.online:
                    return
                self.buffer.popleft()
            if not self._otpravit(syroye):
                # Дейтаграмму отправить не удалось совсем (нет адреса, нет
                # сети). Возвращать её в буфер незачем: к следующей попытке
                # она всё равно протухнет, а место займёт.
                with self.lock:
                    self.dropped += 1
                return

    def set_online(self, value: bool):
        with self.lock:
            bylo = self.online
            self.online = value
        if value and not bylo:
            # Канал вернулся. Первое, что уходит наверх, это не координаты, а
            # отчёт каждого борта о выброшенном: приёмник иначе видит просто
            # разрыв в потоке и не может отличить выброшенное от поломки.
            self._otchitatsya()

    def _otchitatsya(self):
        otchety = []
        seychas = time.time()
        with self.lock:
            v_ocheredi = len(self.buffer)
            for oid, ustarelo in self.ustarelo_borta.items():
                if ustarelo <= self.otchitano.get(oid, 0):
                    continue
                self.otchitano[oid] = ustarelo
                otchety.append((oid, ustarelo))
        for oid, ustarelo in otchety:
            telo = zayavlenie.sobrat_otchet(oid, seychas,
                                            int(seychas + self.zhizn_sek),
                                            ustarelo, v_ocheredi)
            # Приоритет аварийный: отчёт о потере данных должен пережить
            # переполнение приёмного буфера, ради которого потеря и случилась.
            z = zayavlenie.Zayavlenie(telo=telo, tema=self.tema,
                                      prioritet=zayavlenie.PRIORITET_AVARIYA)
            semya, z.klyuch = self._klyuchi(oid)
            z.podpis = podpis.podpisat(semya, z.material())
            with self.lock:
                self.buffer.appendleft((int(seychas + self.zhizn_sek), oid, z.v_bayty()))

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
                    self.dropped += 1

    def _klyuchi(self, oid: int) -> tuple[bytes, bytes]:
        para = self.kluchi_borta.get(oid)
        if para is None:
            semya = kluchi.semya_borta(oid)
            para = (semya, podpis.otkrytyy_klyuch(semya))
            self.kluchi_borta[oid] = para
        return para

    def _v_zayavlenie(self, payload: dict, godno_do: int, oid: int) -> bytes:
        telo = zayavlenie.sobrat_telo(self.park, payload, godno_do)
        prioritet = (zayavlenie.PRIORITET_AVARIYA
                     if payload.get("state") == "down"
                     else zayavlenie.PRIORITET_OBYCHNYY)
        z = zayavlenie.Zayavlenie(telo=telo, prioritet=prioritet, tema=self.tema)
        semya, z.klyuch = self._klyuchi(oid)
        z.podpis = podpis.podpisat(semya, z.material())
        return z.v_bayty()

    # Как часто перечитывать имя приёмника, секунды.
    #
    # 🔴 Найдено на стенде, и это главная грабля протокола без подтверждений.
    # Шлюз пересоздали, у него сменился адрес, борт продолжил слать дейтаграммы
    # по старому. Ошибки нет: UDP не отвечает ничем. На борту при этом идеальная
    # картина, буфер пуст и потерь ноль, а данных нет вообще. Терминал на EGTS
    # или Wialon узнал бы об этом сразу, у него рвётся соединение.
    #
    # Отсюда правило: у протокола без подтверждений имя приёмника перечитывается
    # по часам, а молчание парка ловится не бортом, а тревогой на приёмной
    # стороне. Борт про своё молчание узнать не может в принципе.
    POVTOR_ADRESA_SEK = 30.0

    def _otpravit(self, syroye: bytes) -> bool:
        if self.adres is not None and time.monotonic() > self.adres_do:
            self.adres = None
        if self.adres is None:
            if time.monotonic() < self.sleduyushchaya_popytka:
                return False
            try:
                svedeniya = socket.getaddrinfo(self.host, self.port,
                                               socket.AF_INET, socket.SOCK_DGRAM)
                self.adres = svedeniya[0][4]
                self.adres_do = time.monotonic() + self.POVTOR_ADRESA_SEK
            except OSError:
                # ⚠️ Имя шлюза может ещё не разрешаться: при общем старте
                # борт поднимается раньше приёмника. Для UDP это не ошибка
                # соединения, а просто некуда слать.
                self.sleduyushchaya_popytka = time.monotonic() + 2.0
                return False
        try:
            self.sock.sendto(syroye, self.adres)
            self.otpravleno += 1
            return True
        except OSError:
            self.adres = None
            self.sleduyushchaya_popytka = time.monotonic() + 2.0
            return False
