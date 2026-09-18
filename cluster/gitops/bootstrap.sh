#!/usr/bin/env bash
# GitOps внутри закрытого контура: git-сервер и ArgoCD, который из него читает.
#
# Зачем это после рабочих скриптов выката: скрипт показывает, что кто-то его
# запустил, а не что в кластере сейчас. GitOps переворачивает: желаемое
# состояние лежит в репозитории ВНУТРИ контура, ArgoCD сверяет с ним кластер
# и чинит расхождения сам. На площадке, куда инженер попадает по пропуску,
# это разница между «приезжать на каждый выкат» и «прислать изменение».
#
#   ./bootstrap.sh           поднять git-сервер, залить манифесты, поставить ArgoCD
#   ./bootstrap.sh push      залить текущие манифесты в репозиторий контура
#   ./bootstrap.sh status    что сейчас видит ArgoCD
#
# Всё нужное должно быть в реестре контура: cluster/offline/collect.sh gitops
# приносит образы, cluster/scripts/seed-registry.sh кладёт их в реестр.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ARTIFACTS="${ARTIFACTS:-$REPO/cluster/artifacts}"
export KUBECONFIG="${KUBECONFIG:-$REPO/cluster/kubespray/kubeconfig}"
ARGOCD_MANIFEST="${ARGOCD_MANIFEST:-$ARTIFACTS/offline/gitops/argocd-install.yaml}"
REGISTRY_NAME="${REGISTRY_NAME:-registry.quarry.local:5000}"
PAROL_FAYL="${PAROL_FAYL:-$ARTIFACTS/gitea-password}"
GIT_USER="${GIT_USER:-quarry}"
REPO_NAME="${REPO_NAME:-quarry-apps}"

say() { printf '\n=== %s\n' "$*"; }

uzel_ip() {
  kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}'
}

parol() {
  if [ ! -s "$PAROL_FAYL" ]; then
    mkdir -p "$(dirname "$PAROL_FAYL")"
    # Пароль генерируется на месте и не уезжает в репозиторий: в git лежит
    # код, а не доступы.
    openssl rand -hex 12 > "$PAROL_FAYL"
    chmod 600 "$PAROL_FAYL"
  fi
  cat "$PAROL_FAYL"
}

podnyat_gitea() {
  say "git-сервер внутри контура"
  kubectl apply -f "$HERE/gitea.yaml" >/dev/null
  kubectl -n quarry-infra rollout status sts/gitea --timeout=300s

  local p; p="$(parol)"
  # ⚠️ kubectl exec заходит в контейнер root'ом, а Gitea от root работать
  # отказывается («is not supposed to be run as root») и сразу падает. Свои
  # команды она должна выполнять от пользователя git: в образе есть su-exec.
  gitea_cmd() { kubectl -n quarry-infra exec sts/gitea -- su-exec git gitea "$@"; }

  # Заведение пользователя идемпотентно: второй запуск не должен падать.
  if gitea_cmd admin user list 2>/dev/null | grep -qw "$GIT_USER"; then
    echo "пользователь $GIT_USER уже есть"
  else
    gitea_cmd admin user create \
      --username "$GIT_USER" --password "$p" --email "$GIT_USER@quarry.local" \
      --admin --must-change-password=false >/dev/null
    echo "пользователь $GIT_USER заведён"
  fi

  local url; url="http://$(uzel_ip):30030"
  if curl -sf -u "$GIT_USER:$p" "$url/api/v1/repos/$GIT_USER/$REPO_NAME" >/dev/null 2>&1; then
    echo "репозиторий $REPO_NAME уже есть"
  else
    # Репозиторий открыт на чтение: контур и так закрытый, а пароль в
    # настройках ArgoCD это лишняя вещь, которую потом некому менять.
    curl -sf -u "$GIT_USER:$p" -X POST "$url/api/v1/user/repos" \
      -H 'Content-Type: application/json' \
      -d "{\"name\":\"$REPO_NAME\",\"private\":false,\"auto_init\":true,\"default_branch\":\"main\"}" >/dev/null
    echo "репозиторий $REPO_NAME создан"
  fi
}

zalit_manifesty() {
  say "манифесты карьера в репозиторий контура"
  local p url rabochaya
  p="$(parol)"
  url="http://$GIT_USER:$p@$(uzel_ip):30030/$GIT_USER/$REPO_NAME.git"
  rabochaya="$(mktemp -d)"

  git -c http.sslVerify=false clone -q "$url" "$rabochaya"
  mkdir -p "$rabochaya/apps"
  rm -f "$rabochaya/apps"/*.yaml

  # Реестр в GitOps не отдаём намеренно: он фундамент, из которого ArgoCD и
  # тянет собственные образы. Сносить его синхронизацией нельзя.
  for fayl in "$REPO"/cluster/apps/*.yaml; do
    case "$(basename "$fayl")" in
      10-registry.yaml) continue ;;
    esac
    cp "$fayl" "$rabochaya/apps/"
  done

  # Дашборды лежат файлами и раньше превращались в ConfigMap командой выката.
  # В GitOps всё желаемое состояние должно быть в репозитории, иначе ArgoCD
  # будет считать кластер расходящимся или снесёт то, чего не знает.
  kubectl create configmap grafana-dashboard-quarry -n quarry \
    --from-file="$REPO/grafana/dashboards/" \
    --dry-run=client -o yaml > "$rabochaya/apps/45-dashboards.yaml"

  # Правила тревог лежат файлом рядом с конфигурацией Prometheus и едут в
  # кластер тем же путём, что и дашборды: в репозитории должно быть всё
  # желаемое состояние, иначе ArgoCD снесёт то, чего не знает.
  kubectl create configmap prometheus-rules -n quarry \
    --from-file="$REPO/prometheus/alerts.yml" \
    --dry-run=client -o yaml > "$rabochaya/apps/46-rules.yaml"

  # Prometheus не следит за файлом правил: обновлённый ConfigMap доезжает до
  # пода, а в памяти остаются прежние правила. Снаружи это выглядит хуже
  # обычной поломки: ArgoCD показывает Synced, файл на месте, поведение
  # старое. Поэтому сумма правил едет в шаблон пода: меняются правила -
  # меняется шаблон, и ArgoCD пересоздаёт под сам, без ручного reload.
  local summa
  summa="$(sha256sum "$REPO/prometheus/alerts.yml" | cut -c1-12)"
  sed -i "s/podstavlyaetsya-pri-vykate/$summa/" "$rabochaya/apps/40-monitoring.yaml"

  cat > "$rabochaya/README.md" <<'EOF'
# Манифесты карьера

Этот репозиторий живёт ВНУТРИ закрытого контура и является источником правды
для кластера: ArgoCD сверяет с ним состояние и приводит кластер к описанному.

Менять кластер руками через `kubectl apply` больше не нужно: изменение едет
сюда, ArgoCD применяет его сам.
EOF

  git -C "$rabochaya" add -A
  if git -C "$rabochaya" diff --cached --quiet; then
    echo "изменений нет"
  else
    git -C "$rabochaya" -c user.email="$GIT_USER@quarry.local" -c user.name="$GIT_USER" \
      commit -q -m "Манифесты карьера: состояние на $(date '+%d.%m.%Y %H:%M')"
    git -C "$rabochaya" push -q origin main
    echo "залито в $REPO_NAME"
    # ⚠️ Сам по себе ArgoCD опрашивает репозиторий раз в три минуты, и сразу
    # после push он честно показывает Synced: просто про новый коммит он ещё
    # не знает. Просим перечитать, иначе выкат выглядит как «ничего не
    # произошло» и начинается поиск несуществующей ошибки.
    kubectl -n argocd annotate app quarry argocd.argoproj.io/refresh=hard       --overwrite >/dev/null 2>&1 || true
  fi
  rm -rf "$rabochaya"
}

postavit_argocd() {
  say "ArgoCD"
  [ -s "$ARGOCD_MANIFEST" ] || { echo "нет манифеста ArgoCD, сначала collect.sh gitops"; exit 1; }
  kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f - >/dev/null

  local gotovyy="/tmp/argocd-quarry.yaml"
  python3 - "$ARGOCD_MANIFEST" "$gotovyy" "$REGISTRY_NAME" <<'PY'
import re, sys
ishodnyy, cel, reestr = sys.argv[1], sys.argv[2], sys.argv[3]
tekst = open(ishodnyy, encoding='utf-8').read()

# Образы переписываем на реестр контура: узлы в интернет не ходят,
# а ArgoCD в манифесте по умолчанию тянет их с quay.io и public.ecr.aws.
tekst = tekst.replace('quay.io/argoproj/argocd:', f'{reestr}/mirror/argocd:')
tekst = re.sub(r'public\.ecr\.aws/docker/library/redis:', f'{reestr}/mirror/redis:', tekst)

# Dex выбрасываем целиком: он нужен для входа через внешний SSO, которого в
# закрытом контуре стенда нет. Оставить его значит получить под, вечно
# висящий в ImagePullBackOff, и объяснять потом, что «так и надо».
# ⚠️ Фильтровать по вхождению строки «argocd-dex-server» нельзя: сам
# argocd-server ссылается на dex в своих аргументах, и его тоже вырезает.
# Смотрим именно на имя объекта.
imya_dex = re.compile(r'^  name: argocd-dex-server$', re.M)
ostavleno = [d for d in tekst.split('\n---\n') if not imya_dex.search(d)]
open(cel, 'w', encoding='utf-8').write('\n---\n'.join(ostavleno))
print(f"документов в манифесте: {len(ostavleno)}")
PY

  # 🔴 Обычный apply на CRD ArgoCD падает: «metadata.annotations: Too long».
  # kubectl складывает прошлую конфигурацию в аннотацию, а описание
  # ApplicationSet в неё не влезает (предел 262144 байта). Применение на
  # стороне сервера аннотацию не пишет вовсе.
  kubectl apply -n argocd --server-side --force-conflicts -f "$gotovyy" >/dev/null
  # Вход в ArgoCD снаружи кластера: сервис по умолчанию виден только внутри.
  kubectl -n argocd patch svc argocd-server -p \
    '{"spec":{"type":"NodePort","ports":[{"name":"http","port":80,"targetPort":8080,"nodePort":30080},{"name":"https","port":443,"targetPort":8080,"nodePort":30443}]}}' >/dev/null
  # Локальный вход без внешнего SSO: dex мы не ставили.
  kubectl -n argocd patch cm argocd-cm --type merge -p '{"data":{"dex.config":""}}' >/dev/null 2>&1 || true

  kubectl -n argocd rollout status deploy/argocd-repo-server --timeout=420s
  kubectl -n argocd rollout status deploy/argocd-server --timeout=420s
  kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout=420s
}

sozdat_prilozhenie() {
  say "приложение, которое ArgoCD держит в нужном состоянии"
  # Внутри кластера ходим по имени службы, а не по адресу узла: адрес машины
  # меняется при пересоздании, имя службы нет.
  local git_url="http://gitea.quarry-infra.svc.cluster.local:3000/$GIT_USER/$REPO_NAME.git"
  kubectl apply -f - <<EOF >/dev/null
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: quarry
  namespace: argocd
spec:
  project: default
  source:
    repoURL: $git_url
    targetRevision: main
    path: apps
  destination:
    server: https://kubernetes.default.svc
    namespace: quarry
  syncPolicy:
    automated:
      # Правку, сделанную руками в кластере, ArgoCD откатит к тому, что
      # записано в репозитории: иначе «источник правды» это просто слова.
      selfHeal: true
      # Удалённое из репозитория удаляется и из кластера.
      prune: true
    syncOptions:
      - CreateNamespace=true
EOF
  echo "приложение quarry создано"
}

sostoyanie() {
  say "состояние"
  local ip; ip="$(uzel_ip)"
  kubectl -n argocd get applications.argoproj.io quarry \
    -o custom-columns='ПРИЛОЖЕНИЕ:.metadata.name,СИНХРОН:.status.sync.status,ЗДОРОВЬЕ:.status.health.status' 2>/dev/null || true
  echo
  echo "ArgoCD:  http://$ip:30080   admin / $(kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || echo '(пароль уже сменён)')"
  echo "Gitea:   http://$ip:30030   $GIT_USER / $(parol)"
}

case "${1:-все}" in
  push) zalit_manifesty ;;
  status) sostoyanie ;;
  все|all)
    podnyat_gitea
    zalit_manifesty
    postavit_argocd
    sozdat_prilozhenie
    sostoyanie
    ;;
  *) echo "команды: (без аргумента) | push | status"; exit 1 ;;
esac
