#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка собранного модуля физики: импорты, ABI и сетка полотна.

Три вопроса, на которые отвечает один прогон:

1. **Ноль импортов.** Модуль обязан не требовать от хозяина ни одной функции,
   иначе один и тот же файл нельзя грузить и в wasmtime, и в браузере.
2. **Версия ABI** совпадает с описанием в ``tools/abi_layout.json``, а размеры
   записей в общей памяти — с выпущенными из него раскладками.
3. **Сетка полотна совпадает.** Модуль строит сетку сам из залитой осевой
   линии (см. ``native/src/trackmesh.rs``). Здесь она сверяется с сеткой,
   которую хозяин строил раньше своими руками: FNV-1a по байтам вершин
   и по байтам индексов, на каждой трассе из ``content/tracks``.

Заодно печатает то, ради чего постройку и переносили внутрь: сколько байт
теперь заливается в модуль против того, сколько заливалось раньше, и сколько
миллисекунд занимает постройка сетки внутри модуля (это цена загрузки гонки,
не цена тика).

    python3 native/tools/check_wasm.py [путь/к/racing_physics.wasm]

Возвращает 1, если хоть одна сверка не сошлась.

Нужен ``wasmtime`` для Python. Если он не установлен в систему, путь к
распакованному пакету можно дать переменной окружения ``RP_WASMTIME_PKG``.
"""
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.path.dirname(HERE)
ROOT = os.path.dirname(NATIVE)
sys.path.insert(0, ROOT)

# Обочина и стенка: те же числа, что у хозяина в 12.7. Модуль игровых
# констант не знает — они приходят в rp_track_build снаружи.
WALL_MARGIN = 2.5
WALL_HEIGHT = 2.0
TRACK_FRICTION = 1.0

# Порядок массивов осевой линии — из 12.1.
COLUMNS = ('x', 'y', 'z', 'tangent_x', 'tangent_z',
           'normal_x', 'normal_z', 'half_width', 's')
MESH_COLS = 7

FNV_SEED = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
MASK = (1 << 64) - 1


def fnv1a(data, seed=FNV_SEED):
    h = seed
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & MASK
    return h


def load_wasmtime():
    try:
        import wasmtime
        return wasmtime
    except ImportError:
        pass
    pkg = os.environ.get('RP_WASMTIME_PKG')
    if pkg and os.path.isdir(pkg):
        sys.path.insert(0, pkg)
        import wasmtime
        return wasmtime
    raise SystemExit('нет модуля wasmtime; поставь его или укажи\n'
                     '  RP_WASMTIME_PKG=/путь/к/распакованному/пакету')


# --- сетка, как её строил хозяин --------------------------------------------

def host_mesh(track):
    """Полотно + обочина + вертикальные стены одной треугольной сеткой.

    Дословно та постройка, которую до этого этапа делал хозяин и заливала
    готовой в модуль. Здесь она осталась ровно для одного — чтобы было
    с чем сверить сетку, которую теперь строит модуль.

    Семь столбцов поперёк: верх левой стены, низ левой стены, кромка
    асфальта, ось, кромка, низ правой стены, верх правой стены.
    """
    n = len(track.samples)
    verts = []
    tris = []
    for smp in track.samples:
        hw = smp.half_width
        nx, nz = smp.normal_x, smp.normal_z
        offs = (
            (hw + WALL_MARGIN, WALL_HEIGHT),
            (hw + WALL_MARGIN, 0.0),
            (hw, 0.0),
            (0.0, 0.0),
            (-hw, 0.0),
            (-(hw + WALL_MARGIN), 0.0),
            (-(hw + WALL_MARGIN), WALL_HEIGHT),
        )
        for lateral, up in offs:
            verts.append((smp.x + nx * lateral, smp.y + up, smp.z + nz * lateral))
    for i in range(n):
        j = (i + 1) % n          # круг замкнут
        for c in range(MESH_COLS - 1):
            a = i * MESH_COLS + c
            b = i * MESH_COLS + c + 1
            d = j * MESH_COLS + c
            e = j * MESH_COLS + c + 1
            tris.append((a, b, e))
            tris.append((a, e, d))
    vbytes = struct.pack('<%df' % (len(verts) * 3), *[c for v in verts for c in v])
    tbytes = struct.pack('<%dI' % (len(tris) * 3), *[c for t in tris for c in t])
    return len(verts), len(tris), vbytes, tbytes


def centerline_bytes(track):
    """Девять массивов по N f32 — ровно то, что и так едет клиенту (12.1)."""
    n = len(track.samples)
    out = bytearray()
    for name in COLUMNS:
        out += struct.pack('<%df' % n, *[getattr(s, name) for s in track.samples])
    return n, bytes(out)


# --- модуль -----------------------------------------------------------------

class Module(object):
    def __init__(self, path):
        wasmtime = load_wasmtime()
        self.engine = wasmtime.Engine()
        self.module = wasmtime.Module.from_file(self.engine, path)
        self.store = wasmtime.Store(self.engine)
        inst = wasmtime.Instance(self.store, self.module, [])
        self.e = inst.exports(self.store)
        self.mem = self.e['memory']

    def imports(self):
        return list(self.module.imports)

    def call(self, name, *args):
        return self.e[name](self.store, *args)

    def write(self, ptr, data):
        self.mem.write(self.store, data, ptr)

    def read(self, ptr, size):
        return self.mem.read(self.store, ptr, ptr + size)

    def u64(self, name):
        """Хэш половинками: так же, как его будет читать движок без BigInt."""
        lo = self.call(name + '_lo') & 0xFFFFFFFF
        hi = self.call(name + '_hi') & 0xFFFFFFFF
        return (hi << 32) | lo


def main(argv):
    wasm = argv[0] if argv else os.path.join(NATIVE, 'testbed', 'racing_physics.wasm')
    if not os.path.exists(wasm):
        raise SystemExit('нет файла %s' % wasm)
    from game import track as track_mod

    m = Module(wasm)
    bad = 0

    imports = m.imports()
    print('модуль:   %s' % os.path.relpath(wasm, ROOT))
    print('импортов: %d %s' % (len(imports), 'ok' if not imports else 'ПЛОХО'))
    if imports:
        bad += 1
        for i in imports:
            print('   требует %s.%s' % (i.module, i.name))

    import json
    spec = json.load(open(os.path.join(ROOT, 'tools', 'abi_layout.json'),
                          encoding='utf-8'))
    ver = m.call('rp_abi_version')
    ok = ver == spec['abi_version']
    print('ABI:      модуль %d, описание %d %s'
          % (ver, spec['abi_version'], 'ok' if ok else 'ПЛОХО'))
    if not ok:
        bad += 1
    stride = m.call('rp_output_stride')
    sys.path.insert(0, os.path.join(NATIVE, 'abi'))
    import abi_layout
    if stride != abi_layout.CarOut.SIZE:
        print('CarOut:   модуль %d Б, раскладка %d Б ПЛОХО'
              % (stride, abi_layout.CarOut.SIZE))
        bad += 1
    else:
        print('CarOut:   %d Б, совпадает с раскладкой' % stride)

    tracks = sorted(f[:-5] for f in os.listdir(os.path.join(ROOT, 'content', 'tracks'))
                    if f.endswith('.json'))
    print('')
    print('%-12s %6s %7s %9s %9s %8s  %s'
          % ('трасса', 'точек', 'треуг.', 'было, КБ', 'стало,КБ', 'сборка', 'сверка'))
    total_before = total_after = 0
    for name in tracks:
        tr = track_mod.Track.load(os.path.join(ROOT, 'content', 'tracks', '%s.json' % name))
        nv_h, nt_h, vb, tb = host_mesh(tr)
        n, cl = centerline_bytes(tr)
        before = len(vb) + len(tb)
        after = len(cl)
        total_before += before
        total_after += after

        m.call('rp_world_reset', 0)
        ptr = m.call('rp_track_alloc_centerline', n)
        m.write(ptr, cl)
        t0 = time.perf_counter()
        rc = m.call('rp_track_build', WALL_MARGIN, WALL_HEIGHT, TRACK_FRICTION)
        dt = (time.perf_counter() - t0) * 1000.0

        nv = m.call('rp_track_vert_count')
        nt = m.call('rp_track_tri_count')
        vh = m.u64('rp_track_verts_hash')
        th = m.u64('rp_track_tris_hash')
        same = (rc == 0 and nv == nv_h and nt == nt_h
                and vh == fnv1a(vb) and th == fnv1a(tb))
        if not same:
            bad += 1
        print('%-12s %6d %7d %9.1f %9.1f %7.1f мс  %s'
              % (name, n, nt, before / 1024.0, after / 1024.0, dt,
                 'совпала' if same else 'РАЗОШЛАСЬ (rc=%d)' % rc))
        if not same:
            print('    вершин модуль %d / хозяин %d, хэш %016x / %016x'
                  % (nv, nv_h, vh, fnv1a(vb)))
            print('    треуг. модуль %d / хозяин %d, хэш %016x / %016x'
                  % (nt, nt_h, th, fnv1a(tb)))

    print('')
    print('заливка всего: было %.1f КБ, стало %.1f КБ (в %.1f раза меньше)'
          % (total_before / 1024.0, total_after / 1024.0,
             total_before / float(total_after)))
    print('ИТОГ: %s' % ('всё сошлось' if not bad else '%d проверок не сошлось' % bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
