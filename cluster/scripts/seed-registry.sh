#!/usr/bin/env bash
# Наполнение реестра внутри контура заранее принесёнными файлами.
#
# Порядок важен и он же самый неочевидный кусок закрытого контура:
# реестр не может взять свой собственный образ из реестра, поэтому
# registry:2 кладётся прямо в containerd каждого узла, и только потом
# туда заезжает всё остальное.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TF_DIR="${TF_DIR:-$REPO/cluster/terraform}"
ARTIFACTS="${ARTIFACTS:-$REPO/cluster/artifacts}"
SSH_KEY="${SSH_KEY:-/root/.ssh/quarry-lab}"
SSH_USER="${SSH_USER:-ubuntu}"

# файл с образом -> имя, под которым он ляжет в наш реестр
declare -A MIRROR=(
  ["mirror-apache-kafka-3.8.1.tar"]="mirror/kafka:3.8.1"
  ["mirror-prom-prometheus-v2.55.1.tar"]="mirror/prometheus:v2.55.1"
  ["mirror-grafana-grafana-11.3.0.tar"]="mirror/grafana:11.3.0"
  ["mirror-postgres-16-alpine.tar"]="mirror/postgres:16-alpine"
)

say() { printf '\n=== %s\n' "$*"; }

mapfile -t NODES < <(
  TF_DATA_DIR="${TF_DATA_DIR:-/root/.tf-quarry}" terraform -chdir="$TF_DIR" output -json nodes |
    python3 -c 'import json,sys; [print(k,v) for k,v in sorted(json.load(sys.stdin).items())]'
)
NODE_IP="$(echo "${NODES[0]}" | cut -d' ' -f2)"
REGISTRY="$NODE_IP:30500"

say "кладу registry:2 прямо в containerd узлов"
for entry in "${NODES[@]}"; do
  name="$(echo "$entry" | cut -d' ' -f1)"
  ip="$(echo "$entry" | cut -d' ' -f2)"
  echo "--- $name"
  scp -q -o StrictHostKeyChecking=no -i "$SSH_KEY" "$ARTIFACTS/mirror-registry-2.tar" "$SSH_USER@$ip:/tmp/"
  # k3s прячет containerd за своей обёрткой, у kubeadm он обычный.
  # Пространство имён k8s.io обязательно: иначе kubelet образа не увидит.
  ssh -o StrictHostKeyChecking=no -i "$SSH_KEY" "$SSH_USER@$ip" \
    'if command -v k3s >/dev/null; then
       sudo k3s ctr images import /tmp/mirror-registry-2.tar >/dev/null
       sudo k3s ctr images ls name~=registry | tail -1 | cut -c1-70
     else
       sudo ctr -n k8s.io images import /tmp/mirror-registry-2.tar >/dev/null
       sudo ctr -n k8s.io images ls | grep -m1 registry | cut -c1-70
     fi'
done

say "поднимаю реестр"
kubectl apply -f "$REPO/cluster/apps/10-registry.yaml" >/dev/null
kubectl -n quarry-infra rollout status deploy/registry --timeout=180s

say "жду, пока реестр начнёт отвечать"
until curl -sf -m 3 "http://$REGISTRY/v2/_catalog" >/dev/null; do sleep 3; done

say "заливаю чужие образы"
for file in "${!MIRROR[@]}"; do
  echo "--- ${MIRROR[$file]}"
  skopeo copy --dest-tls-verify=false \
    "docker-archive:$ARTIFACTS/$file" \
    "docker://$REGISTRY/${MIRROR[$file]}"
done

say "заливаю наши образы"
for name in sim collector; do
  echo "--- quarry/$name:local"
  skopeo copy --dest-tls-verify=false \
    "docker-archive:$ARTIFACTS/$name.tar" \
    "docker://$REGISTRY/quarry/$name:local"
done

say "что теперь лежит в реестре"
curl -s "http://$REGISTRY/v2/_catalog"
echo
