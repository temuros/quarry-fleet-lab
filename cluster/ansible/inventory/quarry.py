#!/usr/bin/env python3
"""Инвентарь стенда из вывода Terraform.

Список машин не записан ни в одном файле руками: он берётся оттуда же,
откуда берут его terraform и kubespray. Машину пересоздали, адрес сменился,
инвентарь знает об этом сразу, и не остаётся второго места, где тот же факт
записан по-другому.

⚠️ Состояние terraform лежит вне репозитория (TF_DATA_DIR), поэтому запускать
это нужно тем же пользователем, от которого поднимался стенд.

Группы:
  uzly          узлы кластера
  hranilishche  пара машин хранилища
  stend         всё вместе
"""

import json
import os
import subprocess
import sys

KORNI = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TF_DIR = os.environ.get("TF_DIR", os.path.join(KORNI, "terraform"))
TF_DATA_DIR = os.environ.get("TF_DATA_DIR", "/root/.tf-quarry")
SSH_KEY = os.environ.get("SSH_KEY", "/root/.ssh/quarry-lab")
SSH_USER = os.environ.get("SSH_USER", "ubuntu")


def vyvod_terraform():
    sreda = dict(os.environ, TF_DATA_DIR=TF_DATA_DIR)
    gotovo = subprocess.run(
        ["terraform", f"-chdir={TF_DIR}", "output", "-json"],
        capture_output=True, text=True, env=sreda, check=False,
    )
    if gotovo.returncode != 0:
        print(gotovo.stderr.strip(), file=sys.stderr)
        raise SystemExit("terraform output не отвечает: стенд создан не отсюда?")
    return json.loads(gotovo.stdout)


def sobrat():
    vyvod = vyvod_terraform()
    uzly = vyvod.get("nodes", {}).get("value", {})
    nas = vyvod.get("nas", {}).get("value", {})

    hostvars = {}
    for imya, ip in list(uzly.items()) + list(nas.items()):
        hostvars[imya] = {
            "ansible_host": ip,
            "ansible_user": SSH_USER,
            "ansible_ssh_private_key_file": SSH_KEY,
            # Машины живут в закрытой сети и пересоздаются: сверять ключ хоста
            # не с чем, а приглашение «yes/no» повесило бы прогон молча.
            "ansible_ssh_common_args": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        }

    # Приоритет keepalived: первая по имени машина хранилища старше. Здесь же,
    # а не в роли, потому что это свойство расстановки машин, а не настройки.
    for nomer, imya in enumerate(sorted(nas)):
        hostvars[imya]["nas_prioritet"] = 150 - nomer * 50
        hostvars[imya]["nas_sosedi"] = {k: v for k, v in sorted(nas.items())}

    return {
        "uzly": {"hosts": sorted(uzly)},
        "hranilishche": {"hosts": sorted(nas)},
        "stend": {"children": ["uzly", "hranilishche"]},
        "_meta": {"hostvars": hostvars},
    }


if __name__ == "__main__":
    if "--host" in sys.argv:
        print(json.dumps({}))
    else:
        print(json.dumps(sobrat(), indent=2, ensure_ascii=False))
