# Три машины под кластер, создаются из кода.
#
# Провайдер libvirt выбран намеренно: на площадке заказчика нет облака,
# там стоит гипервизор или голое железо. Тот же код без переписывания
# ложится на Proxmox или на любой KVM у заказчика, в отличие от кода,
# завязанного на конкретное облако.

terraform {
  required_version = ">= 1.5"
  required_providers {
    libvirt = {
      source = "dmacvicar/libvirt"
      # 0.9 это полностью переписанный провайдер с сырым XML вместо блоков.
      # Держимся documented-ветки 0.8, её же используют на площадках.
      version = "~> 0.8.0"
    }
  }
}

provider "libvirt" {
  uri = var.libvirt_uri
}

# Базовый образ качается один раз и служит основой для дисков узлов.
resource "libvirt_volume" "base" {
  name   = "${var.prefix}-base.qcow2"
  pool   = var.pool
  source = var.base_image
  format = "qcow2"
}

resource "libvirt_volume" "node" {
  count          = var.node_count
  name           = "${var.prefix}-${count.index + 1}.qcow2"
  pool           = var.pool
  base_volume_id = libvirt_volume.base.id
  size           = var.disk_gb * 1024 * 1024 * 1024
  format         = "qcow2"
}

resource "libvirt_cloudinit_disk" "init" {
  count = var.node_count
  name  = "${var.prefix}-${count.index + 1}-init.iso"
  pool  = var.pool

  user_data = templatefile("${path.module}/cloud-init/user-data.yaml.tftpl", {
    hostname = "${var.prefix}-${count.index + 1}"
    ssh_key  = trimspace(file(var.ssh_public_key))
  })
}

resource "libvirt_domain" "node" {
  count     = var.node_count
  name      = "${var.prefix}-${count.index + 1}"
  memory    = var.memory_mb
  vcpu      = var.vcpu
  # Узлы должны подниматься сами вместе с гипервизором: на площадке
  # после отключения питания никто не заходит включать их руками.
  autostart = true

  cloudinit = libvirt_cloudinit_disk.init[count.index].id

  network_interface {
    network_name   = var.network
    hostname       = "${var.prefix}-${count.index + 1}"
    wait_for_lease = true
  }

  disk {
    volume_id = libvirt_volume.node[count.index].id
  }

  # Консоль нужна, когда машина не поднялась и SSH ещё нет: ровно тот
  # случай, который на площадке разбирают через IPMI.
  console {
    type        = "pty"
    target_port = "0"
    target_type = "serial"
  }

  graphics {
    type        = "spice"
    listen_type = "address"
    autoport    = true
  }
}
