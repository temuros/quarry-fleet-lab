#!/usr/bin/env bash
# Учебная авария: гаснет управляющий узел, причём тот, который сейчас держит
# общий адрес API.
#
# Это единственная проверка, ради которой управляющих узлов делают несколько.
# Пока узел один, вопрос «переживёт ли кластер его потерю» имеет очевидный
# ответ. Когда узлов три, ответ надо получить отказом, а не рассуждением:
#   - остаётся ли кластер управляемым (kubectl продолжает работать);
#   - переезжает ли общий адрес API на живой узел и за сколько;
#   - сохраняет ли etcd кворум (два голоса из трёх это большинство);
#   - продолжает ли идти смена, пока управление недоступно.
#
#   ./control-plane-down.sh [минут простоя]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$REPO/cluster/kubespray/kubeconfig}"
SSH_KEY="${SSH_KEY:-/root/.ssh/quarry-lab}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_OPTS=(-n -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$SSH_KEY")
PROSTOY_MIN="${1:-3}"
VIP="$(awk '/^kube_vip_address:/ {print $2}' "$REPO/cluster/kubespray/profile.yml")"
OTCHET_DIR="${OTCHET_DIR:-$REPO/cluster/artifacts/drills}"
mkdir -p "$OTCHET_DIR"
OTCHET="$OTCHET_DIR/$(date +%Y-%m-%d-%H%M)-control-plane.txt"

T0=$(date +%s)
hronika() { printf '[%4d с] %s\n' "$(( $(date +%s) - T0 ))" "$*" | tee -a "$OTCHET"; }
razdel()  { printf '\n=== %s\n' "$*" | tee -a "$OTCHET"; }

# Кто сейчас держит общий адрес: его и гасим. Гасить узел, который адрес не
# держит, проверяет заметно меньше.
derzhit_vip() {
  local imya ip
  while read -r imya ip; do
    [ -n "$ip" ] || continue
    if ssh "${SSH_OPTS[@]}" "$SSH_USER@$ip" "ip -4 -o addr show | grep -q '$VIP'" 2>/dev/null; then
      echo "$imya $ip"; return 0
    fi
  done < <(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name} {.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}')
  return 1
}

sobytiy() {
  kubectl -n quarry exec sts/postgres --request-timeout=15s -- \
    psql -U quarry -d quarry -tAc 'select count(*) from events' 2>/dev/null | tr -d '\r' || echo "недоступен"
}

razdel "до аварии"
LIDER="$(derzhit_vip || true)"
[ -n "$LIDER" ] || { echo "не нашёл узел с адресом $VIP" >&2; exit 1; }
UZEL="$(echo "$LIDER" | cut -d' ' -f1)"
UZEL_IP="$(echo "$LIDER" | cut -d' ' -f2)"
DO_SOBYTIY="$(sobytiy)"
{
  echo "общий адрес API: $VIP, его держит $UZEL ($UZEL_IP)"
  echo "событий в журнале: $DO_SOBYTIY"
  kubectl get nodes -o custom-columns=UZEL:.metadata.name,ROL:.metadata.labels.kubernetes\\.io/role,STATUS:.status.conditions[-1].type --no-headers
} | tee -a "$OTCHET"

razdel "гасим управляющий узел $UZEL"
virsh destroy "$UZEL" | tee -a "$OTCHET"
AVARIYA=$(date +%s); T0=$AVARIYA

# Наблюдатель стучится в API ЧЕРЕЗ ОБЩИЙ АДРЕС каждую секунду: именно этот
# путь должен пережить потерю узла, а не прямое обращение к соседу.
ZHURNAL="/tmp/api-vip.$$"
( while :; do
    if kubectl get --raw='/readyz' --request-timeout=2s >/dev/null 2>&1
    then echo "$(date +%s) est"; else echo "$(date +%s) net"; fi
    sleep 1
  done ) > "$ZHURNAL" 2>/dev/null &
NABL=$!
trap 'kill '"$NABL"' 2>/dev/null || true' EXIT

VERNULOS=""
for _ in $(seq 1 120); do
  if kubectl get nodes --request-timeout=3s >/dev/null 2>&1; then
    VERNULOS=$(( $(date +%s) - AVARIYA ))
    hronika "управление вернулось через общий адрес"
    break
  fi
  sleep 2
done

NOVYY="$(derzhit_vip || echo 'никто')"
hronika "общий адрес теперь на: $NOVYY"

razdel "что с кластером без одного управляющего узла"
{
  kubectl get nodes --no-headers --request-timeout=10s | awk '{print $1, $2}'
  echo "-- кворум etcd --"
  ZHIVOY_IP="$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}' | grep -v "^$UZEL_IP$" | head -1)"
  ssh "${SSH_OPTS[@]}" "$SSH_USER@$ZHIVOY_IP" 'set -a; sudo cat /etc/etcd.env > /tmp/e.env; . /tmp/e.env; set +a;
    sudo -E etcdctl endpoint health --cluster -w table 2>/dev/null | tail -6; rm -f /tmp/e.env' || echo "etcd не ответил"
  echo "-- смена идёт? --"
  echo "событий в журнале: $(sobytiy) (до аварии $DO_SOBYTIY)"
} | tee -a "$OTCHET"

sleep $(( PROSTOY_MIN * 60 ))

razdel "поднимаем $UZEL"
virsh start "$UZEL" >/dev/null
PODALI=$(date +%s); T0=$PODALI
for _ in $(seq 1 90); do
  if [ "$(kubectl get node "$UZEL" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = "True" ]; then
    hronika "$UZEL снова Ready"
    break
  fi
  sleep 5
done

kill "$NABL" 2>/dev/null || true
NE_OTVECHAL="$(grep -c ' net$' "$ZHURNAL" || true)"
rm -f "$ZHURNAL"

razdel "итог"
{
  echo "гасили:                        $UZEL, он держал $VIP"
  echo "управление вернулось через:    ${VERNULOS:-не вернулось} с"
  echo "API не отвечал примерно:       $NE_OTVECHAL с"
  echo "общий адрес переехал на:       $NOVYY"
  echo "событий в журнале: до $DO_SOBYTIY, после $(sobytiy)"
  echo
  kubectl get nodes --no-headers | awk '{print $1, $2}'
  echo "отчёт: $OTCHET"
} | tee -a "$OTCHET"
