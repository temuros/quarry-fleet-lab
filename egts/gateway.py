"""Приёмный шлюз бортов: EGTS, Wialon IPS и подписанные заявления в одну шину.

На площадке между техникой и системой всегда стоит такой шлюз: терминалы
говорят по своему протоколу поверх TCP, а дальше живёт обычная обработка.
Здесь он занимает ровно то место, где раньше симулятор писал в Kafka напрямую.

    борт (EGTS/TCP        :7777) ┐
    борт (Wialon IPS/TCP  :7778) ├-> шлюз -> Kafka -> приёмник -> Prometheus
    борт (заявления/UDP   :7779) ┘

🔴 Протокола три намеренно. На карьере техника редко одного поколения: часть
машин отдаёт EGTS по ГОСТ, часть настроена на Wialon IPS. Система, умеющая
один протокол, разворачивается там «после замены терминалов», то есть после
отдельного проекта с деньгами и простоем. Третий протокол отвечает на другой
вопрос: не «дошло ли», а «этот отсчёт правда с борта и его не правили».

⚠️ Подпись заявления шлюз НЕ проверяет и проверять не должен. Шлюз стоит
внутри периметра диспетчерской, и проверка здесь доказывала бы только то, что
диспетчерская не подделывает данные сама у себя. Заявление едет через шину
целиком, вместе с подписью, и проверяет его приёмник показателей.

И это же проверяет главное свойство конструкции: все три протокола сходятся в
ОДНО сообщение шины, и дальше по потоку никто не знает, каким протоколом
пришли данные. Приёмник показателей, журнал смены и экраны не переписаны ни
на строку ради второго и третьего протоколов. Третий добавляет к сообщению
одно поле, подпись, и не трогает ни одного прежнего.

Что важно в шлюзе на реальной площадке и сделано здесь:

* подтверждение приёма. Борт обязан хранить запись, пока сервер не ответил.
  Молчащий шлюз означает, что терминалы копят буфер до отказа;
* устойчивость к обрывам. Половина пакета в TCP это норма, а не ошибка;
* счётчики. Сколько пришло, сколько записей, сколько битых сумм, сколько
  бортов на связи: без этого «данные не идут» невозможно разобрать.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
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
import ips
import skhema
import zayavlenie

BROKER = os.getenv("KAFKA_BROKER", "kafka:9092")
TOPIC_TELEMETRY = os.getenv("TOPIC_TELEMETRY", "quarry.telemetry")
PORT = int(os.getenv("EGTS_PORT", "7777"))
WIALON_PORT = int(os.getenv("WIALON_PORT", "7778"))
STATEMENT_PORT = int(os.getenv("STATEMENT_PORT", "7779"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "8090"))

# ⚠️ Счётчики названы по сущности, а не по протоколу: протокол стал меткой.
# Пока метрика звалась egts_records_total, второй протокол пришлось бы либо
# считать в ней же (и врать именем), либо завести рядом вторую (и складывать
# их в каждом запросе). Метка решает это один раз.
PAKETY = Counter("bort_packets_total", "Принято пакетов", ["protokol", "tip"])
ZAPISI = Counter("bort_records_total", "Принято записей", ["protokol", "kind"])
OSHIBKI = Counter("bort_errors_total", "Ошибки разбора", ["protokol", "prichina"])
BORTA = Gauge("bort_connections", "Бортов на связи", ["protokol"])
POSLEDNIY = Gauge("bort_last_record_wall", "Время последней принятой записи", ["protokol"])

# Отсчёты, пришедшие после своего срока годности. Их не кладут в шину, и это
# главный показатель третьего протокола: видно, сколько данных сознательно
# выброшено вместо того, чтобы залить очередь устаревшим.
PROTUHLO = Counter("bort_statement_expired_total",
                   "Заявлений отвергнуто по сроку годности", ["protokol"])
ZHIZN = Gauge("bort_statement_life_seconds",
              "Сколько оставалось жить принятому заявлению", ["protokol"])
# Сколько отсчётов борт выбросил у себя, не отправляя. Приходит отдельным
# подписанным отчётом после восстановления канала: иначе потеря данных на
# борту остаётся молчаливой, а в потоке видно только разрыв.
NA_BORTU = Counter("bort_statement_dropped_on_board_total",
                   "Отсчётов выброшено на борту по сроку годности", ["protokol"])


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
    """Приём EGTS. Отправку в шину делит с приёмом Wialon IPS."""

    protokol = "egts"

    def __init__(self, producer: Producer):
        self.producer = producer
        self.nomer_otveta = 0

    def sleduyushchiy_nomer(self) -> int:
        self.nomer_otveta = (self.nomer_otveta + 1) & 0xFFFF
        return self.nomer_otveta

    async def obsluzhit(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        adres = writer.get_extra_info("peername")
        BORTA.labels(protokol="egts").inc()
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
                        OSHIBKI.labels(protokol="egts", prichina=str(oshibka)[:40]).inc()
                        bufer = bufer[1:]
                        continue
                    bufer = bufer[dlina:]
                    if tip != PT_APPDATA:
                        continue
                    PAKETY.labels(protokol="egts", tip="appdata").inc()
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
                            OSHIBKI.labels(protokol="egts", prichina=str(oshibka)[:40]).inc()
                            continue
                        if soobshchenie is None:
                            continue
                        ZAPISI.labels(protokol="egts", kind=soobshchenie["kind"]).inc()
                        POSLEDNIY.labels(protokol="egts").set(soobshchenie["t_wall"])
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
            BORTA.labels(protokol="egts").dec()
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
                OSHIBKI.labels(protokol=self.protokol, prichina="очередь Kafka переполнена").inc()
        self.producer.poll(0)


class ShlyuzWialon(Shlyuz):
    """Приём Wialon IPS. Отличается разбором, но не тем, что уходит в шину.

    🔴 Соединение здесь принадлежит ОДНОЙ машине: терминал называет себя
    один раз пакетом входа, и все последующие данные его. Поэтому номер
    борта берётся из входа и помнится на всё соединение, а не читается из
    каждой записи, как в EGTS.
    """

    protokol = "wialon"

    async def obsluzhit(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        adres = writer.get_extra_info("peername")
        BORTA.labels(protokol="wialon").inc()
        # ⚠️ Ответы здесь короткие, и ядро придержало бы их, ожидая попутных
        # данных (алгоритм Нагла). Борт в это время ждёт подтверждения и
        # ничего не шлёт: ровно тот случай, когда придерживание добавляет
        # задержку на пустом месте.
        sokket = writer.get_extra_info("socket")
        if sokket is not None:
            sokket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        imei = None
        try:
            while True:
                # Пакет заканчивается переводом строки, и readline сам
                # собирает его из кусков: в TCP половина строки это норма.
                stroka = await reader.readline()
                if not stroka:
                    break
                try:
                    tip, telo = ips.razobrat_stroku(stroka.decode("ascii", "replace"))
                except ips.WialonError as oshibka:
                    OSHIBKI.labels(protokol="wialon", prichina=str(oshibka)[:40]).inc()
                    continue

                PAKETY.labels(protokol="wialon", tip=tip.lower()).inc()

                if tip == "L":
                    try:
                        imei = ips.razobrat_vhod(telo)
                    except ips.WialonError as oshibka:
                        OSHIBKI.labels(protokol="wialon", prichina=str(oshibka)[:40]).inc()
                        writer.write(ips.otvet("L", 0).encode())
                        await writer.drain()
                        continue
                    writer.write(ips.otvet("L").encode())
                    await writer.drain()
                    continue

                if tip == "P":
                    writer.write(ips.otvet("P", "").encode())
                    await writer.drain()
                    continue

                if imei is None:
                    # Данные без входа принимать нельзя: непонятно, чья это
                    # машина, а угадывать по адресу соединения значит рано
                    # или поздно приписать рейсы чужому борту.
                    OSHIBKI.labels(protokol="wialon", prichina="данные до входа").inc()
                    writer.write(ips.otvet("D", 0).encode())
                    await writer.drain()
                    continue

                if tip == "D":
                    prinyato = self._prinyat(imei, [telo])
                    writer.write(ips.otvet("D", 1 if prinyato else 0).encode())
                    await writer.drain()
                elif tip == "B":
                    tela = [t for t in telo.split("|") if t]
                    prinyato = self._prinyat(imei, tela)
                    # На досылку отвечаем числом принятых записей: борт
                    # удалит из своего буфера ровно их и повторит остальное.
                    writer.write(ips.otvet("B", prinyato).encode())
                    await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            BORTA.labels(protokol="wialon").dec()
            writer.close()
            if imei is not None:
                print(f"борт отключился: {adres}, терминал {imei}", flush=True)

    def _prinyat(self, imei: str, tela: list[str]) -> int:
        prinyato = 0
        for telo in tela:
            try:
                razobrannoe = ips.razobrat_dannye(telo)
            except ips.WialonError as oshibka:
                OSHIBKI.labels(protokol="wialon", prichina=str(oshibka)[:40]).inc()
                continue
            soobshchenie = ips.soobshchenie(imei, razobrannoe)
            if soobshchenie is None:
                OSHIBKI.labels(protokol="wialon", prichina="неизвестный борт").inc()
                continue
            ZAPISI.labels(protokol="wialon", kind=soobshchenie["kind"]).inc()
            POSLEDNIY.labels(protokol="wialon").set(soobshchenie["t_wall"])
            self.otpravit(soobshchenie)
            prinyato += 1
        return prinyato


class ShlyuzZayavleniy(Shlyuz, asyncio.DatagramProtocol):
    """Приём подписанных заявлений по UDP.

    🔴 Соединения здесь нет вовсе, и это не упрощение стенда, а свойство
    протокола: одно заявление это одна дейтаграмма, подтверждения не будет.
    Поэтому «бортов на связи» считается не по сокетам, а по номерам бортов,
    от которых что-то приходило в последние полминуты.

    ⚠️ Что шлюз проверяет, а что нет. Проверяет срок годности, канал и то,
    что заявление вообще разбирается: всё это не требует ключей и отсекает
    мусор до шины. Подпись НЕ проверяет: её проверяет приёмник показателей,
    и только там проверка что-то доказывает.
    """

    protokol = "statement"
    PAMYAT_SEK = 30.0

    def __init__(self, producer: Producer):
        super().__init__(producer)
        self.vidno: dict[int, float] = {}
        # Отчёт борта несёт счётчик с начала работы, а Prometheus считает
        # приращениями: помним прошлое значение по каждому борту.
        self.vybrosheno: dict[int, int] = {}

    def datagram_received(self, dannye: bytes, adres):  # noqa: N802 - имя из asyncio
        PAKETY.labels(protokol="statement", tip="datagram").inc()
        try:
            z = zayavlenie.Zayavlenie.iz_baytov(dannye)
            razobrannoe = zayavlenie.razobrat_telo(z.telo)
        except zayavlenie.ZayavlenieError as oshibka:
            OSHIBKI.labels(protokol="statement", prichina=str(oshibka)[:40]).inc()
            return
        if z.podpis is None:
            OSHIBKI.labels(protokol="statement", prichina="заявление без подписи").inc()
            return
        if z.kanal != zayavlenie.KANAL_TELEMETRIYA:
            OSHIBKI.labels(protokol="statement", prichina="чужой канал").inc()
            return

        ostalos = razobrannoe["godno_do"] - time.time()
        if ostalos < 0:
            # Протухшее в шину не идёт. Такое приходит либо после
            # восстановления канала, либо когда часы борта разошлись с
            # серверными: и то и другое лучше видеть счётчиком, чем в данных.
            PROTUHLO.labels(protokol="statement").inc()
            return
        ZHIZN.labels(protokol="statement").set(ostalos)
        self.vidno[razobrannoe["oid"]] = time.monotonic()

        if razobrannoe["kind"] == "otchet":
            self._prinyat_otchet(razobrannoe)
            return

        soobshchenie = zayavlenie.v_soobshchenie(razobrannoe)
        # Заявление едет через шину целиком: приёмник показателей проверяет
        # подпись по тем же байтам, которые подписал борт, а не по нашему
        # пересказу. Иначе шлюзу пришлось бы верить на слово.
        soobshchenie["zayavlenie"] = base64.b64encode(dannye).decode()
        ZAPISI.labels(protokol="statement", kind=soobshchenie["kind"]).inc()
        POSLEDNIY.labels(protokol="statement").set(soobshchenie["t_wall"])
        self.otpravit(soobshchenie)

    def _prinyat_otchet(self, razobrannoe: dict):
        """Отчёт борта о выброшенном. В шину не идёт: это не телеметрия.

        ⚠️ Подпись отчёта шлюз тоже не проверяет, и счётчику поэтому можно
        верить ровно настолько, насколько доверяешь сети. Отчёт нужен, чтобы
        объяснить разрыв в потоке, а не чтобы на него ссылаться в споре: для
        спора есть подписанные отсчёты, которые дошли.
        """
        oid = razobrannoe["oid"]
        bylo = self.vybrosheno.get(oid, 0)
        stalo = razobrannoe["ustarelo"]
        if stalo > bylo:
            NA_BORTU.labels(protokol="statement").inc(stalo - bylo)
            self.vybrosheno[oid] = stalo
        PAKETY.labels(protokol="statement", tip="otchet").inc()

    def peresschitat_borta(self):
        porog = time.monotonic() - self.PAMYAT_SEK
        self.vidno = {oid: kogda for oid, kogda in self.vidno.items() if kogda >= porog}
        BORTA.labels(protokol="statement").set(len(self.vidno))


async def main():
    start_http_server(METRICS_PORT)
    # 🔴 Счётчики с метками заводятся заранее, нулями. Счётчик, который
    # появляется в момент первого события, для `rate()` невидим: первая точка
    # ряда становится основанием отсчёта, и скачок с нуля до трёх тысяч даёт
    # на графике ровный ноль. Найдено на стенде: панель молчала при
    # заполненном счётчике.
    PROTUHLO.labels(protokol="statement")
    NA_BORTU.labels(protokol="statement")
    producer = sozdat_producera()
    shlyuz = Shlyuz(producer)
    shlyuz_wialon = ShlyuzWialon(producer)
    shlyuz_zayavleniy = ShlyuzZayavleniy(producer)

    async def sbrasyvat():
        while True:
            await asyncio.sleep(1)
            producer.poll(0)
            shlyuz_zayavleniy.peresschitat_borta()

    server = await asyncio.start_server(shlyuz.obsluzhit, "0.0.0.0", PORT)
    server_wialon = await asyncio.start_server(shlyuz_wialon.obsluzhit, "0.0.0.0", WIALON_PORT)
    # ⚠️ Приём дейтаграмм это не сервер: `serve_forever` у него нет, объект
    # живёт, пока жив цикл событий. Потерять ссылку на транспорт значит
    # получить молча закрытый порт.
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: shlyuz_zayavleniy, local_addr=("0.0.0.0", STATEMENT_PORT))
    print(f"шлюз слушает EGTS {PORT}, Wialon IPS {WIALON_PORT}, "
          f"заявления по UDP {STATEMENT_PORT}, метрики на {METRICS_PORT}", flush=True)
    asyncio.create_task(sbrasyvat())
    try:
        async with server, server_wialon:
            await asyncio.gather(server.serve_forever(), server_wialon.serve_forever())
    finally:
        transport.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("остановлен", flush=True)
