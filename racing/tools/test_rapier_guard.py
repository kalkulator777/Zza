# -*- coding: utf-8 -*-
"""Постоянные проверки физики Rapier: барьеры 12.15, откат, перенастройки 12.24.

Зачем отдельный файл. ``tools/test_physics_parity.py`` сверяет Python с JS и
держит шестнадцать антиэксплойт-проверок из §12.15 — но охраняют они ТОЛЬКО
классику: про модуль тот файл не знает вовсе. Барьеры в
``native/src/world.rs`` написаны, а «барьер написан в коде» и «эксплойт
закрыт» — разные утверждения, и разница между ними меряется, а не
объявляется. Здесь она измерена.

Что внутри:

* **шестнадцать проверок §12.15 на Rapier** — те же сценарии абуза и те же
  пять проверок с другой стороны, что честный занос по-прежнему платит все
  три уровня. Что перенесено дословно, а что заменено по смыслу и почему —
  расписано у каждой проверки;
* **шум непрерывного отката** (§12.24) — два забора, оба выведены из
  констант контракта, а не подобраны под прогон (правило §12.20);
* **перенастройки §12.24** — при ``physics=rapier`` бонусы, поток машин и
  происшествия обязаны быть выключены, а гандикап опущен;
* **сверка модуля в двух движках** (wasmtime и Node) плюс сторож md5: если
  ``.wasm`` пересобран, стенд четырёх движков надо прогнать заново, и
  проверка об этом скажет, а не понадеется на память.

Прогон:

    python3 tools/test_rapier_guard.py          # всё
    python3 tools/test_rapier_guard.py --no-wasm-parity

Отдельной командой его звать не обязательно: ``tools/test_physics_parity.py``
зовёт ``main()`` отсюда сам.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import random
import re
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from game import physics                        # noqa: E402
from game import rapier_host as rh              # noqa: E402
from game.cars import load_cars                 # noqa: E402
from game.track import Track, RAMP_BOX_FLOATS   # noqa: E402

abi = rh.abi
OUT = abi.CarOut
INP = abi.CarInput
SAVE = abi.CarSave
EFF = abi.CarEffect

DT = 1.0 / 60.0

# Пороги награды за занос. Берутся ОТТУДА ЖЕ, откуда их берёт модуль:
# game/rapier_host.py кладёт их в CarTuning, а в пресете world.rs игровых
# констант нет вовсе (§12.24). Четвёртой копии этих чисел тут не заводим.
L1 = rh.DRIFT_CHARGE_L1
L2 = rh.DRIFT_CHARGE_L2
L3 = rh.DRIFT_CHARGE_L3

# Скорости и длительности сценариев абуза — те же, что у классики в
# tools/test_physics_parity.py: сравнивать имеет смысл только одинаковое.
WIGGLE_SECONDS = 10.0
GUARD_SPEED = 15.0              # м/с, скорость из жалобы заказчика
WIGGLE_SPIN_DIST = 40.0         # м за десять секунд отделяют езду от волчка

TRACK_ID = 'avenue'
CAR_ID = 'hatch'

# --- допуски отката ---------------------------------------------------------
#
# Оба числа — КОНСТАНТЫ КОНТРАКТА, а не подгонка под замер. Это принципиально:
# §12.20 требует выводить порог из физической величины, и здесь правило
# применимо буквально, потому что величина уже есть в разделе 10.2.
#
# 1. RECONCILE_EPS = 0,05 м (10.2) — расстояние, ниже которого игра объявляет
#    позицию верной и не трогает её. ОДИН откат не имеет права сам по себе
#    сдвинуть машину дальше: переигровка тогда порождала бы ровно ту ошибку,
#    ради устранения которой её и запускают, и коррекция гонялась бы за
#    собственным хвостом. Замер: потолок 0,0051 м на десяти программах ввода,
#    то есть запас без малого ДЕСЯТИКРАТНЫЙ.
#
# 2. PREDICT_EPS_RAPIER = 0,08 м (tools/smoke_test.py) — допуск сквозного
#    теста, и он выведен в §12.24 ИЗ ЭТОГО ЖЕ шума: потолок непрерывного
#    отката 0,031 м плюс те же 5 см, что даются классике на её нулевой шум.
#    Если накопленный уход перерастёт допуск, допуск перестанет значить то,
#    из чего он выведен. Замер: потолок 0,030 м на 1200 тиках, запас 2,6x.
RECONCILE_EPS = 0.05
RECONCILE_EPS_RAPIER = 0.02
PREDICT_EPS_RAPIER = 0.08

ROLLBACK_BACK = 5               # на сколько тиков откатываем
ROLLBACK_EVERY = 3              # как часто (20 откатов в секунду — как в бою)
ROLLBACK_TICKS = 1800           # 30 с езды
ROLLBACK_SEED = 4062

# md5 выпущенного модуля. Сторож, а не украшение: хэши четырёх движков в
# §12.24 сняты С ЭТИХ БАЙТ, и другой файл их не наследует.
WASM_MD5 = 'ea40274096185f3a964a1a17b21a73ad'


# ---------------------------------------------------------------------------
# Каркас
# ---------------------------------------------------------------------------

class Report(object):
    """Счётчик проверок. Печатает строку сразу, вердикт копит."""

    def __init__(self):
        self.checks = 0
        self.failures = []

    def check(self, mark, name, extra=''):
        self.checks += 1
        if not mark:
            self.failures.append(name)
        print('    [%s] %-48s %s' % ('ок' if mark else 'ПРОВАЛ', name, extra))
        return bool(mark)


_HOST = None
_CATALOG = None
_TRACK = None


def host():
    """Один хозяин на весь прогон: мир пересоздаётся ``reset``-ом."""
    global _HOST
    if _HOST is None:
        _HOST = rh.RapierHost()
    return _HOST


def car_spec():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = load_cars(os.path.join(BASE_DIR, 'content', 'cars.json'))
    return _CATALOG.get(CAR_ID)


def track():
    global _TRACK
    if _TRACK is None:
        _TRACK = Track.load(os.path.join(BASE_DIR, 'content', 'tracks',
                                         TRACK_ID + '.json'))
    return _TRACK


class Car(object):
    """Одна машина в свежем мире: плоская площадка либо настоящее полотно.

    Свежий мир на каждый сценарий — не расточительство, а требование. Через
    ``rp_car_save``/``rp_car_restore`` сценарии переиспользовать НЕЛЬЗЯ:
    кэши разогрева солвера в сохранение не входят (§8.3 разведки), и два
    прогона из одного сохранения расходятся. Свежие миры сверены: два
    одинаковых прогона совпадают бит в бит.
    """

    def __init__(self, on_track: bool = False, spawn_lateral: float = None):
        h = host()
        h.reset(0)
        if on_track:
            tr = track()
            margin, height, friction = rh.mesh_params(tr)
            h.build_track(tr, margin, height, friction)
            h.free_track_mesh()
        else:
            # Площадка без кромок и без стен: аналог NullTrack классики.
            # Флаг offtrack при ней подаётся руками — модуль полотна не знает.
            h.add_ground(3000.0, rh.TRACK_FRICTION)
        h.tuning_preset(0)
        h.set_tuning(rh.car_tuning(car_spec(), 'arcade'))
        tune = h.tuning_values()
        rest = (tune['wheel_radius'] + tune['suspension_rest']
                + tune['half_height'] * 0.2)
        if on_track:
            tr = track()
            grid = tr.start_grid[0]
            x, z, yaw = grid['x'], grid['z'], grid['yaw']
            if spawn_lateral is not None:
                i, lateral, half_width, _y, _p = tr.surface(x, z, 0)
                shift = (half_width + spawn_lateral) - lateral
                x += tr._cnx[i] * shift
                z += tr._cnz[i] * shift
            ground = tr.surface(x, z, 0)[3]
        else:
            x = z = yaw = 0.0
            ground = 0.0
        self.h = h
        self.idx = h.spawn_car(x, ground + rest, z, yaw)
        self.out = h.outputs
        self.base = self.idx * OUT.FLOATS
        self.in_base = self.idx * INP.FLOATS
        self.on_track = on_track
        self.hint = 0
        # Курс без разрывов: модуль отдаёт yaw через atan2, а пилотам дуг
        # и слалома нужен непрерывный угол.
        self.yaw_open = 0.0
        self._prev_yaw = self.g(OUT.YAW)
        for _ in range(rh.SETTLE_TICKS):
            self.drive(0.0, 0.0, 0.0, 0.0)

    def g(self, field):
        return self.out[self.base + field]

    # --- внешние воздействия (§12.25) ------------------------------------

    def dose(self, **fields):
        """Записать дозу воздействия на СЛЕДУЮЩИЙ шаг: dose(spin_rate=9.0)."""
        h = self.h
        h._sync()
        base = self.idx * EFF.FLOATS
        for name, value in fields.items():
            h.effects[base + getattr(EFF, name.upper())] = value

    def dose_row(self, idx=None):
        """Что лежит в записи воздействия сейчас — списком из EFF.FLOATS чисел."""
        h = self.h
        h._sync()
        base = (self.idx if idx is None else idx) * EFF.FLOATS
        return [h.effects[base + k] for k in range(EFF.FLOATS)]

    def offtrack(self):
        """Тот же флаг и той же surface(), что у хозяина в ``_read_all``."""
        if not self.on_track:
            return False
        i, lateral, half_width, _y, _p = track().surface(
            self.g(OUT.PX), self.g(OUT.PZ), self.hint)
        self.hint = i
        return lateral > half_width or lateral < -half_width

    def drive(self, throttle, brake, steer, handbrake, offtrack=False):
        h = self.h
        h.set_input(self.idx, throttle, brake, steer, handbrake)
        h.inputs[self.in_base + INP.OFFTRACK] = 1.0 if offtrack else 0.0
        h.step(1)
        yaw = self.g(OUT.YAW)
        delta = yaw - self._prev_yaw
        if delta > math.pi:
            delta -= 2.0 * math.pi
        elif delta < -math.pi:
            delta += 2.0 * math.pi
        self.yaw_open += delta
        self._prev_yaw = yaw

    def v_fwd_lat(self):
        yaw = self.g(OUT.YAW)
        fx, fz = math.sin(yaw), math.cos(yaw)
        vx, vz = self.g(OUT.VX), self.g(OUT.VZ)
        return vx * fx + vz * fz, vx * fz - vz * fx

    def spin_up(self, speed):
        """Разогнаться газом до нужной скорости. Задать её иначе нечем:
        ``rp_car_spawn`` ставит машину, но не толкает."""
        guard = 0
        while self.g(OUT.SPEED) < speed and guard < 3000:
            self.drive(1.0, 0.0, 0.0, 0.0)
            guard += 1
        self.yaw_open = 0.0
        self._prev_yaw = self.g(OUT.YAW)


def drive_charge(pilot, seconds, speed, on_track=False, spawn_lateral=None,
                 force_offtrack=False):
    """Прогнать пилота и вернуть всё, что нужно любой из шестнадцати проверок.

    ``pilot(i, car, v_fwd, v_lat)`` -> (газ, тормоз, руль, ручник).
    Руль как в ``step_cars``: +1 налево, -1 направо.
    """
    car = Car(on_track=on_track, spawn_lateral=spawn_lateral)
    if speed:
        car.spin_up(speed)
    x0, z0 = car.g(OUT.PX), car.g(OUT.PZ)
    charges = []
    boosts = []
    off_ticks = 0
    off_growth = 0.0
    prev_charge = car.g(OUT.DRIFT_CHARGE)
    v_min = 1e9
    speeds = []
    for i in range(int(seconds / DT + 0.5)):
        v_fwd, v_lat = car.v_fwd_lat()
        speeds.append(v_fwd)
        off = True if force_offtrack else car.offtrack()
        if off:
            off_ticks += 1
        throttle, brake, steer, handbrake = pilot(i, car, v_fwd, v_lat)
        car.drive(throttle, brake, steer, handbrake, off)
        charge = car.g(OUT.DRIFT_CHARGE)
        if off:
            # Третий барьер 12.15 буквально: вне полотна заряд НЕ РАСТЁТ.
            off_growth = max(off_growth, charge - prev_charge)
        prev_charge = charge
        charges.append(charge)
        level = int(car.g(OUT.DRIFT_LEVEL))
        if level:
            boosts.append(level)
        if handbrake > 0.5 and 0.0 < v_fwd < v_min:
            v_min = v_fwd
    return {
        'peak': max(charges),
        'boosts': boosts,
        'charges': charges,
        'speeds': speeds,
        'dist': math.hypot(car.g(OUT.PX) - x0, car.g(OUT.PZ) - z0),
        'off_ticks': off_ticks,
        'off_growth': off_growth,
        'v_min': 0.0 if v_min > 1e8 else v_min,
        'car': car,
    }


# --- пилоты (те же, что у классики, с поправкой на ABI модуля) --------------

def pilot_wiggle(half, target):
    """Зажатый ручник плюс перекладка A/D каждые ``half`` шагов."""
    def pilot(i, car, v_fwd, v_lat):
        return (1.0 if v_fwd < target else 0.0, 0.0,
                1.0 if (i // half) % 2 == 0 else -1.0, 1.0)
    return pilot


def pilot_slalom(period, amp, target):
    """Виляние вокруг ПРЯМОГО курса: цель курса ходит ±amp с периодом period.

    Честная модель абуза: игрок не крутится на месте, а едет вперёд и виляет.
    """
    def pilot(i, car, v_fwd, v_lat):
        want = amp if (i % period) * 2 < period else -amp
        err = want - car.yaw_open
        steer = 1.0 if err > 0.01 else (-1.0 if err < -0.01 else 0.0)
        return (1.0 if v_fwd < target else 0.0), 0.0, steer, 1.0
    return pilot


def pilot_arc(radius, sign=1):
    """Честный занос: пилот держит дугу радиуса ``radius``, ручник зажат."""
    box = [0.0]

    def pilot(i, car, v_fwd, v_lat):
        box[0] += sign * v_fwd / radius * DT
        err = box[0] - car.yaw_open
        steer = 1.0 if err > 0.004 else (-1.0 if err < -0.004 else 0.0)
        return 1.0, 0.0, steer, 1.0
    return pilot


def levels(charges):
    """Через сколько секунд заряд впервые дошёл до каждого из трёх уровней."""
    out = [None, None, None]
    for i, charge in enumerate(charges):
        for k, threshold in enumerate((L1, L2, L3)):
            if out[k] is None and charge >= threshold:
                out[k] = (i + 1) * DT
    return out


def fmt_levels(rows):
    return ' '.join('L%d %s' % (k + 1, ('%.2f с' % t) if t else 'нет')
                    for k, t in enumerate(rows))


def charge_floor(v_fwd):
    """Сколько секунд НИКАК не успеть набрать третий уровень на этой скорости.

    Прямо из 6.3: ``drift_charge += k*k*dt``, ``k = clamp((v-7)/(16-7), 0, 1)``.
    Быстрее, чем ``L3 / k**2``, третий уровень не набирается ничем — это не
    подгонка, это обратная функция формулы накопления.
    """
    k = (v_fwd - 7.0) / (16.0 - 7.0)
    k = 0.0 if k < 0.0 else (1.0 if k > 1.0 else k)
    return L3 / (k * k) if k > 0.0 else float('inf')


# ---------------------------------------------------------------------------
# Шестнадцать проверок §12.15 на физике Rapier
# ---------------------------------------------------------------------------
# У классики барьеры от абуза ручника строились в том числе на ЖЁСТКОМ
# ПОТОЛКЕ УГЛА СКОЛЬЖЕНИЯ (``HANDBRAKE_MAX_SLIP`` = 0,36, около 20°).
# У Rapier потолка нет вовсе (§12.24, пункт 3), поэтому переносить надо
# СМЫСЛ — «нельзя получить награду, не заслужив её», — а не формулу.
# У каждой проверки ниже сказано, дословно она перенесена или заменена.

def report_drift_guard(report):
    print('  барьеры награды за занос на Rapier (порог уровня 1 — %.2f с):' % L1)

    # --- половина первая: абуз заряда не даёт ------------------------------

    # 1-3. ДОСЛОВНО. Ровно то, что показал заказчик: ручник зажат, A/D с
    # периодом около секунды. Оговорка про волчка тоже дословная и по той же
    # причине: выше примерно 2 Гц руль не успевает перейти через ноль
    # (у модуля это ``steer_rate``), машина уходит в занос в одну сторону и
    # крутится на месте. Заряд там копится — и у классики тоже, — но платить
    # за него нечем: за десять секунд волчок проходит единицы метров вместо
    # четырёх сотен. Поэтому проверяется и заряд, и пройденный путь.
    for half, label in ((30, '1.00 с'), (15, '0.50 с'), (45, '1.50 с')):
        run = drive_charge(pilot_wiggle(half, GUARD_SPEED), WIGGLE_SECONDS,
                           GUARD_SPEED)
        report.check((run['peak'] < L1 and not run['boosts'])
                     or run['dist'] < WIGGLE_SPIN_DIST,
                     'виляние A/D, период %s, 15 м/с, 10 с' % label,
                     'пик заряда %.3f с, ускорений %d, проехал %.0f м'
                     % (run['peak'], len(run['boosts']), run['dist']))

    # 4-7. ДОСЛОВНО. То же, но игрок реально едет вперёд: виляние вокруг
    # прямого курса. Здесь оговорки про волчка нет и быть не должно — машина
    # проходит полторы сотни метров, и заряд обязан не дотянуть до уровня 1
    # честно, а не за счёт потерянного хода.
    for amp, period, speed in ((0.21, 60, 15.0), (0.21, 30, 15.0),
                               (0.35, 60, 15.0), (0.21, 60, 25.0)):
        run = drive_charge(pilot_slalom(period, amp, speed), WIGGLE_SECONDS,
                           speed)
        report.check(run['peak'] < L1 and not run['boosts'],
                     'слалом ±%.0f°, период %.2f с, %.0f м/с'
                     % (math.degrees(amp), period / 60.0, speed),
                     'пик %.3f с, ускорений %d, проехал %.0f м'
                     % (run['peak'], len(run['boosts']), run['dist']))

    # 8. ЗАМЕНЕНА ПО СМЫСЛУ, и это тот самый барьер из native/src/world.rs,
    # который до сих пор никто не проверял. Держим занос влево, пока заряд не
    # перевалит за первый уровень, потом перекладываем вправо.
    #
    # Дословный перенос классики («после перекладки заряд равен нулю») тут
    # НЕ РАБОТАЕТ, и это выяснилось подсадкой поломки. Две причины:
    #
    #   * у классики сторона меняется в полосе вдвое шире накопления
    #     (FLIP_SLIP 0,06 против CHARGE_SLIP 0,12), поэтому на тике
    #     перекладки заряд обнуляется и НЕ растёт — ровный ноль виден;
    #     у модуля сторона обновляется ВНУТРИ полосы накопления, так что на
    #     том же тике копилка сгорает и тут же получает первое приращение.
    #     Ровного нуля не бывает никогда;
    #   * позже заряд всё равно доходит до нуля — в ветке ВЫПЛАТЫ, когда
    #     скорость падает ниже 7 м/с. Проверка «где-то дальше был ноль»
    #     зеленеет и на сломанном барьере: замер на модуле с вырезанным
    #     обнулением даёт ровно тот же тик первого нуля (166), только там он
    #     ВЫПЛАЧЕН уровнем 1, а не сожжён.
    #
    # Поэтому проверяется то, что барьер и обещает: копилка ПАДАЕТ, пока
    # занос ещё жив (ручник зажат, v_fwd выше 7 м/с), и за весь прогон не
    # выплачено ни одного ускорения. Сгоревшая копилка от выплаченной
    # отличается именно этим.
    flip_at = 90

    def flip_pilot(i, car, v_fwd, v_lat):
        return 1.0, 0.0, (1.0 if i < flip_at else -1.0), 1.0

    run = drive_charge(flip_pilot, 4.0, 26.0)
    charges = run['charges']
    speeds = run['speeds']
    before = max(charges[:flip_at])
    drop = 0.0
    drop_at = None
    for i in range(flip_at, len(charges)):
        if speeds[i] <= 7.0:
            break                # занос кончился сам: дальше это уже выплата
        step = charges[i - 1] - charges[i]
        if step > drop:
            drop, drop_at = step, i
    report.check(before > L1 and drop >= 0.9 * before and not run['boosts'],
                 'перекладка влево -> вправо сжигает копилку',
                 'накопили %.2f с, сгорело %.2f с на тике %s (v_fwd %.1f м/с), '
                 'выплат %d'
                 % (before, drop, drop_at,
                    speeds[drop_at] if drop_at else 0.0, len(run['boosts'])))

    # 9. ДОСЛОВНО. Занос задним ходом: ветка модуля требует ``v_fwd > 7``,
    # то есть строго вперёд. Разгоняемся назад, потом добавляем ручник.
    back = [0.0]

    def reverse_pilot(i, car, v_fwd, v_lat):
        if v_fwd < back[0]:
            back[0] = v_fwd
        return 0.0, 1.0, 1.0, (1.0 if i > 300 else 0.0)

    run = drive_charge(reverse_pilot, 15.0, 0.0)
    report.check(run['peak'] == 0.0 and back[0] < -5.0, 'занос задним ходом',
                 'заряд %.3f с, разогнался назад до %.1f м/с'
                 % (run['peak'], back[0]))

    # 10. ЗАМЕНЕНА ПО СМЫСЛУ, и вот почему. У классики стену держит
    # выталкивание шага 14: оно дарит боковую скорость даром, и проверка
    # ловит именно этот подарок, требуя от заряда РОВНОГО НУЛЯ на заглушке
    # с бортиком. У Rapier выталкивания в хозяине нет вовсе — стены стоят
    # в сетке, их держит солвер (§12.24). Поэтому сценарий сделан на
    # НАСТОЯЩЕМ полотне, а утверждение усилено: машина подъезжает к стене по
    # асфальту и честно набирает там сколько-то заряда, зато вне полотна
    # заряд не растёт НИ НА ОДНОМ тике, и десять секунд тёрки об стену не
    # платят ни одного ускорения. Это точнее «пика, равного нулю»: барьер
    # проверяется отдельно от подъезда к нему.
    def wall_pilot(i, car, v_fwd, v_lat):
        return 1.0, 0.0, 1.0, 1.0        # +1 — руль налево, нос во внешнюю стену

    run = drive_charge(wall_pilot, WIGGLE_SECONDS, 22.0, on_track=True,
                       spawn_lateral=-1.2)
    report.check(run['off_growth'] == 0.0 and not run['boosts']
                 and run['off_ticks'] > 60, 'занос об стену, 10 с',
                 'прирост заряда вне полотна %.6f с, пик %.3f, ускорений %d, '
                 'шагов вне полотна %d'
                 % (run['off_growth'], run['peak'], len(run['boosts']),
                    run['off_ticks']))

    # 11. ДОСЛОВНО. Занос по газону: полотно кончилось, флаг поднят. Награда
    # за занос — плата за быстрый проход поворота, а не за катание по траве.
    run = drive_charge(pilot_arc(20.0), 4.0, 26.0, force_offtrack=True)
    report.check(run['peak'] == 0.0 and not run['boosts'],
                 'занос по газону, 4 с',
                 'заряд %.3f с, шагов вне полотна %d'
                 % (run['peak'], run['off_ticks']))

    # --- половина вторая: честный занос по-прежнему платит ------------------
    # Нужна не меньше первой: без неё «защиту» чинят выключением награды.
    #
    # 12-15. ЗАМЕНЕНЫ ПО СМЫСЛУ — параметрами, не утверждением. У классики
    # дуги заданы как R=40 при входе 35 м/с, R=30 при 32, R=20 при 26 и
    # шпилька R=13 при 22. Под Rapier эти четыре не держатся: ручник
    # управляем примерно до 25 м/с, выше машина разворачивается (§12.24,
    # пункт 3), и на R=40 при входе 35 м/с пилот дугу просто теряет — пик
    # заряда 0,67 с, до первого уровня не дотягивает вовсе. Утверждение
    # осталось прежним слово в слово: длинная дуга обязана платить ВСЕ ТРИ
    # уровня, и копилка не имеет права сгорать на ровном месте. Изменились
    # радиусы и входы — на те, которые Rapier действительно держит.
    #
    # Потолок времени третьего уровня НЕ подобран под прогон: он выведен из
    # формулы накопления 6.3. Быстрее, чем L3/k**2 на своей скорости, третий
    # уровень не набирается физически; удвоение — плата за то, что живая дуга
    # скользит не на каждом тике. Перезакрученный барьер вылезет за этот
    # потолок раньше, чем успеет стать незаметным.
    for radius, entry, name in (
            (20.0, 30.0, 'дуга R=20 м, вход 30 м/с'),
            (16.0, 30.0, 'дуга R=16 м, вход 30 м/с'),
            (13.0, 26.0, 'дуга R=13 м, вход 26 м/с'),
            (13.0, 22.0, 'шпилька R=13 м, вход 22 м/с')):
        run = drive_charge(pilot_arc(radius), 6.0, entry)
        charges = run['charges']
        rows = levels(charges)
        need3 = 2.0 * charge_floor(run['v_min'])
        burned = any(charges[i] == 0.0 and charges[i - 1] > 0.0
                     for i in range(1, len(charges)))
        good = (rows[0] is not None and rows[1] is not None
                and rows[2] is not None and rows[2] <= need3 and not burned)
        report.check(good, name,
                     '%s (потолок L3 %.2f с при %.1f м/с)%s'
                     % (fmt_levels(rows), need3, run['v_min'],
                        ', копилка сгорала' if burned else ''))

    # 16. ДОСЛОВНО. То же вправо: физика обязана быть симметричной.
    run = drive_charge(pilot_arc(13.0, -1), 6.0, 22.0)
    rows = levels(run['charges'])
    report.check(rows[0] is not None and rows[1] is not None
                 and rows[2] is not None,
                 'шпилька R=13 м вправо (симметрия)', fmt_levels(rows))


# ---------------------------------------------------------------------------
# Шум непрерывного отката (§12.24)
# ---------------------------------------------------------------------------

def rollback_program(ticks, seed=ROLLBACK_SEED):
    """Программа ввода: те же биты и тот же строй, что в test_wasm_parity.

    Случайная, но с зерном: езда должна быть настоящей — с разгоном,
    торможением, заносом и ударами о стену, — и при этом воспроизводимой.
    """
    rng = random.Random(seed)
    rows = []
    held = (1.0, 0.0, 0.0, 0.0)
    for tick in range(ticks):
        if tick % 37 == 0:
            r = rng.random()
            steer = 1.0 if r < 0.30 else (-1.0 if r < 0.60 else 0.0)
            brake = 1.0 if r > 0.90 else 0.0
            handbrake = 1.0 if 0.62 < r < 0.74 else 0.0
            held = (1.0, brake, steer, handbrake)
        rows.append(held)
    return rows


def _ring_read(h, idx):
    h.car_save(idx)
    h._sync()
    base = idx * SAVE.FLOATS
    return list(h.saves[base:base + SAVE.FLOATS])


def _ring_write(h, idx, row):
    h._sync()
    base = idx * SAVE.FLOATS
    for k in range(SAVE.FLOATS):
        h.saves[base + k] = row[k]
    h.car_restore(idx)


def rollback_run(program, rolling):
    """Проехать программу; при ``rolling`` откатываться, как это делает клиент.

    Кольцо ``CarSave`` и порядок «восстановить, переиграть» повторяют
    ``RapierLocal`` из static/js/rapier_host.js: тот же откат на
    ``ROLLBACK_BACK`` тиков, та же частота ``ROLLBACK_EVERY``.

    Возвращает (след позиции по тикам, список сдвигов на ОДИН откат).
    """
    car = Car(on_track=True)
    h, idx = car.h, car.idx
    ring = [None] * (ROLLBACK_BACK + 4)
    size = len(ring)
    offs = []
    traj = []
    steps = []
    for tick, cmd in enumerate(program):
        off = car.offtrack()
        offs.append(off)
        car.drive(cmd[0], cmd[1], cmd[2], cmd[3], off)
        if rolling:
            ring[tick % size] = _ring_read(h, idx)
            if tick >= ROLLBACK_BACK and (tick - ROLLBACK_BACK) % ROLLBACK_EVERY == 0:
                before = (car.g(OUT.PX), car.g(OUT.PY), car.g(OUT.PZ))
                _ring_write(h, idx, ring[(tick - ROLLBACK_BACK) % size])
                for k in range(tick - ROLLBACK_BACK + 1, tick + 1):
                    cmd_k = program[k]
                    car.drive(cmd_k[0], cmd_k[1], cmd_k[2], cmd_k[3], offs[k])
                    ring[k % size] = _ring_read(h, idx)
                steps.append(math.dist(before, (car.g(OUT.PX), car.g(OUT.PY),
                                                car.g(OUT.PZ))))
        traj.append((car.g(OUT.PX), car.g(OUT.PY), car.g(OUT.PZ)))
    return traj, steps


def report_rollback(report):
    print('  расхождение при непрерывном откате (%d тиков по %d каждые %d):'
          % (ROLLBACK_TICKS, ROLLBACK_BACK, ROLLBACK_EVERY))
    program = rollback_program(ROLLBACK_TICKS)
    plain, _ = rollback_run(program, False)
    rolled, steps = rollback_run(program, True)

    # Забор 1. Сдвиг от ОДНОГО отката. Величина устойчивая: на десяти разных
    # программах ввода её потолок держится в пределах 0,002-0,005 м.
    steps_sorted = sorted(steps)
    step_top = steps_sorted[-1]
    step_med = steps_sorted[len(steps_sorted) // 2]
    report.check(step_top <= RECONCILE_EPS,
                 'один откат не двигает машину дальше RECONCILE_EPS',
                 'откатов %d, медиана %.5f м, потолок %.5f м (порог %.2f м, '
                 'запас %.1fx)' % (len(steps), step_med, step_top,
                                   RECONCILE_EPS,
                                   RECONCILE_EPS / step_top if step_top else 0.0))

    # Забор 2. Накопленный уход против мира, который не откатывался. Это та
    # самая величина из §12.24 (медиана 0,017 м, потолок 0,031 м). В отличие
    # от первой она ХАОТИЧЕСКАЯ: на других программах ввода потолок гуляет от
    # 0,015 до 0,49 м, потому что откат в момент касания стены разводит
    # траектории по-настоящему, и дальше они живут врозь. Поэтому программа
    # тут закреплена зерном, а порог взят не с потолка замера, а из допуска
    # сквозного теста, который в §12.24 выведен ИЗ ЭТОГО ЖЕ шума.
    errs = sorted(math.dist(plain[i], rolled[i]) for i in range(len(plain)))
    n = len(errs)
    median = errs[n // 2]
    p95 = errs[int(n * 0.95)]
    top = errs[-1]
    report.check(top <= PREDICT_EPS_RAPIER,
                 'накопленный уход не перерастает допуск сквозного теста',
                 'медиана %.4f м, p95 %.4f м, потолок %.4f м (порог %.2f м, '
                 'запас %.1fx)' % (median, p95, top, PREDICT_EPS_RAPIER,
                                   PREDICT_EPS_RAPIER / top if top else 0.0))

    # Забор 3. Типичный уход. Порог — та доля допуска, которую §12.24 ОТДАЛ
    # шуму отката, когда выводил 0,08: «потолок шума 0,031 плюс те же 5 см,
    # что даются классике». Доля шума в этой сумме и есть
    # PREDICT_EPS_RAPIER - RECONCILE_EPS = 0,03 м. Съест её медиана — вывод
    # допуска перестанет держаться, даже если потолок ещё в рамках.
    budget = PREDICT_EPS_RAPIER - RECONCILE_EPS
    report.check(median <= budget,
                 'типичный уход внутри доли, отданной шуму отката',
                 'медиана %.4f м при доле %.2f м (запас %.1fx); полоса '
                 'нечувствительности клиента %.2f м — медиана внутри неё '
                 'с запасом %.1fx'
                 % (median, budget, budget / median if median else 0.0,
                    RECONCILE_EPS_RAPIER,
                    RECONCILE_EPS_RAPIER / median if median else 0.0))


def report_eps_sync(report):
    """Три допуска выше скопированы сюда числами — сверить с их источниками.

    Копия, разъехавшаяся с оригиналом, хуже отсутствующей: она врёт с видом
    проверенной. ``RECONCILE_EPS`` и ``RECONCILE_EPS_RAPIER`` живут в
    static/js/net.js, ``PREDICT_EPS_RAPIER`` — в tools/smoke_test.py.
    """
    print('  допуски совпадают со своими источниками:')
    net = open(os.path.join(BASE_DIR, 'static', 'js', 'net.js'),
               encoding='utf-8').read()
    smoke = open(os.path.join(BASE_DIR, 'tools', 'smoke_test.py'),
                 encoding='utf-8').read()
    for name, want, text, where in (
            ('RECONCILE_EPS', RECONCILE_EPS, net, 'static/js/net.js'),
            ('RECONCILE_EPS_RAPIER', RECONCILE_EPS_RAPIER, net,
             'static/js/net.js'),
            ('PREDICT_EPS_RAPIER', PREDICT_EPS_RAPIER, smoke,
             'tools/smoke_test.py')):
        found = re.search(r'^\s*(?:export\s+const\s+)?%s\s*=\s*([0-9.]+)'
                          % name, text, re.M)
        value = float(found.group(1)) if found else None
        report.check(value == want, '%s = %s' % (name, want),
                     'в %s: %s' % (where, value))


# ---------------------------------------------------------------------------
# Перенастройки при physics=rapier (§12.24)
# ---------------------------------------------------------------------------
# «Всё, что подменяет характеристики машины по ходу гонки, при Rapier надо
# проверять ПОИМЁННО, а не считать работающим» — правило из §12.24, выведенное
# из того, что гандикап был включён и молча ничего не делал. Проверка ниже и
# есть это «поимённо»: она поднимает НАСТОЯЩУЮ симуляцию с настройками, где
# всё четыре механики попрошены включёнными, и смотрит, что вышло.

def _throttle_distance(sim, slot, ticks=180):
    """Путь машины за ``ticks`` тиков полного газа, м.

    Мир пересобирается вместе с гандикапом, поэтому машины стоят на тех же
    местах решётки и замер «до» и «после» сравним: разница — это только
    характеристики.
    """
    from game.protocol import BTN_THROTTLE
    car = sim.car_by_slot[slot]
    x0, z0 = car.state.x, car.state.z
    for t in range(ticks):
        for other in sim.cars:
            sim.set_input(other.slot, t + 1, BTN_THROTTLE)
        sim.tick()
    return math.hypot(car.state.x - x0, car.state.z - z0)


def report_restrictions(report):
    print('  перенастройки при physics=rapier:')
    from game.sim import Simulation

    wanted = {
        'track': TRACK_ID, 'laps': 1, 'mirror': False,
        'items_enabled': True, 'collisions': False, 'traffic': 'dense',
        'events': 'often', 'handicap': True, 'weather': 'dry',
        'physics': 'arcade',
    }
    players = [{'slot': 0, 'name': 'A', 'car': CAR_ID},
               {'slot': 1, 'name': 'B', 'car': CAR_ID}]
    saved = os.environ.get(rh.ENV_VAR)
    os.environ[rh.ENV_VAR] = rh.RAPIER
    try:
        report.check(rh.race_enabled(), 'гонку считает Rapier',
                     'RACING_PHYSICS=%s' % rh.backend())
        sim = Simulation(track(), dict(wanted), players)
        settings = sim.settings
        for name, value, label in rh.RESTRICTED:
            report.check(settings.get(name) == value, 'настройка %s' % label,
                         'просили %r, стало %r'
                         % (wanted[name], settings.get(name)))
        report.check(len(sim.disabled_features) == len(rh.RESTRICTED),
                     'комната сообщает игрокам, что опущено',
                     '%d фраз: %s'
                     % (len(sim.disabled_features),
                        '; '.join(sim.disabled_features)))
        # Настройка снята — подсистема обязана быть пустой, а не просто
        # выключенной флагом: именно на этой разнице сгорел гандикап.
        # Бонусы проверяются С ДРУГОЙ СТОРОНЫ: их попросили включёнными, и
        # они обязаны РАБОТАТЬ (§12.25), а не быть тихо опущенными. Мало
        # флага: та же ошибка гандикапа читалась бы как «включено и молчит»,
        # поэтому смотрим ещё и на то, что боксы на трассе есть.
        items = getattr(sim, 'items', None)
        boxes = 0 if items is None else getattr(items, 'box_count', 0)
        report.check(items is not None and getattr(items, 'enabled', False)
                     and boxes > 0,
                     'бонусы в гонке включены и разложены',
                     'items_enabled=%r, боксов %d'
                     % (None if items is None
                        else getattr(items, 'enabled', None), boxes))
        report.check('items_enabled' not in [row[0] for row in rh.RESTRICTED],
                     'бонусы больше не в списке опущенного',
                     'опущено: %s' % ', '.join(row[0] for row in rh.RESTRICTED))
        # Поток и происшествия — с той же стороны, что бонусы: их попросили
        # включёнными, и они обязаны РАБОТАТЬ (§12.28), а не быть тихо
        # опущенными. Мало флага: та же ошибка гандикапа читалась бы как
        # «включено и молчит», поэтому смотрим на живые болванки и на то,
        # что проход столкновений с ними в симуляции действительно стоит.
        traffic = getattr(sim, 'traffic', None)
        dummies = 0 if traffic is None else len(getattr(traffic, 'cars', ()) or ())
        report.check(traffic is not None and traffic.enabled and dummies > 0,
                     'поток машин в гонке включён и расставлен',
                     'traffic=%r, болванок %d'
                     % (sim.settings.get('traffic'), dummies))
        road = getattr(sim, 'road', None)
        report.check(road is not None and road.enabled,
                     'происшествия в гонке включены',
                     'events=%r, enabled=%r'
                     % (sim.settings.get('events'),
                        None if road is None else road.enabled))
        for name in ('traffic', 'events'):
            report.check(name not in [row[0] for row in rh.RESTRICTED],
                         'настройка %s больше не в списке опущенного' % name,
                         'опущено: %s' % ', '.join(row[0] for row in rh.RESTRICTED))
        # Болванок в мире модуля нет, поэтому удар о них обязан считать
        # хозяин: проход столкновений подменён на рапировский, а не на
        # пустышку. Пустышка означала бы поток, сквозь который проезжают.
        resolver = getattr(sim._resolve_collisions, '__func__', None)
        report.check(resolver is rh.RapierRace.resolve_collisions,
                     'удар о болванку считает проход RapierRace',
                     sim._resolve_collisions.__qualname__)
        report.check(sim.rapier is not None
                     and sim.rapier.__class__.__name__ == 'RapierRace',
                     'мир гонки — RapierRace',
                     sim.rapier.__class__.__name__ if sim.rapier else 'нет')
        # ГАНДИКАП (§12.30). Его попросили включённым, и он обязан
        # РАБОТАТЬ, а не быть тихо опущенным — то есть проверяется он с той
        # же стороны, что бонусы и поток. Мало флага: ровно на гандикапе
        # проект и горел, «включено и молчит». Поэтому дверь дёргается
        # по-настоящему и смотрится результат В МОДУЛЕ.
        report.check(sim.settings.get('handicap') is True,
                     'гандикап больше не опускается',
                     'handicap=%r, опущено: %s'
                     % (sim.settings.get('handicap'),
                        ', '.join(row[0] for row in rh.RESTRICTED) or 'ничего'))
        report.check('handicap' not in [row[0] for row in rh.RESTRICTED],
                     'гандикап больше не в списке опущенного')
        names = ('engine_force', 'max_speed', 'boost_speed')
        # Берём ПОСЛЕДНЮЮ машину: шаблон настроек после сборки мира хранит
        # то, что прочитала последняя spawn_car, и читать его для первой
        # машины значило бы читать чужие числа.
        car = sim.cars[-1]
        base_tuning = dict(rh.car_tuning(car.stats_base, sim.rapier.mode))
        # Замер «до» — на ОТДЕЛЬНОЙ симуляции: после первого тика мир
        # пересобрать уже нельзя (и это не мелочь, а защита: пересборка
        # посреди гонки телепортировала бы всех на решётку).
        base_dist = _throttle_distance(Simulation(track(), dict(wanted),
                                                  players), car.slot)
        sim.apply_handicap(car.slot, 0.97, names)
        live = sim.rapier.host.tuning_values()
        # 1. Тело пересоздано с замедленными настройками. Читаем ШАБЛОН,
        #    в который их положили перед spawn_car: живое тело модуль
        #    наружу не показывает, а шаблон — ровно то, что оно прочитало.
        slowed = all(abs(live[n] - base_tuning[n] * 0.97) < 1e-3 for n in names
                     if n in base_tuning)
        report.check(slowed, 'тело Rapier создано с замедленными настройками',
                     'engine_force %.0f -> %.0f (ждали %.0f)'
                     % (base_tuning['engine_force'], live['engine_force'],
                        base_tuning['engine_force'] * 0.97))
        # 2. Блок tuning каталожной записи ЦЕЛ. Прежняя сборка гандикапа
        #    отдавала голый CarStats без tuning, и машина уехала бы на
        #    чистом пресете режима: другой габарит, другая масса.
        report.check(abs(live['half_length'] - base_tuning['half_length']) < 1e-6
                     and abs(live['mass'] - base_tuning['mass']) < 1e-6,
                     'гандикап не съел блок tuning (габарит и масса целы)',
                     'half_length %.3f, масса %.0f'
                     % (live['half_length'], live['mass']))
        report.check(abs(car.handicap - 0.97) < 1e-9
                     and car.stats is not car.stats_base
                     and getattr(car.stats, 'tuning', None),
                     'классика читает те же замедленные характеристики',
                     'множитель %.4f, engine_force %.3f -> %.3f'
                     % (car.handicap, car.stats_base.engine_force,
                        car.stats.engine_force))
        # И главное: машина РЕАЛЬНО поехала медленнее. Ровно этого не было
        # видно по настройке, когда гандикап молчал. Мера — путь за три
        # секунды полного газа с одного и того же места.
        slow_dist = _throttle_distance(sim, car.slot)
        report.check(slow_dist < base_dist * 0.995,
                     'машина с гандикапом едет медленнее — замер, а не флаг',
                     '%.2f м против %.2f м за 3 с полного газа (-%.1f %%)'
                     % (slow_dist, base_dist,
                        100.0 * (1.0 - slow_dist / base_dist)))
        # 3. Трамплины в НАСТОЯЩЕЙ гонке, а не на стенде: поднимаем вторую
        #    симуляцию на трассе с трамплином и смотрим, что коробки в её
        #    мире стоят и что запись трассы их везёт. Проверять это на
        #    avenue было бы проверкой нуля: трамплинов там нет.
        jump_track = ramp_track()
        jump = Simulation(jump_track, dict(wanted, track=RAMP_TRACK), players)
        ramps = jump.track.to_client().get('ramps') or []
        expect = sum(len(r['boxes']) // RAMP_BOX_FLOATS for r in ramps)
        report.check(len(ramps) > 0 and expect > 0
                     and jump.rapier.ramp_boxes == expect,
                     'трамплины трассы стоят в мире настоящей гонки',
                     '%s: трамплинов %d, коробок %d из %d ожидаемых'
                     % (RAMP_TRACK, len(ramps), jump.rapier.ramp_boxes, expect))
    finally:
        if saved is None:
            os.environ.pop(rh.ENV_VAR, None)
        else:
            os.environ[rh.ENV_VAR] = saved


# ---------------------------------------------------------------------------
# Внешние воздействия: CarEffect (§12.25)
# ---------------------------------------------------------------------------
# Дверь, через которую в мир Rapier попадает всё, что действует на машину
# извне: бонусы сейчас, покрытие и выталкивание происшествий — следом. Дверь
# опасна ровно тем же, чем любая дверь: её можно не закрыть (доза осталась в
# памяти и действует вечно), можно пройти дважды (два применения сложились)
# и можно войти не в свою (воздействие на чужую машину). Проверки ниже
# закрывают каждую из трёх, и каждая доказывает, что СПОСОБНА покраснеть:
# рядом с ней стоит прогон, где охраняемое свойство нарушено нарочно.

def report_effects(report):
    print('  внешние воздействия (CarEffect, §12.25):')

    # 1. Доза живёт ОДИН шаг. Модуль стирает запись после шага; если он этого
    #    не сделает, машина с однажды выписанной раскруткой будет крутиться
    #    до конца гонки. Прогон с повторной выпиской — рядом: он показывает,
    #    что метод вообще видит вращение.
    car = Car()
    car.spin_up(20.0)
    car.dose(spin_rate=rh.SPIN_RATE, stun=1.0)
    before = car.yaw_open
    car.drive(0.0, 0.0, 0.0, 0.0)
    turned_once = car.yaw_open - before
    left = car.dose_row()
    before = car.yaw_open
    car.drive(0.0, 0.0, 0.0, 0.0)
    turned_after = car.yaw_open - before
    car.dose(spin_rate=rh.SPIN_RATE, stun=1.0)
    before = car.yaw_open
    car.drive(0.0, 0.0, 0.0, 0.0)
    turned_again = car.yaw_open - before
    want = rh.SPIN_RATE / 60.0
    report.check(abs(turned_once - want) < 0.02 * want,
                 'доза поворачивает ровно на spin_rate*dt',
                 '%.5f рад против %.5f' % (turned_once, want))
    report.check(not any(left), 'модуль стёр запись воздействия после шага',
                 'осталось: %s' % (['%.3f' % v for v in left] if any(left) else 'нули'))
    report.check(abs(turned_after) < 0.1 * want,
                 'на следующем шаге доза НЕ действует',
                 'без выписки %.5f рад, с выпиской %.5f'
                 % (turned_after, turned_again))
    report.check(abs(turned_again - want) < 0.02 * want,
                 'метод видит вращение, когда дозу выписали заново',
                 '%.5f рад' % turned_again)

    # 2. Два применения подряд НЕ складываются. Правило «новый буст не
    #    укорачивает уже идущий» (6.3) закрывает и это: берётся максимум.
    car = Car()
    car.dose(boost_add=1.0)
    car.drive(0.0, 0.0, 0.0, 0.0)
    one = car.g(OUT.BOOST_TIME)
    car.dose(boost_add=1.0)
    car.drive(0.0, 0.0, 0.0, 0.0)
    twice = car.g(OUT.BOOST_TIME)
    car.dose(boost_add=2.0)
    car.drive(0.0, 0.0, 0.0, 0.0)
    bigger = car.g(OUT.BOOST_TIME)
    report.check(twice <= one and twice > 0.0,
                 'две дозы буста подряд не складываются',
                 'после первой %.3f с, после второй %.3f с' % (one, twice))
    report.check(bigger > twice,
                 'но большая доза буст ПРОДЛЕВАЕТ (метод способен увидеть рост)',
                 '%.3f -> %.3f с' % (twice, bigger))

    # 3. Воздействие адресуется по своей записи. Адресата в структуре нет
    #    намеренно: попасть в чужую машину можно только написав в её запись.
    h = host()
    h.reset(0)
    h.add_ground(3000.0, rh.TRACK_FRICTION)
    h.tuning_preset(0)
    h.set_tuning(rh.car_tuning(car_spec(), 'arcade'))
    tune = h.tuning_values()
    rest = tune['wheel_radius'] + tune['suspension_rest'] + tune['half_height'] * 0.2
    a = h.spawn_car(0.0, rest, 0.0, 0.0)
    b = h.spawn_car(0.0, rest, 30.0, 0.0)
    for _ in range(rh.SETTLE_TICKS):
        h.step(1)
    yaw_a0 = h.outputs[a * OUT.FLOATS + OUT.YAW]
    yaw_b0 = h.outputs[b * OUT.FLOATS + OUT.YAW]
    h._sync()
    h.effects[a * EFF.FLOATS + EFF.SPIN_RATE] = rh.SPIN_RATE
    h.step(1)
    moved_a = abs(h.outputs[a * OUT.FLOATS + OUT.YAW] - yaw_a0)
    moved_b = abs(h.outputs[b * OUT.FLOATS + OUT.YAW] - yaw_b0)
    report.check(moved_a > 0.9 * want and moved_b < 1e-4,
                 'доза действует только на свою машину',
                 'своя %.5f рад, соседняя %.7f рад' % (moved_a, moved_b))

    # 4. ПОПАДАНИЕ НЕ ОПЛАЧИВАЕТ ЗАНОС. Раскрутка глушит ввод, а шаг 15
    #    видит отпущенный ручник как «занос закончен» — то есть без
    #    drift_reset ракета в спину стала бы способом обналичить копилку.
    #    У классики от этого спасает items._apply_hit, который обнуляет
    #    заряд руками; здесь то же самое делает доза.
    def hit_run(with_reset):
        # Копим занос ровно тем же пилотом, каким это делают проверки
        # «честный занос платит все три уровня»: сравнивать имеет смысл
        # только одинаковое.
        c = Car()
        c.spin_up(22.0)
        c.yaw_open = 0.0
        c._prev_yaw = c.g(OUT.YAW)
        pilot = pilot_arc(13.0)
        for i in range(int(60 * 5.0)):
            v_fwd, v_lat = c.v_fwd_lat()
            thr, brk, steer, hb = pilot(i, c, v_fwd, v_lat)
            c.drive(thr, brk, steer, hb)
        charged = c.g(OUT.DRIFT_CHARGE)
        best = 0.0
        for _ in range(int(60 * 1.5)):
            if with_reset:
                c.dose(spin_rate=rh.SPIN_RATE, stun=1.0, drift_reset=1.0)
            else:
                c.dose(spin_rate=rh.SPIN_RATE, stun=1.0)
            c.drive(0.0, 0.0, 0.0, 0.0)
            if c.g(OUT.BOOST_TIME) > best:
                best = c.g(OUT.BOOST_TIME)
        return charged, best

    charged, paid = hit_run(True)
    charged_bad, paid_bad = hit_run(False)
    report.check(charged > L1 and paid == 0.0,
                 'попадание сжигает занос, а не оплачивает его',
                 'копилка %.2f с (уровень 1 с %.2f), выплата %.2f с'
                 % (charged, L1, paid))
    report.check(paid_bad > 0.0,
                 'без drift_reset дыра открыта — проверка способна покраснеть',
                 'без сброса выплачено %.2f с' % paid_bad)

    # 5. stun действительно отнимает управление.
    car = Car()
    for _ in range(60):
        car.dose(stun=1.0)
        car.drive(1.0, 0.0, 0.0, 0.0)
    stunned = car.g(OUT.SPEED)
    car = Car()
    for _ in range(60):
        car.drive(1.0, 0.0, 0.0, 0.0)
    free = car.g(OUT.SPEED)
    report.check(stunned < 0.05 * free and free > 5.0,
                 'stun глушит ввод: секунда полного газа не сдвигает',
                 '%.3f м/с против %.2f м/с без дозы' % (stunned, free))

    # 6. grip_drop режет сцепление: то же покрытие, что масло у классики
    #    (OIL_GRIP = 0.45, то есть доза 0.55). В дуге машина уезжает шире.
    def arc_yaw(drop):
        c = Car()
        c.spin_up(20.0)
        c.yaw_open = 0.0
        c._prev_yaw = c.g(OUT.YAW)
        for _ in range(90):
            if drop:
                c.dose(grip_drop=drop)
            c.drive(0.6, 0.0, 1.0, 0.0)
        return abs(c.yaw_open)

    dry = arc_yaw(0.0)
    oily = arc_yaw(0.55)
    report.check(oily < 0.8 * dry,
                 'grip_drop роняет сцепление: в дуге машину несёт',
                 'поворот за 1,5 с: %.2f рад по сухому, %.2f по маслу'
                 % (dry, oily))

    # 7. speed_drop снимает заказанную долю продольной скорости. Одно поле
    #    на «Грозу» и на тряску по обломкам.
    car = Car()
    car.spin_up(25.0)
    v0 = car.v_fwd_lat()[0]
    car.dose(speed_drop=0.10)
    car.drive(0.0, 0.0, 0.0, 0.0)
    v_dosed = car.v_fwd_lat()[0]
    car2 = Car()
    car2.spin_up(25.0)
    v0b = car2.v_fwd_lat()[0]
    car2.drive(0.0, 0.0, 0.0, 0.0)
    v_free = car2.v_fwd_lat()[0]
    took = (v_free - v_dosed) / v0
    report.check(abs(took - 0.10) < 0.02,
                 'speed_drop снимает ровно заказанную долю',
                 'просили 10 %%, сняло %.1f %% (с %.2f до %.2f м/с)'
                 % (took * 100.0, v0, v_dosed))

    # 8. push и shift двигают тело на заказанное. С 12.28 их зовут завалы
    #    и удар о болванку, но проверка остаётся здесь и на самих числах:
    #    выше по тексту она одна умеет сказать, что метр — это метр.
    car = Car()
    px0 = car.g(OUT.PX)
    car.dose(push_x=5.0)
    car.drive(0.0, 0.0, 0.0, 0.0)
    vx = car.g(OUT.VX)
    car2 = Car()
    px2 = car2.g(OUT.PX)
    car2.dose(shift_x=2.0)
    car2.drive(0.0, 0.0, 0.0, 0.0)
    moved = car2.g(OUT.PX) - px2
    report.check(abs(vx - 5.0) < 0.6,
                 'push задаёт приращение скорости в м/с',
                 'просили +5.00, вышло %+.3f м/с' % vx)
    report.check(abs(moved - 2.0) < 0.05,
                 'shift задаёт приращение позиции в метрах',
                 'просили +2.000, вышло %+.4f м (было %.3f)' % (moved, px0))

    # 9. У ХОЗЯИНА ДВЕ ПОЛОВИНЫ, и обе обязаны писать одно и то же. Сервер
    #    выписывает дозу в RapierRace._write_effect, браузер — в
    #    RapierLocal._writeEffect; всё, что влияет на движение, считается
    #    на обеих сторонах или не считается нигде. Проверка текстовая, и
    #    этого ей достаточно: она ловит ровно тот случай, на котором проект
    #    уже горел, — «серверная половина крюка есть, клиентской нет».
    #    Чего она не ловит: расхождения В ЧИСЛАХ. От него защищает то, что
    #    числа у обеих сторон импортируются, а не переписываются.
    py = open(os.path.join(BASE_DIR, 'game', 'rapier_host.py'),
              encoding='utf-8').read()
    js = open(os.path.join(BASE_DIR, 'static', 'js', 'rapier_host.js'),
              encoding='utf-8').read()
    names = [n for n in dir(EFF) if n.isupper() and not n.startswith('_')
             and n not in ('SIZE', 'FLOATS')]
    py_used = set(n for n in names if ('E.%s]' % n) in py)
    js_used = set(n for n in names if ('E.%s]' % n) in js)
    # BOOST_ADD — единственное поле, которое выписывает только сервер, и это
    # не забывчивость браузера. Остаток буста держит МОДУЛЬ, а снапшот везёт
    # про него один бит (раздел 5.3): выдать секунды клиенту неоткуда, он
    # умеет лишь продлить то, что сервер объявил идущим. Продление он делает
    # там, где буст на самом деле живёт, — в слоте отката CarSave, тем же
    # HOLDOVER-правилом, что и классика. Проверка ниже и требует обоих:
    # сервер пишет дозу, браузер — поле слота.
    report.check(py_used - js_used == {'BOOST_ADD'} and js_used - py_used == set(),
                 'сервер и браузер выписывают одни и те же поля дозы',
                 'сервер: %s | браузер: %s'
                 % (', '.join(sorted(py_used)) or 'ни одного',
                    ', '.join(sorted(js_used)) or 'ни одного'))
    report.check('a.BOOST_TIME]' in js,
                 'браузерная половина буста на месте: продление через CarSave',
                 'applyAuthoritative кладёт state.boostTime в слот отката')
    report.check('abi.CarEffect' in py and 'CarEffect' in js
                 and 'rp_effects_ptr' in py and 'rp_effects_ptr' in js,
                 'обе половины берут раскладку у генератора, а не руками',
                 'смещений руками ни там, ни там')


# ---------------------------------------------------------------------------
# Дорога: поток и происшествия через ту же дверь (§12.28)
# ---------------------------------------------------------------------------
# Болванок и корпусов в мире модуля нет и не появилось. Всё, что дорога
# делает с гонщиком, переводится в дозу CarEffect: покрытие в grip_drop,
# тряска по обломкам в speed_drop, завал и удар о болванку в shift_*/push_*.
# Значит проверять надо не механику модуля (её держит report_effects), а
# ПЕРЕВОД: что хозяин дозу действительно выписывает и что в игре от неё
# что-то меняется. У каждой проверки рядом стоит прогон, где перевод
# сломан нарочно, — иначе она была бы неотличима от проверки, которая
# зеленеет на пустом месте (§12.20, урок про шаткую проверку).

def _road_scene(events='rare', traffic='off', count=1):
    """Гонка при physics=rapier с управляемой дорогой."""
    from game.sim import Simulation
    players = [{'slot': i, 'name': 'ABCDEFGH'[i], 'car': CAR_ID}
               for i in range(count)]
    sim = Simulation(track(), {
        'track': TRACK_ID, 'laps': 9, 'mirror': False, 'items_enabled': False,
        'collisions': True, 'traffic': traffic, 'events': events,
        'handicap': False, 'weather': 'dry', 'physics': 'arcade'}, players)
    # Сами происшествия расставляем руками: случайные тут только мешали бы.
    sim.road._cooldown = 1e9
    return sim


def _place(sim, kind, arc, lateral, phase, slot_no=0):
    """Зажечь происшествие в заданной точке. Габариты — из game/events.py."""
    from game import events as ev
    road = sim.road
    slot = road.pool[slot_no]
    half_len, half_width, _lane = ev.GEOMETRY[kind]
    slot.kind = kind
    slot.phase = phase
    slot.arc = arc % sim.track.length
    slot.lateral = lateral
    slot.half_len = half_len
    slot.half_width = half_width
    slot.track_half_width = 7.0
    slot.timer = 1e6
    slot.hit_mask = 0
    slot.alive = True
    if slot not in road.live:
        road.live.append(slot)
    road._rebuild_zone()
    return slot


def _hold(sim, buttons, ticks, slot=0):
    for _ in range(ticks):
        sim.set_input(slot, sim.tick_no + 1, buttons)
        sim.tick()


def _speed(state):
    return math.hypot(state.vx, state.vz)


def _spin_up(sim, speed, guard=1500):
    state = sim.cars[0].state
    n = 0
    while _speed(state) < speed and n < guard:
        _hold(sim, rh.BTN_THROTTLE, 1)
        n += 1
    return _speed(state)


def report_road(report):
    print('  дорога: покрытие, завалы и поток (§12.28):')
    from game import events as ev
    saved = os.environ.get(rh.ENV_VAR)
    os.environ[rh.ENV_VAR] = rh.RAPIER
    try:
        _report_road(report, ev)
    finally:
        if saved is None:
            os.environ.pop(rh.ENV_VAR, None)
        else:
            os.environ[rh.ENV_VAR] = saved


def _report_road(report, ev):
    # 1. МАСЛО. Мерить надо ПОВОРОТ, а не тормозной путь. Тормозной путь на
    #    масле НЕ МЕНЯЕТСЯ — и это не особенность Rapier, а свойство обеих
    #    физик: масло режет grip_step и drift_grip_step, то есть БОКОВОЕ
    #    сцепление, а тормоз считается отдельно. Замерено спина к спине:
    #    классика теряет 19,01 м/с за 40 тиков и по сухому, и на масле;
    #    Rapier — 9,05 и там и там. Проверка, построенная на торможении,
    #    зеленела бы на сломанной двери, то есть охраняла бы ничего.
    #
    #    Поворот же от сцепления зависит целиком, и тем же замером мерили
    #    дозу в §12.27: за 1,5 с руля 1,93 рад по сухому против 0,92 на
    #    масле. Порог отсюда: сцепление падает в OIL_GRIP = 0,45 раза,
    #    значит поворот обязан упасть заметно ниже сухого; с запасом —
    #    меньше 0,75 от него.
    #
    #    Пятно масла настоящее, из GEOMETRY, а не дорисованное: шесть штук
    #    (два ряда по три) кроют 28 м дуги и всю ширину полотна, дальше
    #    машина за секунду руля не уедет.
    def turn_run(oil, sabotage=False):
        sim = _road_scene()
        state = sim.cars[0].state
        _spin_up(sim, 20.0)
        arc, lat = sim.road._pose(state)
        if oil:
            k = 0
            for ds in (3.0, 17.0):
                for dl in (-4.5, 0.0, 4.5):
                    _place(sim, ev.KIND_OIL, arc + ds, lat + dl,
                           ev.PHASE_ACTIVE, k)
                    k += 1
        if sabotage:
            # Дверь закрыта нарочно: покрытие под машиной есть, дозы нет.
            sim.road.grip_scale = lambda st: 1.0
        yaw0 = state.yaw
        _hold(sim, rh.BTN_THROTTLE | rh.BTN_LEFT, 60)
        turned = state.yaw - yaw0
        while turned > math.pi:
            turned -= 2.0 * math.pi
        while turned < -math.pi:
            turned += 2.0 * math.pi
        return abs(turned)

    dry = turn_run(False)
    wet = turn_run(True)
    broken = turn_run(True, sabotage=True)
    report.check(wet < dry * 0.75,
                 'на масле машина не доворачивает: grip_drop доехал до колёс',
                 'за секунду руля по сухому %.3f рад, на масле %.3f '
                 '(доля %.2f при потолке 0,75)' % (dry, wet, wet / dry))
    report.check(abs(broken - dry) < 0.02 * dry,
                 'та же проверка краснеет, если дозу не выписывать',
                 'с закрытой дверью на масле доворот %.3f рад — тот же, '
                 'что по сухому (%.3f)' % (broken, dry))

    # 2. ОБЛОМКИ. Тряска снимает долю ПРОДОЛЬНОЙ скорости за шаг
    #    (DEBRIS_DAMP = 0,9960), поэтому катящаяся машина обязана потерять
    #    за 60 тиков примерно 1 - 0,996^60 = 21 % сверх обычного наката.
    #    Порог: не меньше половины этого, то есть 10 %.
    def coast_run(debris):
        sim = _road_scene()
        state = sim.cars[0].state
        v0 = _spin_up(sim, 30.0)
        arc, lat = sim.road._pose(state)
        if debris:
            for k in range(4):
                _place(sim, ev.KIND_EXPLOSION, arc + k * 10.0 - 4.0, lat,
                       ev.PHASE_DEBRIS, k)
        _hold(sim, 0, 60)
        return v0, _speed(state)

    v0_clean, clean = coast_run(False)
    v0_deb, deb = coast_run(True)
    lost = (clean - deb) / v0_deb
    report.check(lost > 0.10,
                 'по обломкам машина теряет ход: speed_drop доехал',
                 'накат с %.1f м/с: чисто %.2f, по обломкам %.2f — '
                 'на %.1f %% скорости больше (расчётный потолок 21 %%)'
                 % (v0_deb, clean, deb, lost * 100.0))

    # 3. ТВЁРДОЕ ПРЕПЯТСТВИЕ. Проверка одна и грубая нарочно: машина, которая
    #    едет в завал, обязана в нём ОСТАНОВИТЬСЯ, а не проехать сквозь.
    #    Меряем дугу относительно центра завала: с механикой нос упирается
    #    примерно за полдлины машины плюс полдлины завала (4,4 м), без неё
    #    машина оказывается далеко за ним.
    def ram_run(sabotage=False):
        sim = _road_scene()
        state = sim.cars[0].state
        _spin_up(sim, 16.0)
        arc, lat = sim.road._pose(state)
        wreck_arc = arc + 30.0
        _place(sim, ev.KIND_WRECK, wreck_arc, lat, ev.PHASE_ACTIVE)
        if sabotage:
            sim.road.solid_resolve = lambda st, out: False
        _hold(sim, rh.BTN_THROTTLE, 180)
        now, _lat = sim.road._pose(state)
        return now - wreck_arc

    stopped = ram_run()
    through = ram_run(sabotage=True)
    report.check(stopped < 0.0,
                 'завал не пускает: shift_* и push_* доехали до тела',
                 'нос встал на %.2f м ОТ центра завала (габарит 4,4 м)'
                 % stopped)
    report.check(through > 20.0,
                 'та же проверка краснеет, если выталкивание не выписывать',
                 'с закрытой дверью машина уезжает на %.1f м ЗА завал'
                 % through)

    # 4. ПОТОК. Болванки в мире модуля нет, её удар считает хозяин и кладёт
    #    в отложенную дозу. Проверяем обе половины сразу: болванку толкнуло
    #    на месте, гонщику записалось в дозу.
    sim = _road_scene(events='off', traffic='dense', count=2)
    race = sim.rapier
    car = sim.cars[0]
    unit = sim.traffic.cars[0]
    _hold(sim, rh.BTN_THROTTLE, 30)
    st = car.state
    unit.state.x = st.x + 1.2
    unit.state.z = st.z
    unit.state.yaw = st.yaw
    unit.state.vx = 0.0
    unit.state.vz = 0.0
    unit_before = (unit.state.x, unit.state.z)
    pend = race.slots[0][7]
    pend[0] = pend[1] = pend[2] = pend[3] = 0.0
    sim._resolve_collisions()
    unit_moved = math.hypot(unit.state.x - unit_before[0],
                            unit.state.z - unit_before[1])
    report.check(unit_moved > 1e-6 and (pend[0] or pend[1]),
                 'въехал в болванку: её толкнуло, гонщику записалась доза',
                 'болванку сдвинуло на %.3f м, гонщику отложено '
                 '%.3f м сдвига и %.3f м/с толчка'
                 % (unit_moved, math.hypot(pend[0], pend[1]),
                    math.hypot(pend[2], pend[3])))

    # 5. ...а гонщик против гонщика через этот проход НЕ считается: их уже
    #    посчитал солвер внутри шага, и второй раз означал бы двойной толчок.
    #    Эта проверка охраняет аргумент skip_before и обязана краснеть, если
    #    его убрать, — проверено подстановкой skip_before=0 ниже.
    other = sim.cars[1]
    other.state.x = st.x + 1.0
    other.state.z = st.z
    other.state.yaw = st.yaw
    for u in sim.traffic.cars:          # болванок убрать с дороги совсем
        u.state.x = st.x + 900.0
    pend[0] = pend[1] = pend[2] = pend[3] = 0.0
    sim._resolve_collisions()
    quiet = not (pend[0] or pend[1] or pend[2] or pend[3])
    states = [sim.cars[0].state, sim.cars[1].state]
    stats = [sim.cars[0].stats, sim.cars[1].stats]
    before_x = states[0].x
    physics.resolve_collisions(states, stats, 2, 0)
    report.check(quiet and abs(states[0].x - before_x) > 1e-9,
                 'гонщик против гонщика через хозяина не считается дважды',
                 'проход хозяина молчит, а тот же код при skip_before=0 '
                 'сдвинул бы на %.5f м за тик' % abs(states[0].x - before_x))

    # 6. Доза от болванки живёт ОДИН шаг и не складывается: её забирает
    #    первый же _write_effect и обнуляет запись.
    entry = race.slots[0]
    e = entry[6]
    host = race.host
    host._sync()
    for k in range(EFF.FLOATS):
        host.effects[e + k] = 0.0
    entry[7][0] = 0.5
    entry[7][2] = 3.0
    race._write_effect(entry, host.effects, host.outputs, DT)
    first = (host.effects[e + EFF.SHIFT_X], host.effects[e + EFF.PUSH_X])
    for k in range(EFF.FLOATS):
        host.effects[e + k] = 0.0
    race._write_effect(entry, host.effects, host.outputs, DT)
    again = (host.effects[e + EFF.SHIFT_X], host.effects[e + EFF.PUSH_X])
    report.check(abs(first[0] - 0.5) < 1e-6 and abs(first[1] - 3.0) < 1e-6
                 and again == (0.0, 0.0),
                 'отложенная доза уходит в модуль ровно один раз',
                 'первый шаг %.3f м / %.3f м/с, второй %.3f / %.3f'
                 % (first[0], first[1], again[0], again[1]))

    # 8. СЕТЬ ПОД ПЕРЕВЁРНУТОЙ МАШИНОЙ. Она появилась вместе с потоком и
    #    не является его частью: кузов у Rapier настоящий, после жёсткого
    #    удара он ложится на крышу или на бок, и оттуда не выбраться ни
    #    игроку, ни боту. До 12.28 это не всплывало, потому что в замерах
    #    кувырка не случалось; с потоком на дороге две гонки из пяти висли
    #    до конца восьмиминутного потолка. Проверка кладёт машину на крышу
    #    руками и требует, чтобы она вернулась — и чтобы вернулась НЕ
    #    РАНЬШЕ срока, иначе сеть срабатывала бы на каждом прыжке.
    sim = _road_scene(events='off', traffic='off')
    race = sim.rapier
    entry = race.slots[0]
    host = race.host
    st = sim.cars[0].state
    _hold(sim, 0, 5)
    ground = sim.track.surface(st.x, st.z, st.sample_idx)[3]
    host._sync()
    b = entry[1] * abi.CarSave.FLOATS
    for k in range(abi.CarSave.FLOATS):
        host.saves[b + k] = 0.0
    host.saves[b + abi.CarSave.PX] = st.x
    host.saves[b + abi.CarSave.PY] = ground + 0.6
    host.saves[b + abi.CarSave.PZ] = st.z
    host.saves[b + abi.CarSave.QX] = 1.0      # переворот на 180° вокруг X
    host.car_restore(entry[1])
    out = host.outputs
    _hold(sim, rh.BTN_THROTTLE, 120)          # две секунды — рано
    early = out[entry[3] + OUT.WHEELS_ON_GROUND]
    _hold(sim, rh.BTN_THROTTLE, 150)          # ещё 2,5 с — сеть сработала
    late = out[entry[3] + OUT.WHEELS_ON_GROUND]
    report.check(early < 2.0,
                 'за две секунды на крыше сеть не срабатывает: прыжок ей не '
                 'повод', 'колёс на полотне %.0f' % early)
    report.check(late >= 2.0,
                 'перевёрнутая машина возвращается на ось, а не висит до '
                 'конца гонки', 'колёс на полотне %.0f' % late)

    # 9. Числа покрытия у браузера те же, что у сервера. Копия в net.js
    #    заведена не сегодня, но с 12.28 от неё зависит уже и предсказание
    #    под Rapier: разъедься она — машина поехала бы по разному маслу на
    #    двух сторонах.
    js = open(os.path.join(BASE_DIR, 'static', 'js', 'net.js'),
              encoding='utf-8').read()
    pairs = (('ROAD_OIL_GRIP', ev.OIL_GRIP), ('ROAD_DEBRIS_GRIP', ev.DEBRIS_GRIP),
             ('ROAD_DEBRIS_DAMP', ev.DEBRIS_DAMP),
             ('ROAD_SOLID_BOUNCE', ev.SOLID_BOUNCE))
    bad = []
    for name, want in pairs:
        found = re.search(r'const %s = ([0-9.]+)' % name, js)
        if found is None or abs(float(found.group(1)) - want) > 1e-12:
            bad.append('%s: %s против %r' % (name, found and found.group(1), want))
    report.check(not bad, 'числа покрытия у браузера те же, что у сервера',
                 '; '.join(bad) if bad else ', '.join(n for n, _v in pairs))
    report.check('roadSolve' in js and 'roadDose' in js,
                 'браузерная половина дороги на месте: roadSolve и roadDose',
                 'net.js зовёт их из stepLocal и из переигровки')


# ---------------------------------------------------------------------------
# Модель шин симулятора (§12.32)
# ---------------------------------------------------------------------------
#
# Пороги здесь выведены из РАЗРЫВА между двумя состояниями, а не подобраны
# под прогон (§12.20). У каждой проверки рядом измерено, что она краснеет:
# нейтральное значение `tire_peak_slip = 0` — это ровно прежняя физика, и
# сравнение идёт с ней же, тем же кодом, в том же прогоне. Поэтому «красное»
# тут не подсаженная поломка, а второе честное состояние мира.

TIRE_CARS = ('hatch', 'muscle', 'buggy', 'van', 'wedge')

# Пик обязан быть выше полки за пиком. Нейтраль (потолок трения) даёт
# отношение 0,94-0,97 — сила там не падает, а слегка РАСТЁТ вместе с Fz.
# Порог 1,05 лежит посреди пустого промежутка между 0,97 и 1,10 (худшая
# машина с моделью), то есть это не подгонка, а середина разрыва.
TIRE_PEAK_MIN = 1.05
# Подъём: на трети угла пика сила обязана быть заметно меньше пиковой.
# Нейтраль даёт ровно 1,00 (полка с первого градуса).
TIRE_RISE_MAX = 0.80
# Круг сцепления. Нейтраль: боковая падает на 0,2 % (то есть не падает
# вовсе), а тормоз обваливается до 16 % прямого. Модель обязана дать и то,
# и другое: боковая платит, но и тормоз остаётся тормозом.
TIRE_CIRCLE_LAT_DROP = 0.05
TIRE_CIRCLE_BRAKE_KEEP = 0.50
# Поперечный перенос веса. Нейтраль 1,10-1,16 (встроенный контроллер кладёт
# боковую силу почти на высоте центра масс, roll_influence = 0,1), модель
# 1,80-3,34. Порог посреди разрыва.
TIRE_ROLL_MIN = 1.5


class SimCar(object):
    """Машина каталога на ровной площадке в пресете «симулятор».

    Отдельный класс, а не ``Car``: тому нужен аркадный пресет и одна
    машина, а здесь нужны все пять и переключатель модели шин.
    """

    def __init__(self, car_id, tire=True, over=None):
        h = host()
        h.reset(1)
        h.add_ground(6000.0, rh.TRACK_FRICTION)
        h.tuning_preset(1)
        values = rh.car_tuning(_tire_catalog().get(car_id), 'sim')
        if not tire:
            values['tire_peak_slip'] = 0.0      # нейтраль: прежняя физика
        if over:
            values.update(over)
        h.set_tuning(values)
        tune = h.tuning_values()
        self.t = tune
        self.mass = tune['mass']
        rest = (tune['wheel_radius'] + tune['suspension_rest']
                + tune['half_height'] * 0.2)
        self.h = h
        self.idx = h.spawn_car(0.0, rest, 0.0, 0.0)
        self.base = self.idx * OUT.FLOATS
        for _ in range(rh.SETTLE_TICKS):
            self.drive(0.0, 0.0, 0.0, 0.0)

    def g(self, field):
        return self.h.outputs[self.base + field]

    def wheel(self, w, field):
        return self.h.outputs[self.base + OUT.WHEELS
                              + w * OUT.WHEELS_STRIDE + field]

    def drive(self, throttle, brake, steer, handbrake):
        self.h.set_input(self.idx, throttle, brake, steer, handbrake)
        self.h.step(1)

    def dose(self, **fields):
        h = self.h
        h._sync()
        base = self.idx * EFF.FLOATS
        for name, value in fields.items():
            h.effects[base + getattr(EFF, name.upper())] = value

    def axes(self):
        yaw = self.g(OUT.YAW)
        fx, fz = math.sin(yaw), math.cos(yaw)
        return (fx, fz), (fz, -fx)              # вперёд, налево

    def vfl(self):
        (fx, fz), (lx, lz) = self.axes()
        vx, vz = self.g(OUT.VX), self.g(OUT.VZ)
        return vx * fx + vz * fz, vx * lx + vz * lz

    def beta(self):
        v_fwd, v_lat = self.vfl()
        if abs(v_fwd) < 0.5:
            return 0.0
        return math.degrees(math.atan2(v_lat, abs(v_fwd)))

    def spin_up(self, speed, guard=4000):
        n = 0
        while self.g(OUT.SPEED) < speed and n < guard:
            self.drive(1.0, 0.0, 0.0, 0.0)
            n += 1
        for _ in range(8):
            self.drive(0.0, 0.0, 0.0, 0.0)

    def slide(self, deg):
        """Поставить телу боковую скорость под угол ``deg`` (налево).

        Толчком-воздействием, а не подменой состояния: своей двери «задать
        скорость» у модуля нет, а ``push_*`` — это ровно приращение
        скорости в м/с (§12.27).
        """
        (fx, fz), (lx, lz) = self.axes()
        v_fwd, v_lat = self.vfl()
        d = v_fwd * math.tan(math.radians(deg)) - v_lat
        self.dose(push_x=lx * d, push_z=lz * d)
        self.drive(0.0, 0.0, 0.0, 0.0)


_TIRE_CATALOG = None


def _tire_catalog():
    global _TIRE_CATALOG
    if _TIRE_CATALOG is None:
        _TIRE_CATALOG = load_cars(os.path.join(BASE_DIR, 'content',
                                               'cars.json'))
    return _TIRE_CATALOG


def tire_force(car_id, deg, tire=True, brake=0.0, throttle=0.0, speed=20.0,
               over=None):
    """Боковая и продольная сила на кузове за один тик под углом увода.

    Меряется ПРИРАЩЕНИЕМ СКОРОСТИ кузова, а не тем, что модуль сам про
    себя написал в `side_impulse`: иначе проверка сверяла бы модель с её
    же отчётом. Возврат: (угол, боковая Н, продольная Н, m*g).
    """
    car = SimCar(car_id, tire, over)
    car.spin_up(speed)
    car.slide(deg)
    (fx, fz), (lx, lz) = car.axes()
    vx0, vz0 = car.g(OUT.VX), car.g(OUT.VZ)
    alpha = abs(car.beta())
    car.drive(throttle, brake, 0.0, 0.0)
    dl = (car.g(OUT.VX) - vx0) * lx + (car.g(OUT.VZ) - vz0) * lz
    df = (car.g(OUT.VX) - vx0) * fx + (car.g(OUT.VZ) - vz0) * fz
    return alpha, -car.mass * dl / DT, car.mass * df / DT, car.mass * 9.81


# Семейство простых водителей для ловимости. Занос считается пойманным,
# если его поймал ХОТЬ ОДИН: метрика меряет машину, а не автопилот, и
# «правильные действия» — это право водителя выбрать действие. Сигнал у
# всех один и физический: угол увода ПЕРЕДНЕЙ оси, в нём уже сидит
# рыскание, поэтому демпфирование берётся из физики, а не из коэффициента.
TIRE_DRIVERS = ((0.0, True), (0.5, True), (1.0, True), (1.0, False))


def tire_caught(car_id, deg, tire=True, speed=22.0, over=None, ticks=300):
    for gain, hold in TIRE_DRIVERS:
        car = SimCar(car_id, tire, over)
        car.spin_up(speed)
        car.slide(deg)
        tune = car.t
        good = 0
        spun = False
        for _ in range(ticks):
            v_fwd, v_lat = car.vfl()
            if abs(v_fwd) < 0.5 or abs(car.beta()) > 89.0:
                spun = True
                break
            front = math.atan2(v_lat + car.g(OUT.WY) * tune['axle_z'],
                               abs(v_fwd))
            sp = car.g(OUT.SPEED)
            avail = tune['steer_max'] * (
                1.0 - tune['steer_speed_falloff']
                * min(1.0, sp / tune['max_speed']))
            steer = max(-1.0, min(1.0, gain * front / max(avail, 1e-3)))
            car.drive(max(0.0, min(0.6, (speed - sp) * 0.3)) if hold else 0.0,
                      0.0, steer, 0.0)
            if abs(car.beta()) < 2.0 and abs(car.g(OUT.WY)) < 0.15:
                good += 1
                if good >= 12:
                    return True
            else:
                good = 0
        if spun:
            continue
    return False


def report_tires(report):
    """Кривая увода, круг сцепления, перенос веса и ловимость (§12.32)."""
    print('  модель шин симулятора:')

    # --- 1. у кривой есть ПИК, а не полка ---------------------------------
    worst_peak = (None, 1e9)
    worst_rise = (None, 0.0)
    for car_id in TIRE_CARS:
        peak_deg = math.degrees(
            rh.car_tuning(_tire_catalog().get(car_id), 'sim')['tire_peak_slip'])
        at_peak = tire_force(car_id, peak_deg)[1]
        after = tire_force(car_id, peak_deg + 20.0)[1]
        rise = tire_force(car_id, peak_deg / 3.0)[1]
        if at_peak / after < worst_peak[1]:
            worst_peak = (car_id, at_peak / after)
        if rise / at_peak > worst_rise[1]:
            worst_rise = (car_id, rise / at_peak)
    report.check(worst_peak[1] >= TIRE_PEAK_MIN,
                 'за пиком боковая сила ПАДАЕТ у всех пяти машин',
                 'худшая %s: пик/(пик+20 град) = %.3f при пороге %.2f'
                 % (worst_peak[0], worst_peak[1], TIRE_PEAK_MIN))
    report.check(worst_rise[1] <= TIRE_RISE_MAX,
                 'до пика боковая сила РАСТЁТ, а не стоит полкой',
                 'худшая %s: (пик/3)/пик = %.3f при пороге %.2f'
                 % (worst_rise[0], worst_rise[1], TIRE_RISE_MAX))
    # то же на нейтрали — доказательство, что проверка способна покраснеть
    peak_deg = math.degrees(
        rh.car_tuning(_tire_catalog().get('hatch'), 'sim')['tire_peak_slip'])
    flat_peak = tire_force('hatch', peak_deg, tire=False)[1]
    flat_after = tire_force('hatch', peak_deg + 20.0, tire=False)[1]
    flat_rise = tire_force('hatch', peak_deg / 3.0, tire=False)[1]
    report.check(flat_peak / flat_after < TIRE_PEAK_MIN
                 and flat_rise / flat_peak > TIRE_RISE_MAX,
                 'обе проверки кривой краснеют на нейтрали (tire_peak_slip=0)',
                 'нейтраль: пик/(пик+20) = %.3f, (пик/3)/пик = %.3f — полка'
                 % (flat_peak / flat_after, flat_rise / flat_peak))

    # --- 2. круг сцепления: бюджет один на двоих --------------------------
    hatch_peak = peak_deg
    lat_free = tire_force('hatch', hatch_peak)[1]
    lat_brake, fwd_brake = tire_force('hatch', hatch_peak, brake=1.0)[1:3]
    fwd_straight = tire_force('hatch', 0.5, brake=1.0)[2]
    drop = 1.0 - lat_brake / lat_free
    keep = fwd_brake / fwd_straight
    report.check(drop >= TIRE_CIRCLE_LAT_DROP,
                 'торможение в пол отнимает боковое сцепление',
                 'боковая %.0f -> %.0f Н (-%.1f %%) при пороге %.0f %%'
                 % (lat_free, lat_brake, drop * 100.0,
                    TIRE_CIRCLE_LAT_DROP * 100.0))
    report.check(keep >= TIRE_CIRCLE_BRAKE_KEEP,
                 'и при этом тормоз остаётся тормозом, а не пропадает',
                 'в дуге %.0f %% от прямого торможения при пороге %.0f %%'
                 % (keep * 100.0, TIRE_CIRCLE_BRAKE_KEEP * 100.0))
    n_lat = tire_force('hatch', hatch_peak, tire=False)[1]
    n_lat_b, n_fwd_b = tire_force('hatch', hatch_peak, tire=False, brake=1.0)[1:3]
    n_fwd_s = tire_force('hatch', 0.5, tire=False, brake=1.0)[2]
    report.check(1.0 - n_lat_b / n_lat < TIRE_CIRCLE_LAT_DROP
                 and n_fwd_b / n_fwd_s < TIRE_CIRCLE_BRAKE_KEEP,
                 'обе проверки круга краснеют на нейтрали',
                 'нейтраль: боковая -%.1f %%, тормоз %.0f %% от прямого'
                 % ((1.0 - n_lat_b / n_lat) * 100.0, n_fwd_b / n_fwd_s * 100.0))

    # --- 3. поперечный перенос веса ---------------------------------------
    def roll_ratio(car_id, tire=True, over=None):
        car = SimCar(car_id, tire, over)
        car.spin_up(20.0)
        car.slide(20.0)
        for _ in range(30):
            car.drive(0.0, 0.0, 0.0, 0.0)
        left = (car.wheel(0, abi.WheelOut.SUSPENSION_FORCE)
                + car.wheel(2, abi.WheelOut.SUSPENSION_FORCE))
        right = (car.wheel(1, abi.WheelOut.SUSPENSION_FORCE)
                 + car.wheel(3, abi.WheelOut.SUSPENSION_FORCE))
        return max(left, right) / max(min(left, right), 1.0)

    worst_roll = min((roll_ratio(c), c) for c in TIRE_CARS)
    report.check(worst_roll[0] >= TIRE_ROLL_MIN,
                 'в дуге машина опирается на внешние колёса',
                 'худшая %s: нагруженная пара к разгруженной %.2f при '
                 'пороге %.2f' % (worst_roll[1], worst_roll[0], TIRE_ROLL_MIN))
    flat_roll = roll_ratio('hatch', tire=False)
    report.check(flat_roll < TIRE_ROLL_MIN,
                 'проверка переноса краснеет на нейтрали',
                 'нейтраль: %.2f — поперечного переноса у встроенного '
                 'контроллера почти нет' % flat_roll)

    # --- 4. ловимость -----------------------------------------------------
    # Занос ВТРОЕ круче угла пика обязан ловиться. Тройка не подобрана: это
    # запас, ниже которого «предел найден» и «машина потеряна» сливаются в
    # один угол и водителю нечего ловить.
    bad = [c for c in TIRE_CARS
           if not tire_caught(c, 3.0 * math.degrees(
               rh.car_tuning(_tire_catalog().get(c), 'sim')['tire_peak_slip']))]
    report.check(not bad,
                 'занос втрое круче угла пика ловится встречным рулём',
                 (', '.join(bad) + ' — не поймали') if bad
                 else 'поймали все пять')
    # Красная половина: кривая-лезвие — пик на 1,7 град и за ним 5 % от
    # него. Правится ровно то, что проверка охраняет, — форма кривой.
    report.check(not tire_caught('hatch', 3.0 * hatch_peak,
                                 over={'tire_peak_slip': 0.03,
                                       'tire_tail': 0.05}),
                 'проверка ловимости краснеет на кривой-лезвии',
                 'при пике 0,03 рад и полке 0,05 тот же занос не ловит ни '
                 'один из %d водителей' % len(TIRE_DRIVERS))

    # --- 5. аркада моделью шин НЕ тронута ---------------------------------
    arcade = rh.car_tuning(_tire_catalog().get('hatch'), 'arcade')
    report.check(arcade.get('tire_peak_slip', 0.0) == 0.0,
                 'в аркадном блоке каталога модели шин нет',
                 'tire_peak_slip = %r' % arcade.get('tire_peak_slip', 0.0))
    host().reset(0)
    host().tuning_preset(0)
    report.check(host().tuning_values()['tire_peak_slip'] == 0.0,
                 'аркадный пресет модуля ставит нейтраль',
                 'rp_tuning_preset(0) -> tire_peak_slip = %r'
                 % host().tuning_values()['tire_peak_slip'])


# ---------------------------------------------------------------------------
# Модуль: md5 и сверка движков
# ---------------------------------------------------------------------------
# Стенд четырёх движков (tools/test_wasm_parity.py) поднимает wasmtime, Node,
# Chromium и Firefox — 19 секунд, из них 16 уходит на браузеры. В быстрый
# прогон он целиком не годится, а звать его руками значит полагаться на
# память; ровно так до 4b жил gen_abi.py --check. Поэтому прогон разделён:
#
# * wasmtime и Node гоняются ЗДЕСЬ, на каждом прогоне (3 с). Это две
#   стороны, которые ломает правка кода: wasmtime — сервер, V8 — браузер
#   через Node. Разъехавшаяся раскладка ABI или устаревший abi_layout.py
#   видны тут сразу;
# * Chromium и Firefox остаются отдельной командой. Они способны разойтись
#   только если сменились БАЙТЫ модуля или сам браузер — от правки game/ или
#   static/js/ ни того, ни другого не происходит, и шестнадцать секунд
#   запуска браузеров на каждом прогоне не покупают ничего.
#
# Чтобы «отдельная команда» не держалась на памяти, здесь стоит сторож md5:
# модуль пересобрали — проверка краснеет и называет команду.

def report_handicap_fields(report):
    """Список полей гандикапа один и тот же в трёх местах (§12.30).

    ``server/config.HANDICAP_STATS`` — источник; ``static/js/rapier_host.js``
    режет по нему настройки модуля, ``static/js/main.js`` — характеристики
    предсказания классики. Разошлись бы — своя машина ехала бы у сервера и
    у клиента по-разному, и реконсиляция тянула бы её назад всю гонку.
    """
    print('  список полей гандикапа:')
    sys.path.insert(0, BASE_DIR)
    from server import config
    want = tuple(config.HANDICAP_STATS)
    host_js = open(os.path.join(BASE_DIR, 'static', 'js', 'rapier_host.js'),
                   encoding='utf-8').read()
    found = re.search(r"HANDICAP_STATS\s*=\s*\[([^\]]*)\]", host_js)
    names = tuple(re.findall(r"'([a-z_]+)'", found.group(1))) if found else ()
    report.check(names == want, 'браузерный хозяин режет те же поля',
                 'config %s, rapier_host.js %s' % (list(want), list(names)))
    main_js = open(os.path.join(BASE_DIR, 'static', 'js', 'main.js'),
                   encoding='utf-8').read()
    body = main_js.split('function applyHandicap')[1][:1200]
    camel = {'engine_force': 'engineForce', 'max_speed': 'maxSpeed',
             'boost_speed': 'boostSpeed'}
    missing = [n for n in want
               if ('stats.%s *= factor' % camel.get(n, n)) not in body]
    report.check(not missing,
                 'предсказание классики в браузере режет те же поля',
                 'не нашлось: %s' % (', '.join(missing) or 'ничего'))


def report_module(report, wasm_parity=True):
    print('  модуль и движки:')
    path = rh.WASM_PATH
    digest = hashlib.md5(open(path, 'rb').read()).hexdigest()
    same = digest == WASM_MD5
    report.check(same, 'md5 модуля тот, с которого сняты хэши четырёх движков',
                 digest if same else
                 '%s, а хэши §12.24 сняты с %s -> прогнать '
                 '`python3 tools/test_wasm_parity.py` (четыре движка) и '
                 'обновить WASM_MD5' % (digest, WASM_MD5))
    if not wasm_parity:
        print('    [   ] сверка движков пропущена (--no-wasm-parity)')
        return
    # Два сценария. Первый — прежний, avenue: его хэши записаны в §12.27, и
    # трогать его нельзя. Второй — ridge (§12.30): восемь машин разложены
    # поперёк трамплина и летят с него. Он проверяет ровно то, чего первый
    # проверить не может, — что коробки трамплина у СЕРВЕРА (wasmtime) и у
    # БРАУЗЕРА (V8 через static/js/rapier_host.js) одни и те же. Замерено,
    # что проверка способна покраснеть: сдвиг ОДНОЙ коробки из 38 на 1 мм
    # разводит движки начиная с 200-го тика.
    # Третий сценарий — тот же avenue в пресете «симулятор» (§12.32).
    # Без него ни одна сверка движков не трогает модель шин, а она считает
    # синус и арктангенс: если бы они приезжали импортом от хозяина, движки
    # разошлись бы именно здесь. У модуля импортов ноль, и это надо не
    # заявлять, а мерить.
    for track_id, mode, what in (
            ('avenue', 'arcade', 'один .wasm — один результат'),
            ('ridge', 'arcade', 'коробки трамплина у сервера и у '
                                'браузера одни и те же'),
            ('avenue', 'sim', 'модель шин считается одинаково у сервера '
                              'и у браузера')):
        proc = subprocess.run(
            [sys.executable,
             os.path.join(BASE_DIR, 'tools', 'test_wasm_parity.py'),
             '--engines', 'wasmtime,node', '--track', track_id,
             '--mode', mode],
            cwd=BASE_DIR, capture_output=True, text=True)
        tail = [line for line in proc.stdout.splitlines() if line.strip()]
        report.check(proc.returncode == 0, '%s (wasmtime, Node)' % what,
                     tail[-1].strip() if tail else 'стенд ничего не напечатал')
        if proc.returncode != 0:
            print('      ' + '\n      '.join(tail[-12:]))
    print('      браузеры сверяются отдельно: python3 tools/test_wasm_parity.py')


# ---------------------------------------------------------------------------
# Трамплины (§12.30)
# ---------------------------------------------------------------------------
#
# У классики трамплин — поле высоты, у Rapier — настоящая геометрия из
# неподвижных коробок. Проверок здесь четыре рода, и каждая ловит свой
# способ сломаться:
#
# * КОРОБКИ ПОВТОРЯЮТ ПРОФИЛЬ. Считается по-честному: верх коробки против
#   Track.ramp_height в девятнадцати тысячах точек следа. Порог выведен из
#   раскладки (половина боковой ступеньки), а не подобран под прогон;
# * ЛЕСЕНКА СКОСА НЕ ВЫШЕ, ЧЕМ МОЖЕТ ПРОГЛОТИТЬ ПОДВЕСКА. Оба предела
#   читаются из ЖИВЫХ настроек машины, а не повторяются числом;
# * MAX_PROPS НЕ ТРАТИТСЯ. Неподвижная коробка тела не заводит, и это
#   меряется, а не заявляется: rp_prop_count() после постройки;
# * ПОЛЁТ НЕ ПЛАТИТ ЗА ЗАНОС. Четвёртый барьер §12.15 на настоящем
#   трамплине, а не на догадке.

RAMP_TRACK = 'industrial'      # самый высокий трамплин каталога: rise 1,25 м

# Потолок расхождения профиля. ВЫВЕДЕН: лесенка скоса даёт ступеньку
# rise/steps, и точка следа отстоит от коробки не дальше её половины. Всё
# остальное — сверх этого: полотно под трамплином ломаное по точкам
# оцифровки (шаг 2 м), а верх коробки — плоскость через въезд и кромку.
# Замерено по одной сердцевине, где лесенки нет вовсе: 0,0032 м (ridge).
# Берём с троекратным запасом.
RAMP_PROFILE_SLACK = 0.01

_RAMP_TRACKS = {}


def ramp_track(track_id=RAMP_TRACK):
    tr = _RAMP_TRACKS.get(track_id)
    if tr is None:
        tr = Track.load(os.path.join(BASE_DIR, 'content', 'tracks',
                                     track_id + '.json'))
        _RAMP_TRACKS[track_id] = tr
    return tr


def _box_top(box, x, z):
    """Высота верхней грани коробки в точке (x, z) или None вне её следа."""
    hx, hy, hz, cx, cy, cz, yaw, pitch = box
    sy, cy_ = math.sin(yaw), math.cos(yaw)
    sp, cp = math.sin(pitch), math.cos(pitch)
    if cp <= 0.0:
        return None
    # Центр ВЕРХНЕЙ ГРАНИ: центр коробки плюс полутолщина по её нормали.
    tx = cx + hy * sp * sy
    tz = cz + hy * sp * cy_
    ty = cy + hy * cp
    dx, dz = x - tx, z - tz
    lx = dx * cy_ - dz * sy
    lz = (dx * sy + dz * cy_) / cp
    if abs(lx) > hx + 1e-9 or abs(lz) > hz + 1e-9:
        return None
    return ty + lz * (-sp)


RAMP_SCAN_POINTS = 2500     # точек следа на трамплин
RAMP_SCAN_SEED = 30012
# Отступ от края следа при сверке профиля, м. Профиль классики на кромке
# вылета РАЗРЫВЕН по построению: на ds = span высота равна rise, на
# ds = span + eps — нулю. Коробка кончается ровно на кромке, но `ds`
# классика считает от своей точки оцифровки, и на последних миллиметрах
# два счёта дают разные стороны разрыва. Сравнивать высоты в точке разрыва
# бессмысленно, поэтому пять сантиметров с каждого края не берём — и то же
# самое поперёк, у внешней кромки скоса.
RAMP_SCAN_INSET = 0.05


def ramp_profile_error(tr, ramp, points=RAMP_SCAN_POINTS):
    """(макс. расхождение с профилем классики, дыр в следе, коробок).

    Точки следа берутся СЛУЧАЙНО с фиксированным зерном, а не решёткой:
    решётка с шагом, кратным ширине полосы, ложится ровно в середины полос,
    где расхождение равно нулю по построению, — и проверка меряет ноль.
    Это ловушка §12.20 «стенд создаёт то, что меряет», и здесь она реальна:
    первая версия на решётке 80 столбцов давала 0,009 м там, где честный
    максимум 0,062 м.
    """
    flat = ramp['boxes']
    stride = RAMP_BOX_FLOATS
    boxes = [flat[i:i + stride] for i in range(0, len(flat), stride)]
    s0, span = ramp['s0'], ramp['length']
    off, hw = ramp['offset'], ramp['half_width']
    rng = random.Random(RAMP_SCAN_SEED)
    worst, holes = 0.0, 0
    inset = RAMP_SCAN_INSET
    for _ in range(points):
        s = s0 + inset + (span - 2.0 * inset) * rng.random()
        u = off + (2.0 * rng.random() - 1.0) * (hw - inset)
        px, _py, pz, nx, nz = tr._sample_at(s)
        x, z = px + nx * u, pz + nz * u
        i, lateral, _hw, ground, _p = tr.surface(x, z, 0)
        along = ((x - tr._cx[i]) * tr._ctx[i]
                 + (z - tr._cz[i]) * tr._ctz[i])
        classic = tr.ramp_height(i, lateral, along)
        best = None
        for box in boxes:
            top = _box_top(box, x, z)
            if top is not None and (best is None or top > best):
                best = top
        if best is None:
            holes += 1
            continue
        worst = max(worst, abs((best - ground) - classic))
    return worst, holes, len(boxes)


class RampCar(object):
    """Машина на трассе с трамплинами, пилот держит линию и скорость.

    Руль КНОПОЧНЫЙ (+1/-1/0), как в игре: сравнивать классику и Rapier
    имеет смысл только на одинаковом вводе.
    """

    def __init__(self, track_id=RAMP_TRACK, ramp=0, run_up=140.0,
                 lateral=None):
        self.tr = tr = ramp_track(track_id)
        self.ramp = r = tr.ramps[ramp]
        self.target = r['offset'] if lateral is None else lateral
        x, z, yaw, i = self._place(r['s0'] + r['length'] - run_up, self.target)
        self.h = h = host()
        h.reset(0)
        margin, height, friction = rh.mesh_params(tr)
        h.build_track(tr, margin, height, friction)
        h.free_track_mesh()
        self.boxes = h.add_ramps(tr, friction)
        h.tuning_preset(0)
        h.set_tuning(rh.car_tuning(car_spec(), 'arcade'))
        tune = h.tuning_values()
        self.rest = (tune['wheel_radius'] + tune['suspension_rest']
                     + tune['half_height'] * 0.2)
        self.travel = tune['max_suspension_travel']
        self.clearance = tune['wheel_radius'] + tune['suspension_rest'] \
            - 0.8 * tune['half_height']
        ground = tr.surface(x, z, i)[3]
        self.idx = h.spawn_car(x, ground + self.rest, z, yaw)
        self.base = self.idx * OUT.FLOATS
        self.hint = i
        for _ in range(rh.SETTLE_TICKS):
            h.set_input(self.idx, 0.0, 0.0, 0.0, 0.0)
            h.step(1)

    def _place(self, s, lateral):
        tr = self.tr
        n = tr._n
        f = (s % tr.length) * tr._inv_step
        i = int(f) % n
        t = f - int(f)
        j = (i + 1) % n
        x = (tr._cx[i] + (tr._cx[j] - tr._cx[i]) * t + tr._cnx[i] * lateral)
        z = (tr._cz[i] + (tr._cz[j] - tr._cz[i]) * t + tr._cnz[i] * lateral)
        return x, z, math.atan2(tr._ctx[i], tr._ctz[i]), i

    def g(self, field):
        return self.h.outputs[self.base + field]

    def inject_charge(self, value):
        """Положить машине копилку заноса через слот сохранения."""
        h = self.h
        h.car_save(self.idx)
        h._sync()
        h.saves[self.idx * SAVE.FLOATS + SAVE.DRIFT_CHARGE] = float(value)
        h.car_restore(self.idx)

    def run(self, v_target, seconds=14.0, abuse_in_air=False):
        """Прогон пилота. Возвращает кадры: (t, height, speed, x, z,
        airborne, v_vert, drift_charge, drift_level).

        ``abuse_in_air`` — как только машина оторвалась, пилот жмёт ручник
        и выкручивает руль до упора. На земле он этого не делает: задача —
        доехать до трамплина, а не разбиться до него.
        """
        tr = self.tr
        rec = []
        ticks = int(seconds / DT)
        for k in range(ticks):
            x, z = self.g(OUT.PX), self.g(OUT.PZ)
            i, lateral, hw, ground, _p = tr.surface(x, z, self.hint)
            self.hint = i
            head = math.atan2(tr._ctx[i], tr._ctz[i]) - self.g(OUT.YAW)
            while head > math.pi:
                head -= 2.0 * math.pi
            while head < -math.pi:
                head += 2.0 * math.pi
            v = self.g(OUT.SPEED)
            cmd = (self.target - lateral) * 0.18 + head * 2.2
            steer = 1.0 if cmd > 0.04 else (-1.0 if cmd < -0.04 else 0.0)
            hb = 0.0
            if abuse_in_air and self.g(OUT.WHEELS_ON_GROUND) == 0.0:
                hb, steer = 1.0, 1.0
            self.h.set_input(self.idx, 1.0 if v < v_target else 0.0,
                             1.0 if v > v_target + 1.5 else 0.0, steer, hb)
            self.h.step(1)
            x, z = self.g(OUT.PX), self.g(OUT.PZ)
            i, lateral, hw, ground, _p = tr.surface(x, z, self.hint)
            rec.append((k * DT, self.g(OUT.PY) - ground - self.rest,
                        self.g(OUT.SPEED), x, z,
                        self.g(OUT.WHEELS_ON_GROUND) == 0.0, self.g(OUT.VY),
                        self.g(OUT.DRIFT_CHARGE), self.g(OUT.DRIFT_LEVEL)))
        return rec


def longest_flight(rec):
    """(начало, конец) самого длинного отрезка полёта или None."""
    best, k = None, 0
    while k < len(rec):
        if rec[k][5]:
            j = k
            while j < len(rec) and rec[j][5]:
                j += 1
            if best is None or (j - k) > (best[1] - best[0]):
                best = (k, j)
            k = j
        else:
            k += 1
    if best is None or best[1] - best[0] < 6:
        return None
    return best


def flight_stats(rec):
    span = longest_flight(rec)
    if span is None:
        return None
    a, b = span
    return {
        'apex': max(r[1] for r in rec[a:b]),
        'air': (b - a) * DT,
        'dist': math.hypot(rec[b - 1][3] - rec[a][3], rec[b - 1][4] - rec[a][4]),
        'v_in': rec[a][2],
        'v_out': rec[b - 1][2],
        'v_vert': rec[a][6],
        'charge_peak': max(r[7] for r in rec[a:b]),
        'level_sum': sum(r[8] for r in rec[a:b]),
        'span': span,
    }


def classic_flight(track_id=RAMP_TRACK, ramp=0, run_up=140.0, v_target=40.0,
                   seconds=14.0):
    """Тот же прыжок у классики: пилот и трасса те же, физика другая."""
    from game.protocol import BTN_THROTTLE, BTN_BRAKE, BTN_LEFT, BTN_RIGHT
    tr = ramp_track(track_id)
    r = tr.ramps[ramp]
    stats = car_spec()
    n = tr._n
    s = (r['s0'] + r['length'] - run_up) % tr.length
    f = s * tr._inv_step
    i = int(f) % n
    t = f - int(f)
    j = (i + 1) % n
    x = tr._cx[i] + (tr._cx[j] - tr._cx[i]) * t + tr._cnx[i] * r['offset']
    z = tr._cz[i] + (tr._cz[j] - tr._cz[i]) * t + tr._cnz[i] * r['offset']
    yaw = math.atan2(tr._ctx[i], tr._ctz[i])
    st = physics.CarState(x, z, yaw)
    tr.init_state(st)
    st.x, st.z, st.yaw, st.sample_idx = x, z, yaw, i
    rec = []
    for k in range(int(seconds / DT)):
        v = math.hypot(st.vx, st.vz)
        idx, lateral, hw, ground, _p = tr.surface(st.x, st.z, st.sample_idx)
        head = math.atan2(tr._ctx[idx], tr._ctz[idx]) - st.yaw
        while head > math.pi:
            head -= 2.0 * math.pi
        while head < -math.pi:
            head += 2.0 * math.pi
        cmd = (r['offset'] - lateral) * 0.18 + head * 2.2
        buttons = BTN_THROTTLE if v < v_target else 0
        if v > v_target + 1.5:
            buttons |= BTN_BRAKE
        if cmd > 0.04:
            buttons |= BTN_LEFT
        elif cmd < -0.04:
            buttons |= BTN_RIGHT
        physics.step(st, stats, buttons, DT, tr, st.sample_idx)
        rec.append((k * DT, st.height, math.hypot(st.vx, st.vz), st.x, st.z,
                    bool(st.airborne), st.v_vert, st.drift_charge, 0.0))
    return rec


def report_ramps(report):
    print('  трамплины (§12.30):')
    from game import track as track_mod

    # 1. Клиенту трамплины едут при любой физике, и вместе с коробками.
    tr = ramp_track()
    payload = tr.to_client()
    rows = payload.get('ramps') or []
    with_boxes = [r for r in rows if r.get('boxes')]
    report.check(len(rows) == len(tr.ramps) and len(with_boxes) == len(rows),
                 'запись трассы везёт трамплины вместе с коробками',
                 '%d трамплинов, у %d есть boxes' % (len(rows), len(with_boxes)))

    # 2. Коробки повторяют профиль классики, дыр в следе нет.
    worst_all, holes_all, boxes_all = 0.0, 0, 0
    worst_bound = 0.0
    for track_id in ('serpentine', 'industrial', 'ridge'):
        t = ramp_track(track_id)
        for ramp in t.ramps:
            worst, holes, count = ramp_profile_error(t, ramp)
            steps = (count - 1) // 2
            bound = ramp['rise'] / (2.0 * steps) + RAMP_PROFILE_SLACK
            worst_all = max(worst_all, worst)
            worst_bound = max(worst_bound, bound)
            holes_all += holes
            boxes_all += count
    report.check(worst_all <= worst_bound,
                 'коробки повторяют профиль ramp_height',
                 'максимум %.4f м при пороге %.4f (половина ступеньки + %.2f)'
                 % (worst_all, worst_bound, RAMP_PROFILE_SLACK))
    report.check(holes_all == 0,
                 'в следе трамплина нет дыр',
                 'непокрытых точек %d из %d'
                 % (holes_all, 4 * RAMP_SCAN_POINTS))

    # 3. Ступенька лесенки — ниже того, что глотает подвеска, и ниже
    #    половины просвета кузова. Оба числа из ЖИВЫХ настроек машины.
    car = RampCar()
    worst_step = 0.0
    for track_id in ('serpentine', 'industrial', 'ridge'):
        for ramp in ramp_track(track_id).ramps:
            steps = (len(ramp['boxes']) // RAMP_BOX_FLOATS - 1) // 2
            worst_step = max(worst_step, ramp['rise'] / steps)
    report.check(worst_step <= car.travel,
                 'боковая ступенька ниже хода подвески',
                 '%.3f м при ходе %.3f м' % (worst_step, car.travel))
    report.check(worst_step <= car.clearance * 0.5,
                 'боковая ступенька ниже половины просвета кузова',
                 '%.3f м при просвете %.3f м' % (worst_step, car.clearance))
    # Ход подвески приезжает из модуля в f32, поэтому 0,13 читается как
    # 0,129999995: сравнение с допуском в микрометр, а не «меньше».
    report.check(track_mod.RAMP_STEP_MAX <= car.travel + 1e-6,
                 'потолок ступеньки выведен из хода подвески, а не подобран',
                 'RAMP_STEP_MAX %.3f, ход %.3f' % (track_mod.RAMP_STEP_MAX,
                                                   car.travel))

    # 4. MAX_PROPS неподвижные коробки не расходуют — замер, не заявление.
    report.check(car.h.prop_count() == 0,
                 'коробки трамплина не тратят MAX_PROPS',
                 '%d коробок в мире, подвижных предметов %d из %d'
                 % (car.boxes, car.h.prop_count(), abi.MAX_PROPS))

    # 5. Прыжок. Числа рядом с классикой: совпадать они не обязаны (у
    #    Rapier рельеф настоящий, тяготение 9,81 против 12,0 у классики, а
    #    скорость отрыва не зажата RAMP_LIFT_MAX), но прыжок обязан
    #    остаться прыжком, а не улётом.
    rec = car.run(40.0)
    fly = flight_stats(rec)
    ok = fly is not None
    report.check(ok, 'машина улетает с трамплина',
                 'полёта нет' if not ok else
                 'высота %.2f м, время %.3f с, дальность %.1f м'
                 % (fly['apex'], fly['air'], fly['dist']))
    if ok:
        cls = flight_stats(classic_flight())
        report.check(cls is not None and fly['air'] <= cls['air'] * 1.5
                     and fly['apex'] <= cls['apex'] * 1.5,
                     'прыжок не длиннее классического в полтора раза',
                     'rapier %.2f м / %.3f с против классики %.2f м / %.3f с'
                     % (fly['apex'], fly['air'], cls['apex'], cls['air']))
        # Отсечку прыжком не перепрыгнуть — то же требование, что в §12.18.
        gap = min(ramp_track(t).length / 12.0
                  for t in ('serpentine', 'industrial', 'ridge'))
        report.check(fly['dist'] < gap,
                     'прыжком не перепрыгнуть отсечку',
                     'дальность %.1f м при самой короткой отсечке %.1f м'
                     % (fly['dist'], gap))

        # 6. Четвёртый барьер §12.15 на НАСТОЯЩЕМ трамплине, а не на
        #    догадке: в воздухе ручник и полный руль не копят заряд.
        air = RampCar()
        rec_air = air.run(40.0, abuse_in_air=True)
        fa = flight_stats(rec_air)
        report.check(fa is not None and fa['charge_peak'] == 0.0,
                     'ручник и полный руль весь полёт не копят заряд',
                     'полёт %.2f с, пик заряда %.5f с'
                     % (fa['air'] if fa else 0.0,
                        fa['charge_peak'] if fa else -1.0))
        report.check(fa is not None and fa['level_sum'] == 0.0,
                     'полёт не платит за занос ни одного уровня',
                     'сумма уровней %.1f' % (fa['level_sum'] if fa else -1.0))

        # Та же дыра с другой стороны: копилка, ПРИНЕСЁННАЯ на кромку,
        # сгорает без выплаты. Одной первой проверки мало — она зеленела бы
        # и на сломанном барьере, если бы заряд просто не набирался
        # (§12.29, урок про масло). Поэтому рядом тот же заряд, отпущенный
        # на земле: он обязан заплатить.
        paid_air, left_air = charge_release(True)
        paid_ground, left_ground = charge_release(False)
        report.check(paid_air == 0.0 and left_air == 0.0,
                     'копилка 3,00 с, принесённая на кромку, сгорает без награды',
                     'выплат %.0f, остаток %.3f с' % (paid_air, left_air))
        report.check(paid_ground > 0.0,
                     'та же копилка на земле платит — проверка умеет краснеть',
                     'уровень %.0f, остаток %.3f с' % (paid_ground, left_ground))

    # 7. Машина, задевшая скос трамплина сбоку, не встаёт: лесенка — это
    #    поребрик, а не стена. Замер по потере скорости на проезде мимо.
    ramp0 = ramp_track().ramps[0]
    edge = RampCar(lateral=ramp0['offset'] - ramp0['half_width'] + 0.4)
    rec_edge = edge.run(35.0)
    v_min = min(r[2] for r in rec_edge[120:])
    report.check(v_min > 10.0,
                 'проезд по самому скосу не останавливает машину',
                 'минимум скорости %.1f м/с на линии скоса' % v_min)


CHARGE_INJECT = 3.00        # с, копилка из §12.18: «3,00 с на кромке»


def charge_release(in_air):
    """Вложить копилку и отпустить ручник в воздухе либо на земле.

    Возвращает (сумма уровней выплаты, остаток копилки). Копилка кладётся
    через слот сохранения — это единственное место, где её можно задать
    снаружи, и ровно то же место, которым пользуется откат клиента.
    """
    car = RampCar()
    tr = car.tr
    paid, left, armed = 0.0, 0.0, False
    for _ in range(int(14.0 / DT)):
        rec = car.run(40.0, seconds=DT)
        if not rec:
            break
        airborne = rec[-1][5]
        x, z = rec[-1][3], rec[-1][4]
        i = tr.surface(x, z, car.hint)[0]
        if not armed:
            # В воздухе — сразу после отрыва; на земле — на подъезде, за
            # тридцать метров до въезда, чтобы выплата успела случиться.
            ready = airborne if in_air else (tr._cs[i] > car.ramp['s0'] - 40.0
                                             and not airborne)
            if ready:
                car.inject_charge(CHARGE_INJECT)
                armed = True
                continue
        if armed:
            paid += rec[-1][8]
            left = rec[-1][7]
            if not in_air and paid > 0.0:
                break
            if in_air and not airborne:
                break
    return paid, left


# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='test_rapier_guard.py',
        description='Постоянные проверки физики Rapier: барьеры 12.15, '
                    'откат, перенастройки 12.24.')
    parser.add_argument('--no-wasm-parity', action='store_true',
                        help='не звать стенд сверки движков (быстрее на 3 с)')
    args = parser.parse_args(argv)

    print('')
    print('-' * 72)
    print('  Постоянные проверки физики Rapier (§12.15, §12.24)')
    print('-' * 72)
    report = Report()
    try:
        report_drift_guard(report)
        report_rollback(report)
        report_eps_sync(report)
        report_restrictions(report)
        report_effects(report)
        report_road(report)
        report_ramps(report)
        report_handicap_fields(report)
        report_tires(report)
        report_module(report, wasm_parity=not args.no_wasm_parity)
    except rh.HostError as exc:
        print('  ПРОПУЩЕНО: модуль физики недоступен — %s' % exc)
        return 0
    print('')
    if report.failures:
        print('  ПРОВАЛЕНО %d из %d проверок Rapier:' % (len(report.failures),
                                                         report.checks))
        for name in report.failures:
            print('    - %s' % name)
        return 1
    print('  Rapier: %d проверок, все зелёные.' % report.checks)
    return 0


if __name__ == '__main__':
    sys.exit(main())
