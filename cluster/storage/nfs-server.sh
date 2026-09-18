#!/usr/bin/env bash
# Сетевое хранилище контура: то, что на площадке называют СХД.
#
# Зачем оно появилось. Тома local-path это каталог на диске КОНКРЕТНОЙ машины,
# и Kubernetes помнит эту привязку. Пока узел жив, всё хорошо; когда он гаснет,
# под с журналом смен не может переехать: на другом узле его данных просто нет.
# Учение это показало: журнал ждал возвращения своей машины.
#
# Сетевое хранилище разрывает эту связь. Файлы лежат не на узле, а рядом с
# кластером, и любой узел видит их одинаково. Под переезжает вместе с данными.
#
# Здесь роль СХД играет шлюз контура: на площадке в этом месте стоит стойка с
# дисковым массивом, а узлы точно так же монтируют его по сети.
#
# ⚠️ Сам сервер хранилища это единственная точка отказа, и это честное
# ограничение стенда. На площадке оно закрывается массивом с двумя
# контроллерами или парой серверов, а не настройкой.
#
#   ./nfs-server.sh start [адрес]   поднять хранилище (по умолчанию 192.168.200.1)
#   ./nfs-server.sh status          что сейчас отдаётся и кто примонтирован
#   ./nfs-server.sh stop            снять экспорт
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DANNYE="${DANNYE:-/srv/quarry-nfs}"
SET="${SET:-192.168.200.0/24}"
ADDR="${2:-192.168.200.1}"

say() { printf '\n=== %s\n' "$*"; }

start() {
  say "каталог хранилища $DANNYE"
  mkdir -p "$DANNYE"
  # Права открытые намеренно: в контейнерах свои идентификаторы пользователей,
  # и сопоставлять их с хостовыми на стенде смысла нет. На площадке здесь
  # настраивают отображение пользователей на самой СХД.
  chmod 0777 "$DANNYE"

  say "экспорт для сети $SET"
  # no_root_squash нужен, потому что базы в контейнерах работают от root и
  # создают свои каталоги сами. Без него том монтируется, но остаётся
  # недоступным для записи, и под падает не сразу, а на первой записи.
  local stroka="$DANNYE $SET(rw,sync,no_subtree_check,no_root_squash)"
  if grep -qF "$DANNYE " /etc/exports 2>/dev/null; then
    sed -i "s#^$DANNYE .*#$stroka#" /etc/exports
  else
    echo "$stroka" >> /etc/exports
  fi
  exportfs -ra
  systemctl restart nfs-kernel-server 2>/dev/null || service nfs-kernel-server restart

  say "проверка"
  exportfs -v
  echo "адрес хранилища для узлов: $ADDR:$DANNYE"
}

status() {
  say "что отдаётся"
  exportfs -v 2>/dev/null || echo "экспортов нет"
  say "служба"
  systemctl is-active nfs-kernel-server 2>/dev/null || echo "не запущена"
  say "кто примонтирован"
  # Показывает узлы, которые сейчас держат том: удобно перед тем, как
  # останавливать хранилище.
  ss -tn state established '( sport = :2049 )' 2>/dev/null | tail -n +2 | awk '{print $4, "<-", $5}' || true
  say "занято"
  du -sh "$DANNYE" 2>/dev/null || true
  ls -1 "$DANNYE" 2>/dev/null | head -10 || true
}

stop() {
  say "снимаю экспорт"
  exportfs -u "$SET:$DANNYE" 2>/dev/null || true
  systemctl stop nfs-kernel-server 2>/dev/null || service nfs-kernel-server stop
  echo "данные остались в $DANNYE"
}

case "${1:-status}" in
  start)  start ;;
  status) status ;;
  stop)   stop ;;
  *) echo "команды: start [адрес] | status | stop"; exit 1 ;;
esac
