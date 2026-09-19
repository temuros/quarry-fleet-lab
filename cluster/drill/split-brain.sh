#!/usr/bin/env bash
# Учение: рвём связь МЕЖДУ машинами хранилища, оставив обеим связь с
# кластером и шлюзом.
#
# Это единственная авария пары, которую не показывает гашение машины. Когда
# машину гасят, вторая сторона мертва по-настоящему, и спорить не с кем. А
# при разрыве канала между ними обе живы, обе видят потребителей, и картина
# у обеих одинаковая: «сосед пропал». Без третьего голоса каждая берёт том в
# запись, копии расходятся необратимо, и дальше одну из них придётся
# выбросить вместе с тем, что на неё записали.
#
# Что проверяем: главной остаётся РОВНО ОДНА машина, вторая честно снимает
# притязания, а после восстановления связи пара сходится без ручного разбора.
#
#   ./split-brain.sh        провести учение целиком
#   ./split-brain.sh razorvat / vernut    по шагам, если нужно посмотреть
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TF_DIR="${TF_DIR:-$REPO/cluster/terraform}"
SSH_KEY="${SSH_KEY:-/root/.ssh/quarry-lab}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_OPTS=(-n -o StrictHostKeyChecking=no -o ConnectTimeout=10 -i "$SSH_KEY")
VIP="${VIP:-192.168.200.251}"
ARBITR="${ARBITR:-192.168.200.1:8099}"

mapfile -t NAS < <(
  TF_DATA_DIR="${TF_DATA_DIR:-/root/.tf-quarry}" terraform -chdir="$TF_DIR" output -json nas |
    python3 -c 'import json,sys; [print(k,v) for k,v in sorted(json.load(sys.stdin).items())]'
)
IMYA1="$(echo "${NAS[0]}" | cut -d' ' -f1)"; IP1="$(echo "${NAS[0]}" | cut -d' ' -f2)"
IMYA2="$(echo "${NAS[1]}" | cut -d' ' -f1)"; IP2="$(echo "${NAS[1]}" | cut -d' ' -f2)"

HRONIKI="${HRONIKI:-$REPO/cluster/artifacts/drills}"
mkdir -p "$HRONIKI"
HRONIKA="$HRONIKI/$(date +%Y-%m-%d-%H%M)-split-brain.txt"

# Хроника пишется в файл сразу: разбирать аварию по памяти через неделю
# бесполезно, а по хронике видно, что и когда произошло.
say() { printf '\n=== %s\n' "$*" | tee -a "$HRONIKA"; }
skazat() { printf '%s\n' "$*" | tee -a "$HRONIKA"; }
na() { local ip="$1"; shift; ssh "${SSH_OPTS[@]}" "$SSH_USER@$ip" "$@"; }

kartina() {
  for entry in "${NAS[@]}"; do
    imya="$(echo "$entry" | cut -d' ' -f1)"
    ip="$(echo "$entry" | cut -d' ' -f2)"
    rol="$(na "$ip" "sudo drbdadm role quarry 2>/dev/null" || echo '?')"
    svyaz="$(na "$ip" "sudo drbdadm cstate quarry 2>/dev/null" || echo '?')"
    vip="нет"
    na "$ip" "ip -4 -o addr show | grep -q '$VIP'" 2>/dev/null && vip="ДЕРЖИТ"
    tom="нет"
    na "$ip" "mountpoint -q /srv/quarry-nfs" 2>/dev/null && tom="смонтирован"
    skazat "$(printf '  %-13s роль %-10s связь %-12s общий адрес %-7s том %s' \
      "$imya" "$rol" "$svyaz" "$vip" "$tom")"
  done
  skazat "  арбитр: $(curl -s --max-time 3 "http://$ARBITR/status" || echo 'не отвечает')"
}

razorvat() {
  say "рву связь между $IMYA1 и $IMYA2 (канал реплики и VRRP)"
  # Рвём ТОЛЬКО между ними: связь с кластером и шлюзом остаётся, иначе это
  # была бы обычная изоляция машины, а не спор двух живых.
  na "$IP1" "sudo iptables -I INPUT  -s $IP2 -j DROP; sudo iptables -I OUTPUT -d $IP2 -j DROP"
  na "$IP2" "sudo iptables -I INPUT  -s $IP1 -j DROP; sudo iptables -I OUTPUT -d $IP1 -j DROP"
  skazat "связь разорвана в $(date -Iseconds)"
}

vernut() {
  say "возвращаю связь"
  na "$IP1" "sudo iptables -D INPUT  -s $IP2 -j DROP 2>/dev/null; sudo iptables -D OUTPUT -d $IP2 -j DROP 2>/dev/null" || true
  na "$IP2" "sudo iptables -D INPUT  -s $IP1 -j DROP 2>/dev/null; sudo iptables -D OUTPUT -d $IP1 -j DROP 2>/dev/null" || true
  skazat "связь возвращена в $(date -Iseconds)"
}

uchenie() {
  say "до учения"
  kartina

  razorvat
  nachalo="$(date +%s)"

  for shag in 1 2 3 4 5 6; do
    sleep 10
    say "через $(( $(date +%s) - nachalo )) с после разрыва"
    kartina
  done

  say "сколько машин держат том в записи"
  glavnyh=0
  for entry in "${NAS[@]}"; do
    ip="$(echo "$entry" | cut -d' ' -f2)"
    # ⚠️ `drbdadm role` печатает «Primary/Secondary»: своя роль и роль
    # соседа через косую черту. Сравнение всей строки с «Primary» всегда
    # ложно, и учение отчитывается «главных: 0» при любом исходе.
    rol="$(na "$ip" 'sudo drbdadm role quarry 2>/dev/null' || echo '?')"
    [ "${rol%%/*}" = "Primary" ] && glavnyh=$((glavnyh + 1))
  done
  skazat "главных: $glavnyh"
  if [ "$glavnyh" -gt 1 ]; then
    skazat "🔴 УЧЕНИЕ ПРОВАЛЕНО: том взят в запись с обеих сторон, копии разошлись"
  else
    skazat "✅ главная ровно одна, расхождения копий не будет"
  fi

  vernut
  sleep 20
  say "после возвращения связи"
  kartina
  echo
  echo "хроника: $HRONIKA"
}

case "${1:-uchenie}" in
  uchenie|"") uchenie ;;
  razorvat)   razorvat ;;
  vernut)     vernut ;;
  kartina)    kartina ;;
  *) echo "команды: uchenie | razorvat | vernut | kartina"; exit 1 ;;
esac
