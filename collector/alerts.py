"""Приём тревог от Alertmanager и запись их в журнал смены.

Зачем тревога едет в журнал, а не только в почту. Отчёт по смене отвечает на
вопрос «почему вывезли меньше, чем собирались», и половина ответов это не
поломки техники, а отказы самой системы: встал приёмник, оборвался канал,
кончилось место. Если такие события живут отдельно от журнала, разбор смены
превращается в сопоставление двух несвязанных списков по часам.

Поэтому тревога попадает в ту же ленту, что и «машина встала» или «забой без
машин»: диспетчер видит её на своём экране, а начальник смены находит в
отчёте.

⚠️ Приёмник тревог намеренно примитивен: это несколько строк на стандартной
библиотеке, без веб-фреймворка. Внутри контура он доступен только
Alertmanager'у, а всё, что сложнее, пришлось бы тащить в закрытый периметр и
потом обновлять.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Как показывать тревогу человеку. Alertmanager присылает имя правила, а в
# ленте событий должно стоять то, что понятно без чтения конфигурации.
NAZVANIYA = {
    "QuarryTelemetryStopped": "данные с бортов не идут",
    "QuarryEventsStopped": "события смены не идут",
    "QuarryIngestLagHigh": "задержка приёма выше договорной",
    "QuarryQueueGrowing": "очередь записи в журнал растёт",
    "QuarryShiftBehindPlan": "наряд смены под угрозой",
    "QuarryCollectorDown": "приёмник показателей не отвечает",
    "QuarryGatewayDown": "приёмный шлюз не отвечает",
    "QuarryNodeDown": "узел кластера не отвечает",
}


class Obrabotchik(BaseHTTPRequestHandler):
    zhurnal = None

    def do_POST(self):  # noqa: N802 - имя задано библиотекой
        dlina = int(self.headers.get("Content-Length", 0))
        syroy = self.rfile.read(dlina) if dlina else b"{}"
        try:
            payload = json.loads(syroy.decode("utf-8", "replace"))
        except Exception:
            payload = {}

        for trevoga in payload.get("alerts", []):
            self._zapisat(trevoga)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def _zapisat(self, trevoga):
        labels = trevoga.get("labels", {})
        annotations = trevoga.get("annotations", {})
        imya = labels.get("alertname", "неизвестная тревога")
        status = trevoga.get("status", "firing")
        chitaemo = NAZVANIYA.get(imya, annotations.get("summary", imya))

        # Снятие тревоги пишем тоже: без него в отчёте видно, что началось, и
        # не видно, когда закончилось, а длительность и есть главный вопрос.
        vid = "тревога снята" if status == "resolved" else "тревога"
        prichina = annotations.get("description") or chitaemo

        print("[alerts] {}: {}".format(vid, chitaemo), flush=True)
        if self.zhurnal is not None:
            self.zhurnal.zapisat_trevogu(
                nazvanie=chitaemo,
                vid=vid,
                prichina=prichina,
                vazhnost=labels.get("severity", "warning"),
            )

    def log_message(self, *args):
        # Своя запись выше уже всё сказала, а стандартный журнал HTTP-сервера
        # засоряет вывод приёмника на каждой проверке.
        return


def zapustit(zhurnal, port=8001):
    """Поднять приём тревог в отдельном потоке."""
    Obrabotchik.zhurnal = zhurnal
    server = HTTPServer(("0.0.0.0", port), Obrabotchik)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("[alerts] принимаю тревоги на :{}".format(port), flush=True)
    return server
