#!/usr/bin/env bash
# Зеркало внутри контура: раздаёт узлам файлы и образы, собранные collect.sh.
#
# На площадке заказчика это выделенная машина в их сети. Здесь ту же роль
# играет управляющая машина: у неё есть интерфейс в изолированной сети, а
# узлы наружу не ходят вообще.
#
#   ./mirror.sh start [адрес]    поднять (по умолчанию 192.168.200.1)
#   ./mirror.sh stop             остановить
#   ./mirror.sh status           что запущено и отвечает ли
#
# Ни nginx, ни docker не нужны: файлы раздаёт python3, образы - бинарник
# реестра, принесённый той же фазой сбора. Меньше зависимостей - меньше
# поводов для «у меня не воспроизводится».
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ARTIFACTS="${ARTIFACTS:-$REPO/cluster/artifacts/offline}"
RUN="$ARTIFACTS/run"
LOGS="$ARTIFACTS/logs"
ADDR="${2:-${MIRROR_ADDR:-192.168.200.1}}"
PORT_FILES="${PORT_FILES:-8080}"
PORT_REGISTRY="${PORT_REGISTRY:-5000}"

say() { printf '\n=== %s\n' "$*"; }

zapushchen() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

start() {
  mkdir -p "$RUN" "$LOGS" "$ARTIFACTS/files" "$ARTIFACTS/registry" "$ARTIFACTS/apt"
  [ -x "$ARTIFACTS/bin/registry" ] || { echo "нет бинарника реестра, сначала collect.sh images"; exit 1; }

  if zapushchen "$RUN/files.pid"; then
    echo "раздача файлов уже работает (pid $(cat "$RUN/files.pid"))"
  else
    say "раздача файлов: http://$ADDR:$PORT_FILES/"
    # Каталог отдаётся целиком: files/ для Kubespray и apt/ для узлов.
    nohup python3 -m http.server "$PORT_FILES" --bind "$ADDR" --directory "$ARTIFACTS" \
      > "$LOGS/files.log" 2>&1 &
    echo $! > "$RUN/files.pid"
  fi

  if zapushchen "$RUN/registry.pid"; then
    echo "реестр уже работает (pid $(cat "$RUN/registry.pid"))"
  else
    say "реестр образов: http://$ADDR:$PORT_REGISTRY/"
    cat > "$RUN/registry.yml" <<EOF
version: 0.1
log:
  level: warn
storage:
  filesystem:
    rootdirectory: $ARTIFACTS/registry
  delete:
    enabled: true
http:
  addr: $ADDR:$PORT_REGISTRY
EOF
    nohup "$ARTIFACTS/bin/registry" serve "$RUN/registry.yml" > "$LOGS/registry.log" 2>&1 &
    echo $! > "$RUN/registry.pid"
  fi

  # Ждём, пока оба начнут отвечать: без этого следующий шаг сбора успевает
  # ткнуться в ещё не поднявшийся порт и упасть на ровном месте.
  local i
  for i in $(seq 1 30); do
    curl -sf -m 2 "http://$ADDR:$PORT_REGISTRY/v2/" >/dev/null &&
      curl -sf -m 2 "http://$ADDR:$PORT_FILES/" >/dev/null && break
    sleep 1
  done
  status
}

stop() {
  local imya put
  for imya in files registry; do
    put="$RUN/$imya.pid"
    if zapushchen "$put"; then
      kill "$(cat "$put")" 2>/dev/null || true
      echo "остановлено: $imya"
    fi
    rm -f "$put"
  done
}

status() {
  say "зеркало на $ADDR"
  if zapushchen "$RUN/files.pid"; then
    printf 'файлы   pid %-7s %s\n' "$(cat "$RUN/files.pid")" \
      "$(curl -sf -m 3 -o /dev/null -w 'отвечает' "http://$ADDR:$PORT_FILES/" || echo 'НЕ отвечает')"
  else
    echo "файлы   не запущены"
  fi
  if zapushchen "$RUN/registry.pid"; then
    printf 'реестр  pid %-7s %s\n' "$(cat "$RUN/registry.pid")" \
      "$(curl -sf -m 3 -o /dev/null -w 'отвечает' "http://$ADDR:$PORT_REGISTRY/v2/" || echo 'НЕ отвечает')"
    curl -sf -m 3 "http://$ADDR:$PORT_REGISTRY/v2/_catalog" | head -c 600
    echo
  else
    echo "реестр  не запущен"
  fi
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  *) echo "команды: start [адрес] | stop | status"; exit 1 ;;
esac
