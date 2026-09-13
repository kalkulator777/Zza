"""Шаг физики машины — чистая функция, без знания о сети и трассе.

ЗЕРКАЛО: static/js/game/carstep.js — правки повторять один-в-один.
tools/crosscheck.py прогоняет одинаковые вводы через обе версии и требует
ПОБИТОВОГО совпадения траекторий. Если оно падает — это баг, а не шум.

Правило: только + - * / sqrt и свои fsin/fcos. Никаких math.sin, atan2,
hypot, pow — они не гарантируют одинаковый результат в Python и JS.
"""

from .fixmath import fsin, fcos, norm_angle

# Биты ввода
IN_UP = 1
IN_DOWN = 2
IN_LEFT = 4
IN_RIGHT = 8
IN_HANDBRAKE = 16
IN_USE = 32

# Индексы покрытий. Порядок фиксирован и передаётся клиенту вместе с трассой.
SURFACE_ORDER = ["road", "grass", "sand", "ice", "boost", "wall"]


def step(car, inp, dt, surf, C, eff):
    """Продвигает car на dt секунд.

    car  — изменяемый объект с полями x, y, vx, vy, a
    inp  — битовая маска ввода
    surf — модификаторы покрытия [grip, drag, accel, maxSpeed]
    C    — константы машины из physics.json
    eff  — эффекты бонусов [accelMul, maxSpeedMul, steerMul, gripMul, spin, control]
           control: 1 обычно, -1 управление наоборот, 0 нет управления
    """
    control = eff[5]

    if control == 0.0:
        throttle = 0.0
        braking = 0.0
        steer = 0.0
    else:
        throttle = 1.0 if (inp & IN_UP) else 0.0
        braking = 1.0 if (inp & IN_DOWN) else 0.0
        steer = (1.0 if (inp & IN_RIGHT) else 0.0) - (1.0 if (inp & IN_LEFT) else 0.0)
        if control < 0.0:
            steer = -steer

    # Ручник тоже отключается вместе с управлением: иначе зажатый Shift на
    # отсчёте давал бы клиенту физику, отличную от серверной.
    handbrake = 0.0 if control == 0.0 else (1.0 if (inp & IN_HANDBRAKE) else 0.0)

    ca = fcos(car.a)
    sa = fsin(car.a)

    # Раскладываем скорость на продольную и поперечную в ТЕКУЩЕМ базисе машины.
    vf = car.vx * ca + car.vy * sa
    vl = -car.vx * sa + car.vy * ca

    max_speed = C["maxSpeed"] * surf[3] * eff[1]
    accel = C["accel"] * surf[2] * eff[0]

    # Двигатель
    if throttle > 0.0:
        if vf < max_speed:
            vf = vf + accel * dt
            if vf > max_speed:
                vf = max_speed

    # Тормоз, затем задний ход
    if braking > 0.0:
        if vf > 0.0:
            vf = vf - C["brake"] * dt
            if vf < 0.0:
                vf = 0.0
        else:
            vf = vf - C["reverseAccel"] * dt
            max_rev = -C["maxReverse"]
            if vf < max_rev:
                vf = max_rev

    # Сопротивление. Коэффициент зажат в [0,1]: живая подстройка (F4) позволяет
    # выкрутить константы так, что явная схема разойдётся в осцилляцию.
    kd = C["drag"] * surf[1] * dt
    if kd > 1.0:
        kd = 1.0
    elif kd < 0.0:
        kd = 0.0
    vf = vf - vf * kd

    # Боковое сцепление: чем меньше, тем сильнее занос.
    if handbrake > 0.0:
        grip = C["handbrakeGrip"]
    else:
        grip = C["grip"]
    kg = grip * surf[0] * eff[3] * dt
    if kg > 1.0:
        kg = 1.0
    elif kg < 0.0:
        kg = 0.0
    vl = vl - vl * kg

    # Поворот: на месте не крутимся, на большой скорости руль тяжелеет.
    av = vf if vf >= 0.0 else -vf
    sf = av / C["steerFullSpeed"]
    if sf > 1.0:
        sf = 1.0
    sf = sf / (1.0 + av * C["steerFalloff"])

    turn = C["steerRate"] * eff[2] * steer * sf
    if vf < 0.0:
        turn = -turn
    if handbrake > 0.0:
        turn = turn * C["handbrakeSteerBonus"]

    # Скорость собираем обратно в СТАРОМ базисе — поперечная составляющая
    # остаётся в мировых координатах, это и есть занос.
    car.vx = vf * ca - vl * sa
    car.vy = vf * sa + vl * ca

    car.a = norm_angle(car.a + turn * dt + eff[4] * dt)
    car.x = car.x + car.vx * dt
    car.y = car.y + car.vy * dt


def no_effects():
    return [1.0, 1.0, 1.0, 1.0, 0.0, 1.0]


def drive_tick(car, inp, dt, track, C, CC, eff, surfaces):
    """Один тик езды: покрытие под машиной -> физика -> стены.

    ЗЕРКАЛО: driveTick в static/js/game/carstep.js.

    Это единица, которую сервер выполняет для каждой машины, а клиент — только
    для своей (предсказание). Столкновений машин здесь нет: их считает исключи-
    тельно сервер, клиент их не предсказывает.

    Возвращает силу удара о стену (0 — не было).
    """
    q = track.query(car.x, car.y, car.seg)
    car.seg = q[0]
    step(car, inp, dt, surfaces[q[4]], C, eff)

    q = track.query(car.x, car.y, car.seg)
    car.seg = q[0]
    return track.resolve_wall(car, q[2], q[3], q[5], q[6], CC)
