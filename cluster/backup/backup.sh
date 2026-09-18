#!/usr/bin/env bash
# Резервная копия стенда: состояние кластера и журнал смен.
#
# Что копировать, а что не надо. У стенда три слоя, и восстанавливаются они
# по-разному:
#
#   1. КОД (Terraform, Kubespray, манифесты) лежит в git и в Gitea внутри
#      контура. Копировать его отдельно нечего: кластер поднимается из кода
#      с нуля, ArgoCD раскатывает приложения сам.
#   2. СОСТОЯНИЕ КЛАСТЕРА (объекты API: секреты, учётные записи, всё, что
#      завели руками или чем распорядился оператор) живёт в etcd. Это снимок
#      etcd.
#   3. ДАННЫЕ ПРИЛОЖЕНИЯ — журнал смен в PostgreSQL. В снимке etcd их НЕТ:
#      etcd хранит описание тома, а сами файлы лежат на диске узла. Потерять
#      узел с томом и надеяться на снимок etcd — самый частый способ остаться
#      без данных.
#
# Копия уезжает С УЗЛА на машину оператора: копия на том же диске, что и
# оригинал, копией не является.
#
#   ./backup.sh                 снять копию
#   ./backup.sh list            какие копии есть
#   ./backup.sh check [каталог] что внутри копии и цела ли она
#
# Восстановление и учения: ./restore.sh
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

# Управляющий узел ищем у самого кластера, а не в файле с настройками:
# файл может отстать от жизни, кластер нет.
upravlyayushchiy_ip() {
  kubectl get nodes -l node-role.kubernetes.io/control-plane \
    -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}'
}

na_uzle() {
  local ip="$1"; shift
  ssh "${SSH_OPTS[@]}" "$SSH_USER@$ip" "$@"
}

snimok_etcd() {
  local kuda="$1" ip; ip="$(upravlyayushchiy_ip)"
  say "снимок etcd с узла $ip"

  # Снимок снимается с РАБОТАЮЩЕЙ базы: etcdctl просит у неё согласованную
  # копию, останавливать кластер не нужно. Доступы etcdctl читает из
  # /etc/etcd.env, который Kubespray положил на узел.
  #
  # 🔴 Адрес НЕ передаём флагом --endpoints: в etcd.env уже лежит
  # ETCDCTL_ENDPOINTS, а etcdctl 3.6 отказывается работать, когда флаг
  # перекрывает переменную («conflicting environment variable is shadowed»).
  # Либо переменные, либо флаги, смешивать нельзя.
  na_uzle "$ip" 'set -a; sudo cat /etc/etcd.env > /tmp/etcd.env; . /tmp/etcd.env; set +a;
    sudo -E etcdctl snapshot save /tmp/etcd-snapshot.db >/dev/null &&
    sudo chmod 0644 /tmp/etcd-snapshot.db && rm -f /tmp/etcd.env'

  scp "${SSH_OPTS[@]}" -q "$SSH_USER@$ip:/tmp/etcd-snapshot.db" "$kuda/etcd-snapshot.db"
  na_uzle "$ip" 'sudo rm -f /tmp/etcd-snapshot.db'

  # etcdutl считает по файлу хэш и ревизию. Это единственная проверка, которая
  # говорит не «файл скачался», а «база внутри читается».
  na_uzle "$ip" 'true' >/dev/null
  etcd_status "$kuda/etcd-snapshot.db" "$ip" > "$kuda/etcd-snapshot.txt"
  cat "$kuda/etcd-snapshot.txt"
}

etcd_status() {
  local fayl="$1" ip="$2"
  scp "${SSH_OPTS[@]}" -q "$fayl" "$SSH_USER@$ip:/tmp/proverka.db"
  na_uzle "$ip" 'etcdutl snapshot status /tmp/proverka.db --write-out=table; rm -f /tmp/proverka.db'
}

dump_zhurnala() {
  local kuda="$1"
  say "журнал смен из PostgreSQL"
  # pg_dump изнутри пода: наружу база не смотрит, и не должна.
  kubectl -n quarry exec sts/postgres -- \
    pg_dump -U quarry -d quarry --no-owner --clean --if-exists \
    | gzip > "$kuda/journal.sql.gz"

  # 🔴 Сколько событий в копии, считаем ПО САМОЙ КОПИИ, а не по живой базе.
  # Приёмник пишет непрерывно, и count(*) через секунду после дампа даёт
  # число, которого в файле уже нет. Копия, которая говорит о себе неправду,
  # хуже отсутствия копии: сверка при восстановлении всегда «не сходится»,
  # и человек привыкает не смотреть на неё.
  local sobytiy
  sobytiy="$(zcat "$kuda/journal.sql.gz" |
    awk '/^COPY public[.]events /{v=1; next} v && $0=="\\." {v=0} v{n++} END{print n+0}')"
  echo "$sobytiy" > "$kuda/journal-events.txt"
  echo "событий в копии: $sobytiy"
}

dostupy() {
  local kuda="$1"
  say "доступы и состояние Terraform"
  # ⚠️ Эти файлы НЕ лежат в git и восстановлению из кода не поддаются:
  # без них кластер поднимется, но чужой. Поэтому копия хранится там же,
  # где и остальные артефакты, мимо репозитория.
  mkdir -p "$kuda/dostupy"
  for f in "$REPO/cluster/kubespray/kubeconfig" "$ARTIFACTS/gitea-password"; do
    [ -f "$f" ] && cp "$f" "$kuda/dostupy/" || true
  done
  [ -f "$REPO/cluster/terraform/terraform.tfstate" ] &&
    cp "$REPO/cluster/terraform/terraform.tfstate" "$kuda/dostupy/" || true
  ls -1 "$kuda/dostupy"
}

opis() {
  local kuda="$1"
  {
    echo "Копия стенда карьера"
    echo "снята: $(date -Iseconds)"
    echo "кластер: $(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')"
    echo "узлов: $(kubectl get nodes --no-headers | wc -l)"
    echo "событий в журнале: $(cat "$kuda/journal-events.txt")"
    echo
    echo "Что чем восстанавливается:"
    echo "  etcd-snapshot.db  состояние кластера (узлы те же)  -> ./restore.sh etcd"
    echo "  journal.sql.gz    журнал смен                      -> ./restore.sh journal"
    echo "  dostupy/          kubeconfig, пароли, tfstate      -> руками"
    echo "  приложения        из Gitea через ArgoCD            -> cluster/gitops/bootstrap.sh"
  } > "$kuda/opis.txt"
  cat "$kuda/opis.txt"
}

snyat() {
  local kuda="$BACKUPS/$(date +%Y-%m-%d-%H%M)"
  mkdir -p "$kuda"
  snimok_etcd "$kuda"
  dump_zhurnala "$kuda"
  dostupy "$kuda"
  opis "$kuda"
  say "копия готова: $kuda"
  du -sh "$kuda"
}

poslednyaya() {
  ls -1d "$BACKUPS"/*/ 2>/dev/null | sort | tail -1 | sed 's:/$::'
}

case "${1:-snyat}" in
  snyat|backup|"") snyat ;;
  list)
    ls -1d "$BACKUPS"/*/ 2>/dev/null | sort || echo "копий нет"
    ;;
  check)
    kuda="${2:-$(poslednyaya)}"
    [ -n "$kuda" ] || { echo "копий нет"; exit 1; }
    cat "$kuda/opis.txt"
    say "проверка снимка etcd"
    etcd_status "$kuda/etcd-snapshot.db" "$(upravlyayushchiy_ip)"
    say "проверка дампа журнала"
    gzip -t "$kuda/journal.sql.gz" && echo "архив цел, строк: $(zcat "$kuda/journal.sql.gz" | wc -l)"
    ;;
  *) echo "команды: snyat | list | check [каталог]"; exit 1 ;;
esac
