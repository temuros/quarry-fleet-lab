#!/usr/bin/env bash
# Фаза 1 закрытого контура: собрать всё, что понадобится внутри.
#
# Запускается на управляющей машине, пока у неё ещё ЕСТЬ интернет, и
# складывает в cluster/artifacts/offline четыре вещи:
#
#   files/      бинарники и манифесты, которые Kubespray обычно тянет
#               с dl.k8s.io и GitHub, разложенные по тем же путям
#   registry/   хранилище реестра: сюда skopeo кладёт образы кластера
#   apt/        крошечный репозиторий пакетов, чтобы на узлах проходил apt update
#   bin/        сам реестр (бинарник distribution), которым потом раздаём образы
#
# Дальше mirror.sh раздаёт это узлам внутри контура, а
# cluster/kubespray/deploy.sh с OFFLINE=1 ставит кластер, ни разу не выйдя
# наружу с узлов.
#
#   ./collect.sh            собрать всё
#   ./collect.sh lists      только пересобрать списки файлов и образов
#   ./collect.sh files      только файлы
#   ./collect.sh images     только образы
#   ./collect.sh apt        только репозиторий пакетов
#   ./collect.sh gitops     манифесты и образы ArgoCD и Gitea
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
KUBESPRAY="${KUBESPRAY:-/opt/kubespray}"
VENV="${VENV:-/opt/kubespray-venv}"
ARTIFACTS="${ARTIFACTS:-$REPO/cluster/artifacts/offline}"
FILES="$ARTIFACTS/files"
REGDATA="$ARTIFACTS/registry"
APT="$ARTIFACTS/apt"
BIN="$ARTIFACTS/bin"
LISTS="$ARTIFACTS/lists"

# Имя реестра внутри контура. Постоянное: адрес зеркала может меняться, а имя,
# под которым образы лежат в манифестах, меняться не должно.
REGISTRY_NAME="${REGISTRY_NAME:-registry.quarry.local:5000}"
# Куда заливаем образы на этой машине. Хост в имени образа реестр не хранит,
# поэтому залитое сюда прекрасно отдаётся потом с любого адреса.
LOCAL_REGISTRY="${LOCAL_REGISTRY:-127.0.0.1:5000}"

# Версия реестра, которым раздаём образы внутри контура.
REGISTRY_VERSION="${REGISTRY_VERSION:-3.0.0}"
REGISTRY_URL="https://github.com/distribution/distribution/releases/download/v${REGISTRY_VERSION}/registry_${REGISTRY_VERSION}_linux_amd64.tar.gz"

# Что из огромного списка Kubespray нам действительно нужно.
# Списки generate_list.sh перечисляют ВСЁ, что Kubespray умеет поставить:
# cilium, kube-ovn, kata, gvisor, cri-o, metallb и так далее. В контур несём
# ровно наш профиль, иначе вместо гигабайта поедет десяток.
NUZHNY_FILES='/kubelet$|/kubectl$|/kubeadm$|etcd-v.*-linux-amd64\.tar\.gz$|cni-plugins-linux-amd64|calicoctl-linux-amd64$|calico/raw/.*/crds\.yaml$|crictl-v.*-linux-amd64\.tar\.gz$|runc\.amd64$|containerd-[0-9].*-linux-amd64\.tar\.gz$|nerdctl-.*-linux-amd64\.tar\.gz$'
# ⚠️ library/nginx тут не для сайта: Kubespray ставит его локальным
# балансировщиком до API на каждом рабочем узле. Забыли образ - установка
# доходит до самого конца и падает на выкате nginx-proxy.
NUZHNY_IMAGES='kube-apiserver|kube-controller-manager|kube-scheduler|kube-proxy|/pause|coredns/coredns|k8s-dns-node-cache|cluster-proportional-autoscaler|calico/node|calico/cni|calico/kube-controllers|local-path-provisioner|library/nginx'

# Пакеты, которые Kubespray ставит на узлы (роль system_packages, Ubuntu).
# 🔴 Проверять их наличие на узле, где Kubespray уже отработал, бесполезно:
# половину этого списка он туда и поставил. Набор считается относительно
# ЧИСТОГО образа, его состав снят в base-image-packages.txt.
APT_PKGS="${APT_PKGS:-apparmor apt-transport-https bash-completion conntrack curl e2fsprogs ebtables iproute2 iptables iputils-ping ipvsadm ipset libseccomp2 openssl python3-apt rsync socat software-properties-common tar unzip xfsprogs}"
# GitOps внутри контура: ArgoCD тянет манифесты из git-сервера, который тоже
# стоит внутри. Gitea выбрана вместо GitLab намеренно: у неё один образ и файл
# базы, а в закрытый контур каждую площадку тащить GitLab это отдельная работа.
ARGOCD_VERSION="${ARGOCD_VERSION:-v3.5.3}"
GITEA_VERSION="${GITEA_VERSION:-1.27.3}"
APT_SUITE="${APT_SUITE:-noble}"
APT_BASE="${APT_BASE:-http://archive.ubuntu.com/ubuntu}"

say() { printf '\n=== %s\n' "$*"; }
warn() { printf '⚠️  %s\n' "$*" >&2; }

trebuetsya_internet() {
  curl -sfI -m 15 https://dl.k8s.io >/dev/null 2>&1 ||
    { echo "нет выхода в интернет, а фаза сбора без него бессмысленна"; exit 1; }
}

# ─────────────────────────────── списки ───────────────────────────────
# generate_list.sh считает списки по переменным инвентаря. Прогоняем его
# дважды: без offline.yml он говорит, ОТКУДА образ брать, с offline.yml тем же
# порядком строк говорит, КАК он будет называться внутри контура. Строки
# соответствуют друг другу один в один, отсюда пары «источник -> цель».
sobrat_spiski() {
  say "списки файлов и образов"
  local tmp="/tmp/quarry-lists"
  rm -rf "$tmp"
  mkdir -p "$tmp/online/group_vars/k8s_cluster" "$tmp/offline/group_vars/k8s_cluster" "$tmp/offline/group_vars/all" "$LISTS"

  # Инвентарь-заглушка: живые узлы для расчёта списков не нужны, нужны
  # только переменные. Сбор поэтому можно делать до создания машин.
  local hosts="$tmp/online/hosts.yaml"
  cat > "$hosts" <<'EOF'
all:
  hosts:
    node1:
      ansible_host: 127.0.0.1
      ip: 127.0.0.1
      access_ip: 127.0.0.1
  children:
    kube_control_plane:
      hosts:
        node1:
    etcd:
      hosts:
        node1:
    kube_node:
      hosts:
        node1:
    k8s_cluster:
      children:
        kube_control_plane:
        kube_node:
    calico_rr:
      hosts: {}
EOF
  cp "$REPO/cluster/kubespray/profile.yml" "$tmp/online/group_vars/k8s_cluster/profile.yml"
  cp -r "$tmp/online/." "$tmp/offline/"
  REGISTRY_HOST="$REGISTRY_NAME" FILES_REPO="http://mirror.quarry.local:8080/files" \
    "$HERE/render-offline-vars.sh" > "$tmp/offline/group_vars/all/offline.yml"

  local cfg="$KUBESPRAY/ansible.cfg"
  for rezhim in online offline; do
    # generate_list.sh зовёт ansible-playbook из PATH, а он живёт в venv:
    # без этой строки скрипт падает на «ansible-playbook: command not found».
    PATH="$VENV/bin:$PATH" ANSIBLE_CONFIG="$cfg"       "$KUBESPRAY/contrib/offline/generate_list.sh" -i "$tmp/$rezhim/hosts.yaml" \
      > "$LISTS/generate-$rezhim.log" 2>&1
    cp "$KUBESPRAY/contrib/offline/temp/files.list" "$LISTS/files.$rezhim.list"
    cp "$KUBESPRAY/contrib/offline/temp/images.list" "$LISTS/images.$rezhim.list"
  done

  # Длины обязаны совпадать: на этом держится сопоставление пар.
  local a b
  a="$(wc -l < "$LISTS/images.online.list")"
  b="$(wc -l < "$LISTS/images.offline.list")"
  [ "$a" = "$b" ] || { echo "списки образов разной длины ($a и $b), пары не сходятся"; exit 1; }
  echo "файлов в списке: $(wc -l < "$LISTS/files.online.list"), образов: $a"
}

# ─────────────────────────────── файлы ───────────────────────────────
skachat_fayly() {
  say "файлы кластера"
  mkdir -p "$FILES"
  local vsego=0 skachano=0
  while read -r url; do
    [ -n "$url" ] || continue
    echo "$url" | grep -Eq "$NUZHNY_FILES" || continue
    vsego=$((vsego + 1))
    # files_repo ожидает путь вида <домен>/<путь>, ровно как в исходном адресе.
    local put="${url#https://}"
    local cel="$FILES/$put"
    if [ -s "$cel" ]; then
      echo "уже есть: $put"
      continue
    fi
    mkdir -p "$(dirname "$cel")"
    echo "качаю:   $put"
    curl -fL --retry 3 --progress-bar -o "$cel.chast" "$url"
    mv "$cel.chast" "$cel"
    skachano=$((skachano + 1))
  done < "$LISTS/files.online.list"
  echo "нужных файлов $vsego, скачано за этот раз $skachano"
  du -sh "$FILES"
}

# ─────────────────────────── бинарник реестра ───────────────────────────
skachat_reestr() {
  say "бинарник реестра для зеркала"
  mkdir -p "$BIN"
  if [ -x "$BIN/registry" ]; then
    echo "уже есть: $("$BIN/registry" --version 2>/dev/null || echo registry)"
    return
  fi
  curl -fL --retry 3 --progress-bar -o /tmp/registry.tar.gz "$REGISTRY_URL"
  tar -xzf /tmp/registry.tar.gz -C "$BIN" registry
  rm -f /tmp/registry.tar.gz
  chmod +x "$BIN/registry"
  echo "готов: $("$BIN/registry" --version 2>/dev/null || echo registry)"
}

# ─────────────────────────────── образы ───────────────────────────────
zalit_obrazy() {
  say "образы кластера"
  command -v skopeo >/dev/null || { echo "нужен skopeo"; exit 1; }

  # Реестр на время сбора поднимаем здесь же: складывать образы в каталог
  # хранилища напрямую нельзя, формат внутренний.
  #
  # ⚠️ Зеркало может быть уже поднято на адресе контура (после первого прогона
  # установки так и есть). Тогда второй экземпляр на 127.0.0.1 не поднимется,
  # порт занят, а skopeo будет стучаться не туда. Берём адрес у работающего.
  if [ -f "$ARTIFACTS/run/registry.yml" ] && kill -0 "$(cat "$ARTIFACTS/run/registry.pid" 2>/dev/null)" 2>/dev/null; then
    LOCAL_REGISTRY="$(awk '/addr:/{print $2}' "$ARTIFACTS/run/registry.yml")"
    echo "реестр уже работает на $LOCAL_REGISTRY, заливаю туда"
  else
    "$HERE/mirror.sh" start 127.0.0.1 >/dev/null
    trap '"$HERE/mirror.sh" stop >/dev/null 2>&1 || true' EXIT
  fi

  python3 - "$LISTS/images.online.list" "$LISTS/images.offline.list" "$NUZHNY_IMAGES" <<'PY' > /tmp/quarry-pary.txt
import re, sys
istochniki = [s.strip() for s in open(sys.argv[1], encoding='utf-8') if s.strip()]
celi = [s.strip() for s in open(sys.argv[2], encoding='utf-8') if s.strip()]
nuzhno = re.compile(sys.argv[3])
for src, dst in zip(istochniki, celi):
    if not nuzhno.search(src):
        continue
    # Отрезаем хост реестра: имя пути внутри реестра от адреса не зависит.
    put = dst.split('/', 1)[1] if '/' in dst else dst
    print(f"{src} {put}")
PY

  local vsego=0
  while read -r src put; do
    [ -n "$src" ] || continue
    vsego=$((vsego + 1))
    echo "--- $put"
    skopeo copy --retry-times 3 --dest-tls-verify=false \
      "docker://$src" "docker://$LOCAL_REGISTRY/$put" >/dev/null
  done < /tmp/quarry-pary.txt

  # local-path-provisioner тянет вспомогательный busybox мимо всех настроек
  # реестра: имя зашито в роли. Приносим его отдельно, а в offline.yml
  # переопределяем local_path_provisioner_helper_image_repo.
  echo "--- library/busybox:stable (вспомогательный образ local-path)"
  skopeo copy --retry-times 3 --dest-tls-verify=false \
    docker://docker.io/library/busybox:stable "docker://$LOCAL_REGISTRY/library/busybox:stable" >/dev/null
  vsego=$((vsego + 1))

  echo "образов в реестре: $vsego"
  curl -s "http://$LOCAL_REGISTRY/v2/_catalog" | head -c 2000
  echo
  du -sh "$REGDATA"
}

# ───────────────────────── репозиторий пакетов ─────────────────────────
# Плоский репозиторий: Packages + Release в одном каталоге, подключается
# строкой «deb [trusted=yes] http://зеркало/apt ./». Подписи нет намеренно:
# внутри контура источник доверенный, а ключи усложнили бы стенд без пользы.
sobrat_apt() {
  say "репозиторий пакетов"
  # Разрешение зависимостей, скачивание и индекс живут в apt_mirror.py:
  # рекурсия по Depends на bash выходит нечитаемой, а без рекурсии установка
  # падает на первом же пакете, которому чего-то не хватило.
  python3 "$HERE/apt_mirror.py"     --out "$APT"     --baseline "$HERE/base-image-packages.txt"     --suite "$APT_SUITE"     --base "$APT_BASE"     $APT_PKGS
  du -sh "$APT"
}

# ─────────────────────────── GitOps: ArgoCD и Gitea ───────────────────────────
# Образы кладём файлами рядом с образами карьера: внутрь контура они попадают
# тем же путём, через seed-registry.sh в реестр кластера. Зеркало на шлюзе
# нужно только для установки САМОГО кластера, дальше живём своим реестром.
sobrat_gitops() {
  say "GitOps: манифесты и образы"
  local vygruzka="$REPO/cluster/artifacts"
  mkdir -p "$ARTIFACTS/gitops"

  echo "--- манифест ArgoCD $ARGOCD_VERSION"
  curl -fL --retry 3 -s     "https://raw.githubusercontent.com/argoproj/argo-cd/$ARGOCD_VERSION/manifests/install.yaml"     -o "$ARTIFACTS/gitops/argocd-install.yaml"
  grep -cE '^\s+image:' "$ARTIFACTS/gitops/argocd-install.yaml" >/dev/null

  # Dex намеренно не приносим: он нужен для входа через внешний SSO, а на стенде
  # хватает локального администратора. Меньше образ, меньше подов, меньше слов
  # в объяснении, что тут вообще происходит.
  local -A OBRAZY=(
    ["mirror-argocd-$ARGOCD_VERSION.tar"]="quay.io/argoproj/argocd:$ARGOCD_VERSION"
    ["mirror-redis-8.2.3-alpine.tar"]="public.ecr.aws/docker/library/redis:8.2.3-alpine"
    ["mirror-gitea-$GITEA_VERSION.tar"]="docker.io/gitea/gitea:$GITEA_VERSION"
  )
  for fayl in "${!OBRAZY[@]}"; do
    if [ -s "$vygruzka/$fayl" ]; then
      echo "уже есть: $fayl"
      continue
    fi
    echo "--- ${OBRAZY[$fayl]}"
    skopeo copy --retry-times 3 "docker://${OBRAZY[$fayl]}" "docker-archive:$vygruzka/$fayl" >/dev/null
  done
  du -sh "$vygruzka"/mirror-argocd-* "$vygruzka"/mirror-gitea-* "$vygruzka"/mirror-redis-* 2>/dev/null
}

shag="${1:-все}"
case "$shag" in
  lists)  trebuetsya_internet; sobrat_spiski ;;
  files)  trebuetsya_internet; skachat_fayly ;;
  images) trebuetsya_internet; skachat_reestr; zalit_obrazy ;;
  apt)    trebuetsya_internet; sobrat_apt ;;
  gitops) trebuetsya_internet; sobrat_gitops ;;
  все|all)
    trebuetsya_internet
    sobrat_spiski
    skachat_fayly
    skachat_reestr
    zalit_obrazy
    sobrat_apt
    say "собрано"
    du -sh "$ARTIFACTS"
    echo
    echo "дальше: cluster/offline/mirror.sh start <адрес в контуре>"
    echo "        OFFLINE=1 cluster/kubespray/deploy.sh"
    ;;
  *) echo "неизвестный шаг: $shag"; exit 1 ;;
esac
