"""Третий голос для пары хранилища: кто держит замок, тот и главный.

Зачем он нужен. Две машины не могут отличить «сосед умер» от «связь с
соседом пропала»: картина у обеих одинаковая. При разрыве между ними каждая
считает себя единственной живой, обе берут том в запись, и копии расходятся
необратимо. Спросить не у кого: голосов ровно два, и большинства не бывает.

Этот арбитр и есть третий голос. Он живёт на шлюзе контура, вне обеих машин,
и умеет ровно одно: выдать право быть главным одной машине и не выдать его
второй, пока первая это право продлевает.

🔴 Спрашивают его НЕ всегда. Пока машины видят друг друга по каналу реплики,
спорить не о чем, и арбитр не нужен: DRBD сам не даст двум сторонам стать
Primary. Замок берётся только тогда, когда соседа не видно, то есть ровно в
той ситуации, ради которой он и заведён. Поэтому падение арбитра не мешает
работе пары: оно мешает только переезду во время разрыва, когда переезжать и
не следует.

⚠️ Это не кворум-устройство промышленного уровня. Настоящее живёт на третьей
машине и голосует вместе с остальными; здесь же одна точка, совмещённая со
шлюзом. Выбор осознанный: шлюз это тот самый гипервизор, без которого обеих
машин не существует, поэтому его отказ не добавляет нового класса аварий.

    python3 arbitr.py [порт]
"""
from __future__ import annotations

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# Сколько живёт право быть главным без продления. Держатель продлевает его
# каждые несколько секунд (проверка keepalived ходит раз в пять), поэтому
# пятнадцати хватает с запасом, а машина, которая умерла по-настоящему,
# освобождает замок за то же время.
ARENDA_SEK = 15.0

zamok = {"kto": None, "do": 0.0}
lock = threading.Lock()


def vzyat(kto: str) -> tuple[bool, str]:
    """Выдать или продлить право. Второму отказываем, пока первый жив."""
    teper = time.time()
    with lock:
        derzhatel = zamok["kto"]
        istek = zamok["do"] <= teper

        if derzhatel is None or istek or derzhatel == kto:
            prezhniy = derzhatel
            zamok["kto"] = kto
            zamok["do"] = teper + ARENDA_SEK
            if derzhatel == kto:
                return True, "продлён"
            return True, "выдан" if prezhniy is None or istek else "перехвачен"
        return False, f"занят машиной {derzhatel}"


class Obrabotchik(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - имя задано библиотекой
        put = urlparse(self.path)
        parametry = parse_qs(put.query)
        kto = (parametry.get("kto") or [""])[0].strip()

        if put.path == "/zamok" and kto:
            dali, pochemu = vzyat(kto)
            otvet = f"{'da' if dali else 'net'} {pochemu}\n"
            # Решение арбитра попадает в журнал шлюза: после аварии это
            # единственное место, где видно, кому и когда дали право.
            print(f"{time.strftime('%H:%M:%S')} {kto}: {otvet.strip()}", flush=True)
            self._otvetit(200 if dali else 409, otvet)
            return

        if put.path in ("/", "/status"):
            with lock:
                ostalos = max(0.0, zamok["do"] - time.time())
                kto_derzhit = zamok["kto"] if ostalos > 0 else None
            self._otvetit(200, f"держит: {kto_derzhit or 'никто'}, осталось {ostalos:.0f} с\n")
            return

        self._otvetit(404, "нет такого\n")

    def log_message(self, *args):  # обычные обращения в журнал не пишем
        pass

    def _otvetit(self, kod: int, telo: str):
        dannye = telo.encode("utf-8")
        self.send_response(kod)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(dannye)))
        self.end_headers()
        self.wfile.write(dannye)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    server = HTTPServer(("0.0.0.0", port), Obrabotchik)
    print(f"арбитр пары хранилища слушает {port}, аренда {ARENDA_SEK:.0f} с", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
