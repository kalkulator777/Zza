# -*- coding: utf-8 -*-
"""Траффик: машины-болванки, едущие по трассе в общем потоке.

Задумка целиком повторяет контракт: болванка — это ОБЫЧНАЯ машина
из ``game/physics.py`` с обычным ``CarState`` и обычными ``CarStats``.
Единственное отличие от гонщика в том, что маску кнопок ей даёт не сеть,
а скриптовый водитель из этого файла: «держись полосы, держи скорость».
Поэтому столкновения, стены, сцепление, занос и любые будущие правки
физики достаются траффику даром — в том числе вертикальная ось, которую
сейчас заводят под трамплины: этот файл ни разу не трогает ``state.y``
и не перечисляет поля состояния.

Что здесь есть
--------------
* ``TrafficSystem`` — пул болванок, расстановка, водитель, шаг, снапшот;
* правила честности (раздел «Где траффику можно быть» ниже);
* объезд происшествий на дороге (``game/events.py``), чтобы поток
  не упёрся в перекрытие и не встал поперёк трассы стеной.

Где траффику можно быть (требование заказчика «не быть несправедливым»)
----------------------------------------------------------------------
1. **Болванки расставляются ОДИН раз, до старта**, равномерно по кругу,
   и дальше просто едут. Появления по ходу гонки нет вообще — значит
   физически невозможно «выскочить перед носом». Это самый дешёвый
   способ закрыть требование, и он же самый надёжный.
2. Перед стартовой решёткой держится чистый коридор ``CLEAR_AHEAD_START``:
   на зелёный свет пачка разгоняется в пустоту, а не в чужой бампер.
3. Болванка держит СВОЮ полосу — смещение от оси в долях полуширины,
   ``LANE_FRACTION``. Полоса выбрана так, что рядом всегда остаётся
   не меньше ``MIN_FREE_WIDTH`` метров асфальта: обогнать можно везде.
4. Две болванки не встают борт к борту: у каждой включён простейший
   адаптивный круиз — она не подъезжает к передней ближе ``FOLLOW_GAP``
   независимо от того, в какой та полосе. Стены поперёк трассы поэтому
   не образуется даже когда быстрая догоняет медленную.
5. Болванка не стоит: если её зажали в стену или развернули, водитель
   сдаёт назад, а если и это не помогло за ``STUCK_RESCUE`` секунд —
   болванку переставляют, но ТОЛЬКО туда, где её не видит ни один
   гонщик (``RESPAWN_MIN_GAP`` метров впереди лидера).
6. В слепом повороте болванка не «стоит стеной» по построению: она
   едет с той же логикой торможения по кривизне, что и живой водитель,
   поэтому скорость в дуге у неё близка к гоночной, а не нулевая.

Цена
----
Одна болванка стоит один вызов ``physics.step`` плюс водитель. Замер —
в отчёте и в ``tools/test_sim.py``. Аллокаций в шаге нет: пул, буферы
и снапшотные кортежи выделены при создании гонки и правятся на месте.
"""

from __future__ import annotations

import math
import random

from . import physics
from . import protocol

__all__ = ['TRAFFIC_LEVELS', 'TRAFFIC_SPACING', 'MAX_TRAFFIC',
           'TrafficCar', 'TrafficSystem', 'normalize_level']

MAX_TRAFFIC = protocol.MAX_TRAFFIC
TRAFFIC_LOOKS = protocol.TRAFFIC_LOOKS

# --- настройка комнаты -------------------------------------------------------
#
# Три значения, как просил заказчик: выключено, редкий, плотный. Плотность
# задаётся расстоянием между болванками по дуге, а не их числом: на короткой
# трассе «плотный» не должен превращаться в пробку, а на длинной — исчезать.

TRAFFIC_LEVELS = ('off', 'sparse', 'dense')
TRAFFIC_SPACING = {
    'off': 0.0,
    'sparse': 340.0,     # м между болванками: на круге 1,4 км это четыре штуки
    'dense': 155.0,      # на том же круге — девять
}
TRAFFIC_DEFAULT = 'off'

# --- правила расстановки -----------------------------------------------------

CLEAR_AHEAD_START = 190.0   # м чистого коридора перед стартовой решёткой
GRID_BACK = 60.0            # м позади линии старта, где стоит сама решётка
LANE_FRACTION = 0.46        # доля полуширины, на которой держится полоса
LANE_MAX = 3.1              # м: дальше от оси не отходим и на широкой трассе
LANE_EDGE_MARGIN = 0.55     # м запаса между бортом болванки и кромкой асфальта
MIN_FREE_WIDTH = 5.2        # м свободного асфальта рядом с болванкой
FOLLOW_GAP = 46.0           # м, ближе к передней болванке не подъезжаем
FOLLOW_HARD = 17.0          # м, здесь уже тормозим в пол
RESPAWN_MIN_GAP = 260.0     # м впереди лидера при вынужденной перестановке
RESPAWN_MAX_GAP = 420.0

# --- скорость и водитель -----------------------------------------------------
#
# Болванка едет заметно медленнее гонщика: 20..26 м/с против 50..55. Это и
# есть весь геймплей — обгон становится решением, а не формальностью.

SPEED_BASE = 23.0           # м/с, опорная крейсерская скорость потока
SPEED_SPREAD = 1.8          # м/с, разброс между болванками
SPEED_WOBBLE = 0.55         # м/с, медленное «дыхание» скорости у каждой
WOBBLE_RATE = 0.21          # рад/с фазы этого дыхания
SPEED_MIN = 7.0             # м/с, ниже поток не проседает даже в шпильке

THROTTLE_MARGIN = 0.35      # м/с ниже цели — жмём газ
BRAKE_MARGIN = 1.25         # м/с выше цели — тормозим

LOOKAHEAD_MIN = 7.0         # м, точка прицеливания на малой скорости
LOOKAHEAD_K = 0.42          # м на каждый м/с скорости
LOOKAHEAD_MAX = 20.0

STEER_DEADBAND = 0.035      # ниже этой разницы руль не трогаем: иначе рыскание
LANE_CORRECTION = 0.85      # доля накопленного сноса, которую возвращает руль
LANE_CORRECTION_MAX = 2.4   # м, потолок этой поправки
LOOKAHEAD_RADIUS_K = 0.34   # доля радиуса дуги — потолок дальности прицела
# Доля предельной скорости в дуге, на которую согласен поток. 0,68 по
# скорости — это 0,46 по поперечному ускорению: болванка едет с запасом
# вдвое, а не по кромке сцепления. Замер на serpentine (шпильки R = 13..15
# при полуширине 3,75 м): 0,80 давало вынос на газон в 1,1 % тиков, 0,68 —
# ноль. Гонщику при этом всё равно есть что отыгрывать: его предел выше
# вдвое, и в шпильке он проезжает болванку как стоячую.
CORNER_SAFETY = 0.68
BRAKE_SCAN = 70.0           # м вперёд, на которых ищем крутую дугу
BRAKE_SCAN_STEP = 6.0       # м между точками просмотра

# --- застревание и спасение --------------------------------------------------

STUCK_SPEED = 1.6           # м/с, ниже этого болванка считается стоящей
STUCK_PATIENCE = 1.5        # с стояния, после которых водитель сдаёт назад
STUCK_REVERSE = 1.1         # с заднего хода
STUCK_RESCUE = 7.0          # с безнадёжного стояния — переставляем

# --- объезд происшествий -----------------------------------------------------

AVOID_SCAN = 75.0           # м вперёд, на которых замечаем перекрытие
AVOID_MARGIN = 1.4          # м запаса между бортом и краем препятствия

# --- характеристики болванки -------------------------------------------------
#
# Свои, а не из cars.json: болванке не нужен ни потолок в 55 м/с, ни разгон
# гонщика. Сцепление оставлено близким к гоночному — иначе поток вылетал бы
# в поворотах и сам себя блокировал, а это ровно то, чего мы избегаем.

# Фактический потолок — корень уравнения из 12.5, а не поле max_speed:
# 11,0 * (1 - v/34) = 0,0006*v^2 + 0,045*v даёт 28,5 м/с (103 км/ч). Целевая
# крейсерская SPEED_BASE лежит заметно ниже потолка — иначе у водителя не
# осталось бы запаса тяги, чтобы держать скорость в гору и после толчка.
TRAFFIC_STATS = dict(
    engine_force=11.0,
    max_speed=34.0,
    brake_force=20.0,
    reverse_force=9.0,
    turn_rate=2.6,
    grip_step=0.185,
    drift_grip_step=0.055,
    drag=0.0006,
    roll=0.045,
    boost_speed=34.0,
    mass=1.35,              # болванка тяжелее гонщика: толкать её — решение
)

_TWO_PI = math.pi * 2.0

_BTN_THROTTLE = protocol.BTN_THROTTLE
_BTN_BRAKE = protocol.BTN_BRAKE
_BTN_LEFT = protocol.BTN_LEFT
_BTN_RIGHT = protocol.BTN_RIGHT


def normalize_level(value):
    """Привести значение настройки к одному из TRAFFIC_LEVELS."""
    if isinstance(value, str) and value in TRAFFIC_LEVELS:
        return value
    return TRAFFIC_DEFAULT


class TrafficCar(object):
    """Одна болванка: состояние физики плюс память водителя."""

    __slots__ = (
        'id', 'look', 'state', 'stats',
        'lane_side', 'lane', 'lane_target', 'base_speed', 'wobble_phase',
        'buttons', 'target_speed', 'follow_speed', 'speed', 'arc', 'lateral', 'dyaw',
        'stuck_time', 'reverse_time', 'active',
    )

    def __init__(self, traffic_id, stats):
        self.id = traffic_id
        self.look = 0
        self.state = physics.CarState(0.0, 0.0, 0.0)
        self.stats = stats
        self.lane_side = 1.0       # сторона полосы: +1 или -1
        self.lane = 0.0            # текущая целевая полоса, м от оси
        self.lane_target = 0.0     # куда полосу уводит объезд препятствия
        self.base_speed = SPEED_BASE
        self.wobble_phase = 0.0
        self.buttons = 0
        self.target_speed = SPEED_BASE
        self.follow_speed = 1e9    # потолок от передней болванки, см. круиз
        self.speed = 0.0
        self.arc = 0.0             # положение вдоль дуги, м (для снапшота)
        self.lateral = 0.0
        self.dyaw = 0.0
        self.stuck_time = 0.0
        self.reverse_time = 0.0
        self.active = False

    def __repr__(self):
        return '<TrafficCar %d полоса %.1f скорость %.1f>' % (
            self.id, self.lane, self.speed)


class TrafficSystem(object):
    """Поток машин-болванок на одной трассе.

    Интерфейс для ``game/sim.py``::

        traffic = TrafficSystem(track, settings, rng)
        traffic.spawn(grid_cars)          # один раз, после расстановки гонщиков
        traffic.step(dt, sim)             # каждый тик, ДО resolve_collisions
        traffic.fill_snapshot(out)        # 20 Гц
        traffic.collect(states, stats, n) # дописать в буфер столкновений
    """

    def __init__(self, track, settings=None, rng=None):
        self.track = track
        settings = settings or {}
        self.level = normalize_level(settings.get('traffic'))
        self.rng = rng if rng is not None else random.Random(
            (track.decor_seed if hasattr(track, 'decor_seed') else 0) ^ 0x7ACF1C)

        # --- плоские копии осевой линии (как в items.py): индекс по списку
        #     float дешевле атрибута объекта в горячем цикле
        samples = track.samples
        count = len(samples)
        self._n = count
        self._sx = [s.x for s in samples]
        self._sz = [s.z for s in samples]
        self._stx = [s.tangent_x for s in samples]
        self._stz = [s.tangent_z for s in samples]
        self._snx = [s.normal_x for s in samples]
        self._snz = [s.normal_z for s in samples]
        self._shw = [s.half_width for s in samples]
        self._ss = [s.s for s in samples]
        # Курс касательной: нужен и водителю (целевой угол), и снапшоту
        # (там едет РАЗНИЦА курса с касательной, а не сам курс).
        self._syaw = [math.atan2(s.tangent_x, s.tangent_z) for s in samples]
        self._radius = _curvature_radii(self._sx, self._sz, count)
        self.length = track.length
        self._step = track.length / count if count else 1.0
        self._inv_step = 1.0 / self._step if self._step else 0.0
        self._half_length = track.length * 0.5

        # --- пул -------------------------------------------------------------
        spacing = TRAFFIC_SPACING.get(self.level, 0.0)
        if spacing > 0.0 and track.length > 0.0:
            wanted = int(track.length / spacing)
        else:
            wanted = 0
        if wanted > MAX_TRAFFIC:
            wanted = MAX_TRAFFIC
        if wanted < 0:
            wanted = 0
        self.wanted = wanted
        self.enabled = wanted > 0

        stats = physics.CarStats(**TRAFFIC_STATS)
        self.stats = stats
        self.cars = [TrafficCar(i, stats) for i in range(wanted)]
        self.count = wanted
        # Скользкая копия характеристик: ею подменяется stats, пока болванка
        # стоит в масле (см. game/events.py). Объект один на весь поток —
        # болванки одинаковые, и в шаге он только читается.
        self.oiled = physics.CarStats(**TRAFFIC_STATS)
        self._oil_factor = 1.0

        self._limit = self._build_speed_limits() if wanted else []
        self._order = []
        self._snap_scratch = []
        self._arc_scratch = []
        self._effects = None          # ссылка на RoadEvents, ставит Simulation

    # --- расстановка ---------------------------------------------------------

    def attach_events(self, effects):
        """Подключить систему происшествий: поток обязан их объезжать."""
        self._effects = effects

    def spawn(self):
        """Расставить поток по кругу. Зовётся один раз, до первого тика.

        Болванки стоят равномерно, начиная за ``CLEAR_AHEAD_START`` метров
        перед линией старта: пачка гонщиков разгоняется в пустой коридор.
        """
        count = self.count
        if count <= 0:
            return
        track = self.track
        length = self.length
        # Доступная дуга: от конца чистого коридора впереди решётки и до
        # самой решётки (она позади линии, поэтому её конец — length - GRID_BACK).
        free_start = CLEAR_AHEAD_START
        free_end = length - GRID_BACK
        if free_end - free_start < count * 30.0:
            # Совсем короткая трасса: расставляем по всему кругу, коридор
            # перед решёткой всё равно сохраняем минимальным.
            free_start = min(CLEAR_AHEAD_START, length * 0.12)
            free_end = length - min(GRID_BACK, length * 0.06)
        span = free_end - free_start
        if span <= 0.0:
            span = length
            free_start = 0.0
        rng = self.rng
        gap = span / count
        for k in range(count):
            car = self.cars[k]
            car.look = rng.randrange(TRAFFIC_LOOKS)
            car.base_speed = SPEED_BASE + rng.uniform(-SPEED_SPREAD, SPEED_SPREAD)
            car.wobble_phase = rng.uniform(0.0, _TWO_PI)
            # Полосы чередуются: поток идёт «змейкой», а не колонной по одной.
            side = 1.0 if (k & 1) == 0 else -1.0
            arc = free_start + gap * (k + 0.5)
            self._place(car, arc, side)
            track.init_state(car.state)
            car.state.vx = 0.0
            car.state.vz = 0.0
            car.active = True
            car.stuck_time = 0.0
            car.reverse_time = 0.0

    def _lane_at(self, index, side):
        """Смещение полосы в точке ``index``, метры от оси.

        Считается КАЖДЫЙ тик по фактической полуширине, а не один раз при
        расстановке: у serpentine полотно сужается с 6,25 до 3,75 м, и
        полоса, выбранная на широком участке, уводила болванку за кромку
        на узком (замер до правки: до 1,4 полуширины, то есть на газон).
        """
        half = self._shw[index]
        lane = half * LANE_FRACTION
        if lane > LANE_MAX:
            lane = LANE_MAX
        # Борт обязан остаться на асфальте с запасом.
        limit = half - physics.CAR_RADIUS - LANE_EDGE_MARGIN
        if lane > limit:
            lane = limit
        # И рядом обязан остаться проезд: не меньше MIN_FREE_WIDTH или, если
        # полотно уже этого, всей ширины за вычетом габарита болванки.
        room = half * 2.0 - 2.0 * physics.CAR_RADIUS
        want_free = MIN_FREE_WIDTH if MIN_FREE_WIDTH < room else room
        free = half + lane - physics.CAR_RADIUS
        if free < want_free:
            lane = want_free - half + physics.CAR_RADIUS
        if lane < 0.0:
            lane = 0.0
        return lane * side

    def _place(self, car, arc, side):
        """Поставить болванку на дугу ``arc``, полоса — сторона ``side``."""
        length = self.length
        arc %= length
        i = int(arc * self._inv_step) % self._n
        car.lane_side = side
        car.lane = self._lane_at(i, side)
        car.lane_target = car.lane
        along = arc - self._ss[i]
        state = car.state
        state.reset(
            self._sx[i] + self._stx[i] * along + self._snx[i] * car.lane,
            self._sz[i] + self._stz[i] * along + self._snz[i] * car.lane,
            self._syaw[i])
        state.sample_idx = i
        car.arc = arc
        car.lateral = car.lane
        car.dyaw = 0.0
        car.speed = 0.0

    # --- шаг -----------------------------------------------------------------

    def step(self, dt, sim):
        """Один тик потока: водитель + физика каждой болванки.

        Зовётся из ``Simulation.tick`` сразу после шага гонщиков и ДО
        ``resolve_collisions``: болванка обязана попасть в общий проход
        столкновений наравне с живыми машинами.
        """
        if not self.enabled:
            return
        track = self.track
        phys_step = physics.step
        effects = self._effects
        self._update_following()
        for car in self.cars:
            if not car.active:
                continue
            state = car.state
            # Происшествия трогают болванку теми же правилами, что и гонщика.
            oil = 0.0
            if effects is not None:
                oil = effects.grip_scale(state)
            self._drive(car, dt, effects)
            if oil > 0.0 and oil < 1.0:
                stats = self._oiled_stats(oil)
            else:
                stats = self.stats
            phys_step(state, stats, car.buttons, dt, track, state.sample_idx)
            if effects is not None:
                effects.apply_after_step(state, dt)
            self._track_pose(car)
            self._watch_stuck(car, dt, sim)

    def _oiled_stats(self, factor):
        """Характеристики со сниженным сцеплением. Объект переиспользуется."""
        if factor != self._oil_factor:
            self._oil_factor = factor
            base = self.stats
            self.oiled.grip_step = base.grip_step * factor
            self.oiled.drift_grip_step = base.drift_grip_step * factor
        return self.oiled

    def _track_pose(self, car):
        """Пересчитать (дуга, смещение, разница курса) после шага физики."""
        state = car.state
        i = state.sample_idx
        dx = state.x - self._sx[i]
        dz = state.z - self._sz[i]
        along = dx * self._stx[i] + dz * self._stz[i]
        arc = self._ss[i] + along
        length = self.length
        if arc >= length:
            arc -= length
        elif arc < 0.0:
            arc += length
        car.arc = arc
        car.lateral = dx * self._snx[i] + dz * self._snz[i]
        car.dyaw = _wrap_angle(state.yaw - self._syaw[i])
        vx = state.vx
        vz = state.vz
        car.speed = math.sqrt(vx * vx + vz * vz)

    # --- водитель ------------------------------------------------------------

    def _drive(self, car, dt, effects):
        """Скриптовый ввод: держись полосы, держи скорость.

        Руль — сервопривод по УГЛУ РУЛЯ, а не по курсу: цель считается
        погоней за точкой (``pure pursuit``), переводится в желаемый угол
        руля и сравнивается с текущим. Релейный руль прямо по ошибке курса
        рыскал бы — у руля своя инерция (STEER_RATE), и без учёта текущего
        положения контур получается с запаздыванием.
        """
        state = car.state
        # Задний ход после застревания идёт вне обычной логики.
        if car.reverse_time > 0.0:
            car.reverse_time -= dt
            car.buttons = _BTN_BRAKE
            return

        i = state.sample_idx
        n = self._n
        speed = car.speed

        # --- полоса: обычно своя, при перекрытии — свободная сторона --------
        lane = self._lane_at(i, car.lane_side)
        car.lane = lane
        if effects is not None:
            lane = self._avoid_lane(car, i, lane, effects)
        car.lane_target = lane

        # --- точка прицеливания ---------------------------------------------
        # К целевой полосе добавляется поправка на НАКОПЛЕННОЕ отклонение.
        # Без неё погоня за точкой впереди даёт устойчивую ошибку в дуге
        # (машина срезает и уезжает к внутренней кромке) — замер показал
        # до 2 м сноса от полосы на шпильке.
        look = LOOKAHEAD_MIN + LOOKAHEAD_K * speed
        if look > LOOKAHEAD_MAX:
            look = LOOKAHEAD_MAX
        # В крутой дуге точка прицеливания обязана подъехать ближе: далёкая
        # точка на радиусе 25 м лежит уже «за поворотом», погоня за ней
        # срезает вершину и выносит машину на внешнюю кромку. Замер на
        # avenue до этой правки: снос до 1,13 полуширины, то есть за асфальт.
        near = self._radius[i] * LOOKAHEAD_RADIUS_K
        if look > near:
            look = near if near > LOOKAHEAD_MIN else LOOKAHEAD_MIN
        aim = lane + (lane - car.lateral) * LANE_CORRECTION
        limit = lane + LANE_CORRECTION_MAX
        if aim > limit:
            aim = limit
        elif aim < lane - LANE_CORRECTION_MAX:
            aim = lane - LANE_CORRECTION_MAX
        j = int(i + look * self._inv_step) % n
        tx = self._sx[j] + self._snx[j] * aim
        tz = self._sz[j] + self._snz[j] * aim

        yaw = state.yaw
        fx = math.sin(yaw)
        fz = math.cos(yaw)
        rx = fz
        rz = -fx
        dx = tx - state.x
        dz = tz - state.z
        along = dx * fx + dz * fz
        side = dx * rx + dz * rz
        dist2 = along * along + side * side
        if dist2 < 1.0:
            dist2 = 1.0
        # Кривизна дуги погони. Положительный steer крутит курс в сторону
        # вектора ``right`` (раздел 6.2, шаги 4 и 9), поэтому знак тот же.
        kappa = 2.0 * side / dist2
        want_rate = kappa * (speed if speed > 3.0 else 3.0)
        rate_full = self._max_yaw_rate(speed)
        want_steer = want_rate / rate_full if rate_full > 0.0 else 0.0
        if want_steer > 1.0:
            want_steer = 1.0
        elif want_steer < -1.0:
            want_steer = -1.0

        buttons = 0
        diff = want_steer - state.steer
        if diff > STEER_DEADBAND:
            buttons |= _BTN_LEFT
        elif diff < -STEER_DEADBAND:
            buttons |= _BTN_RIGHT

        # --- целевая скорость ------------------------------------------------
        car.wobble_phase += WOBBLE_RATE * dt
        if car.wobble_phase > _TWO_PI:
            car.wobble_phase -= _TWO_PI
        target = car.base_speed + SPEED_WOBBLE * math.sin(car.wobble_phase)

        corner = self._limit[i]
        if corner < target:
            target = corner
        follow = car.follow_speed
        if follow < target:
            target = follow
        if target < SPEED_MIN:
            target = SPEED_MIN
        car.target_speed = target

        v_fwd = state.vx * fx + state.vz * fz
        if v_fwd < target - THROTTLE_MARGIN:
            buttons |= _BTN_THROTTLE
        elif v_fwd > target + BRAKE_MARGIN:
            buttons |= _BTN_BRAKE
        car.buttons = buttons

    def _max_yaw_rate(self, speed):
        """Какую угловую скорость даст ПОЛНЫЙ руль на этой скорости.

        Повторяет шаг 9 раздела 6.2: поворотливость растёт до
        TURN_FULL_SPEED, дальше срезается TURN_FALLOFF, и поверх всего
        лежит предел по сцеплению. Нужно именно это число, а не голый
        ``turn_rate``: на 20 м/с предел сцепления режет поворот в полтора
        раза, и сервопривод руля, считающий по ``turn_rate``, недокручивал
        бы ровно во столько же — машину выносило за кромку.
        """
        stats = self.stats
        v = speed if speed > 0.0 else 0.0
        factor = v / physics.TURN_FULL_SPEED
        if factor > 1.0:
            factor = 1.0
        span = stats.max_speed - physics.TURN_FULL_SPEED
        if span > 0.0:
            over = (v - physics.TURN_FULL_SPEED) / span
            if over < 0.0:
                over = 0.0
            elif over > 1.0:
                over = 1.0
        else:
            over = 0.0
        rate = stats.turn_rate * factor * (1.0 - physics.TURN_FALLOFF * over)
        ref = v if v > physics.LAT_CAP_MIN_SPEED else physics.LAT_CAP_MIN_SPEED
        cap = stats.grip_step * physics.GRIP_LAT_ACCEL / ref
        if rate > cap:
            rate = cap
        return rate if rate > 1e-4 else 1e-4

    def _build_speed_limits(self):
        """Потолок скорости в каждой точке трассы. Считается ОДИН раз.

        Смотрим вперёд на BRAKE_SCAN метров и берём минимум из
        ``sqrt(a_lat * R)`` по найденным радиусам, разрешая по дороге
        тормозной путь. Это ровно та формула, которую даёт предел сцепления
        шага 9 (раздел 6.2), уменьшенная на CORNER_SAFETY: болванка едет
        аккуратно, а не по пределу.

        Величина зависит ТОЛЬКО от геометрии трассы, поэтому держать её
        в тике незачем: двенадцать корней на болванку каждый тик — это
        3 мкс из 15, которые болванка стоит. Таблица строится за один
        проход при создании гонки и дальше только читается.
        """
        n = self._n
        a_lat = self.stats.grip_step * physics.GRIP_LAT_ACCEL * CORNER_SAFETY
        brake = self.stats.brake_force
        radius = self._radius
        inv_step = self._inv_step
        # Заранее: смещение выборок и прибавка тормозного пути на каждый шаг
        # просмотра — они одинаковы для всех точек трассы.
        scan = []
        offset = 0.0
        while offset <= BRAKE_SCAN:
            bonus = 2.0 * brake * offset * 0.5 if offset > 1.0 else 0.0
            scan.append((int(offset * inv_step), bonus))
            offset += BRAKE_SCAN_STEP
        out = [0.0] * n
        sqrt = math.sqrt
        for i in range(n):
            best = 1e9
            for step_off, bonus in scan:
                limit_sq = a_lat * radius[(i + step_off) % n] + bonus
                if limit_sq < best:
                    best = limit_sq
            out[i] = sqrt(best)
        return out

    def _update_following(self):
        """Адаптивный круиз всему потоку разом: один проход по кругу.

        Болванки сортируются по дуге, и передней для каждой оказывается
        следующая в этом порядке. Так вместо квадрата сравнений (до 132
        на тик при плотном траффике) выходит одна сортировка почти
        отсортированного списка.

        Ради чего это вообще: без ограничения быстрая болванка упирается
        в медленную и встаёт с ней борт о борт — ровно та стена поперёк
        трассы, которой быть не должно.
        """
        order = self._order
        del order[:]
        for car in self.cars:
            if car.active:
                order.append(car)
        count = len(order)
        if count == 0:
            return
        order.sort(key=_arc_of)
        length = self.length
        if count == 1:
            order[0].follow_speed = 1e9
            return
        for k in range(count):
            car = order[k]
            lead = order[k + 1] if k + 1 < count else order[0]
            gap = lead.arc - car.arc
            if gap < 0.0:
                gap += length
            if gap >= FOLLOW_GAP:
                car.follow_speed = 1e9
            elif gap <= FOLLOW_HARD:
                car.follow_speed = SPEED_MIN
            else:
                t = (gap - FOLLOW_HARD) / (FOLLOW_GAP - FOLLOW_HARD)
                car.follow_speed = lead.speed + (car.base_speed - lead.speed) * t

    def _avoid_lane(self, car, i, lane, effects):
        """Увести полосу в сторону, если впереди перекрытие.

        Без этого поток упирался бы в «кратковременное перекрытие полотна»
        и вставал поперёк трассы — ровно та стена, которой быть не должно.
        """
        block = effects.blocked_span(car.arc, AVOID_SCAN)
        if block is None:
            return lane
        low, high, half_width = block
        half_car = physics.CAR_RADIUS + AVOID_MARGIN
        if lane + half_car < low or lane - half_car > high:
            return lane            # полоса и так свободна
        # Считаем, с какой стороны от препятствия больше места.
        left_room = half_width - high
        right_room = low + half_width
        if left_room >= right_room:
            want = high + half_car
            limit = half_width - physics.CAR_RADIUS - 0.3
            if want > limit:
                want = limit
        else:
            want = low - half_car
            limit = -(half_width - physics.CAR_RADIUS - 0.3)
            if want < limit:
                want = limit
        return want

    # --- застревание ---------------------------------------------------------

    def _watch_stuck(self, car, dt, sim):
        """Болванка не имеет права стоять: чинится задним ходом, затем сносом."""
        if car.speed > STUCK_SPEED:
            car.stuck_time = 0.0
            return
        car.stuck_time += dt
        if car.stuck_time > STUCK_RESCUE:
            # Безнадёжно: переставляем далеко впереди лидера, где её не видно.
            self._rescue(car, sim)
            return
        if car.stuck_time > STUCK_PATIENCE and car.reverse_time <= 0.0:
            car.reverse_time = STUCK_REVERSE

    def _rescue(self, car, sim):
        """Перестановка застрявшей болванки — только вне поля зрения гонщиков."""
        arc = self._safe_arc(sim)
        side = 1.0 if self.rng.random() < 0.5 else -1.0
        self._place(car, arc, side)
        self.track.init_state(car.state)
        car.stuck_time = 0.0
        car.reverse_time = 0.0
        car.buttons = 0

    def _safe_arc(self, sim):
        """Дуга, до которой всем гонщикам ехать не меньше RESPAWN_MIN_GAP.

        Перестановка обязана произойти вне поля зрения: иначе болванка
        «возникнет перед носом» — ровно то, чего быть не должно. Ищем
        случайную дугу, удовлетворяющую условию по КАЖДОМУ гонщику;
        если за десять попыток не нашли (тесная трасса, много машин),
        отступаем на середину самого длинного свободного промежутка.
        """
        length = self.length
        cars = self._arc_scratch
        del cars[:]
        for race_car in getattr(sim, 'cars', ()):
            if not race_car.removed:
                cars.append(race_car.state.progress % length)
        if not cars:
            return self.rng.random() * length
        span = RESPAWN_MAX_GAP - RESPAWN_MIN_GAP
        for _ in range(10):
            arc = self.rng.random() * length
            ok = True
            for car_arc in cars:
                gap = _forward_gap(car_arc, arc, length)
                if gap < RESPAWN_MIN_GAP:
                    ok = False
                    break
            if ok:
                return arc
        cars.sort()
        best_arc = cars[0]
        best_gap = -1.0
        for k in range(len(cars)):
            gap = _forward_gap(cars[k], cars[(k + 1) % len(cars)], length)
            if len(cars) == 1:
                gap = length
            if gap > best_gap:
                best_gap = gap
                best_arc = cars[k]
        offset = RESPAWN_MIN_GAP + self.rng.random() * span
        if offset > best_gap * 0.5:
            offset = best_gap * 0.5
        return (best_arc + offset) % length

    # --- стык с симуляцией ---------------------------------------------------

    def collect(self, states, stats, count):
        """Дописать болванки в буферы ``physics.resolve_collisions``.

        Возвращает новое значение счётчика. Буферы растит вызывающий —
        ``Simulation`` выделяет их на всю гонку с запасом на траффик.
        """
        if not self.enabled:
            return count
        for car in self.cars:
            if not car.active:
                continue
            states[count] = car.state
            stats[count] = car.stats
            count += 1
        return count

    def fill_snapshot(self, out):
        """Дописать болванок в список кортежей снапшота (раздел 12.3).

        Кортеж: ``(ident, s_q, lat_q, dyaw_q, spd_q)``; ``ident`` несёт
        в младшей половине байта номер в пуле, в старшей — вид.
        """
        if not self.enabled:
            return
        length = self.length
        arc_q = protocol.quantize_arc
        lat_q = protocol.quantize_lateral
        ang_q = protocol.quantize_angle
        spd_q = protocol.quantize_speed
        for car in self.cars:
            if not car.active:
                continue
            out.append((car.id | (car.look << 4),
                        arc_q(car.arc, length),
                        lat_q(car.lateral),
                        ang_q(car.dyaw),
                        spd_q(car.speed)))

    def positions_into(self, out):
        """Дописать (дуга, скорость, гонщик_ли) болванок для правил размещения."""
        if not self.enabled:
            return out
        for car in self.cars:
            if car.active:
                out.append((car.arc, car.speed, False))
        return out

    def blast_token(self, car):
        """Токен болванки для ``RoadEvents.blast_spin``: после слотов гонщиков."""
        return protocol.MAX_CARS + car.id

    def __repr__(self):
        return '<TrafficSystem %s, болванок %d>' % (self.level, self.count)


# --- вспомогательное ---------------------------------------------------------

def _arc_of(car):
    """Ключ сортировки потока по дуге. Функция, а не lambda: её берут
    по имени раз в тик, замыкание в горячем пути не создаётся."""
    return car.arc


def _wrap_angle(a):
    """Угол в (-pi, pi]."""
    while a > math.pi:
        a -= _TWO_PI
    while a <= -math.pi:
        a += _TWO_PI
    return a


def _forward_gap(from_arc, to_arc, length):
    """Расстояние вперёд по кругу от одной дуги до другой."""
    gap = to_arc - from_arc
    if gap < 0.0:
        gap += length
    return gap


def _curvature_radii(xs, zs, n):
    """Радиус кривизны осевой линии в каждой точке, м.

    Считается один раз на гонку: водителю он нужен каждый тик, чтобы знать,
    насколько крутая дуга ждёт через тормозную дистанцию. Формула — радиус
    описанной окружности треугольника из трёх точек через две выборки.
    """
    out = [0.0] * n
    for i in range(n):
        ax = xs[(i - 2) % n]
        az = zs[(i - 2) % n]
        bx = xs[i]
        bz = zs[i]
        cx = xs[(i + 2) % n]
        cz = zs[(i + 2) % n]
        ab = math.hypot(bx - ax, bz - az)
        bc = math.hypot(cx - bx, cz - bz)
        ca = math.hypot(ax - cx, az - cz)
        area2 = abs((bx - ax) * (cz - az) - (cx - ax) * (bz - az))
        out[i] = 1.0e6 if area2 < 1e-9 else ab * bc * ca / (2.0 * area2)
    # Сглаживание минимумом по окну: оценка по трём точкам на четырёх метрах
    # дёргается, а водителю нужна ХУДШАЯ дуга рядом, а не средняя. Без этого
    # болванка успевала разогнаться между двумя соседними выборками шпильки.
    smooth = [0.0] * n
    for i in range(n):
        worst = out[i]
        for off in (-3, -2, -1, 1, 2, 3):
            r = out[(i + off) % n]
            if r < worst:
                worst = r
        smooth[i] = worst
    return smooth
