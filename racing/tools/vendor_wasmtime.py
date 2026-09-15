#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кладёт wasmtime в репозиторий папкой — и умеет проверить, что он там цел.

Зачем. На машинах заказчика из пакетов есть только Python 3.11 и tornado,
интернета нет (§1 контракта). Модуль физики грузится через wasmtime, значит
wasmtime обязан приехать вместе с репозиторием. Колесо
``py3-none-manylinux1_x86_64`` — это чистый Python на ctypes плюс одна
разделяемая библиотека, ставить его нечем и незачем: достаточно распаковать.

Куда. В ``racing/vendor/`` — так, чтобы ``sys.path`` с этим каталогом давал
обычный ``import wasmtime``. Рядом ложится ``vendor/wasmtime.lock.json``:
имя колеса, его SHA-256 и SHA-256 каждого распакованного файла. По нему
``--check`` проверяет дерево БЕЗ самого колеса — колесо в репозиторий не
кладём, это были бы лишние 9,8 МБ поверх распакованных 30.

Обновление версии делается не руками:

    python3 tools/vendor_wasmtime.py --wheel /путь/wasmtime-NN.0.0-....whl --pin
    python3 tools/vendor_wasmtime.py --check

Без ``--pin`` скрипт не примет колесо с чужим SHA-256: молчаливая подмена
рантайма физики — ровно то, чего не должно случиться незаметно.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import zipfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(BASE_DIR, 'vendor')
LOCK_PATH = os.path.join(VENDOR_DIR, 'wasmtime.lock.json')

# Принятая версия. Меняется только вместе с --pin и пересдачей замеров
# из §12.22: рантайм физики — часть контракта, а не деталь установки.
WHEEL_NAME = 'wasmtime-48.0.0-py3-none-manylinux1_x86_64.whl'
WHEEL_SHA256 = '58544d539053dff7bd4cf30c40d7a540862d683013c0dfa6ba46a063f5b682f7'

# Единая дата в распакованном дереве: mtime в git всё равно не едет, но
# повторная распаковка обязана давать побайтово тот же результат.
FIXED_MTIME = (2020, 1, 1, 0, 0, 0)

# Что вообще разрешено приехать из колеса. Всё прочее — повод остановиться,
# а не «наверное, так надо».
ALLOWED_TOP = ('wasmtime', 'wasmtime-')


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rel(path: str) -> str:
    return os.path.relpath(path, VENDOR_DIR).replace(os.sep, '/')


def walk_vendored(names):
    """Файлы распакованного дерева, отсортированные, путями от vendor/."""
    out = []
    for top in names:
        root = os.path.join(VENDOR_DIR, top)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            # __pycache__ появляется от первого же импорта и в лок не входит.
            if '__pycache__' in dirnames:
                dirnames.remove('__pycache__')
            for name in sorted(filenames):
                out.append(_rel(os.path.join(dirpath, name)))
    out.sort()
    return out


def extract(wheel: str, pin: bool) -> int:
    digest = sha256_file(wheel)
    name = os.path.basename(wheel)
    if not pin:
        if name != WHEEL_NAME or digest != WHEEL_SHA256:
            print('колесо не то, что принято:')
            print('  ждали  %s  %s' % (WHEEL_NAME, WHEEL_SHA256))
            print('  дали   %s  %s' % (name, digest))
            print('если версия меняется сознательно — повторите с --pin')
            return 2
    with zipfile.ZipFile(wheel) as zf:
        members = [item for item in zf.infolist() if not item.is_dir()]
        tops = sorted({item.filename.split('/')[0] for item in members})
        for top in tops:
            if not top.startswith(ALLOWED_TOP):
                print('в колесе неожиданный каталог верхнего уровня: %s' % top)
                return 2
        for item in members:
            if item.filename.startswith('/') or '..' in item.filename.split('/'):
                print('подозрительный путь в колесе: %s' % item.filename)
                return 2

        for top in tops:
            victim = os.path.join(VENDOR_DIR, top)
            if os.path.isdir(victim):
                shutil.rmtree(victim)
        os.makedirs(VENDOR_DIR, exist_ok=True)

        files = {}
        for item in sorted(members, key=lambda i: i.filename):
            target = os.path.join(VENDOR_DIR, item.filename)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            data = zf.read(item)
            with open(target, 'wb') as fh:
                fh.write(data)
            # Библиотеке — исполняемый бит, остальному 0644: иначе права
            # зависят от umask того, кто распаковывал.
            mode = 0o755 if item.filename.endswith('.so') else 0o644
            os.chmod(target, mode)
            os.utime(target, (_fixed_stamp(), _fixed_stamp()))
            files[item.filename] = {'sha256': sha256_bytes(data),
                                    'size': len(data),
                                    'mode': '0%o' % mode}

    lock = {
        'doc': 'Выпущено tools/vendor_wasmtime.py. Руками не править.',
        'wheel': name,
        'wheel_sha256': digest,
        'tops': tops,
        'files': files,
    }
    with open(LOCK_PATH, 'w', encoding='utf-8') as fh:
        json.dump(lock, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write('\n')

    total = sum(entry['size'] for entry in files.values())
    print('распаковано %d файлов, %.1f МБ -> %s'
          % (len(files), total / 1048576.0, _rel(os.path.join(VENDOR_DIR, tops[0]))))
    if pin and digest != WHEEL_SHA256:
        print('новая закрепляемая строка для WHEEL_SHA256:')
        print("WHEEL_NAME = '%s'" % name)
        print("WHEEL_SHA256 = '%s'" % digest)
    return 0


def _fixed_stamp() -> float:
    import calendar
    return float(calendar.timegm(FIXED_MTIME + (0, 0, 0)))


def check() -> int:
    if not os.path.isfile(LOCK_PATH):
        print('нет %s — wasmtime не разложен, выполните'
              ' python3 tools/vendor_wasmtime.py --wheel ...' % _rel(LOCK_PATH))
        return 1
    with open(LOCK_PATH, encoding='utf-8') as fh:
        lock = json.load(fh)
    files = lock['files']
    bad = []
    for name in sorted(files):
        path = os.path.join(VENDOR_DIR, name)
        if not os.path.isfile(path):
            bad.append('нет файла: %s' % name)
            continue
        entry = files[name]
        size = os.path.getsize(path)
        if size != entry['size']:
            bad.append('размер разошёлся: %s (%d вместо %d)'
                       % (name, size, entry['size']))
            continue
        if sha256_file(path) != entry['sha256']:
            bad.append('содержимое разошлось: %s' % name)
            continue
        mode = '0%o' % stat.S_IMODE(os.stat(path).st_mode)
        if mode != entry['mode']:
            bad.append('права разошлись: %s (%s вместо %s)'
                       % (name, mode, entry['mode']))
    extra = [name for name in walk_vendored(lock['tops'])
             if name not in files]
    for name in extra:
        bad.append('лишний файл: %s' % name)

    if bad:
        print('вендоринг wasmtime разошёлся с %s:' % _rel(LOCK_PATH))
        for line in bad[:20]:
            print('  ' + line)
        if len(bad) > 20:
            print('  ... и ещё %d' % (len(bad) - 20))
        return 1
    total = sum(entry['size'] for entry in files.values())
    print('wasmtime на месте: %d файлов, %.1f МБ, колесо %s'
          % (len(files), total / 1048576.0, lock['wheel']))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='вендоринг wasmtime в racing/vendor/')
    parser.add_argument('--wheel', help='путь к колесу wasmtime (*.whl)')
    parser.add_argument('--pin', action='store_true',
                        help='принять колесо с другим SHA-256 и напечатать новую закрепку')
    parser.add_argument('--check', action='store_true',
                        help='сверить разложенное дерево с vendor/wasmtime.lock.json')
    args = parser.parse_args(argv)

    if args.check:
        return check()
    if not args.wheel:
        parser.error('нужен либо --wheel, либо --check')
    if not os.path.isfile(args.wheel):
        print('нет такого колеса: %s' % args.wheel)
        return 2
    code = extract(args.wheel, args.pin)
    if code:
        return code
    return check()


if __name__ == '__main__':
    sys.exit(main())
