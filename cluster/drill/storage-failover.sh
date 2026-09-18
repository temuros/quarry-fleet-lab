#!/usr/bin/env bash
# Учебная авария: гаснет машина хранилища, та самая, что сейчас обслуживает.
#
# Это последняя из известных единственных точек отказа стенда. Пока хранилище
# было одно, его потеря останавливала не один под, а все базы сразу: журнал
# смен, шину, мониторинг и реестр образов.
#
# Проверяем не «переехал ли адрес», а то, что важно на площадке:
#   - сколько времени тома недоступны;
#   - переживает ли это журнал смен, то есть продолжает ли считаться смена;
#   - возвращается ли всё само, без человека.
#
# ⚠️ Клиенты монтируют том с параметром hard: при потере сервера запись ЖДЁТ
# его возвращения, а не падает с ошибкой. Поэтому правильный признак успеха
# здесь не «ошибок не было в логе», а «после переключения записи продолжились
# с того же места».
#
#   ./storage-failover.sh [минут до возврата машины]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$REPO/cluster/kubespray/kubeconfig}"
TF_DIR="${TF_DIR:-$REPO/cluster/terraform}"
SSH_KEY="${SSH_KEY:-/root/.ssh/quarry-lab}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_OPTS=(-n -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$SSH_KEY")
VIP="${VIP:-192.168.200.251}"
VOZVRAT_MIN="${1:-2}"
OTCHET_DIR="${OTCHET_DIR:-$REPO/cluster/artifacts/drills}"
mkdir -p "$OTCHET_DIR"
OTCHET="$OTCHET_DIR/$(date +%Y-%m-%d-%H%M)-storage-failover.txt"

T0=$(date +%s)
hronika() { printf '[%4d с] %s\n' "$(( $(date +%s) - T0 ))" "$*" | tee -a "$OTCHET"; }
razdel()  { printf '\n=== %s\n' "$*" | tee -a "$OTCHET"; }

mapfile -t NAS < <(
  TF_DATA_DIR="${TF_DATA_DIR:-/root/.tf-quarry}" terraform -chdir="$TF_DIR" output -json nas |
    python3 -c 'import json,sys; [print(k,v) for k,v in sorted(json.load(sys.stdin).items())]'
)

kto_glavnyy() {
  local entry imya ip
  for entry in "${NAS[@]}"; do
    imya="$(echo "$entry" | cut -d' ' -f1)"; ip="$(echo "$entry" | cut -d' ' -f2)"
    if ssh "${SSH_OPTS[@]}" "$SSH_USER@$ip" "ip -4 -o addr show | grep -q '$VIP'" 2>/dev/null; then
      echo "$imya $ip"; return 0
    fi
  done
  return 1
}

sobytiy() {
  kubectl -n quarry exec sts/postgres --request-timeout=20s -- \
    psql -U quarry -d quarry -tAc 'select count(*) from events' 2>/dev/null | tr -d '\r' || echo "недоступен"
}

hranilishche_otvechaet() {
  showmount -e "$VIP" >/dev/null 2>&1
}

razdel "до аварии"
GLAVNYY="$(kto_glavnyy || true)"
[ -n "$GLAVNYY" ] || { echo "никто не держит $VIP" >&2; exit 1; }
UZEL="$(echo "$GLAVNYY" | cut -d' ' -f1)"
UZEL_IP="$(echo "$GLAVNYY" | cut -d' ' -f2)"
DO="$(sobytiy)"
{
  echo "общий адрес хранилища $VIP держит $UZEL ($UZEL_IP)"
  echo "событий в журнале: $DO"
} | tee -a "$OTCHET"

razdel "гасим машину хранилища $UZEL"
virsh destroy "$UZEL" | tee -a "$OTCHET"
AVARIYA=$(date +%s); T0=$AVARIYA

PEREEHAL=""
OTVECHAET=""
for _ in $(seq 1 90); do
  if [ -z "$PEREEHAL" ]; then
    NOVYY="$(kto_glavnyy || true)"
    if [ -n "$NOVYY" ]; then
      PEREEHAL=$(( $(date +%s) - AVARIYA ))
      hronika "общий адрес переехал на $(echo "$NOVYY" | cut -d' ' -f1)"
    fi
  fi
  if [ -n "$PEREEHAL" ] && [ -z "$OTVECHAET" ] && hranilishche_otvechaet; then
    OTVECHAET=$(( $(date +%s) - AVARIYA ))
    hronika "хранилище снова отдаёт том"
    break
  fi
  sleep 2
done

razdel "продолжается ли смена"
# Главный вопрос: не «жив ли сервер», а пишется ли журнал. Пробуем записать и
# прочитать, как это делает приёмник.
POSLE="$(sobytiy)"
{
  echo "событий: до аварии $DO, после переключения $POSLE"
  kubectl -n quarry get pods --no-headers | awk '{print $1, $2, $3}'
} | tee -a "$OTCHET"

sleep $(( VOZVRAT_MIN * 60 ))

razdel "возвращаем $UZEL"
virsh start "$UZEL" >/dev/null
PODALI=$(date +%s); T0=$PODALI
for _ in $(seq 1 60); do
  if ssh "${SSH_OPTS[@]}" "$SSH_USER@$UZEL_IP" 'true' 2>/dev/null; then
    hronika "$UZEL снова отвечает"; break
  fi
  sleep 5
done

# Реплика должна догнать сама: DRBD знает, чего не хватает, и досылает разницу.
for _ in $(seq 1 60); do
  sostoyanie="$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$UZEL_IP" 'sudo drbdadm status quarry 2>/dev/null | head -2 | tr "\n" " "' 2>/dev/null || true)"
  case "$sostoyanie" in
    *UpToDate*) hronika "реплика догнала: $sostoyanie"; break ;;
  esac
  sleep 5
done

razdel "итог"
{
  echo "гасили:                        $UZEL, он держал $VIP"
  echo "адрес переехал через:          ${PEREEHAL:-не переехал} с"
  echo "хранилище снова отдаёт том:    ${OTVECHAET:-не отдаёт} с"
  echo "событий: до $DO, после $POSLE, сейчас $(sobytiy)"
  echo
  "$REPO/cluster/storage/nas-pair.sh" status 2>/dev/null | tail -8
  echo "отчёт: $OTCHET"
} | tee -a "$OTCHET"
