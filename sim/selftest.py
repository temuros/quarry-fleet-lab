"""Прогон модели карьера без Kafka и Docker.

Сравнивает стратегии назначения на одной и той же случайности.
Запуск: python sim/selftest.py [часов] [прогонов]
"""

import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import simulator as sim  # noqa: E402


class NullUplink:
    """Заглушка канала: события копим, телеметрию выбрасываем."""

    online = True
    buffer = ()

    def __init__(self):
        self.events = []

    def send(self, topic, payload):
        if topic == sim.TOPIC_EVENTS:
            self.events.append(payload)

    def pump(self, wall_dt):
        pass

    def set_online(self, value):
        pass


def run(strategy, hours, seed, trucks=None):
    sim.rnd = random.Random(seed)
    sim.DISTURBANCE_SEED = seed          # сценарий отказов один на обе стратегии
    if trucks:
        sim.TRUCKS = trucks
    up = NullUplink()
    quarry = sim.Quarry(up, strategy)
    for _ in range(int(hours * 3600 / sim.TICK_SEC)):
        quarry.step(sim.TICK_SEC)

    cycles = [e for e in up.events if e.get("event") == "cycle_completed"]
    tons = quarry.tons_by_material
    total = sum(tons.values())
    return {
        "тонн в час": total / hours,
        "доля вскрыши": tons["вскрыша"] / total if total else 0.0,
        "рейсов": len(cycles),
        "время рейса, мин": statistics.mean(e["cycle_sec"] for e in cycles) / 60 if cycles else 0,
        "ожидание, мин": statistics.mean(e["wait_sec"] for e in cycles) / 60 if cycles else 0,
        "простой EX-01": quarry.excavators["EX-01"].idle_sec / max(1, quarry.now),
        "простой EX-02": quarry.excavators["EX-02"].idle_sec / max(1, quarry.now),
    }


def compare(hours, runs, trucks=None):
    """Парные прогоны: один и тот же сценарий отказов на обе стратегии."""
    acc = {"fixed": [], "balanced": []}
    gains = []
    for i in range(runs):
        pair = {st: run(st, hours, seed=100 + i, trucks=trucks) for st in acc}
        for st in acc:
            acc[st].append(pair[st])
        gains.append(pair["balanced"]["тонн в час"] / pair["fixed"]["тонн в час"] - 1)
    rows = {st: {k: statistics.mean(r[k] for r in acc[st]) for k in acc[st][0]} for st in acc}
    rows["_gains"] = gains
    return rows


def sweep(hours, runs):
    """Как выигрыш зависит от размера парка при тех же двух забоях."""
    print("Парк      закреплены   балансировка   выигрыш   разброс по прогонам")
    for trucks in (4, 6, 8, 10, 12, 14):
        rows = compare(hours, runs, trucks=trucks)
        f = rows["fixed"]["тонн в час"]
        b = rows["balanced"]["тонн в час"]
        g = rows["_gains"]
        print("{:>2} шт   {:>10.0f}   {:>12.0f}   {:>+7.1%}   (от {:+.1%} до {:+.1%})".format(
            trucks, f, b, statistics.mean(g), min(g), max(g)))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "sweep":
        sweep(float(sys.argv[2]) if len(sys.argv) > 2 else 24.0,
              int(sys.argv[3]) if len(sys.argv) > 3 else 3)
        return
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    rows = compare(hours, runs)

    names = {"fixed": "закреплены за забоем", "balanced": "балансировка"}
    gains = rows.pop("_gains")
    keys = list(rows["fixed"])
    width = max(len(k) for k in keys) + 2
    print("Прогон {:.0f} ч карьера, {} повторов, среднее\n".format(hours, runs))
    print("{:<{w}}{:>22}{:>22}".format("показатель", names["fixed"], names["balanced"], w=width))
    for k in keys:
        print("{:<{w}}{:>22.2f}{:>22.2f}".format(k, rows["fixed"][k], rows["balanced"][k], w=width))
    print()
    print("выигрыш балансировки по тоннажу: {:+.1%} в среднем, от {:+.1%} до {:+.1%} по прогонам".format(
        statistics.mean(gains), min(gains), max(gains)))


if __name__ == "__main__":
    main()
