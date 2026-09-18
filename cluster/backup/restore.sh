#!/usr/bin/env bash
# Восстановление стенда из резервной копии.
#
# Копия, которую ни разу не восстанавливали, копией не является: пока её не
# залили обратно, это просто файл нужного размера. Поэтому у скрипта есть
# режим учения, который ломает нарочно и сразу чинит.
#
# Два разных случая, и путают их постоянно:
#
#   ПОТЕРЯЛИ etcd, УЗЛЫ ЦЕЛЫ  -> ./restore.sh etcd
#       Кластер тот же, сертификаты те же, вернуть надо только содержимое
#       базы. Снимок кладётся на место данных etcd, службы поднимаются.
#
#   ПОТЕРЯЛИ ВСЁ              -> кластер поднимается из кода заново
#       terraform apply + kubespray deploy.sh + gitops/bootstrap.sh, и уже
#       потом ./restore.sh journal. Снимок etcd СЮДА НЕ ГОДИТСЯ: у нового
#       кластера другие сертификаты и другие адреса, а всё, что в снимке
#       ценного, и так лежит в git. Единственное, чего в коде нет, это
#       данные приложения: журнал смен.
#
#   ./restore.sh etcd [каталог]      вернуть состояние кластера из снимка
#   ./restore.sh journal [каталог]   залить журнал смен в PostgreSQL
#   ./restore.sh ucheniye [каталог]  учение: сломать нарочно и восстановить
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ARTIFACTS="${ARTIFACTS:-$REPO/cluster/artifacts}"
BACKUPS="${BACKUPS:-$ARTIFACTS/backups}"
export KUBECONFIG="${KUBECONFIG:-$REPO/cluster/kubespray/kubeconfig}"
SSH_KEY="${SSH_KEY:-/root/.ssh/quarry-lab}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -i "$SSH_KEY")

say() { printf '\n=== %s\n' "$*"; }

poslednyaya() {
  ls -1d "$BACKUPS"/*/ 2>/dev/null | sort | tail -1 | sed 's:/$::'
}

kopiya() {
  local k="${1:-$(poslednyaya)}"
  [ -n "$k" ] && [ -d "$k" ] || { echo "копия не найдена: ${1:-нет копий в $BACKUPS}" >&2; exit 1; }
  echo "$k"
}

upravlyayushchiy_ip() {
  kubectl get nodes -l node-role.kubernetes.io/control-plane \
    -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null \
    || cat "$ARTIFACTS/backups/.control-plane-ip" 2>/dev/null
}

na_uzle() {
  local ip="$1"; shift
  ssh "${SSH_OPTS[@]}" "$SSH_USER@$ip" "$@"
}

ARGO_NS="${ARGO_NS:-argocd}"
ARGO_APP="${ARGO_APP:-quarry}"

# 🔴 В кластере с GitOps остановить компонент командой kubectl НЕЛЬЗЯ.
# У приложения включено самолечение: ArgoCD видит, что реплик стало 0, хотя
# в репозитории написана одна, и возвращает приёмник за секунды. Он снова
# создаёт таблицу и пишет поверх восстановления.
#
# Правильный порядок: на время работ приложение переводится в ручной режим,
# после работ автосинхронизация возвращается. Это же правило действует на
# площадке: чинить мимо репозитория бесполезно, надо сначала договориться
# с тем, кто считает репозиторий истиной.
gitops_est() {
  kubectl -n "$ARGO_NS" get applications.argoproj.io "$ARGO_APP" >/dev/null 2>&1
}

gitops_pauza() {
  gitops_est || return 0
  kubectl -n "$ARGO_NS" patch applications.argoproj.io "$ARGO_APP" --type=json \
    -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]' >/dev/null 2>&1 || true
  echo "ArgoCD переведён в ручной режим на время работ"
}

gitops_vernut() {
  gitops_est || return 0
  kubectl -n "$ARGO_NS" patch applications.argoproj.io "$ARGO_APP" --type=merge \
    -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' >/dev/null
  echo "ArgoCD снова следит за кластером сам"
}

vosstanovit_etcd() {
  local k ip; k="$(kopiya "${1:-}")"; ip="$(upravlyayushchiy_ip)"
  [ -n "$ip" ] || { echo "не вижу управляющий узел" >&2; exit 1; }
  say "восстановление etcd на $ip из $k"

  scp "${SSH_OPTS[@]}" -q "$k/etcd-snapshot.db" "$SSH_USER@$ip:/tmp/vosstanovlenie.db"

  # Сколько кластер лежал, надо мерить СНАРУЖИ и во время работ. Опрос после
  # того, как всё поднялось, покажет ноль секунд и соврёт: к этому моменту
  # перерыв уже кончился. Наблюдатель стучится в API каждую секунду, пока
  # идёт восстановление, и отвечает на вопрос заказчика «сколько минут
  # диспетчер не видел кластер».
  local zhurnal="/tmp/api-pereryv.$$"
  ( while :; do
      if kubectl get --raw='/readyz' --request-timeout=2s >/dev/null 2>&1
      then echo "$(date +%s) est"; else echo "$(date +%s) net"; fi
      sleep 1
    done ) > "$zhurnal" 2>/dev/null &
  local nablyudatel=$!
  trap 'kill '"$nablyudatel"' 2>/dev/null || true' RETURN

  # Порядок важен. Сначала гасится kubelet: пока он жив, kube-apiserver
  # пишет в etcd и мешает подменить данные под ним.
  na_uzle "$ip" 'set -e
    sudo systemctl stop kubelet
    sudo systemctl stop etcd
    sudo rm -rf /var/lib/etcd.otkat
    sudo mv /var/lib/etcd /var/lib/etcd.otkat

    # Старые данные не удаляем, а отодвигаем: если снимок окажется негодным,
    # откатываться будет некуда. Чистит их уже человек, после проверки.
    #
    # Восстановление идёт БЕЗ обращения к кластеру: etcdutl работает с файлом.
    # Имя члена и адрес должны совпасть с /etc/etcd.env, иначе поднятая база
    # будет считать себя другим участником и кластер не соберётся.
    NAME=$(sudo grep -oP "^ETCD_NAME=\K.*" /etc/etcd.env)
    PEER=$(sudo grep -oP "^ETCD_INITIAL_ADVERTISE_PEER_URLS=\K.*" /etc/etcd.env)
    CLUSTER=$(sudo grep -oP "^ETCD_INITIAL_CLUSTER=\K.*" /etc/etcd.env)
    sudo etcdutl snapshot restore /tmp/vosstanovlenie.db \
      --name "$NAME" \
      --initial-cluster "$CLUSTER" \
      --initial-advertise-peer-urls "$PEER" \
      --data-dir /var/lib/etcd >/dev/null

    sudo systemctl start etcd
    sudo systemctl start kubelet
    sudo rm -f /tmp/vosstanovlenie.db'

  say "ждём, пока вернётся kube-apiserver"
  for _ in $(seq 1 60); do
    kubectl get --raw='/readyz' --request-timeout=3s >/dev/null 2>&1 && break
    sleep 3
  done

  # Даём наблюдателю записать ещё пару удачных опросов и считаем перерыв.
  sleep 3
  kill "$nablyudatel" 2>/dev/null || true
  local ne_otvechal
  ne_otvechal="$(grep -c ' net$' "$zhurnal" || true)"
  echo "API не отвечал примерно $ne_otvechal с"
  rm -f "$zhurnal"

  kubectl get nodes
  say "старые данные лежат на узле в /var/lib/etcd.otkat, удалить после проверки"
}

vosstanovit_zhurnal() {
  local k; k="$(kopiya "${1:-}")"
  say "журнал смен из $k"
  local bylo ozhidaem
  ozhidaem="$(cat "$k/journal-events.txt" 2>/dev/null || echo '?')"
  bylo="$(kubectl -n quarry exec sts/postgres -- \
    psql -U quarry -d quarry -tAc 'select count(*) from events' 2>/dev/null | tr -d '\r' || echo 0)"
  echo "сейчас в базе: $bylo, в копии: $ozhidaem"

  # 🔴 Писателя на время заливки останавливаем. Приёмник переживает потерю
  # таблицы (в журнале стоит CREATE TABLE IF NOT EXISTS) и молча создаёт её
  # заново пустой, продолжая писать. Восстанавливать базу под работающим
  # писателем значит мерить результат вперемешку с новыми строками и никогда
  # не сойтись с копией.
  local pisatel=0
  if kubectl -n quarry get deploy/collector >/dev/null 2>&1; then
    pisatel="$(kubectl -n quarry get deploy/collector -o jsonpath='{.spec.replicas}')"
    gitops_pauza
    kubectl -n quarry scale deploy/collector --replicas=0 >/dev/null
    # ⚠️ `kubectl wait --for=delete` возвращается и когда под ещё доживает:
    # приёмник при остановке сливает накопленную очередь в базу, и эти строки
    # приходят уже поверх залитого дампа. Ждём, пока подов не останется ни
    # одного, и только потом заливаем.
    local ostalos
    for _ in $(seq 1 60); do
      ostalos="$(kubectl -n quarry get pods -l app=collector --no-headers 2>/dev/null | wc -l)"
      [ "$ostalos" = "0" ] && break
      sleep 2
    done
    echo "приёмник остановлен на время заливки (подов осталось: $ostalos)"
  fi

  # В дампе стоит DROP ... IF EXISTS: заливка заменяет таблицу целиком, а не
  # подмешивает строки к тому, что уже есть. Иначе «восстановили» означало бы
  # «удвоили».
  zcat "$k/journal.sql.gz" | kubectl -n quarry exec -i sts/postgres -- \
    psql -U quarry -d quarry -v ON_ERROR_STOP=1 -q

  local stalo
  stalo="$(kubectl -n quarry exec sts/postgres -- \
    psql -U quarry -d quarry -tAc 'select count(*) from events' | tr -d '\r')"
  echo "после восстановления: $stalo"
  [ "$stalo" = "$ozhidaem" ] && echo "✅ сходится с копией" || echo "⚠️ не сходится с копией ($ozhidaem)"

  if [ "$pisatel" != "0" ]; then
    kubectl -n quarry scale deploy/collector --replicas="$pisatel" >/dev/null
    kubectl -n quarry rollout status deploy/collector --timeout=120s
    gitops_vernut
  fi

  say "смены в восстановленном журнале"
  kubectl -n quarry exec sts/postgres -- psql -U quarry -d quarry -c \
    'select strategy, shift_no, count(*) sobytiy, round(sum(tons)::numeric) tonn
       from events group by 1,2 order by 1,2'
}

# Учение: ломаем журнал нарочно и возвращаем его из копии. Проверяется не
# файл, а вся цепочка целиком, включая доступы и права.
ucheniye() {
  local k; k="$(kopiya "${1:-}")"
  say "УЧЕНИЕ: теряем журнал смен"
  local do_ucheniya
  do_ucheniya="$(kubectl -n quarry exec sts/postgres -- \
    psql -U quarry -d quarry -tAc 'select count(*) from events' | tr -d '\r')"
  echo "до учения в базе событий: $do_ucheniya"

  kubectl -n quarry exec sts/postgres -- \
    psql -U quarry -d quarry -c 'DROP TABLE events'
  echo "таблица удалена"

  kubectl -n quarry exec sts/postgres -- \
    psql -U quarry -d quarry -c 'select count(*) from events' 2>&1 | head -2 || true

  vosstanovit_zhurnal "$k"
  say "учение закончено"
}

case "${1:-}" in
  etcd)     vosstanovit_etcd "${2:-}" ;;
  journal)  vosstanovit_zhurnal "${2:-}" ;;
  ucheniye) ucheniye "${2:-}" ;;
  *) sed -n '2,30p' "$0"; exit 1 ;;
esac
