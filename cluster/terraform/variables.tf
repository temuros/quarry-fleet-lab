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
  description = "Ядер на узел"
  type        = number
  default     = 2
}

variable "memory_mb" {
  description = "Памяти на узел, МБ"
  type        = number
  default     = 3072
}

variable "disk_gb" {
  description = "Диск узла, ГБ"
  type        = number
  default     = 20
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
