"""Что именно борт кладёт в поля EGTS и как это читать обратно.

Файл один на обе стороны: им пользуются и симулятор борта, и шлюз. Разъедутся
эти две стороны, и данные поедут молча неправильные, а не с ошибкой.

Логика раскладки такая же, как на площадке: борт передаёт СВОИ показания
(где он, как быстро едет, что показывают датчики), а смысл им придаёт сервер
по справочнику. Ни слова «самосвал» в эфире нет, есть номер борта.
"""
from __future__ import annotations

import math

# Опорная точка карьера. Условная: стенд не привязан к конкретному разрезу,
# а модель считает в метрах на плане, поэтому координаты пересчитываются
# относительно этой точки.
BAZA_SHIROTA = float(55.0)
BAZA_DOLGOTA = float(60.0)
METROV_V_GRADUSE = 111320.0

# Номер борта: парк, тип техники, номер машины. На площадке номер борта тоже
# несёт смысл: по нему сразу понятно, чья машина и какого типа.
PARKI = {"fixed": 1, "balanced": 2}
PARKI_OBRATNO = {kod: imya for imya, kod in PARKI.items()}
TIPY = {"truck": 1, "excavator": 2}
TIPY_OBRATNO = {kod: imya for imya, kod in TIPY.items()}

SOSTOYANIYA_TRUCK = {
    "to_face": 1, "queue": 2, "loading": 3,
    "to_dump": 4, "dumping": 5, "down": 6,
}
SOSTOYANIYA_TRUCK_OBRATNO = {kod: imya for imya, kod in SOSTOYANIYA_TRUCK.items()}

SOSTOYANIYA_EXC = {"idle": 1, "loading": 2, "down": 3}
SOSTOYANIYA_EXC_OBRATNO = {kod: imya for imya, kod in SOSTOYANIYA_EXC.items()}

# Забои нумеруются, ноль означает «не назначен».
ZABOI = {"EX-01": 1, "EX-02": 2}
ZABOI_OBRATNO = {kod: imya for imya, kod in ZABOI.items()}

# Номера аналоговых датчиков. У самосвала и экскаватора они означают разное:
# так же, как на площадке один и тот же вход терминала на разной технике
# подключён к разным приборам.
ADS_T_SIM = 1        # часы карьера, секунды
ADS_TRUCK_CYCLES = 2
ADS_TRUCK_STATE = 3
ADS_TRUCK_FACE = 4
ADS_EXC_QUEUE = 2
ADS_EXC_STATE = 3
ADS_EXC_TONS = 4     # тонны, умноженные на 10
ADS_EXC_IDLE = 5     # секунды простоя
ADS_EXC_BUSY = 6     # секунды работы
# ⚠️ Время в EGTS секундное, а стенд меряет задержку доставки в долях секунды:
# на секундной сетке этот показатель просто не виден. Поэтому миллисекунды
# едут отдельным датчиком. На площадке так же поступают, когда нужна точность
# выше, чем даёт стандартное поле времени.

ADS_MS = 7           # миллисекунды времени формирования записи

DIN_LOADED = 0x01    # кузов гружён


def nomer_borta(park: str, tip: str, imya: str) -> int:
    """«balanced», «truck», «BAL-03» -> 210003."""
    nomer = 0
    hvost = imya.rsplit("-", 1)[-1]
    if hvost.isdigit():
        nomer = int(hvost)
    return PARKI.get(park, 9) * 100000 + TIPY.get(tip, 9) * 10000 + nomer


def razobrat_nomer(oid: int) -> tuple[str, str, str]:
    """210003 -> («balanced», «truck», «BAL-03»)."""
    park = PARKI_OBRATNO.get(oid // 100000, "unknown")
    tip = TIPY_OBRATNO.get((oid // 10000) % 10, "unknown")
    nomer = oid % 10000
    if tip == "excavator":
        imya = f"EX-{nomer:02d}"
    else:
        imya = f"{park[:3].upper()}-{nomer:02d}"
    return park, tip, imya


def v_gradusy(x: float, y: float) -> tuple[float, float]:
    """План карьера в метрах -> широта и долгота."""
    shirota = BAZA_SHIROTA + y / METROV_V_GRADUSE
    dolgota = BAZA_DOLGOTA + x / (METROV_V_GRADUSE * math.cos(math.radians(BAZA_SHIROTA)))
    return shirota, dolgota


def v_metry(shirota: float, dolgota: float) -> tuple[float, float]:
    """Широта и долгота -> план карьера в метрах."""
    y = (shirota - BAZA_SHIROTA) * METROV_V_GRADUSE
    x = (dolgota - BAZA_DOLGOTA) * METROV_V_GRADUSE * math.cos(math.radians(BAZA_SHIROTA))
    return x, y
