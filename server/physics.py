# -*- coding: utf-8 -*-
"""Движение и коллизии круга с тайловой сеткой (DESIGN.md 4.1, 4.2).

Единица — клетка. Тайл — целая клетка. Сущность — круг радиуса r.

Коллизия скользящая: движение разложено по осям, и упор в стену гасит
только ту ось, которая в неё упёрлась. Поэтому бег под углом в стену
превращается в бег вдоль стены, а не в залипание.
"""

import math

TILE_WALL = 0
TILE_FLOOR = 1

EPS = 1e-6


class Grid(object):
    """Сетка тайлов. Всё за границей — стена."""

    __slots__ = ("w", "h", "tiles")

    def __init__(self, w, h, tiles=None):
        self.w = w
        self.h = h
        self.tiles = bytearray(tiles) if tiles is not None else bytearray(w * h)

    def at(self, tx, ty):
        if tx < 0 or ty < 0 or tx >= self.w or ty >= self.h:
            return TILE_WALL
        return self.tiles[ty * self.w + tx]

    def set(self, tx, ty, v):
        if 0 <= tx < self.w and 0 <= ty < self.h:
            self.tiles[ty * self.w + tx] = v

    def solid(self, tx, ty):
        return self.at(tx, ty) == TILE_WALL

    def solid_at(self, x, y):
        return self.solid(int(math.floor(x)), int(math.floor(y)))


def circle_hits(grid, x, y, r):
    """Пересекается ли круг (x,y,r) хоть с одним сплошным тайлом."""
    r2 = r * r
    tx0 = int(math.floor(x - r))
    tx1 = int(math.floor(x + r))
    ty0 = int(math.floor(y - r))
    ty1 = int(math.floor(y + r))
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            if not grid.solid(tx, ty):
                continue
            # ближайшая точка тайла [tx,tx+1]x[ty,ty+1] к центру круга
            cx = x if tx <= x <= tx + 1 else (tx if x < tx else tx + 1)
            cy = y if ty <= y <= ty + 1 else (ty if y < ty else ty + 1)
            dx = x - cx
            dy = y - cy
            if dx * dx + dy * dy < r2 - EPS:
                return True
    return False


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _resolve_x(grid, x, y, r, moving_right):
    """Выталкивание по X после шага. Возвращает исправленный x.

    Рассматриваются только тайлы, которые круг действительно режет и в
    которые он въехал спереди по ходу движения: тайл позади (из которого
    круг только что выехал) выталкивал бы в обратную сторону.
    """
    r2 = r * r
    ty0 = int(math.floor(y - r))
    ty1 = int(math.floor(y + r))
    tx0 = int(math.floor(x - r))
    tx1 = int(math.floor(x + r))
    best = None
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            if not grid.solid(tx, ty):
                continue
            if moving_right:
                if x >= tx + 1:      # тайл позади
                    continue
            else:
                if x <= tx:
                    continue
            cx = _clamp(x, tx, tx + 1.0)
            cy = _clamp(y, ty, ty + 1.0)
            dx = x - cx
            dy = y - cy
            if dx * dx + dy * dy >= r2 - EPS:
                continue             # этот тайл круг не режет
            need = math.sqrt(r2 - dy * dy)
            nx = (tx - need - EPS) if moving_right else (tx + 1.0 + need + EPS)
            if best is None:
                best = nx
            elif moving_right:
                best = min(best, nx)
            else:
                best = max(best, nx)
    return x if best is None else best


def _resolve_y(grid, x, y, r, moving_down):
    r2 = r * r
    tx0 = int(math.floor(x - r))
    tx1 = int(math.floor(x + r))
    ty0 = int(math.floor(y - r))
    ty1 = int(math.floor(y + r))
    best = None
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            if not grid.solid(tx, ty):
                continue
            if moving_down:
                if y >= ty + 1:
                    continue
            else:
                if y <= ty:
                    continue
            cx = _clamp(x, tx, tx + 1.0)
            cy = _clamp(y, ty, ty + 1.0)
            dx = x - cx
            dy = y - cy
            if dx * dx + dy * dy >= r2 - EPS:
                continue
            need = math.sqrt(r2 - dx * dx)
            ny = (ty - need - EPS) if moving_down else (ty + 1.0 + need + EPS)
            if best is None:
                best = ny
            elif moving_down:
                best = min(best, ny)
            else:
                best = max(best, ny)
    return y if best is None else best


def move_circle(grid, x, y, dx, dy, r):
    """Сдвинуть круг на (dx,dy) со скольжением вдоль стен.

    Возвращает (x, y, hit), где hit — маска: 1 упёрся по X, 2 по Y.

    Шаг режется на подшаги не длиннее радиуса, иначе быстрая сущность
    (рывок 14 кл/с, снаряд 12 кл/с) проскакивает сквозь стену в один тик.
    """
    hit = 0
    dist = max(abs(dx), abs(dy))
    steps = 1
    if dist > r:
        steps = int(dist / r) + 1
    sdx = dx / steps
    sdy = dy / steps
    for _ in range(steps):
        if sdx:
            nx = x + sdx
            if circle_hits(grid, nx, y, r):
                nx = _resolve_x(grid, nx, y, r, sdx > 0)
                hit |= 1
            x = nx
        if sdy:
            ny = y + sdy
            if circle_hits(grid, x, ny, r):
                ny = _resolve_y(grid, x, ny, r, sdy > 0)
                hit |= 2
            y = ny
    return x, y, hit


def free_spot(grid, x, y, r, max_ring=12):
    """Ближайшая свободная точка к (x,y) — для спавна и телепорта."""
    if not circle_hits(grid, x, y, r):
        return x, y
    for ring in range(1, max_ring + 1):
        for ty in range(-ring, ring + 1):
            for tx in range(-ring, ring + 1):
                if max(abs(tx), abs(ty)) != ring:
                    continue
                nx = x + tx
                ny = y + ty
                if not circle_hits(grid, nx, ny, r):
                    return nx, ny
    return x, y
