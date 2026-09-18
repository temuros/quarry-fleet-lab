#!/usr/bin/env bash
# Установка сетевого хранилища в кластер.
#
# Порядок такой: сначала хранилище контура (nfs-server.sh), потом драйвер и
# класс. Драйвер сам по себе ничего не хранит, он только учит кластер
# выдавать тома с того, что уже стоит.
#
#   ./install.sh          поставить драйвер и класс, сделать его классом по умолчанию
#   ./install.sh check    проверить, что тома выдаются и переживают переезд
#
# Образы драйвера приносятся в контур как все остальные: они перечислены в
# cluster/scripts/images.map, заливает их seed-registry.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$REPO/cluster/kubespray/kubeconfig}"
HRANILISHCHE="${HRANILISHCHE:-192.168.200.1}"
# Для проверки берём образ, который ТОЧНО лежит в реестре кластера. busybox
# живёт в зеркале контура, но в реестр кластера не заезжает: он нужен только
# местному хранилищу local-path, а не нагрузке.
PROVERKA_OBRAZ="${PROVERKA_OBRAZ:-registry.quarry.local:5000/mirror/redis:8.2.3-alpine}"

say() { printf '\n=== %s\n' "$*"; }

postavit() {
  say "проверяю, что хранилище контура отвечает"
  # Если хранилище не поднято, драйвер установится и будет молча висеть:
  # тома останутся в ожидании, а причина будет не видна в кластере.
  showmount -e "$HRANILISHCHE" 2>/dev/null | tail -2 ||
    { echo "хранилище $HRANILISHCHE не отвечает, подними: cluster/storage/nfs-server.sh start" >&2; exit 1; }

  say "драйвер CSI"
  kubectl apply -f "$HERE/csi-nfs/" >/dev/null
  kubectl -n kube-system rollout status deploy/csi-nfs-controller --timeout=300s
  kubectl -n kube-system rollout status ds/csi-nfs-node --timeout=300s

  say "класс хранилища"
  # local-path остаётся в кластере, но перестаёт быть классом по умолчанию:
  # иначе новые тома продолжат появляться на дисках узлов, и переезд подов
  # снова окажется невозможным там, где про это забыли.
  kubectl apply -f "$HERE/storageclass.yaml" >/dev/null
  kubectl patch storageclass local-path \
    -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}' >/dev/null 2>&1 || true

  kubectl get storageclass
}

# Проверка не «том создался», а «том пережил переезд пода на другой узел»:
# ради этого хранилище и заводили.
proverka() {
  local ns=quarry-proverka
  say "создаю том и пишу в него на одном узле"
  kubectl delete ns "$ns" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  kubectl create ns "$ns" >/dev/null
  cat <<'YAML' | sed "s/NAMESPACE/$ns/" | kubectl apply -f - >/dev/null
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: proverka
  namespace: NAMESPACE
spec:
  accessModes: ["ReadWriteMany"]
  resources:
    requests:
      storage: 1Gi
YAML

  local uzly uzel1 uzel2
  mapfile -t uzly < <(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
  uzel1="${uzly[0]}"; uzel2="${uzly[1]:-${uzly[0]}}"

  zapustit_pod "$ns" "$uzel1" 'echo "запись с '"$uzel1"' $(date -Iseconds)" >> /data/zhurnal.txt; sleep 5'
  kubectl -n "$ns" wait --for=condition=Ready pod/proverka --timeout=180s >/dev/null
  kubectl -n "$ns" logs proverka >/dev/null 2>&1 || true
  kubectl -n "$ns" delete pod proverka --wait=true >/dev/null

  say "запускаю на другом узле ($uzel2) и читаю то же самое"
  zapustit_pod "$ns" "$uzel2" 'cat /data/zhurnal.txt; echo "прочитано с '"$uzel2"'"; sleep 5'
  kubectl -n "$ns" wait --for=condition=Ready pod/proverka --timeout=180s >/dev/null
  kubectl -n "$ns" logs proverka

  kubectl delete ns "$ns" --wait=false >/dev/null
  say "том переехал между узлами вместе с данными"
}

# ⚠️ Команда передаётся блоком, а не строкой в квадратных скобках: любые
# кавычки внутри ломают YAML, и ошибка приходит невнятная («did not find
# expected ','»), хотя дело не в списке, а в экранировании.
zapustit_pod() {
  local ns="$1" uzel="$2" cmd="$3"
  {
    cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: proverka
  namespace: $ns
spec:
  nodeName: $uzel
  restartPolicy: Never
  containers:
    - name: proverka
      image: $PROVERKA_OBRAZ
      command: ["sh", "-c"]
      args:
        - |
YAML
    printf '          %s\n' "$cmd"
    cat <<YAML
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: proverka
YAML
  } | kubectl apply -f - >/dev/null
}

case "${1:-postavit}" in
  postavit|"") postavit ;;
  check)       proverka ;;
  *) echo "команды: postavit | check"; exit 1 ;;
esac
