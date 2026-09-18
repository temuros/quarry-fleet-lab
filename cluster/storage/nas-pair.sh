#!/usr/bin/env bash
# Хранилище контура из двух машин: реплика диска и общий адрес.
#
# Сетевое хранилище развязало данные и узлы кластера, но само осталось в одном
# экземпляре и стало последней единственной точкой отказа: его потеря
# останавливает не один под, а все базы сразу.
#
# Здесь три вещи, и порядок между ними важен:
#
#   1. DRBD держит копию диска на второй машине. Запись подтверждается,
#      только когда её приняли ОБЕ (протокол C): журнал смен не должен терять
#      последние секунды, потому что именно в них обычно и случается авария.
#   2. NFS отдаёт этот диск кластеру, но только с той машины, которая сейчас
#      главная.
#   3. keepalived держит общий адрес и переносит его на живую машину. Кластеру
#      про переключение знать не нужно: он как ходил на один адрес, так и ходит.
#
# ⚠️ Две машины это минимум, а не идеал. При разрыве связи между ними каждая
# может решить, что вторая умерла; на площадке от этого ставят третий голос
# или ограждение по питанию. В стенде принят более простой ответ: главной
# становится та, что удержала общий адрес, вторая остаётся ведомой.
#
#   ./nas-pair.sh nastroit      поставить пакеты, собрать реплику, поднять NFS
#   ./nas-pair.sh status        кто сейчас главный и в каком состоянии реплика
#   ./nas-pair.sh perenesti     перенести данные со старого хранилища на шлюзе
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TF_DIR="${TF_DIR:-$REPO/cluster/terraform}"
SSH_KEY="${SSH_KEY:-/root/.ssh/quarry-lab}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_OPTS=(-n -o StrictHostKeyChecking=no -o ConnectTimeout=10 -i "$SSH_KEY")
VIP="${VIP:-192.168.200.251}"
DANNYE="${DANNYE:-/srv/quarry-nfs}"
SET="${SET:-192.168.200.0/24}"
STAROE="${STAROE:-192.168.200.1:/srv/quarry-nfs}"

say() { printf '\n=== %s\n' "$*"; }

mapfile -t NAS < <(
  TF_DATA_DIR="${TF_DATA_DIR:-/root/.tf-quarry}" terraform -chdir="$TF_DIR" output -json nas |
    python3 -c 'import json,sys; [print(k,v) for k,v in sorted(json.load(sys.stdin).items())]'
)
IP1="$(echo "${NAS[0]}" | cut -d' ' -f2)"
IP2="$(echo "${NAS[1]}" | cut -d' ' -f2)"
IMYA1="$(echo "${NAS[0]}" | cut -d' ' -f1)"
IMYA2="$(echo "${NAS[1]}" | cut -d' ' -f1)"

na() { local ip="$1"; shift; ssh "${SSH_OPTS[@]}" "$SSH_USER@$ip" "$@"; }

postavit_pakety() {
  local ip="$1"
  na "$ip" 'set -e
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      drbd-utils keepalived nfs-kernel-server "linux-modules-extra-$(uname -r)" >/dev/null
    sudo modprobe drbd
    echo drbd | sudo tee /etc/modules-load.d/drbd.conf >/dev/null
    # Реплика должна подниматься сама при старте машины: иначе вернувшаяся из
    # ремонта машина выглядит живой, а копии данных на ней нет.
    sudo systemctl enable drbd >/dev/null 2>&1 || true'
}

nastroit_drbd() {
  local ip="$1" nomer="$2"
  # Ресурс описывает обе стороны одинаково: конфигурация на машинах
  # совпадает до байта, иначе DRBD отказывается собирать пару.
  na "$ip" "sudo tee /etc/drbd.d/quarry.res >/dev/null <<'EOF'
resource quarry {
  protocol C;                 # запись подтверждается, когда её приняли обе машины
  device    /dev/drbd0;
  disk      /dev/vdb;
  meta-disk internal;

  net {
    # Расхождение после разрыва связи разбираем автоматически в пользу той
    # стороны, где меньше несогласованных изменений: на стенде это лучше, чем
    # ждать человека, а на площадке сюда ставят ручной разбор.
    after-sb-0pri discard-zero-changes;
    after-sb-1pri discard-secondary;
    after-sb-2pri disconnect;
  }

  # ⚠️ Без node-id: он нужен только схемам больше чем на две машины, а
  # здешние утилиты на него отвечают невнятным «Parse error ... but got
  # node-id», и ресурс просто не собирается.
  on $IMYA1 {
    address $IP1:7789;
  }
  on $IMYA2 {
    address $IP2:7789;
  }
}
EOF"
  na "$ip" 'sudo drbdadm create-md --force quarry >/dev/null 2>&1 || true
    sudo drbdadm up quarry 2>&1 | tail -2 || true'
}

nastroit_keepalived() {
  local ip="$1" rol="$2" prioritet="$3"
  # Скрипты переключения: главная машина поднимает реплику в режим записи,
  # монтирует том и включает отдачу; ведомая делает обратное. Без этого общий
  # адрес переедет на машину, которая ничего не отдаёт.
  na "$ip" "sudo tee /usr/local/sbin/quarry-nas-glavnyy >/dev/null <<'EOF'
#!/bin/sh
set -e
# ⚠️ Реплику поднимаем явно: после перезагрузки машины ресурс DRBD может быть
# не поднят, и тогда «стать главным» молча не срабатывает.
drbdadm up quarry 2>/dev/null || true
drbdadm primary quarry
mountpoint -q $DANNYE || mount /dev/drbd0 $DANNYE
systemctl start nfs-kernel-server
exportfs -ra
logger 'quarry-nas: стал главным, том отдан'
EOF
  sudo tee /usr/local/sbin/quarry-nas-vedomyy >/dev/null <<'EOF'
#!/bin/sh
exportfs -ua || true
systemctl stop nfs-kernel-server || true
umount $DANNYE 2>/dev/null || true
drbdadm secondary quarry || true
logger 'quarry-nas: стал ведомым'
EOF
  sudo chmod +x /usr/local/sbin/quarry-nas-glavnyy /usr/local/sbin/quarry-nas-vedomyy"

  # 🔴 Машина с неготовой репликой не должна претендовать на общий адрес.
  # Именно это и случилось на учении: погашенная машина вернулась, VRRP отдал
  # ей адрес по старшинству, а диск у неё был ещё Inconsistent, и хранилище
  # перестало отдавать том, хотя вторая машина была цела и свежа.
  na "$ip" "sudo tee /usr/local/sbin/quarry-nas-gotov >/dev/null <<'EOF'
#!/bin/sh
# Готова обслуживать только машина, чья копия данных полная.
drbdadm status quarry 2>/dev/null | grep -q 'disk:UpToDate' || exit 1
exit 0
EOF
  sudo chmod +x /usr/local/sbin/quarry-nas-gotov"

  na "$ip" "sudo tee /etc/keepalived/keepalived.conf >/dev/null <<EOF
vrrp_script proverka_repliki {
    script \"/usr/local/sbin/quarry-nas-gotov\"
    interval 5
    fall 2
    rise 2
}

vrrp_instance quarry_nas {
    # Обе машины BACKUP и nopreempt: вернувшаяся из ремонта НЕ забирает
    # обслуживание обратно, пока работает вторая. Обратное переключение это
    # решение человека, а не автоматика: данные только что синхронизировались,
    # и лишний переезд ничего не улучшает.
    state BACKUP
    nopreempt
    interface ens3
    virtual_router_id 51
    priority $prioritet
    advert_int 1
    authentication {
        auth_type PASS
        auth_pass quarrynas
    }
    virtual_ipaddress {
        $VIP/24
    }
    track_script {
        proverka_repliki
    }
    notify_master \"/usr/local/sbin/quarry-nas-glavnyy\"
    notify_backup \"/usr/local/sbin/quarry-nas-vedomyy\"
    notify_fault  \"/usr/local/sbin/quarry-nas-vedomyy\"
}
EOF
  sudo mkdir -p $DANNYE
  sudo systemctl enable keepalived >/dev/null 2>&1 || true"
}

nastroit() {
  say "машины хранилища: $IMYA1 ($IP1) и $IMYA2 ($IP2)"

  for ip in "$IP1" "$IP2"; do
    echo "--- пакеты на $ip"
    postavit_pakety "$ip"
  done

  say "реплика диска"
  nastroit_drbd "$IP1" 0
  nastroit_drbd "$IP2" 1

  # Первая синхронизация: одна из сторон объявляется источником, иначе DRBD
  # не знает, чьи данные считать правильными, и ждёт решения человека.
  say "первая синхронизация, источник $IMYA1"
  na "$IP1" 'sudo drbdadm primary --force quarry; sleep 5
    sudo mkfs.ext4 -q -F /dev/drbd0 2>/dev/null || echo "файловая система уже есть"'

  say "общий адрес $VIP"
  nastroit_keepalived "$IP1" MASTER 150
  nastroit_keepalived "$IP2" BACKUP 100

  for ip in "$IP1" "$IP2"; do
    na "$ip" 'sudo systemctl restart keepalived'
  done
  sleep 10

  say "экспорт для кластера"
  for ip in "$IP1" "$IP2"; do
    na "$ip" "echo '$DANNYE $SET(rw,sync,no_subtree_check,no_root_squash,fsid=1)' | sudo tee /etc/exports >/dev/null"
  done
  na "$IP1" 'sudo /usr/local/sbin/quarry-nas-glavnyy' || true

  status
}

status() {
  say "реплика"
  for ip in "$IP1" "$IP2"; do
    printf '%s: ' "$ip"
    na "$ip" 'sudo drbdadm status quarry 2>/dev/null | head -3 | tr "\n" " "; echo' || echo "не отвечает"
  done
  say "кто держит общий адрес $VIP"
  for ip in "$IP1" "$IP2"; do
    if na "$ip" "ip -4 -o addr show | grep -q '$VIP'" 2>/dev/null; then
      echo "$ip"
      na "$ip" "df -h $DANNYE | tail -1; exportfs -v | head -2" || true
    fi
  done
}

# Перенос данных со старого хранилища: тома уже лежат на шлюзе, и терять их
# нельзя. Копируем с сохранением владельцев и прав, иначе базы не запустятся.
perenesti() {
  say "переношу данные со старого хранилища"
  na "$IP1" "sudo mkdir -p /mnt/staroe
    sudo mount -t nfs -o vers=4 $STAROE /mnt/staroe
    sudo cp -a /mnt/staroe/. $DANNYE/
    sudo umount /mnt/staroe
    echo 'перенесено:'; sudo ls -1 $DANNYE | head -12
    sudo du -sh $DANNYE"
}

# Обновить только правила переключения, не трогая диск и данные: нужно, когда
# конфигурация keepalived изменилась, а хранилище работает.
perenastroit() {
  say "обновляю правила переключения на обеих машинах"
  nastroit_keepalived "$IP1" BACKUP 150
  nastroit_keepalived "$IP2" BACKUP 100
  for ip in "$IP1" "$IP2"; do
    na "$ip" 'sudo systemctl restart keepalived' || true
  done
  sleep 12
  status
}

case "${1:-status}" in
  nastroit)     nastroit ;;
  perenastroit) perenastroit ;;
  status)    status ;;
  perenesti) perenesti ;;
  *) echo "команды: nastroit | perenastroit | status | perenesti"; exit 1 ;;
esac
