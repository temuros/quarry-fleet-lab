#!/usr/bin/env bash
# Печатает offline.yml для Kubespray: откуда брать файлы и образы внутри контура.
#
# Один и тот же файл нужен дважды и в разных местах, поэтому он собирается
# здесь, а не в каждом скрипте отдельно:
#   collect.sh  чтобы узнать, КАК образы будут называться в нашем реестре
#                 (второй прогон generate_list.sh с этими переменными);
#   deploy.sh   чтобы установка брала всё из зеркала, а не из интернета.
#
#   REGISTRY_HOST=192.168.200.1:5000 FILES_REPO=http://192.168.200.1:8080/files ./render-offline-vars.sh
set -euo pipefail

REGISTRY_HOST="${REGISTRY_HOST:?нужен адрес реестра внутри контура}"
FILES_REPO="${FILES_REPO:?нужен адрес файлового зеркала}"

cat <<EOF
# Файл собирается cluster/offline/render-offline-vars.sh, править руками нет смысла.
#
# Всё, что Kubespray обычно тянет из интернета, берётся внутри контура:
# образы из своего реестра, файлы с файлового зеркала.
registry_host: "$REGISTRY_HOST"
files_repo: "$FILES_REPO"

kube_image_repo: "{{ registry_host }}"
gcr_image_repo: "{{ registry_host }}"
github_image_repo: "{{ registry_host }}"
docker_image_repo: "{{ registry_host }}"
quay_image_repo: "{{ registry_host }}"

# 🔴 local-path-provisioner тянет вспомогательный busybox мимо всех *_image_repo:
# имя зашито в роли строкой "busybox". Без этой строки тома не создаются вообще,
# а причина выглядит как «под Pending без объяснений».
local_path_provisioner_helper_image_repo: "{{ registry_host }}/library/busybox"
local_path_provisioner_helper_image_tag: "stable"

# Реестр без TLS описывается не здесь, а в containerd_registries_mirrors
# (см. cluster/kubespray/deploy.sh): в Kubespray 2.31 это единственный
# механизм, containerd_insecure_registries там уже нет.

# Файлы. Пути повторяют исходные адреса: зеркало раздаёт дерево вида
# <домен>/<путь>, ровно как оно лежит у dl.k8s.io и GitHub.
kubelet_download_url: "{{ files_repo }}/dl.k8s.io/release/v{{ kube_version }}/bin/linux/{{ image_arch }}/kubelet"
kubectl_download_url: "{{ files_repo }}/dl.k8s.io/release/v{{ kube_version }}/bin/linux/{{ image_arch }}/kubectl"
kubeadm_download_url: "{{ files_repo }}/dl.k8s.io/release/v{{ kube_version }}/bin/linux/{{ image_arch }}/kubeadm"
etcd_download_url: "{{ files_repo }}/github.com/etcd-io/etcd/releases/download/v{{ etcd_version }}/etcd-v{{ etcd_version }}-linux-{{ image_arch }}.tar.gz"
cni_download_url: "{{ files_repo }}/github.com/containernetworking/plugins/releases/download/v{{ cni_version }}/cni-plugins-linux-{{ image_arch }}-v{{ cni_version }}.tgz"
crictl_download_url: "{{ files_repo }}/github.com/kubernetes-sigs/cri-tools/releases/download/v{{ crictl_version }}/crictl-v{{ crictl_version }}-{{ ansible_system | lower }}-{{ image_arch }}.tar.gz"
runc_download_url: "{{ files_repo }}/github.com/opencontainers/runc/releases/download/v{{ runc_version }}/runc.{{ image_arch }}"
containerd_download_url: "{{ files_repo }}/github.com/containerd/containerd/releases/download/v{{ containerd_version }}/containerd-{{ containerd_version }}-linux-{{ image_arch }}.tar.gz"
nerdctl_download_url: "{{ files_repo }}/github.com/containerd/nerdctl/releases/download/v{{ nerdctl_version }}/nerdctl-{{ nerdctl_version }}-{{ ansible_system | lower }}-{{ image_arch }}.tar.gz"
calicoctl_download_url: "{{ files_repo }}/github.com/projectcalico/calico/releases/download/v{{ calico_ctl_version }}/calicoctl-linux-{{ image_arch }}"
calico_crds_download_url: "{{ files_repo }}/github.com/projectcalico/calico/raw/v{{ calico_version }}/manifests/crds.yaml"
EOF
