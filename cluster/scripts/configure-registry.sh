#!/usr/bin/env bash
# Узлы кластера должны знать, где внутренний реестр, и ходить только туда.
#
# Имя registry.quarry.local:5000 в манифестах постоянное, а физический адрес
# реестра подставляется здесь. Меняется адрес, манифесты не трогаются.
#
# 🔴 Адресов теперь несколько, и это главное изменение после учения с
# гашением узла. Раньше все узлы ходили на NodePort ОДНОГО узла: падал он, и
# образы переставали скачиваться на всём кластере, даже когда сам реестр был
# жив на другой машине. Теперь каждый узел обращается сначала к СЕБЕ, потом к
# соседям; NodePort слушает на всех узлах, а kube-proxy доводит запрос до
# живой реплики.
#
# ⚠️ Свой адрес это адрес узла в сети контура, а не 127.0.0.1: kube-proxy
# работает в режиме ipvs, и NodePort на loopback там не обслуживается.
#
# ⚠️ Раздача адресов переехала в роль `cluster/ansible/roles/reestr`. Этот
# скрипт выполняется один раз и молчит о том, что стало дальше: он отработал,
# когда узел был один, а после расширения кластера настройка у всех трёх
# узлов указывала на один и тот же узел. Роль сравнивает желаемое с текущим
# и показывает расхождение до того, как его найдёт авария.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TF_DIR="${TF_DIR:-$REPO/cluster/terraform}"
SSH_KEY="${SSH_KEY:-/root/.ssh/quarry-lab}"
SSH_USER="${SSH_USER:-ubuntu}"
NODE_PORT="${NODE_PORT:-30500}"
IMYA="${IMYA:-registry.quarry.local:5000}"

mapfile -t NODES < <(
  TF_DATA_DIR="${TF_DATA_DIR:-/root/.tf-quarry}" terraform -chdir="$TF_DIR" output -json nodes |
    python3 -c 'import json,sys; [print(k,v) for k,v in sorted(json.load(sys.stdin).items())]'
)

# Порядок обхода: сначала свой адрес, потом остальные. containerd пробует
# хосты по очереди, пока кто-нибудь не ответит.
poryadok() {
  local svoy="$1" entry ip
  echo "$svoy"
  for entry in "${NODES[@]}"; do
    ip="$(echo "$entry" | cut -d' ' -f2)"
    [ "$ip" = "$svoy" ] || echo "$ip"
  done
}

for entry in "${NODES[@]}"; do
  name="$(echo "$entry" | cut -d' ' -f1)"
  ip="$(echo "$entry" | cut -d' ' -f2)"
  echo "=== $name ($ip)"

  # hosts.toml для containerd (kubeadm/Kubespray): server задаёт основной
  # адрес, блоки host перечисляются как запасные в порядке следования.
  hosts_toml="server = \"http://$ip:$NODE_PORT\""$'\n'
  while read -r adres; do
    hosts_toml+="[host.\"http://$adres:$NODE_PORT\"]"$'\n'
    hosts_toml+='  capabilities = ["pull","resolve"]'$'\n'
    hosts_toml+='  skip_verify = true'$'\n'
    hosts_toml+='  override_path = false'$'\n'
  done < <(poryadok "$ip")

  # registries.yaml для k3s: тот же смысл, свой синтаксис.
  k3s_endpoints=""
  while read -r adres; do
    k3s_endpoints+="      - \"http://$adres:$NODE_PORT\""$'\n'
  done < <(poryadok "$ip")

  ssh -n -o StrictHostKeyChecking=no -i "$SSH_KEY" "$SSH_USER@$ip" "
    set -e
    if command -v k3s >/dev/null; then
      sudo mkdir -p /etc/rancher/k3s
      printf '%s' 'mirrors:
  \"$IMYA\":
    endpoint:
$k3s_endpoints' | sudo tee /etc/rancher/k3s/registries.yaml >/dev/null
      if systemctl is-active --quiet k3s; then sudo systemctl restart k3s; else sudo systemctl restart k3s-agent; fi
    else
      sudo mkdir -p '/etc/containerd/certs.d/$IMYA'
      printf '%s' '$hosts_toml' | sudo tee '/etc/containerd/certs.d/$IMYA/hosts.toml' >/dev/null
      # containerd перечитывает certs.d на каждый запрос образа, перезапуск
      # не нужен: демон трогать на работающем узле дороже, чем польза.
      echo 'адреса реестра обновлены'
    fi"
done

echo
echo "реестр для узлов: $IMYA -> NodePort $NODE_PORT на всех узлах, свой первым"
