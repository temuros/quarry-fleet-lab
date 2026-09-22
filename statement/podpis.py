"""Подпись отсчёта: Ed25519 двумя способами, родным и на чистом Python.

Зачем борту подпись вообще. EGTS и Wialon IPS доверяют каналу: кто дозвонился
до порта приёма, тот и борт. На площадке этого хватает ровно до первого спора
о смене. Подписанный отсчёт отвечает на другой вопрос: не «дошло ли», а «этот
отсчёт правда сформировал борт 210003 и его после этого не правили».

Ключ рождается в терминале и наружу не выходит. Приёмная сторона знает только
открытую часть, и подделать отсчёт, имея её, нельзя. Поэтому проверка стоит
не на шлюзе (шлюз внутри периметра диспетчерской), а в приёмнике показателей:
между ними есть шина, и сообщение в шину может положить кто угодно.

⚠️ Два исполнения одного алгоритма. Родное (`cryptography`, под ним нативная
библиотека) считает проверку за десятки микросекунд. Чистый Python считает ту
же проверку 3,8 мс: при 120 отсчётах в секунду это половина ядра на одну
только арифметику, и приёмник начал бы отставать. Родное стоит в образах,
чистое нужно, чтобы `statement/selftest.py` запускался где угодно без
установки пакетов, как selftest двух соседних протоколов.

🔴 Оба исполнения обязаны давать бит в бит одинаковый результат, иначе борт и
приёмник разойдутся молча. Ed25519 детерминирован (подпись одного и того же
не меняется от запуска к запуску), поэтому сходимость проверяется прямым
сравнением байтов, и selftest это делает, когда доступны оба.
"""
from __future__ import annotations

import hashlib

RAZMER_SEMENI = 32
RAZMER_KLYUCHA = 32
RAZMER_PODPISI = 64


class PodpisError(ValueError):
    """Подпись не сошлась или ключ не того размера."""


# --------------------------------------------------------- чистый Python
#
# Ed25519 по RFC 8032 без зависимостей. Медленно, но считает то же самое.

_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493


def _obratnoe(x: int) -> int:
    return pow(x, _P - 2, _P)


_D = -121665 * _obratnoe(121666) % _P
_KOREN_MINUS_1 = pow(2, (_P - 1) // 4, _P)


def _vosstanovit_x(y: int) -> int:
    xx = (y * y - 1) * _obratnoe(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = x * _KOREN_MINUS_1 % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = 4 * _obratnoe(5) % _P
_BX = _vosstanovit_x(_BY)
_B = (_BX % _P, _BY % _P, 1, _BX * _BY % _P)


def _slozhit(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = t1 * 2 * _D * t2 % _P
    dd = z1 * 2 * z2 % _P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _udvoit(P):
    x1, y1, z1, _ = P
    a = x1 * x1 % _P
    b = y1 * y1 % _P
    c = 2 * z1 * z1 % _P
    e = ((x1 + y1) * (x1 + y1) - a - b) % _P
    g = -a + b
    f, h = g - c, -a - b
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _umnozhit(P, e: int):
    Q = (0, 1, 1, 0)
    for bit in bin(e)[2:]:
        Q = _udvoit(Q)
        if bit == "1":
            Q = _slozhit(Q, P)
    return Q


def _zapisat_tochku(P) -> bytes:
    x, y, z, _ = P
    zi = _obratnoe(z)
    x = x * zi % _P
    y = y * zi % _P
    return ((y & ~(1 << 255)) | ((x & 1) << 255)).to_bytes(32, "little")


def _prochitat_tochku(s: bytes):
    y = int.from_bytes(s, "little") & ~(1 << 255)
    x = _vosstanovit_x(y)
    if x & 1 != (s[31] >> 7) & 1:
        x = _P - x
    return (x, y, 1, x * y % _P)


def _h(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _sekret(semya: bytes) -> int:
    h = _h(semya)
    return 2 ** 254 + sum(2 ** i * ((h[i // 8] >> (i % 8)) & 1) for i in range(3, 254))


def _py_otkrytyy(semya: bytes) -> bytes:
    return _zapisat_tochku(_umnozhit(_B, _sekret(semya)))


def _py_podpisat(semya: bytes, dannye: bytes) -> bytes:
    h = _h(semya)
    a = _sekret(semya)
    otkrytyy = _zapisat_tochku(_umnozhit(_B, a))
    r = int.from_bytes(_h(h[32:64] + dannye), "little") % _Q
    R = _zapisat_tochku(_umnozhit(_B, r))
    s = (r + int.from_bytes(_h(R + otkrytyy + dannye), "little") * a) % _Q
    return R + s.to_bytes(32, "little")


def _py_proverit(otkrytyy: bytes, podpis: bytes, dannye: bytes) -> bool:
    try:
        R = _prochitat_tochku(podpis[:32])
        A = _prochitat_tochku(otkrytyy)
    except Exception:  # noqa: BLE001 - битая точка это просто неверная подпись
        return False
    s = int.from_bytes(podpis[32:64], "little")
    if s >= _Q:
        return False
    h = int.from_bytes(_h(podpis[:32] + otkrytyy + dannye), "little") % _Q
    sleva = _umnozhit(_B, s)
    sprava = _slozhit(R, _umnozhit(A, h))
    return _zapisat_tochku(sleva) == _zapisat_tochku(sprava)


# ------------------------------------------------------------------ родное

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    ISPOLNENIE = "rodnoe"

    def _rodnoy_otkrytyy(semya: bytes) -> bytes:
        return Ed25519PrivateKey.from_private_bytes(semya).public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw)

    def _rodnoy_podpisat(semya: bytes, dannye: bytes) -> bytes:
        return Ed25519PrivateKey.from_private_bytes(semya).sign(dannye)

    def _rodnoy_proverit(otkrytyy: bytes, podpis: bytes, dannye: bytes) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(otkrytyy).verify(podpis, dannye)
            return True
        except (InvalidSignature, ValueError):
            return False

except ImportError:  # pragma: no cover - на машине без пакета
    ISPOLNENIE = "python"
    _rodnoy_otkrytyy = _rodnoy_podpisat = _rodnoy_proverit = None


RODNOE_EST = ISPOLNENIE == "rodnoe"


# ------------------------------------------------------------------- наружу


def otkrytyy_klyuch(semya: bytes) -> bytes:
    _proverit_razmer(semya, RAZMER_SEMENI, "семя ключа")
    if RODNOE_EST:
        return _rodnoy_otkrytyy(semya)
    return _py_otkrytyy(semya)


def podpisat(semya: bytes, dannye: bytes) -> bytes:
    _proverit_razmer(semya, RAZMER_SEMENI, "семя ключа")
    if RODNOE_EST:
        return _rodnoy_podpisat(semya, dannye)
    return _py_podpisat(semya, dannye)


def proverit(otkrytyy: bytes, podpis: bytes, dannye: bytes) -> bool:
    """Сошлась ли подпись. Исключений не бросает: неверная подпись это ответ."""
    if len(otkrytyy) != RAZMER_KLYUCHA or len(podpis) != RAZMER_PODPISI:
        return False
    if RODNOE_EST:
        return _rodnoy_proverit(otkrytyy, podpis, dannye)
    return _py_proverit(otkrytyy, podpis, dannye)


def _proverit_razmer(znachenie: bytes, dlina: int, chto: str):
    if not isinstance(znachenie, (bytes, bytearray)) or len(znachenie) != dlina:
        raise PodpisError(f"{chto}: нужно {dlina} байт, пришло {len(znachenie)}")
