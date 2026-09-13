#!/usr/bin/env python3
"""Кросс-проверка физики: Python против JavaScript.

shared/carstep.py и static/js/game/carstep.js — два зеркальных кода, и от их
совпадения зависит клиентское предсказание. Этот скрипт прогоняет через обе
версии одну и ту же длинную последовательность вводов и требует ПОБИТОВОГО
совпадения траекторий.

Запуск:  python3 tools/crosscheck.py
Нужен node. Если node нет — скрипт скажет об этом и выйдет с кодом 2
(на боевых машинах он не нужен, это инструмент разработки).
"""

import json
import os
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from shared.carstep import step, IN_UP, IN_DOWN, IN_LEFT, IN_RIGHT, IN_HANDBRAKE  # noqa: E402

STEPS = 12000
DT = 1.0 / 60.0


class Car:
    __slots__ = ("x", "y", "vx", "vy", "a")

    def __init__(self):
        self.x = 0.0
        self.y = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.a = 0.0


def make_script():
    """Детерминированный сценарий: вводы, покрытия и эффекты на каждый тик.

    Генерируется один раз здесь и передаётся обеим сторонам файлом, чтобы не
    пришлось зеркалить ещё и генератор случайных чисел.
    """
    surfaces = [
        [1.0, 1.0, 1.0, 1.0],     # асфальт
        [0.72, 4.1, 0.45, 0.55],  # трава
        [0.16, 0.7, 0.75, 1.0],   # лёд
        [0.55, 5.6, 0.35, 0.45],  # песок
    ]
    effects = [
        [1.0, 1.0, 1.0, 1.0, 0.0, 1.0],    # без эффектов
        [2.4, 1.7, 1.0, 1.0, 0.0, 1.0],    # ускорение
        [1.0, 1.0, 1.0, 1.0, 0.0, -1.0],   # управление наоборот
        [1.0, 1.0, 1.0, 1.0, 7.5, 0.0],    # раскрутило после попадания
        [1.0, 0.45, 0.6, 1.0, 0.0, 1.0],   # замедление
    ]
    seed = 0x2F6E3C91
    script = []
    for i in range(STEPS):
        # xorshift32 — целочисленный, одинаково считается везде
        seed ^= (seed << 13) & 0xFFFFFFFF
        seed ^= seed >> 17
        seed ^= (seed << 5) & 0xFFFFFFFF
        seed &= 0xFFFFFFFF

        inp = 0
        if seed & 1:
            inp |= IN_UP
        if (seed >> 3) & 1 and not (seed & 1):
            inp |= IN_DOWN
        if (seed >> 5) & 3 == 1:
            inp |= IN_LEFT
        if (seed >> 7) & 3 == 1:
            inp |= IN_RIGHT
        if (seed >> 11) & 15 == 3:
            inp |= IN_HANDBRAKE

        script.append([inp, (seed >> 17) % 4, (seed >> 21) % 5])
    return {"surfaces": surfaces, "effects": effects, "script": script, "dt": DT}


def hexd(v):
    return struct.pack("<d", v).hex()


def run_python(data, C):
    car = Car()
    car.a = 0.35
    out = []
    for inp, si, ei in data["script"]:
        step(car, inp, data["dt"], data["surfaces"][si], C, data["effects"][ei])
        out.append(hexd(car.x) + hexd(car.y) + hexd(car.vx) + hexd(car.vy) + hexd(car.a))
    return out


JS_DRIVER_A = r"""
import { readFileSync } from 'fs';
import { step } from '%(root)s/static/js/game/carstep.js';

const data = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const C = JSON.parse(readFileSync('%(root)s/shared/physics.json', 'utf8')).car;

const buf = new ArrayBuffer(8), dv = new DataView(buf);
function hexd(v) {
  dv.setFloat64(0, v, true);
  let s = '';
  for (let i = 0; i < 8; i++) s += dv.getUint8(i).toString(16).padStart(2, '0');
  return s;
}

const car = { x: 0.0, y: 0.0, vx: 0.0, vy: 0.0, a: 0.35 };
const out = [];
for (const [inp, si, ei] of data.script) {
  step(car, inp, data.dt, data.surfaces[si], C, data.effects[ei]);
  out.push(hexd(car.x) + hexd(car.y) + hexd(car.vx) + hexd(car.vy) + hexd(car.a));
}
console.log(out.join('\n'));
"""

JS_DRIVER_B = r"""
import { readFileSync } from 'fs';
import { driveTick } from '%(root)s/static/js/game/carstep.js';
import { TrackGeom } from '%(root)s/static/js/game/trackgeom.js';

const data = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const phys = JSON.parse(readFileSync('%(root)s/shared/physics.json', 'utf8'));
const C = phys.car;
const CC = phys.collision;
const track = new TrackGeom(data.track);

const buf = new ArrayBuffer(8), dv = new DataView(buf);
function hexd(v) {
  dv.setFloat64(0, v, true);
  let s = '';
  for (let i = 0; i < 8; i++) s += dv.getUint8(i).toString(16).padStart(2, '0');
  return s;
}

const car = { ...data.start, seg: data.start.seg };
const out = [];
for (const [inp, ei] of data.script2) {
  const hit = driveTick(car, inp, data.dt, track, C, CC, data.effects[ei], data.surfaces2);
  out.push(hexd(car.x) + hexd(car.y) + hexd(car.vx) + hexd(car.vy) + hexd(car.a) +
           '|' + car.seg + '|' + hexd(hit));
}
console.log(out.join('\n'));
"""


def run_node(driver_src, payload, root):
    tmp = os.path.join(root, ".crosscheck_script.json")
    drv = os.path.join(root, ".crosscheck_driver.mjs")
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f)
        with open(drv, "w") as f:
            f.write(driver_src % {"root": root})
        res = subprocess.run(["node", drv, tmp], capture_output=True, text=True)
    finally:
        for p in (tmp, drv):
            if os.path.exists(p):
                os.remove(p)
    if res.returncode != 0:
        raise RuntimeError("node упал:\n" + res.stderr)
    return res.stdout.strip().split("\n")


def compare(name, py, js, hint):
    if len(py) != len(js):
        print(f"[{name}] Разная длина: python={len(py)} js={len(js)}")
        return False
    for i, (a, b) in enumerate(zip(py, js)):
        if a != b:
            print(f"[{name}] РАСХОЖДЕНИЕ на тике {i} из {len(py)}")
            print(f"  python: {a}")
            print(f"  js:     {b}")
            print("\n" + hint)
            return False
    print(f"[{name}] совпадает побитово: {len(py)} тиков")
    return True


def phase_track(root, C, CC, data):
    """Фаза 2: полный круг по трассе — физика + покрытия + стены.

    Именно здесь ловится разница в остатке от деления: в JS (-3 % 10) === -3,
    а в Python 7, и оконный поиск сегмента возле нуля разъезжается.
    """
    from server.track import load_tracks

    tracks, errs = load_tracks(os.path.join(root, "shared", "tracks"))
    if errs:
        raise RuntimeError("трассы не грузятся: " + "; ".join(errs))
    track = tracks.get("test_ring") or next(iter(tracks.values()))

    surfaces = []
    from shared.carstep import SURFACE_ORDER
    for name in SURFACE_ORDER:
        s = json.load(open(os.path.join(root, "shared", "physics.json")))["surfaces"][name]
        surfaces.append([s["grip"], s["drag"], s["accel"], s["maxSpeed"]])

    slot = track.start_slots(1)[0]

    class Car:
        pass

    car = Car()
    car.x, car.y = slot["x"], slot["y"]
    car.vx = car.vy = 0.0
    car.a = slot["a"]
    car.seg = slot["seg"]

    # Сценарий: в основном газ с рысканием рулём, чтобы машина
    # регулярно вылетала на траву и тёрлась о стены.
    seed = 0x51A3C7D9
    script2 = []
    for i in range(9000):
        seed ^= (seed << 13) & 0xFFFFFFFF
        seed ^= seed >> 17
        seed ^= (seed << 5) & 0xFFFFFFFF
        seed &= 0xFFFFFFFF
        inp = 1  # газ
        if (seed >> 4) & 7 == 1:
            inp |= 4
        if (seed >> 9) & 7 == 1:
            inp |= 8
        if (seed >> 14) & 63 == 5:
            inp |= 16
        script2.append([inp, (seed >> 20) % 5])

    out = []
    from shared.carstep import drive_tick
    for inp, ei in script2:
        hit = drive_tick(car, inp, DT, track, C, CC, data["effects"][ei], surfaces)
        out.append(hexd(car.x) + hexd(car.y) + hexd(car.vx) + hexd(car.vy) + hexd(car.a)
                   + "|" + str(car.seg) + "|" + hexd(hit))

    payload = {
        "dt": DT, "script2": script2, "effects": data["effects"],
        "surfaces2": surfaces, "track": track.client_payload(),
        "start": {"x": slot["x"], "y": slot["y"], "vx": 0.0, "vy": 0.0,
                  "a": slot["a"], "seg": slot["seg"]},
    }
    js = run_node(JS_DRIVER_B, payload, root)
    return compare("трасса", out, js,
                   "Сверь query/resolve_wall в server/track.py и "
                   "static/js/game/trackgeom.js.")


def main():
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("node не найден — кросс-проверку пропускаю (это инструмент разработки).")
        return 2

    phys = json.load(open(os.path.join(ROOT, "shared", "physics.json")))
    C, CC = phys["car"], phys["collision"]
    data = make_script()

    ok = True

    py = run_python(data, C)
    js = run_node(JS_DRIVER_A, data, ROOT)
    ok &= compare("физика", py, js,
                  "Сверь shared/carstep.py и static/js/game/carstep.js построчно.")

    ok &= phase_track(ROOT, C, CC, data)

    if not ok:
        print("\nФизика разъехалась — клиентское предсказание будет дрейфовать.")
        return 1
    print("\nPython и JavaScript считают одно и то же.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
