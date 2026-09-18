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
# Свой путь к доступу, чтобы скрипт работал и из чистой оболочки: без этого
# kubectl молча идёт на localhost:8080 и падает на первом же apply.
export KUBECONFIG="${KUBECONFIG:-$REPO/cluster/kubespray/kubeconfig}"

# Что нести в реестр, перечислено в images.map: файл и имя через пробел.
# Раньше список жил прямо здесь, и каждый новый образ означал правку кода.
declare -A MIRROR=()
while read -r fayl imya; do
  case "$fayl" in ''|'#'*) continue ;; esac
  MIRROR["$fayl"]="$imya"
done < "$HERE/images.map"

say() { printf '\n=== %s\n' "$*"; }

mapfile -t NODES < <(
  TF_DATA_DIR="${TF_DATA_DIR:-/root/.tf-quarry}" terraform -chdir="$TF_DIR" output -json nodes |
    python3 -c 'import json,sys; [print(k,v) for k,v in sorted(json.load(sys.stdin).items())]'
)
NODE_IP="$(echo "${NODES[0]}" | cut -d' ' -f2)"
REGISTRY="$NODE_IP:30500"

# 🔴 Реплик реестра две, и у каждой СВОЁ хранилище: общего тома на несколько
# узлов в стенде нет. Заливать через общий вход нельзя, kube-proxy разложит
# образы по репликам как ему удобнее, и каждая останется наполовину пустой.
# Такой реестр хуже одного: выкаты начнут падать через раз, и причина будет
# выглядеть случайной. Поэтому у каждой реплики свой вход, и льём в оба.
KOPII="${KOPII:-$NODE_IP:30501 $NODE_IP:30502}"

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
kubectl -n quarry-infra rollout status sts/registry --timeout=300s

say "жду, пока обе реплики начнут отвечать"
for kopiya in $KOPII; do
  until curl -sf -m 3 "http://$kopiya/v2/_catalog" >/dev/null; do sleep 3; done
  echo "$kopiya отвечает"
done

say "заливаю чужие образы в обе реплики"
for file in "${!MIRROR[@]}"; do
  echo "--- ${MIRROR[$file]}"
  for kopiya in $KOPII; do
    skopeo copy --dest-tls-verify=false \
      "docker-archive:$ARTIFACTS/$file" \
      "docker://$kopiya/${MIRROR[$file]}" >/dev/null
  done
done

say "заливаю наши образы в обе реплики"
for name in sim collector egts; do
  echo "--- quarry/$name:local"
  for kopiya in $KOPII; do
    skopeo copy --dest-tls-verify=false \
      "docker-archive:$ARTIFACTS/$name.tar" \
      "docker://$kopiya/quarry/$name:local" >/dev/null
  done
done

say "сверяю реплики"
# Копия, про которую не проверили, что она полная, резервом не является:
# расхождение вскроется ровно тогда, когда одна из реплик останется одна.
etalon=""
for kopiya in $KOPII; do
  spisok="$(curl -s "http://$kopiya/v2/_catalog" | tr ',' '\n' | tr -d '\r' | sort)"
  echo "$kopiya: образов $(echo "$spisok" | grep -c .)"
  if [ -z "$etalon" ]; then
    etalon="$spisok"
  elif [ "$spisok" != "$etalon" ]; then
    echo "⚠️ реплики разошлись" >&2
    diff <(echo "$etalon") <(echo "$spisok") || true
    exit 1
  fi
done
echo "✅ реплики совпадают"

say "что теперь лежит в реестре"
curl -s "http://$REGISTRY/v2/_catalog"
echo
