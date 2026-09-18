# -*- coding: utf-8 -*-
"""ИИ врагов: градиент, шум, зрение через туман (DESIGN.md 4.2, 4.4, 8.2, 8.3).

Три решения, на которых держится весь этап, и все три — переиспользование
уже оплаченного, а не новый механизм.

**ПУТЬ — ОДНА ВОЛНА НА ВСЕХ (8.3).** Никакого A* на врага. Волна
`nav.Field` считается раз в NAV_PERIOD тиков со ВСЕМИ живыми игроками в
источниках, враги катятся по её градиенту. Выбор цели делать не нужно:
многоисточниковая волна уже ведёт каждого к ближайшему игроку. Замер — в
`tests/ai_check.py`.

**ЗРЕНИЕ — ОДИН БАЙТ ТУМАНА (4.4).** Враг на клетке `VIS_LIT` стоит в
прямой видимости кого-то из группы; обзор почти симметричен, значит и он
видит группу. Луч от врага к игроку не бросается НИ ОДНОГО: 200 лучей на
30 Гц — это ровно тот расход, которым 4.4 объясняет своё решение. Цена
здесь — одно чтение `fog.state[i]`, и туман всё равно уже посчитан для
снапшота. Цена альтернативы замерена в `tests/ai_check.py`.

Обратная сторона честная и намеренная: враг видит ровно то же, что группа.
Игрок, стоящий за углом, невидим для врага, даже если напарник этот угол
освещает — нет, наоборот: если напарник освещает клетку врага, врага
«видит» вся группа, и он тоже поднят. Это общий обзор (4.4), а не личный,
и разводить его обратно значило бы вернуть 200 лучей.

**ШУМ — ТА ЖЕ ВОЛНА С ОГРАНИЧЕНИЕМ ДАЛЬНОСТИ (8.2).** Не круг по радиусу:
круг слышен сквозь скалу. Волна `nav.noise_wave` идёт по полу, огибая
стены, поэтому «через стену в двух шагах» не слышно, а «в соседней комнате
через дверь» слышно. Дальний бой громкий, ближний тихий, рывок почти
беззвучный — числа и их вывод в world.NOISE_*.

ПОЧЕМУ УДАР ПО ВРАГУ НЕ ПОДНИМАЕТ ЕГО ОТДЕЛЬНОЙ СТРОКОЙ. Он уже поднят
шумом самого удара, и это следствие чисел, а не удача: дальность снаряда
14 клеток (4.2) МЕНЬШЕ слышимости выстрела 18 клеток по пути, а ближняя
атака достаёт на 1.55 клетки при слышимости 6. Попасть по врагу, который
не слышал выстрела, физически нельзя.
"""

import math
import random

from . import combat
from . import nav
from . import physics
from . import vis as vis_mod
from . import world as world_mod

K_ENEMY = world_mod.K_ENEMY
K_PLAYER = world_mod.K_PLAYER
F_DEAD = world_mod.F_DEAD
F_OFFLINE = world_mod.F_OFFLINE
F_WINDUP = world_mod.F_WINDUP
VIS_LIT = vis_mod.VIS_LIT
TICK_HZ = world_mod.TICK_HZ

# --- виды врагов -----------------------------------------------------------
AI_NONE = 0
AI_MELEE = 1        # прёт в ближний бой
AI_RANGED = 2       # держит дистанцию и стреляет

# Оба вида уходят на провод одним kind = K_ENEMY. Пятое значение kind
# потребовало бы строки в таблице KIND клиента, а клиент сейчас в чужих
# руках (правило 4). Предложение правки — в отчёте.

# --- общие числа ----------------------------------------------------------
# Скорость врага НИЖЕ бега игрока (5.0 кл/с, 4.2) намеренно: убежать можно
# всегда, опасна толпа и окружение, а не догонялки.
MELEE_SPEED = 3.2
RANGED_SPEED = 2.8

MELEE_HP = 40       # 2 удара игрока (20, 4.2)
RANGED_HP = 25      # 2 снаряда (12) или 2 удара
ENEMY_R = 0.35      # тот же радиус, что у игрока: проём в клетку проходим

# Сколько тиков враг гонится, потеряв игрока из виду или услышав шум.
# ВЫВОД: услышавший выстрел стоит не дальше NOISE_SHOT = 18 клеток пути, а
# идёт со скоростью 3.2 кл/с, то есть 18 / 3.2 = 5.6 с = 169 тиков. Меньше —
# и враг разворачивается на полпути, то есть шум ничего не притягивает.
# Округлено вверх до 180 тиков (6 с), запас 6%.
ALERT_TICKS = 180

# --- ближний бой врага (4.2: замах врага 12-18 тиков) ---------------------
MELEE_WIND = 12                 # 0.400 с — нижний край вилки 4.2
MELEE_CD = 36                   # 1.2 с между ударами: 20 урона -> 16.7 hp/с
MELEE_START = 1.5               # начинать замах с этой дистанции
# ВЫВОД MELEE_START: удар достаёт на combat.MELEE_REACH + радиус цели =
# 1.2 + 0.35 = 1.55 клетки. Начинать замах дальше — гарантированный промах
# даже по неподвижному.
MELEE_STOP = 1.0                # ближе не подходить: 0.35+0.35 тел + зазор
MELEE_CREEP = 0.35              # доля скорости на замахе

# --- дальний бой врага ----------------------------------------------------
RANGED_WIND = 18                # 0.600 с — верхний край вилки 4.2
RANGED_CD = 60                  # 2 с между выстрелами: 12 урона -> 6 hp/с
RANGED_NEAR = 5.0               # ближе — пятится
RANGED_FAR = 9.0                # дальше — подходит
RANGED_LOS_EVERY = 3            # луч на выстрел проверяется раз в 3 тика
# ВЫВОД RANGED_NEAR/FAR: дальность снаряда 14 клеток (4.2), радиус обзора
# 10 (4.1). Полоса 5..9 целиком внутри обзора и внутри дальности, то есть
# стрелок виден игроку ровно тогда, когда сам стреляет.

# --- расстановка (8.1) ----------------------------------------------------
ENEMY_BASE = 8          # на первом этаже
ENEMY_PER_FLOOR = 4
ENEMY_CAP = 48
RANGED_EVERY = 3        # каждый третий — стрелок
# ВЫВОД ПОТОЛКА: бюджет 2.2 считается на 200 сущностях. 48 врагов + 8
# игроков + снаряды — вчетверо ниже потолка, и при 10 комнатах на этаже это
# около 5 врагов на комнату: бой, а не стена из тел.


def count_for(floor):
    """Сколько врагов на этаже глубины floor."""
    n = ENEMY_BASE + ENEMY_PER_FLOOR * (int(floor) - 1)
    return max(0, min(ENEMY_CAP, n))


def make_enemy(w, x, y, ai_kind):
    """Завести врага. Урон он получает через combat.damage без единой правки."""
    if ai_kind == AI_RANGED:
        hp, speed = RANGED_HP, RANGED_SPEED
    else:
        hp, speed = MELEE_HP, MELEE_SPEED
    e = w.spawn(K_ENEMY, x, y, r=ENEMY_R, speed=speed, hp=hp, hp_max=hp)
    e.ai = ai_kind
    return e


def populate(w, fl, rnd=None):
    """Расставить врагов на этаже (8.1): по комнатам, кроме стартовой.

    Детерминизм ровно как у генератора: один Random, засеянный (seed, floor),
    и обход СПИСКА комнат, а не множества. Один сид — один и тот же бестиарий.

    ЧИТАЕТСЯ ТОЛЬКО ТО, ЧТО ЕСТЬ. room.start() до этого этапа брал у этажа
    ровно .grid/.spawns/.stairs, и на этом стоят стенды с картой-заглушкой
    (tests/two_clients.py). Поэтому rooms/entry_room/seed/floor берутся
    через getattr: нет комнат — нет и врагов, 8.1 ставит их именно по
    комнатам. Падать из-за заглушки расстановка не имеет права.
    """
    rooms_all = getattr(fl, "rooms", None)
    if not rooms_all:
        return []
    floor_n = int(getattr(fl, "floor", 1))
    entry_room = getattr(fl, "entry_room", -1)
    if rnd is None:
        rnd = random.Random(((int(getattr(fl, "seed", 0)) & 0x3FFFFFFF) * 2654435761
                             ^ (floor_n * 40503) ^ 0x5EED))
    rooms = [r for i, r in enumerate(rooms_all) if i != entry_room]
    if not rooms:
        return []
    want = count_for(floor_n)
    grid = w.grid
    out = []
    taken = []
    tries = 0
    while len(out) < want and tries < want * 40 + 40:
        tries += 1
        r = rooms[rnd.randrange(len(rooms))]
        tx = rnd.randint(r[0], r[2])
        ty = rnd.randint(r[1], r[3])
        x = tx + 0.5
        y = ty + 0.5
        if physics.circle_hits(grid, x, y, ENEMY_R):
            continue
        if any(abs(x - ox) < 1.0 and abs(y - oy) < 1.0 for ox, oy in taken):
            continue      # не в одной точке: иначе толпа выглядит одним телом
        taken.append((x, y))
        kind = AI_RANGED if (len(out) % RANGED_EVERY) == RANGED_EVERY - 1 else AI_MELEE
        e = make_enemy(w, x, y, kind)
        e.facing = rnd.random() * 6.283
        out.append(e)
    return out


# --- слух (8.2) ------------------------------------------------------------

def _hear(w, tick, enemies):
    """Разобрать шумы прошлого тика. Одна волна на (команда, громкость).

    Волны считаются только когда шум БЫЛ. Шесть игроков, стреляющих в один
    тик, дают ОДНУ волну с шестью источниками, а не шесть волн: слышно, если
    рядом хоть один источник, а это ровно многоисточниковая волна (8.3).
    """
    noises = w.noises
    if not noises:
        return 0
    groups = {}
    for tx, ty, loud, team in noises:
        key = (team, loud)
        g = groups.get(key)
        if g is None:
            groups[key] = [(tx, ty)]
        else:
            g.append((tx, ty))
    del noises[:]
    teams = set()
    for e in enemies:
        teams.add(e.team)
    gw = w.grid.w
    until = tick + ALERT_TICKS
    waves = 0
    for (team, loud), srcs in groups.items():
        if not (teams - {team}):
            continue          # слушать некому: волну не считаем вовсе
        d = nav.noise_wave(w.grid, srcs, loud)
        waves += 1
        w.noise_waves += 1
        for e in enemies:
            if e.team == team:
                continue
            if d[int(e.y) * gw + int(e.x)] >= 0:
                e.ai_alert = until
    return waves


# --- прямая видимость для выстрела ----------------------------------------

def line_clear(grid, x0, y0, x1, y1, step=0.4):
    """Есть ли прямая без стен. Нужна ТОЛЬКО стрелку перед выстрелом.

    Это не «зрение» (зрение — байт тумана, см. шапку), а проверка ствола:
    стрелять в стену в упор глупо. Зовётся не чаще раза в RANGED_LOS_EVERY
    тиков и только когда откат готов, поэтому 200 лучей в тик тут неоткуда
    взяться.
    """
    dx = x1 - x0
    dy = y1 - y0
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return True
    n = int(d / step) + 1
    sx = dx / n
    sy = dy / n
    solid = grid.solid
    for k in range(1, n):
        if solid(int(x0 + sx * k), int(y0 + sy * k)):
            return False
    return True


# --- тик -------------------------------------------------------------------

_ALL_LIT = {}


def _all_lit(n):
    b = _ALL_LIT.get(n)
    if b is None:
        b = _ALL_LIT[n] = bytes((VIS_LIT,)) * n
    return b


def step(w, dt):
    """Зовётся из world.step ДО движения. Состав мира не меняет.

    Ни одного spawn/remove: ИИ только выставляет vx/vy/facing и боевые
    таймеры. Рождение снаряда идёт через e.shot_q, который combat.resolve
    разбирает ПОСЛЕ прохода по словарю, — то же место, что у игрока.
    """
    ents = w.entities
    tick = w.tick
    enemies = None
    targets = None
    # один проход по сущностям на всё: и цели, и исполнители
    for e in ents.values():
        k = e.kind
        if k == K_ENEMY:
            if e.ai and not (e.flags & F_DEAD):
                if enemies is None:
                    enemies = [e]
                else:
                    enemies.append(e)
        elif k == K_PLAYER:
            if not (e.flags & (F_DEAD | F_OFFLINE)):
                if targets is None:
                    targets = [e]
                else:
                    targets.append(e)

    field = w.nav
    if targets:
        # 8.3: ОДНА волна, все живые игроки в источниках
        field.maybe_rebuild(tick, w.grid, [(int(p.x), int(p.y)) for p in targets])
    else:
        field.maybe_rebuild(tick, w.grid, ())

    if enemies is None:
        del w.noises[:]
        return 0

    if w.noises:
        _hear(w, tick, enemies)

    fog = w.fog
    fstate = getattr(fog, "state", None)
    if fstate is None:
        # Заглушка «тумана нет» (так стенды сравнивают сервер с досветовым).
        # Раз скрываться нечем — видно всех; ветка вынесена ИЗ цикла, в
        # рабочем пути остаётся один getattr на тик.
        fstate = _all_lit(w.grid.w * w.grid.h)
    gw = w.grid.w
    grid = w.grid
    step_dir = field.step_dir
    awake = 0
    for e in enemies:
        ex = e.x
        ey = e.y
        i = int(ey) * gw + int(ex)
        # 4.4: ОДИН БАЙТ вместо луча. Клетка освещена -> кто-то из группы
        # видит врага -> враг видит группу.
        if fstate[i] == VIS_LIT:
            e.ai_alert = tick + ALERT_TICKS
        elif tick > e.ai_alert:
            if e.vx or e.vy:
                e.vx = 0.0
                e.vy = 0.0
            continue                       # спит: дешевле некуда
        awake += 1

        # ближайший игрок (для удара и для прицела). Волна уже привела нас
        # к ближайшему, здесь нужна дистанция, а не выбор маршрута.
        best = None
        bd2 = 1e18
        if targets is not None:
            for p in targets:
                dx = p.x - ex
                dy = p.y - ey
                d2 = dx * dx + dy * dy
                if d2 < bd2:
                    bd2 = d2
                    best = p
        if best is None:
            e.vx = e.vy = 0.0
            continue
        d = math.sqrt(bd2)

        if e.ai == AI_RANGED:
            _ranged(w, e, best, d, tick, grid, step_dir)
        else:
            _melee(w, e, best, d, tick, step_dir)
    return awake


def _face(e, tx, ty):
    e.facing = math.atan2(ty - e.y, tx - e.x)


def _roll(e, gx, gy, speed):
    """Катиться по градиенту. Подталкивание на углах сделает world.step."""
    if gx or gy:
        e.vx = gx * speed
        e.vy = gy * speed
        e.facing = math.atan2(gy, gx)
    else:
        e.vx = 0.0
        e.vy = 0.0


def _melee(w, e, target, d, tick, step_dir):
    if e.atk_hit:
        # замах идёт: направление заморожено (клиент рисует его по F_WINDUP),
        # тело ползёт вперёд на MELEE_CREEP — видно, что удар состоится, и
        # видно, куда уходить.
        sp = e.speed * MELEE_CREEP
        gx, gy, _ = step_dir(e.x, e.y)
        if d <= MELEE_STOP or not (gx or gy):
            e.vx = e.vy = 0.0
        else:
            e.vx = gx * sp
            e.vy = gy * sp
        return
    if d <= MELEE_START and tick >= e.atk_ready:
        e.vx = e.vy = 0.0
        _face(e, target.x, target.y)
        combat.start_melee(w, e, windup=MELEE_WIND, cd=MELEE_CD)
        return
    if d <= MELEE_STOP:
        e.vx = e.vy = 0.0
        _face(e, target.x, target.y)
        return
    gx, gy, _ = step_dir(e.x, e.y)
    _roll(e, gx, gy, e.speed)


def _ranged(w, e, target, d, tick, grid, step_dir):
    if e.ai_wind:
        # замах выстрела: стоим, ствол заморожен — ровно то же обещание
        # игроку, что и у ближнего замаха
        e.vx = e.vy = 0.0
        if tick >= e.ai_wind:
            e.ai_wind = 0
            e.flags &= ~F_WINDUP
            e.shot_q = 1                   # родится в combat.resolve
            e.shot_ready = tick + RANGED_CD
        return
    gx, gy, _ = step_dir(e.x, e.y)
    if d < RANGED_NEAR:
        # пятится ПРОТИВ градиента, а не по прямой от игрока: по прямой
        # стрелок вжимается в стену и стоит, против градиента — уходит тем
        # же коридором, которым пришёл
        _roll(e, -gx, -gy, e.speed)
        _face(e, target.x, target.y)
        return
    if d > RANGED_FAR:
        _roll(e, gx, gy, e.speed)
        return
    e.vx = e.vy = 0.0
    _face(e, target.x, target.y)
    if tick < e.shot_ready:
        return
    if (tick + e.id) % RANGED_LOS_EVERY:
        return                              # разнос проверки ствола по тикам
    if not line_clear(grid, e.x, e.y, target.x, target.y):
        return
    e.ai_wind = tick + RANGED_WIND
    e.flags |= F_WINDUP
