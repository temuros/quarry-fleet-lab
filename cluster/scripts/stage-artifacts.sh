#!/usr/bin/env bash
# Всё, что понадобится внутри контура, скачивается заранее и снаружи.
#
# Это ровно та работа, которую на площадке делают до выезда: внутри
# интернета не будет, и докачать оттуда ничего нельзя.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ARTIFACTS="${ARTIFACTS:-$REPO/cluster/artifacts}"
K3S_VERSION="${K3S_VERSION:-v1.36.4+k3s1}"
DOCKER="${DOCKER:-docker.exe}"

# Образы, которые кластеру нужны, но которые не наши. Внутри контура они
# тоже должны лежать в своём реестре, иначе узел пойдёт в интернет.
MIRROR_IMAGES=(
  "registry:2"
  "apache/kafka:3.8.1"
  "prom/prometheus:v2.55.1"
  "grafana/grafana:11.3.0"
  "postgres:16-alpine"
)

say() { printf '\n=== %s\n' "$*"; }
mkdir -p "$ARTIFACTS"

say "k3s ${K3S_VERSION}"
enc="${K3S_VERSION/+/%2B}"
base="https://github.com/k3s-io/k3s/releases/download/${enc}"
if [ ! -f "$ARTIFACTS/k3s" ]; then
  curl -fsSL -o "$ARTIFACTS/k3s" "$base/k3s"
fi
if [ ! -f "$ARTIFACTS/k3s-airgap-images-amd64.tar" ]; then
  curl -fsSL -o "$ARTIFACTS/k3s-airgap-images-amd64.tar.zst" "$base/k3s-airgap-images-amd64.tar.zst"
  zstd -qdf "$ARTIFACTS/k3s-airgap-images-amd64.tar.zst" -o "$ARTIFACTS/k3s-airgap-images-amd64.tar"
  rm -f "$ARTIFACTS/k3s-airgap-images-amd64.tar.zst"
fi
if [ ! -f "$ARTIFACTS/k3s-install.sh" ]; then
  curl -fsSL -o "$ARTIFACTS/k3s-install.sh" https://get.k3s.io
fi
chmod +x "$ARTIFACTS/k3s" "$ARTIFACTS/k3s-install.sh"

say "чужие образы, которые поедут в наш реестр"
for image in "${MIRROR_IMAGES[@]}"; do
  file="$ARTIFACTS/mirror-$(echo "$image" | tr '/:' '--').tar"
  if [ -f "$file" ]; then
    echo "уже есть: $image"
    continue
  fi
  echo "тяну $image"
  "$DOCKER" pull -q "$image" >/dev/null
  "$DOCKER" save "$image" -o "$(wslpath -w "$file")"
done

say "что приготовлено"
ls -lh "$ARTIFACTS"
echo
echo "итого: $(du -sh "$ARTIFACTS" | cut -f1)"
