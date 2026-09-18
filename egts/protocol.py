"""Кодек EGTS: тот протокол, на котором в России говорят бортовые терминалы.

EGTS описан ГОСТ Р 54619 и приказом Минтранса: на нём работают терминалы
ЭРА-ГЛОНАСС и большая часть телематики, которая стоит на карьерной технике.
Здесь реализовано подмножество, которого хватает для парка машин:

    EGTS_PT_APPDATA          данные от борта
    EGTS_PT_RESPONSE         подтверждение от сервера
    EGTS_SR_TERM_IDENTITY    борт представляется при подключении
    EGTS_SR_POS_DATA         координаты, скорость, курс, время
    EGTS_SR_AD_SENSORS_DATA  датчики: состояние, счётчики, тонны
    EGTS_SR_RECORD_RESPONSE  ответ на каждую запись

Чего здесь нет: шифрования, сжатия, маршрутизации между серверами, команд на
борт. На площадке это обычно и не включают, а стенду добавило бы кода без
единого нового доказательства.

⚠️ Время в EGTS отсчитывается не от 1970 года, а от 00:00:00 01.01.2010 UTC.
Перепутать легко, а выглядит потом как «терминал из будущего».
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Начало отсчёта времени в EGTS
EGTS_EPOCH = 1262304000  # 2010-01-01 00:00:00 UTC в обычных секундах

# Типы пакетов транспортного уровня
PT_RESPONSE = 0
PT_APPDATA = 1

# Службы
SERVICE_AUTH = 1
SERVICE_TELEDATA = 2

# Типы подзаписей
SR_RECORD_RESPONSE = 0
SR_TERM_IDENTITY = 1
SR_POS_DATA = 16
SR_AD_SENSORS_DATA = 18

# Коды результата
RESULT_OK = 0

# Флаги записи
RFL_OBFE = 0x01  # есть идентификатор объекта
RFL_EVFE = 0x02  # есть идентификатор события
RFL_TMFE = 0x04  # есть время


class EgtsError(Exception):
    """Пакет разобрать не удалось."""


class NeedMoreData(EgtsError):
    """Пакет пришёл не целиком: в TCP это норма, ждём остаток."""


# --------------------------------------------------------------- контрольные суммы

def _tablica_crc8() -> list[int]:
    """CRC-8 с полиномом x^8+x^5+x^4+1, как в ГОСТ на EGTS."""
    tablica = []
    for bayt in range(256):
        znachenie = bayt
        for _ in range(8):
            znachenie = ((znachenie << 1) ^ 0x31) & 0xFF if znachenie & 0x80 else (znachenie << 1) & 0xFF
        tablica.append(znachenie)
    return tablica


_CRC8 = _tablica_crc8()


def crc8(dannye: bytes) -> int:
    znachenie = 0xFF
    for bayt in dannye:
        znachenie = _CRC8[znachenie ^ bayt]
    return znachenie


def crc16(dannye: bytes) -> int:
    """CRC-16/CCITT-FALSE: полином 0x1021, начальное значение 0xFFFF."""
    znachenie = 0xFFFF
    for bayt in dannye:
        znachenie ^= bayt << 8
        for _ in range(8):
            znachenie = ((znachenie << 1) ^ 0x1021) & 0xFFFF if znachenie & 0x8000 else (znachenie << 1) & 0xFFFF
    return znachenie


# --------------------------------------------------------------- подзаписи

@dataclass
class Poziciya:
    """EGTS_SR_POS_DATA: где машина, как быстро едет и куда смотрит."""

    vremya: int          # обычные секунды (не EGTS): переводим внутри
    shirota: float
    dolgota: float
    skorost: float = 0.0  # км/ч
    kurs: int = 0         # градусы
    probeg_km: float = 0.0
    din: int = 0          # дискретные входы, битовая маска

    def v_bayty(self) -> bytes:
        ntm = max(0, int(self.vremya) - EGTS_EPOCH)
        lat = int(abs(self.shirota) / 90.0 * 0xFFFFFFFF)
        lon = int(abs(self.dolgota) / 180.0 * 0xFFFFFFFF)
        # Полушария кодируются флагами, а не знаком: LAHS для южного,
        # LOHS для западного.
        flg = 0x01  # VLD: координаты достоверны
        if self.shirota < 0:
            flg |= 0x20
        if self.dolgota < 0:
            flg |= 0x40
        skorost = min(int(round(self.skorost * 10)), 0x3FFF)
        if self.kurs > 255:
            skorost |= 0x8000  # старший бит курса живёт в поле скорости
        return struct.pack(
            "<IIIBHB3sBB",
            ntm, lat, lon, flg,
            skorost, self.kurs & 0xFF,
            int(self.probeg_km * 10).to_bytes(3, "little"),
            self.din, 0,
        )

    @classmethod
    def iz_baytov(cls, dannye: bytes) -> "Poziciya":
        if len(dannye) < 21:
            raise EgtsError("короткая подзапись позиции")
        ntm, lat, lon, flg, spd, dir_, odm, din, _src = struct.unpack("<IIIBHB3sBB", dannye[:21])
        shirota = lat / 0xFFFFFFFF * 90.0
        dolgota = lon / 0xFFFFFFFF * 180.0
        if flg & 0x20:
            shirota = -shirota
        if flg & 0x40:
            dolgota = -dolgota
        kurs = dir_ + (256 if spd & 0x8000 else 0)
        return cls(
            vremya=ntm + EGTS_EPOCH,
            shirota=shirota,
            dolgota=dolgota,
            skorost=(spd & 0x3FFF) / 10.0,
            kurs=kurs,
            probeg_km=int.from_bytes(odm, "little") / 10.0,
            din=din,
        )


@dataclass
class Datchiki:
    """EGTS_SR_AD_SENSORS_DATA: показания датчиков борта.

    На реальной машине сюда попадают уровень топлива, вес ковша, положение
    кузова. У нас тем же путём едут состояние машины, счётчики и часы карьера:
    борт передаёт то, что знает о себе, а смысл им придаёт уже сервер.
    """

    analogovye: dict[int, int] = field(default_factory=dict)  # номер 1..8 -> значение (24 бита)
    diskretnye: int = 0

    def v_bayty(self) -> bytes:
        dioe = 0x01 if self.diskretnye else 0x00
        asfe = 0
        for nomer in self.analogovye:
            asfe |= 1 << (nomer - 1)
        kusochki = [struct.pack("<BBB", dioe, 0, asfe)]
        if dioe:
            kusochki.append(struct.pack("<B", self.diskretnye & 0xFF))
        for nomer in range(1, 9):
            if asfe & (1 << (nomer - 1)):
                znachenie = max(0, min(self.analogovye[nomer], 0xFFFFFF))
                kusochki.append(znachenie.to_bytes(3, "little"))
        return b"".join(kusochki)

    @classmethod
    def iz_baytov(cls, dannye: bytes) -> "Datchiki":
        if len(dannye) < 3:
            raise EgtsError("короткая подзапись датчиков")
        dioe, _dout, asfe = struct.unpack("<BBB", dannye[:3])
        smeshchenie = 3
        diskretnye = 0
        if dioe & 0x01:
            diskretnye = dannye[smeshchenie]
            smeshchenie += 1
        analogovye = {}
        for nomer in range(1, 9):
            if asfe & (1 << (nomer - 1)):
                analogovye[nomer] = int.from_bytes(dannye[smeshchenie:smeshchenie + 3], "little")
                smeshchenie += 3
        return cls(analogovye=analogovye, diskretnye=diskretnye)


@dataclass
class Predstavlenie:
    """EGTS_SR_TERM_IDENTITY: борт сообщает, кто он."""

    terminal: int

    def v_bayty(self) -> bytes:
        return struct.pack("<IB", self.terminal, 0)

    @classmethod
    def iz_baytov(cls, dannye: bytes) -> "Predstavlenie":
        if len(dannye) < 5:
            raise EgtsError("короткая подзапись представления")
        (terminal,) = struct.unpack("<I", dannye[:4])
        return cls(terminal=terminal)


@dataclass
class Podzapis:
    tip: int
    dannye: bytes

    def v_bayty(self) -> bytes:
        return struct.pack("<BH", self.tip, len(self.dannye)) + self.dannye


@dataclass
class Zapis:
    """EGTS_RECORD: одна запись от одного объекта."""

    nomer: int
    obyekt: int | None
    podzapisi: list[Podzapis]
    sluzhba: int = SERVICE_TELEDATA
    vremya: int | None = None

    def v_bayty(self) -> bytes:
        telo = b"".join(p.v_bayty() for p in self.podzapisi)
        flagi = 0
        dopolnitelno = b""
        if self.obyekt is not None:
            flagi |= RFL_OBFE
            dopolnitelno += struct.pack("<I", self.obyekt)
        if self.vremya is not None:
            flagi |= RFL_TMFE
            dopolnitelno += struct.pack("<I", max(0, int(self.vremya) - EGTS_EPOCH))
        zagolovok = struct.pack("<HHB", len(telo), self.nomer, flagi) + dopolnitelno
        return zagolovok + struct.pack("<BB", self.sluzhba, self.sluzhba) + telo


def razobrat_zapisi(dannye: bytes) -> list[Zapis]:
    """Разбор записей из тела пакета."""
    zapisi = []
    smeshchenie = 0
    while smeshchenie < len(dannye):
        if len(dannye) - smeshchenie < 5:
            break
        dlina, nomer, flagi = struct.unpack("<HHB", dannye[smeshchenie:smeshchenie + 5])
        smeshchenie += 5
        obyekt = None
        vremya = None
        if flagi & RFL_OBFE:
            (obyekt,) = struct.unpack("<I", dannye[smeshchenie:smeshchenie + 4])
            smeshchenie += 4
        if flagi & RFL_EVFE:
            smeshchenie += 4
        if flagi & RFL_TMFE:
            (syroe,) = struct.unpack("<I", dannye[smeshchenie:smeshchenie + 4])
            vremya = syroe + EGTS_EPOCH
            smeshchenie += 4
        if len(dannye) - smeshchenie < 2:
            break
        sluzhba = dannye[smeshchenie]
        smeshchenie += 2
        telo = dannye[smeshchenie:smeshchenie + dlina]
        smeshchenie += dlina

        podzapisi = []
        vnutri = 0
        while vnutri + 3 <= len(telo):
            tip, dlina_p = struct.unpack("<BH", telo[vnutri:vnutri + 3])
            vnutri += 3
            podzapisi.append(Podzapis(tip=tip, dannye=telo[vnutri:vnutri + dlina_p]))
            vnutri += dlina_p
        zapisi.append(Zapis(nomer=nomer, obyekt=obyekt, podzapisi=podzapisi,
                            sluzhba=sluzhba, vremya=vremya))
    return zapisi


# --------------------------------------------------------------- пакеты

def sobrat_paket(tip: int, nomer: int, telo: bytes) -> bytes:
    """Транспортный уровень: заголовок, тело, две контрольные суммы."""
    zagolovok = struct.pack(
        "<BBBBBHHB",
        0x01,        # версия протокола
        0x00,        # идентификатор ключа
        0x00,        # флаги: без шифрования, сжатия и маршрутизации
        11,          # длина заголовка
        0x00,        # кодирование заголовка
        len(telo),
        nomer & 0xFFFF,
        tip,
    )
    zagolovok += bytes([crc8(zagolovok)])
    return zagolovok + telo + struct.pack("<H", crc16(telo))


def razobrat_paket(bufer: bytes) -> tuple[int, int, bytes, int]:
    """Возвращает (тип, номер, тело, сколько байт заняли).

    Кидает NeedMoreData, если пакет ещё не дошёл целиком: по TCP данные
    приходят кусками, и половина пакета это обычное дело, а не ошибка.
    """
    if len(bufer) < 11:
        raise NeedMoreData("нет заголовка")
    dlina_zagolovka = bufer[3]
    if dlina_zagolovka < 11 or len(bufer) < dlina_zagolovka:
        raise NeedMoreData("заголовок не целиком")
    if crc8(bufer[:dlina_zagolovka - 1]) != bufer[dlina_zagolovka - 1]:
        raise EgtsError("не сходится контрольная сумма заголовка")
    dlina_tela, nomer = struct.unpack("<HH", bufer[5:9])
    tip = bufer[9]
    vsego = dlina_zagolovka + dlina_tela + (2 if dlina_tela else 0)
    if len(bufer) < vsego:
        raise NeedMoreData("тело не целиком")
    telo = bufer[dlina_zagolovka:dlina_zagolovka + dlina_tela]
    if dlina_tela:
        (summa,) = struct.unpack("<H", bufer[dlina_zagolovka + dlina_tela:vsego])
        if crc16(telo) != summa:
            raise EgtsError("не сходится контрольная сумма тела")
    return tip, nomer, telo, vsego


def sobrat_otvet(nomer_paketa: int, nomera_zapisey: list[int], nomer_svoy: int) -> bytes:
    """EGTS_PT_RESPONSE: подтверждение приёма.

    Без подтверждения борт обязан считать данные недоставленными и слать их
    заново. Терминал, которому не отвечают, копит буфер до отказа.
    """
    zapisi = b""
    for nomer in nomera_zapisey:
        podzapis = Podzapis(SR_RECORD_RESPONSE, struct.pack("<HB", nomer, RESULT_OK))
        zapisi += Zapis(nomer=nomer, obyekt=None, podzapisi=[podzapis],
                        sluzhba=SERVICE_TELEDATA).v_bayty()
    telo = struct.pack("<HB", nomer_paketa & 0xFFFF, RESULT_OK) + zapisi
    return sobrat_paket(PT_RESPONSE, nomer_svoy, telo)
