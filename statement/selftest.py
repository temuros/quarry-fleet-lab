"""Проверка третьего протокола без сети, шины и Docker.

Здесь проверяется не «работает ли Ed25519» (он работает), а то, ради чего
подпись вообще добавлена: что правка отсчёта по дороге ломает подпись, что
чужой ключ не принимается и что протухшее видно по самому заявлению.

    python statement/selftest.py

⚠️ Без пакета `cryptography` тот же код считает подписи на чистом Python, и
прогон занимает секунды вместо долей секунды. Это нормально: в образах стоит
родное исполнение, а selftest должен запускаться на голой машине.
"""
from __future__ import annotations

import os
import sys
import time

SVOYA_PAPKA = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SVOYA_PAPKA)
sys.path.insert(0, os.path.join(os.path.dirname(SVOYA_PAPKA), "egts"))

import kluchi  # noqa: E402
import podpis  # noqa: E402
import skhema  # noqa: E402
import zayavlenie  # noqa: E402

oshibok = 0


def proverit(chto: str, uslovie: bool, podrobnosti: str = ""):
    global oshibok
    if uslovie:
        print(f"  ок    {chto}")
    else:
        oshibok += 1
        print(f"  СБОЙ  {chto} {podrobnosti}")


OTSCHET = {
    "kind": "truck", "id": "BAL-03", "state": "to_dump", "t_sim": 4820.5,
    "t_wall": 1758500000.375, "x": 1234.5, "y": -678.9, "face": "EX-02",
    "cycles": 17, "loaded": True,
}
EKSKAVATOR = {
    "kind": "excavator", "id": "EX-01", "state": "loading", "t_sim": 4820.5,
    "t_wall": 1758500000.5, "queue": 3, "idle_sec": 120.0, "busy_sec": 3400.0,
    "tons": 8123.4,
}

print("тело отсчёта: собрать и разобрать обратно")
for otschet in (OTSCHET, EKSKAVATOR):
    telo = zayavlenie.sobrat_telo("balanced", otschet, godno_do=1758500010)
    nazad = zayavlenie.razobrat_telo(telo)
    soobshchenie = zayavlenie.v_soobshchenie(nazad)
    proverit(f"{otschet['id']}: тип и номер борта",
             soobshchenie["id"] == otschet["id"] and soobshchenie["kind"] == otschet["kind"],
             f"вышло {soobshchenie['id']}, {soobshchenie['kind']}")
    proverit(f"{otschet['id']}: состояние", soobshchenie["state"] == otschet["state"],
             f"вышло {soobshchenie['state']}")
    proverit(f"{otschet['id']}: время с миллисекундами",
             abs(soobshchenie["t_wall"] - otschet["t_wall"]) < 0.002,
             f"вышло {soobshchenie['t_wall']}")
    proverit(f"{otschet['id']}: срок годности внутри тела", nazad["godno_do"] == 1758500010)

telo = zayavlenie.sobrat_telo("balanced", OTSCHET, godno_do=1758500010)
soobshchenie = zayavlenie.v_soobshchenie(zayavlenie.razobrat_telo(telo))
proverit("координаты с точностью до дециметра",
         soobshchenie["x"] == 1234.5 and soobshchenie["y"] == -678.9,
         f"вышло {soobshchenie['x']}, {soobshchenie['y']}")
proverit("забой и гружёность",
         soobshchenie["face"] == "EX-02" and soobshchenie["loaded"] is True)

print("заявление: сборка, разбор, размер")
oid = skhema.nomer_borta("balanced", "truck", "BAL-03")
semya = kluchi.semya_borta(oid)
otkrytyy = podpis.otkrytyy_klyuch(semya)
z = zayavlenie.Zayavlenie(telo=telo, prioritet=zayavlenie.PRIORITET_OBYCHNYY,
                          tema=zayavlenie.tema_parka("balanced"))
z.klyuch = otkrytyy
z.podpis = podpis.podpisat(semya, z.material())
syroye = z.v_bayty()
proverit(f"заявление помещается в дейтаграмму: {len(syroye)} байт",
         len(syroye) <= zayavlenie.MAX_RAZMER)
nazad = zayavlenie.Zayavlenie.iz_baytov(syroye)
proverit("после разбора поля те же",
         nazad.telo == z.telo and nazad.prioritet == z.prioritet
         and nazad.kanal == z.kanal and nazad.tema == z.tema
         and nazad.podpis == z.podpis and nazad.klyuch == z.klyuch)
proverit("подпись сходится ключом борта",
         podpis.proverit(otkrytyy, nazad.podpis, nazad.material()))

print("подделка: каждая правка ломает подпись")
# Байт состояния в теле: подменить «едет гружёным» на «в ремонте» значит
# дорисовать простой, которого не было.
porchenoe = bytearray(z.telo)
porchenoe[zayavlenie.OBSHCHEE.size - 1] = skhema.SOSTOYANIYA_TRUCK["down"]
podelka = zayavlenie.Zayavlenie(telo=bytes(porchenoe), prioritet=z.prioritet,
                                tema=z.tema, podpis=z.podpis, klyuch=z.klyuch)
proverit("правка состояния в теле",
         not podpis.proverit(otkrytyy, podelka.podpis, podelka.material()))

# Тот же отсчёт, переложенный в чужой парк темой.
chuzhaya_tema = zayavlenie.Zayavlenie(telo=z.telo, prioritet=z.prioritet,
                                      tema=zayavlenie.tema_parka("fixed"),
                                      podpis=z.podpis, klyuch=z.klyuch)
proverit("подмена темы (парка)",
         not podpis.proverit(otkrytyy, chuzhaya_tema.podpis, chuzhaya_tema.material()))

# Понижение приоритета: авария превращается в рядовой отсчёт и первой
# вылетает при переполнении.
drugoy_prioritet = zayavlenie.Zayavlenie(telo=z.telo, prioritet=1, tema=z.tema,
                                         podpis=z.podpis, klyuch=z.klyuch)
proverit("подмена приоритета",
         not podpis.proverit(otkrytyy, drugoy_prioritet.podpis, drugoy_prioritet.material()))

# Подпись чужим ключом: подделыватель умеет подписывать, но своим ключом.
chuzhoye_semya = kluchi.semya_borta(999999)
chuzhoy_otkrytyy = podpis.otkrytyy_klyuch(chuzhoye_semya)
chuzhaya = zayavlenie.Zayavlenie(telo=bytes(porchenoe), prioritet=z.prioritet, tema=z.tema)
chuzhaya.klyuch = chuzhoy_otkrytyy
chuzhaya.podpis = podpis.podpisat(chuzhoye_semya, chuzhaya.material())
proverit("подпись чужим ключом сходится сама с собой",
         podpis.proverit(chuzhoy_otkrytyy, chuzhaya.podpis, chuzhaya.material()))
proverit("но ключом настоящего борта не сходится",
         not podpis.proverit(otkrytyy, chuzhaya.podpis, chuzhaya.material()))

print("отчёт борта о выброшенном")
otchet = zayavlenie.sobrat_otchet(oid, 1758500000.5, 1758500010, ustarelo=3710, v_ocheredi=110)
razobrannyy = zayavlenie.razobrat_telo(otchet)
proverit("отчёт разбирается как отчёт", razobrannyy["kind"] == "otchet",
         f"вышло {razobrannyy['kind']}")
proverit("в отчёте счётчик выброшенного",
         razobrannyy["ustarelo"] == 3710 and razobrannyy["v_ocheredi"] == 110)
try:
    zayavlenie.v_soobshchenie(razobrannyy)
    proverit("отчёт не уходит в шину как телеметрия", False, "ушёл")
except zayavlenie.ZayavlenieError:
    proverit("отчёт не уходит в шину как телеметрия", True)

print("реестр бортов")
reestr = kluchi.Reestr.prochitat()
proverit("парк balanced требует подписи", reestr.trebuet_podpisi("balanced"))
proverit("парк fixed подписи не требует", not reestr.trebuet_podpisi("fixed"))
proverit("ключ борта BAL-03 из реестра совпадает с ключом борта",
         reestr.klyuch(oid) == otkrytyy,
         "перевыпустить: python statement/vydat_kluchi.py")
proverit("незаведённого борта в реестре нет", reestr.klyuch(999999) is None)

print("срок годности")
prosrochennoe = zayavlenie.sobrat_telo("balanced", OTSCHET, godno_do=int(time.time()) - 5)
proverit("протухшее видно по самому телу",
         zayavlenie.razobrat_telo(prosrochennoe)["godno_do"] < time.time())

print("битые заявления не роняют разбор")
plohie = [
    (b"", "пусто"),
    (b"\x00", "один байт"),
    (b"\x04\x08\x10\xff\xff", "обрезанное тело"),
    (bytes([0x0c, 3]) + b"\x00" * 32 + bytes([2]) + b"\x00" * 4
     + bytes([8, 0]), "поля не по порядку"),
    (bytes([0x08, 9, 0]), "неизвестное поле"),
    (syroye + b"\x00", "лишний байт в хвосте"),
]
for plohoye, imya in plohie:
    try:
        zayavlenie.Zayavlenie.iz_baytov(plohoye)
        proverit(f"«{imya}» отвергнуто", False, "разобралось как верное")
    except zayavlenie.ZayavlenieError:
        proverit(f"«{imya}» отвергнуто с понятной ошибкой", True)
    except Exception as oshibka:  # noqa: BLE001
        proverit(f"«{imya}» отвергнуто", False, f"чужая ошибка {oshibka!r}")

print("исполнения Ed25519")
print(f"  сейчас считает: {podpis.ISPOLNENIE}")
if podpis.RODNOE_EST:
    # 🔴 Родное и чистое исполнения обязаны давать одинаковые байты, иначе
    # борт с одним и приёмник с другим разойдутся молча.
    py_podpis = podpis._py_podpisat(semya, z.material())
    proverit("родное и чистое дают ту же подпись байт в байт",
             py_podpis == z.podpis)
    proverit("чистое проверяет родную подпись",
             podpis._py_proverit(otkrytyy, z.podpis, z.material()))
    proverit("открытый ключ выводится одинаково",
             podpis._py_otkrytyy(semya) == otkrytyy)

nachalo = time.perf_counter()
for _ in range(20):
    podpis.proverit(otkrytyy, z.podpis, z.material())
print(f"  проверка подписи: {(time.perf_counter() - nachalo) / 20 * 1000:.2f} мс")

print()
if oshibok:
    print(f"СБОЕВ: {oshibok}")
    sys.exit(1)
print("третий протокол в порядке")
