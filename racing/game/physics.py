# -*- coding: utf-8 -*-
"""Физика аркадной машины: один шаг симуляции, раздел 6 контракта.

Зеркало этого файла — ``static/js/physics.js``. Порядок операций, имена
констант и формулы совпадают построчно; правишь здесь — правь и там, иначе
предсказание на клиенте разъедется с сервером и картинку начнёт дёргать.

Как пользоваться на сервере::

    level = physics.step(car.state, car.stats, buttons, physics.DT, track,
                         car.state.sample_idx)
    if level:
        room.broadcast_drift_boost(car.slot, level)   # race_event drift_boost

Между шагами 14 и 15 (то есть уже внутри ``step``) столкновения не считаются:
машина-машина — это отдельный проход по всем машинам сразу, только на сервере::

    for car in cars: physics.step(...)         # шаги 1..16 каждой машины
    physics.resolve_collisions(states, stats, count)

Ограничения раздела 6, соблюдены буквально:

* внутри шага нет ``exp``, ``pow``, ``atan2`` и оператора ``**`` — все
  затухания заданы сразу «на шаг» при фиксированном ``dt = 1/60``;
* внутри шага ноль аллокаций: ни списков, ни кортежей, ни замыканий,
  ни временных объектов. Состояние — плоские поля ``CarState`` с ``__slots__``;
* высота (y) на физику не влияет, всё считается в плоскости (x, z).

Подкрученные константы (раздел 6.4 это прямо разрешает)
------------------------------------------------------
Стартовые значения давали вялый руль и пару прямых ошибок по величине.
Изменено ровно следующее, синхронно в обоих файлах:

* ``STEER_RATE``      3.2 -> 4.5   — полный руль за 0.22 с вместо 0.31 с.
  На 3.2 машина «доворачивала» уже после того, как поворот пройден.
* ``STEER_RETURN``    5.0 -> 6.5   — руль центруется за 0.15 с. Именно это
  делает занос ловимым: отпустил — машина сразу начала выпрямляться.
* ``TURN_FULL_SPEED`` 12.0 -> 9.0  — полная поворотливость доступна с 32 км/ч.
  Реальные скорости на трассе 12..22 м/с (см. ниже про ``roll``), с порогом
  12.0 машина недоворачивала ровно в самом ходовом диапазоне.
* ``DRIFT_TURN_GAIN`` 1.55 -> 1.7  — по ручнику машина охотно срывается
  в занос, а не «немного сильнее поворачивает».
* ``DRIFT_MIN_SPEED`` 8.0 -> 7.0   — дрифт доступен и в медленных шпильках.
* ``SLOW_FACTOR``     0.985 -> 0.995 — 0.985 на шаг это -18 м/с² на скорости
  20 м/с, вдвое сильнее двигателя: «Гроза» не замедляла, а почти
  останавливала. 0.995 снимает около 15 % скорости за свои 1.2 с.
* ``OFFTRACK_FACTOR`` 0.985 -> 0.992 — на 0.985 трава резала скорость до 43 %
  от трассовой, машина в ней вязла намертво. 0.992 даёт около 60 % —
  срезка наказана, но не смертельна.

Добавлены константы, которых в 6.4 не было, но без которых шаг недописан:
``REVERSE_MAX_SPEED``, ``BRAKE_REVERSE_SPEED``, ``DRIFT_STEER_MIN``,
``DRIFT_CHARGE_L1..L3``, ``DRIFT_BOOST_L1..L3``, ``COLLISION_RESTITUTION``,
``STEER_MAX``. Значения и причины — в комментариях у самих констант.

Места, где контракт пришлось дотолковать (все вынесены в отчёт)
---------------------------------------------------------------
1. ``max_speed_eff`` из шага 6 нигде не определён. Принято:
   ``boost_speed``, пока ``boost_time > 0``, иначе ``max_speed``.
2. Задний ход в шаге 6 ничем не ограничен: с ``reverse_force = 10`` и
   ``roll = 0.35`` машина разгонялась назад до 26 м/с. Введён
   ``REVERSE_MAX_SPEED`` и тот же вид формулы, что у газа.
3. ``shield_time`` в порядке операций не убывает вообще. Уменьшается
   в шаге 1 вместе с остальными таймерами.
4. Флага «вне трассы» нет в разделе 6.1, хотя шаг 11 его требует, а в
   снапшоте под него отведён бит. Добавлено поле ``offtrack``, его выставляет
   ``track.clamp_to_track`` (шаг 14), шаг 11 читает значение прошлого шага.
5. ``__slots__`` не даст ``game/track.py`` завести на состоянии свои поля,
   поэтому здесь заранее объявлены ``lap`` и ``checkpoint`` — их пишет
   ``advance_progress`` (раздел 7.4). Нужны ещё — добавлять сюда.
6. Затухание при обмене импульсом в столкновениях названо, но не задано
   числом: введён ``COLLISION_RESTITUTION``.
"""

from math import sin, cos, sqrt

__all__ = [
    "DT",
    "STEER_RATE", "STEER_RETURN", "STEER_MAX",
    "TURN_FULL_SPEED", "TURN_FALLOFF",
    "DRIFT_TURN_GAIN", "DRIFT_MIN_SPEED", "DRIFT_STEER_MIN",
    "DRIFT_CHARGE_L1", "DRIFT_CHARGE_L2", "DRIFT_CHARGE_L3",
    "DRIFT_BOOST_L1", "DRIFT_BOOST_L2", "DRIFT_BOOST_L3",
    "SPIN_RATE", "BOOST_ACCEL", "SLOW_FACTOR", "OFFTRACK_FACTOR",
    "BRAKE_REVERSE_SPEED", "REVERSE_MAX_SPEED",
    "WALL_BOUNCE", "COLLISION_PUSH", "COLLISION_RESTITUTION", "CAR_RADIUS",
    "BTN_THROTTLE", "BTN_BRAKE", "BTN_LEFT", "BTN_RIGHT", "BTN_DRIFT",
    "CarState", "CarStats", "step", "resolve_collisions",
]

# ---------------------------------------------------------------------------
# КОНСТАНТЫ (раздел 6.4)
# Все затухания — «на шаг» при DT = 1/60. Менять DT нельзя, не пересчитав их.
# ---------------------------------------------------------------------------

DT = 1.0 / 60.0                  # фиксированный шаг симуляции, с

STEER_RATE = 4.5                 # скорость набора угла руля, 1/с (было 3.2)
STEER_RETURN = 6.5               # скорость возврата руля в ноль, 1/с (было 5.0)
STEER_MAX = 1.0                  # предел |steer| (шаг 3 требует ограничения)

TURN_FULL_SPEED = 9.0            # м/с, выше — полная поворотливость (было 12.0)
TURN_FALLOFF = 0.45              # насколько срезается поворот на max_speed

DRIFT_TURN_GAIN = 1.7            # множитель поворота в заносе (было 1.55)
DRIFT_MIN_SPEED = 7.0            # м/с, ниже занос не начинается (было 8.0)
DRIFT_STEER_MIN = 0.35           # порог |steer| для заноса (раздел 6.3)

DRIFT_CHARGE_L1 = 0.7            # с заряда -> уровень 1, синие искры
DRIFT_CHARGE_L2 = 1.4            # с заряда -> уровень 2, оранжевые искры
DRIFT_CHARGE_L3 = 2.4            # с заряда -> уровень 3, фиолетовые искры
DRIFT_BOOST_L1 = 0.7             # с ускорения за уровень 1
DRIFT_BOOST_L2 = 1.2             # с ускорения за уровень 2
DRIFT_BOOST_L3 = 1.8             # с ускорения за уровень 3

SPIN_RATE = 9.0                  # рад/с раскрутки после урона
BOOST_ACCEL = 22.0               # м/с², подтягивание к boost_speed
SLOW_FACTOR = 0.995              # на шаг, пока slow_time > 0 (было 0.985)
OFFTRACK_FACTOR = 0.992          # на шаг вне трассы (было 0.985)

BRAKE_REVERSE_SPEED = 0.5        # м/с, ниже тормоз превращается в задний ход
REVERSE_MAX_SPEED = 9.0          # м/с, потолок заднего хода

WALL_BOUNCE = 0.35               # доля нормальной скорости после стены
COLLISION_PUSH = 0.6             # доля перекрытия, снимаемая за шаг
COLLISION_RESTITUTION = 0.35     # упругость обмена импульсом машина-машина
CAR_RADIUS = 1.1                 # м, круг столкновений

# Биты ввода. Значения обязаны совпадать с BTN_* из game/protocol.py;
# продублированы здесь, чтобы физика не тянула за собой сетевой модуль.
BTN_THROTTLE = 1 << 0
BTN_BRAKE = 1 << 1
BTN_LEFT = 1 << 2
BTN_RIGHT = 1 << 3
BTN_DRIFT = 1 << 4


class CarStats:
    """Характеристики машины из ``content/cars.json`` (раздел 6.5).

    Читаются в шаге по атрибутам, а не по ключам словаря: поиск в ``__dict__``
    класса со ``__slots__`` заметно дешевле хеширования строк, а шаг зовётся
    480 раз в секунду. ``game/cars.py`` может отдавать любой объект с этими
    же атрибутами — ``CarStats.from_dict`` здесь для удобства и для тестов.
    """

    __slots__ = (
        "engine_force", "max_speed", "brake_force", "reverse_force",
        "turn_rate", "grip_step", "drift_grip_step", "drag", "roll",
        "boost_speed", "mass",
    )

    def __init__(self, engine_force=14.0, max_speed=44.0, brake_force=26.0,
                 reverse_force=10.0, turn_rate=2.5, grip_step=0.17,
                 drift_grip_step=0.055, drag=0.0016, roll=0.35,
                 boost_speed=58.0, mass=1.0):
        self.engine_force = engine_force
        self.max_speed = max_speed
        self.brake_force = brake_force
        self.reverse_force = reverse_force
        self.turn_rate = turn_rate
        self.grip_step = grip_step
        self.drift_grip_step = drift_grip_step
        self.drag = drag
        self.roll = roll
        self.boost_speed = boost_speed
        self.mass = mass

    @classmethod
    def from_dict(cls, stats):
        """Собрать из словаря ``stats`` записи машины в cars.json."""
        return cls(
            engine_force=stats["engine_force"],
            max_speed=stats["max_speed"],
            brake_force=stats["brake_force"],
            reverse_force=stats["reverse_force"],
            turn_rate=stats["turn_rate"],
            grip_step=stats["grip_step"],
            drift_grip_step=stats["drift_grip_step"],
            drag=stats["drag"],
            roll=stats["roll"],
            boost_speed=stats["boost_speed"],
            mass=stats["mass"],
        )


class CarState:
    """Состояние машины, раздел 6.1.

    Первые четырнадцать полей — ровно перечисленные в контракте. Последние
    три (``offtrack``, ``lap``, ``checkpoint``) контракт не называет, но без
    них не собирается ни шаг 11, ни флаги снапшота, ни учёт кругов из 7.4;
    при ``__slots__`` их нельзя завести снаружи, поэтому они объявлены здесь.
    """

    __slots__ = (
        # --- раздел 6.1, в порядке контракта
        "x", "z",                # позиция, м
        "yaw",                   # курс, рад; 0 — нос в +Z
        "vx", "vz",              # скорость в мировых координатах, м/с
        "steer",                 # текущий угол руля, -1..1
        "drift_charge",          # накопленный заряд дрифта, с
        "drift_active",          # идёт ли занос
        "boost_time",            # остаток ускорения, с
        "spin_time",             # остаток раскрутки после урона, с
        "shield_time",           # остаток щита, с
        "slow_time",             # остаток замедления, с
        "progress",              # накопленный путь по трассе, м
        "sample_idx",            # индекс ближайшей точки осевой линии
        # --- расширения, см. докстринг модуля
        "offtrack",              # вне трассы (выставляет clamp_to_track)
        "lap",                   # пройдено полных кругов (advance_progress)
        "checkpoint",            # индекс следующей ожидаемой отсечки
    )

    def __init__(self, x=0.0, z=0.0, yaw=0.0):
        self.reset(x, z, yaw)

    def reset(self, x=0.0, z=0.0, yaw=0.0):
        """Поставить машину на решётку: позиция и курс, всё остальное в ноль."""
        self.x = x
        self.z = z
        self.yaw = yaw
        self.vx = 0.0
        self.vz = 0.0
        self.steer = 0.0
        self.drift_charge = 0.0
        self.drift_active = False
        self.boost_time = 0.0
        self.spin_time = 0.0
        self.shield_time = 0.0
        self.slow_time = 0.0
        self.progress = 0.0
        self.sample_idx = 0
        self.offtrack = False
        self.lap = 0
        self.checkpoint = 0

    def copy_from(self, other):
        """Скопировать чужое состояние поверх своего, без аллокаций.

        Нужно серверу (откат к авторитетному состоянию) и клиенту
        (реконсиляция из раздела 10.2)."""
        self.x = other.x
        self.z = other.z
        self.yaw = other.yaw
        self.vx = other.vx
        self.vz = other.vz
        self.steer = other.steer
        self.drift_charge = other.drift_charge
        self.drift_active = other.drift_active
        self.boost_time = other.boost_time
        self.spin_time = other.spin_time
        self.shield_time = other.shield_time
        self.slow_time = other.slow_time
        self.progress = other.progress
        self.sample_idx = other.sample_idx
        self.offtrack = other.offtrack
        self.lap = other.lap
        self.checkpoint = other.checkpoint


def step(state, car_stats, buttons, dt, track, hint):
    """Один шаг физики одной машины. Порядок операций — раздел 6.2, дословно.

    ``buttons`` — битовая маска ввода (раздел 5.2), ``track`` — объект с
    интерфейсом раздела 7.3, ``hint`` — индекс точки осевой линии, вокруг
    которого трасса ищет ближайшую (обычно ``state.sample_idx``).

    Возвращает уровень ускорения за занос, полученного ИМЕННО на этом шаге:
    0 — ничего, 1..3 — уровень из раздела 6.3. Сервер по ненулевому ответу
    рассылает ``race_event`` вида ``drift_boost``. Аллокаций нет: ответ —
    маленькое целое.
    """
    # --- состояние и характеристики в локальные имена: поиск атрибута в
    #     горячем цикле дороже локальной переменной, а шаг зовётся 480 раз/с
    yaw = state.yaw
    steer = state.steer
    spin_time = state.spin_time
    boost_time = state.boost_time
    slow_time = state.slow_time
    shield_time = state.shield_time
    drift_active = state.drift_active
    drift_charge = state.drift_charge
    vx = state.vx
    vz = state.vz

    max_speed = car_stats.max_speed
    boost_speed = car_stats.boost_speed
    turn_rate = car_stats.turn_rate

    btn_gas = buttons & BTN_THROTTLE
    btn_brake = buttons & BTN_BRAKE
    btn_left = buttons & BTN_LEFT
    btn_right = buttons & BTN_RIGHT
    btn_drift = buttons & BTN_DRIFT

    # --- шаг 1: раскрутка после урона глушит ввод и крутит машину
    if spin_time > 0.0:
        btn_gas = 0
        btn_brake = 0
        btn_left = 0
        btn_right = 0
        btn_drift = 0
        yaw += SPIN_RATE * dt
        spin_time -= dt
        if spin_time < 0.0:
            spin_time = 0.0
        spinning = True
    else:
        spinning = False
    # шаг 1, расширение: контракт нигде не уменьшает щит, делаем это здесь
    if shield_time > 0.0:
        shield_time -= dt
        if shield_time < 0.0:
            shield_time = 0.0

    # --- шаг 2: целевой угол руля
    steer_target = 0.0
    if btn_left:
        steer_target += 1.0
    if btn_right:
        steer_target -= 1.0

    # --- шаг 3: руль тянется к цели, без ввода — возвращается в ноль
    if steer_target != 0.0:
        steer_step = STEER_RATE * dt
    else:
        steer_step = STEER_RETURN * dt
    steer_delta = steer_target - steer
    if steer_delta > steer_step:
        steer += steer_step
    elif steer_delta < -steer_step:
        steer -= steer_step
    else:
        steer = steer_target
    if steer > STEER_MAX:
        steer = STEER_MAX
    elif steer < -STEER_MAX:
        steer = -STEER_MAX

    # --- шаг 4: базис машины (раздел 4: ноль yaw — нос в +Z)
    fwd_x = sin(yaw)
    fwd_z = cos(yaw)
    right_x = fwd_z
    right_z = -fwd_x

    # --- шаг 5: разложение скорости на продольную и боковую
    v_fwd = vx * fwd_x + vz * fwd_z
    v_lat = vx * right_x + vz * right_z

    # --- шаг 6: продольная сила (газ / тормоз / задний ход)
    if btn_gas:
        # max_speed_eff: под ускорением потолок поднимается до boost_speed
        if boost_time > 0.0:
            accel = car_stats.engine_force * (1.0 - v_fwd / boost_speed)
        else:
            accel = car_stats.engine_force * (1.0 - v_fwd / max_speed)
        if accel < 0.0:
            accel = 0.0
    elif btn_brake:
        if v_fwd > BRAKE_REVERSE_SPEED:
            accel = -car_stats.brake_force
        else:
            # задний ход гаснет к REVERSE_MAX_SPEED тем же видом формулы,
            # что и газ: v_fwd здесь отрицательный
            reverse_k = 1.0 + v_fwd / REVERSE_MAX_SPEED
            if reverse_k < 0.0:
                reverse_k = 0.0
            accel = -car_stats.reverse_force * reverse_k
    else:
        accel = 0.0
    v_fwd += accel * dt

    # --- шаг 7: ускорение от бонуса (и от заноса — уровни 1..3)
    if boost_time > 0.0:
        boost_delta = boost_speed - v_fwd
        boost_step = BOOST_ACCEL * dt
        if boost_delta > boost_step:
            v_fwd += boost_step
        elif boost_delta < -boost_step:
            v_fwd -= boost_step
        else:
            v_fwd = boost_speed
        boost_time -= dt
        if boost_time < 0.0:
            boost_time = 0.0

    # --- шаг 8: замедление от «Грозы»
    if slow_time > 0.0:
        v_fwd *= SLOW_FACTOR
        slow_time -= dt
        if slow_time < 0.0:
            slow_time = 0.0

    # --- шаг 9: поворот
    abs_fwd = -v_fwd if v_fwd < 0.0 else v_fwd
    speed_factor = abs_fwd / TURN_FULL_SPEED
    if speed_factor > 1.0:
        speed_factor = 1.0
    over_speed = abs_fwd - TURN_FULL_SPEED
    if over_speed <= 0.0:
        falloff = 1.0
    else:
        over_span = max_speed - TURN_FULL_SPEED
        if over_span > 0.0:
            over_t = over_speed / over_span
            if over_t > 1.0:
                over_t = 1.0
        else:
            over_t = 1.0
        falloff = 1.0 - TURN_FALLOFF * over_t
    turn = steer * turn_rate * speed_factor * falloff
    if v_fwd < 0.0:
        turn = -turn          # задним ходом руль работает наоборот
    if drift_active:
        turn *= DRIFT_TURN_GAIN
    yaw += turn * dt

    # --- шаг 10: боковое сцепление (в заносе оно резко ниже)
    if drift_active:
        v_lat *= 1.0 - car_stats.drift_grip_step
    else:
        v_lat *= 1.0 - car_stats.grip_step

    # --- шаг 11: сопротивление; вне трассы дополнительно вязнем
    abs_fwd = -v_fwd if v_fwd < 0.0 else v_fwd
    v_fwd -= (car_stats.drag * v_fwd * abs_fwd + car_stats.roll * v_fwd) * dt
    if state.offtrack:
        v_fwd *= OFFTRACK_FACTOR

    # --- шаг 12: сборка скорости обратно в мировые координаты
    vx = fwd_x * v_fwd + right_x * v_lat
    vz = fwd_z * v_fwd + right_z * v_lat

    # --- шаг 13: интегрирование позиции
    x = state.x + vx * dt
    z = state.z + vz * dt

    # состояние в поля до обращения к трассе: шаги 14 и 16 правят его на месте
    state.x = x
    state.z = z
    state.vx = vx
    state.vz = vz
    state.yaw = yaw
    state.steer = steer
    state.spin_time = spin_time
    state.shield_time = shield_time
    state.slow_time = slow_time
    state.boost_time = boost_time

    # --- шаг 14: границы трассы, выталкивание и гашение по WALL_BOUNCE
    track.clamp_to_track(state, hint)

    # (столкновения машина-машина — resolve_collisions, только на сервере,
    #  между шагами 14 и 15 для всех машин сразу; в шаг они не входят)

    # --- шаг 15: заряд дрифта и награда за занос (раздел 6.3)
    level = 0
    if spinning:
        # раскрутило — занос сбит, заряд сгорает без награды
        if drift_active:
            state.drift_active = False
        if drift_charge != 0.0:
            state.drift_charge = 0.0
    else:
        if btn_drift and v_fwd > DRIFT_MIN_SPEED and \
                (steer > DRIFT_STEER_MIN or steer < -DRIFT_STEER_MIN):
            if not drift_active:
                state.drift_active = True
            state.drift_charge = drift_charge + dt
        elif drift_active:
            # ручник отпущен, руль выпрямлен или скорость потеряна
            if drift_charge >= DRIFT_CHARGE_L3:
                level = 3
                reward = DRIFT_BOOST_L3
            elif drift_charge >= DRIFT_CHARGE_L2:
                level = 2
                reward = DRIFT_BOOST_L2
            elif drift_charge >= DRIFT_CHARGE_L1:
                level = 1
                reward = DRIFT_BOOST_L1
            else:
                reward = 0.0
            # новый буст не укорачивает уже идущий
            if reward > boost_time:
                state.boost_time = reward
            state.drift_active = False
            state.drift_charge = 0.0

    # --- шаг 16: progress, круги и отсечки
    track.advance_progress(state, hint)

    return level


def resolve_collisions(cars, stats, count):
    """Столкновения машина-машина, круг-круг радиуса ``CAR_RADIUS``.

    Считает ТОЛЬКО сервер и ТОЛЬКО между шагами 14 и 15 (раздел 6.2),
    для всех машин сразу; клиент их не предсказывает, поэтому функция
    намеренно не является частью ``step``.

    ``cars`` — последовательность ``CarState``, ``stats`` — параллельная ей
    последовательность характеристик (нужна ``mass``), ``count`` — сколько
    первых элементов участвует. Расталкивание и обмен импульсом делятся
    обратно пропорционально массе. Аллокаций нет: только индексы и числа.
    """
    diameter = CAR_RADIUS + CAR_RADIUS
    min_dist_sq = diameter * diameter
    i = 0
    last = count - 1
    while i < last:
        a = cars[i]
        mass_a = stats[i].mass
        j = i + 1
        while j < count:
            b = cars[j]
            dx = b.x - a.x
            dz = b.z - a.z
            dist_sq = dx * dx + dz * dz
            if dist_sq < min_dist_sq:
                mass_b = stats[j].mass
                if dist_sq > 1e-9:
                    dist = sqrt(dist_sq)
                    inv = 1.0 / dist
                    nx = dx * inv
                    nz = dz * inv
                    overlap = diameter - dist
                else:
                    # машины строго в одной точке — расталкиваем по оси X,
                    # направление зависит только от индексов, значит
                    # результат детерминирован
                    nx = 1.0
                    nz = 0.0
                    overlap = diameter

                # --- расталкивание, тяжёлую машину двигаем меньше
                total_mass = mass_a + mass_b
                push = overlap * COLLISION_PUSH / total_mass
                push_a = push * mass_b
                push_b = push * mass_a
                a.x -= nx * push_a
                a.z -= nz * push_a
                b.x += nx * push_b
                b.z += nz * push_b

                # --- обмен импульсом вдоль нормали, с затуханием
                rel_n = (b.vx - a.vx) * nx + (b.vz - a.vz) * nz
                if rel_n < 0.0:     # только если реально сближаются
                    inv_a = 1.0 / mass_a
                    inv_b = 1.0 / mass_b
                    impulse = -(1.0 + COLLISION_RESTITUTION) * rel_n / (inv_a + inv_b)
                    a.vx -= nx * impulse * inv_a
                    a.vz -= nz * impulse * inv_a
                    b.vx += nx * impulse * inv_b
                    b.vz += nz * impulse * inv_b
            j += 1
        i += 1
