#!/usr/bin/env bash
# Тот же кластер, но обычным kubeadm через Kubespray.
#
# Зачем, если k3s уже работает: на площадках заказчиков стоит обычный
# Kubernetes, и разворачивают его как раз Ansible. k3s показывает контур,
# Kubespray показывает, что тем же контуром накрывается штатный кластер.
#
#   ./deploy.sh                развернуть кластер, узлы качают всё из интернета
#   OFFLINE=1 ./deploy.sh      развернуть в закрытом контуре: файлы и образы
#                              берутся с зеркала, узлы наружу не ходят вообще
#   ./deploy.sh reset          снести кластер с узлов, машины оставить
#
# Перед офлайн-установкой должно быть сделано:
#   cluster/offline/collect.sh              собрать артефакты (нужен интернет)
#   cluster/offline/mirror.sh start <адрес> поднять зеркало в контуре
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
OFFLINE="${OFFLINE:-0}"
# Адрес зеркала внутри контура. По умолчанию это шлюз изолированной сети,
# то есть управляющая машина: у неё единственной есть интерфейс и в контуре,
# и снаружи, ровно как у бастиона на площадке.
MIRROR_IP="${MIRROR_IP:-192.168.200.1}"

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
  # 🔴 Управляющих узлов и членов etcd столько же, сколько машин, и это не
  # щедрость, а арифметика кворума: большинство от трёх это два, значит один
  # узел можно потерять. С одним управляющим его потеря останавливает
  # управление, с двумя останавливает тоже: большинство от двух это два.
  echo "    kube_control_plane:"
  echo "      hosts:"
  for entry in "${NODES[@]}"; do
    echo "        $(echo "$entry" | cut -d' ' -f1):"
  done
  echo "    etcd:"
  echo "      hosts:"
  for entry in "${NODES[@]}"; do
    echo "        $(echo "$entry" | cut -d' ' -f1):"
  done
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

# Постоянная часть профиля лежит отдельным файлом: тот же profile.yml читает
# cluster/offline/collect.sh, когда считает, что нести в контур.
cp "$HERE/profile.yml" "$INVENTORY/group_vars/k8s_cluster/profile.yml"

cat > "$INVENTORY/group_vars/k8s_cluster/quarry.yml" <<EOF
# Файл собирается deploy.sh, править руками нет смысла.
#
# containerd на узлах ходит в реестры внутри контура, а не в интернет.
# Имя registry.quarry.local постоянное, адрес подставляется отсюда.
#
# 🔴 Адресов перечислено столько, сколько узлов. Раньше здесь стоял один
# управляющий узел, и его падение останавливало скачивание образов на всём
# кластере, хотя сам реестр был жив на другой машине. NodePort слушает на
# каждом узле, поэтому список адресов это список запасных дверей: containerd
# пробует их по очереди. После установки cluster/scripts/configure-registry.sh
# переставляет на каждом узле его собственный адрес первым.
containerd_registries_mirrors:
  - prefix: registry.quarry.local:5000
    server: http://$SERVER_IP:30500
    mirrors:
$(for entry in "${NODES[@]}"; do
  ip="$(echo "$entry" | cut -d' ' -f2)"
  echo "      - host: http://$ip:30500"
  echo '        capabilities: ["pull", "resolve"]'
  echo "        skip_verify: true"
done)
$(if [ "$OFFLINE" = "1" ]; then cat <<VNUTRI
  # Зеркало, с которого узлы берут образы САМОГО кластера. Внутрикластерный
  # реестр для этого не годится: его ещё нет, пока кластера нет.
  - prefix: $MIRROR_IP:5000
    server: http://$MIRROR_IP:5000
    mirrors:
      - host: http://$MIRROR_IP:5000
        capabilities: ["pull", "resolve"]
        skip_verify: true
VNUTRI
fi)
EOF

if [ "$OFFLINE" = "1" ]; then
  say "закрытый контур: зеркало $MIRROR_IP"
  curl -sf -m 5 "http://$MIRROR_IP:5000/v2/" >/dev/null ||
    { echo "реестр зеркала не отвечает, подними: cluster/offline/mirror.sh start $MIRROR_IP"; exit 1; }
  curl -sf -m 5 "http://$MIRROR_IP:8080/files/" >/dev/null ||
    { echo "файловое зеркало не отвечает, подними: cluster/offline/mirror.sh start $MIRROR_IP"; exit 1; }

  REGISTRY_HOST="$MIRROR_IP:5000" FILES_REPO="http://$MIRROR_IP:8080/files"     "$REPO/cluster/offline/render-offline-vars.sh" > "$INVENTORY/group_vars/all/offline.yml"
  echo "offline.yml собран"

  # Проверка изоляции до установки, а не после: если узел всё ещё видит
  # интернет, «установка без интернета» ничего не доказывает.
  say "проверяю, что узлы отрезаны от интернета"
  for entry in "${NODES[@]}"; do
    ip="$(echo "$entry" | cut -d' ' -f2)"
    name="$(echo "$entry" | cut -d' ' -f1)"
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -i "$SSH_KEY" "$SSH_USER@$ip"       'curl -sfI -m 5 https://registry.k8s.io >/dev/null 2>&1'; then
      echo "🔴 $name достаёт до registry.k8s.io: это не закрытый контур"
      exit 1
    fi
    echo "$name: наружу не ходит"
  done

  # 🔴 Кэш apt узлы снимают при первой загрузке, а зеркало с тех пор могло
  # пополниться. Kubespray свой apt update пропускает, пока кэшу меньше суток,
  # и установка падает на «No package matching ...», хотя пакет в зеркале есть.
  say "освежаю кэш apt на узлах"
  "$VENV/bin/ansible" -i "$INVENTORY/hosts.yaml" all --become     -m ansible.builtin.apt -a "update_cache=yes" -o
else
  rm -f "$INVENTORY/group_vars/all/offline.yml"
fi

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
# Доступ выписывается на ОБЩИЙ адрес, а не на конкретный узел: иначе три
# управляющих узла бессмысленны, потому что kubeconfig всё равно ходит в
# один из них и вместе с ним теряется. Если общий адрес не задан в профиле,
# остаётся прежнее поведение с первым узлом.
API_ADDR="$(awk '/^kube_vip_address:/ {print $2}' "$HERE/profile.yml")"
API_ADDR="${API_ADDR:-$SERVER_IP}"
echo "адрес API: $API_ADDR"
ssh -o StrictHostKeyChecking=no -i "$SSH_KEY" "$SSH_USER@$SERVER_IP" 'sudo cat /etc/kubernetes/admin.conf' |
  sed "s#https://127.0.0.1:6443#https://$API_ADDR:6443#; s#https://localhost:6443#https://$API_ADDR:6443#; s#https://$SERVER_IP:6443#https://$API_ADDR:6443#" > "$KUBECONFIG_OUT"
chmod 600 "$KUBECONFIG_OUT"

say "кластер"
KUBECONFIG="$KUBECONFIG_OUT" kubectl get nodes -o wide
echo
echo "дальше: export KUBECONFIG=$KUBECONFIG_OUT"
