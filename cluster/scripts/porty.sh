#!/usr/bin/env bash
# Доступ к экранам стенда с управляющей машины.
#
# Узлы живут в изолированной сети, поэтому смотреть на них можно только через
# управляющую машину: ровно как на площадке, где в контур ходят через бастион.
#
# ⚠️ Проброс порта живёт, пока жив под на той стороне. Каждое пересоздание
# (выкат, переезд тома, учение) рвёт его, и экран «перестаёт открываться»,
# хотя со стендом всё в порядке. Поэтому каждый проброс поднимается в цикле:
# порвался, подождал, поднялся снова.
#
#   ./porty.sh start    поднять пробросы
#   ./porty.sh status   что сейчас слушается
#   ./porty.sh stop     снять пробросы
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$REPO/cluster/kubespray/kubeconfig}"
RUN="${RUN:-/run/quarry-porty}"
mkdir -p "$RUN"

# экран : пространство : служба : порт снаружи : порт службы
EKRANY=(
  "Grafana:quarry:svc/grafana:3003:3000"
  "ArgoCD:argocd:svc/argocd-server:3080:80"
  "Prometheus:quarry:svc/prometheus:3090:9090"
  "Тревоги:quarry:svc/alertmanager:3093:9093"
  "Git-сервер:quarry-infra:svc/gitea:3030:3000"
)

say() { printf '\n=== %s\n' "$*"; }

start() {
  say "поднимаю пробросы"
  for zapis in "${EKRANY[@]}"; do
    IFS=: read -r imya ns sluzhba snaruzhi vnutri <<< "$zapis"
    local pidfile="$RUN/$snaruzhi.pid"
    if [ -s "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      echo "$imya уже на :$snaruzhi"
      continue
    fi
    # Цикл вокруг port-forward: под пересоздадут, проброс упадёт, и через
    # секунду он поднимется сам. Без этого экран открывается через раз.
    nohup bash -c "while true; do
        kubectl -n '$ns' port-forward --address 0.0.0.0 '$sluzhba' '$snaruzhi:$vnutri' >/dev/null 2>&1
        sleep 2
      done" > "$RUN/$snaruzhi.log" 2>&1 &
    echo $! > "$pidfile"
    echo "$imya -> http://localhost:$snaruzhi"
  done
  sleep 6
  status
}

status() {
  say "что слушается"
  for zapis in "${EKRANY[@]}"; do
    IFS=: read -r imya ns sluzhba snaruzhi vnutri <<< "$zapis"
    if ss -tln 2>/dev/null | grep -q ":$snaruzhi "; then
      printf '%-12s http://localhost:%-6s отвечает\n' "$imya" "$snaruzhi"
    else
      printf '%-12s http://localhost:%-6s НЕ отвечает\n' "$imya" "$snaruzhi"
    fi
  done
  echo
  echo "Grafana: admin / quarry. Доступы к ArgoCD и git-серверу печатает"
  echo "cluster/gitops/bootstrap.sh status"
}

stop() {
  say "снимаю пробросы"
  for zapis in "${EKRANY[@]}"; do
    IFS=: read -r imya ns sluzhba snaruzhi vnutri <<< "$zapis"
    local pidfile="$RUN/$snaruzhi.pid"
    [ -s "$pidfile" ] || continue
    # Снимаем только свой процесс по записанному номеру: массовое гашение по
    # имени задело бы чужие пробросы в той же оболочке.
    pkill -P "$(cat "$pidfile")" 2>/dev/null || true
    kill "$(cat "$pidfile")" 2>/dev/null || true
    rm -f "$pidfile"
    echo "$imya снят"
  done
}

case "${1:-status}" in
  start)  start ;;
  status) status ;;
  stop)   stop ;;
  *) echo "команды: start | status | stop"; exit 1 ;;
esac
