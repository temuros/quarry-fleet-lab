#!/usr/bin/env bash
# Доставка образов внутрь контура.
#
# Так это выглядит на площадке заказчика: собрали снаружи, выгрузили в файл,
# файл прошёл проверку службы безопасности и приехал внутрь, там его положили
# в свой реестр. Никакого GitLab внутри и никакого выхода в интернет.
#
#   ./deliver-images.sh            собрать, выгрузить в файл, положить в реестр
#   SKIP_BUILD=1 ./deliver-images.sh   только доставка уже выгруженных файлов
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TF_DIR="${TF_DIR:-$REPO/cluster/terraform}"
ARTIFACTS="${ARTIFACTS:-$REPO/cluster/artifacts}"
SKIP_BUILD="${SKIP_BUILD:-0}"
# docker.exe: сборка идёт снаружи контура, на машине инженера
DOCKER="${DOCKER:-docker.exe}"

say() { printf '\n=== %s\n' "$*"; }

mkdir -p "$ARTIFACTS"

say "адрес реестра внутри контура"
NODE_IP="$(TF_DATA_DIR="${TF_DATA_DIR:-/root/.tf-quarry}" terraform -chdir="$TF_DIR" output -json nodes |
  python3 -c 'import json,sys; print(sorted(json.load(sys.stdin).items())[0][1])')"
REGISTRY="$NODE_IP:30500"
echo "$REGISTRY"

if [ "$SKIP_BUILD" != "1" ]; then
  say "сборка образов снаружи контура"
  # Контекст сборки это корень репозитория: кодек EGTS общий у борта и шлюза,
  # две копии одного протокола расходятся молча.
  # ⚠️ docker.exe это windows-процесс: путь к Dockerfile он ищет от СВОЕГО
  # текущего каталога, а не от каталога, из которого мы зовём его в WSL.
  # Относительный путь молча превращается в «файл не найден».
  KORNEN="$(wslpath -w "$REPO")"
  for obraz in sim collector egts; do
    "$DOCKER" build -q -f "$(wslpath -w "$REPO/$obraz/Dockerfile")"       -t "quarry/$obraz:local" "$KORNEN"
  done

  say "выгрузка в файлы"
  "$DOCKER" save quarry/sim:local -o "$(wslpath -w "$ARTIFACTS/sim.tar")"
  "$DOCKER" save quarry/collector:local -o "$(wslpath -w "$ARTIFACTS/collector.tar")"
  "$DOCKER" save quarry/egts:local -o "$(wslpath -w "$ARTIFACTS/egts.tar")"
fi
ls -lh "$ARTIFACTS"/*.tar

say "перенос файлов в реестр внутри контура"
for name in sim collector egts; do
  skopeo copy --dest-tls-verify=false \
    "docker-archive:$ARTIFACTS/$name.tar" \
    "docker://$REGISTRY/quarry/$name:local"
done

say "что лежит в реестре"
curl -s "http://$REGISTRY/v2/_catalog"
echo
for name in sim collector egts; do
  printf 'quarry/%s: ' "$name"
  curl -s "http://$REGISTRY/v2/quarry/$name/tags/list"
  echo
done
