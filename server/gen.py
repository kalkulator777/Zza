# -*- coding: utf-8 -*-
"""Генерация этажа: комнаты, коридоры, лестница (DESIGN.md 8.1, 8.6, 8.7).

Этаж — ЦЕЛЬНАЯ карта, по которой скроллится камера, а не набор экранов
(8.1). Делается разбиением прямоугольника (BSP) на области, в каждой
области — комната, и соединением комнат по дереву разбиения.

Три свойства, ради которых выбран именно BSP:

* **Связность гарантирована конструкцией, а не удачей.** Всякий пол на
  карте рождается одним из двух способов: прямоугольник комнаты или
  коридор, прорытый от центра одной комнаты к центру другой. Дерево
  разбиения соединяется снизу вверх: на каждом узле левое поддерево
  сшивается с правым одним коридором. Значит пол — одна компонента
  связности по построению, без «сгенерировали и проверили, не вышло —
  бросили кубик ещё раз». Проверка (`Floor.repairs`) существует, но она
  сторож, а не механизм: на 1000 этажах она не срабатывает ни разу.
* **Столбы не режут комнату.** Столб ставится только туда, где все восемь
  соседей — пол, и не ближе двух клеток к другому столбу. Вокруг такого
  столба остаётся целое кольцо пола, обойти его можно всегда.
* **Детерминизм.** Один `random.Random`, засеянный (seed, floor), и ни
  одного обхода множества/словаря по хешу. Один сид — побайтово один этаж,
  иначе параметр «сид» в лобби (8.7) — обман.

Тема (8.6) — это НАБОР ПАРАМЕТРОВ, а не копия генератора: см. THEMES.
Вторая тема добавляется строкой в этот словарь, кода не прибавляется.

Лестница (8.1) — тайл TILE_STAIRS = 2. Физике он не виден как стена
(`physics.solid` считает стеной только 0), поэтому замуровать лестницу
нельзя: она всегда стоит на клетке пола внутри комнаты.
"""

import random

from . import nav
from . import physics

TILE_WALL = physics.TILE_WALL      # 0
TILE_FLOOR = physics.TILE_FLOOR    # 1
TILE_STAIRS = 2                    # проходима: всё, что не 0, физике не стена

# размер карты по умолчанию; потолок — 128x128 по 4.1
ROOM_W = 64
ROOM_H = 48
MAX_W = 128
MAX_H = 128
MIN_W = 24
MIN_H = 18

R_PLAYER = 0.35        # тот же радиус, что в world.R_PLAYER (4.1, 4.3)
MAX_SPAWNS = 8         # сколько точек появления готовим (room.MAX_PLAYERS)
# Сколько мест готовим под предметы алтаря (8.4). Генератор знает только
# «сколько мест», что на них положить — дело items.py; импортировать оттуда
# нельзя, items читает world, а world читает gen через room.
ALTAR_ITEMS = 3


class Theme(object):
    """Набор правил генератора (DESIGN.md 8.6).

    Вторая тема — это ещё одна строка в THEMES, а не второй генератор.
    Поэтому здесь только числа, и ни одной ветки «если тема такая-то».
    """

    __slots__ = ("name", "min_leaf", "min_room", "max_room", "room_shrink",
                 "corridor_w", "loops", "pillar_rate", "max_depth")

    def __init__(self, name, min_leaf=11, min_room=4, max_room=11,
                 room_shrink=0.5, corridor_w=1, loops=0.35,
                 pillar_rate=0.012, max_depth=6):
        self.name = name
        self.min_leaf = min_leaf        # меньше этого область не делим
        self.min_room = min_room        # минимальная сторона комнаты
        self.max_room = max_room        # максимальная сторона комнаты
        self.room_shrink = room_shrink  # насколько комната меньше своей области
        self.corridor_w = corridor_w    # ширина коридора в клетках
        self.loops = loops              # доля лишних связей (петли, не дерево)
        self.pillar_rate = pillar_rate  # столбов на клетку площади комнаты
        self.max_depth = max_depth      # глубина разбиения


# Темы. На этапе 1 сделана одна («залы»), вторая заводится здесь же —
# параметрами, а не копипастой. "пещеры" оставлены закомментированными
# как образец: включать их без замера бестиария и палитры рано (8.6).
THEMES = {
    "halls": Theme("halls"),
    # "caves": Theme("caves", min_leaf=10, min_room=4, max_room=9,
    #                room_shrink=0.45, corridor_w=2, loops=0.8,
    #                pillar_rate=0.03, max_depth=6),
}
DEFAULT_THEME = "halls"


def theme_names():
    """Список допустимых тем: сервер — источник истины по параметрам (8.7)."""
    return sorted(THEMES.keys())


def get_theme(name):
    return THEMES.get(name) or THEMES[DEFAULT_THEME]


class Floor(object):
    """Результат генерации. Всё, что знает о карте мир и комната."""

    __slots__ = ("grid", "spawns", "stairs", "rooms", "entry", "seed",
                 "floor", "theme", "repairs", "passable",
                 "entry_room", "stairs_room", "stairs_dist",
                 "altar", "altar_room", "altar_slots", "altar_detour",
                 "altars", "boss_room")

    def __init__(self, grid, spawns, stairs, rooms, entry, seed, floor, theme):
        self.grid = grid
        self.spawns = spawns        # [(x,y)] — точки появления, круг влезает
        self.stairs = stairs        # (tx,ty) — лестница вниз
        self.rooms = rooms          # [(x0,y0,x1,y1)] включительно
        self.entry = entry          # (tx,ty) — центр стартовой комнаты
        self.seed = seed
        self.floor = floor
        self.theme = theme
        self.repairs = 0            # сколько раз сработал сторож связности
        self.passable = 0           # число проходимых клеток
        self.entry_room = 0         # номер стартовой комнаты в rooms
        self.stairs_room = 0        # номер комнаты с лестницей
        self.stairs_dist = -1       # длина пути от входа до лестницы, клеток
        # алтарь (8.4): где лежит выбор этажа и во сколько клеток пути он
        # обходится сверх дороги вниз
        self.altar = None           # (tx,ty) — центр алтаря
        self.altar_room = -1        # номер комнаты с алтарём
        self.altar_slots = []       # [(x,y)] — куда класть предметы
        self.altar_detour = -1      # крюк в клетках пути (см. _pick_altars)
        # 8.4: алтарей на этаж 1 + (живых - 1) // 2, то есть ДО ЧЕТЫРЁХ при
        # потолке room.MAX_PLAYERS = 8. Генератор готовит все четыре места
        # сразу и всегда: сколько из них займут предметы, решает
        # items.populate по числу живых — карта от состава группы зависеть не
        # имеет права (8.7: один сид — одна карта).
        # Каждый элемент: ((tx,ty), номер комнаты, [(x,y) слоты], крюк).
        # altar/altar_room/altar_slots/altar_detour — первый из них.
        self.altars = []
        # 8.1: арена босса на каждом пятом этаже, -1 на остальных
        self.boss_room = -1


# --- карта расстояний ------------------------------------------------------
# Одна структура на три задачи: связность при генерации (здесь), поиск пути
# врагов (8.3) и распространение шума (8.2). 8.3 разрешал держать её в этом
# файле ровно до появления ВТОРОГО потребителя; на этапе 2b он появился, и
# волна переехала в server/nav.py. Имя gen.distance_map оставлено: генератор
# зовёт его в четырёх местах, и менять их ради переезда незачем.

distance_map = nav.distance_map


# --- разбиение -------------------------------------------------------------

def _split(rnd, th, area, depth, out):
    """Рекурсивное деление области. Возвращает узел дерева разбиения.

    Узел — либо лист (номер комнаты в out), либо пара (левый, правый).
    Именно по этому дереву потом сшиваются комнаты, поэтому соединяются
    соседние по разбиению области, а не случайные пары через всю карту.
    """
    x0, y0, x1, y1 = area
    aw = x1 - x0 + 1
    ah = y1 - y0 + 1
    can_v = aw >= 2 * th.min_leaf
    can_h = ah >= 2 * th.min_leaf
    if depth >= th.max_depth or not (can_v or can_h):
        out.append(area)
        return len(out) - 1
    if can_v and can_h:
        if aw > ah * 1.25:
            vertical = True
        elif ah > aw * 1.25:
            vertical = False
        else:
            vertical = rnd.random() < 0.5
    else:
        vertical = can_v
    if vertical:
        cut = rnd.randint(x0 + th.min_leaf - 1, x1 - th.min_leaf)
        a = _split(rnd, th, (x0, y0, cut, y1), depth + 1, out)
        b = _split(rnd, th, (cut + 1, y0, x1, y1), depth + 1, out)
    else:
        cut = rnd.randint(y0 + th.min_leaf - 1, y1 - th.min_leaf)
        a = _split(rnd, th, (x0, y0, x1, cut), depth + 1, out)
        b = _split(rnd, th, (x0, cut + 1, x1, y1), depth + 1, out)
    return (a, b)


def _room_in(rnd, th, area):
    """Комната внутри области, с отступом >= 1 от её краёв."""
    x0, y0, x1, y1 = area
    aw = x1 - x0 + 1 - 2
    ah = y1 - y0 + 1 - 2
    rw = max(th.min_room, min(th.max_room, aw - int(aw * th.room_shrink * rnd.random())))
    rh = max(th.min_room, min(th.max_room, ah - int(ah * th.room_shrink * rnd.random())))
    rw = min(rw, aw)
    rh = min(rh, ah)
    rx = x0 + 1 + rnd.randint(0, aw - rw)
    ry = y0 + 1 + rnd.randint(0, ah - rh)
    return (rx, ry, rx + rw - 1, ry + rh - 1)


def _fill(grid, x0, y0, x1, y1, v=TILE_FLOOR):
    w = grid.w
    h = grid.h
    tiles = grid.tiles
    if x0 < 0:
        x0 = 0
    if y0 < 0:
        y0 = 0
    if x1 > w - 1:
        x1 = w - 1
    if y1 > h - 1:
        y1 = h - 1
    for y in range(y0, y1 + 1):
        row = y * w
        for x in range(x0, x1 + 1):
            tiles[row + x] = v


def _corridor(grid, a, b, width):
    """Г-образный коридор из клетки a в клетку b. Прорывается целиком."""
    ax, ay = a
    bx, by = b
    k = width - 1
    # сначала по X, потом по Y (или наоборот — решает вызывающий, меняя a и b)
    x0, x1 = (ax, bx) if ax <= bx else (bx, ax)
    _fill(grid, x0, ay, x1, ay + k)
    y0, y1 = (ay, by) if ay <= by else (by, ay)
    _fill(grid, bx, y0, bx + k, y1 + k)


def _center(r):
    return ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)


def _connect_tree(grid, rnd, th, rooms, node):
    """Сшить поддеревья снизу вверх. Возвращает представителя поддерева.

    Здесь и живёт гарантия связности: на каждом узле ровно один коридор
    соединяет левую половину с правой, а лист представлен центром своей
    комнаты — то есть клеткой пола. Коридор прорывается целиком, значит
    пол левого поддерева и пол правого после этого — одна компонента.
    """
    if isinstance(node, int):
        return _center(rooms[node])
    a = _connect_tree(grid, rnd, th, rooms, node[0])
    b = _connect_tree(grid, rnd, th, rooms, node[1])
    if rnd.random() < 0.5:
        _corridor(grid, a, b, th.corridor_w)
    else:
        _corridor(grid, b, a, th.corridor_w)
    return a if rnd.random() < 0.5 else b


def _add_loops(grid, rnd, th, rooms):
    """Лишние связи: петли делают карту не деревом. Связность не трогают."""
    n = len(rooms)
    extra = int(n * th.loops)
    for _ in range(extra):
        i = rnd.randrange(n)
        j = rnd.randrange(n)
        if i == j:
            continue
        ci = _center(rooms[i])
        cj = _center(rooms[j])
        if abs(ci[0] - cj[0]) + abs(ci[1] - cj[1]) > max(grid.w, grid.h) // 2:
            continue          # петля через полкарты — это не петля, а шоссе
        _corridor(grid, ci, cj, th.corridor_w)


def _pillars(grid, rnd, th, rooms, keep_out):
    """Столбы. Ставятся только там, где вокруг целое кольцо пола.

    Поэтому столб не может разрезать комнату надвое: обойти его можно с
    любой стороны, и связность, доказанная выше, остаётся доказанной.
    """
    w = grid.w
    tiles = grid.tiles
    placed = []
    for r in rooms:
        x0, y0, x1, y1 = r
        area = (x1 - x0 + 1) * (y1 - y0 + 1)
        want = int(area * th.pillar_rate)
        tries = 0
        got = 0
        while got < want and tries < want * 12 + 8:
            tries += 1
            if x1 - x0 < 4 or y1 - y0 < 4:
                break
            px = rnd.randint(x0 + 2, x1 - 2)
            py = rnd.randint(y0 + 2, y1 - 2)
            if (px, py) in keep_out:
                continue
            if any(max(abs(px - ox), abs(py - oy)) < 2 for ox, oy in placed):
                continue
            ok = True
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if tiles[(py + dy) * w + px + dx] != TILE_FLOOR:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                continue
            tiles[py * w + px] = TILE_WALL
            placed.append((px, py))
            got += 1
    return placed


# --- точки появления и лестница --------------------------------------------

def _spiral(cx, cy, n):
    """n клеток вокруг (cx,cy) по расширяющимся кольцам, порядок фиксирован."""
    out = [(cx, cy)]
    ring = 1
    while len(out) < n and ring < 8:
        for dy in range(-ring, ring + 1):
            for dx in range(-ring, ring + 1):
                if max(abs(dx), abs(dy)) != ring:
                    continue
                out.append((cx + dx, cy + dy))
        ring += 1
    return out[:n]


def _spawn_points(grid, room, n):
    """Точки появления в стартовой комнате: круг игрока обязан влезать."""
    cx, cy = _center(room)
    pts = []
    for tx, ty in _spiral(cx, cy, n * 6):
        if len(pts) >= n:
            break
        if not (room[0] <= tx <= room[2] and room[1] <= ty <= room[3]):
            continue
        x = tx + 0.5
        y = ty + 0.5
        if physics.circle_hits(grid, x, y, R_PLAYER):
            continue
        pts.append((x, y))
    if not pts:
        # комната вырожденная быть не может (min_room >= 4), но если вдруг —
        # берём ближайшее свободное место, а не падаем
        pts = [physics.free_spot(grid, cx + 0.5, cy + 0.5, R_PLAYER)]
    i = 0
    while len(pts) < n:               # мест меньше, чем игроков: по кругу
        pts.append(pts[i % len(pts)])
        i += 1
    return pts[:n]


ARENA_MIN_SIDE = 8
# ВЫВОД ARENA_MIN_SIDE. «Обвал» босса накрывает круг 3.65 + 0.35 = 4.00
# клетки от его центра (server/boss.py), а единственный ответ на круг —
# разорвать дистанцию. В комнате со стороной меньше 8 игроку до стены
# меньше 4.00, то есть уйти от обвала, не выбежав из комнаты, нельзя вовсе.
# 8 — ровно тот размер, при котором из центра комнаты до стены ровно один
# радиус обвала. Замерено на 180 этажах: комната с минимальной стороной >= 8
# есть на КАЖДОМ (медиана лучшей комнаты 11, минимум 8), так что запасной
# ветке ниже работы не остаётся; она стоит потому, что «на минимальной карте
# 24x18 может» — тот же довод, что у _pick_altars.


def _pick_stairs(grid, rooms, dist, entry_idx, arena_min=0):
    """Лестница — в самой дальней от входа комнате, на клетке пола.

    Выбирается по карте расстояний, то есть по РЕАЛЬНОМУ пути, а не по
    прямой: замурованной или недостижимой она оказаться не может.

    arena_min > 0 (этаж босса, 8.1) — сначала среди комнат со стороной не
    меньше arena_min, и только если таких нет — как обычно. Лестница на
    этаже босса это АРЕНА: босс стоит на ней, и в чулане 5x5 бой с ним был
    бы не боем, а казнью.
    """
    w = grid.w
    best_room = entry_idx
    best_d = -1
    for i, r in enumerate(rooms):
        if i == entry_idx:
            continue
        if arena_min and min(r[2] - r[0] + 1, r[3] - r[1] + 1) < arena_min:
            continue
        cx, cy = _center(r)
        d = dist[cy * w + cx]
        if d > best_d:
            best_d = d
            best_room = i
    if arena_min and best_d < 0:
        return _pick_stairs(grid, rooms, dist, entry_idx, 0)
    r = rooms[best_room]
    cx, cy = _center(r)
    best = None
    best_score = None
    for ty in range(r[1], r[3] + 1):
        for tx in range(r[0], r[2] + 1):
            i = ty * w + tx
            if grid.tiles[i] != TILE_FLOOR or dist[i] < 0:
                continue
            if physics.circle_hits(grid, tx + 0.5, ty + 0.5, R_PLAYER):
                continue
            score = abs(tx - cx) + abs(ty - cy)
            if best_score is None or score < best_score:
                best_score = score
                best = (tx, ty)
    return best, best_room


# --- алтарь (8.4) ----------------------------------------------------------

ALTAR_MIN_GAP = 1.5     # между предметами; ВЫВОД в _altar_slots


def _altar_slots(grid, room, n, avoid=None):
    """Места под предметы внутри комнаты: пол, круг игрока влезает, врозь.

    ВЫВОД ALTAR_MIN_GAP: предмет берётся с items.PICK_R = 0.80 клетки. Если
    два предмета стоят ближе 1.6, игрок может стоять в радиусе обоих сразу,
    и «что именно я беру» становится вопросом к порядку обхода словаря, а не
    к игроку. 1.5 — тот же смысл с поправкой на то, что комната бывает 4
    клетки стороной и трём предметам в ней надо поместиться.

    Порядок перебора фиксирован (по клеткам комнаты, сверху вниз), поэтому
    один сид даёт те же места — детерминизм 8.7.
    """
    cx, cy = _center(room)
    cand = []
    for ty in range(room[1], room[3] + 1):
        for tx in range(room[0], room[2] + 1):
            if grid.tiles[ty * grid.w + tx] != TILE_FLOOR:
                continue          # в том числе лестница: на ней предмету не место
            if avoid is not None and (tx, ty) in avoid:
                continue
            x = tx + 0.5
            y = ty + 0.5
            if physics.circle_hits(grid, x, y, R_PLAYER):
                continue          # к предмету надо суметь подойти
            d = abs(tx - cx) + abs(ty - cy)
            cand.append((d, ty, tx, x, y))
    cand.sort()
    out = []
    for _, _, _, x, y in cand:
        if len(out) >= n:
            break
        if any(abs(x - ox) < ALTAR_MIN_GAP and abs(y - oy) < ALTAR_MIN_GAP
               for ox, oy in out):
            continue
        out.append((x, y))
    return out


ALTAR_ROOMS_MAX = 5
# ВЫВОД: 8.4 даёт алтарей по числу живых (items.altar_count), потолок живых —
# room.MAX_PLAYERS = 8, значит 1 + 8 // 2 = 5. Больше мест готовить незачем,
# меньше — значит запереть восьмерых на четырёх алтарях.


def _pick_altars(grid, rooms, dist, dist_st, entry_idx, stairs_room, base,
                 n, k=ALTAR_ROOMS_MAX):
    """До k комнат под алтари, самые дорогие с дороги вниз — первыми.

    Алтарей на этаж 1 + (живых - 1) // 2 (8.4), и это ЧИСЛО ЖИВЫХ, а не
    свойство карты: карта обязана быть одной и той же при одном сиде (8.7).
    Поэтому генератор готовит ВСЕ k мест всегда, а сколько занять —
    решает items.populate. Порядок фиксирован (крюк по убыванию, при равном
    крюке — номер комнаты), так что второй алтарь у одного сида всегда один
    и тот же.

    Комнаты РАЗНЫЕ: два алтаря в одной комнате — это шесть предметов в одном
    месте, то есть не «разделяться выгодно» (4.4), а «подошли вдвоём и взяли
    по одному, не расходясь».

    Правило выбора каждой комнаты — прежнее и целиком ниже.

    КРЮК = путь(вход -> алтарь) + путь(алтарь -> лестница) - путь(вход ->
    лестница). Это ровно те лишние клетки, которые группа пробежит в темноте
    мимо врагов, если пойдёт за апгрейдом. Ноль означает «алтарь и так по
    дороге», то есть подарок; поэтому берётся МАКСИМУМ, и поэтому комната
    лестницы и стартовая исключены — там крюк нулевой по построению.

    Выбор идёт по НАСТОЯЩЕМУ пути (карта расстояний), а не по прямой: иначе
    «дальняя» комната могла бы оказаться за стеной в двух шагах.

    ПОТОЛОК КРЮКА — САМА ДОРОГА ВНИЗ (base). Просто «самая дальняя комната»
    не годится: на настоящем этаже 64x48 это даёт крюк 132 клетки при пути
    вниз 86, то есть за апгрейд надо пройти этаж ещё дважды. Такую цену не
    платят никогда, а апгрейд, за которым не ходят, — это отсутствующий
    апгрейд. Поэтому берётся САМЫЙ ДАЛЬНИЙ ИЗ ТЕХ, ЧЕЙ КРЮК НЕ БОЛЬШЕ base:
    «сходить за предметом = пройти этот этаж ещё раз, максимум». Если таких
    нет вовсе (бывает на мелких картах), берётся ближайший — цена меньше
    обещанной не ломает ничего, цена больше обещанной ломает выбор.
    """
    w = grid.w
    inside = []          # крюк не больше дороги вниз: эти и нужны
    outside = []         # остальные: запасная скамейка
    for i, r in enumerate(rooms):
        if i == entry_idx or i == stairs_room:
            continue
        cx, cy = _center(r)
        j = cy * w + cx
        de = dist[j]
        ds = dist_st[j]
        if de < 0 or ds < 0:
            continue
        detour = de + ds - base
        if detour <= base:
            inside.append((-detour, i, detour))   # самый дорогой первым
        else:
            outside.append((detour, i, detour))   # самый дешёвый первым
    inside.sort()
    outside.sort()
    order = [(i, d) for _, i, d in inside] + [(i, d) for _, i, d in outside]
    if not order and stairs_room != entry_idx:
        # комнат всего две (стартовая и с лестницей) — такой этаж генератор
        # на 64x48 не делает, но на минимальной карте 24x18 может. Тогда
        # алтарь идёт в комнату лестницы: крюк нулевой, зато выбор есть.
        order = [(stairs_room, 0)]
    out = []
    for i, detour in order:
        if len(out) >= k:
            break
        slots = _altar_slots(grid, rooms[i], n)
        if not slots:
            continue
        out.append((_center(rooms[i]), i, slots, detour))
    return out


# --- сторож связности ------------------------------------------------------

def _repair(grid, dist, entry):
    """Сторож: если недостижимый пол всё же есть — прорыть к нему ход.

    По построению сюда попадать нечему, и проверка `tests/gen_check.py`
    показывает 0 срабатываний на 1000 этажах. Сторож оставлен потому, что
    игроку нельзя отдавать сломанный этаж, а тихо чинить — нельзя тем
    более: каждое срабатывание считается и видно в отчёте.
    """
    w = grid.w
    h = grid.h
    tiles = grid.tiles
    fixes = 0
    for _ in range(64):
        bad = -1
        for i in range(w * h):
            if tiles[i] != TILE_WALL and dist[i] < 0:
                bad = i
                break
        if bad < 0:
            break
        bx, by = bad % w, bad // w
        # ближайшая достижимая клетка — и прямой ход к ней
        near = None
        near_d = None
        for i in range(w * h):
            if dist[i] < 0:
                continue
            x, y = i % w, i // w
            d = abs(x - bx) + abs(y - by)
            if near_d is None or d < near_d:
                near_d = d
                near = (x, y)
        if near is None:
            break
        _corridor(grid, (bx, by), near, 1)
        fixes += 1
        dist[:] = distance_map(grid, [entry])
    return fixes


# --- главная функция -------------------------------------------------------

def generate(seed=1, floor=1, w=ROOM_W, h=ROOM_H, theme=DEFAULT_THEME):
    """Этаж из параметров лобби (8.7). Возвращает Floor.

    Детерминизм: всё случайное берётся из одного rnd, засеянного
    (seed, floor). Один сид — побайтово одна карта.
    """
    w = max(MIN_W, min(MAX_W, int(w)))
    h = max(MIN_H, min(MAX_H, int(h)))
    th = get_theme(theme)
    rnd = random.Random((int(seed) & 0x3FFFFFFF) * 1000003 ^ (int(floor) * 7919))

    grid = physics.Grid(w, h)          # всё стена, пол прорывается

    leaves = []
    tree = _split(rnd, th, (1, 1, w - 2, h - 2), 0, leaves)
    rooms = [_room_in(rnd, th, a) for a in leaves]
    for r in rooms:
        _fill(grid, r[0], r[1], r[2], r[3], TILE_FLOOR)

    _connect_tree(grid, rnd, th, rooms, tree)
    _add_loops(grid, rnd, th, rooms)

    entry_idx = rnd.randrange(len(rooms))
    entry = _center(rooms[entry_idx])

    # 8.1: босс на каждом пятом этаже. Импорт внутри функции сознательно:
    # boss.py тянет за собой combat -> items -> world -> ai, а ai тянет
    # boss, и на уровне модуля это кольцо. Зовётся generate раз на этаж, в
    # тике её нет, так что поиск в sys.modules здесь ничего не стоит.
    from . import boss as boss_mod
    arena = boss_mod.on_floor(floor)

    dist = distance_map(grid, [entry])
    stairs, stairs_room = _pick_stairs(grid, rooms, dist, entry_idx,
                                       ARENA_MIN_SIDE if arena else 0)

    keep_out = set()
    if stairs is not None:
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                keep_out.add((stairs[0] + dx, stairs[1] + dy))
    ex, ey = entry
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            keep_out.add((ex + dx, ey + dy))
    if arena:
        # АРЕНА БЕЗ СТОЛБОВ (8.1). Столб в арене ломает обе атаки босса
        # сразу: за ним не достаёт «Обвал» (круг), об него гибнет «Залп»
        # (прямая), и бой с боссом превращается в хоровод вокруг столба —
        # ровно та одна тактика, против которой боссу и даны две атаки.
        # Заодно это и есть видимая разница этажа: спуск стережёт пустой
        # зал, а не такая же комната с колоннами.
        ar = rooms[stairs_room]
        for ty in range(ar[1], ar[3] + 1):
            for tx in range(ar[0], ar[2] + 1):
                keep_out.add((tx, ty))
    _pillars(grid, rnd, th, rooms, keep_out)

    # столбы могли перекрыть отдельные клетки — карта расстояний пересчитана
    dist = distance_map(grid, [entry])
    fl_repairs = _repair(grid, dist, entry)

    if stairs is not None:
        grid.tiles[stairs[1] * w + stairs[0]] = TILE_STAIRS

    spawns = _spawn_points(grid, rooms[entry_idx], MAX_SPAWNS)

    # алтарь (8.4). Вторая карта расстояний — от лестницы: без неё «крюк»
    # посчитать нечем, а крюк и есть цена апгрейда. Стоит она столько же,
    # сколько первая (замер — tests/items_check.py, раздел «генератор»), и
    # платится РАЗ НА ЭТАЖ, в тике её нет.
    altars = []
    if stairs is not None:
        dist_st = distance_map(grid, [stairs])
        base = dist[stairs[1] * w + stairs[0]]
        altars = _pick_altars(
            grid, rooms, dist, dist_st, entry_idx, stairs_room, base,
            ALTAR_ITEMS)

    fl = Floor(grid, spawns, stairs, rooms, entry, int(seed), int(floor), th.name)
    fl.altars = altars
    first = altars[0] if altars else (None, -1, [], -1)
    fl.altar = first[0]
    fl.altar_room = first[1]
    fl.altar_slots = first[2]
    fl.altar_detour = first[3]
    fl.boss_room = stairs_room if (arena and stairs is not None) else -1
    fl.repairs = fl_repairs
    fl.entry_room = entry_idx
    fl.stairs_room = stairs_room
    fl.stairs_dist = dist[stairs[1] * w + stairs[0]] if stairs is not None else -1
    fl.passable = sum(1 for v in grid.tiles if v != TILE_WALL)
    return fl
