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
4. Прогоняет сценарии столкновений: машины съезжаются в одну точку, догоняют
   друг друга в лоб-в-корму и идут борт о борт по параллельным прямым.
   Это единственное, что проверяет ``resolve_collisions``, потому что
   в первом сценарии машина одна, а столкновения считает только сервер.
   Машина — КАПСУЛА (отрезок 2*CAR_AXIS_HALF плюс радиус CAR_RADIUS,
   габарит 4,00 x 1,90 м), поэтому расстояние между машинами меряется
   между отрезками, а не между центрами.
5. Печатает максимальное расхождение по позиции и по курсу, сверяет события
   ускорения за занос, флаг «вне трассы» и круги, и меряет время одного шага
   на одну машину — чистого и вместе с обращениями к трассе.
6. Печатает замеры ощущений, которые иначе проверять глазами: ручник против
   сцепления (угол за 0,75 с, потеря скорости, время возврата) и контакт
   тяжёлой машины с лёгкой.
7. Проверяет защиту награды за занос от абуза (``report_drift_guard``).
   Заказчик на плейтесте нашёл дыру: «зажать ручник и просто нажимать A/D —
   едем не быстро, но очки бонуса дрифта набираются и дают скорость».
   Здесь это ловится навсегда: виляние рулём под зажатым ручником обязано
   давать заряд НИЖЕ первого уровня, а честный занос на дуге — все три
   уровня. Заодно перекрыты способы набрать заряд даром: занос задним ходом,
   занос об стену и занос по газону. Если кто-нибудь вернёт прежнее
   поведение (``drift_charge += dt`` без стороны, без скорости и без
   проверки на трассу), этот блок падает первым.

Сценарий обязан задеть все ветки шага: если какая-то (трава, стена, задний
ход, дрифт, соприкосновение машин) не случилась, тест не молчит, а падает —
сверка, которая ничего не сверила, хуже отсутствующей.

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
from math import atan2, cos, degrees, floor, hypot, sin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from game import physics                                  # noqa: E402
from game.physics import (CarState, CarStats, WALL_BOUNCE,  # noqa: E402
                          CAR_RADIUS, CAR_AXIS_HALF)

# Допуски. sin/cos в Python и в V8 могут разойтись в младшем разряде, и за
# несколько тысяч шагов интегрирования эта разница подрастает. Сантиметры на
# 70 секундах гонки — норма (RECONCILE_EPS из раздела 10.2 — целых 5 см),
# метры — это уже разъехавшийся порядок операций, то есть баг.
POS_TOL = 0.02      # м, выше этого — провал
YAW_TOL = 0.005     # рад, выше этого — провал
# Порог подозрения. Реально наблюдаемый шум от разницы sin/cos в Python и V8 —
# около 2e-13 м на 4200 шагов. Всё, что заметно больше, но ещё в допуске, —
# почти наверняка не шум, а разошедшаяся константа или переставленная операция;
# такое лучше увидеть сразу, а не когда оно дорастёт до сантиметров.
POS_SUSPECT = 1e-6  # м

# Биты ввода (раздел 5.2)
GAS = physics.BTN_THROTTLE
BRAKE = physics.BTN_BRAKE
LEFT = physics.BTN_LEFT
RIGHT = physics.BTN_RIGHT
HANDBRAKE = physics.BTN_HANDBRAKE
DRIFT = HANDBRAKE          # имя бита 4 из раздела 5.2

# Характеристики «Хэтча» — копия записи из content/cars.json. Копия, а не
# загрузка файла: тот же словарь уезжает в JS-драйвер вместе с планом, и тест
# обязан проверять физику, а не чтение JSON. Числа раздела 6.5 контракта здесь
# не годятся: с ними машина упирается в 20 м/с, а реальная езда идёт на 20..55,
# то есть ровно там, где работает предел по сцеплению из шага 9.
HATCH_STATS = {
    "engine_force": 14.95,
    "max_speed": 62.70,
    "brake_force": 28.0,
    "reverse_force": 10.0,
    "turn_rate": 2.40,
    "grip_step": 0.172,
    "drift_grip_step": 0.055,
    "drag": 0.0004,
    "roll": 0.027,
    "boost_speed": 62.0,
    "mass": 1.0,
}

# Тяжёлая и лёгкая — для проверки, что масса решает контакт (Фургон и Багги).
HEAVY_MASS = 1.6
LIGHT_MASS = 0.7


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
    (240, GAS | LEFT | DRIFT,  "дрифт влево, 4 с — уровень 3"),
    (120, GAS,                 "срыв заноса, награда и разгон на ней"),
    (120, GAS | RIGHT | DRIFT, "дрифт вправо, 2 с — уровень 2"),
    (120, GAS,                 "срыв"),
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

SCRIPT_STEPS = sum(count for count, _mask, _why in SCRIPT)

# Внешние воздействия: (шаг, поле состояния, значение). Через них подаётся
# то, чего не выразить кнопками: попадания, бонусы и постановка машины
# в нужную точку. Без постановки фазы вырождаются: машину уносит доворотами
# в стену коридора, и «торможение» на деле проверяет стоящую у стены машину.
def _restart(at, speed):
    """Поставить машину в центр коридора носом в +Z на заданной скорости."""
    return ((at, "x", 0.0), (at, "yaw", 0.0), (at, "vx", 0.0), (at, "vz", speed))


INJECT = (
    # 24 м/с перед длинным дрифтом: на 49 м/с дуга полного лока не влезает
    # в коридор шириной 80 м, машина уезжает на газон, а там заряд не копится
    _restart(840, 24.0)             # перед «дрифтом влево»
    + _restart(1200, 18.0)          # перед «дрифтом вправо»
    + _restart(1440, 18.0)          # перед коротким дрифтом
    + _restart(1710, 18.0)          # перед торможением в пол
    + _restart(2070, 0.0)           # перед разгоном с нуля
    + ((2100, "spin_time", 1.5),)   # попадание ракеты, раскрутка 1.5 с
    + _restart(2370, 18.0)          # перед виражом
    + ((2450, "boost_time", 2.5),)  # применил «Турбо»
    + _restart(2670, 18.0)          # перед дрифтом под «Грозой»
    + ((2700, "slow_time", 1.2),    # накрыло «Грозой»
       (2880, "shield_time", 8.0),  # поднял щит
       # ставим машину на траву в 2 м от стены носом точно в неё, на 22 м/с:
       # гарантированный удар и гарантированная работа OFFTRACK_FACTOR
       (2900, "x", 58.0),
       (2900, "yaw", 1.5707963267948966),
       (2900, "vx", 22.0),
       (2900, "vz", 0.0))
    # перед блоком виляния ставим машину в центр коридора на 15 м/с: это
    # ровно та скорость, на которой заказчик показал абуз
    + _restart(SCRIPT_STEPS, 15.0)
)

# Блок абуза в общем плане: зажатый ручник плюс перекладка A/D каждые
# WIGGLE_HALF шагов. Он тут не ради вердикта (вердикт выносит
# report_drift_guard на чистой заглушке), а ради СВЕРКИ: ветка «сменилась
# сторона заноса» обязана срабатывать в Python и в JS на одном и том же шаге,
# иначе заряд разъедется и вместе с ним разъедется цвет искр.
WIGGLE_STEPS = 600          # 10 с виляния
WIGGLE_HALF = 30            # 0.5 с на сторону — период перекладки 1 с

TOTAL_STEPS = 4200

# Сценарии столкновений. Проверяют resolve_collisions — форму (капсула),
# расталкивание с учётом массы и обмен импульсом. Столкновения считает только
# сервер, поэтому в первом сценарии их нет вовсе.
#
# Запись машины: x, z, yaw, vx, vz, масса, кнопки.
COLLIDE_STEPS = 600
PI = 3.141592653589793
HALF_PI = 1.5707963267948966

COLLIDE_SETS = (
    ("съезд в одну точку", (
        (0.0, -12.0, 0.0, 0.0, 0.0, 1.0, GAS),
        (0.0, 12.0, PI, 0.0, 0.0, 1.4, GAS),
        (-12.0, 0.0, HALF_PI, 0.0, 0.0, 0.8, GAS | LEFT),
        (12.0, 0.0, -HALF_PI, 0.0, 0.0, 1.2, GAS | RIGHT),
    )),
    ("догон в корму, 30 и 20 м/с", (
        (0.0, 0.0, 0.0, 0.0, 20.0, 1.0, 0),
        (0.0, -30.0, 0.0, 0.0, 30.0, 1.0, 0),
    )),
    ("удар в бок под 90°", (
        (0.0, 0.0, 0.0, 0.0, 18.0, 1.0, 0),
        (-18.0, 17.8, HALF_PI, 18.0, 0.0, 1.0, 0),
    )),
    ("тяжёлая догоняет лёгкую", (
        (0.0, 0.0, 0.0, 0.0, 20.0, LIGHT_MASS, 0),
        (0.0, -24.0, 0.0, 0.0, 30.0, HEAVY_MASS, 0),
    )),
    ("лёгкая догоняет тяжёлую", (
        (0.0, 0.0, 0.0, 0.0, 20.0, HEAVY_MASS, 0),
        (0.0, -24.0, 0.0, 0.0, 30.0, LIGHT_MASS, 0),
    )),
    # 2.05 м между центрами: старый круг радиусом 1.1 тут срабатывал бы
    # постоянно, капсула шириной 1.90 не должна касаться никогда
    ("борт о борт по параллельным прямым", (
        (-1.025, 0.0, 0.0, 0.0, 30.0, 1.0, GAS),
        (1.025, 0.0, 0.0, 0.0, 30.0, 1.0, GAS),
    )),
    # зажим: лёгкая машина между тяжёлой и стеной коридора (x = 60).
    # Тяжёлая идёт быстрее и обгоняет вплотную, лёгкой деваться некуда
    ("зажим между машиной и стеной", (
        (58.2, 6.0, 0.0, 0.0, 20.0, LIGHT_MASS, GAS),
        (56.6, 0.0, 0.0, 0.0, 30.0, HEAVY_MASS, GAS),
    )),
)


def build_plan():
    """Собрать план прогона: маска кнопок на каждый шаг плюс воздействия."""
    buttons = []
    for count, mask, _why in SCRIPT:
        for _ in range(count):
            buttons.append(mask)
    # блок абуза: ручник зажат, руль перекладывается каждые WIGGLE_HALF шагов
    for k in range(WIGGLE_STEPS):
        buttons.append(GAS | DRIFT | (LEFT if (k // WIGGLE_HALF) % 2 == 0
                                      else RIGHT))
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
        "collide_steps": COLLIDE_STEPS,
        "collide_sets": [[list(car) for car in cars] for _name, cars in COLLIDE_SETS],
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
    offs = [0] * n
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
        offs[i] = 1 if state.offtrack else 0

    return {
        "x": xs, "z": zs, "yaw": yaws, "vx": vxs, "vz": vzs,
        "charge": charges, "offtrack": offs, "events": events,
        "progress": state.progress, "lap": state.lap,
    }


def run_python_collisions(plan):
    """Прогнать все сценарии столкновений через resolve_collisions.

    Возвращает список сценариев; сценарий — список машин; машина — плоская
    траектория по пять чисел на шаг: x, z, vx, vz, yaw. Курс нужен потому,
    что габарит теперь капсула, и расстояние между машинами без курса
    не посчитать.
    """
    dt = plan["dt"]
    steps = plan["collide_steps"]
    out = []
    for rows in plan["collide_sets"]:
        count = len(rows)
        cars = []
        stats = []
        tracks = []
        buttons = []
        for x, z, yaw, vx, vz, mass, mask in rows:
            state = CarState(x, z, yaw)
            state.vx = vx
            state.vz = vz
            cars.append(state)
            car_stats = CarStats(**plan["stats"])
            car_stats.mass = mass
            stats.append(car_stats)
            tracks.append(StubTrack())   # своя заглушка: last_s у неё своя
            buttons.append(int(mask))

        scene = []
        for _ in range(count):
            scene.append([0.0] * (steps * 5))
        for i in range(steps):
            for c in range(count):
                physics.step(cars[c], stats[c], buttons[c], dt, tracks[c],
                             cars[c].sample_idx)
            # между шагами: расталкивание и обмен импульсом для всех сразу
            physics.resolve_collisions(cars, stats, count)
            for c in range(count):
                row = scene[c]
                base = i * 5
                row[base] = cars[c].x
                row[base + 1] = cars[c].z
                row[base + 2] = cars[c].vx
                row[base + 3] = cars[c].vz
                row[base + 4] = cars[c].yaw
        out.append(scene)
    return out


# Драйвер для node: та же заглушка трассы, тот же цикл. План приходит файлом,
# траектория уходит файлом — так не зависим от кодировки и буферов stdout.
STUB_JS = """
import { readFileSync, writeFileSync } from 'node:fs';
import {
    step, resolveCollisions, createCarState, createCarStats, WALL_BOUNCE,
} from './physics.mjs';

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
const offs = new Array(n);
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
    offs[i] = state.offtrack ? 1 : 0;
}

/* сценарии столкновений машина-машина */
const collideSteps = plan.collide_steps;
const collide = [];
for (const rows of plan.collide_sets) {
    const count = rows.length;
    const cars = [];
    const carStats = [];
    const tracks = [];
    const masks = [];
    for (let c = 0; c < count; c++) {
        const state = createCarState(rows[c][0], rows[c][1], rows[c][2]);
        state.vx = rows[c][3];
        state.vz = rows[c][4];
        cars.push(state);
        const cs = createCarStats(plan.stats);
        cs.mass = rows[c][5];
        carStats.push(cs);
        tracks.push(new StubTrack());
        masks.push(rows[c][6]);
    }
    const scene = [];
    for (let c = 0; c < count; c++) {
        scene.push(new Array(collideSteps * 5));
    }
    for (let i = 0; i < collideSteps; i++) {
        for (let c = 0; c < count; c++) {
            step(cars[c], carStats[c], masks[c], dt, tracks[c], cars[c].sampleIdx);
        }
        resolveCollisions(cars, carStats, count);
        for (let c = 0; c < count; c++) {
            const row = scene[c];
            const base = i * 5;
            row[base] = cars[c].x;
            row[base + 1] = cars[c].z;
            row[base + 2] = cars[c].vx;
            row[base + 3] = cars[c].vz;
            row[base + 4] = cars[c].yaw;
        }
    }
    collide.push(scene);
}

writeFileSync(process.argv[3], JSON.stringify({
    x: xs, z: zs, yaw: yaws, vx: vxs, vz: vzs,
    charge: charges, offtrack: offs, events: events,
    progress: state.progress, lap: state.lap,
    collide: collide,
}));
"""


def run_js(plan, node):
    """Прогнать тот же план через static/js/physics.js.

    Возвращает траекторию или None, если прогон не удался. Отсутствие node
    проверяет вызывающий: «node нет» — законный пропуск, «node есть, но
    прогон упал» — провал.
    """
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

    print("  расхождение позиции: %.3e м (максимум на шаге %d)"
          % (max_pos, max_pos_at))
    print("  расхождение курса:   %.3e рад (максимум на шаге %d)"
          % (max_yaw, max_yaw_at))
    print("  расхождение скорости: %.3e м/с" % max_vel)
    print("  progress: python %.6f м, js %.6f м, разница %.6f м"
          % (py["progress"], js["progress"], abs(py["progress"] - js["progress"])))

    ok = True
    if POS_SUSPECT < max_pos <= POS_TOL:
        print("  ВНИМАНИЕ: расхождение сильно выше шума sin/cos (~2e-13 м).")
        print("  В допуск укладывается, но так выглядит разошедшаяся константа")
        print("  или переставленная операция — сверь файлы бок о бок.")
    if py["events"] != js["events"]:
        print("  РАСХОЖДЕНИЕ: события ускорения за занос не совпали")
        print("    python: %r" % (py["events"],))
        print("    js:     %r" % (js["events"],))
        ok = False
    else:
        print("  события ускорения за занос совпали: %d штук, уровни %s"
              % (len(py["events"]), [e[1] for e in py["events"]]))
    if py["offtrack"] != js["offtrack"]:
        bad = next(i for i in range(n) if py["offtrack"][i] != js["offtrack"][i])
        print("  РАСХОЖДЕНИЕ: флаг «вне трассы» разошёлся на шаге %d" % bad)
        ok = False
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


def _point_seg(px, pz, cx, cz, ux, uz):
    """Расстояние от точки до отрезка ``c ± CAR_AXIS_HALF * u``."""
    t = (px - cx) * ux + (pz - cz) * uz
    if t > CAR_AXIS_HALF:
        t = CAR_AXIS_HALF
    elif t < -CAR_AXIS_HALF:
        t = -CAR_AXIS_HALF
    dx = px - (cx + ux * t)
    dz = pz - (cz + uz * t)
    return (dx * dx + dz * dz) ** 0.5


def _side(ax, az, bx, bz, cx, cz):
    return (bx - ax) * (cz - az) - (bz - az) * (cx - ax)


def capsule_gap(ax, az, ayaw, bx, bz, byaw):
    """Зазор между двумя капсулами, м. Ноль и меньше — они пересекаются.

    Считается независимо от физики: расстояние между отрезками как минимум
    из четырёх расстояний «конец одного до другого отрезка» (и ноль, если
    отрезки пересеклись), минус два радиуса. Формулу из resolve_collisions
    тест намеренно не переиспользует — иначе он сверял бы её саму с собой.
    """
    h = CAR_AXIS_HALF
    ux, uz = sin(ayaw), cos(ayaw)
    wx, wz = sin(byaw), cos(byaw)
    a1x, a1z = ax - ux * h, az - uz * h
    a2x, a2z = ax + ux * h, az + uz * h
    b1x, b1z = bx - wx * h, bz - wz * h
    b2x, b2z = bx + wx * h, bz + wz * h
    d1 = _side(b1x, b1z, b2x, b2z, a1x, a1z)
    d2 = _side(b1x, b1z, b2x, b2z, a2x, a2z)
    d3 = _side(a1x, a1z, a2x, a2z, b1x, b1z)
    d4 = _side(a1x, a1z, a2x, a2z, b2x, b2z)
    if ((d1 > 0.0) != (d2 > 0.0)) and ((d3 > 0.0) != (d4 > 0.0)):
        dist = 0.0                       # отрезки пересеклись
    else:
        dist = min(_point_seg(a1x, a1z, bx, bz, wx, wz),
                   _point_seg(a2x, a2z, bx, bz, wx, wz),
                   _point_seg(b1x, b1z, ax, az, ux, uz),
                   _point_seg(b2x, b2z, ax, az, ux, uz))
    return dist - (CAR_RADIUS + CAR_RADIUS)


def _pair_gap(rows, a, b, i):
    base = i * 5
    return capsule_gap(rows[a][base], rows[a][base + 1], rows[a][base + 4],
                       rows[b][base], rows[b][base + 1], rows[b][base + 4])


def collide_stats(rows, sample=1):
    """Минимальный зазор между капсулами за прогон и в конце прогона."""
    count = len(rows)
    steps = len(rows[0]) // 5
    closest = 1e9
    final_min = 1e9
    for i in range(0, steps, sample):
        for a in range(count - 1):
            for b in range(a + 1, count):
                d = _pair_gap(rows, a, b, i)
                if d < closest:
                    closest = d
    last = steps - 1
    for a in range(count - 1):
        for b in range(a + 1, count):
            d = _pair_gap(rows, a, b, last)
            if d < final_min:
                final_min = d
    return closest, final_min


def compare_collisions(py_sets, js_sets):
    """Сравнить траектории всех сценариев столкновений."""
    if len(py_sets) != len(js_sets):
        print("  РАСХОЖДЕНИЕ: число сценариев столкновений не совпало")
        return False
    worst = 0.0
    worst_where = ""
    for k in range(len(py_sets)):
        py_rows = py_sets[k]
        js_rows = js_sets[k]
        name = COLLIDE_SETS[k][0]
        for c in range(len(py_rows)):
            a = py_rows[c]
            b = js_rows[c]
            if len(a) != len(b):
                print("  РАСХОЖДЕНИЕ: длина траектории %s / машина %d"
                      % (name, c))
                return False
            for m in range(0, len(a), 5):
                dx = a[m] - b[m]
                dz = a[m + 1] - b[m + 1]
                d = (dx * dx + dz * dz) ** 0.5
                if d > worst:
                    worst = d
                    worst_where = "%s, машина %d" % (name, c)
    print("  столкновения: расхождение позиции %.3e м (%s)"
          % (worst, worst_where))
    if worst > POS_TOL:
        print("  РАСХОЖДЕНИЕ: столкновения вышли за допуск %.3f м" % POS_TOL)
        return False
    return True


class NullTrack:
    """Трасса, которая ничего не делает: чтобы замерить чистую цену шага."""

    __slots__ = ()

    def clamp_to_track(self, state, hint):
        pass

    def advance_progress(self, state, hint):
        pass


def bench(plan, track, repeats=40):
    """Сколько микросекунд занимает один шаг на одну машину."""
    buttons = plan["buttons"]
    stats = CarStats(**plan["stats"])
    dt = plan["dt"]
    n = len(buttons)
    state = CarState(0.0, 0.0, 0.0)
    phys_step = physics.step

    # прогрев
    for i in range(n):
        phys_step(state, stats, buttons[i], dt, track, state.sample_idx)

    best = None
    for _ in range(repeats):
        state.reset(0.0, 0.0, 0.0)
        if isinstance(track, StubTrack):
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
    from math import sin, cos
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

    # покрытие: сценарий обязан задеть все ветки шага, иначе сверка пустая
    n = len(vx)
    fwd = [vx[i] * sin(py["yaw"][i]) + vz[i] * cos(py["yaw"][i]) for i in range(n)]
    off_steps = sum(py["offtrack"])
    wall_steps = sum(1 for i in range(n)
                     if py["x"][i] >= StubTrack.WALL_LIMIT - 1e-9
                     or py["x"][i] <= -StubTrack.WALL_LIMIT + 1e-9)
    reverse_steps = sum(1 for v in fwd if v < -1.0)
    drifting = sum(1 for c in py["charge"] if c > 0.0)
    print("  покрытие: вне трассы %d шагов, в стене %d, задним ходом %d, "
          "в заносе %d, задний ход до %.1f м/с"
          % (off_steps, wall_steps, reverse_steps, drifting, min(fwd)))
    missing = []
    if off_steps == 0:
        missing.append("трава")
    if wall_steps == 0:
        missing.append("стена")
    if reverse_steps == 0:
        missing.append("задний ход")
    if drifting == 0:
        missing.append("дрифт")
    return missing


def _speed_along(rows, car, i, ux, uz):
    base = i * 5
    return rows[car][base + 2] * ux + rows[car][base + 3] * uz


def report_collisions(sets):
    """Что именно проверено в столкновениях. Возвращает False, если сценарий
    вырожден: не касались там, где обязаны, или коснулись там, где нельзя."""
    print("  столкновения (габарит капсулы %.2f x %.2f м, %d шагов на сценарий):"
          % (2.0 * (CAR_AXIS_HALF + CAR_RADIUS), 2.0 * CAR_RADIUS,
             COLLIDE_STEPS))
    ok = True
    for k, (name, _cars) in enumerate(COLLIDE_SETS):
        rows = sets[k]
        closest, final_min = collide_stats(rows)
        touched = closest <= 0.0
        must_touch = "параллельным" not in name
        extra = ""
        if name.startswith("догон") or "догоняет" in name:
            # скорости вдоль +Z до и после контакта
            steps = len(rows[0]) // 5
            hit = None
            for i in range(steps):
                if _pair_gap(rows, 0, 1, i) <= 0.0:
                    hit = i
                    break
            if hit is not None and hit > 0:
                before = (rows[0][(hit - 1) * 5 + 3], rows[1][(hit - 1) * 5 + 3])
                after_i = min(hit + 2, steps - 1)
                after = (rows[0][after_i * 5 + 3], rows[1][after_i * 5 + 3])
                extra = (", догоняющий %.2f -> %.2f м/с, впереди идущий "
                         "%.2f -> %.2f м/с"
                         % (before[1], after[1], before[0], after[0]))
        mark = "ок" if touched == must_touch else "ПРОВАЛ"
        if touched != must_touch:
            ok = False
        print("    [%s] %-34s зазор минимум %+.3f м, в конце %+.3f м%s"
              % (mark, name, closest, final_min, extra))
    if not ok:
        print("  ОШИБКА: сценарий столкновений вырожден, править тест")
    return ok


def report_handbrake():
    """Ручник против сцепления: угол, потеря скорости, время возврата."""
    stats = CarStats(**HATCH_STATS)
    track = NullTrack()
    dt = physics.DT

    def drive(mask, seconds, state=None):
        st = state
        if st is None:
            st = CarState(0.0, 0.0, 0.0)
            st.vz = 30.0
        for _ in range(int(seconds / dt + 0.5)):
            physics.step(st, stats, mask, dt, track, 0)
        return st

    def slip_deg(st):
        fwd = st.vx * sin(st.yaw) + st.vz * cos(st.yaw)
        lat = st.vx * cos(st.yaw) - st.vz * sin(st.yaw)
        return degrees(atan2(abs(lat), abs(fwd) if fwd else 1e-9))

    grip = drive(GAS | LEFT, 0.75)
    hand = drive(GAS | LEFT | HANDBRAKE, 0.75)
    straight = drive(GAS | HANDBRAKE, 1.0)
    coast = drive(HANDBRAKE, 1.0)

    # снимаем показания ДО фазы возврата: дальше это же состояние докатывается
    grip_yaw = degrees(grip.yaw)
    grip_speed = hypot(grip.vx, grip.vz)
    hand_yaw = degrees(hand.yaw)
    hand_speed = hypot(hand.vx, hand.vz)
    hand_slip = slip_deg(hand)

    # сколько тиков до выхода из заноса после отпускания ручника
    recovered = None
    for i in range(int(2.0 / dt)):
        physics.step(hand, stats, GAS, dt, track, 0)
        if slip_deg(hand) < 3.0:
            recovered = (i + 1) * dt
            break

    print("  ручник против сцепления (Хэтч, старт 30 м/с, полный руль, 0.75 с):")
    print("    на сцеплении   поворот %5.1f°, скорость 30.0 -> %.1f м/с"
          % (grip_yaw, grip_speed))
    print("    по ручнику     поворот %5.1f°, скорость 30.0 -> %.1f м/с, "
          "угол скольжения %.1f°" % (hand_yaw, hand_speed, hand_slip))
    print("    ручник круче в %.2f раза, возврат в управляемое состояние за %s"
          % (hand_yaw / grip_yaw,
             ("%.2f с" % recovered) if recovered else "больше 2 с"))
    print("    ручник на прямой 1 с: с газом 30.0 -> %.1f м/с, накатом "
          "30.0 -> %.1f м/с; заряд за занос на прямой %.2f с (должен быть 0)"
          % (hypot(straight.vx, straight.vz), hypot(coast.vx, coast.vz),
             straight.drift_charge))


# ---------------------------------------------------------------------------
# Защита награды за занос от абуза (раздел 6.3 плюс докстринг game/physics.py)
# ---------------------------------------------------------------------------
# Заказчик на плейтесте: «Мы можем зажать ручник и просто нажимать A/D, едем
# не быстро но очки бонуса дрифта набираются и дают скорость, это абуз».
# Прежний порог HANDBRAKE_CHARGE_SLIP закрывал только «зажать пробел на
# прямой»: виляние рулём даёт боковое скольжение и на малом ходу. Теперь
# заряд защищён тремя барьерами (сторона заноса, темп по скорости, «только
# на трассе»), и всё, что ниже, — постоянная проверка этих трёх барьеров.
# Проверки делятся пополам: половина ловит абуз (заряда быть не должно),
# половина ловит перезакрученную гайку (честный занос обязан платить все три
# уровня). Обе половины нужны: без второй барьер чинится тем, что награду
# просто выключают.

WIGGLE_SECONDS = 10.0           # столько виляем в замерах абуза
GUARD_SPEED = 15.0              # м/с, скорость из жалобы заказчика


def _drive_charge(pilot, seconds, start_x, start_z, start_yaw, speed, track):
    """Прогнать пилота и вернуть (пик заряда, уровни бустов, состояние, след).

    ``pilot(i, state, v_fwd, v_lat)`` возвращает маску кнопок. След — список
    заряда по шагам: по нему видно и пик, и обнуления.
    """
    stats = CarStats(**HATCH_STATS)
    state = CarState(start_x, start_z, start_yaw)
    state.vx = speed * sin(start_yaw)
    state.vz = speed * cos(start_yaw)
    charges = []
    boosts = []
    for i in range(int(seconds / physics.DT + 0.5)):
        fx = sin(state.yaw)
        fz = cos(state.yaw)
        v_fwd = state.vx * fx + state.vz * fz
        v_lat = state.vx * fz - state.vz * fx
        level = physics.step(state, stats, pilot(i, state, v_fwd, v_lat),
                             physics.DT, track, state.sample_idx)
        if level:
            boosts.append(level)
        charges.append(state.drift_charge)
    return max(charges), boosts, state, charges


def _pilot_wiggle(half, target):
    """Зажатый ручник плюс перекладка A/D каждые ``half`` шагов."""
    def pilot(i, state, v_fwd, v_lat):
        mask = HANDBRAKE | (LEFT if (i // half) % 2 == 0 else RIGHT)
        if v_fwd < target:
            mask |= GAS
        return mask
    return pilot


def _pilot_slalom(period, amp, target):
    """Виляние вокруг ПРЯМОГО курса: цель курса ходит ±amp с периодом period.

    Это честная модель абуза: игрок не крутится на месте, а едет вперёд и
    виляет. Простая перекладка A/D на высокой частоте вырождается в занос
    в одну сторону (руль не успевает перейти через ноль) и вперёд не едет.
    """
    def pilot(i, state, v_fwd, v_lat):
        want = amp if (i % period) * 2 < period else -amp
        err = want - state.yaw
        mask = HANDBRAKE
        if v_fwd < target:
            mask |= GAS
        if err > 0.01:
            mask |= LEFT
        elif err < -0.01:
            mask |= RIGHT
        return mask
    return pilot


def _pilot_arc(radius, sign=1):
    """Честный занос: пилот держит дугу радиуса ``radius``, ручник зажат."""
    box = [0.0]
    def pilot(i, state, v_fwd, v_lat):
        box[0] += sign * v_fwd / radius * physics.DT
        err = box[0] - state.yaw
        mask = GAS | HANDBRAKE
        if err > 0.004:
            mask |= LEFT
        elif err < -0.004:
            mask |= RIGHT
        return mask
    return pilot


def _levels(charges):
    """Через сколько секунд заряд впервые дошёл до каждого из трёх уровней."""
    out = [None, None, None]
    for i, c in enumerate(charges):
        for k, threshold in enumerate((physics.DRIFT_CHARGE_L1,
                                       physics.DRIFT_CHARGE_L2,
                                       physics.DRIFT_CHARGE_L3)):
            if out[k] is None and c >= threshold:
                out[k] = (i + 1) / 60.0
    return out


def _fmt_levels(levels):
    return " ".join("L%d %s" % (k + 1, ("%.2f с" % t) if t else "нет")
                    for k, t in enumerate(levels))


def report_drift_guard():
    """Абуз награды за занос: ловится навсегда. Возвращает False при провале."""
    print("  защита награды за занос (порог уровня 1 — %.2f с заряда):"
          % physics.DRIFT_CHARGE_L1)
    ok = True
    null = NullTrack()

    def check(mark, name, extra=""):
        nonlocal ok
        if not mark:
            ok = False
        print("    [%s] %-46s %s" % ("ок" if mark else "ПРОВАЛ", name, extra))

    # --- половина первая: абуз заряда не даёт -----------------------------
    # 1. Ровно то, что показал заказчик: ручник зажат, A/D с периодом 1 с.
    for half, label in ((30, "1.00 с"), (15, "0.50 с"), (45, "1.50 с")):
        peak, boosts, _st, _c = _drive_charge(
            _pilot_wiggle(half, GUARD_SPEED), WIGGLE_SECONDS,
            0.0, 0.0, 0.0, GUARD_SPEED, null)
        check(peak < physics.DRIFT_CHARGE_L1 and not boosts,
              "виляние A/D, период %s, 15 м/с, 10 с" % label,
              "пик заряда %.3f с, ускорений %d" % (peak, len(boosts)))

    # 2. То же, но игрок реально едет вперёд: виляние вокруг прямого курса.
    for amp, period, speed in ((0.21, 60, 15.0), (0.21, 30, 15.0),
                               (0.35, 60, 15.0), (0.21, 60, 25.0)):
        peak, boosts, st, _c = _drive_charge(
            _pilot_slalom(period, amp, speed), WIGGLE_SECONDS,
            0.0, 0.0, 0.0, speed, null)
        check(peak < physics.DRIFT_CHARGE_L1 and not boosts,
              "слалом ±%.0f°, период %.2f с, %.0f м/с"
              % (degrees(amp), period / 60.0, speed),
              "пик %.3f с, ускорений %d, проехал %.0f м"
              % (peak, len(boosts), hypot(st.x, st.z)))

    # 3. Смена стороны обязана сжигать копилку. Держим занос влево, пока
    #    заряд не перевалит за первый уровень, потом перекладываем вправо.
    def flip_pilot(i, state, v_fwd, v_lat):
        mask = GAS | HANDBRAKE
        return mask | (LEFT if i < 90 else RIGHT)
    _peak, _b, _st, charges = _drive_charge(flip_pilot, 3.0, 0.0, 0.0, 0.0,
                                            26.0, null)
    before = charges[89]
    after = min(charges[90:])
    check(before > physics.DRIFT_CHARGE_L1 and after == 0.0,
          "перекладка влево -> вправо обнуляет копилку",
          "было %.2f с, стало %.2f с" % (before, after))

    # 4. Занос задним ходом: ветка требует v_fwd > HANDBRAKE_MIN_SPEED.
    def reverse_pilot(i, state, v_fwd, v_lat):
        return BRAKE | LEFT | (HANDBRAKE if i > 300 else 0)
    peak, boosts, st, _c = _drive_charge(reverse_pilot, 15.0, 0.0, 0.0, 0.0,
                                         0.0, null)
    fx, fz = sin(st.yaw), cos(st.yaw)
    check(peak == 0.0 and st.vx * fx + st.vz * fz < -5.0,
          "занос задним ходом", "заряд %.3f с" % peak)

    # 5. Занос об стену: выталкивание из шага 14 дарит боковую скорость даром.
    #    Жёсткая стена всегда за кромкой асфальта, поэтому ловится по offtrack.
    wall = StubTrack()
    hits = [0]
    def wall_pilot(i, state, v_fwd, v_lat):
        if state.x >= StubTrack.WALL_LIMIT - 1e-9:
            hits[0] += 1
        return GAS | HANDBRAKE | RIGHT
    peak, boosts, _st, _c = _drive_charge(
        wall_pilot, WIGGLE_SECONDS, 58.0, 0.0, 0.21, 22.0, wall)
    check(peak == 0.0 and hits[0] > 60, "занос об стену, 10 с",
          "заряд %.3f с, шагов в стене %d" % (peak, hits[0]))

    # --- половина вторая: честный занос по-прежнему платит ----------------
    # Длинная дуга обязана давать все три уровня, шпилька — как минимум два
    # и третий в разумное время. Если кто-нибудь закрутит барьеры сильнее,
    # эти строки покажут цену.
    for radius, entry, need3, name in (
            (40.0, 35.0, 3.0, "длинная дуга R=40 м, вход 35 м/с"),
            (30.0, 32.0, 3.0, "дуга R=30 м, вход 32 м/с"),
            (20.0, 26.0, 3.2, "дуга R=20 м, вход 26 м/с"),
            (13.0, 22.0, 4.5, "шпилька R=13 м, вход 22 м/с")):
        _peak, _b, _st, charges = _drive_charge(
            _pilot_arc(radius), 6.0, 0.0, 0.0, 0.0, entry, null)
        levels = _levels(charges)
        # честный занос не должен терять копилку на ровном месте: дрожания
        # руля внутри зоны нечувствительности не считаются перекладкой
        reset = any(charges[i] == 0.0 and charges[i - 1] > 0.0
                    for i in range(1, len(charges)))
        good = (levels[0] is not None and levels[1] is not None
                and levels[2] is not None and levels[2] <= need3 and not reset)
        check(good, name, "%s%s" % (_fmt_levels(levels),
                                    ", копилка сгорала" if reset else ""))

    # то же вправо: физика обязана быть симметричной
    _peak, _b, _st, charges = _drive_charge(
        _pilot_arc(13.0, -1), 6.0, 0.0, 0.0, 0.0, 22.0, null)
    mirrored = _levels(charges)
    check(mirrored[0] is not None and mirrored[1] is not None
          and mirrored[2] is not None, "шпилька R=13 м вправо (симметрия)",
          _fmt_levels(mirrored))

    if not ok:
        print("  ОШИБКА: защита награды за занос не держит — см. строки ПРОВАЛ")
    return ok


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
    missing = report_feel(py)
    if missing:
        print("  ОШИБКА: сценарий не задел ветки: %s" % ", ".join(missing))
        return 1

    print()
    py_collide = run_python_collisions(plan)
    if not report_collisions(py_collide):
        return 1

    print()
    node = shutil.which("node")
    if node is None:
        print("  node в системе не найден — сравнение с JS пропущено.")
        print("  Python-реализация прогнана целиком и отработала.")
        ok = True
    else:
        js = run_js(plan, node)
        if js is None:
            print("  ПРОВАЛ: node есть, но прогон static/js/physics.js не удался.")
            ok = False
        else:
            ok = compare(py, js)
            if not compare_collisions(py_collide, js["collide"]):
                ok = False

    print()
    report_handbrake()

    print()
    if not report_drift_guard():
        ok = False

    print()
    pure = bench(plan, NullTrack())
    full = bench(plan, StubTrack())
    print("  один шаг физики на одну машину: %.2f мкс" % pure)
    print("  то же вместе с заглушкой трассы (шаги 14 и 16): %.2f мкс" % full)
    print("  восемь машин на тик: %.3f мс чистой физики, %.3f мс с трассой "
          "(бюджет тика — 2 мс)" % (pure * 8 / 1000.0, full * 8 / 1000.0))

    print()
    if ok:
        print("ГОТОВО: расхождений сверх допуска нет.")
        return 0
    print("ПРОВАЛ: реализации разошлись, ищи разницу в порядке операций.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
