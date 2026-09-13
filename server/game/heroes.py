# -*- coding: utf-8 -*-
"""Герои: характеристики, способности, пассивки.

Каждая способность — функция fn(world, fighter, action), вызываемая каждый тик,
пока действие живо. action.t — номер тика от начала (0 = тик применения).
Функция может завершить действие досрочно, выставив fighter.act = None.
"""

import math

from . import const as C


class Ability:
    __slots__ = ("key", "name", "desc", "cd", "dur", "fn", "move", "face_lock", "armor")

    def __init__(self, key, name, desc, cd, dur, fn, move=0.0, face_lock=True, armor=0.0):
        self.key = key
        self.name = name
        self.desc = desc
        self.cd = cd
        self.dur = dur
        self.fn = fn
        self.move = move
        self.face_lock = face_lock
        self.armor = armor

    def info(self):
        return {"key": self.key, "name": self.name, "desc": self.desc, "cd": self.cd}


class Hero:
    __slots__ = ("id", "name", "role", "color", "color2", "tagline", "hp", "weight",
                 "speed", "jumps", "grav", "ab", "passive", "passive_desc",
                 "on_tick", "on_deal", "on_take", "on_spawn", "difficulty")

    def __init__(self, id, name, role, color, color2, tagline, hp, weight, speed,
                 jumps, abilities, passive, passive_desc, grav=1.0, difficulty=2,
                 on_tick=None, on_deal=None, on_take=None, on_spawn=None):
        self.id = id
        self.name = name
        self.role = role
        self.color = color
        self.color2 = color2
        self.tagline = tagline
        self.hp = hp
        self.weight = weight
        self.speed = speed
        self.jumps = jumps
        self.grav = grav
        self.difficulty = difficulty
        self.ab = {a.key: a for a in abilities}
        self.passive = passive
        self.passive_desc = passive_desc
        self.on_tick = on_tick
        self.on_deal = on_deal
        self.on_take = on_take
        self.on_spawn = on_spawn

    def info(self):
        return {
            "id": self.id, "name": self.name, "role": self.role,
            "color": self.color, "color2": self.color2, "tagline": self.tagline,
            "hp": self.hp, "weight": self.weight, "speed": self.speed,
            "jumps": self.jumps, "difficulty": self.difficulty,
            "passive": {"name": self.passive, "desc": self.passive_desc},
            "abilities": [self.ab[k].info() for k in ("basic", "q", "e", "r") if k in self.ab],
        }


# ------------------------------------------------------------------ хелперы
def aim(f):
    ax, ay = f.aim_x, f.aim_y
    d = math.hypot(ax, ay)
    if d < 0.05:
        return float(f.face), 0.0
    return ax / d, ay / d


def aim_point(f, max_dist):
    ax, ay = aim(f)
    return f.cx + ax * max_dist, f.cy + ay * max_dist


def clamp_point(f, max_dist):
    """Точка прицела на расстоянии не больше max_dist (мышь шлёт вектор, длина 0..1)."""
    ax, ay = f.aim_x, f.aim_y
    d = math.hypot(ax, ay)
    if d < 0.05:
        return f.cx + f.face * max_dist * 0.6, f.cy
    k = min(1.0, d) * max_dist
    return f.cx + ax / d * k, f.cy + ay / d * k


# ================================================================== РЕЗАК
def rezak_basic(w, f, a):
    step = f.combo % 3
    if a.t == 0:
        f.combo = (f.combo + 1) % 3
        f.vx += 100 * f.face
    if a.t == 3:
        if step == 2:
            w.spawn_hitbox(f, 56, 0, 94, 70, 12, 570, 34, ttl=3, tag="blade")
            w.fx("slash", f.cx + 56 * f.face, f.cy, d=f.face, s=1.4)
        else:
            w.spawn_hitbox(f, 50, -4 + step * 10, 80, 60, 5, 190, 14 + step * 6, ttl=3, tag="blade")
            w.fx("slash", f.cx + 50 * f.face, f.cy, d=f.face, s=1.0)


def _rezak_q_hit(w, src, t):
    """Попал рывком — вернулся прыжок и часть отката: можно сразу заходить снова."""
    src.jumps = max(src.jumps, 1)
    src.cds["q"] = min(src.cds["q"], 4.0)


def rezak_q(w, f, a):
    if a.t == 0:
        ax, ay = aim(f)
        ay = max(-0.78, min(0.55, ay))          # вверх — почти свободно, вниз — чуть-чуть
        n = math.hypot(ax, ay) or 1.0
        ax, ay = ax / n, ay / n
        f.face = 1 if ax >= 0 else -1
        f.vx = 1010 * ax
        f.vy = 1010 * ay
        f.iframes = max(f.iframes, 0.26)
        f.jumps = max(f.jumps, 1)
        w.spawn_hitbox(f, 0, 0, 96, 74, 8, 290, 40, ttl=17, follow=True,
                       on_hit=_rezak_q_hit, tag="blade")
        w.fx("dashslash", f.cx, f.cy, d=f.face)
    if a.t < 13:
        f.mem["nograv"] = True
    else:
        f.vx *= 0.86


def rezak_e(w, f, a):
    if a.t in (2, 12, 22):
        last = a.t == 22
        w.spawn_hitbox_at(f, f.cx, f.cy, 250, 158,
                          5 if not last else 8,
                          230 if not last else 430,
                          0, ttl=3, radial=True, tag="blade")
        w.fx("whirl", f.cx, f.cy, s=1.0 if not last else 1.5)
    if not f.on_ground:
        f.vy = min(f.vy, 110.0)


def rezak_r(w, f, a):
    if a.t == 0:
        f.iframes = max(f.iframes, 1.35)
        f.mem["hunt"] = 0
        w.fx("ultflash", f.cx, f.cy, c=f.hero.color)
    f.mem["nograv"] = True
    f.vx *= 0.6
    f.vy *= 0.6
    if a.t in (4, 20, 36, 52):
        tgt = w.nearest_enemy(f)
        if tgt is None:
            return
        side = -1 if tgt.cx > f.cx else 1
        f.x = tgt.cx + side * 72 - C.FW / 2
        f.y = tgt.y
        f.face = 1 if side < 0 else -1
        f.vx = f.vy = 0.0
        last = a.t == 52
        w.spawn_hitbox(f, 46, 0, 86, 72, 13 if not last else 16,
                       220 if not last else 880, 30 if not last else 46,
                       ttl=3, tag="blade")
        w.fx("slash", tgt.cx, tgt.cy, d=f.face, s=1.6)


def rezak_deal(w, f, t, dmg, tag):
    """Каждое попадание — щит и рывок скорости. Резак живёт, пока давит."""
    if tag not in ("burn", "fire", "blizzard"):
        f.shield = min(12.0, f.shield + 1.5)
        f.add_effect("haste", 1.5, 0.10)
    if t.hp / t.max_hp < 0.4:
        return dmg * 1.12
    return dmg


REZAK = Hero(
    "rezak", "Резак", "Ассасин", "#ff4d6d", "#ffa8b8",
    "Быстрый, живёт на попаданиях: не бьёт — умирает.",
    hp=108, weight=0.98, speed=1.12, jumps=2, difficulty=2,
    passive="Кровь на лезвии",
    passive_desc="Попадание: щит +1.5 (до 12) и +10% скорости на 1.5 с. +12% урона по целям ниже 40% HP.",
    on_deal=rezak_deal,
    abilities=[
        Ability("basic", "Серия", "Комбо из трёх ударов, третий выбивает за край.", 0.27, 0.27, rezak_basic, move=0.55),
        Ability("q", "Рывок-разрез", "Рывок по прицелу с неуязвимостью. Попал — вернулся прыжок и часть отката.",
                6.5, 0.34, rezak_q, face_lock=False),
        Ability("e", "Вихрь", "Три волны вокруг себя, последняя подбрасывает.", 9.5, 0.42, rezak_e,
                move=0.55, armor=0.15),
        Ability("r", "Охота", "4 телепорт-удара по ближайшему врагу. Неуязвимость.", 0.6, 1.02, rezak_r),
    ],
)


# ================================================================== БУНКЕР
def bunker_basic(w, f, a):
    if a.t == 6:
        w.spawn_hitbox(f, 52, 0, 92, 80, 22, 350, 22, ttl=4, tag="bash")
        w.fx("bash", f.cx + 52 * f.face, f.cy, d=f.face)
    if a.t < 6:
        f.vx *= 0.9


def bunker_q(w, f, a):
    f.add_effect("dr", 0.12, 0.65)
    f.mem["guard"] = 1
    if a.t == 0:
        w.fx("guard", f.cx, f.cy, d=f.face)
        f.mem["guard_left"] = 4
    if a.t % 10 == 0:
        w.fx("guardtick", f.cx + 30 * f.face, f.cy, d=f.face)
    # сбиваем вражеские снаряды перед собой, но не больше трёх за барьер
    for p in list(w.projectiles):
        if f.mem.get("guard_left", 0) <= 0:
            break
        if p.team == f.team:
            continue
        if abs(p.y - f.cy) < 56 and 0 < (p.x - f.cx) * f.face < 72:
            w.projectiles.remove(p)
            f.mem["guard_left"] -= 1
            w.fx("shieldhit", p.x, p.y)


def bunker_e(w, f, a):
    if a.t < 11:
        f.vx *= 0.82
        if a.t == 0:
            w.fx("windup", f.cx, f.cy, d=f.face)
        return
    if a.t == 11:
        f.vx = 1080 * f.face
        w.spawn_hitbox(f, 12, 0, 102, 82, 32, 720, 36, ttl=26, follow=True,
                       on_hit=lambda ww, src, t: t.add_effect("stun", 0.5),
                       tag="ram")
        w.fx("ram", f.cx, f.cy, d=f.face)
    f.mem["nograv"] = a.t < 30
    if a.t > 30:
        f.vx *= 0.9


def bunker_r(w, f, a):
    if a.t == 0:
        x, y = f.cx, f.cy
        w.fx("ultflash", x, y, c=f.hero.color)

        def tick(ww, z):
            for o in ww.fighters.values():
                if not o.alive or not ww.in_zone(z, o):
                    continue
                if o.team == z.team:
                    o.add_effect("dr", 0.12, 0.5)
                else:
                    o.add_effect("slow", 0.12, 0.3)
        w.spawn_zone(f, "dome", x, y, 235, 6.5, on_tick=tick)


def bunker_tick(w, f):
    if f.no_dmg_t > 3.5 and f.hp < f.max_hp:
        w.heal(f, 12.0 * C.DT)


BUNKER = Hero(
    "bunker", "Бункер", "Танк", "#f4a259", "#ffd7a8",
    "Медленный кирпич, который держит линию.",
    hp=194, weight=1.5, speed=0.96, jumps=2, grav=1.05, difficulty=1,
    passive="Ремонт", passive_desc="Через 3.5 с без урона восстанавливает 12 HP/с.",
    on_tick=bunker_tick,
    abilities=[
        Ability("basic", "Удар щитом", "Тяжёлый удар с отбросом.", 0.40, 0.4, bunker_basic, move=0.6),
        Ability("q", "Барьер", "2.4 с: −65% урона, гасит до 4 снарядов спереди, можно идти.", 8.5, 2.4,
                bunker_q, move=0.7, face_lock=False, armor=0.6),
        Ability("e", "Таран", "Разбег и рывок: 32 урона, оглушение 0.5 с.", 10.0, 0.75, bunker_e, armor=0.5),
        Ability("r", "Купол", "Зона на 6.5 с: союзникам −50% урона, врагам −30% скорости.", 0.6, 0.5, bunker_r, move=0.3),
    ],
)


# ==================================================================== ИГЛА
def igla_basic(w, f, a):
    if a.t == 2:
        ax, ay = aim(f)
        gap = w.time - f.mem.get("shot_t", -9.0)
        f.mem["shot_t"] = w.time
        boost = gap > 2.0
        w.spawn_proj(f, "bolt", ax * 1300, ay * 1300,
                     10 * (1.7 if boost else 1.0), 135 * (1.8 if boost else 1.0),
                     1.4, r=7, ox=30, oy=-4)
        if boost:
            w.fx("crit", f.cx + 30 * f.face, f.cy)


def igla_q(w, f, a):
    hold = f.held(C.IN_Q)
    power = min(1.0, a.t / 70.0)
    f.mem["charge"] = power
    if a.t % 6 == 0:
        w.fx("charge", f.cx, f.cy, m=round(power, 2))
    if (not hold and a.t > 6) or a.t >= a.dur - 1:
        ax, ay = aim(f)
        sp = 1500 + 900 * power
        w.spawn_proj(f, "slug", ax * sp, ay * sp, 28 + 38 * power,
                     260 + 520 * power, 1.6, r=9 + 6 * power, ox=30, oy=-4,
                     pierce=3 if power > 0.65 else 1)
        w.fx("bang", f.cx + 30 * f.face, f.cy, m=round(power, 2))
        f.mem.pop("charge", None)
        f.act = None
        f.vx -= ax * 160
    else:
        f.vx *= 0.9


def igla_e(w, f, a):
    if a.t == 0:
        ax, _ = aim(f)
        d = -1 if ax >= 0 else 1
        f.vx = 560 * d
        f.vy = -560
        f.jumps = max(f.jumps, 1)

        def mine_tick(ww, s):
            if s.t < 0.25:
                return
            for o in ww.fighters.values():
                if not o.alive or o.team == s.team:
                    continue
                if abs(o.cx - s.cx) < 74 and abs(o.cy - s.cy) < 84:
                    s.hp = 0
                    return

        def mine_end(ww, s):
            if s.t < 0.25:
                return
            owner = ww.fighters.get(s.owner)
            if owner is None:
                return
            ww.spawn_hitbox_at(owner, s.cx, s.cy, 215, 195, 37, 700, 0,
                               ttl=3, radial=True, tag="mine")
            ww.fx("boom", s.cx, s.cy, r=95)
        w.spawn_struct(f, "mine", f.cx, f.y + C.FH, 30, 16, 22, 15.0,
                       data={"tick": mine_tick, "end": mine_end})


def igla_r(w, f, a):
    f.vx *= 0.86
    ax, ay = aim(f)
    if a.t == 0:
        w.fx("ultflash", f.cx, f.cy, c=f.hero.color)
    if a.t < 42:
        w.fx("beamaim", f.cx, f.cy, dx=round(ax, 3), dy=round(ay, 3))
        return
    if a.t == 42:
        w.spawn_proj(f, "beam", ax * 5200, ay * 5200, 64, 760, 0.55,
                     r=16, ox=26, oy=-4, pierce=99, terrain=False)
        w.fx("beam", f.cx, f.cy, dx=round(ax, 3), dy=round(ay, 3))
        w.shake = min(20.0, w.shake + 9.0)


IGLA = Hero(
    "igla", "Игла", "Снайпер", "#5bc8ff", "#c7ecff",
    "Бьёт больно и издалека, но рассыпается в ближнем бою.",
    hp=116, weight=0.95, speed=1.05, jumps=2, difficulty=3,
    passive="Первый выстрел", passive_desc="Выстрел после 2 с паузы: ×1.7 урона и отброса.",
    abilities=[
        Ability("basic", "Болт", "Быстрый дальний выстрел.", 0.32, 0.2, igla_basic, move=0.7, face_lock=False),
        Ability("q", "Заряд", "Держи Q: до 66 урона, пробивает насквозь.", 6.0, 1.5,
                igla_q, move=0.25, face_lock=False),
        Ability("e", "Отскок", "Прыжок назад, на месте остаётся мина.", 8.0, 0.4, igla_e, move=0.3),
        Ability("r", "Луч", "0.7 с наводки, затем луч через всю арену: 64 урона.", 0.6, 1.2,
                igla_r, move=0.2, face_lock=False),
    ],
)


# =================================================================== ВЬЮГА
def vyuga_basic(w, f, a):
    if a.t == 2:
        ax, ay = aim(f)
        w.spawn_proj(f, "ice", ax * 950, ay * 950 - 60, 9, 150, 1.6, r=9,
                     ox=28, oy=-4, grav=340,
                     on_hit=lambda ww, p, t: t.add_effect("slow", 2.0, 0.3) if t else None)


def vyuga_q(w, f, a):
    if a.t == 0:
        x, y = clamp_point(f, 230)
        y = min(y, f.cy + 60)
        w.spawn_struct(f, "wall", x, y + 70, 28, 140, 70, 6.0,
                       solid=True, blocks_shots=True)


def vyuga_e(w, f, a):
    if a.t == 8:
        f.effects.pop("burn", None)     # собственный холод сбивает с неё пламя
        w.spawn_hitbox(f, 112, 0, 206, 122, 17, 170, 20, ttl=4,
                       on_hit=lambda ww, src, t: t.add_effect("freeze", 0.95),
                       tag="frost")
        w.fx("cone", f.cx, f.cy, d=f.face)
    if a.t < 8:
        f.vx *= 0.86


def vyuga_r(w, f, a):
    if a.t == 0:
        x, y = clamp_point(f, 340)
        w.fx("ultflash", f.cx, f.cy, c=f.hero.color)

        def tick(ww, z):
            if z.tick < 0.5:
                return
            z.tick = 0.0
            owner = ww.fighters.get(z.owner)
            for o in ww.fighters.values():
                if o.alive and o.team != z.team and ww.in_zone(z, o):
                    ww.damage(owner, o, 10, 70, 90, fx="frosthit", tag="blizzard")
                    o.add_effect("slow", 1.0, 0.45)
            ww.fx("snow", z.x, z.y, r=z.r)
        w.spawn_zone(f, "blizzard", x, y, 265, 5.5, on_tick=tick)


def vyuga_deal(w, f, t, dmg, tag):
    if t.has("slow") or t.has("freeze"):
        return dmg * 1.26
    return dmg


VYUGA = Hero(
    "vyuga", "Вьюга", "Контроль", "#8ad7c8", "#d9fff7",
    "Никого не убивает быстро — просто не даёт двигаться.",
    hp=106, weight=1.0, speed=1.0, jumps=2, difficulty=2,
    passive="Хрупкость", passive_desc="+26% урона по замедленным и замороженным.",
    on_deal=vyuga_deal,
    abilities=[
        Ability("basic", "Ледяная стрела", "9 урона и замедление на 2 с.", 0.44, 0.24,
                vyuga_basic, move=0.7, face_lock=False),
        Ability("q", "Ледяная стена", "Стена на 6 с: держит снаряды, на ней можно стоять.", 8.5, 0.35,
                vyuga_q, move=0.4, face_lock=False),
        Ability("e", "Стужа", "Конус: 17 урона и заморозка на 0.95 с. Гасит горение на себе.", 10.5, 0.55, vyuga_e, move=0.2),
        Ability("r", "Метель", "Зона на 5.5 с: 10 урона каждые 0.5 с и сильное замедление.", 0.6, 0.6,
                vyuga_r, move=0.3, face_lock=False),
    ],
)


# =================================================================== ГАЙКА
def gayka_basic(w, f, a):
    if a.t == 4:
        n = f.mem.get("wrench", 0) + 1
        f.mem["wrench"] = n
        big = n % 4 == 0
        if big:
            w.spawn_hitbox(f, 48, 0, 82, 72, 29, 550, 32, ttl=3,
                           on_hit=lambda ww, src, t: t.add_effect("stun", 0.35),
                           tag="wrench")
            w.fx("sparks", f.cx + 48 * f.face, f.cy, s=1.6)
        else:
            w.spawn_hitbox(f, 46, 0, 78, 70, 15, 280, 26, ttl=3, tag="wrench")
            w.fx("sparks", f.cx + 46 * f.face, f.cy, s=1.0)


def _turret_tick(w, s):
    owner = w.fighters.get(s.owner)
    s.data["cool"] = s.data.get("cool", 0.5) - C.DT
    best, bd = None, 560.0 ** 2
    for o in w.fighters.values():
        if not o.alive or o.team == s.team:
            continue
        d = (o.cx - s.cx) ** 2 + (o.cy - s.cy) ** 2
        if d < bd:
            bd, best = d, o
    if best is None:
        return
    ang = math.degrees(math.atan2(-(best.cy - s.cy), best.cx - s.cx))
    s.data["aim"] = ang
    if s.data["cool"] > 0:
        return
    s.data["cool"] = 0.44
    dx, dy = best.cx - s.cx, best.cy - s.cy - 8
    d = math.hypot(dx, dy) or 1.0
    p = w.spawn_proj(owner or best, "bolt2", dx / d * 1000, dy / d * 1000,
                     9, 115, 1.1, r=6)
    p.x, p.y = s.cx, s.cy - 6
    p.team = s.team


def gayka_q(w, f, a):
    if a.t == 0:
        for s in w.structs:
            if s.owner == f.pid and s.kind == "turret":
                s.ttl = 0.0
        x, y = f.cx + 44 * f.face, f.y + C.FH
        w.spawn_struct(f, "turret", x, y, 36, 42, 64, 14.0,
                       data={"tick": _turret_tick, "aim": 0.0})


def _hook_hit(w, p, t):
    owner = w.fighters.get(p.owner)
    if owner is None:
        return
    if t is not None:
        dx, dy = owner.cx - t.cx, owner.cy - t.cy
        d = math.hypot(dx, dy) or 1.0
        t.vx = dx / d * 1000
        t.vy = dy / d * 1000 - 140
        t.hitstun = max(t.hitstun, 0.28)
        t.act = None
        w.fx("pull", t.cx, t.cy)
    else:
        dx, dy = p.x - owner.cx, p.y - owner.cy
        d = math.hypot(dx, dy) or 1.0
        if d > 60:
            owner.vx = dx / d * 950
            owner.vy = dy / d * 950 - 120
            owner.jumps = max(owner.jumps, 1)
            w.fx("pull", owner.cx, owner.cy)


def gayka_e(w, f, a):
    if a.t == 3:
        ax, ay = aim(f)
        w.spawn_proj(f, "hook", ax * 1300, ay * 1300, 13, 0, 0.45, r=9,
                     ox=26, oy=-4, on_hit=_hook_hit)
    if a.t < 3:
        f.vx *= 0.85


def gayka_r(w, f, a):
    if a.t == 0:
        x, y = clamp_point(f, 430)
        w.fx("ultflash", f.cx, f.cy, c=f.hero.color)

        def end(ww, z):
            owner = ww.fighters.get(z.owner)
            if owner is None:
                return
            ww.spawn_hitbox_at(owner, z.x, z.y, 340, 310, 64, 860, 0,
                               ttl=3, radial=True, tag="orbital")
            ww.fx("boom", z.x, z.y, r=165)
            ww.shake = min(24.0, ww.shake + 14.0)
        w.spawn_zone(f, "mark", x, y, 165, 1.7, on_end=end)


GAYKA = Hero(
    "gayka", "Гайка", "Инженер", "#ffd166", "#fff0b8",
    "Ставит железо, дёргает за крюк и роняет небо на голову.",
    hp=118, weight=1.1, speed=1.05, jumps=2, difficulty=3,
    passive="Перегрузка", passive_desc="Каждый 4-й удар ключом: ×2 урона и оглушение.",
    abilities=[
        Ability("basic", "Ключ", "Ближний удар. Каждый 4-й — усиленный.", 0.38, 0.3, gayka_basic, move=0.6),
        Ability("q", "Турель", "Турель на 14 с: 9 урона каждые 0.44 с. Только одна.", 10.0, 0.45, gayka_q, move=0.35),
        Ability("e", "Крюк", "Во врага — притягивает его. В стену — притягивает себя.", 8.0, 0.55,
                gayka_e, move=0.25, face_lock=False),
        Ability("r", "Орбитальный удар", "Метка, через 1.7 с — 64 урона и мощный подброс.", 0.6, 0.5,
                gayka_r, move=0.3, face_lock=False),
    ],
)


# =================================================================== ПУЛЬС
def puls_basic(w, f, a):
    if a.t == 2:
        ax, ay = aim(f)
        w.spawn_proj(f, "orb", ax * 900, ay * 900 - 50, 10, 170, 1.7, r=10,
                     ox=28, oy=-4, grav=170, heal=13)


def puls_q(w, f, a):
    if a.t == 0:
        x, y = clamp_point(f, 270)

        def tick(ww, z):
            if z.tick < 0.5:
                return
            z.tick = 0.0
            for o in ww.fighters.values():
                if not (o.alive and o.team == z.team and ww.in_zone(z, o)):
                    continue
                if ww.time - o.mem.get("field_t", -9.0) < 0.45:
                    continue            # поля не складываются
                o.mem["field_t"] = ww.time
                ww.heal(o, 3.8)
            ww.fx("pulse", z.x, z.y, r=z.r)
        w.spawn_zone(f, "field", x, y, 158, 4.0, on_tick=tick)


def puls_e(w, f, a):
    if a.t == 3:
        w.spawn_hitbox_at(f, f.cx, f.cy, 310, 250, 10, 470, 0, ttl=3,
                          radial=True, tag="push")
        for o in w.allies_of(f):
            if (o.cx - f.cx) ** 2 + (o.cy - f.cy) ** 2 < 170 ** 2:
                o.shield = max(o.shield, 22.0)
                o.add_effect("haste", 2.5, 0.25)
                w.fx("shieldon", o.cx, o.cy)
        for p in list(w.projectiles):
            if p.team != f.team and (p.x - f.cx) ** 2 + (p.y - f.cy) ** 2 < 175 ** 2:
                w.projectiles.remove(p)
                w.fx("pdie", p.x, p.y, kk=p.kind)
        w.fx("shock", f.cx, f.cy, r=170)


def puls_r(w, f, a):
    if a.t == 0:
        w.fx("ultflash", f.cx, f.cy, c=f.hero.color)
    if a.t == 10:
        for o in w.allies_of(f):
            if (o.cx - f.cx) ** 2 + (o.cy - f.cy) ** 2 > 430 ** 2:
                continue
            w.heal(o, 34, f)
            o.add_effect("invuln", 1.0)
            for bad in ("slow", "freeze", "stun", "burn"):
                o.effects.pop(bad, None)
            w.fx("revive", o.cx, o.cy)
        w.fx("shock", f.cx, f.cy, r=430)


def puls_deal(w, f, t, dmg, tag):
    if tag != "burn":
        w.heal(f, dmg * 0.10)
    return dmg


PULS = Hero(
    "puls", "Пульс", "Поддержка", "#b58cff", "#e6d9ff",
    "Один держит команду на ногах. В соло — терпимо, в 2х2 — обязателен.",
    hp=106, weight=1.0, speed=1.08, jumps=2, difficulty=2,
    passive="Симбиоз", passive_desc="Лечит себя на 10% от нанесённого урона.",
    on_deal=puls_deal,
    abilities=[
        Ability("basic", "Сгусток", "10 урона врагу или 13 лечения союзнику.", 0.42, 0.25,
                puls_basic, move=0.7, face_lock=False),
        Ability("q", "Поле", "Зона на 4 с: союзникам +7.6 HP/с. Два поля не складываются.", 10.5, 0.5, puls_q, move=0.4, face_lock=False),
        Ability("e", "Толчок", "Отбрасывает врагов и сбивает снаряды, союзникам щит 22.", 11.5, 0.4,
                puls_e, move=0.35),
        Ability("r", "Второе дыхание", "Союзникам рядом: +34 HP, чистка эффектов, неуязвимость 1 с.",
                0.6, 0.7, puls_r, move=0.3),
    ],
)


# ==================================================================== ГОРН
def _burn(t, dur, dps, src):
    t.add_effect("burn", dur, dps, src=src)


def _fire_zone(w, owner, x, y, r, ttl, dps, burn_dps, burn_dur, weak=0.0):
    def tick(ww, z):
        if z.tick < 0.5:
            return
        z.tick = 0.0
        o_ = ww.fighters.get(z.owner)
        for o in ww.fighters.values():
            if not o.alive or o.team == z.team or not ww.in_zone(z, o):
                continue
            ww.damage(o_, o, dps, 0, 0, fx="burn", tag="fire")
            _burn(o, burn_dur, burn_dps, z.owner)
            if weak > 0:
                o.add_effect("weak", 1.2, weak)
        ww.fx("burn", z.x, z.y, r=z.r)
    return w.spawn_zone(owner, "fire", x, y, r, ttl, on_tick=tick)


def gorn_basic(w, f, a):
    if a.t == 2:
        ax, ay = aim(f)
        w.spawn_proj(f, "bolt", ax * 1020, ay * 1020 - 40, 6, 120, 1.3, r=9,
                     ox=28, oy=-4, grav=260,
                     on_hit=lambda ww, p, t: _burn(t, 2.0, 4.2, p.owner) if t else None)


def gorn_q(w, f, a):
    if a.t == 3:
        ax, ay = aim(f)

        def land(ww, p, t):
            owner = ww.fighters.get(p.owner)
            if owner is None:
                return
            _fire_zone(ww, owner, p.x, p.y, 128, 5.0, 3.6, 4.0, 1.8)
            ww.fx("boom", p.x, p.y, r=80)
        w.spawn_proj(f, "orb", ax * 880, ay * 880 - 150, 9, 110, 2.4, r=12,
                     ox=26, oy=-6, grav=880, on_hit=land)
    if a.t < 3:
        f.vx *= 0.88


def gorn_e(w, f, a):
    if a.t == 0:
        w.fx("windup", f.cx, f.cy, d=f.face)
        f.vx *= 0.7
    if a.t == 5:
        w.spawn_hitbox(f, 128, 0, 244, 136, 13, 420, 26, ttl=4, fx="burn",
                       on_hit=lambda ww, src, t: _burn(t, 2.0, 5.0, src.pid),
                       tag="flame")
        w.fx("cone", f.cx, f.cy, d=f.face)
        # реактивная отдача: отбрасывает Горна назад — это же и его возврат на арену
        f.vx = -640 * f.face
        f.vy = min(f.vy, 0.0) - 330
        f.jumps = max(f.jumps, 1)


def gorn_r(w, f, a):
    if a.t == 0:
        x, y = clamp_point(f, 360)
        w.fx("ultflash", f.cx, f.cy, c=f.hero.color)
        z = _fire_zone(w, f, x, y, 232, 4.5, 4.5, 5.0, 1.8, weak=0.25)

        def end(ww, zz):
            owner = ww.fighters.get(zz.owner)
            if owner is None:
                return
            ww.spawn_hitbox_at(owner, zz.x, zz.y, 300, 280, 28, 620, 0,
                               ttl=3, radial=True, tag="fire")
            ww.fx("boom", zz.x, zz.y, r=150)
            ww.shake = min(24.0, ww.shake + 12.0)
        z.on_end = end


def gorn_tick(w, f):
    n = sum(1 for o in w.enemies_of(f) if o.has("burn"))
    if n:
        f.add_effect("dmg_up", 0.2, min(0.14, 0.07 * n))
        w.heal(f, 0.8 * n * C.DT)


GORN = Hero(
    "gorn", "Горн", "Поджигатель", "#e8482c", "#ffb08a",
    "Не убивает сразу — убивает потом. Под ним бесполезно лечиться.",
    hp=122, weight=1.12, speed=0.98, jumps=2, difficulty=2,
    passive="Раскалённый",
    passive_desc="За каждого горящего врага: +7% урона (до 14%) и +0.8 HP/с. Горящие лечатся вдвое хуже.",
    on_tick=gorn_tick,
    abilities=[
        Ability("basic", "Уголёк", "6 урона и поджог: 4.2 урона в секунду на 2 с.", 0.46, 0.24,
                gorn_basic, move=0.7, face_lock=False),
        Ability("q", "Напалм", "Навесной заряд: лужа огня на 5 с, поджигает всех внутри.", 7.0, 0.35,
                gorn_q, move=0.35, face_lock=False),
        Ability("e", "Выхлоп", "Струя пламени: 13 урона и поджог. Горна отбрасывает назад.", 8.5, 0.45,
                gorn_e, move=0.3),
        Ability("r", "Домна", "Зона на 4.5 с: 4.5 урона каждые 0.5 с, поджог и −25% урона врагам. "
                "В конце — взрыв на 28.", 0.6, 0.55, gorn_r, move=0.3, face_lock=False),
    ],
)


# ================================================================= ЗЕРКАЛО
def zerkalo_basic(w, f, a):
    if a.t == 3:
        w.spawn_hitbox(f, 60, -6, 88, 52, 6, 170, 12, ttl=3, tag="rapier")
        w.fx("slash", f.cx + 60 * f.face, f.cy - 6, d=f.face, s=0.9)
    if a.t == 11:
        w.spawn_hitbox(f, 68, 4, 94, 58, 9, 300, 20, ttl=3, tag="rapier")
        w.fx("slash", f.cx + 68 * f.face, f.cy + 4, d=f.face, s=1.2)


def zerkalo_q(w, f, a):
    if a.t == 0:
        f.mem["parry"] = 1
        w.fx("guard", f.cx, f.cy, d=f.face)
    if a.t % 7 == 0 and a.t < 17:
        w.fx("guardtick", f.cx + 26 * f.face, f.cy, d=f.face)
    f.vx *= 0.88
    if a.t >= 17:
        f.mem.pop("parry", None)


def _clinch(w, src, t):
    sx, sy = src.x, src.y
    src.x, src.y = t.x, t.y
    t.x, t.y = sx, sy
    t.vx *= 0.25
    t.hitstun = max(t.hitstun, 0.3)
    t.act = None
    t.add_effect("weak", 3.0, 0.35)
    src.face = 1 if t.cx >= src.cx else -1
    w.fx("pull", t.cx, t.cy)
    w.fx("pull", src.cx, src.cy)


def zerkalo_e(w, f, a):
    if a.t < 4:
        f.vx *= 0.85
    if a.t == 4:
        w.spawn_hitbox(f, 48, 0, 92, 80, 9, 0, 0, ttl=4, on_hit=_clinch, tag="clinch")
        w.fx("bash", f.cx + 48 * f.face, f.cy, d=f.face)


def zerkalo_r(w, f, a):
    if a.t == 0:
        w.fx("ultflash", f.cx, f.cy, c=f.hero.color)
        f.mem["reflect"] = w.time + 3.6
        f.add_effect("dr", 3.6, 0.5)
        f.shield = max(f.shield, 26.0)
    if a.t % 9 == 0:
        w.fx("guard", f.cx, f.cy, d=f.face)


def zerkalo_spawn(w, f):
    f.mem.pop("parry", None)
    f.mem.pop("reflect", None)


def zerkalo_take(w, f, src, dmg, tag):
    # 1) парирование: съедает удар и возвращает рипост с оглушением
    if f.mem.get("parry") and src is not None and dmg >= 5.0 and tag != "burn":
        f.mem.pop("parry", None)
        f.face = 1 if src.cx >= f.cx else -1
        f.iframes = max(f.iframes, 0.14)
        f.add_effect("dmg_up", 2.5, 0.3)
        f.ult = min(C.ULT_MAX, f.ult + 14.0)
        w.heal(f, min(18.0, dmg * 0.6))
        w.spawn_hitbox(f, 64, 0, 128, 84, 13, 440, 24, ttl=4,
                       on_hit=lambda ww, s, t: t.add_effect("stun", 0.45),
                       tag="riposte")
        w.fx("sparks", f.cx + 44 * f.face, f.cy, s=1.8)
        w.fx("guardtick", f.cx, f.cy, d=f.face)
        return 0.0
    # 2) ульта: часть урона улетает обратно самонаводящимся снарядом
    if f.mem.get("reflect", 0.0) > w.time and src is not None and dmg >= 3.0:
        dx, dy = src.cx - f.cx, src.cy - f.cy
        d = math.hypot(dx, dy) or 1.0
        w.spawn_proj(f, "bolt2", dx / d * 1150, dy / d * 1150,
                     dmg * 0.65, 160, 1.4, r=8, homing=2.4)
        w.fx("shieldhit", f.cx, f.cy)
    # 3) пассивка: тяжёлый удар заводит Зеркало, мелкий чип — нет
    if dmg >= 12.0:
        f.add_effect("dmg_up", 2.0, 0.35)
    return dmg


ZERKALO = Hero(
    "zerkalo", "Зеркало", "Контратака", "#aebfd4", "#eef3f9",
    "Чем сильнее по нему бьют, тем больнее он отвечает.",
    hp=106, weight=1.10, speed=1.03, jumps=2, difficulty=3,
    passive="Контртемп",
    passive_desc="Удар в 12+ урона даёт Зеркалу +35% урона на 2 с. Мелкий чип не считается.",
    on_take=zerkalo_take, on_spawn=zerkalo_spawn,
    abilities=[
        Ability("basic", "Двойка", "Два быстрых укола рапирой с хорошим вылетом.", 0.38, 0.26,
                zerkalo_basic, move=0.6),
        Ability("q", "Парирование", "0.28 с: следующий серьёзный удар поглощается, "
                "в ответ — 13 урона, оглушение 0.45 с и лечение.", 8.5, 0.42,
                zerkalo_q, move=0.3, face_lock=False, armor=1.0),
        Ability("e", "Клинч", "Меняется местами с врагом: 9 урона и −35% его урона на 3 с.",
                8.0, 0.4, zerkalo_e, move=0.35),
        Ability("r", "Отражение", "3.6 с: −50% входящего урона, часть летит обратно в атакующего.",
                0.6, 0.45, zerkalo_r, move=0.5),
    ],
)


# =================================================================== ЯКОРЬ
def yakor_basic(w, f, a):
    if a.t == 5:
        if f.on_ground:
            # низкий пологий замах: гонит врага вбок, к краю
            w.spawn_hitbox(f, 54, -2, 94, 78, 19, 440, 14, ttl=3, tag="maul")
        else:
            # в воздухе — вниз. Под краем арены это смертельно
            w.spawn_hitbox(f, 48, 18, 84, 78, 16, 420, -56, ttl=3, tag="maul")
        w.fx("bash", f.cx + 52 * f.face, f.cy, d=f.face)
    if a.t < 5:
        f.vx *= 0.9


def yakor_q(w, f, a):
    if a.t == 3:
        x, y = clamp_point(f, 300)

        def tick(ww, z):
            for o in ww.fighters.values():
                if not o.alive:
                    continue
                own = o.pid == z.owner
                if o.team == z.team and not own:
                    continue
                if own and o.on_ground:
                    continue            # себя тянет только в воздухе — это возврат на арену
                dx, dy = z.x - o.cx, z.y - o.cy
                d = math.hypot(dx, dy) or 1.0
                if d > z.r * 1.4:
                    continue
                pull = 1050.0 if own else 1850.0
                o.vx += dx / d * pull * C.DT
                o.vy += dy / d * pull * C.DT
                if not own:
                    o.add_effect("slow", 0.3, 0.22)
            if z.tick >= 0.3:
                z.tick = 0.0
                owner = ww.fighters.get(z.owner)
                for o in ww.fighters.values():
                    if o.alive and o.team != z.team and ww.in_zone(z, o):
                        ww.damage(owner, o, 4, 0, 0, fx="shock", tag="gravity")
                ww.fx("pull", z.x, z.y, r=z.r)
        w.spawn_zone(f, "well", x, y, 178, 1.7, on_tick=tick)
    if a.t < 3:
        f.vx *= 0.9


def _slam_hit(w, src, t):
    dx, dy = src.cx - t.cx, src.cy - t.cy
    d = math.hypot(dx, dy) or 1.0
    t.vx = dx / d * 540
    t.vy = -250
    t.on_ground = False
    t.hitstun = max(t.hitstun, 0.32)
    t.act = None
    t.add_effect("weak", 3.0, 0.3)
    t.add_effect("slow", 1.6, 0.3)
    w.fx("pull", t.cx, t.cy)


def yakor_e(w, f, a):
    if a.t == 0:
        f.vy = -300
        f.vx *= 0.5
        w.fx("windup", f.cx, f.cy, d=f.face)
    if a.t == 8:
        f.mem["slam"] = 1
    if a.t >= 8 and f.mem.get("slam"):
        f.vy = max(f.vy, 1500.0)
        if f.on_ground or a.t >= a.dur - 2:
            f.mem.pop("slam", None)
            w.spawn_hitbox_at(f, f.cx, f.y + C.FH, 355, 205, 24, 0, 0, ttl=3,
                              on_hit=_slam_hit, fx="shock", tag="slam")
            w.fx("boom", f.cx, f.y + C.FH, r=150)
            w.shake = min(18.0, w.shake + 7.0)
            f.add_effect("anchor", 3.0)
            f.act = None


def yakor_r(w, f, a):
    if a.t == 0:
        x, y = clamp_point(f, 380)
        w.fx("ultflash", f.cx, f.cy, c=f.hero.color)

        def tick(ww, z):
            owner = ww.fighters.get(z.owner)
            for o in ww.fighters.values():
                if not o.alive or o.team == z.team:
                    continue
                dx, dy = z.x - o.cx, z.y - o.cy
                d = math.hypot(dx, dy) or 1.0
                if d > z.r * 1.5:
                    continue
                o.vx += dx / d * 2000 * C.DT
                o.vy += dy / d * 2000 * C.DT
                o.add_effect("slow", 0.3, 0.35)
                o.add_effect("weak", 0.6, 0.3)
            if z.tick >= 0.3:
                z.tick = 0.0
                for o in ww.fighters.values():
                    if o.alive and o.team != z.team and ww.in_zone(z, o):
                        ww.damage(owner, o, 4, 0, 0, fx="shock", tag="gravity")
                ww.fx("pull", z.x, z.y, r=z.r)

        def end(ww, z):
            owner = ww.fighters.get(z.owner)
            if owner is None:
                return
            ww.spawn_hitbox_at(owner, z.x, z.y, 340, 320, 24, 880, 0,
                               ttl=3, radial=True, tag="gravity")
            ww.fx("boom", z.x, z.y, r=170)
            ww.shake = min(24.0, ww.shake + 13.0)
        w.spawn_zone(f, "well", x, y, 258, 3.2, on_tick=tick, on_end=end)


def yakor_tick(w, f):
    if f.on_ground:
        f.add_effect("anchor", 0.12)


YAKOR = Hero(
    "yakor", "Якорь", "Гравитация", "#4361ee", "#a8b8ff",
    "Стоя на земле — не сдвинуть. В воздухе — обычный мешок.",
    hp=138, weight=1.18, speed=0.95, jumps=2, grav=1.12, difficulty=2,
    passive="Балласт",
    passive_desc="Пока стоит на земле — −65% получаемого отброса. В воздухе пассивка не работает.",
    on_tick=yakor_tick,
    abilities=[
        Ability("basic", "Грузило", "На земле бьёт вбок, в воздухе — вниз. "
                "Удар сверху сбивает под арену.", 0.40, 0.3, yakor_basic, move=0.55),
        Ability("q", "Воронка", "Колодец на 1.7 с: тянет врагов к центру и бьёт по 4 каждые 0.3 с. "
                "Самого Якоря в воздухе тоже — так он возвращается.", 8.0, 0.4,
                yakor_q, move=0.4, face_lock=False),
        Ability("e", "Обвал", "Падение с ударной волной: 24 урона, стягивает врагов к себе, "
                "−30% их урона на 3 с.", 9.0, 0.9, yakor_e, move=0.25, armor=0.4),
        Ability("r", "Сингулярность", "Колодец на 3.2 с: тянет и бьёт по 4 каждые 0.3 с, "
                "в конце схлопывается на 24 с мощным отбросом.", 0.6, 0.5,
                yakor_r, move=0.3, face_lock=False),
    ],
)


HEROES = {h.id: h for h in (REZAK, BUNKER, IGLA, VYUGA, GAYKA, PULS,
                            GORN, ZERKALO, YAKOR)}
HERO_ORDER = ["rezak", "bunker", "igla", "vyuga", "gayka", "puls",
               "gorn", "zerkalo", "yakor"]


def get(hid):
    return HEROES.get(hid) or REZAK


def catalog():
    return [HEROES[h].info() for h in HERO_ORDER]
