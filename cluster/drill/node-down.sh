#!/usr/bin/env bash
# Учебная авария: гасим рабочий узел и смотрим, что переживёт кластер.
#
# Стенд, который ни разу не ломали нарочно, отвечает только на вопрос
# «а заведётся ли». Заказчика на площадке интересует другое: сколько
# диспетчер не увидит наряд, вернутся ли цифры сами, и что потребует
# человека. Ответ на это даёт не архитектура на бумаге, а хроника.
#
# Узел гасится ЖЁСТКО (`virsh destroy`, это выдернутый шнур, а не
# «выключить»). Питание на карьере пропадает именно так.
#
#   ./node-down.sh [узел] [минут простоя]
#   ./node-down.sh quarry-3 6
#
# Хроника пишется на экран и в cluster/artifacts/drills/<время>.txt
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$REPO/cluster/kubespray/kubeconfig}"
UZEL="${1:-quarry-3}"
PROSTOY_MIN="${2:-6}"
OTCHET_DIR="${OTCHET_DIR:-$REPO/cluster/artifacts/drills}"
mkdir -p "$OTCHET_DIR"
OTCHET="$OTCHET_DIR/$(date +%Y-%m-%d-%H%M)-$UZEL.txt"

T0=$(date +%s)
hronika() { printf '[%4d с] %s\n' "$(( $(date +%s) - T0 ))" "$*" | tee -a "$OTCHET"; }
razdel()  { printf '\n=== %s\n' "$*" | tee -a "$OTCHET"; }

sostoyanie_uzla() {
  kubectl get node "$UZEL" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "?"
}

# Поды, которые жили на узле в момент аварии: за ними и следим.
podov_na_uzle() {
  kubectl get pods -A --field-selector "spec.nodeName=$UZEL" --no-headers 2>/dev/null | wc -l
}

ne_gotovy() {
  # Колонки: NAMESPACE NAME READY STATUS RESTARTS AGE, статус четвёртый.
  kubectl get pods -A --no-headers 2>/dev/null |
    grep -vE 'kube-system|Completed' | awk '$4 != "Running" {print $2" "$4}'
}

sobytiy_v_zhurnale() {
  kubectl -n quarry exec sts/postgres --request-timeout=10s -- \
    psql -U quarry -d quarry -tAc 'select count(*) from events' 2>/dev/null | tr -d '\r' || echo "недоступен"
}

razdel "до аварии"
{
  date -Iseconds
  echo "узел под удар: $UZEL, простой $PROSTOY_MIN мин"
  kubectl get nodes -o wide --no-headers | awk '{print $1, $2, $6}'
  echo "-- что живёт на $UZEL --"
  kubectl get pods -A --field-selector "spec.nodeName=$UZEL" \
    -o custom-columns=NS:.metadata.namespace,POD:.metadata.name --no-headers
} | tee -a "$OTCHET"
DO_ZHURNAL="$(sobytiy_v_zhurnale)"
hronika "событий в журнале до аварии: $DO_ZHURNAL"

razdel "гасим $UZEL"
virsh destroy "$UZEL" | tee -a "$OTCHET"
AVARIYA=$(date +%s)
T0=$AVARIYA

# Наблюдение. Отмечаем ровно три вещи: когда кластер ЗАМЕТИЛ потерю узла,
# когда он начал выселять с него поды и когда встали те, кому уехать некуда.
ZAMETIL=""; VYSELIL=""
KONEC=$(( AVARIYA + PROSTOY_MIN * 60 ))
while [ "$(date +%s)" -lt "$KONEC" ]; do
  gotov="$(sostoyanie_uzla)"
  if [ -z "$ZAMETIL" ] && [ "$gotov" != "True" ]; then
    ZAMETIL=$(( $(date +%s) - AVARIYA ))
    hronika "кластер заметил потерю узла: NotReady"
  fi
  if [ -n "$ZAMETIL" ] && [ -z "$VYSELIL" ]; then
    # Выселение начинается не сразу: у подов стоит терпение к NotReady
    # (по умолчанию 300 с). До этого кластер ждёт, не вернётся ли узел.
    pereehalo="$(kubectl get pods -A --no-headers 2>/dev/null |
      grep -cE 'Pending|ContainerCreating|Terminating' || true)"
    if [ "${pereehalo:-0}" -gt 0 ]; then
      VYSELIL=$(( $(date +%s) - AVARIYA ))
      hronika "пошло выселение подов с узла"
      ne_gotovy | sed 's/^/          /' | tee -a "$OTCHET" >/dev/null
    fi
  fi
  sleep 10
done

razdel "что стало за $PROSTOY_MIN мин простоя"
{
  kubectl get nodes --no-headers | awk '{print $1, $2}'
  echo "-- не работает --"
  ne_gotovy
  echo "-- где теперь поды quarry --"
  kubectl -n quarry get pods -o custom-columns=POD:.metadata.name,STATUS:.status.phase,UZEL:.spec.nodeName --no-headers
} | tee -a "$OTCHET"
hronika "журнал смен: $(sobytiy_v_zhurnale)"

razdel "поднимаем $UZEL"
virsh start "$UZEL" | tee -a "$OTCHET"
PODNYALI=$(date +%s)
T0=$PODNYALI

VERNULSYA=""
while [ $(( $(date +%s) - PODNYALI )) -lt 600 ]; do
  if [ -z "$VERNULSYA" ] && [ "$(sostoyanie_uzla)" = "True" ]; then
    VERNULSYA=$(( $(date +%s) - PODNYALI ))
    hronika "узел снова Ready"
  fi
  if [ -n "$VERNULSYA" ] && [ -z "$(ne_gotovy)" ]; then
    hronika "вся нагрузка вернулась"
    break
  fi
  sleep 10
done

razdel "итог"
POSLE_ZHURNAL="$(sobytiy_v_zhurnale)"
{
  echo "узел заметили потерянным через: ${ZAMETIL:-не заметили} с"
  echo "выселение подов началось через: ${VYSELIL:-не начиналось} с"
  echo "узел вернулся в Ready через:    ${VERNULSYA:-не вернулся} с после включения"
  echo "событий в журнале: до $DO_ZHURNAL, после $POSLE_ZHURNAL"
  echo
  kubectl get nodes --no-headers | awk '{print $1, $2}'
  kubectl -n quarry get pods --no-headers | awk '{print $1, $3}'
  echo
  echo "отчёт: $OTCHET"
} | tee -a "$OTCHET"
