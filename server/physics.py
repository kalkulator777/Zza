# -*- coding: utf-8 -*-
"""Движение и коллизии круга с тайловой сеткой (DESIGN.md 4.1, 4.2).

Единица — клетка. Тайл — целая клетка. Сущность — круг радиуса r.

Коллизия скользящая: движение разложено по осям, и упор в стену гасит
только ту ось, которая в неё упёрлась. Поэтому бег под углом в стену
превращается в бег вдоль стены, а не в залипание.

Поверх этого — подталкивание на углах (corner assist, см. ASSIST_REACH_R):
движение строго вдоль одной оси не встаёт намертво перед проёмом в одну
клетку, а доводится поперёк до створа. Без него тело радиуса 0.35 проходит
дверь только из полосы 0.30 клетки, а промахнувшись — стоит вечно.
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


# --- подталкивание на углах ----------------------------------------------
#
# ОТКУДА ПОРОГ (выведен из величин, не подобран под прогон).
#
# Тело — круг радиуса r, проём в стене — ровно одна клетка (4.1). Центр
# тела проходит в дверь только по полосе шириной 1 - 2r: при r = 0.35 это
# 0.30 клетки, то есть +-0.15 от середины створа. Промахнулся на 0.16 —
# круг цепляет соседний ряд, ход по оси гасится, и если движение идёт
# СТРОГО вдоль этой оси (одна зажатая клавиша у игрока; враг 8.3, катящийся
# по градиенту карты расстояний, у самой двери движется ровно так же), то
# поперечной скорости, которая вытащила бы тело в створ, взяться неоткуда.
# Замерено на голой физике: 150 тиков (5 с) в одной точке, 70 % подходов из
# клетки проёма не проходят никогда.
#
# Подталкивание обязано дотягивать РОВНО до края своей клетки и ни клеткой
# дальше: «я уже в дверях, доведи меня в створ» — да, «перетащи через угол
# в соседнюю дверь» — нет. Центр тела внутри клетки проёма — это промах не
# больше r: худший случай, центр ровно на границе клетки, до ближней
# кромки полосы прохода ровно (1 - 2r)/2 + (1 - 1 + 2r)/2 = r. Отсюда
#
#     ASSIST_REACH = ASSIST_REACH_R * r = r
#
# Полоса захвата = вся клетка проёма, 1.00 вместо 0.30 — в 3.3 раза шире
# при r = 0.35. Запас в обе стороны ровно по устройству: из соседней
# клетки требуется смещение строго больше r, и оно не даётся никогда, а
# внутри своей клетки хватает всегда. Число 0.35 нигде не зашито: reach
# считается от r той сущности, которая шагает.
ASSIST_REACH_R = 1.0

# Скорость подталкивания — ровно та, что отняла стена: шаг, не прошедший по
# заблокированной оси, тратится поперёк, и суммарное смещение за подшаг не
# растёт (было hypot(sdx, sdy) — столько и осталось). Отсюда worst case по
# времени: r / скорость = 0.35 / 5.0 = 0.07 с = 2.1 тика при беге 5 кл/с
# (4.2). Скорость сущности подталкивание не повышает никогда.


def _sign(v):
    return 0 if v == 0.0 else (1 if v > 0.0 else -1)


def _slip(grid, x, y, r, budget, allow_dir, axis_y):
    """Минимальное смещение поперёк, при котором круг в (x,y) свободен.

    Считается точно, а не перебором: для каждого сплошного тайла, который
    круг режет, берётся, насколько надо отойти, чтобы его не резать, и по
    каждому направлению берётся максимум таких требований. Ответ проверяется
    circle_hits — то есть предлагает арифметика, а решает геометрия.

    allow_dir: 0 — любое направление, +1/-1 — только указанное (чтобы не
    спорить с собственным поперечным ходом сущности).
    Возвращает смещение со знаком или None, если такого нет в пределах
    budget.
    """
    r2 = r * r
    tx0 = int(math.floor(x - r))
    tx1 = int(math.floor(x + r))
    ty0 = int(math.floor(y - r))
    ty1 = int(math.floor(y + r))
    plus = 0.0        # сколько надо пройти в +
    minus = 0.0       # сколько надо пройти в -
    cuts = False
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            if not grid.solid(tx, ty):
                continue
            cx = _clamp(x, tx, tx + 1.0)
            cy = _clamp(y, ty, ty + 1.0)
            dx = x - cx
            dy = y - cy
            if dx * dx + dy * dy >= r2 - EPS:
                continue                  # этот тайл круг не режет
            cuts = True
            if axis_y:
                lo, hi, cur = ty, ty + 1.0, y
            else:
                lo, hi, cur = tx, tx + 1.0, x
            # Отход берётся ПОЛНЫМ радиусом, а не по текущей глубине захода
            # (sqrt(r^2 - dx^2) было бы меньше). Это не запас на глазок, это
            # то, что делает порог независимым от глубины: иначе тело,
            # вжимаясь в стену, каждый тик получает новое, чуть меньшее
            # требование, оно снова влезает в reach — и мелкие подталкивания
            # сцепляются в одно большое. Замерено на этой самой проверке: по
            # глубине захода тело засасывало в проём из соседней клетки с
            # промахом до 0.175 (16 попыток из 80), при полном радиусе —
            # ни одной. Условие «дотянусь» превращается ровно в «центр тела
            # внутри клетки проёма», как и задумано.
            need = r
            d = (hi + need + EPS) - cur
            if d > plus:
                plus = d
            d = cur - (lo - need - EPS)
            if d > minus:
                minus = d
    if not cuts:
        return 0.0
    cand = []
    if allow_dir >= 0 and plus <= budget:
        cand.append(plus)
    if allow_dir <= 0 and minus <= budget:
        cand.append(-minus)
    cand.sort(key=abs)
    for o in cand:
        nx, ny = (x, y + o) if axis_y else (x + o, y)
        if not circle_hits(grid, nx, ny, r):
            return o
    return None


def _assist(grid, x, y, hx, hy, r, cap, allow_dir, axis_y):
    """Подвинуть тело поперёк, если это открывает заблокированный ход.

    (hx, hy) — точка, в которую тело не пустили: смещение считается ИМЕННО
    там («если бы я стоял в проёме, насколько надо поправиться»), а
    применяется при текущих (x, y) и проверяется circle_hits — поэтому
    подталкивание не проносит сквозь стену и не срезает угол: оно двигает
    тело только по свободному месту и только на долю шага.
    """
    if cap <= 0.0:
        return x, y
    o = _slip(grid, hx, hy, r, ASSIST_REACH_R * r, allow_dir, axis_y)
    if not o:
        return x, y                       # None (некуда) или 0.0 (не нужно)
    step = cap if abs(o) > cap else abs(o)
    if o < 0.0:
        step = -step
    nx, ny = (x, y + step) if axis_y else (x + step, y)
    if circle_hits(grid, nx, ny, r):
        return x, y                       # поперёк тоже стена — не лезем
    return nx, ny


def move_circle(grid, x, y, dx, dy, r, assist=True):
    """Сдвинуть круг на (dx,dy) со скольжением вдоль стен.

    Возвращает (x, y, hit), где hit — маска: 1 упёрся по X, 2 по Y.

    Шаг режется на подшаги не длиннее радиуса, иначе быстрая сущность
    (рывок 14 кл/с, снаряд 12 кл/с) проскакивает сквозь стену в один тик.

    assist=True — подталкивание на углах (ASSIST_REACH_R). Оно включается
    только тогда, когда ось действительно заблокирована, и двигает тело
    поперёк не дальше, чем отнял шаг по заблокированной оси. Поперечное
    направление берётся либо любое (если поперечного хода у сущности нет —
    именно этот случай и залипает), либо только совпадающее с собственным
    поперечным ходом: спорить с рулёжкой и ломать скольжение вдоль стены
    подталкивание не должно. Снаряду (прямая линия — часть его смысла)
    передавать assist=False.
    """
    hit = 0
    dist = max(abs(dx), abs(dy))
    steps = 1
    if dist > r:
        steps = int(dist / r) + 1
    sdx = dx / steps
    sdy = dy / steps
    # доля шага, которую отняла стена: ею и оплачивается подталкивание
    step_len = math.hypot(sdx, sdy)
    for _ in range(steps):
        if sdx:
            nx = x + sdx
            if circle_hits(grid, nx, y, r):
                if assist:
                    x, y = _assist(grid, x, y, nx, y, r,
                                   step_len - abs(sdy), _sign(sdy), True)
                    nx = x + sdx
                if circle_hits(grid, nx, y, r):
                    nx = _resolve_x(grid, nx, y, r, sdx > 0)
                    hit |= 1
            x = nx
        if sdy:
            ny = y + sdy
            if circle_hits(grid, x, ny, r):
                if assist:
                    x, y = _assist(grid, x, y, x, ny, r,
                                   step_len - abs(sdx), _sign(sdx), False)
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
