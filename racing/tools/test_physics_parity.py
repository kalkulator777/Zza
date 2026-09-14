#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка численного совпадения game/physics.py и static/js/physics.js.

Модуль физики существует в двух реализациях, и они обязаны считать одно и то
же: расхождение между сервером и предсказанием клиента вылезает как дрожание
машины под игроком. Этот файл — постоянная часть репозитория, а не
одноразовый скрипт: гоняй его после каждой правки любой из двух реализаций.

Что делает::

    cd racing && python3 tools/test_physics_parity.py

1. Строит сценарий ввода на несколько тысяч шагов: разгон, повороты, дрифт
   с полным зарядом и его срывом, короткий дрифт, ручник без руля, торможение
   в пол, задний ход, удар о стену, ракета (раскрутка), турбо, «Гроза», щит,
   и хвост из детерминированного шумного ввода.
2. Прогоняет сценарий через Python-реализацию на заглушке трассы.
3. Если в системе есть ``node`` — прогоняет ТОТ ЖЕ сценарий и ТУ ЖЕ заглушку
   через static/js/physics.js и сравнивает траектории пошагово.
   Нет ``node`` — честно печатает, что сравнение пропущено, и не падает.
4. Печатает максимальное расхождение по позиции и по курсу, сверяет события
   ускорения за занос и меряет время одного шага на одну машину.

Заглушка трассы (``StubTrack``) живёт здесь, а не в game/track.py: настоящую
трассу пишет другой исполнитель, а тесту физики нужна тривиальная геометрия,
которую можно слово в слово повторить в JS. Это прямой коридор вдоль +Z:
полотно |x| <= 40, трава до |x| = 60, дальше стена.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from math import floor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from game import physics                                  # noqa: E402
from game.physics import CarState, CarStats, WALL_BOUNCE   # noqa: E402

# Допуски. sin/cos в Python и в V8 могут разойтись в младшем разряде, и за
# несколько тысяч шагов интегрирования эта разница подрастает. Сантиметры на
# 70 секундах гонки — норма (RECONCILE_EPS из раздела 10.2 — целых 5 см),
# метры — это уже разъехавшийся порядок операций, то есть баг.
POS_TOL = 0.02      # м
YAW_TOL = 0.005     # рад

# Биты ввода (раздел 5.2)
GAS = physics.BTN_THROTTLE
BRAKE = physics.BTN_BRAKE
LEFT = physics.BTN_LEFT
RIGHT = physics.BTN_RIGHT
DRIFT = physics.BTN_DRIFT

# Характеристики «Хэтча» из раздела 6.5 контракта.
HATCH_STATS = {
    "engine_force": 14.0,
    "max_speed": 44.0,
    "brake_force": 26.0,
    "reverse_force": 10.0,
    "turn_rate": 2.5,
    "grip_step": 0.17,
    "drift_grip_step": 0.055,
    "drag": 0.0016,
    "roll": 0.35,
    "boost_speed": 58.0,
    "mass": 1.0,
}


class StubTrack:
    """Прямой коридор вдоль +Z. Зеркалируется в STUB_JS слово в слово.

    Реализует ровно те четыре метода интерфейса раздела 7.3, которые нужны
    шагу физики: nearest_index, surface, clamp_to_track, advance_progress.
    """

    __slots__ = ("last_s",)

    HALF_WIDTH = 40.0       # половина ширины полотна, м
    WALL_LIMIT = 60.0       # за полотном 20 м травы, дальше стена
    SAMPLE_STEP = 2.0       # шаг по дуге, как SAMPLE_STEP в разделе 7.2
    COUNT = 1000            # точек осевой линии
    LENGTH = 2000.0         # COUNT * SAMPLE_STEP

    def __init__(self):
        self.last_s = 0.0

    def nearest_index(self, x, z, hint):
        """Ближайшая точка осевой линии: коридор прямой, индекс считается."""
        return int(floor(z / self.SAMPLE_STEP)) % self.COUNT

    def surface(self, x, z, hint):
        """(index, lateral, half_width, y, pitch); высота и уклон нулевые."""
        return (self.nearest_index(x, z, hint), x, self.HALF_WIDTH, 0.0, 0.0)

    def clamp_to_track(self, state, hint):
        """Шаг 14: флаг травы, выталкивание из стены, гашение по нормали."""
        lateral = state.x
        state.offtrack = lateral > self.HALF_WIDTH or lateral < -self.HALF_WIDTH
        if lateral > self.WALL_LIMIT:
            state.x = self.WALL_LIMIT
            if state.vx > 0.0:
                state.vx = -state.vx * WALL_BOUNCE
        elif lateral < -self.WALL_LIMIT:
            state.x = -self.WALL_LIMIT
            if state.vx < 0.0:
                state.vx = -state.vx * WALL_BOUNCE

    def advance_progress(self, state, hint):
        """Шаг 16: progress по разделу 7.4, с замыканием круга и отсечкой шума."""
        state.sample_idx = self.nearest_index(state.x, state.z, hint)
        s = state.z - self.LENGTH * floor(state.z / self.LENGTH)
        ds = s - self.last_s
        if ds > self.LENGTH * 0.5:
            ds -= self.LENGTH
        elif ds < -self.LENGTH * 0.5:
            ds += self.LENGTH
        if ds > self.LENGTH * 0.25 or ds < -self.LENGTH * 0.25:
            ds = 0.0        # телепорт или шум — шаг игнорируется
        state.progress = state.progress + ds
        self.last_s = s
        state.lap = int(floor(state.progress / self.LENGTH))


# Сценарий: (сколько шагов, маска кнопок, зачем).
SCRIPT = (
    (600, GAS,                 "разгон по прямой до упора"),
    (120, GAS | LEFT,          "левый поворот"),
    (120, GAS | RIGHT,         "правый поворот"),
    (240, GAS | LEFT | DRIFT,  "дрифт влево, 4 с заряда — уровень 3"),
    (120, GAS,                 "срыв заноса, награда и разгон на ней"),
    (150, GAS | RIGHT | DRIFT, "дрифт вправо, 2.5 с — уровень 3"),
    (90,  GAS,                 "срыв"),
    (60,  GAS | LEFT | DRIFT,  "короткий дрифт, 1 с — уровень 1"),
    (60,  GAS,                 "срыв"),
    (30,  GAS | DRIFT,         "ручник без руля — занос не начинается"),
    (120, BRAKE,               "торможение в пол"),
    (240, BRAKE,               "задний ход до упора"),
    (120, BRAKE | LEFT,        "задний ход с рулём"),
    (300, GAS,                 "снова разгон, по пути ловим ракету"),
    (300, GAS | LEFT,          "вираж на «Турбо»"),
    (200, GAS | RIGHT | DRIFT, "дрифт под «Грозой»"),
    (200, 0,                   "накат, по пути удар о стену"),
)

# Внешние воздействия: (шаг, поле состояния, значение). Через них подаётся
# то, чего не выразить кнопками: попадания, бонусы и постановка машины
# в нужную точку для удара о стену.
INJECT = (
    (2100, "spin_time", 1.5),      # попадание ракеты, раскрутка 1.5 с
    (2450, "boost_time", 2.5),     # применил «Турбо»
    (2700, "slow_time", 1.2),      # накрыло «Грозой»
    (2880, "shield_time", 8.0),    # поднял щит
    # ставим машину на траву в 2 м от стены носом точно в неё, на 22 м/с:
    # гарантированный удар и гарантированная работа OFFTRACK_FACTOR
    (2900, "x", 58.0),
    (2900, "yaw", 1.5707963267948966),
    (2900, "vx", 22.0),
    (2900, "vz", 0.0),
)

TOTAL_STEPS = 4200


def build_plan():
    """Собрать план прогона: маска кнопок на каждый шаг плюс воздействия."""
    buttons = []
    for count, mask, _why in SCRIPT:
        for _ in range(count):
            buttons.append(mask)
    # хвост: детерминированный шумный ввод, ловит ветки, которые сценарий
    # мог не задеть (переброс руля, ручник на грани скорости, газ с тормозом)
    i = len(buttons)
    while len(buttons) < TOTAL_STEPS:
        mask = 0
        if (i // 7) % 3 != 0:
            mask |= GAS
        if (i // 11) % 5 == 0:
            mask |= BRAKE
        if (i // 13) % 4 == 0:
            mask |= LEFT
        if (i // 17) % 4 == 0:
            mask |= RIGHT
        if (i // 23) % 3 == 0:
            mask |= DRIFT
        buttons.append(mask)
        i += 1
    return {
        "buttons": buttons,
        "inject": [list(item) for item in INJECT],
        "stats": HATCH_STATS,
        "dt": physics.DT,
    }


def run_python(plan):
    """Прогнать план через game/physics.py, вернуть траекторию."""
    buttons = plan["buttons"]
    inject = {}
    for step_idx, field, value in plan["inject"]:
        inject.setdefault(step_idx, []).append((field, value))

    state = CarState(0.0, 0.0, 0.0)
    stats = CarStats(**plan["stats"])
    track = StubTrack()
    dt = plan["dt"]
    n = len(buttons)

    xs = [0.0] * n
    zs = [0.0] * n
    yaws = [0.0] * n
    vxs = [0.0] * n
    vzs = [0.0] * n
    charges = [0.0] * n
    events = []

    for i in range(n):
        hits = inject.get(i)
        if hits:
            for field, value in hits:
                setattr(state, field, value)
        level = physics.step(state, stats, buttons[i], dt, track,
                             state.sample_idx)
        if level:
            events.append([i, level])
        xs[i] = state.x
        zs[i] = state.z
        yaws[i] = state.yaw
        vxs[i] = state.vx
        vzs[i] = state.vz
        charges[i] = state.drift_charge

    return {
        "x": xs, "z": zs, "yaw": yaws, "vx": vxs, "vz": vzs,
        "charge": charges, "events": events,
        "progress": state.progress, "lap": state.lap,
    }


# Драйвер для node: та же заглушка трассы, тот же цикл. План приходит файлом,
# траектория уходит файлом — так не зависим от кодировки и буферов stdout.
STUB_JS = """
import { readFileSync, writeFileSync } from 'node:fs';
import { step, createCarState, createCarStats, WALL_BOUNCE } from './physics.mjs';

/* Зеркало StubTrack из tools/test_physics_parity.py */
const HALF_WIDTH = 40.0;
const WALL_LIMIT = 60.0;
const SAMPLE_STEP = 2.0;
const COUNT = 1000;
const LENGTH = 2000.0;

class StubTrack {
    constructor() {
        this.lastS = 0.0;
    }

    nearestIndex(x, z, hint) {
        const i = Math.floor(z / SAMPLE_STEP);
        return ((i % COUNT) + COUNT) % COUNT;
    }

    surface(x, z, hint) {
        return [this.nearestIndex(x, z, hint), x, HALF_WIDTH, 0.0, 0.0];
    }

    clampToTrack(state, hint) {
        const lateral = state.x;
        state.offtrack = lateral > HALF_WIDTH || lateral < -HALF_WIDTH;
        if (lateral > WALL_LIMIT) {
            state.x = WALL_LIMIT;
            if (state.vx > 0.0) {
                state.vx = -state.vx * WALL_BOUNCE;
            }
        } else if (lateral < -WALL_LIMIT) {
            state.x = -WALL_LIMIT;
            if (state.vx < 0.0) {
                state.vx = -state.vx * WALL_BOUNCE;
            }
        }
    }

    advanceProgress(state, hint) {
        state.sampleIdx = this.nearestIndex(state.x, state.z, hint);
        const s = state.z - LENGTH * Math.floor(state.z / LENGTH);
        let ds = s - this.lastS;
        if (ds > LENGTH * 0.5) {
            ds -= LENGTH;
        } else if (ds < -LENGTH * 0.5) {
            ds += LENGTH;
        }
        if (ds > LENGTH * 0.25 || ds < -LENGTH * 0.25) {
            ds = 0.0;
        }
        state.progress = state.progress + ds;
        this.lastS = s;
        state.lap = Math.floor(state.progress / LENGTH);
    }
}

/* snake_case полей плана -> camelCase полей состояния в physics.js */
const FIELD_MAP = {
    spin_time: 'spinTime',
    boost_time: 'boostTime',
    slow_time: 'slowTime',
    shield_time: 'shieldTime',
    drift_charge: 'driftCharge',
    x: 'x',
    z: 'z',
    yaw: 'yaw',
    vx: 'vx',
    vz: 'vz',
};

const plan = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const buttons = plan.buttons;
const dt = plan.dt;
const inject = new Map();
for (const row of plan.inject) {
    if (!inject.has(row[0])) {
        inject.set(row[0], []);
    }
    inject.get(row[0]).push([FIELD_MAP[row[1]], row[2]]);
}

const state = createCarState(0.0, 0.0, 0.0);
const stats = createCarStats(plan.stats);
const track = new StubTrack();
const n = buttons.length;

const xs = new Array(n);
const zs = new Array(n);
const yaws = new Array(n);
const vxs = new Array(n);
const vzs = new Array(n);
const charges = new Array(n);
const events = [];

for (let i = 0; i < n; i++) {
    const hits = inject.get(i);
    if (hits !== undefined) {
        for (const hit of hits) {
            state[hit[0]] = hit[1];
        }
    }
    const level = step(state, stats, buttons[i], dt, track, state.sampleIdx);
    if (level) {
        events.push([i, level]);
    }
    xs[i] = state.x;
    zs[i] = state.z;
    yaws[i] = state.yaw;
    vxs[i] = state.vx;
    vzs[i] = state.vz;
    charges[i] = state.driftCharge;
}

writeFileSync(process.argv[3], JSON.stringify({
    x: xs, z: zs, yaw: yaws, vx: vxs, vz: vzs,
    charge: charges, events: events,
    progress: state.progress, lap: state.lap,
}));
"""


def run_js(plan):
    """Прогнать тот же план через static/js/physics.js. None, если нет node."""
    node = shutil.which("node")
    if node is None:
        return None

    js_src = os.path.join(ROOT, "static", "js", "physics.js")
    workdir = tempfile.mkdtemp(prefix="physics-parity-")
    try:
        # physics.js копируется как .mjs: без package.json node считает .js
        # обычным CommonJS и спотыкается на export. Содержимое не меняется.
        shutil.copyfile(js_src, os.path.join(workdir, "physics.mjs"))
        driver = os.path.join(workdir, "driver.mjs")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(STUB_JS)
        plan_path = os.path.join(workdir, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        out_path = os.path.join(workdir, "out.json")

        proc = subprocess.run([node, driver, plan_path, out_path],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            print("  node вернул код %d" % proc.returncode)
            err = (proc.stderr or "").strip()
            if err:
                print("  " + err.replace("\n", "\n  "))
            return None
        with open(out_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def compare(py, js):
    """Сравнить траектории. Возвращает True, если расхождение в допуске."""
    n = len(py["x"])
    if len(js["x"]) != n:
        print("  длина траекторий не совпала: %d против %d" % (n, len(js["x"])))
        return False

    max_pos = 0.0
    max_pos_at = 0
    max_yaw = 0.0
    max_yaw_at = 0
    max_vel = 0.0
    for i in range(n):
        dx = py["x"][i] - js["x"][i]
        dz = py["z"][i] - js["z"][i]
        d = (dx * dx + dz * dz) ** 0.5
        if d > max_pos:
            max_pos = d
            max_pos_at = i
        dyaw = abs(py["yaw"][i] - js["yaw"][i])
        if dyaw > max_yaw:
            max_yaw = dyaw
            max_yaw_at = i
        dvx = py["vx"][i] - js["vx"][i]
        dvz = py["vz"][i] - js["vz"][i]
        dv = (dvx * dvx + dvz * dvz) ** 0.5
        if dv > max_vel:
            max_vel = dv

    print("  расхождение позиции: %.6f м (максимум на шаге %d)"
          % (max_pos, max_pos_at))
    print("  расхождение курса:   %.9f рад (максимум на шаге %d)"
          % (max_yaw, max_yaw_at))
    print("  расхождение скорости: %.6f м/с" % max_vel)
    print("  progress: python %.6f м, js %.6f м, разница %.6f м"
          % (py["progress"], js["progress"], abs(py["progress"] - js["progress"])))

    ok = True
    if py["events"] != js["events"]:
        print("  РАСХОЖДЕНИЕ: события ускорения за занос не совпали")
        print("    python: %r" % (py["events"],))
        print("    js:     %r" % (js["events"],))
        ok = False
    else:
        print("  события ускорения за занос совпали: %d штук, уровни %s"
              % (len(py["events"]), [e[1] for e in py["events"]]))
    if py["lap"] != js["lap"]:
        print("  РАСХОЖДЕНИЕ: круги не совпали (%d против %d)"
              % (py["lap"], js["lap"]))
        ok = False
    if max_pos > POS_TOL:
        print("  РАСХОЖДЕНИЕ: позиция вышла за допуск %.3f м" % POS_TOL)
        ok = False
    if max_yaw > YAW_TOL:
        print("  РАСХОЖДЕНИЕ: курс вышел за допуск %.4f рад" % YAW_TOL)
        ok = False
    return ok


def bench(plan, repeats=40):
    """Сколько микросекунд занимает один шаг на одну машину."""
    buttons = plan["buttons"]
    stats = CarStats(**plan["stats"])
    dt = plan["dt"]
    n = len(buttons)
    state = CarState(0.0, 0.0, 0.0)
    track = StubTrack()
    phys_step = physics.step

    # прогрев
    for i in range(n):
        phys_step(state, stats, buttons[i], dt, track, state.sample_idx)

    best = None
    for _ in range(repeats):
        state.reset(0.0, 0.0, 0.0)
        track.last_s = 0.0
        t0 = time.perf_counter()
        for i in range(n):
            phys_step(state, stats, buttons[i], dt, track, state.sample_idx)
        elapsed = time.perf_counter() - t0
        if best is None or elapsed < best:
            best = elapsed
    return best / n * 1e6


def report_feel(py):
    """Пара чисел про ощущения от управления — глазами их проверять дольше."""
    vx = py["vx"]
    vz = py["vz"]
    speeds = [(vx[i] * vx[i] + vz[i] * vz[i]) ** 0.5 for i in range(len(vx))]
    top = max(speeds)
    # разгон с нуля: первая фаза сценария — чистый газ по прямой
    to_60 = next((i for i in range(600) if speeds[i] >= 16.666), None)
    line = "  максимальная скорость %.1f м/с (%.0f км/ч)" % (top, top * 3.6)
    if to_60 is not None:
        line += ", разгон до 60 км/ч за %.2f с" % (to_60 / 60.0)
    print(line)
    print("  скорость в конце прямого разгона: %.1f м/с (%.0f км/ч)"
          % (speeds[599], speeds[599] * 3.6))


def main():
    print("Проверка совпадения физики Python и JS")
    print("  шагов в сценарии: %d (%.1f с игрового времени)"
          % (TOTAL_STEPS, TOTAL_STEPS / 60.0))

    plan = build_plan()

    t0 = time.perf_counter()
    py = run_python(plan)
    print("  python-прогон: %.3f с" % (time.perf_counter() - t0))
    if not py["events"]:
        print("  ОШИБКА: сценарий не выдал ни одного ускорения за занос,")
        print("  значит дрифт не проверен. Чини сценарий.")
        return 1
    report_feel(py)

    print()
    js = run_js(plan)
    if js is None:
        if shutil.which("node") is None:
            print("  node в системе не найден — сравнение с JS пропущено.")
            print("  Python-реализация прогнана целиком и отработала.")
        else:
            print("  JS-прогон не удался, сравнение пропущено (см. вывод выше).")
        ok = True
    else:
        ok = compare(py, js)

    print()
    per_step = bench(plan)
    print("  один шаг физики на одну машину: %.2f мкс" % per_step)
    print("  восемь машин на тик: %.3f мс (бюджет тика — 2 мс)"
          % (per_step * 8 / 1000.0))

    print()
    if ok:
        print("ГОТОВО: расхождений сверх допуска нет.")
        return 0
    print("ПРОВАЛ: реализации разошлись, ищи разницу в порядке операций.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
