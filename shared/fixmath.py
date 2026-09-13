"""Детерминированная математика.

ЗЕРКАЛО: static/js/game/fixmath.js — любая правка здесь должна быть повторена
там один-в-один. tools/crosscheck.py проверяет побитовое совпадение.

Зачем свои sin/cos вместо math.sin: Math.sin в JS и sin из libm в C могут
отличаться в последнем бите. Для клиентского предсказания это означало бы
медленный дрейф, неотличимый от настоящего бага. Полином ниже использует
только + - * / — а они по IEEE-754 корректно округляются одинаково в обоих
языках, значит результат совпадает побитово.
"""

import math

PI = 3.141592653589793
TWO_PI = 6.283185307179586
HALF_PI = 1.5707963267948966

# Коэффициенты ряда Тейлора для sin на [-pi/2, pi/2] (до x^13).
# Записаны десятичными литералами, чтобы оба языка разобрали их в один double.
C3 = 0.16666666666666666
C5 = 0.008333333333333333
C7 = 0.0001984126984126984
C9 = 2.755731922398589e-06
C11 = 2.505210838544172e-08
C13 = 1.6059043836821613e-10


def norm_angle(a):
    """Приводит угол к [-pi, pi)."""
    return a - TWO_PI * math.floor((a + PI) / TWO_PI)


def fsin(x):
    x = norm_angle(x)
    if x > HALF_PI:
        x = PI - x
    elif x < -HALF_PI:
        x = -PI - x
    u = x * x
    p = C13 * u - C11
    p = p * u + C9
    p = p * u - C7
    p = p * u + C5
    p = p * u - C3
    p = p * u + 1.0
    return x * p


def fcos(x):
    return fsin(x + HALF_PI)


def flen(x, y):
    return math.sqrt(x * x + y * y)


def clamp(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def sign(v):
    if v > 0.0:
        return 1.0
    if v < 0.0:
        return -1.0
    return 0.0
