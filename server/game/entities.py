"""Сущности симуляции. Никакой логики — только данные и мелкие хелперы."""

import math

from . import const as C


class Action:
    """Текущее действие бойца (атака/способность). fn вызывается каждый тик."""

    __slots__ = ("key", "fn", "t", "dur", "move", "mem", "face_lock", "armor")

    def __init__(self, key, fn, dur_ticks, move=0.0, face_lock=True, armor=0.0):
        self.key = key
        self.fn = fn
        self.t = 0
        self.dur = dur_ticks
        self.move = move          # 0..1 — сколько контроля над движением остаётся
        self.face_lock = face_lock
        self.armor = armor        # 0..1 — сколько отброса игнорируется


class Fighter:
    __slots__ = (
        "pid", "team", "slot", "name", "hero", "is_bot",
        "x", "y", "vx", "vy", "on_ground", "jumps", "face",
        "hp", "max_hp", "stocks", "ult", "alive", "respawn_t",
        "inp", "aim_x", "aim_y", "act", "cds", "dash_cd", "dash_t",
        "hitstun", "iframes", "effects", "combo", "combo_t",
        "dmg_dealt", "dmg_taken", "kills", "deaths", "last_hit_by", "last_hit_t",
        "no_dmg_t", "mem", "dr", "speed_mul", "dmg_mul", "shield", "platform_drop",
        "prev_inp", "hitlag",
    )

    def __init__(self, pid, team, slot, name, hero, is_bot=False):
        self.pid = pid
        self.team = team
        self.slot = slot
        self.name = name
        self.hero = hero
        self.is_bot = is_bot
        self.x = 0.0
        self.y = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.on_ground = False
        self.jumps = hero.jumps
        self.face = 1
        self.max_hp = float(hero.hp)
        self.hp = self.max_hp
        self.stocks = C.DEFAULT_STOCKS
        self.ult = 0.0
        self.alive = True
        self.respawn_t = 0.0
        self.inp = 0
        self.prev_inp = 0
        self.aim_x = 1.0
        self.aim_y = 0.0
        self.act = None
        self.cds = {"q": 0.0, "e": 0.0, "r": 0.0, "basic": 0.0}
        self.dash_cd = 0.0
        self.dash_t = 0.0
        self.hitstun = 0.0
        self.hitlag = 0.0
        self.iframes = 0.0
        self.effects = {}
        self.combo = 0
        self.combo_t = 0.0
        self.dmg_dealt = 0.0
        self.dmg_taken = 0.0
        self.kills = 0
        self.deaths = 0
        self.last_hit_by = None
        self.last_hit_t = 0.0
        self.no_dmg_t = 0.0
        self.mem = {}
        self.dr = 0.0            # damage reduction 0..1, пересчитывается каждый тик
        self.speed_mul = 1.0
        self.dmg_mul = 1.0
        self.shield = 0.0
        self.platform_drop = 0.0

    # геометрия
    @property
    def cx(self):
        return self.x + C.FW * 0.5

    @property
    def cy(self):
        return self.y + C.FH * 0.5

    def rect(self):
        return (self.x, self.y, C.FW, C.FH)

    def pressed(self, bit):
        return bool(self.inp & bit) and not (self.prev_inp & bit)

    def held(self, bit):
        return bool(self.inp & bit)

    def has(self, eff):
        return eff in self.effects

    def add_effect(self, name, dur, mag=1.0, src=None):
        e = self.effects.get(name)
        if e and e["t"] > dur and name not in ("slow",):
            e["mag"] = max(e["mag"], mag)
            return
        self.effects[name] = {"t": dur, "mag": mag, "src": src}


class Hitbox:
    __slots__ = ("id", "owner", "team", "x", "y", "w", "h", "dmg", "kb", "angle",
                 "ttl", "follow", "ox", "oy", "hit", "on_hit", "fx", "hitstun_mul",
                 "hits_structs", "tag", "freeze_angle")

    def __init__(self, hid, owner, team, x, y, w, h, dmg, kb, angle, ttl,
                 follow=None, ox=0.0, oy=0.0, on_hit=None, fx="hit",
                 hitstun_mul=1.0, hits_structs=True, tag="", freeze_angle=False):
        self.id = hid
        self.owner = owner
        self.team = team
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.dmg = dmg
        self.kb = kb
        self.angle = angle          # градусы, 0 = вправо, 90 = вверх
        self.ttl = ttl
        self.follow = follow
        self.ox = ox
        self.oy = oy
        self.hit = set()
        self.on_hit = on_hit
        self.fx = fx
        self.hitstun_mul = hitstun_mul
        self.hits_structs = hits_structs
        self.tag = tag
        self.freeze_angle = freeze_angle  # True -> угол абсолютный, не зеркалится


class Projectile:
    __slots__ = ("id", "owner", "team", "kind", "x", "y", "vx", "vy", "r",
                 "dmg", "kb", "angle", "ttl", "grav", "pierce", "hit", "on_hit",
                 "terrain", "heal", "spin", "data", "homing")

    def __init__(self, pid_, owner, team, kind, x, y, vx, vy, r, dmg, kb, ttl,
                 angle=None, grav=0.0, pierce=1, on_hit=None, terrain=True,
                 heal=0.0, data=None, homing=0.0):
        self.id = pid_
        self.owner = owner
        self.team = team
        self.kind = kind
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.r = r
        self.dmg = dmg
        self.kb = kb
        self.angle = angle
        self.ttl = ttl
        self.grav = grav
        self.pierce = pierce
        self.hit = set()
        self.on_hit = on_hit
        self.terrain = terrain
        self.heal = heal
        self.spin = 0.0
        self.data = data or {}
        self.homing = homing


class Structure:
    """Турель, мина, ледяная стена — всё, что стоит на арене."""

    __slots__ = ("id", "owner", "team", "kind", "x", "y", "w", "h", "hp", "max_hp",
                 "ttl", "solid", "blocks_shots", "data", "t")

    def __init__(self, sid, owner, team, kind, x, y, w, h, hp, ttl,
                 solid=False, blocks_shots=False, data=None):
        self.id = sid
        self.owner = owner
        self.team = team
        self.kind = kind
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.hp = float(hp)
        self.max_hp = float(hp)
        self.ttl = ttl
        self.solid = solid
        self.blocks_shots = blocks_shots
        self.data = data or {}
        self.t = 0.0

    @property
    def cx(self):
        return self.x + self.w * 0.5

    @property
    def cy(self):
        return self.y + self.h * 0.5


class Zone:
    """Круглая область с периодическим эффектом."""

    __slots__ = ("id", "owner", "team", "kind", "x", "y", "r", "ttl", "max_ttl",
                 "tick", "data", "on_tick", "on_end", "follow")

    def __init__(self, zid, owner, team, kind, x, y, r, ttl,
                 on_tick=None, on_end=None, data=None, follow=None):
        self.id = zid
        self.owner = owner
        self.team = team
        self.kind = kind
        self.x = x
        self.y = y
        self.r = r
        self.ttl = ttl
        self.max_ttl = ttl
        self.tick = 0.0
        self.data = data or {}
        self.on_tick = on_tick
        self.on_end = on_end
        self.follow = follow


def rects_overlap(ax, ay, aw, ah, bx, by, bw, bh):
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def circle_rect(cx, cy, r, rx, ry, rw, rh):
    nx = max(rx, min(cx, rx + rw))
    ny = max(ry, min(cy, ry + rh))
    dx = cx - nx
    dy = cy - ny
    return dx * dx + dy * dy <= r * r


def ang_vec(deg):
    a = math.radians(deg)
    return math.cos(a), -math.sin(a)
