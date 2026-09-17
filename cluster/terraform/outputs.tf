output "nodes" {
  description = "Имена узлов и их адреса"
  # try на случай, когда машина выключена и адреса ещё нет: вывод не должен
  # ронять весь план.
  value = {
    for d in libvirt_domain.node : d.name => try(d.network_interface[0].addresses[0], "нет адреса")
  }
}

output "inventory" {
  description = "Готовый список адресов для Ansible и установки k3s"
  value = <<-EOT
    %{~for d in libvirt_domain.node~}
    ${d.name} ansible_host=${try(d.network_interface[0].addresses[0], "")}
    %{~endfor~}
  EOT
}
