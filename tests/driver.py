"""Простой автопилот для тестов: рулит на точку впереди по осевой линии.

Ботов в игре нет — это только тестовая оснастка, чтобы гонять симуляцию без
браузера и проверять круги, столкновения и бонусы.
"""

import math

from shared.carstep import IN_UP, IN_LEFT, IN_RIGHT, IN_DOWN


def autopilot(car, track, look=5, sloppy=0.0):
    i = (car.seg + look) % track.nseg
    tx, ty = track.px[i], track.py[i]
    if sloppy:
        tx += math.cos(car.seg * 0.7) * sloppy
        ty += math.sin(car.seg * 1.3) * sloppy
    want = math.atan2(ty - car.y, tx - car.x)
    d = (want - car.a + math.pi) % (2 * math.pi) - math.pi
    inp = IN_UP
    if d > 0.06:
        inp |= IN_RIGHT
    elif d < -0.06:
        inp |= IN_LEFT
    if abs(d) > 1.1 and (car.vx * car.vx + car.vy * car.vy) > 300 * 300:
        inp |= IN_DOWN
    return inp
