"""Шлюз EGTS: принимает телеметрию бортов и кладёт её в шину.

На площадке между техникой и системой всегда стоит такой шлюз: терминалы
говорят по своему протоколу поверх TCP, а дальше живёт обычная обработка.
Здесь он занимает ровно то место, где раньше симулятор писал в Kafka напрямую.

    борт (EGTS/TCP) -> шлюз -> Kafka -> приёмник -> Prometheus -> Grafana

Что важно в шлюзе на реальной площадке и сделано здесь:

* подтверждение приёма. Борт обязан хранить запись, пока сервер не ответил.
  Молчащий шлюз означает, что терминалы копят буфер до отказа;
* устойчивость к обрывам. Половина пакета в TCP это норма, а не ошибка;
* счётчики. Сколько пришло, сколько записей, сколько битых сумм, сколько
  бортов на связи: без этого «данные не идут» невозможно разобрать.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from confluent_kafka import Producer
from prometheus_client import Counter, Gauge, start_http_server

from protocol import (
    PT_APPDATA,
    SR_AD_SENSORS_DATA,
    SR_POS_DATA,
    SR_TERM_IDENTITY,
    Datchiki,
    EgtsError,
    NeedMoreData,
    Poziciya,
    Predstavlenie,
    razobrat_paket,
    razobrat_zapisi,
    sobrat_otvet,
)
import skhema

BROKER = os.getenv("KAFKA_BROKER", "kafka:9092")
TOPIC_TELEMETRY = os.getenv("TOPIC_TELEMETRY", "quarry.telemetry")
PORT = int(os.getenv("EGTS_PORT", "7777"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "8090"))

PAKETY = Counter("egts_packets_total", "Принято пакетов EGTS", ["tip"])
ZAPISI = Counter("egts_records_total", "Принято записей", ["kind"])
OSHIBKI = Counter("egts_errors_total", "Ошибки разбора", ["prichina"])
BORTA = Gauge("egts_connections", "Бортов на связи")
POSLEDNIY = Gauge("egts_last_record_wall", "Время последней принятой записи")


def sozdat_producera() -> Producer:
    return Producer({
        "bootstrap.servers": BROKER,
        "linger.ms": 20,
        "compression.type": "lz4",
        "queue.buffering.max.messages": 200000,
    })


def zapis_v_soobshchenie(zapis) -> dict | None:
    """EGTS-запись -> то же сообщение, что раньше слал борт напрямую.

    Приёмник за шлюзом не переписан ни на строку: он как читал телеметрию
    карьера, так и читает. Смена протокола на борту не должна доходить до
    обработки данных.
    """
    poziciya = None
    datchiki = None
    for podzapis in zapis.podzapisi:
        if podzapis.tip == SR_POS_DATA:
            poziciya = Poziciya.iz_baytov(podzapis.dannye)
        elif podzapis.tip == SR_AD_SENSORS_DATA:
            datchiki = Datchiki.iz_baytov(podzapis.dannye)
    if poziciya is None or zapis.obyekt is None:
        return None

    park, tip, imya = skhema.razobrat_nomer(zapis.obyekt)
    x, y = skhema.v_metry(poziciya.shirota, poziciya.dolgota)
    analogovye = datchiki.analogovye if datchiki else {}
    milliskundy = analogovye.get(skhema.ADS_MS, 0) / 1000.0

    soobshchenie = {
        "fleet": park,
        "strategy": park,
        "t_sim": round(analogovye.get(skhema.ADS_T_SIM, 0), 1),
        "kind": tip,
        "id": imya,
        # Время формирования записи НА БОРТУ, а не время приёма: иначе
        # задержка доставки всегда выходит нулевой, и мерить нечего.
        "t_wall": poziciya.vremya + milliskundy,
    }

    if tip == "truck":
        soobshchenie.update({
            "state": skhema.SOSTOYANIYA_TRUCK_OBRATNO.get(
                analogovye.get(skhema.ADS_TRUCK_STATE, 0), "unknown"),
            "x": round(x, 1),
            "y": round(y, 1),
            "face": skhema.ZABOI_OBRATNO.get(analogovye.get(skhema.ADS_TRUCK_FACE, 0)),
            "cycles": analogovye.get(skhema.ADS_TRUCK_CYCLES, 0),
            "loaded": bool(poziciya.din & skhema.DIN_LOADED),
        })
    else:
        soobshchenie.update({
            "state": skhema.SOSTOYANIYA_EXC_OBRATNO.get(
                analogovye.get(skhema.ADS_EXC_STATE, 0), "unknown"),
            "queue": analogovye.get(skhema.ADS_EXC_QUEUE, 0),
            "idle_sec": float(analogovye.get(skhema.ADS_EXC_IDLE, 0)),
            "busy_sec": float(analogovye.get(skhema.ADS_EXC_BUSY, 0)),
            "tons": analogovye.get(skhema.ADS_EXC_TONS, 0) / 10.0,
        })
    return soobshchenie


class Shlyuz:
    def __init__(self, producer: Producer):
        self.producer = producer
        self.nomer_otveta = 0

    def sleduyushchiy_nomer(self) -> int:
        self.nomer_otveta = (self.nomer_otveta + 1) & 0xFFFF
        return self.nomer_otveta

    async def obsluzhit(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        adres = writer.get_extra_info("peername")
        BORTA.inc()
        bufer = b""
        terminal = None
        try:
            while True:
                kusok = await reader.read(65536)
                if not kusok:
                    break
                bufer += kusok
                while bufer:
                    try:
                        tip, nomer, telo, dlina = razobrat_paket(bufer)
                    except NeedMoreData:
                        break
                    except EgtsError as oshibka:
                        # Битый пакет не повод рвать соединение целиком, но и
                        # разобрать поток дальше нельзя: сдвигаемся на байт и
                        # ищем следующий заголовок.
                        OSHIBKI.labels(prichina=str(oshibka)[:40]).inc()
                        bufer = bufer[1:]
                        continue
                    bufer = bufer[dlina:]
                    if tip != PT_APPDATA:
                        continue
                    PAKETY.labels(tip="appdata").inc()
                    nomera = []
                    for zapis in razobrat_zapisi(telo):
                        nomera.append(zapis.nomer)
                        predstavilsya = any(p.tip == SR_TERM_IDENTITY for p in zapis.podzapisi)
                        if predstavilsya:
                            for p in zapis.podzapisi:
                                if p.tip == SR_TERM_IDENTITY:
                                    terminal = Predstavlenie.iz_baytov(p.dannye).terminal
                            continue
                        try:
                            soobshchenie = zapis_v_soobshchenie(zapis)
                        except EgtsError as oshibka:
                            OSHIBKI.labels(prichina=str(oshibka)[:40]).inc()
                            continue
                        if soobshchenie is None:
                            continue
                        ZAPISI.labels(kind=soobshchenie["kind"]).inc()
                        POSLEDNIY.set(soobshchenie["t_wall"])
                        self.otpravit(soobshchenie)
                    if nomera:
                        # Подтверждаем после постановки в очередь отправки, а
                        # не после записи в Kafka: иначе борт ждёт круг по сети
                        # на каждую запись. Цена честная и названа: пакет,
                        # принятый перед падением шлюза, может потеряться.
                        writer.write(sobrat_otvet(nomer, nomera, self.sleduyushchiy_nomer()))
                        await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            BORTA.dec()
            writer.close()
            # Проверка живости от Kubernetes открывает и закрывает порт каждые
            # десять секунд. Если писать в журнал каждое такое соединение,
            # настоящие отключения бортов в нём утонут.
            if terminal is not None:
                print(f"борт отключился: {adres}, терминал {terminal}", flush=True)

    def otpravit(self, soobshchenie: dict):
        telo = json.dumps(soobshchenie, ensure_ascii=False).encode()
        try:
            self.producer.produce(TOPIC_TELEMETRY, telo)
        except BufferError:
            self.producer.poll(0.2)
            try:
                self.producer.produce(TOPIC_TELEMETRY, telo)
            except BufferError:
                OSHIBKI.labels(prichina="очередь Kafka переполнена").inc()
        self.producer.poll(0)


async def main():
    start_http_server(METRICS_PORT)
    producer = sozdat_producera()
    shlyuz = Shlyuz(producer)

    async def sbrasyvat():
        while True:
            await asyncio.sleep(1)
            producer.poll(0)

    server = await asyncio.start_server(shlyuz.obsluzhit, "0.0.0.0", PORT)
    print(f"шлюз EGTS слушает {PORT}, метрики на {METRICS_PORT}", flush=True)
    asyncio.create_task(sbrasyvat())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("остановлен", flush=True)
