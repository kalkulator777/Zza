# -*- coding: utf-8 -*-
"""Бой: замах, удар по дуге, рывок с неуязвимостью, снаряды, урон, смерть.

Контракт: 4.2 (скорости и времена), 4.2a (снаряду подталкивание выключено),
4.3 (поля hp/hp_max/flags), 5.2 (сообщения ev), 8.5 (смерть = дух).

Два правила, которые здесь держатся жёстко:

* **урон применяется к ЛЮБОЙ сущности, а не к игроку.** Точка входа одна —
  damage(). Проверено этапом 2b: врагам не понадобилось НИ ОДНОЙ строки в
  этом файле про получение урона — хватило сущности с hp и team. Замах
  врага тоже зовёт start_melee со своим windup, как здесь и было обещано;
* **всё, что клиент обязан РИСОВАТЬ, живёт в flags, а не в ev.** 5.2 прямо
  говорит: событие может потеряться, на нём нельзя держать состояние. Замах
  видно 8 тиков подряд, и если бы признак замаха ехал одним событием, при
  потере пакета игрок получил бы удар из ниоткуда. Поэтому F_WINDUP и F_DASH —
  биты в flags (см. world.py), а ev шлёт ровно то, что нельзя восстановить из
  снапшота: факт попадания, факт смерти, факт выстрела.

ШУМ (8.2) РОЖДАЕТСЯ ЗДЕСЬ, А СЧИТАЕТСЯ НЕ ЗДЕСЬ. Три вызова w.make_noise —
в start_dash, start_melee и _make_shot — только КЛАДУТ источник в очередь
мира с громкостью из world.NOISE_*. Волну по ним считает ai.step в начале
следующего тика, одну на все источники одной громкости (8.3).

КВАНТОВАНИЕ 30 Гц. Времена 4.2 заданы в секундах, а тик один — 1/30 с.
0.18 с = 5.4 тика, 0.25 с = 7.5 тика: ни то, ни другое не представимо. Все
времена округляются к ближайшему целому тику в одном месте (ticks_of), ошибка
по построению не больше половины тика = 16.7 мс. Числа расхождения — в отчёте
этапа 2a, подгонять их молча нельзя.
"""

import math

from . import items
from . import physics
from . import proto
from . import world as world_mod

TICK_HZ = world_mod.TICK_HZ
DT = world_mod.DT


def ticks_of(sec):
    """Секунды -> целые тики, к ближайшему. Ошибка <= 0.5 тика = 16.7 мс."""
    return max(1, int(round(sec * TICK_HZ)))


# --- рывок (4.2: 21 кл/с, 0.18 с, откат 0.9 с) -----------------------------
# 21 = 14 * SPEED_K: рывок поднят тем же множителем, что и бег (world.py).
# Иначе он перестаёт быть рывком: при беге 7.5 старые 14 кл/с — это всего
# 1.87 скорости бега против прежних 2.8, то есть уход из-под замаха
# превращается в чуть более быструю пробежку.
DASH_SPEED = world_mod.SPEED_RUN * 2.8    # 21.0 кл/с
DASH_TIME_S = 0.18
DASH_CD_S = 0.9
DASH_TICKS = ticks_of(DASH_TIME_S)        # 5 тиков = 0.167 с
DASH_CD_TICKS = ticks_of(DASH_CD_S)       # 27 тиков = 0.900 с
# Путь рывка = DASH_SPEED * DASH_TICKS * DT = 3.50 клетки (было 2.333).
# Отношение к бегу то же самое: 2.8 шага бега за то же время.
#
# ПРО ФИЗИКУ. Шаг рывка за тик = 21/30 = 0.70 клетки — ВДВОЕ больше радиуса
# тела 0.35, то есть за один тик тело перепрыгнуло бы стену в клетку
# толщиной. Не перепрыгивает: move_circle режет шаг на подшаги не длиннее
# радиуса (int(0.70/0.35)+1 = 3 подшага), и это проверено не рассуждением, а
# tests/physics_gap.py — сквозь глухую стену 0 из 80 при этой самой
# скорости.

# --- неуязвимость в рывке (4.2: "уход из-под замаха") ----------------------
# Ровно длительность рывка, ни тиком больше. Отдельное поле, а не проверка
# dash_end, потому что 8.4 разрешает апгрейдам менять поведение рывка, и тогда
# эти два времени разойдутся.
DASH_INV_TICKS = DASH_TICKS

# --- ближняя атака (4.2: замах 0.25 с) -------------------------------------
MELEE_WINDUP_S = 0.25
MELEE_WINDUP_TICKS = ticks_of(MELEE_WINDUP_S)     # 8 тиков = 0.267 с
MELEE_CD_S = 0.45                 # замах + возврат: чуть больше двух ударов в с
MELEE_CD_TICKS = ticks_of(MELEE_CD_S)             # 14 тиков
MELEE_REACH = 1.2                 # от центра тела; до цели считается +её радиус
MELEE_ARC_DEG = 110.0             # дуга перед собой, полная ширина
MELEE_COS = math.cos(math.radians(MELEE_ARC_DEG * 0.5))
MELEE_DMG = 20                    # 5 ударов по игроку в 100 hp

# --- снаряд (4.2: 12 кл/с) -------------------------------------------------
SHOT_SPEED = world_mod.SPEED_SHOT     # 12.0
SHOT_R = 0.12                     # заметно меньше тела: пролезает где тело нет
SHOT_RANGE = 14.0                 # клеток; дальше растворяется
SHOT_TICKS = int(math.ceil(SHOT_RANGE / SHOT_SPEED * TICK_HZ))
SHOT_DMG = 12
SHOT_CD_S = 0.5
SHOT_CD_TICKS = ticks_of(SHOT_CD_S)
SHOT_MUZZLE = 0.02                # зазор, чтобы снаряд не родился внутри тела

# Шаг снаряда за тик 18/30 = 0.6 клетки > SHOT_R = 0.12, поэтому move_circle
# режет его на подшаги сам (см. physics.move_circle) и сквозь стену снаряд не
# проскакивает: подшагов int(0.6/0.12)+1 = 6. Проверено tests/combat_check.py.
# Живёт снаряд по-прежнему ровно свои 14 клеток: SHOT_TICKS считается от
# дальности и скорости, поэтому на 18 кл/с их 24 вместо 35 — дальность
# осталась дальностью, а не превратилась в 21 клетку.


# --- кто кому враг ---------------------------------------------------------

def hostile(a, b):
    """Бьют друг друга только разные команды. Снаряд целью не бывает."""
    if b.kind == world_mod.K_SHOT:
        return False
    ta = a.team
    tb = b.team
    return bool(ta) and bool(tb) and ta != tb


# --- урон и смерть (4.3, 8.5) ----------------------------------------------

def invulnerable(w, e):
    """Сейчас ли сущность неуязвима. Неуязвимость даёт рывок (4.2).

    Проверка на inv_end != 0 обязательна: у сущности, которая ни разу не
    рвалась, inv_end равен нулю, и на нулевом тике мира (мир только создан,
    ещё не шагал) сравнение tick <= inv_end дало бы неуязвимость ВСЕМ.
    """
    return e.inv_end and w.tick <= e.inv_end


def damage(w, target, amount, src=None, src_id=0):
    """Единая точка урона. Возвращает сколько сняли (0 — не прошло).

    Принимает ЛЮБУЮ сущность с hp: игрока, врага этапа 2b, разрушаемый
    ящик. Ни одной проверки вида "если это игрок" здесь нет и быть не должно.
    """
    if amount <= 0:
        return 0
    if target.hp <= 0 or (target.flags & world_mod.F_DEAD):
        return 0                       # мёртвый урона не получает (8.5)
    if target.inv_end and w.tick <= target.inv_end:
        return 0                       # рывок (4.2), см. invulnerable()
    hp = target.hp - amount
    if hp < 0:
        hp = 0
    target.hp = hp
    sid = src.id if src is not None else src_id
    w.event("hit", a=sid, b=target.id, dmg=int(amount),
            x=proto.r3(target.x), y=proto.r3(target.y))
    if hp == 0:
        kill(w, target, src, sid)
        if src is not None:
            # Жатва (8.4): лечит ДОБИВАНИЕ, а не попадание. Здесь, а не в
            # _land_melee, ровно по тому же доводу, по которому здесь живёт
            # сам урон: точка входа одна, и убийство снарядом, тараном или
            # ударом лечит одинаково — без единой ветки "если это игрок".
            # Ноль стаков даёт ноль, то есть у всех остальных это сравнение
            # с нулём и ничего больше.
            got = items.kill_heal(src)
            if got:
                items.heal(src, got)
    return amount


def kill(w, e, src=None, src_id=0):
    """Смерть: бит DEAD (4.3). Игрок остаётся духом, остальные убираются."""
    e.hp = 0
    e.flags |= world_mod.F_DEAD
    e.flags &= ~(world_mod.F_WINDUP | world_mod.F_DASH)
    e.vx = e.vy = 0.0
    e.atk_hit = 0
    e.dash_end = 0
    e.inv_end = 0
    e.shot_q = 0
    w.event("die", a=(src.id if src is not None else src_id), b=e.id,
            x=proto.r3(e.x), y=proto.r3(e.y))
    if e.kind != world_mod.K_PLAYER:
        return False                   # труп убирает вызывающий: см. resolve
    return True


def reset(e):
    """Снять все боевые таймеры. Смена этажа, воскрешение, переподключение."""
    e.atk_hit = 0
    e.atk_ready = 0
    e.dash_end = 0
    e.dash_ready = 0
    e.inv_end = 0
    e.shot_ready = 0
    e.shot_q = 0
    e.dash_hit = None       # рывка нет — и задевать этим рывком уже некого
    e.flags &= ~(world_mod.F_WINDUP | world_mod.F_DASH)


# --- начало тика: кнопки и таймеры -----------------------------------------

def begin(w, e):
    """Вызывается из world.step ДО движения, для сущностей с боевым делом.

    Рывок выставляет скорость сам: world.step видит dash_end и не затирает её
    ходом с клавиш.
    """
    tick = w.tick
    if e.dash_end:
        if tick <= e.dash_end:
            e.vx = e.dash_dx * DASH_SPEED
            e.vy = e.dash_dy * DASH_SPEED
        else:
            e.dash_end = 0
            e.flags &= ~world_mod.F_DASH
    if not e.ctl:
        return                          # ИИ врагов — этап 2b, кнопок у него нет
    if e.flags & (world_mod.F_DEAD | world_mod.F_OFFLINE):
        # дух урона не наносит, камень не машет мечом (8.5). Без OFFLINE
        # оборвавшийся на зажатой атаке игрок молотил бы воздух 120 с.
        return
    btn = e.btn
    if not btn:
        return
    if (btn & proto.BTN_DASH) and not e.dash_end and tick >= e.dash_ready:
        start_dash(w, e)
    if (btn & proto.BTN_ATTACK) and not e.atk_hit and tick >= e.atk_ready:
        start_melee(w, e)
    # Отдельного бита под дальний бой в 5.1 нет: 1 атака, 2 рывок, 4 действие,
    # 8 предмет. Снаряд повешен на 8 — см. правку контракта в отчёте 2a.
    if btn & proto.BTN_ITEM:
        # 8.4: подбор предмета. Сам подбор — в resolve: begin зовётся ИЗ
        # цикла по словарю сущностей, а подбор его меняет.
        items.request_pickup(w, e)
    if (btn & proto.BTN_SHOOT) and tick >= e.shot_ready:
        # Сам снаряд рождается в resolve: spawn идёт из world.step, то есть
        # ИЗНУТРИ цикла по словарю сущностей, а это RuntimeError. Откат
        # ставится здесь, а не при рождении: ствол, упёртый в стену, всё
        # равно щёлкнул.
        e.shot_q = 1
        e.shot_ready = tick + SHOT_CD_TICKS


def start_dash(w, e):
    dx, dy = e.mv
    d = math.hypot(dx, dy)
    if d < 1e-9:
        dx = math.cos(e.facing)         # стоим на месте — рывок вперёд
        dy = math.sin(e.facing)
    else:
        dx /= d
        dy /= d
    e.dash_dx = dx
    e.dash_dy = dy
    e.dash_end = w.tick + DASH_TICKS - 1        # включая текущий тик
    # 8.4 «Ветер» делит откат; без апгрейда items.dash_cd вернёт ровно
    # DASH_CD_TICKS, то есть число 4.2 остаётся числом 4.2.
    cd = items.dash_cd(e, DASH_CD_TICKS)
    # Неуязвимость не имеет права занять больше половины отката: иначе
    # укороченный откат превращает уход в постоянный щит (вывод и замер — в
    # items.DASH_INV_SHARE). Без Ветра это ровно DASH_INV_TICKS.
    e.inv_end = w.tick + items.dash_inv(DASH_INV_TICKS, cd) - 1
    e.dash_ready = w.tick + cd
    e.dash_hit = None       # новый рывок — новый список задетых (Таран)
    e.flags |= world_mod.F_DASH
    w.make_noise(e.x, e.y, world_mod.NOISE_DASH, e.team)     # 8.2
    e.vx = dx * DASH_SPEED
    e.vy = dy * DASH_SPEED


def start_melee(w, e, windup=MELEE_WINDUP_TICKS, cd=MELEE_CD_TICKS):
    """Замах. Удар придёт через windup тиков — это и есть его смысл.

    windup и cd — параметры, а не константы внутри, ровно ради этапа 2b: у
    врага по 4.2 замах 0.4-0.6 с, и ему нужно будет вызвать эту же функцию с
    своим числом, а не заводить вторую ближнюю атаку.
    """
    e.atk_hit = w.tick + windup
    e.atk_ready = w.tick + cd
    e.flags |= world_mod.F_WINDUP
    w.make_noise(e.x, e.y, world_mod.NOISE_MELEE, e.team)    # 8.2
    # направление на время замаха заморожено (world.step не трогает facing,
    # пока atk_hit != 0): клиент рисует дугу ровно там, где она ударит.


# --- конец тика: удары, снаряды, трупы -------------------------------------

def resolve(w, dt=DT):
    """Вызывается из world.step ПОСЛЕ движения.

    Один проход по сущностям: кто ударил, кто летит, кому родиться. Всё, что
    меняет состав мира (spawn/remove), делается ПОСЛЕ прохода — иначе
    RuntimeError на изменении словаря во время итерации.
    """
    tick = w.tick
    # 8.4: подбор предмета меняет состав мира (предмет исчезает вместе с
    # собратьями по алтарю), поэтому он идёт ДО прохода, а не внутри него.
    if w.pickups:
        items.resolve_pickups(w)
    swings = None
    shots = None
    born = None
    rams = None
    for e in w.entities.values():
        if e.kind == world_mod.K_SHOT:
            if shots is None:
                shots = []
            shots.append(e)
            continue
        if e.atk_hit and e.atk_hit <= tick:
            if swings is None:
                swings = []
            swings.append(e)
        if e.shot_q:
            q = e.shot_q
            e.shot_q = 0
            if born is None:
                born = []
            born.append((e, q))
        if e.dash_end and e.ups:
            # Таран (8.4). Условие стоит двумя сравнениями с нулём: у того,
            # кто не в рывке или ничего не собрал, дальше ничего не считается.
            if rams is None:
                rams = []
            rams.append(e)
    gone = None
    if swings:
        for e in swings:
            _land_melee(w, e)
    if rams:
        # ПОСЛЕ движения: тело уже там, куда его привёл этот тик рывка, —
        # значит задеты те, сквозь кого он прошёл, а не те, к кому он летел.
        for e in rams:
            items.dash_strike(w, e, damage)
    if shots:
        for s in shots:
            t = _shot_target(w, s)
            if t is not None:
                # источник урона — стрелявший, а не снаряд: он мог уже умереть,
                # тогда в ev уходит его id, и клиент всё равно знает, чей был
                damage(w, t, s.dmg, w.entities.get(s.owner), s.owner)
                if gone is None:
                    gone = []
                gone.append(s.id)
    # трупы не-игроков (враги этапа 2b): DEAD выставлен, сущность убирается
    for e in w.entities.values():
        if (e.flags & world_mod.F_DEAD) and e.kind != world_mod.K_PLAYER:
            if gone is None:
                gone = []
            gone.append(e.id)
    if gone:
        for i in gone:
            w.remove(i)
    if born:
        # shot_q — либо 1 («выстрел как у всех»), либо СПИСОК (угол, урон):
        # веер босса (8.1). Список, а не своя очередь и не спавн из ИИ:
        # состав мира меняется только здесь, после прохода по словарю, и это
        # то же правило, по которому здесь рождается снаряд игрока.
        for e, q in born:
            if q == 1:
                make_shot(w, e)
            else:
                for da, dmg in q:
                    make_shot(w, e, facing=e.facing + da, dmg=dmg)


def _land_melee(w, e):
    """Удар по дуге перед собой. Направление — то, что замерло на замахе.

    Дальность, дуга и урон берутся У СУЩНОСТИ, а числа 4.2 остаются
    значением по умолчанию: ноль в поле — значит «как у всех». Это ровно та
    правка, которую 4.2 сам называет недостающей («другой баланс — это
    правка сюда и поле в сущности»), и она одна на всех: боссу (8.1) нужен
    удар по КРУГУ (atk_cos = -1.0: косинус половины дуги 360 градусов), а не
    вторая ближняя атака рядом с этой. У игрока, рубаки и стрелка поля
    нулевые, и ни одно число здесь не меняется.
    """
    e.atk_hit = 0
    e.flags &= ~world_mod.F_WINDUP
    if e.flags & world_mod.F_DEAD:
        return 0
    reach = e.atk_r or MELEE_REACH
    cos_half = e.atk_cos or MELEE_COS
    dmg = e.atk_dmg or MELEE_DMG
    fx = math.cos(e.facing)
    fy = math.sin(e.facing)
    n = 0
    for t in w.entities.values():
        if t is e or not hostile(e, t):
            continue
        if t.flags & world_mod.F_DEAD:
            continue
        dx = t.x - e.x
        dy = t.y - e.y
        rr = reach + t.r
        d2 = dx * dx + dy * dy
        if d2 > rr * rr:
            continue
        if d2 > 1e-12 and cos_half > -1.0:
            d = math.sqrt(d2)
            if (dx * fx + dy * fy) / d < cos_half:
                continue                 # за спиной или сбоку — мимо дуги
        if damage(w, t, dmg, e):
            n += 1
    return n


def _shot_target(w, s):
    for t in w.entities.values():
        if not hostile(s, t) or (t.flags & world_mod.F_DEAD):
            continue
        dx = t.x - s.x
        dy = t.y - s.y
        rr = s.r + t.r
        if dx * dx + dy * dy <= rr * rr:
            return t
    return None


def make_shot(w, e, facing=None, dmg=None):
    """Снаряд рождается у края тела и летит по прямой (4.2a).

    facing/dmg — параметры, а не константы внутри: веер босса (8.1) — это
    три вызова с разными углами, а не третий вид снаряда.
    """
    if facing is None:
        facing = e.facing
    if dmg is None:
        dmg = SHOT_DMG
    ca = math.cos(facing)
    sa = math.sin(facing)
    off = e.r + SHOT_R + SHOT_MUZZLE
    x = e.x + ca * off
    y = e.y + sa * off
    if physics.circle_hits(w.grid, x, y, SHOT_R):
        return None                      # ствол упёрт в стену — выстрела нет
    s = w.spawn(world_mod.K_SHOT, x, y, r=SHOT_R, team=e.team, owner=e.id,
                dmg=dmg, ttl=SHOT_TICKS, facing=facing, hp=1, hp_max=1,
                # Рикошет (8.4): сколько раз этот снаряд переживёт стену.
                # Считается при рождении, а не в полёте: апгрейд стрелявшего
                # снаряду уже не догнать, если тот умрёт на лету.
                bounce=items.shot_bounces(e))
    s.vx = ca * SHOT_SPEED
    s.vy = sa * SHOT_SPEED
    w.event("shot", a=e.id, b=s.id, x=proto.r3(x), y=proto.r3(y))
    w.make_noise(x, y, world_mod.NOISE_SHOT, e.team)         # 8.2
    return s


_make_shot = make_shot          # прежнее имя: звалось только отсюда
