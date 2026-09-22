"""Проверка подписи отсчёта на приёмной стороне.

Почему проверка стоит здесь, а не в шлюзе. Шлюз это край периметра
диспетчерской: он принимает дейтаграмму и кладёт её в шину. Проверка там
доказывала бы, что диспетчерская не подделывает данные сама у себя, то есть
ничего. Между шлюзом и приёмником есть шина, и написать в неё может любой,
кто дотянулся до брокера, включая человека, которому не нравятся цифры смены.

Поэтому приёмник берёт из сообщения САМО заявление, в тех байтах, в которых
его подписал борт, проверяет подпись ключом из реестра и только потом
сверяет, что пересказ шлюза совпадает с подписанным содержимым. Отсчёт, не
прошедший проверку, не попадает ни в показатели, ни в журнал смены, ни в
отчёт: он существует только как счётчик отвергнутых.

🔴 Подпись требуется не со всех. Парки на EGTS и Wialon IPS подписи не
ставят, и требовать её от них значит выключить половину стенда. Список парков,
от которых подпись обязательна, лежит в реестре: без такого списка защита не
стоит ничего, потому что подделать отсчёт можно было бы, просто не приложив
подпись.
"""
from __future__ import annotations

import base64
import time

from prometheus_client import Counter, Gauge

import kluchi
import podpis
import zayavlenie

# Запас на расхождение часов между бортом и приёмником. Ноль здесь означал бы,
# что отсчёт, годный в момент отправки, отвергается из-за полусекунды разницы
# в часах, и стенд бы «терял» данные на ровном месте.
ZAPAS_SEK = 2.0

m_provereno = Counter("quarry_statement_verified_total",
                      "Отсчётов с проверенной подписью", ["fleet"])
m_otvergnuto = Counter("quarry_statement_rejected_total",
                       "Отсчётов отвергнуто при проверке", ["fleet", "prichina"])
m_vremya = Counter("quarry_statement_verify_seconds_total",
                   "Суммарное время проверки подписей, секунды")
g_ispolnenie = Gauge("quarry_statement_backend",
                     "Каким исполнением Ed25519 считается проверка", ["ispolnenie"])

# 🔴 Все причины отказа перечислены здесь и заводятся нулями при старте.
# Счётчик, который появляется в момент первого события, для `rate()` невидим:
# первая точка ряда становится основанием отсчёта. Тревога на подлог по
# `rate(...) > 0` из-за этого промолчала бы ровно на первом подлоге, то есть
# тогда, когда она и нужна.
PRICHINY = (
    "без подписи",
    "заявление не разбирается",
    "борт не в реестре",
    "чужой ключ",
    "подпись не сошлась",
    "срок годности вышел",
    "расхождение с подписанным",
)


class Proverka:
    """Реестр открытых ключей и решение по каждому сообщению телеметрии."""

    def __init__(self, reestr: kluchi.Reestr):
        self.reestr = reestr
        g_ispolnenie.labels(ispolnenie=podpis.ISPOLNENIE).set(1)
        for park in sorted(reestr.podpisannye_parki):
            m_provereno.labels(park)
            for prichina in PRICHINY:
                m_otvergnuto.labels(park, prichina)

    @classmethod
    def podnyat(cls) -> "Proverka":
        try:
            reestr = kluchi.Reestr.prochitat()
            print("[collector] реестр бортов: {} ключей, парки с подписью: {}, "
                  "проверка подписи {}".format(
                      len(reestr.borta),
                      ", ".join(sorted(reestr.podpisannye_parki)) or "нет",
                      podpis.ISPOLNENIE), flush=True)
        except FileNotFoundError:
            # ⚠️ Нет реестра это не отказ приёмника: стенд должен подниматься
            # и без третьего протокола. Но молчать об этом нельзя, иначе
            # пропажа файла выглядит как «подписи просто не нужны».
            print("[collector] реестра бортов нет, подпись не требуется ни с кого",
                  flush=True)
            reestr = kluchi.Reestr.pustoy()
        return cls(reestr)

    def prinyat(self, soobshchenie: dict) -> bool:
        """Можно ли считать этот отсчёт по-настоящему пришедшим с борта."""
        fleet = soobshchenie.get("fleet", "?")
        syroye = soobshchenie.pop("zayavlenie", None)
        if not self.reestr.trebuet_podpisi(fleet):
            # Парк на протоколе без подписи. Заявление, если оно вдруг
            # пришло, игнорируется: требовать подпись задним числом нельзя.
            return True
        if syroye is None:
            m_otvergnuto.labels(fleet, "без подписи").inc()
            return False

        nachalo = time.perf_counter()
        prichina = self._pochemu_ne(syroye, soobshchenie)
        m_vremya.inc(time.perf_counter() - nachalo)
        if prichina is not None:
            m_otvergnuto.labels(fleet, prichina).inc()
            return False
        m_provereno.labels(fleet).inc()
        return True

    def _pochemu_ne(self, syroye: str, soobshchenie: dict) -> str | None:
        try:
            dannye = base64.b64decode(syroye)
            z = zayavlenie.Zayavlenie.iz_baytov(dannye)
            razobrannoe = zayavlenie.razobrat_telo(z.telo)
        except Exception:  # noqa: BLE001 - любой мусор здесь это одно и то же
            return "заявление не разбирается"

        if z.podpis is None or z.klyuch is None:
            return "без подписи"

        klyuch = self.reestr.klyuch(razobrannoe["oid"])
        if klyuch is None:
            return "борт не в реестре"
        # 🔴 Ключ из заявления обязан совпасть с реестром. Проверить подпись
        # тем ключом, который приложен к самому заявлению, значит проверить,
        # что подделыватель умеет подписывать своим ключом. Умеет.
        if z.klyuch != klyuch:
            return "чужой ключ"
        if not podpis.proverit(klyuch, z.podpis, z.material()):
            return "подпись не сошлась"
        if razobrannoe["godno_do"] + ZAPAS_SEK < time.time():
            return "срок годности вышел"

        # Пересказ шлюза против подписанного содержимого. Здесь ловится
        # правка по дороге: шлюз или кто-то за ним положил в шину другие
        # цифры, чем подписал борт.
        dolzhno = zayavlenie.v_soobshchenie(razobrannoe)
        if dolzhno != soobshchenie:
            return "расхождение с подписанным"
        return None
