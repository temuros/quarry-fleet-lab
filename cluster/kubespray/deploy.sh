#!/usr/bin/env bash
# Тот же кластер, но обычным kubeadm через Kubespray.
#
# Зачем, если k3s уже работает: на площадках заказчиков стоит обычный
# Kubernetes, и разворачивают его как раз Ansible. k3s показывает контур,
# Kubespray показывает, что тем же контуром накрывается штатный кластер.
#
#   ./deploy.sh              развернуть кластер
#   ./deploy.sh reset        снести кластер с узлов, машины оставить
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TF_DIR="${TF_DIR:-$REPO/cluster/terraform}"
KUBESPRAY="${KUBESPRAY:-/opt/kubespray}"
VENV="${VENV:-/opt/kubespray-venv}"
SSH_KEY="${SSH_KEY:-/root/.ssh/quarry-lab}"
SSH_USER="${SSH_USER:-ubuntu}"
INVENTORY="$HERE/inventory/quarry"
KUBECONFIG_OUT="${KUBECONFIG_OUT:-$HERE/kubeconfig}"

say() { printf '\n=== %s\n' "$*"; }

say "адреса узлов из Terraform"
mapfile -t NODES < <(
  TF_DATA_DIR="${TF_DATA_DIR:-/root/.tf-quarry}" terraform -chdir="$TF_DIR" output -json nodes |
    python3 -c 'import json,sys; [print(k,v) for k,v in sorted(json.load(sys.stdin).items())]'
)
printf '%s\n' "${NODES[@]}"
SERVER_IP="$(echo "${NODES[0]}" | cut -d' ' -f2)"

say "собираю инвентарь"
mkdir -p "$INVENTORY/group_vars/k8s_cluster" "$INVENTORY/group_vars/all"
{
  echo "# Файл собирается deploy.sh из вывода Terraform, править руками нет смысла."
  echo "all:"
  echo "  hosts:"
  for entry in "${NODES[@]}"; do
    name="$(echo "$entry" | cut -d' ' -f1)"
    ip="$(echo "$entry" | cut -d' ' -f2)"
    echo "    $name:"
    echo "      ansible_host: $ip"
    echo "      ip: $ip"
    echo "      access_ip: $ip"
  done
  echo "  children:"
  echo "    kube_control_plane:"
  echo "      hosts:"
  echo "        $(echo "${NODES[0]}" | cut -d' ' -f1):"
  echo "    etcd:"
  echo "      hosts:"
  echo "        $(echo "${NODES[0]}" | cut -d' ' -f1):"
  echo "    kube_node:"
  echo "      hosts:"
  for entry in "${NODES[@]}"; do
    echo "        $(echo "$entry" | cut -d' ' -f1):"
  done
  echo "    k8s_cluster:"
  echo "      children:"
  echo "        kube_control_plane:"
  echo "        kube_node:"
  echo "    calico_rr:"
  echo "      hosts: {}"
} > "$INVENTORY/hosts.yaml"

cat > "$INVENTORY/group_vars/all/quarry.yml" <<EOF
# Доступ на узлы
ansible_user: $SSH_USER
ansible_ssh_private_key_file: $SSH_KEY
ansible_python_interpreter: /usr/bin/python3
ansible_ssh_common_args: "-o StrictHostKeyChecking=no"
EOF

cat > "$INVENTORY/group_vars/k8s_cluster/quarry.yml" <<EOF
cluster_name: quarry.local

# containerd на узлах ходит в наш реестр внутри контура, а не в интернет.
# Имя постоянное, адрес подставляется отсюда.
containerd_registries_mirrors:
  - prefix: registry.quarry.local:5000
    mirrors:
      - host: http://$SERVER_IP:30500
        capabilities: ["pull", "resolve"]
        skip_verify: true

# Стенд скромный по ресурсам, лишнее не ставим.
dashboard_enabled: false
metrics_server_enabled: false
helm_enabled: false
kube_network_plugin: calico
EOF

say "инвентарь"
cat "$INVENTORY/hosts.yaml"

if [ "${1:-}" = "reset" ]; then
  say "снос кластера"
  export ANSIBLE_CONFIG="$KUBESPRAY/ansible.cfg"
  cd "$KUBESPRAY"
  "$VENV/bin/ansible-playbook" -i "$INVENTORY/hosts.yaml" --become "$KUBESPRAY/reset.yml" -e reset_confirmation=yes
  exit 0
fi

# Kubespray ищет свои роли относительно собственного ansible.cfg, поэтому
# запускать надо из его каталога, иначе не находится даже первая роль.
export ANSIBLE_CONFIG="$KUBESPRAY/ansible.cfg"
cd "$KUBESPRAY"

say "проверка связи с узлами"
"$VENV/bin/ansible" -i "$INVENTORY/hosts.yaml" all -m ping -o

say "разворачиваю кластер, это минут двадцать"
"$VENV/bin/ansible-playbook" -i "$INVENTORY/hosts.yaml" --become "$KUBESPRAY/cluster.yml"

say "забираю доступ к кластеру"
ssh -o StrictHostKeyChecking=no -i "$SSH_KEY" "$SSH_USER@$SERVER_IP" 'sudo cat /etc/kubernetes/admin.conf' |
  sed "s#https://127.0.0.1:6443#https://$SERVER_IP:6443#; s#https://localhost:6443#https://$SERVER_IP:6443#" > "$KUBECONFIG_OUT"
chmod 600 "$KUBECONFIG_OUT"

say "кластер"
KUBECONFIG="$KUBECONFIG_OUT" kubectl get nodes -o wide
echo
echo "дальше: export KUBECONFIG=$KUBECONFIG_OUT"
