#!/usr/bin/env bash
# Выкат карьера в кластер. Всё, что нужно, уже лежит в реестре внутри контура.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
APPS="$REPO/cluster/apps"

say() { printf '\n=== %s\n' "$*"; }

say "Kafka и топики"
kubectl apply -f "$APPS/20-kafka.yaml" >/dev/null
kubectl -n quarry rollout status sts/kafka --timeout=300s

say "журнал событий"
kubectl apply -f "$APPS/15-postgres.yaml" >/dev/null
kubectl -n quarry rollout status sts/postgres --timeout=300s

say "дашборды как ConfigMap"
# Дашборды одни на весь стенд: и docker compose, и кластер берут те же файлы.
kubectl create configmap grafana-dashboard-quarry -n quarry \
  --from-file="$REPO/grafana/dashboards/" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

say "карьер и мониторинг"
kubectl apply -f "$APPS/30-quarry.yaml" >/dev/null
kubectl apply -f "$APPS/40-monitoring.yaml" >/dev/null

say "жду готовности"
for d in collector sim-fixed sim-balanced prometheus grafana; do
  kubectl -n quarry rollout status "deploy/$d" --timeout=240s
done

say "что работает"
kubectl -n quarry get pods -o wide
echo
NODE_IP="$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
echo "Grafana:    http://$NODE_IP:30300  (admin / quarry)"
echo "Prometheus: http://$NODE_IP:30090"
