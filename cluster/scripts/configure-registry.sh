#!/usr/bin/env bash
# Узлы кластера должны знать, где внутренний реестр, и ходить только туда.
#
# Имя registry.quarry.local:5000 в манифестах постоянное, а физический адрес
# реестра подставляется здесь. Меняется адрес, манифесты не трогаются.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TF_DIR="${TF_DIR:-$REPO/cluster/terraform}"
SSH_KEY="${SSH_KEY:-/root/.ssh/quarry-lab}"
SSH_USER="${SSH_USER:-ubuntu}"

mapfile -t NODES < <(
  TF_DATA_DIR="${TF_DATA_DIR:-/root/.tf-quarry}" terraform -chdir="$TF_DIR" output -json nodes |
    python3 -c 'import json,sys; [print(k,v) for k,v in sorted(json.load(sys.stdin).items())]'
)
NODE_IP="$(echo "${NODES[0]}" | cut -d' ' -f2)"
REGISTRY="$NODE_IP:30500"

for entry in "${NODES[@]}"; do
  name="$(echo "$entry" | cut -d' ' -f1)"
  ip="$(echo "$entry" | cut -d' ' -f2)"
  echo "=== $name ($ip)"
  # На рабочих узлах каталога /etc/rancher/k3s нет: его заводит только сервер.
  ssh -o StrictHostKeyChecking=no -i "$SSH_KEY" "$SSH_USER@$ip" "sudo mkdir -p /etc/rancher/k3s
  sudo tee /etc/rancher/k3s/registries.yaml >/dev/null <<'EOF'
mirrors:
  \"registry.quarry.local:5000\":
    endpoint:
      - \"http://$REGISTRY\"
EOF
  if systemctl is-active --quiet k3s; then sudo systemctl restart k3s; else sudo systemctl restart k3s-agent; fi"
done

echo
echo "реестр для узлов: registry.quarry.local:5000 -> http://$REGISTRY"
