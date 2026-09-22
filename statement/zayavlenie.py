"""Кодек подписанного заявления: третий протокол борта.

Откуда взята форма. Statement Store у Parity (`substrate/client/statement-store`
и `substrate/frame/statement`, в продуктовом виде это Celerity): маленькое
подписанное сообщение, у которого есть автор, срок жизни и приоритет, и нет
ни доставки с подтверждением, ни хранения. Отсюда взяты набор полей, порядок
и способ считать байты, которые подписываются.

Чем он отличается от двух соседних протоколов и зачем нужен третий:

* EGTS и Wialon IPS отвечают на вопрос «дошло ли». Оба гарантируют доставку:
  борт держит запись, пока сервер не подтвердил. Заявление отвечает на другой
  вопрос, «кто это сформировал и не правили ли после». Доставка при этом
  негарантированная, одно заявление это одна дейтаграмма UDP;
* в EGTS и Wialon подлинность обеспечивает канал: кто дозвонился до порта, тот
  и борт. Здесь подлинность несёт само сообщение, поэтому проверять его можно
  где угодно по пути, хоть через сутки, хоть из копии журнала;
* хранения нет. У отсчёта есть срок годности, и протухший отсчёт не досылают:
  на диспетчерском экране координата получасовой давности хуже, чем её
  отсутствие, потому что выглядит как текущая.

Состав заявления, поля идут по возрастанию номера:

    0  доказательство: вид подписи, сама подпись 64 байта, ключ борта 32
    2  приоритет, 32 бита: что переживёт переполнение приёмного буфера
    3  канал, 32 байта: куда это относится (здесь поток телеметрии карьера)
    4  тема, 32 байта: по ней подписчик отбирает свой парк, не разбирая тело
    8  тело: сам отсчёт, вместе со сроком годности

⚠️ Подписывается НЕ всё заявление, а его «материал»: те же поля без поля
подписи. Иначе подпись пришлось бы считать от данных, в которые она входит.
Материал собирается тем же кодом, что и заявление целиком, ровно чтобы борт и
приёмник не разошлись в том, какие байты считать подписанными.

🔴 Срок годности лежит ВНУТРИ подписанного тела, а не рядом. Лежал бы рядом,
его можно было бы подправить по дороге и оживить протухший отсчёт, не трогая
подпись. У Parity в хранилище срок общий на весь узел (сутки), здесь он свой
у каждого отсчёта: телеметрия стареет за секунды, а не за сутки.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

import skhema

# Предельный размер заявления. У Parity ограничение того же порядка и по той
# же причине: сообщение должно уезжать одной дейтаграммой, без сборки из
# кусков по дороге.
MAX_RAZMER = 512

POLE_PODPIS = 0
POLE_KLYUCH_RASSHIFROVKI = 1
POLE_PRIORITET = 2
POLE_KANAL = 3
POLE_TEMA = 4
POLE_TELO = 8
IZVESTNYE_POLYA = (POLE_PODPIS, POLE_KLYUCH_RASSHIFROVKI, POLE_PRIORITET,
                   POLE_KANAL, POLE_TEMA, POLE_TELO)

# Вид подписи. Ed25519 выбран из трёх, потому что проверяется быстрее всех и
# не требует ничего, кроме открытого ключа.
PODPIS_SR25519 = 0
PODPIS_ED25519 = 1
PODPIS_SECP256K1 = 2

# Приоритет решает, что выкинут первым при переполнении. Машина, вставшая на
# линии, важнее очередного отсчёта координаты идущей машины.
PRIORITET_OBYCHNYY = 100
PRIORITET_AVARIYA = 900

# Сколько отсчёт годен, секунды настоящего времени. Порог задержки приёма на
# стенде 5 секунд, так что десять это «уже поздно, но ещё объяснимо».
ZHIZN_SEK = 10

# Третий тип это не техника, а отчёт самого борта: сколько отсчётов он
# выбросил по сроку годности. Без него потеря данных остаётся молчаливой:
# приёмник видит разрыв в потоке и не может отличить «борт выбросил протухшее»
# от «борт сломался». Отчёт тоже подписан, поэтому стереть его по дороге
# нельзя, можно только не доставить.
TIPY = {"truck": 1, "excavator": 2, "otchet": 3}
TIPY_OBRATNO = {kod: imya for imya, kod in TIPY.items()}

FLAG_GRUZHEN = 0x01

# Общая часть тела и две хвостовые, по типу техники. Порядок и размеры
# одинаковы на борту и в приёмнике: это один файл на обе стороны.
OBSHCHEE = struct.Struct("<IIIHIBB")          # годен до, борт, время, мс, часы карьера, тип, состояние
HVOST_TRUCK = struct.Struct("<iiBBH")         # x и y в дециметрах, забой, флаги, рейсов
HVOST_EXC = struct.Struct("<HIII")            # очередь, тонны x10, простой, работа
HVOST_OTCHET = struct.Struct("<II")           # выброшено всего, осталось в очереди


class ZayavlenieError(ValueError):
    """Заявление не разбирается: битые байты или чужой формат."""


def kanal_po_imeni(imya: str) -> bytes:
    """Имя канала -> 32 байта. Канал у Parity это просто метка потока."""
    return hashlib.sha256(imya.encode()).digest()


KANAL_TELEMETRIYA = kanal_po_imeni("quarry.telemetry")


def tema_parka(park: str) -> bytes:
    return hashlib.sha256(f"park:{park}".encode()).digest()


# ------------------------------------------------------------------ SCALE

def szhatoe(n: int) -> bytes:
    """Сжатое число SCALE: длины в заявлении пишутся им."""
    if n < 0:
        raise ZayavlenieError("отрицательная длина")
    if n < 64:
        return bytes([n << 2])
    if n < 2 ** 14:
        return ((n << 2) | 0b01).to_bytes(2, "little")
    if n < 2 ** 30:
        return ((n << 2) | 0b10).to_bytes(4, "little")
    raise ZayavlenieError("длина не помещается в сжатое число")


def razobrat_szhatoe(syroye: bytes, i: int) -> tuple[int, int]:
    if i >= len(syroye):
        raise ZayavlenieError("нет байтов под длину")
    rezhim = syroye[i] & 0b11
    if rezhim == 0b00:
        return syroye[i] >> 2, i + 1
    if rezhim == 0b01:
        if i + 2 > len(syroye):
            raise ZayavlenieError("нет байтов под длину")
        return int.from_bytes(syroye[i:i + 2], "little") >> 2, i + 2
    if rezhim == 0b10:
        if i + 4 > len(syroye):
            raise ZayavlenieError("нет байтов под длину")
        return int.from_bytes(syroye[i:i + 4], "little") >> 2, i + 4
    raise ZayavlenieError("длина в большом режиме здесь не бывает")


# -------------------------------------------------------------- заявление

@dataclass
class Zayavlenie:
    telo: bytes
    prioritet: int = PRIORITET_OBYCHNYY
    kanal: bytes = KANAL_TELEMETRIYA
    tema: bytes | None = None
    podpis: bytes | None = None
    klyuch: bytes | None = None
    vid_podpisi: int = PODPIS_ED25519

    def material(self) -> bytes:
        """Байты, которые подписывает борт: заявление без поля подписи.

        🔴 Сюда входят и канал, и тема, и приоритет. Оставить их вне подписи
        значит разрешить по дороге переложить отсчёт в чужой парк или понизить
        приоритет аварии до обычного, и подпись при этом сойдётся.
        """
        return self._polya(s_podpisyu=False)

    def v_bayty(self) -> bytes:
        syroye = self._polya(s_podpisyu=True)
        if len(syroye) > MAX_RAZMER:
            raise ZayavlenieError(
                f"заявление {len(syroye)} байт, предел {MAX_RAZMER}")
        return syroye

    def _polya(self, s_podpisyu: bool) -> bytes:
        chasti = []
        if s_podpisyu and self.podpis is not None:
            if len(self.podpis) != 64 or len(self.klyuch or b"") != 32:
                raise ZayavlenieError("подпись 64 байта, ключ 32")
            chasti.append(bytes([POLE_PODPIS, self.vid_podpisi]) + self.podpis + self.klyuch)
        chasti.append(bytes([POLE_PRIORITET]) + struct.pack("<I", self.prioritet))
        chasti.append(bytes([POLE_KANAL]) + self.kanal)
        if self.tema is not None:
            chasti.append(bytes([POLE_TEMA]) + self.tema)
        chasti.append(bytes([POLE_TELO]) + szhatoe(len(self.telo)) + self.telo)
        return szhatoe(len(chasti)) + b"".join(chasti)

    @classmethod
    def iz_baytov(cls, syroye: bytes) -> "Zayavlenie":
        if len(syroye) > MAX_RAZMER:
            raise ZayavlenieError(f"заявление длиннее {MAX_RAZMER} байт")
        skolko, i = razobrat_szhatoe(syroye, 0)
        if not 1 <= skolko <= len(IZVESTNYE_POLYA):
            raise ZayavlenieError(f"полей заявлено {skolko}")
        z = cls(telo=b"")
        telo_bylo = False
        proshloe = -1
        for _ in range(skolko):
            if i >= len(syroye):
                raise ZayavlenieError("поля кончились раньше счётчика")
            nomer = syroye[i]
            # ⚠️ Поля обязаны идти по возрастанию и не повторяться. Иначе одно
            # и то же заявление можно записать разными байтами, а подпись
            # считается именно от байтов: сошлась бы не та строка, что
            # разобрана.
            if nomer <= proshloe:
                raise ZayavlenieError(f"поле {nomer} не по порядку")
            proshloe = nomer
            i += 1
            if nomer == POLE_PODPIS:
                if i + 1 + 64 + 32 > len(syroye):
                    raise ZayavlenieError("подпись обрезана")
                z.vid_podpisi = syroye[i]
                if z.vid_podpisi != PODPIS_ED25519:
                    raise ZayavlenieError(f"вид подписи {z.vid_podpisi} не поддержан")
                z.podpis = syroye[i + 1:i + 65]
                z.klyuch = syroye[i + 65:i + 97]
                i += 97
            elif nomer == POLE_PRIORITET:
                if i + 4 > len(syroye):
                    raise ZayavlenieError("приоритет обрезан")
                z.prioritet = struct.unpack_from("<I", syroye, i)[0]
                i += 4
            elif nomer in (POLE_KANAL, POLE_TEMA, POLE_KLYUCH_RASSHIFROVKI):
                if i + 32 > len(syroye):
                    raise ZayavlenieError(f"поле {nomer} обрезано")
                if nomer == POLE_KANAL:
                    z.kanal = syroye[i:i + 32]
                elif nomer == POLE_TEMA:
                    z.tema = syroye[i:i + 32]
                i += 32
            elif nomer == POLE_TELO:
                dlina, i = razobrat_szhatoe(syroye, i)
                if i + dlina > len(syroye):
                    raise ZayavlenieError("тело обрезано")
                z.telo = syroye[i:i + dlina]
                telo_bylo = True
                i += dlina
            else:
                raise ZayavlenieError(f"неизвестное поле {nomer}")
        if i != len(syroye):
            raise ZayavlenieError("после последнего поля остались байты")
        if not telo_bylo:
            raise ZayavlenieError("заявление без тела")
        return z


# ------------------------------------------------------------------- тело

def sobrat_telo(park: str, payload: dict, godno_do: int) -> bytes:
    """Отсчёт симулятора -> подписываемое тело.

    Раскладка та же по смыслу, что в EGTS и Wialon IPS: борт передаёт свои
    показания, а смысл им придаёт приёмник по справочнику `skhema`.
    """
    tip = payload["kind"]
    oid = skhema.nomer_borta(park, tip, payload["id"])
    t_wall = payload.get("t_wall", 0.0)
    vremya = int(t_wall)
    ms = int(round((t_wall - vremya) * 1000)) % 1000
    if tip == "truck":
        sostoyanie = skhema.SOSTOYANIYA_TRUCK.get(payload.get("state", ""), 0)
    else:
        sostoyanie = skhema.SOSTOYANIYA_EXC.get(payload.get("state", ""), 0)

    golova = OBSHCHEE.pack(godno_do, oid, vremya, ms,
                           int(payload.get("t_sim", 0)), TIPY.get(tip, 0), sostoyanie)
    if tip == "truck":
        flagi = FLAG_GRUZHEN if payload.get("loaded") else 0
        hvost = HVOST_TRUCK.pack(
            int(round(payload.get("x", 0.0) * 10)),
            int(round(payload.get("y", 0.0) * 10)),
            skhema.ZABOI.get(payload.get("face") or "", 0),
            flagi,
            min(int(payload.get("cycles", 0)), 65535),
        )
    else:
        hvost = HVOST_EXC.pack(
            min(int(payload.get("queue", 0)), 65535),
            int(round(float(payload.get("tons", 0.0)) * 10)),
            int(payload.get("idle_sec", 0)),
            int(payload.get("busy_sec", 0)),
        )
    return golova + hvost


def sobrat_otchet(oid: int, vremya: float, godno_do: int,
                  ustarelo: int, v_ocheredi: int) -> bytes:
    """Отчёт борта о выброшенном: тот же конверт, другой тип тела."""
    sekundy = int(vremya)
    golova = OBSHCHEE.pack(godno_do, oid, sekundy,
                           int(round((vremya - sekundy) * 1000)) % 1000,
                           0, TIPY["otchet"], 0)
    return golova + HVOST_OTCHET.pack(min(ustarelo, 2 ** 32 - 1),
                                      min(v_ocheredi, 2 ** 32 - 1))


def razobrat_telo(telo: bytes) -> dict:
    if len(telo) < OBSHCHEE.size:
        raise ZayavlenieError("тело короче общей части")
    godno_do, oid, vremya, ms, t_sim, tip, sostoyanie = OBSHCHEE.unpack_from(telo, 0)
    imya_tipa = TIPY_OBRATNO.get(tip)
    if imya_tipa is None:
        raise ZayavlenieError(f"тип техники {tip} неизвестен")
    hvost = telo[OBSHCHEE.size:]
    razobrannoe = {
        "godno_do": godno_do,
        "oid": oid,
        "t_wall": vremya + ms / 1000.0,
        "t_sim": float(t_sim),
        "kind": imya_tipa,
        "sostoyanie": sostoyanie,
    }
    if imya_tipa == "otchet":
        if len(hvost) != HVOST_OTCHET.size:
            raise ZayavlenieError("хвост отчёта не того размера")
        ustarelo, v_ocheredi = HVOST_OTCHET.unpack(hvost)
        razobrannoe.update({"ustarelo": ustarelo, "v_ocheredi": v_ocheredi})
    elif imya_tipa == "truck":
        if len(hvost) != HVOST_TRUCK.size:
            raise ZayavlenieError("хвост самосвала не того размера")
        x_dm, y_dm, zaboy, flagi, cycles = HVOST_TRUCK.unpack(hvost)
        razobrannoe.update({
            "x": x_dm / 10.0, "y": y_dm / 10.0, "zaboy": zaboy,
            "loaded": bool(flagi & FLAG_GRUZHEN), "cycles": cycles,
        })
    else:
        if len(hvost) != HVOST_EXC.size:
            raise ZayavlenieError("хвост экскаватора не того размера")
        queue, tonny10, idle, busy = HVOST_EXC.unpack(hvost)
        razobrannoe.update({
            "queue": queue, "tons": tonny10 / 10.0,
            "idle_sec": float(idle), "busy_sec": float(busy),
        })
    return razobrannoe


def v_soobshchenie(razobrannoe: dict) -> dict:
    """Разобранное тело -> то же сообщение шины, что у двух других протоколов.

    Приёмник показателей, журнал смены и экраны не знают, каким протоколом
    пришли данные. Третий протокол это правило не нарушает: он добавляет к
    сообщению подпись, но не меняет ни одного прежнего поля.
    """
    if razobrannoe["kind"] == "otchet":
        raise ZayavlenieError("отчёт борта в шину не идёт")
    park, tip, imya = skhema.razobrat_nomer(razobrannoe["oid"])
    soobshchenie = {
        "fleet": park,
        "strategy": park,
        "t_sim": round(razobrannoe["t_sim"], 1),
        "kind": tip,
        "id": imya,
        "t_wall": razobrannoe["t_wall"],
    }
    if tip == "truck":
        soobshchenie.update({
            "state": skhema.SOSTOYANIYA_TRUCK_OBRATNO.get(razobrannoe["sostoyanie"], "unknown"),
            "x": round(razobrannoe["x"], 1),
            "y": round(razobrannoe["y"], 1),
            "face": skhema.ZABOI_OBRATNO.get(razobrannoe["zaboy"]),
            "cycles": razobrannoe["cycles"],
            "loaded": razobrannoe["loaded"],
        })
    else:
        soobshchenie.update({
            "state": skhema.SOSTOYANIYA_EXC_OBRATNO.get(razobrannoe["sostoyanie"], "unknown"),
            "queue": razobrannoe["queue"],
            "idle_sec": razobrannoe["idle_sec"],
            "busy_sec": razobrannoe["busy_sec"],
            "tons": razobrannoe["tons"],
        })
    return soobshchenie
