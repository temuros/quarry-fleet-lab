"""Учение: диспетчерская дорисовывает рейсы. Проверка подписи это ловит.

Стенд, на котором защиту не пробовали ломать, доказывает только то, что она
не мешает работать. Здесь три попытки положить в шину то, чего борт не
подписывал, каждая со своим способом:

1. **без подписи.** Сообщение того же вида, что кладёт шлюз, но заявления в
   нём нет. Так выглядит самый простой подлог: написать в шину напрямую;
2. **правка подписанного.** Настоящее сообщение с настоящим заявлением, но
   число рейсов в пересказе увеличено. Так выглядит подлог у того, кто
   дотянулся до шины и не хочет трогать подпись;
3. **чужой ключ.** Заявление подписано по-настоящему, но не тем ключом,
   который числится за этой машиной в реестре. Так выглядит подлог у того,
   кто умеет подписывать, но своим ключом;
4. **борт не в реестре.** Заявление от машины, которой не выдавали ключ. Так
   выглядит и подлог, и честная ошибка: новый самосвал завели в парк, а ключ
   ему выдать забыли.

Запуск на поднятом стенде:

    docker cp statement/podlog.py quarry-collector:/app/
    docker exec quarry-collector python podlog.py

Дальше смотреть `quarry_statement_rejected_total` на :8000/metrics или панель
«Подписанная телеметрия» на дашборде: ни одна из четырёх попыток не должна
попасть ни в показатели, ни на экраны, и у каждой должна быть своя причина.

⛔ Это учение, а не инструмент. Оно пишет в шину стенда и больше никуда.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

from confluent_kafka import Consumer, Producer, TopicPartition

import kluchi
import podpis
import skhema
import zayavlenie

BROKER = os.getenv("KAFKA_BROKER", "kafka:9092")
TOPIC = os.getenv("TOPIC_TELEMETRY", "quarry.telemetry")
PARK = os.getenv("PARK", "balanced")


def nayti_nastoyashcheye() -> dict:
    """Взять из шины СВЕЖЕЕ подписанное сообщение нужного парка.

    🔴 Свежее принципиально. На старом отсчёте вторая попытка (правка
    подписанного) отвергается по сроку годности и не доказывает ничего: до
    сверки с подписью дело просто не доходит.

    ⚠️ Разделы назначаются руками, с конца. Подписка группой здесь не годится:
    пока новая группа распределяет разделы, свежие сообщения уже уехали, и
    чтение начинается с пустоты.
    """
    consumer = Consumer({
        "bootstrap.servers": BROKER,
        "group.id": f"podlog-{int(time.time())}",
        "enable.auto.commit": False,
    })
    svedeniya = consumer.list_topics(TOPIC, timeout=10)
    razdely = list(svedeniya.topics[TOPIC].partitions)
    konec = []
    for nomer in razdely:
        _, verh = consumer.get_watermark_offsets(TopicPartition(TOPIC, nomer), timeout=10)
        konec.append(TopicPartition(TOPIC, nomer, verh))
    consumer.assign(konec)
    do = time.time() + 60
    try:
        while time.time() < do:
            syroye = consumer.poll(1.0)
            if syroye is None or syroye.error():
                continue
            msg = json.loads(syroye.value())
            if msg.get("fleet") == PARK and msg.get("zayavlenie"):
                return msg
    finally:
        consumer.close()
    raise SystemExit(f"за минуту не пришло ни одного подписанного отсчёта парка {PARK}")


def podpisannoye(oid: int, obrazec: dict, cycles: int, imya: str | None = None) -> str:
    """Собрать заявление от имени борта `oid` его собственным ключом.

    ⚠️ Имя машины берётся из тела, а не из пересказа: номер борта, по которому
    приёмник ищет ключ в реестре, лежит ВНУТРИ подписанного. Попытка выдать
    себя за незаведённую машину, оставив в теле чужое имя, отвергается как
    «чужой ключ», а не как «борт не в реестре»: реестр смотрит на тело.
    """
    telo = zayavlenie.sobrat_telo(PARK, {
        "kind": "truck", "id": imya or obrazec["id"], "state": "to_dump",
        "t_sim": obrazec["t_sim"], "t_wall": time.time(),
        "x": 0.0, "y": 0.0, "face": "EX-01", "cycles": cycles, "loaded": True,
    }, godno_do=int(time.time() + 10))
    z = zayavlenie.Zayavlenie(telo=telo, tema=zayavlenie.tema_parka(PARK))
    semya = kluchi.semya_borta(oid)
    z.klyuch = podpis.otkrytyy_klyuch(semya)
    z.podpis = podpis.podpisat(semya, z.material())
    return base64.b64encode(z.v_bayty()).decode()


def main():
    nastoyashcheye = nayti_nastoyashcheye()
    print("взял живой отсчёт: {} {}, рейсов {}".format(
        nastoyashcheye["kind"], nastoyashcheye["id"], nastoyashcheye.get("cycles")))

    popytki = []

    # 1. Без подписи вовсе.
    bez_podpisi = {k: v for k, v in nastoyashcheye.items() if k != "zayavlenie"}
    bez_podpisi["cycles"] = 999
    popytki.append(("без подписи", bez_podpisi))

    # 2. Подпись настоящая, пересказ подправлен.
    pravlenoye = dict(nastoyashcheye)
    pravlenoye["cycles"] = nastoyashcheye.get("cycles", 0) + 50
    popytki.append(("правка подписанного", pravlenoye))

    # 3. Подпись настоящая, но ключ не тот, что числится за этой машиной.
    chuzhoy = dict(nastoyashcheye)
    chuzhoy["cycles"] = 999
    chuzhoy["zayavlenie"] = podpisannoye(999999, nastoyashcheye, 999)
    popytki.append(("чужой ключ", chuzhoy))

    # 4. Машина, которой не выдавали ключ: её номер в реестре не числится.
    nezavedennyy = skhema.nomer_borta(PARK, "truck", f"{PARK[:3].upper()}-99")
    novichok = dict(nastoyashcheye)
    novichok["id"] = f"{PARK[:3].upper()}-99"
    novichok["cycles"] = 999
    novichok["zayavlenie"] = podpisannoye(nezavedennyy, nastoyashcheye, 999,
                                          imya=f"{PARK[:3].upper()}-99")
    popytki.append(("борт не в реестре", novichok))

    producer = Producer({"bootstrap.servers": BROKER})
    for imya, soobshchenie in popytki:
        producer.produce(TOPIC, json.dumps(soobshchenie, ensure_ascii=False).encode())
        print(f"положил в шину: {imya}")
    producer.flush(10)

    print()
    print("смотреть: curl -s localhost:8000/metrics | grep quarry_statement_rejected")
    print("ни в показателях, ни на экранах этих рейсов быть не должно")


if __name__ == "__main__":
    sys.exit(main())
