"""Ключи бортов: закрытый на борту, реестр открытых у приёмника.

Так это устроено на площадке. Терминал при вводе в эксплуатацию порождает
себе пару ключей, закрытый не покидает устройство никогда, открытый уезжает
в реестр вместе с номером борта. Дальше приёмная сторона знает только реестр,
и подделать отсчёт, имея его целиком, нельзя.

🔴 На стенде закрытое семя выводится из одной константы и номера борта, иначе
восемь контейнеров при каждом запуске порождали бы новые ключи, а реестр в
репозитории устаревал бы к первому же `docker compose up`. Это допущение
стенда, и оно названо здесь, а не спрятано: на площадке семя рождается в
терминале, и `semya_borta` там не существует вовсе.

⚠️ Реестр и борта разделены не для красоты. `semya_borta` импортирует только
сторона борта (`sim/statement_uplink.py`), `Reestr` только приёмник
показателей. Приёмник, которому доступно семя, проверяет собственную подпись
и доказывает ровно ничего.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

import podpis

# Константа стенда. Не секрет: в репозитории она лежит открыто, и именно
# поэтому стенд не выдаёт себя за настоящую систему выдачи ключей.
SOL_STENDA = b"quarry-fleet-lab/statement/v1"

PUT_REESTRA = os.getenv(
    "REESTR_BORTOV",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "kluchi-bortov.json"))


def semya_borta(oid: int) -> bytes:
    """Закрытое семя борта. Только для стороны борта."""
    return hashlib.sha256(SOL_STENDA + str(oid).encode()).digest()


def otkrytyy_borta(oid: int) -> bytes:
    return podpis.otkrytyy_klyuch(semya_borta(oid))


class Reestr:
    """Открытые ключи бортов и список парков, с которых требуется подпись."""

    def __init__(self, borta: dict[int, bytes], podpisannye_parki: set[str]):
        self.borta = borta
        self.podpisannye_parki = podpisannye_parki

    @classmethod
    def prochitat(cls, put: str = PUT_REESTRA) -> "Reestr":
        with open(put, encoding="utf-8") as f:
            syroye = json.load(f)
        borta = {int(nomer): base64.b64decode(klyuch)
                 for nomer, klyuch in syroye.get("borta", {}).items()}
        for nomer, klyuch in borta.items():
            if len(klyuch) != podpis.RAZMER_KLYUCHA:
                raise ValueError(f"ключ борта {nomer} не {podpis.RAZMER_KLYUCHA} байт")
        return cls(borta, set(syroye.get("podpisannye_parki", [])))

    @classmethod
    def pustoy(cls) -> "Reestr":
        """Реестра нет: подпись ни с кого не требуется, всё как раньше."""
        return cls({}, set())

    def klyuch(self, oid: int) -> bytes | None:
        return self.borta.get(oid)

    def trebuet_podpisi(self, park: str) -> bool:
        """Должен ли этот парк присылать подписанные отсчёты.

        🔴 Без этого списка защита ничего не стоит: подделать отсчёт можно
        было бы, просто НЕ прикладывая подпись. Приёмник обязан знать заранее,
        от кого он подписи ждёт, и отвергать неподписанное от них.
        """
        return park in self.podpisannye_parki
