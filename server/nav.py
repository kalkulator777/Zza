# -*- coding: utf-8 -*-
"""Карта расстояний: одна структура на три задачи (DESIGN.md 8.2, 8.3).

Сюда перенесена `distance_map` из `gen.py`. Контракт 8.3 разрешал держать её
в генераторе ровно до появления ВТОРОГО потребителя — он появился: поиск пути
врагов (8.3) и распространение шума (8.2). Связность при генерации осталась
третьим потребителем и зовёт ту же функцию через `gen.distance_map`.

ЧТО ЗДЕСЬ ГЛАВНОЕ — ОДНА ВОЛНА НА ВСЕХ.

8.3 запрещает A* на каждого врага каждый тик и требует многоисточниковой
волны: все живые игроки кладутся в источники ОДНОЙ волны, а не по волне на
игрока. Замер 8.3 на этаже 64x48: одна волна 0.241 мс, шесть персональных
1.542 мс. Врагу нужен ближайший игрок — это ровно и есть многоисточниковая
волна, никакого выбора цели делать не надо: он уже сделан волной.

Пересчёт — раз в NAV_PERIOD тиков, врагам между пересчётами хватает старого
градиента: за 6 тиков игрок при 5 кл/с проходит 1 клетку (4.2), то есть
карта врёт не больше чем на клетку — меньше, чем ширина двери.

ГРАДИЕНТ И ДВЕРИ. Шаг считается по восьми соседям, диагональ разрешена только
когда ОБЕ соседние ортогонали проходимы (иначе тело срезало бы угол и въехало
в стену). Волна четырёхсвязная, поэтому в коридоре шириной в клетку
единственный убывающий сосед — ортогональный, и враг у самой двери едет
строго вдоль оси. Это ровно тот случай, который без подталкивания на углах
(4.2a) залипает навсегда; 8.3 прямо называет подталкивание предусловием
этого этапа. Цель шага — ЦЕНТР лучшей клетки, а не «вектор градиента»:
центр клетки проёма и есть середина створа, так что враг сам собирается в
полосу прохода, вместо того чтобы тереться о косяк.
"""

import math
from time import perf_counter as _clock

from . import physics

TILE_WALL = physics.TILE_WALL

# 8.3: пересчёт раз в 6 тиков на весь этаж.
NAV_PERIOD = 6

_BIG = 1 << 30


def distance_map(grid, sources, limit=-1):
    """BFS от списка клеток. Возвращает list[int], -1 = недостижимо.

    Перенесено из gen.py дословно: волновой обход по 4 соседям, стоимость
    клетки 1. limit >= 0 обрывает волну на этом расстоянии — на этом и
    держится шум (8.2): заполнены клетки с dist <= limit, дальше -1.
    """
    w = grid.w
    h = grid.h
    tiles = grid.tiles
    dist = [-1] * (w * h)
    frontier = []
    for tx, ty in sources:
        if 0 <= tx < w and 0 <= ty < h:
            i = ty * w + tx
            if tiles[i] != TILE_WALL and dist[i] < 0:
                dist[i] = 0
                frontier.append(i)
    d = 0
    while frontier:
        if limit >= 0 and d >= limit:
            break
        d += 1
        nxt = []
        for i in frontier:
            x = i % w
            if x > 0 and dist[i - 1] < 0 and tiles[i - 1] != TILE_WALL:
                dist[i - 1] = d
                nxt.append(i - 1)
            if x < w - 1 and dist[i + 1] < 0 and tiles[i + 1] != TILE_WALL:
                dist[i + 1] = d
                nxt.append(i + 1)
            j = i - w
            if j >= 0 and dist[j] < 0 and tiles[j] != TILE_WALL:
                dist[j] = d
                nxt.append(j)
            j = i + w
            if j < w * h and dist[j] < 0 and tiles[j] != TILE_WALL:
                dist[j] = d
                nxt.append(j)
        frontier = nxt
    return dist


class Field(object):
    """Карта расстояний до всех живых игроков, живёт между пересчётами.

    Владелец — World. Потребитель — ai.py. Счётчики waves/ms не украшение:
    без них нельзя отличить «волна дешёвая» от «волна не считается».
    """

    __slots__ = ("grid", "dist", "at_tick", "gen", "waves", "ms", "sources")

    def __init__(self, grid=None):
        self.grid = grid
        self.dist = None
        self.at_tick = -1 << 30
        self.gen = 0            # номер поколения: меняется на каждый пересчёт
        self.waves = 0          # сколько волн посчитано за жизнь поля
        self.ms = 0.0           # суммарное время волн, мс
        self.sources = 0        # сколько источников было в последней волне

    def rebuild(self, grid, sources, tick=0):
        t0 = _clock()
        self.grid = grid
        self.dist = distance_map(grid, sources)
        self.ms += (_clock() - t0) * 1000.0
        self.waves += 1
        self.gen += 1
        self.at_tick = tick
        self.sources = len(sources)
        return self.dist

    def maybe_rebuild(self, tick, grid, sources, period=NAV_PERIOD):
        """Пересчитать, если пора или если этаж сменился. True — считали."""
        if (self.dist is None or grid is not self.grid
                or tick - self.at_tick >= period):
            self.rebuild(grid, sources, tick)
            return True
        return False

    def drop(self):
        """Забыть карту: смена этажа, конец забега."""
        self.dist = None
        self.grid = None
        self.at_tick = -1 << 30

    # --- чтение ------------------------------------------------------------

    def at(self, tx, ty):
        """Расстояние до ближайшего источника или -1."""
        d = self.dist
        if d is None:
            return -1
        g = self.grid
        if 0 <= tx < g.w and 0 <= ty < g.h:
            return d[ty * g.w + tx]
        return -1

    def step_dir(self, x, y):
        """Куда катиться из точки (x,y). Возвращает (dx, dy, dist_here).

        (0.0, 0.0) — идти некуда: карты нет, клетка недостижима, или мы уже
        в локальном минимуме (стоим на источнике). Вектор нормирован.
        """
        dist = self.dist
        if dist is None:
            return 0.0, 0.0, -1
        g = self.grid
        w = g.w
        h = g.h
        tiles = g.tiles
        tx = int(x)
        ty = int(y)
        if tx < 0 or ty < 0 or tx >= w or ty >= h:
            return 0.0, 0.0, -1
        i = ty * w + tx
        d0 = dist[i]
        best = d0 if d0 >= 0 else _BIG
        bx = -1
        by = 0
        # ортогонали: они же решают, можно ли ходить по диагонали
        ok_l = tx > 0 and tiles[i - 1] != TILE_WALL
        ok_r = tx < w - 1 and tiles[i + 1] != TILE_WALL
        ok_u = ty > 0 and tiles[i - w] != TILE_WALL
        ok_d = ty < h - 1 and tiles[i + w] != TILE_WALL
        if ok_l:
            d = dist[i - 1]
            if 0 <= d < best:
                best = d
                bx = tx - 1
                by = ty
        if ok_r:
            d = dist[i + 1]
            if 0 <= d < best:
                best = d
                bx = tx + 1
                by = ty
        if ok_u:
            d = dist[i - w]
            if 0 <= d < best:
                best = d
                bx = tx
                by = ty - 1
        if ok_d:
            d = dist[i + w]
            if 0 <= d < best:
                best = d
                bx = tx
                by = ty + 1
        # диагонали — только через открытый угол: иначе тело радиуса r
        # въезжает в стену, которую волна считает обойдённой
        if ok_l and ok_u:
            j = i - w - 1
            if tiles[j] != TILE_WALL:
                d = dist[j]
                if 0 <= d < best:
                    best = d
                    bx = tx - 1
                    by = ty - 1
        if ok_r and ok_u:
            j = i - w + 1
            if tiles[j] != TILE_WALL:
                d = dist[j]
                if 0 <= d < best:
                    best = d
                    bx = tx + 1
                    by = ty - 1
        if ok_l and ok_d:
            j = i + w - 1
            if tiles[j] != TILE_WALL:
                d = dist[j]
                if 0 <= d < best:
                    best = d
                    bx = tx - 1
                    by = ty + 1
        if ok_r and ok_d:
            j = i + w + 1
            if tiles[j] != TILE_WALL:
                d = dist[j]
                if 0 <= d < best:
                    best = d
                    bx = tx + 1
                    by = ty + 1
        if bx < 0:
            return 0.0, 0.0, d0
        ddx = bx + 0.5 - x
        ddy = by + 0.5 - y
        n = math.sqrt(ddx * ddx + ddy * ddy)
        if n < 1e-9:
            return 0.0, 0.0, d0
        return ddx / n, ddy / n, d0


# --- шум (8.2) -------------------------------------------------------------
# Шум — НЕ отдельный механизм и не круг по радиусу. Это та же волна с
# ограничением дальности: за стеной звук идёт вокруг стены, как и положено,
# а «в соседней комнате через метр камня» его не слышно. Волна на 18 клеток
# стоит 0.046 мс (8.2), и считается она только в тик, когда шум был.

def noise_wave(grid, sources, radius):
    """Волна слышимости из клеток sources радиусом radius клеток ПУТИ.

    Источников может быть много: шесть игроков, выстреливших в один тик, —
    это ОДНА волна с шестью источниками, а не шесть волн (8.3). Слышно, если
    рядом хоть один источник, — ровно многоисточниковая волна и есть.
    """
    return distance_map(grid, sources, radius)
