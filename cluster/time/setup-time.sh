#!/usr/bin/env bash
# Время внутри закрытого контура.
#
# Наружу NTP не ходит по условию задачи, поэтому часы узлов расходятся сами
# по себе: на стенде разошлись на 21 минуту, и это выглядело как «журнал смен
# отстаёт», хотя данные шли без задержки. Метку времени событию ставит тот
# узел, где работает приёмник, так что расхождение часов едет прямо в отчёт
# по смене.
#
# Источник времени здесь сам гипервизор: модуль ptp_kvm показывает гостю часы
# хоста через /dev/ptp0, а chrony держит по ним системное время. Ни одного
# пакета наружу, стратум 1 внутри периметра. На площадке вместо гипервизора
# обычно стоит сервер времени в стойке, настройка та же.
#
# 🔴 Чинится в ДВА шага, и порядок обязателен. Отставшие часы ломают apt:
# «Release file is not valid yet (invalid for another 21min)» - репозиторий
# датирован будущим, apt его не берёт. То есть пакеты, которыми чинят время,
# не поставить, пока время не поправлено хотя бы грубо. Сначала date, потом
# apt, потом chrony держит точно.
#
#   ./setup-time.sh          настроить на всех узлах
#   ./setup-time.sh check    показать расхождение часов
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$REPO/cluster/kubespray/kubeconfig}"
SSH_KEY="${SSH_KEY:-/root/.ssh/quarry-lab}"
SSH_USER="${SSH_USER:-ubuntu}"
# ⚠️ `-n` обязателен. Без него ssh в цикле `while read` читает тот же stdin,
# что и сам цикл, съедает список узлов и настраивает ровно один: остальные
# молча пропускаются, а скрипт заканчивается успехом.
SSH_OPTS=(-n -o StrictHostKeyChecking=no -o ConnectTimeout=10 -i "$SSH_KEY")

say() { printf '\n=== %s\n' "$*"; }

uzly() {
  kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name} {.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}'
}

na_uzle() {
  local ip="$1"; shift
  ssh "${SSH_OPTS[@]}" "$SSH_USER@$ip" "$@"
}

CHRONY_CONF='# Часы контура. Источник времени это гипервизор, наружу ничего не ходит.
#
# refclock PHC берёт часы хоста через /dev/ptp0 (модуль ptp_kvm). Сеть для
# этого не нужна вообще: значение читается через гипервизор, а не по NTP.
#
# 🔴 filter и maxdistance здесь не украшение. Виртуальная машина читает часы
# хоста с задержкой, а под нагрузкой (установка кластера, сборка образов)
# задержка скачет на секунды. Chrony честно считает такой источник негодным
# («no selectable sources») и остаётся вообще без времени, а часы уезжают.
# filter усредняет пачку измерений и убирает выбросы, maxdistance разрешает
# пользоваться источником, оценка погрешности которого великовата.
refclock PHC /dev/ptp0 poll 2 dpoll -2 filter 16 stratum 1
maxdistance 16

# Шагать, а не подтягивать плавно. Узел после долгого простоя возвращается с
# часами в прошлом, и «плавно» он догонял бы их часами: при отставании в 20
# минут плавная подстройка занимает больше суток.
makestep 1.0 -1

rtcsync
driftfile /var/lib/chrony/chrony.drift
logdir /var/log/chrony
'

nastroit_uzel() {
  local imya="$1" ip="$2" epoch="$3"

  # Шаг 1: грубо выставить время, иначе apt откажется брать репозиторий.
  na_uzle "$ip" "sudo date -u -s @$epoch >/dev/null"

  # Шаг 2: пакеты из зеркала контура. linux-modules-extra привязан к версии
  # ядра: в нём и лежит ptp_kvm, в базовом облачном образе его нет.
  na_uzle "$ip" 'set -e
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      chrony "linux-modules-extra-$(uname -r)" >/dev/null'

  # Шаг 3: часы гипервизора. Модуль прописывается в автозагрузку, иначе после
  # перезагрузки узла /dev/ptp0 не появится и chrony останется без источника.
  na_uzle "$ip" 'set -e
    sudo modprobe ptp_kvm
    echo ptp_kvm | sudo tee /etc/modules-load.d/ptp_kvm.conf >/dev/null
    test -e /dev/ptp0'

  # Шаг 4: chrony вместо systemd-timesyncd. Двум службам времени на одной
  # машине делать нечего, они будут тянуть часы каждая в свою сторону.
  na_uzle "$ip" "set -e
    sudo systemctl disable --now systemd-timesyncd >/dev/null 2>&1 || true
    printf '%s' '$CHRONY_CONF' | sudo tee /etc/chrony/chrony.conf >/dev/null
    sudo systemctl enable --now chrony >/dev/null 2>&1 || true
    sudo systemctl restart chrony"

  echo "  $imya готов"
}

proverka() {
  local moyo; moyo="$(date -u +%s)"
  printf '%-10s %-12s %-10s %s\n' "узел" "расхождение" "источник" "состояние"
  while read -r imya ip; do
    [ -n "$ip" ] || continue
    local ih raznica istochnik sostoyanie
    ih="$(na_uzle "$ip" 'date -u +%s' 2>/dev/null || echo 0)"
    raznica=$(( ih - moyo ))
    istochnik="$(na_uzle "$ip" 'chronyc sources 2>/dev/null | tail -1 | awk "{print \$2}"' 2>/dev/null || true)"
    sostoyanie="$(na_uzle "$ip" 'timedatectl show -p NTPSynchronized --value 2>/dev/null' 2>/dev/null || true)"
    printf '%-10s %-12s %-10s %s\n' "$imya" "${raznica} с" "${istochnik:-нет}" "синхронизирован: ${sostoyanie:-?}"
  done < <(uzly)
}

case "${1:-nastroit}" in
  nastroit|"")
    say "часы узлов до настройки"
    proverka
    epoch="$(date -u +%s)"
    say "настройка"
    while read -r imya ip; do
      [ -n "$ip" ] || continue
      nastroit_uzel "$imya" "$ip" "$epoch"
    done < <(uzly)
    sleep 5
    say "часы узлов после настройки"
    proverka
    ;;
  check) proverka ;;
  *) echo "команды: nastroit | check"; exit 1 ;;
esac
