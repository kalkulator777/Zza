"""Физика: свойства, которые обязаны выполняться независимо от настройки."""

import math

import pytest

from shared.carstep import (IN_DOWN, IN_HANDBRAKE, IN_LEFT, IN_RIGHT, IN_UP,
                            no_effects, step)
from shared.fixmath import fcos, fsin, norm_angle

DT = 1.0 / 60.0
ROAD = [1.0, 1.0, 1.0, 1.0]


class Car:
    def __init__(self, **kw):
        self.x = self.y = self.vx = self.vy = self.a = 0.0
        self.seg = 0
        for k, v in kw.items():
            setattr(self, k, v)


def drive(car, C, inp, ticks, surf=ROAD, eff=None):
    eff = eff or no_effects()
    for _ in range(ticks):
        step(car, inp, DT, surf, C, eff)
    return car


def test_fixmath_matches_libm():
    """Свой sin не обязан быть точным, но обязан быть близким."""
    for i in range(2000):
        x = -30.0 + i * 0.03
        assert abs(fsin(x) - math.sin(x)) < 1e-8
        assert abs(fcos(x) - math.cos(x)) < 1e-8


def test_norm_angle_range():
    for i in range(1000):
        a = -100.0 + i * 0.21
        n = norm_angle(a)
        assert -math.pi - 1e-9 <= n < math.pi + 1e-9
        assert abs(math.sin(n) - math.sin(a)) < 1e-9


def test_step_is_deterministic(phys):
    """Одинаковый вход — побитово одинаковый выход. На этом стоит предсказание."""
    a, b = Car(a=0.4), Car(a=0.4)
    for i in range(600):
        inp = IN_UP | (IN_LEFT if i % 7 < 3 else IN_RIGHT)
        step(a, inp, DT, ROAD, phys["car"], no_effects())
        step(b, inp, DT, ROAD, phys["car"], no_effects())
    assert (a.x, a.y, a.vx, a.vy, a.a) == (b.x, b.y, b.vx, b.vy, b.a)


def test_speed_is_capped(phys):
    car = drive(Car(), phys["car"], IN_UP, 1200)
    assert math.hypot(car.vx, car.vy) <= phys["car"]["maxSpeed"] + 1.0


def test_cannot_turn_while_standing(phys):
    """Машина на месте не должна крутиться вокруг оси."""
    car = Car(a=0.0)
    drive(car, phys["car"], IN_LEFT | IN_RIGHT, 60)
    assert abs(car.a) < 1e-9
    car2 = Car(a=0.0)
    drive(car2, phys["car"], IN_LEFT, 60)
    assert abs(car2.a) < 1e-6, "стоящая машина повернулась"


def test_brake_then_reverse(phys):
    car = drive(Car(), phys["car"], IN_UP, 120)
    fwd = car.vx * math.cos(car.a) + car.vy * math.sin(car.a)
    assert fwd > 100
    drive(car, phys["car"], IN_DOWN, 240)
    fwd = car.vx * math.cos(car.a) + car.vy * math.sin(car.a)
    assert fwd < -10, "после долгого торможения должен включиться задний ход"
    assert fwd >= -phys["car"]["maxReverse"] - 1


def test_handbrake_increases_slide(phys):
    """Ручник обязан заметно уменьшать сцепление — иначе он бесполезен."""
    def slide(hand):
        car = drive(Car(), phys["car"], IN_UP, 180)
        inp = IN_UP | IN_RIGHT | (IN_HANDBRAKE if hand else 0)
        drive(car, phys["car"], inp, 60)
        fx, fy = math.cos(car.a), math.sin(car.a)
        return abs(-car.vx * fy + car.vy * fx)
    assert slide(True) > slide(False) * 1.5


def test_no_control_ignores_input(phys):
    """control=0 (оглушение, отсчёт, респавн) должен отключать ВЕСЬ ввод."""
    eff = [1.0, 1.0, 1.0, 1.0, 0.0, 0.0]
    car = Car()
    drive(car, phys["car"], IN_UP | IN_LEFT | IN_HANDBRAKE, 120, eff=eff)
    assert car.x == 0.0 and car.y == 0.0 and car.a == 0.0


def test_reversed_control(phys):
    eff = [1.0, 1.0, 1.0, 1.0, 0.0, -1.0]
    normal = drive(Car(), phys["car"], IN_UP | IN_RIGHT, 120)
    flipped = drive(Car(), phys["car"], IN_UP | IN_RIGHT, 120, eff=eff)
    assert normal.a * flipped.a < 0, "перевёрнутое управление должно рулить наоборот"


def test_extreme_tuning_stays_stable(phys):
    """Ползунки F4 позволяют выкрутить константы во что угодно —
    симуляция не должна взрываться."""
    C = dict(phys["car"])
    C["grip"] = 5000.0
    C["drag"] = 900.0
    C["accel"] = 99999.0
    car = Car()
    drive(car, C, IN_UP | IN_LEFT, 600)
    assert all(math.isfinite(v) for v in (car.x, car.y, car.vx, car.vy, car.a))


@pytest.mark.parametrize("surf,slower", [
    ([0.72, 4.1, 0.45, 0.55], True),
    ([0.16, 0.7, 0.75, 1.0], False),
])
def test_surfaces_change_behaviour(phys, surf, slower):
    road = drive(Car(), phys["car"], IN_UP, 240, surf=[1.0, 1.0, 1.0, 1.0])
    other = drive(Car(), phys["car"], IN_UP, 240, surf=surf)
    vr = math.hypot(road.vx, road.vy)
    vo = math.hypot(other.vx, other.vy)
    if slower:
        assert vo < vr * 0.8, "на траве должно быть заметно медленнее"
