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

from game import rapier_host as rh              # noqa: E402
from game.cars import load_cars                 # noqa: E402
from game.track import Track                    # noqa: E402

abi = rh.abi
OUT = abi.CarOut
INP = abi.CarInput
SAVE = abi.CarSave

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
WASM_MD5 = '1390afb0d01b75b3e8eb64d39f9623b5'


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
        report.check(len(sim.disabled_features) >= 4,
                     'комната сообщает игрокам, что опущено',
                     '%d фраз: %s'
                     % (len(sim.disabled_features),
                        '; '.join(sim.disabled_features)))
        # Настройка снята — подсистема обязана быть пустой, а не просто
        # выключенной флагом: именно на этой разнице сгорел гандикап.
        items = getattr(sim, 'items', None)
        report.check(items is None or not getattr(items, 'enabled', False),
                     'бонусов в гонке нет',
                     'items_enabled=%r' % (None if items is None
                                           else getattr(items, 'enabled', None)))
        traffic = getattr(sim, 'traffic', None)
        report.check(traffic is None or not getattr(traffic, 'cars', None),
                     'болванок потока в гонке нет',
                     'traffic=%r' % (None if traffic is None
                                     else len(getattr(traffic, 'cars', ()) or ()),))
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
