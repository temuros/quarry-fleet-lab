#!/usr/bin/env bash
# Установка кластера k3s на машины, созданные Terraform.
#
# Адреса берутся из вывода Terraform, ничего не вбивается руками.
# Скрипт идемпотентный: повторный запуск ничего не ломает.
#
#   ./install.sh                 обычная установка, узлы тянут k3s из интернета
#   OFFLINE=1 ./install.sh       установка из заранее скачанных файлов
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${TF_DIR:-$HERE/../terraform}"
SSH_KEY="${SSH_KEY:-/root/.ssh/quarry-lab}"
SSH_USER="${SSH_USER:-ubuntu}"
OFFLINE="${OFFLINE:-0}"
ARTIFACTS="${ARTIFACTS:-$HERE/../artifacts}"
KUBECONFIG_OUT="${KUBECONFIG_OUT:-$HERE/kubeconfig}"

ssh_node() {
  local host="$1"; shift
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -i "$SSH_KEY" "$SSH_USER@$host" "$@"
}

say() { printf '\n=== %s\n' "$*"; }

say "адреса узлов из Terraform"
mapfile -t NODES < <(
  TF_DATA_DIR="${TF_DATA_DIR:-/root/.tf-quarry}" terraform -chdir="$TF_DIR" output -json nodes |
    python3 -c 'import json,sys; [print(k,v) for k,v in sorted(json.load(sys.stdin).items())]'
)
[ "${#NODES[@]}" -ge 2 ] || { echo "узлов меньше двух, нечего собирать"; exit 1; }
printf '%s\n' "${NODES[@]}"

SERVER_NAME="$(echo "${NODES[0]}" | cut -d' ' -f1)"
SERVER_IP="$(echo "${NODES[0]}" | cut -d' ' -f2)"

say "управляющий узел: $SERVER_NAME ($SERVER_IP)"
if ssh_node "$SERVER_IP" 'command -v k3s >/dev/null'; then
  echo "k3s уже стоит, пропускаю установку"
else
  if [ "$OFFLINE" = "1" ]; then
    echo "ставлю из локальных файлов, интернет узлу не нужен"
    scp -o StrictHostKeyChecking=no -i "$SSH_KEY" \
      "$ARTIFACTS/k3s" "$ARTIFACTS/k3s-install.sh" "$ARTIFACTS/k3s-airgap-images-amd64.tar" \
      "$SSH_USER@$SERVER_IP:/tmp/"
    ssh_node "$SERVER_IP" 'sudo install -m 755 /tmp/k3s /usr/local/bin/k3s
      sudo mkdir -p /var/lib/rancher/k3s/agent/images
      sudo cp /tmp/k3s-airgap-images-amd64.tar /var/lib/rancher/k3s/agent/images/
      sudo INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_EXEC="server --disable traefik --write-kubeconfig-mode 644" sh /tmp/k3s-install.sh'
  else
    ssh_node "$SERVER_IP" 'curl -sfL https://get.k3s.io | sudo INSTALL_K3S_EXEC="server --disable traefik --write-kubeconfig-mode 644" sh -'
  fi
fi

say "жду готовности управляющего узла"
ssh_node "$SERVER_IP" 'until sudo k3s kubectl get nodes >/dev/null 2>&1; do sleep 3; done; sudo k3s kubectl get nodes'

TOKEN="$(ssh_node "$SERVER_IP" 'sudo cat /var/lib/rancher/k3s/server/node-token')"

for entry in "${NODES[@]:1}"; do
  name="$(echo "$entry" | cut -d' ' -f1)"
  ip="$(echo "$entry" | cut -d' ' -f2)"
  say "рабочий узел: $name ($ip)"
  if ssh_node "$ip" 'command -v k3s >/dev/null'; then
    echo "k3s уже стоит, пропускаю"
    continue
  fi
  if [ "$OFFLINE" = "1" ]; then
    scp -o StrictHostKeyChecking=no -i "$SSH_KEY" \
      "$ARTIFACTS/k3s" "$ARTIFACTS/k3s-install.sh" "$ARTIFACTS/k3s-airgap-images-amd64.tar" \
      "$SSH_USER@$ip:/tmp/"
    ssh_node "$ip" "sudo install -m 755 /tmp/k3s /usr/local/bin/k3s
      sudo mkdir -p /var/lib/rancher/k3s/agent/images
      sudo cp /tmp/k3s-airgap-images-amd64.tar /var/lib/rancher/k3s/agent/images/
      sudo INSTALL_K3S_SKIP_DOWNLOAD=true K3S_URL=https://$SERVER_IP:6443 K3S_TOKEN=$TOKEN sh /tmp/k3s-install.sh"
  else
    ssh_node "$ip" "curl -sfL https://get.k3s.io | sudo K3S_URL=https://$SERVER_IP:6443 K3S_TOKEN=$TOKEN sh -"
  fi
done

say "забираю доступ к кластеру"
ssh_node "$SERVER_IP" 'sudo cat /etc/rancher/k3s/k3s.yaml' | sed "s#https://127.0.0.1:6443#https://$SERVER_IP:6443#" > "$KUBECONFIG_OUT"
chmod 600 "$KUBECONFIG_OUT"

say "кластер"
KUBECONFIG="$KUBECONFIG_OUT" kubectl get nodes -o wide
echo
echo "дальше: export KUBECONFIG=$KUBECONFIG_OUT"
