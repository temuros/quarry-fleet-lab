"""Проверка кодека EGTS без сети, Kafka и Docker: занимает секунду.

    python egts/selftest.py

Проверяется то, на чём протокол ломается молча: контрольные суммы, границы
пакета в потоке TCP, знаки полушарий, обратное чтение датчиков и сборка
сообщения, которое ждёт приёмник за шлюзом.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skhema  # noqa: E402
from protocol import (  # noqa: E402
    PT_APPDATA,
    SR_AD_SENSORS_DATA,
    SR_POS_DATA,
    Datchiki,
    EgtsError,
    NeedMoreData,
    Podzapis,
    Poziciya,
    Zapis,
    crc8,
    crc16,
    razobrat_paket,
    razobrat_zapisi,
    sobrat_otvet,
    sobrat_paket,
)

oshibki = []


def proverit(uslovie, opisanie):
    if uslovie:
        print(f"  ок    {opisanie}")
    else:
        print(f"  СБОЙ  {opisanie}")
        oshibki.append(opisanie)


def zapis_borta(oid, t_wall, t_sim, x, y, sostoyanie, cycles, gruzhen):
    analogovye = {
        skhema.ADS_T_SIM: int(t_sim),
        skhema.ADS_MS: int((t_wall - int(t_wall)) * 1000),
        skhema.ADS_TRUCK_CYCLES: cycles,
        skhema.ADS_TRUCK_STATE: skhema.SOSTOYANIYA_TRUCK[sostoyanie],
        skhema.ADS_TRUCK_FACE: skhema.ZABOI["EX-01"],
    }
    din = skhema.DIN_LOADED if gruzhen else 0
    shirota, dolgota = skhema.v_gradusy(x, y)
    poziciya = Poziciya(vremya=int(t_wall), shirota=shirota, dolgota=dolgota, din=din)
    return Zapis(
        nomer=1, obyekt=oid, vremya=int(t_wall),
        podzapisi=[
            Podzapis(SR_POS_DATA, poziciya.v_bayty()),
            Podzapis(SR_AD_SENSORS_DATA, Datchiki(analogovye=analogovye, diskretnye=din).v_bayty()),
        ],
    ).v_bayty()


print("контрольные суммы")
proverit(crc8(b"") == 0xFF, "CRC8 пустых данных это начальное значение")
proverit(crc16(b"123456789") == 0x29B1, "CRC16 на контрольной строке даёт 0x29B1")

print("\nпакет туда и обратно")
t_wall = time.time()
telo = zapis_borta(110003, t_wall, 3600, 250.0, -120.0, "to_dump", 7, True)
paket = sobrat_paket(PT_APPDATA, 42, telo)
tip, nomer, telo_obratno, dlina = razobrat_paket(paket)
proverit(tip == PT_APPDATA and nomer == 42, "тип и номер пакета читаются обратно")
proverit(dlina == len(paket), "длина пакета посчитана верно")
proverit(telo_obratno == telo, "тело не изменилось")

print("\nграницы пакета в потоке TCP")
try:
    razobrat_paket(paket[:7])
    proverit(False, "обрезанный пакет должен требовать продолжения")
except NeedMoreData:
    proverit(True, "обрезанный пакет требует продолжения, а не ошибка")
except EgtsError:
    proverit(False, "обрезанный пакет принят за битый")

sliplos = paket + paket
tip, _, _, dlina = razobrat_paket(sliplos)
proverit(dlina == len(paket), "из двух слипшихся пакетов разбирается первый")

bityy = bytearray(paket)
bityy[12] ^= 0xFF
try:
    razobrat_paket(bytes(bityy))
    proverit(False, "битое тело должно ловиться контрольной суммой")
except EgtsError:
    proverit(True, "битое тело ловится контрольной суммой")

print("\nсодержимое записи")
zapisi = razobrat_zapisi(telo_obratno)
proverit(len(zapisi) == 1, "запись одна")
zapis = zapisi[0]
proverit(zapis.obyekt == 110003, "номер борта на месте")
park, tip_tehniki, imya = skhema.razobrat_nomer(zapis.obyekt)
proverit((park, tip_tehniki, imya) == ("fixed", "truck", "FIX-03"),
         "номер борта разбирается в парк, тип и имя машины")

poziciya = Poziciya.iz_baytov(zapis.podzapisi[0].dannye)
x, y = skhema.v_metry(poziciya.shirota, poziciya.dolgota)
proverit(abs(x - 250.0) < 1.0 and abs(y + 120.0) < 1.0,
         f"координаты вернулись в метры плана: {x:.1f}, {y:.1f}")
proverit(poziciya.din & skhema.DIN_LOADED, "признак гружёного кузова дошёл")

datchiki = Datchiki.iz_baytov(zapis.podzapisi[1].dannye)
proverit(datchiki.analogovye[skhema.ADS_T_SIM] == 3600, "часы карьера дошли")
proverit(datchiki.analogovye[skhema.ADS_TRUCK_CYCLES] == 7, "счётчик рейсов дошёл")
proverit(skhema.SOSTOYANIYA_TRUCK_OBRATNO[datchiki.analogovye[skhema.ADS_TRUCK_STATE]] == "to_dump",
         "состояние машины дошло")

vosstanovlennoe = poziciya.vremya + datchiki.analogovye[skhema.ADS_MS] / 1000.0
proverit(abs(vosstanovlennoe - t_wall) < 0.002,
         f"время формирования записи восстановлено с точностью до миллисекунд "
         f"(разница {abs(vosstanovlennoe - t_wall) * 1000:.1f} мс)")

print("\nюжное и западное полушария")
yug = Poziciya(vremya=int(t_wall), shirota=-33.9, dolgota=-70.6)
obratno = Poziciya.iz_baytov(yug.v_bayty())
proverit(abs(obratno.shirota + 33.9) < 0.001 and abs(obratno.dolgota + 70.6) < 0.001,
         "отрицательные координаты не теряют знак")

print("\nподтверждение приёма")
otvet = sobrat_otvet(42, [1, 2, 3], 7)
tip, nomer, telo_otveta, _ = razobrat_paket(otvet)
proverit(tip == 0 and nomer == 7, "ответ собран как EGTS_PT_RESPONSE")
proverit(int.from_bytes(telo_otveta[:2], "little") == 42, "ответ ссылается на номер принятого пакета")

print()
if oshibki:
    print(f"СБОЕВ: {len(oshibki)}")
    sys.exit(1)
print("кодек EGTS в порядке")
