variable "libvirt_uri" {
  description = "Куда подключается провайдер. На площадке заказчика это адрес их гипервизора."
  type        = string
  default     = "qemu:///system"
}

variable "prefix" {
  description = "Приставка в именах машин и дисков"
  type        = string
  default     = "quarry"
}

variable "node_count" {
  description = "Сколько узлов в кластере"
  type        = number
  default     = 3
}

variable "vcpu" {
  # 🔴 Три, а не два. С двумя ядрами узел вмещал нагрузку, пока управляющая
  # часть стояла на одной машине. Когда управляющих узлов стало три, только
  # apiserver, controller-manager и scheduler занимают около 550 мЦПУ на
  # каждом узле, и свободной ёмкости не осталось: при потере узла его подам
  # некуда переезжать, планировщик честно отвечает «Insufficient cpu».
  # Отказоустойчивость это не только копии данных, но и запас, куда переехать.
  description = "Ядер на узел"
  type        = number
  default     = 3
}

variable "memory_mb" {
  description = "Памяти на узел, МБ. Kubespray ставит обычный kubeadm-кластер, ему нужно больше, чем k3s."
  type        = number
  default     = 4096
}

variable "disk_gb" {
  description = "Диск узла, ГБ"
  type        = number
  default     = 25
}

variable "pool" {
  description = "Хранилище libvirt"
  type        = string
  default     = "default"
}

variable "network" {
  description = "Сеть libvirt"
  type        = string
  default     = "default"
}

variable "base_image" {
  description = "Образ системы. Скачивается заранее: в закрытом контуре интернета на узлах нет."
  type        = string
  default     = "/var/lib/libvirt/images/noble-server-cloudimg-amd64.img"
}

variable "ssh_public_key" {
  description = "Открытый ключ, который кладётся на узлы"
  type        = string
  default     = "/root/.ssh/quarry-lab.pub"
}

variable "isolated" {
  # 🔴 По умолчанию ДА, и это не вкусовщина. Стенд создавался с
  # `-var isolated=true`, а значение по умолчанию оставалось false, поэтому
  # любой `terraform plan` без этого ключа показывал «уничтожить сеть и
  # пересоздать машины»: сеть уходила в count = 0, а cloud-init менялся, потому
  # что адрес зеркала внутри контура подставляется в него. Один apply без
  # нужного ключа снёс бы работающий кластер целиком.
  #
  # Значение по умолчанию должно совпадать с тем, как систему действительно
  # запускают, иначе код перестаёт описывать стенд и начинает описывать
  # намерение.
  description = "Держать узлы в сети без выхода наружу. Так выглядит контур заказчика."
  type        = bool
  default     = true
}

variable "mirror_ip" {
  description = <<-EOT
    Адрес зеркала внутри контура: шлюз изолированной сети, он же управляющая
    машина. Оттуда узлы берут пакеты, файлы кластера и образы. Снаружи контура
    за этим адресом ничего нет.
  EOT
  type        = string
  default     = "192.168.200.1"
}
