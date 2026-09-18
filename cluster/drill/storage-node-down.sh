#!/usr/bin/env bash
# Учебная авария: гаснет узел, на котором работает журнал смен.
#
# Это проверка ради которой заводили сетевое хранилище. Раньше том лежал на
# диске узла, и журнал ждал возвращения своей машины: переехать он не мог,
# потому что данных на других узлах не было. Теперь данные лежат на хранилище
# контура, и вопрос только в том, разрешит ли кластер переезд.
#
# 🔴 Само по себе гашение узла базу НЕ переносит, и это не недоработка.
# Kubernetes не удаляет поды StatefulSet с недоступного узла: он не знает,
# умер узел или просто потерял сеть, а поднять вторую копию базы поверх
# работающей первой хуже, чем подождать. Решение принимает человек, и
# выражается оно меткой `node.kubernetes.io/out-of-service`: ею оператор
# говорит «машина действительно вышла из строя, переносите».
#
# Учение показывает обе фазы: сколько кластер ждёт сам и сколько занимает
# переезд после подтверждения.
#
#   ./storage-node-down.sh [минут ожидания до подтверждения]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$REPO/cluster/kubespray/kubeconfig}"
ZHDAT_MIN="${1:-2}"
OTCHET_DIR="${OTCHET_DIR:-$REPO/cluster/artifacts/drills}"
mkdir -p "$OTCHET_DIR"
OTCHET="$OTCHET_DIR/$(date +%Y-%m-%d-%H%M)-storage.txt"

T0=$(date +%s)
hronika() { printf '[%4d с] %s\n' "$(( $(date +%s) - T0 ))" "$*" | tee -a "$OTCHET"; }
razdel()  { printf '\n=== %s\n' "$*" | tee -a "$OTCHET"; }

sobytiy() {
  kubectl -n quarry exec sts/postgres --request-timeout=15s -- \
    psql -U quarry -d quarry -tAc 'select count(*) from events' 2>/dev/null | tr -d '\r' || echo "недоступен"
}

# ⚠️ `|| true` обязателен. Под может на секунды исчезнуть совсем, kubectl
# вернёт ненулевой код, и `set -e` оборвёт учение ровно в тот момент, ради
# которого оно затевалось: на переезде.
gde_zhurnal() {
  kubectl -n quarry get pod postgres-0 -o jsonpath='{.spec.nodeName}' 2>/dev/null || true
}

razdel "до аварии"
UZEL="$(gde_zhurnal)"
[ -n "$UZEL" ] || { echo "не вижу под журнала" >&2; exit 1; }
DO="$(sobytiy)"
{
  echo "журнал смен работает на узле: $UZEL"
  echo "событий в журнале: $DO"
  kubectl -n quarry get pvc data-postgres-0 -o custom-columns=PVC:.metadata.name,CLASS:.spec.storageClassName --no-headers
} | tee -a "$OTCHET"

razdel "гасим $UZEL"
virsh destroy "$UZEL" | tee -a "$OTCHET"
AVARIYA=$(date +%s); T0=$AVARIYA

for _ in $(seq 1 60); do
  sostoyanie="$(kubectl get node "$UZEL" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
  [ "$sostoyanie" != "True" ] && { hronika "кластер заметил потерю узла"; break; }
  sleep 5
done

hronika "ждём $ZHDAT_MIN мин, как если бы оператор ещё выяснял, что случилось"
sleep $(( ZHDAT_MIN * 60 ))
{
  echo "-- что с журналом, пока решение не принято --"
  kubectl -n quarry get pod postgres-0 -o custom-columns=POD:.metadata.name,STATUS:.status.phase,UZEL:.spec.nodeName --no-headers 2>/dev/null
  echo "событий: $(sobytiy)"
} | tee -a "$OTCHET"

razdel "оператор подтверждает, что узел вышел из строя"
# Метка out-of-service это и есть подтверждение. После неё кластер снимает
# поды с узла и переносит их, потому что тома больше не привязаны к машине.
kubectl taint node "$UZEL" node.kubernetes.io/out-of-service=nodeshutdown:NoExecute --overwrite | tee -a "$OTCHET"
PODTVERDIL=$(date +%s); T0=$PODTVERDIL

PEREEHAL=""
for _ in $(seq 1 90); do
  novyy="$(gde_zhurnal)"
  gotov="$(kubectl -n quarry get pod postgres-0 -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || true)"
  [ -n "$novyy" ] || { sleep 5; continue; }
  if [ -n "$novyy" ] && [ "$novyy" != "$UZEL" ] && [ "$gotov" = "true" ]; then
    PEREEHAL=$(( $(date +%s) - PODTVERDIL ))
    hronika "журнал переехал на $novyy и отвечает"
    break
  fi
  sleep 5
done

razdel "данные на месте?"
POSLE="$(sobytiy)"
{
  echo "событий: до аварии $DO, после переезда $POSLE"
  kubectl -n quarry get pod postgres-0 -o custom-columns=POD:.metadata.name,UZEL:.spec.nodeName,STATUS:.status.phase --no-headers
} | tee -a "$OTCHET"

razdel "возвращаем $UZEL"
virsh start "$UZEL" >/dev/null
PODALI=$(date +%s); T0=$PODALI
for _ in $(seq 1 90); do
  sostoyanie="$(kubectl get node "$UZEL" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
  if [ "$sostoyanie" = "True" ]; then
    hronika "$UZEL снова Ready"; break
  fi
  sleep 5
done
# ⚠️ Метку обязательно снять: пока она стоит, узел остаётся пустым, даже
# когда машина давно вернулась в строй.
kubectl taint node "$UZEL" node.kubernetes.io/out-of-service- >/dev/null 2>&1 || true
hronika "метка снята, узел снова принимает нагрузку"

razdel "итог"
{
  echo "гасили узел:                       $UZEL"
  echo "переезд после подтверждения занял: ${PEREEHAL:-не переехал} с"
  echo "событий: до $DO, после $POSLE"
  echo
  kubectl get nodes --no-headers | awk '{print $1, $2}'
  kubectl -n quarry get pods --no-headers | awk '{print $1, $3}'
  echo "отчёт: $OTCHET"
} | tee -a "$OTCHET"
