"""Выдача ключей бортам: отдельное действие, а не побочный эффект запуска.

На площадке ключи выдают один раз, при вводе терминала в эксплуатацию, и
результат этого действия (открытая часть плюс номер борта) уезжает в реестр.
Здесь так же: реестр лежит в репозитории файлом, и приёмник читает только его.

    python statement/vydat_kluchi.py                 # перевыпустить реестр
    python statement/vydat_kluchi.py balanced 12 3   # парк, самосвалов, экскаваторов

⚠️ Машина, которой ключа не выдали, на дашборде выглядит не как ошибка, а как
отвергнутый отсчёт: `quarry_statement_rejected_total{prichina="борт не в реестре"}`.
Это ровно то поведение, которое нужно: новый борт не попадает в отчёт по смене,
пока его не завели, а не попадает тихо и с данными.
"""
from __future__ import annotations

import base64
import json
import os
import sys

SVOYA_PAPKA = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SVOYA_PAPKA)
sys.path.insert(0, os.path.join(os.path.dirname(SVOYA_PAPKA), "egts"))

import kluchi  # noqa: E402
import skhema  # noqa: E402


def sobrat(park: str, samosvalov: int, ekskavatorov: int) -> dict:
    borta = {}
    for nomer in range(1, samosvalov + 1):
        oid = skhema.nomer_borta(park, "truck", f"{park[:3].upper()}-{nomer:02d}")
        borta[str(oid)] = base64.b64encode(kluchi.otkrytyy_borta(oid)).decode()
    for nomer in range(1, ekskavatorov + 1):
        oid = skhema.nomer_borta(park, "excavator", f"EX-{nomer:02d}")
        borta[str(oid)] = base64.b64encode(kluchi.otkrytyy_borta(oid)).decode()
    return {
        "opisanie": "Открытые ключи бортов. Закрытая часть остаётся в терминале.",
        "podpisannye_parki": [park],
        "borta": dict(sorted(borta.items())),
    }


def main():
    park = sys.argv[1] if len(sys.argv) > 1 else "balanced"
    samosvalov = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    ekskavatorov = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    reestr = sobrat(park, samosvalov, ekskavatorov)
    put = os.path.join(SVOYA_PAPKA, "kluchi-bortov.json")
    with open(put, "w", encoding="utf-8", newline="\n") as f:
        json.dump(reestr, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"реестр записан: {put}, бортов {len(reestr['borta'])}, "
          f"парк с подписью: {park}")


if __name__ == "__main__":
    main()
