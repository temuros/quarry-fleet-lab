"""Проверка кодека Wialon IPS без сети, шины и Docker.

Прогон занимает меньше секунды, поэтому кодек проверяется до того, как
собран образ и поднят кластер. Ошибка в разборе координат или времени иначе
всплывает на дашборде в виде техники посреди океана, и искать её приходится
через весь поток данных.

    python wialon/selftest.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "egts"))

import ips  # noqa: E402
import skhema  # noqa: E402

oshibok = 0


def proverit(chto: str, uslovie: bool, podrobnosti: str = ""):
    global oshibok
    if uslovie:
        print(f"  ок    {chto}")
    else:
        oshibok += 1
        print(f"  СБОЙ  {chto} {podrobnosti}")


print("координаты: градусы и минуты как у терминала")
for shirota, dolgota in ((55.123456, 60.654321), (-33.5, -70.25), (0.0, 0.0)):
    lat, lat_s = ips.v_gradusy_minuty(shirota, dolgota=False)
    lon, lon_s = ips.v_gradusy_minuty(dolgota, dolgota=True)
    nazad_lat = ips.iz_gradusov_minut(lat, lat_s)
    nazad_lon = ips.iz_gradusov_minut(lon, lon_s)
    proverit(f"{shirota}, {dolgota} -> {lat}{lat_s} {lon}{lon_s} и обратно",
             abs(nazad_lat - shirota) < 1e-6 and abs(nazad_lon - dolgota) < 1e-6,
             f"получилось {nazad_lat}, {nazad_lon}")

# ⚠️ Долгота занимает три знака под градусы, широта два: перепутать значит
# получить разбор, который на малых углах работает, а на больших врёт.
lon, _ = ips.v_gradusy_minuty(9.5, dolgota=True)
proverit("долгота 9.5 пишется тремя знаками градусов", lon.startswith("009"), f"вышло {lon}")

print("вход терминала")
tip, telo = ips.razobrat_stroku(ips.paket_vhoda("210003"))
proverit("тип пакета входа L", tip == "L", f"вышло {tip}")
proverit("идентификатор читается", ips.razobrat_vhod(telo) == "210003")
proverit("версия 2.0 во входе не мешает",
         ips.razobrat_vhod("2.0;210003;NA;1A2B") == "210003")

print("пакет данных туда и обратно")
teper = float(int(time.time()))
shirota, dolgota = skhema.v_gradusy(1200.0, -450.0)
telo = ips.telo_dannyh(
    shirota, dolgota, teper, skorost=27, kurs=180,
    params={
        ips.P_T_SIM: 4321.5,
        ips.P_MS: 250,
        ips.P_STATE: skhema.SOSTOYANIYA_TRUCK["to_dump"],
        ips.P_FACE: skhema.ZABOI["EX-02"],
        ips.P_CYCLES: 7,
        ips.P_LOADED: 1,
    },
)
tip, syroye = ips.razobrat_stroku(ips.paket_dannyh(telo))
proverit("тип пакета данных D", tip == "D", f"вышло {tip}")
razobrannoe = ips.razobrat_dannye(syroye)
proverit("время восстановлено до секунды", abs(razobrannoe["vremya"] - teper) < 1.0,
         f"разница {razobrannoe['vremya'] - teper}")

soobshchenie = ips.soobshchenie("210003", razobrannoe)
proverit("парк и машина узнаны", soobshchenie["fleet"] == "balanced" and soobshchenie["id"] == "BAL-03",
         str(soobshchenie))
proverit("состояние читается", soobshchenie["state"] == "to_dump", str(soobshchenie["state"]))
proverit("забой читается", soobshchenie["face"] == "EX-02", str(soobshchenie["face"]))
proverit("кузов гружён", soobshchenie["loaded"] is True)
proverit("рейсы на месте", soobshchenie["cycles"] == 7)
proverit("миллисекунды не потеряны", abs(soobshchenie["t_wall"] - (teper + 0.25)) < 0.01,
         f"вышло {soobshchenie['t_wall'] - teper}")
proverit("координаты вернулись в метры плана",
         abs(soobshchenie["x"] - 1200.0) < 1.0 and abs(soobshchenie["y"] + 450.0) < 1.0,
         f"вышло {soobshchenie['x']}, {soobshchenie['y']}")

print("экскаватор")
telo = ips.telo_dannyh(
    shirota, dolgota, teper,
    params={
        ips.P_T_SIM: 100.0,
        ips.P_STATE: skhema.SOSTOYANIYA_EXC["loading"],
        ips.P_QUEUE: 3,
        ips.P_TONS: 1234,
        ips.P_IDLE: 60,
        ips.P_BUSY: 240,
    },
)
soobshchenie = ips.soobshchenie(str(skhema.nomer_borta("fixed", "excavator", "EX-01")),
                                ips.razobrat_dannye(ips.razobrat_stroku(ips.paket_dannyh(telo))[1]))
proverit("экскаватор узнан", soobshchenie["kind"] == "excavator" and soobshchenie["id"] == "EX-01",
         str(soobshchenie))
proverit("тонны делятся на десять", abs(soobshchenie["tons"] - 123.4) < 0.01, str(soobshchenie["tons"]))
proverit("очередь на месте", soobshchenie["queue"] == 3)

print("чёрный ящик")
pachka = ips.paket_chernogo_yashchika([telo, telo, telo])
tip, syroye = ips.razobrat_stroku(pachka)
proverit("тип пакета досылки B", tip == "B", f"вышло {tip}")
proverit("в пачке три записи", len(syroye.split("|")) == 3)

print("битые пакеты не роняют разбор")
for plohoy in ("", "мусор", "#", "#D#слишком;мало;полей"):
    try:
        tip, telo = ips.razobrat_stroku(plohoy)
        ips.razobrat_dannye(telo)
        proverit(f"«{plohoy}» отвергнут", False, "разобрался как верный")
    except ips.WialonError:
        proverit(f"«{plohoy}» отвергнут с понятной ошибкой", True)
    except Exception as oshibka:  # noqa: BLE001
        proverit(f"«{plohoy}» отвергнут", False, f"чужая ошибка {oshibka!r}")

print("ответы сервера")
proverit("на вход", ips.otvet("L") == "#AL#1\r\n", repr(ips.otvet("L")))
proverit("на данные", ips.otvet("D") == "#AD#1\r\n", repr(ips.otvet("D")))
proverit("на досылку", ips.otvet("B", 3) == "#AB#3\r\n", repr(ips.otvet("B", 3)))

print()
if oshibok:
    print(f"СБОЕВ: {oshibok}")
    sys.exit(1)
print("кодек Wialon IPS в порядке")
