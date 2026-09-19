#!/usr/bin/env bash
# Третий голос для пары хранилища: запуск арбитра на шлюзе контура.
#
# Арбитр живёт ВНЕ обеих машин хранилища, иначе он не голос, а мнение одной
# из сторон. Здесь это шлюз контура, тот же, где стоит зеркало: машина, без
# которой пары не существует вообще, поэтому его отказ не добавляет нового
# класса аварий.
#
#   ./arbitr.sh start    поднять арбитра
#   ./arbitr.sh status   кто сейчас держит право быть главным
#   ./arbitr.sh stop     снять арбитра
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${ARBITR_PORT:-8099}"
RUN="${RUN:-/run/quarry-arbitr}"
ADRES="${ADRES:-192.168.200.1}"
mkdir -p "$RUN"

say() { printf '\n=== %s\n' "$*"; }

start() {
  say "арбитр пары хранилища на $ADRES:$PORT"
  if [ -s "$RUN/pid" ] && kill -0 "$(cat "$RUN/pid")" 2>/dev/null; then
    echo "уже работает (pid $(cat "$RUN/pid"))"
  else
    nohup python3 "$HERE/arbitr.py" "$PORT" > "$RUN/log" 2>&1 &
    echo $! > "$RUN/pid"
    sleep 1
    echo "запущен (pid $(cat "$RUN/pid"))"
  fi
  status
}

status() {
  if curl -sf --max-time 3 "http://$ADRES:$PORT/status"; then
    :
  else
    echo "НЕ отвечает"
    return 1
  fi
}

stop() {
  say "снимаю арбитра"
  # Точечно по записанному номеру: массовое гашение по имени задело бы
  # другие процессы python на шлюзе, включая зеркало контура.
  if [ -s "$RUN/pid" ]; then
    kill "$(cat "$RUN/pid")" 2>/dev/null || true
    rm -f "$RUN/pid"
    echo "снят"
  else
    echo "не был запущен"
  fi
}

case "${1:-status}" in
  start)  start ;;
  status) status ;;
  stop)   stop ;;
  *) echo "команды: start | status | stop"; exit 1 ;;
esac
