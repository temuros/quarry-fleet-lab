#!/usr/bin/env bash
# Учебная авария: пропало питание во всей серверной.
#
# Гашение одного узла (node-down.sh) проверяет отказоустойчивость: соседи
# живы, кластер сам разбирается. Обрыв питания на объекте так не выглядит.
# Гаснет ВСЁ и сразу: управляющий узел, рабочие узлы, диски с базами. Никто
# ничего не успевает записать, сессии не закрываются, кворум пропадает
# мгновенно, а не по очереди.
#
# Вопросы, на которые отвечает этот прогон:
#   - поднимется ли кластер сам, без человека с клавиатурой;
#   - переживут ли базы обрыв на середине записи (etcd, Kafka, PostgreSQL);
#   - сколько времени объект без системы;
#   - что окажется сломанным, когда всё вроде бы поднялось.
#
#   ./power-loss.sh [минут без питания]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$REPO/cluster/kubespray/kubeconfig}"
BEZ_PITANIYA_MIN="${1:-2}"
UZLY="${UZLY:-quarry-1 quarry-2 quarry-3}"
OTCHET_DIR="${OTCHET_DIR:-$REPO/cluster/artifacts/drills}"
mkdir -p "$OTCHET_DIR"
OTCHET="$OTCHET_DIR/$(date +%Y-%m-%d-%H%M)-power-loss.txt"

T0=$(date +%s)
hronika() { printf '[%4d с] %s\n' "$(( $(date +%s) - T0 ))" "$*" | tee -a "$OTCHET"; }
razdel()  { printf '\n=== %s\n' "$*" | tee -a "$OTCHET"; }

sobytiy() {
  kubectl -n quarry exec sts/postgres --request-timeout=15s -- \
    psql -U quarry -d quarry -tAc 'select count(*) from events' 2>/dev/null | tr -d '\r' || echo "недоступен"
}

ne_gotovy() {
  kubectl get pods -A --no-headers --request-timeout=15s 2>/dev/null |
    grep -vE 'Completed' | awk '$4 != "Running" {print $2" "$4}'
}

smeshcheniya() {
  kubectl -n quarry exec kafka-0 --request-timeout=20s -- bash -c \
    '/opt/kafka/bin/kafka-get-offsets.sh --bootstrap-server localhost:9092' 2>/dev/null |
    grep quarry || echo "шина не отвечает"
}

razdel "до обрыва"
DO_SOBYTIY="$(sobytiy)"
{
  date -Iseconds
  echo "событий в журнале: $DO_SOBYTIY"
  echo "-- смещения шины до обрыва --"
  smeshcheniya
  kubectl get nodes --no-headers | awk '{print $1, $2}'
} | tee -a "$OTCHET"

razdel "обрыв питания: гаснет всё сразу"
for u in $UZLY; do virsh destroy "$u" >/dev/null 2>&1 || true; done
virsh list --all | tail -n +3 | tee -a "$OTCHET"
OBRYV=$(date +%s); T0=$OBRYV
hronika "питания нет"

# Проверяем, что снаружи объект действительно молчит: если API отвечает,
# значит погасили не всё.
if kubectl get --raw='/readyz' --request-timeout=5s >/dev/null 2>&1; then
  hronika "⚠️ API всё ещё отвечает, гашение неполное"
else
  hronika "система недоступна, как и должно быть"
fi

sleep $(( BEZ_PITANIYA_MIN * 60 ))

razdel "питание вернулось"
for u in $UZLY; do virsh start "$u" >/dev/null; done
PODALI=$(date +%s); T0=$PODALI
hronika "машины включены"

API=""; UZLY_GOTOVY=""; NAGRUZKA=""
while [ $(( $(date +%s) - PODALI )) -lt 900 ]; do
  if [ -z "$API" ] && kubectl get --raw='/readyz' --request-timeout=5s >/dev/null 2>&1; then
    API=$(( $(date +%s) - PODALI )); hronika "API кластера отвечает"
  fi
  if [ -n "$API" ] && [ -z "$UZLY_GOTOVY" ]; then
    gotovo="$(kubectl get nodes --no-headers --request-timeout=10s 2>/dev/null | grep -cw Ready || true)"
    vsego="$(echo "$UZLY" | wc -w)"
    if [ "${gotovo:-0}" -ge "$vsego" ]; then
      UZLY_GOTOVY=$(( $(date +%s) - PODALI )); hronika "все узлы Ready"
    fi
  fi
  if [ -n "$UZLY_GOTOVY" ] && [ -z "$(ne_gotovy)" ]; then
    NAGRUZKA=$(( $(date +%s) - PODALI )); hronika "вся нагрузка вернулась"
    break
  fi
  sleep 10
done

razdel "что с данными"
POSLE_SOBYTIY="$(sobytiy)"
{
  echo "событий в журнале: до обрыва $DO_SOBYTIY, после $POSLE_SOBYTIY"
  echo
  echo "-- целостность журнала (читается ли таблица) --"
  kubectl -n quarry exec sts/postgres -- psql -U quarry -d quarry -tAc \
    'select count(*), min(ts), max(ts) from events' 2>&1 | tail -1
  # 🔴 Смещения после подъёма должны ПРОДОЛЖИТЬСЯ с прежних значений. Ноль
  # означает, что шина потеряла журналы разделов и начала с чистого листа:
  # ровно так и было, пока Kafka писала в /tmp вместо смонтированного тома.
  echo "-- смещения шины после подъёма --"
  smeshcheniya
  echo "-- ArgoCD --"
  kubectl -n argocd get applications.argoproj.io --no-headers \
    -o custom-columns=A:.metadata.name,S:.status.sync.status,H:.status.health.status 2>/dev/null
  echo "-- часы узлов --"
  "$REPO/cluster/time/setup-time.sh" check 2>/dev/null | tail -n +2
} | tee -a "$OTCHET"

razdel "итог"
{
  echo "без питания:                 $BEZ_PITANIYA_MIN мин"
  echo "API ответил через:           ${API:-не ответил} с после включения"
  echo "все узлы Ready через:        ${UZLY_GOTOVY:-не все} с"
  echo "вся нагрузка вернулась за:   ${NAGRUZKA:-не вернулась} с"
  echo
  ne_gotovy | sed 's/^/осталось сломанным: /' || true
  echo "отчёт: $OTCHET"
} | tee -a "$OTCHET"
