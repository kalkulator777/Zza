# -*- coding: utf-8 -*-
"""Генерация этажа.

ЭТАП 0a: ЗАГЛУШКА. Одна прямоугольная комната 64x48, стены по периметру,
десяток столбов и одна внутренняя перегородка с проходом. Нужна только для
того, чтобы физике и коллизиям было обо что стукаться.

Настоящая генерация (комнаты, коридоры, лестницы) — этап 1 по DESIGN.md 9.
"""

import random

from . import physics

ROOM_W = 64
ROOM_H = 48
N_PILLARS = 10


def generate(seed=1, floor=1, w=ROOM_W, h=ROOM_H):
    """Возвращает (Grid, [(x,y), ...] точки спавна)."""
    rnd = random.Random((seed * 1000003) ^ floor)
    g = physics.Grid(w, h)

    # пол везде, кроме рамки в один тайл
    for y in range(1, h - 1):
        row = y * w
        for x in range(1, w - 1):
            g.tiles[row + x] = physics.TILE_FLOOR

    # внутренняя перегородка: вертикальная стена с проходом посередине
    px = w // 2
    gap_y = h // 2
    for y in range(6, h - 6):
        if abs(y - gap_y) <= 2:
            continue          # проход в 5 клеток
        g.set(px, y, physics.TILE_WALL)

    # столбы 1x1, подальше от стен, перегородки и друг от друга
    pillars = []
    tries = 0
    while len(pillars) < N_PILLARS and tries < 500:
        tries += 1
        x = rnd.randrange(3, w - 3)
        y = rnd.randrange(3, h - 3)
        if abs(x - px) < 3:
            continue
        if any(abs(x - ox) < 4 and abs(y - oy) < 4 for ox, oy in pillars):
            continue
        pillars.append((x, y))
        g.set(x, y, physics.TILE_WALL)

    # точки спавна — в левой половине, подальше от перегородки
    spawns = []
    sx = 4.5
    sy = 4.5
    for i in range(8):
        spawns.append((sx + (i % 4) * 1.5, sy + (i // 4) * 1.5))
    spawns = [physics.free_spot(g, x, y, 0.35) for x, y in spawns]
    return g, spawns
