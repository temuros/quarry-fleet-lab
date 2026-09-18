#!/usr/bin/env python3
"""Сборка apt-репозитория для закрытого контура.

Kubespray ставит на узлы два десятка системных пакетов. В контуре внешних
зеркал нет, поэтому нужное приносится заранее. Тащить полное зеркало Ubuntu
(десятки гигабайт) незачем: набор считается относительно КОНКРЕТНОГО образа,
из которого рождаются узлы.

Состав образа снимается один раз и лежит рядом в base-image-packages.txt.
Всё, что в образе уже есть, в зеркало не попадает, а зависимости
разворачиваются рекурсивно: без этого установка падает на первом же пакете,
которого не хватило.

⚠️ Сменился базовый образ - пересними base-image-packages.txt, иначе набор
посчитается для чужого состава.

    python3 apt_mirror.py --out КАТАЛОГ --baseline ФАЙЛ пакет [пакет...]
"""
import argparse
import email.utils
import gzip
import hashlib
import io
import os
import subprocess
import sys
import urllib.request


def skachat_indeks(url):
    """Возвращает список записей Packages: каждая запись это словарь полей."""
    try:
        with urllib.request.urlopen(url, timeout=120) as otvet:
            syroy = otvet.read()
    except Exception as oshibka:  # индекса может не быть, это не беда
        print(f"пропускаю {url}: {oshibka}")
        return []
    tekst = gzip.decompress(syroy).decode("utf-8", "replace")
    zapisi = []
    tekushchaya = {}
    for stroka in io.StringIO(tekst):
        stroka = stroka.rstrip("\n")
        if not stroka:
            if tekushchaya:
                zapisi.append(tekushchaya)
                tekushchaya = {}
            continue
        if stroka[0] == " ":
            continue
        if ": " in stroka:
            klyuch, znachenie = stroka.split(": ", 1)
            tekushchaya[klyuch] = znachenie
    if tekushchaya:
        zapisi.append(tekushchaya)
    return zapisi


def razobrat_zavisimosti(stroka):
    """«a (>= 1), b | c» -> [['a'], ['b', 'c']]: группы альтернатив."""
    gruppy = []
    for kusok in stroka.split(","):
        varianty = []
        for variant in kusok.split("|"):
            imya = variant.strip().split(" ")[0].split(":")[0]
            if imya:
                varianty.append(imya)
        if varianty:
            gruppy.append(varianty)
    return gruppy


def sobrat_nabor(nuzhnye, katalog_paketov, predostavlyayut, v_obraze):
    """Разворачивает зависимости, пропуская то, что уже есть в образе."""
    nabor = {}
    ochered = list(nuzhnye)
    nenaydennye = []
    while ochered:
        imya = ochered.pop(0)
        if imya in nabor or imya in v_obraze:
            continue
        zapis = katalog_paketov.get(imya)
        if zapis is None:
            # Виртуальный пакет: берём первого, кто его предоставляет.
            kandidaty = predostavlyayut.get(imya, [])
            zapis = katalog_paketov.get(kandidaty[0]) if kandidaty else None
            if zapis is None:
                nenaydennye.append(imya)
                continue
        nabor[zapis["Package"]] = zapis
        for pole in ("Pre-Depends", "Depends"):
            for gruppa in razobrat_zavisimosti(zapis.get(pole, "")):
                # Если любой вариант группы уже стоит в образе, группа закрыта.
                if any(v in v_obraze for v in gruppa):
                    continue
                for variant in gruppa:
                    if variant in katalog_paketov or variant in predostavlyayut:
                        ochered.append(variant)
                        break
    return nabor, nenaydennye


def zapisat_indeks(katalog):
    """Packages и Release для плоского репозитория."""
    zapisi = []
    for imya in sorted(os.listdir(katalog)):
        if not imya.endswith(".deb"):
            continue
        put = os.path.join(katalog, imya)
        info = subprocess.run(
            ["dpkg-deb", "-f", put], capture_output=True, text=True, check=True
        ).stdout.strip()
        dannye = open(put, "rb").read()
        zapisi.append(
            info
            + f"\nFilename: {imya}\nSize: {len(dannye)}"
            + f"\nMD5sum: {hashlib.md5(dannye).hexdigest()}"
            + f"\nSHA256: {hashlib.sha256(dannye).hexdigest()}\n"
        )
    packages = os.path.join(katalog, "Packages")
    open(packages, "w", encoding="utf-8").write("\n".join(zapisi))
    dannye = open(packages, "rb").read()
    # Без поля Date apt на каждом update ругается «Invalid Date entry»,
    # а при строгих настройках просто отказывается брать репозиторий.
    release = (
        "Origin: quarry-lab\nLabel: quarry-lab\nSuite: stable\n"
        "Architectures: amd64\nComponents: main\n"
        "Description: пакеты, принесённые в закрытый контур\n"
        f"Date: {email.utils.formatdate(usegmt=True)}\n"
        f"MD5Sum:\n {hashlib.md5(dannye).hexdigest()} {len(dannye)} Packages\n"
        f"SHA256:\n {hashlib.sha256(dannye).hexdigest()} {len(dannye)} Packages\n"
    )
    open(os.path.join(katalog, "Release"), "w", encoding="utf-8").write(release)
    return len(zapisi)


def main():
    razbor = argparse.ArgumentParser()
    razbor.add_argument("--out", required=True)
    razbor.add_argument("--baseline", required=True)
    razbor.add_argument("--suite", default="noble")
    razbor.add_argument("--base", default="http://archive.ubuntu.com/ubuntu")
    razbor.add_argument("--security", default="http://security.ubuntu.com/ubuntu")
    razbor.add_argument("--components", default="main,universe")
    razbor.add_argument("pakety", nargs="+")
    args = razbor.parse_args()

    v_obraze = set()
    if os.path.exists(args.baseline):
        v_obraze = {
            s.strip() for s in open(args.baseline, encoding="utf-8") if s.strip()
        }
    print(f"в базовом образе уже стоит пакетов: {len(v_obraze)}")

    katalog_paketov = {}
    predostavlyayut = {}
    # Порядок важен: сначала релиз, потом обновления и безопасность, чтобы
    # свежая версия перекрывала старую.
    istochniki = []
    for komponent in args.components.split(","):
        istochniki.append(f"{args.base}/dists/{args.suite}/{komponent}/binary-amd64/Packages.gz")
        istochniki.append(f"{args.base}/dists/{args.suite}-updates/{komponent}/binary-amd64/Packages.gz")
        istochniki.append(f"{args.security}/dists/{args.suite}-security/{komponent}/binary-amd64/Packages.gz")
    for url in istochniki:
        for zapis in skachat_indeks(url):
            katalog_paketov[zapis["Package"]] = zapis
            for virtualnyy in zapis.get("Provides", "").split(","):
                imya = virtualnyy.strip().split(" ")[0]
                if imya:
                    predostavlyayut.setdefault(imya, []).append(zapis["Package"])
    print(f"пакетов в индексах: {len(katalog_paketov)}")

    nabor, nenaydennye = sobrat_nabor(
        args.pakety, katalog_paketov, predostavlyayut, v_obraze
    )
    if nenaydennye:
        print("не нашлись в индексах: " + ", ".join(sorted(nenaydennye)))
    print(f"нести в контур: {len(nabor)}")

    os.makedirs(args.out, exist_ok=True)
    # Файлы, которых больше нет в наборе, убираем: иначе репозиторий копит
    # старые версии и индекс расходится с содержимым.
    nuzhnye_fayly = {os.path.basename(z["Filename"]) for z in nabor.values()}
    for imya in os.listdir(args.out):
        if imya.endswith(".deb") and imya not in nuzhnye_fayly:
            os.remove(os.path.join(args.out, imya))

    for imya in sorted(nabor):
        zapis = nabor[imya]
        fayl = os.path.join(args.out, os.path.basename(zapis["Filename"]))
        if os.path.exists(fayl):
            continue
        baza = args.security if "-security" in zapis.get("Filename", "") else args.base
        print(f"качаю: {imya} ({zapis.get('Version', '')})")
        try:
            urllib.request.urlretrieve(f"{args.base}/{zapis['Filename']}", fayl)
        except Exception:
            urllib.request.urlretrieve(f"{args.security}/{zapis['Filename']}", fayl)

    vsego = zapisat_indeks(args.out)
    print(f"в репозитории пакетов: {vsego}")


if __name__ == "__main__":
    sys.exit(main())
