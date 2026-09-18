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
from game.track import Track                    # noqa: E402

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
WASM_MD5 = 'c254183dfe1421c13789f5ae847d2bc1'


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
        report.check(len(sim.disabled_features) == 2,
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
        # Гандикап: он не в симуляции, а в комнате, и читает НАСТРОЙКИ
        # СИМУЛЯЦИИ. Убедиться, что после снятия настройки он ничего не
        # подменяет, можно прямо по ней: ветка выходит на первом же if.
        report.check(sim.settings.get('handicap') is False,
                     'гандикап опущен до того, как комната его наложит',
                     '_apply_handicap читает settings симуляции (room.py)')
        # Трамплины сюда не берём СОЗНАТЕЛЬНО: вычищает их room.py из
        # race_init, а не симуляция, и проверить это, не подняв комнату,
        # можно было бы только копией тех же двух строк — то есть проверкой
        # собственной копии. Проверка, не способная покраснеть, хуже
        # отсутствующей.
        # Трамплины: при rapier их в сетке нет, и рисовать их нельзя.
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
    proc = subprocess.run(
        [sys.executable, os.path.join(BASE_DIR, 'tools', 'test_wasm_parity.py'),
         '--engines', 'wasmtime,node'],
        cwd=BASE_DIR, capture_output=True, text=True)
    tail = [line for line in proc.stdout.splitlines() if line.strip()]
    report.check(proc.returncode == 0,
                 'один .wasm — один результат (wasmtime, Node)',
                 tail[-1].strip() if tail else 'стенд ничего не напечатал')
    if proc.returncode != 0:
        print('      ' + '\n      '.join(tail[-12:]))
    print('      браузеры сверяются отдельно: python3 tools/test_wasm_parity.py')


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
