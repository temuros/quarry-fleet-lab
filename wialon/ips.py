"""Кодек Wialon IPS: второй протокол борта рядом с EGTS.

Зачем второй протокол вообще. На карьере техника редко бывает одного
поколения: часть машин с терминалами, говорящими EGTS по ГОСТ, часть с
теми, что настроены на Wialon IPS. Система, которая умеет только один
протокол, на такой площадке разворачивается «после замены терминалов», а
это отдельный проект с деньгами и простоем.

Чем он устроен иначе, чем EGTS, и почему это важно для приёмной стороны:

* EGTS двоичный, Wialon IPS текстовый, строками с переводом строки в конце;
* в EGTS один канал несёт записи МНОГИХ объектов, у каждой записи свой номер
  борта. В Wialon IPS объект называется один раз при входе, и дальше всё
  соединение принадлежит ему. Поэтому парк на этом протоколе открывает
  столько соединений, сколько машин, и это не изъян реализации, а свойство
  протокола;
* подтверждения простые: на каждый вид пакета свой короткий ответ.

⚠️ Общая часть с EGTS вынесена в `skhema`: номер борта, пересчёт координат,
коды состояний. Если два протокола начнут понимать поля по-разному, данные
поедут молча неправильные, а не с ошибкой.
"""
from __future__ import annotations

import time

import skhema


class WialonError(ValueError):
    """Строка не разбирается: битый пакет или чужой протокол."""


KONETS = "\r\n"

# Номера параметров в блоке params. На площадке терминал шлёт туда показания
# своих входов, а сервер придаёт им смысл по справочнику: здесь ровно так же.
P_T_SIM = "tsim"        # часы карьера, секунды
P_MS = "ms"             # миллисекунды времени формирования записи
P_STATE = "state"       # код состояния
P_FACE = "face"         # код забоя
P_CYCLES = "cycles"     # рейсов с начала смены
P_QUEUE = "queue"       # машин в очереди у экскаватора
P_TONS = "tons"         # тонн, умноженных на 10
P_IDLE = "idle"         # секунд простоя
P_BUSY = "busy"         # секунд работы
P_LOADED = "loaded"     # кузов гружён


# ------------------------------------------------------------------ координаты

def v_gradusy_minuty(gradusy: float, dolgota: bool) -> tuple[str, str]:
    """55.123456 -> («5507.4074», «N»). Формат NMEA, как у терминалов.

    ⚠️ Это не просто другое написание числа: градусы тут целые, а минуты
    десятичные. Прочитать «5507.4074» как 5507.4 градуса значит увезти
    машину в другое полушарие, и такая ошибка не падает, а тихо рисует
    технику посреди океана.
    """
    storona = ("E" if gradusy >= 0 else "W") if dolgota else ("N" if gradusy >= 0 else "S")
    gradusy = abs(gradusy)
    celye = int(gradusy)
    minuty = (gradusy - celye) * 60.0
    shirina = 3 if dolgota else 2
    return f"{celye:0{shirina}d}{minuty:07.4f}", storona


def iz_gradusov_minut(znachenie: str, storona: str) -> float:
    """«5507.4074», «N» -> 55.123456."""
    if not znachenie or znachenie == "NA":
        raise WialonError("координата не передана")
    tochka = znachenie.find(".")
    if tochka < 3:
        raise WialonError(f"координата не по формату: {znachenie}")
    celye = int(znachenie[: tochka - 2])
    minuty = float(znachenie[tochka - 2 :])
    gradusy = celye + minuty / 60.0
    return -gradusy if storona in ("S", "W") else gradusy


# ------------------------------------------------------------------ сборка

def paket_vhoda(imei: str, parol: str = "NA") -> str:
    return f"#L#{imei};{parol}{KONETS}"


def sobrat_params(znacheniya: dict[str, float]) -> str:
    """«tsim:2:1234.5,state:1:3» - имя, тип, значение.

    Тип 1 это целое, 2 дробное, 3 строка. Приёмная сторона обязана уметь оба
    числовых: терминалы разных вендоров кладут одно и то же то так, то этак.
    """
    chasti = []
    for imya, znachenie in znacheniya.items():
        if isinstance(znachenie, int):
            chasti.append(f"{imya}:1:{znachenie}")
        else:
            chasti.append(f"{imya}:2:{znachenie:.3f}")
    return ",".join(chasti)


def telo_dannyh(shirota: float, dolgota: float, vremya: float,
                skorost: float = 0.0, kurs: int = 0, vysota: int = 0,
                sputnikov: int = 8, params: dict[str, float] | None = None) -> str:
    """Тело пакета данных без обрамления: им же наполняется чёрный ящик."""
    t = time.gmtime(vremya)
    data = time.strftime("%d%m%y", t)
    chasy = time.strftime("%H%M%S", t)
    lat, lat_storona = v_gradusy_minuty(shirota, dolgota=False)
    lon, lon_storona = v_gradusy_minuty(dolgota, dolgota=True)
    # Поля по порядку: дата, время, широта, долгота, скорость, курс, высота,
    # спутники, hdop, входы, выходы, АЦП, ключ водителя, параметры.
    return ";".join([
        data, chasy,
        lat, lat_storona, lon, lon_storona,
        f"{skorost:.0f}", f"{kurs:d}", f"{vysota:d}", f"{sputnikov:d}",
        "1.0", "0", "0", "NA", "NA",
        sobrat_params(params or {}),
    ])


def paket_dannyh(telo: str) -> str:
    return f"#D#{telo}{KONETS}"


def paket_chernogo_yashchika(tela: list[str]) -> str:
    """Досылка после обрыва: несколько записей в одном пакете через «|».

    Это и есть то место, где Wialon IPS решает ту же задачу, что EGTS решает
    очередью неподтверждённых записей.
    """
    return "#B#" + "|".join(tela) + KONETS


def otvet(tip: str, znachenie: int | str = 1) -> str:
    """Ответы сервера: #AL#1 на вход, #AD#1 на данные, #AB#N на чёрный ящик."""
    return f"#A{tip}#{znachenie}{KONETS}"


# ------------------------------------------------------------------ разбор

def razobrat_stroku(stroka: str) -> tuple[str, str]:
    """«#D#190926;041500;...» -> («D», «190926;041500;...»)."""
    stroka = stroka.strip()
    if not stroka.startswith("#"):
        raise WialonError("пакет не начинается с решётки")
    vtoraya = stroka.find("#", 1)
    if vtoraya < 0:
        raise WialonError("в пакете нет второй решётки")
    tip = stroka[1:vtoraya]
    telo = stroka[vtoraya + 1 :]
    if not tip:
        raise WialonError("пустой тип пакета")
    return tip, telo


def razobrat_vhod(telo: str) -> str:
    """Тело пакета входа -> идентификатор терминала.

    ⚠️ У версии 2.0 первым полем идёт номер версии, и терминалы шлют оба
    варианта. Приёмник, который знает только один, молча теряет весь парк.
    """
    polya = telo.split(";")
    if polya and polya[0].replace(".", "", 1).isdigit() and "." in polya[0]:
        polya = polya[1:]
    if not polya or not polya[0]:
        raise WialonError("во входе нет идентификатора")
    return polya[0]


def razobrat_params(telo: str) -> dict[str, float]:
    znacheniya: dict[str, float] = {}
    if not telo or telo == "NA":
        return znacheniya
    for kusok in telo.split(","):
        chasti = kusok.split(":")
        if len(chasti) != 3:
            continue
        imya, tip, znachenie = chasti
        try:
            znacheniya[imya] = int(znachenie) if tip == "1" else float(znachenie)
        except ValueError:
            continue
    return znacheniya


def razobrat_dannye(telo: str) -> dict:
    """Тело пакета данных -> координаты, время и параметры."""
    # ⚠️ Полей ровно 16, и параметры последние. Считать их пятнадцатым полем
    # значит прочитать вместо них ключ водителя: разбор при этом не падает,
    # просто все датчики приходят пустыми.
    polya = telo.split(";")
    if len(polya) < 16:
        raise WialonError(f"в пакете данных {len(polya)} полей вместо 16")
    data, chasy = polya[0], polya[1]
    shirota = iz_gradusov_minut(polya[2], polya[3])
    dolgota = iz_gradusov_minut(polya[4], polya[5])
    try:
        # Время терминала всегда UTC: местное на площадке привело бы к
        # смещению отчёта по смене ровно на часовой пояс.
        vremya = time.mktime(time.strptime(data + chasy, "%d%m%y%H%M%S")) - time.timezone
    except ValueError as oshibka:
        raise WialonError(f"время не по формату: {data} {chasy}") from oshibka
    return {
        "shirota": shirota,
        "dolgota": dolgota,
        "vremya": vremya,
        "params": razobrat_params(polya[15]),
    }


def soobshchenie(imei: str, razobrannoe: dict) -> dict | None:
    """Разобранный пакет -> то же сообщение шины, что даёт шлюз EGTS.

    Ради этой функции всё и затевалось: дальше по потоку никто не знает, каким
    протоколом пришли данные. Приёмник показателей, журнал смены и экраны не
    переписаны ни на строку.
    """
    try:
        oid = int(imei)
    except ValueError:
        return None
    park, tip, imya = skhema.razobrat_nomer(oid)
    if park == "unknown":
        return None

    params = razobrannoe["params"]
    x, y = skhema.v_metry(razobrannoe["shirota"], razobrannoe["dolgota"])
    milliskundy = params.get(P_MS, 0) / 1000.0

    obshchee = {
        "fleet": park,
        "strategy": park,
        "t_sim": round(params.get(P_T_SIM, 0), 1),
        "kind": tip,
        "id": imya,
        # Время формирования записи НА БОРТУ, а не время приёма.
        "t_wall": razobrannoe["vremya"] + milliskundy,
    }

    if tip == "truck":
        obshchee.update({
            "state": skhema.SOSTOYANIYA_TRUCK_OBRATNO.get(int(params.get(P_STATE, 0)), "unknown"),
            "x": round(x, 1),
            "y": round(y, 1),
            "face": skhema.ZABOI_OBRATNO.get(int(params.get(P_FACE, 0))),
            "cycles": int(params.get(P_CYCLES, 0)),
            "loaded": bool(int(params.get(P_LOADED, 0))),
        })
    else:
        obshchee.update({
            "state": skhema.SOSTOYANIYA_EXC_OBRATNO.get(int(params.get(P_STATE, 0)), "unknown"),
            "queue": int(params.get(P_QUEUE, 0)),
            "idle_sec": float(params.get(P_IDLE, 0)),
            "busy_sec": float(params.get(P_BUSY, 0)),
            "tons": params.get(P_TONS, 0) / 10.0,
        })
    return obshchee
