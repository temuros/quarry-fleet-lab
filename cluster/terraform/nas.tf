# Хранилище контура: пара машин вместо одной.
#
# Сетевое хранилище развязало данные и узлы кластера: под с журналом смен
# переезжает на любую машину. Но само хранилище осталось в одном экземпляре,
# и оно же стало последней единственной точкой отказа: его потеря
# останавливает не один под, а все базы сразу.
#
# Здесь две машины с репликацией диска между ними и общим адресом, который
# живёт на той, что сейчас обслуживает. Для кластера ничего не меняется: он
# как ходил на один адрес, так и ходит, а какая из машин за ним стоит,
# его не касается.
#
# ⚠️ Машины намеренно скромные: хранилищу нужен диск и сеть, а не процессор.
# Одно ядро и гигабайт памяти это осознанный минимум, чтобы стенд помещался
# на один ноутбук рядом с тремя узлами кластера.

variable "nas_count" {
  description = "Машин хранилища. Две это минимум, при котором отказ одной не останавливает работу."
  type        = number
  default     = 2
}

variable "nas_vcpu" {
  description = "Ядер на машину хранилища"
  type        = number
  default     = 1
}

variable "nas_memory_mb" {
  description = "Памяти на машину хранилища, МБ"
  type        = number
  default     = 1024
}

variable "nas_data_gb" {
  description = "Размер диска под данные. Реплицируется целиком, поэтому лишнего не берём."
  type        = number
  default     = 8
}

resource "libvirt_volume" "nas_system" {
  count          = var.nas_count
  name           = "${var.prefix}-nas-${count.index + 1}.qcow2"
  pool           = var.pool
  base_volume_id = libvirt_volume.base.id
  size           = 12 * 1024 * 1024 * 1024
  format         = "qcow2"
}

# Данные лежат на ОТДЕЛЬНОМ диске, а не в системном разделе. Так реплика
# копирует только полезное, а переустановка системы не трогает данные:
# ровно то же разделение, что на массиве в стойке.
resource "libvirt_volume" "nas_data" {
  count  = var.nas_count
  name   = "${var.prefix}-nas-${count.index + 1}-data.qcow2"
  pool   = var.pool
  size   = var.nas_data_gb * 1024 * 1024 * 1024
  format = "qcow2"
}

resource "libvirt_cloudinit_disk" "nas_init" {
  count = var.nas_count
  name  = "${var.prefix}-nas-${count.index + 1}-init.iso"
  pool  = var.pool

  user_data = templatefile("${path.module}/cloud-init/user-data.yaml.tftpl", {
    hostname   = "${var.prefix}-nas-${count.index + 1}"
    ssh_key    = trimspace(file(var.ssh_public_key))
    apt_mirror = var.isolated ? "http://${var.mirror_ip}:8080/apt" : ""
  })
}

resource "libvirt_domain" "nas" {
  count     = var.nas_count
  name      = "${var.prefix}-nas-${count.index + 1}"
  memory    = var.nas_memory_mb
  vcpu      = var.nas_vcpu
  autostart = true

  cloudinit = libvirt_cloudinit_disk.nas_init[count.index].id

  cpu {
    mode = "host-passthrough"
  }

  network_interface {
    network_name   = var.isolated ? libvirt_network.isolated[0].name : var.network
    hostname       = "${var.prefix}-nas-${count.index + 1}"
    wait_for_lease = true
  }

  disk {
    volume_id = libvirt_volume.nas_system[count.index].id
  }

  disk {
    volume_id = libvirt_volume.nas_data[count.index].id
  }

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

output "nas" {
  description = "Машины хранилища и их адреса"
  value = {
    for d in libvirt_domain.nas : d.name => try(d.network_interface[0].addresses[0], "нет адреса")
  }
}
