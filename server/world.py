# -*- coding: utf-8 -*-
"""Состояние мира: тик, реестр сущностей, снапшоты (DESIGN.md 4, 5.2).

Мир не знает ни про сеть, ни про рендер. Он умеет: шагнуть на тик,
завести/убрать сущность и отдать снапшот — полный или дельту.

Дельта считается сравнением с тем, что уже отправлено, причём сравниваются
УЖЕ ОКРУГЛЁННЫЕ массивы (proto.encode_entity). Поэтому дрожание ниже
точности округления (5.2) трафика не создаёт.
"""

import math

from . import physics
from . import proto

TICK_HZ = 30
DT = 1.0 / TICK_HZ

# kind
K_PLAYER = 1
K_ENEMY = 2
K_PROP = 3
K_SHOT = 4

# flags
F_DEAD = 1
F_OFFLINE = 2

# 4.2: скорости в клетках в секунду
SPEED_RUN = 5.0
SPEED_SHOT = 12.0

R_PLAYER = 0.35   # радиус игрока: проходит в проём в одну клетку


class Entity(object):
    __slots__ = ("id", "kind", "x", "y", "vx", "vy", "hp", "hp_max",
                 "flags", "facing", "r", "speed", "ctl", "mv", "btn",
                 "aim", "name", "ttl")

    def __init__(self, eid, kind, x, y):
        self.id = eid
        self.kind = kind
        self.x = float(x)
        self.y = float(y)
        self.vx = 0.0
        self.vy = 0.0
        self.hp = 100
        self.hp_max = 100
        self.flags = 0
        self.facing = 0.0
        self.r = 0.3
        self.speed = 0.0
        self.ctl = False          # управляется вводом игрока
        self.mv = (0.0, 0.0)
        self.btn = 0
        self.aim = (0.0, 0.0)
        self.name = ""
        self.ttl = 0


class World(object):
    def __init__(self, grid, seed=1, floor=1, spawns=None):
        self.grid = grid
        self.seed = seed
        self.floor = floor
        self.spawns = spawns or [(2.5, 2.5)]
        self.tick = 0
        self.entities = {}
        self._next_id = 1
        self._sent = {}        # id -> последний отправленный массив
        self._removed = []     # id, удалённые с прошлой дельты

    # --- реестр ------------------------------------------------------------

    def new_id(self):
        i = self._next_id
        self._next_id += 1     # id не переиспользуются (4.3)
        return i

    def spawn(self, kind, x, y, **kw):
        e = Entity(self.new_id(), kind, x, y)
        for k, v in kw.items():
            setattr(e, k, v)
        self.entities[e.id] = e
        return e

    def spawn_player(self, name="", idx=0):
        sx, sy = self.spawns[idx % len(self.spawns)]
        sx, sy = physics.free_spot(self.grid, sx, sy, R_PLAYER)
        return self.spawn(K_PLAYER, sx, sy, r=R_PLAYER, speed=SPEED_RUN,
                          ctl=True, name=name, hp=100, hp_max=100)

    def remove(self, eid):
        if self.entities.pop(eid, None) is not None:
            if self._sent.pop(eid, None) is not None:
                self._removed.append(eid)

    # --- тик ---------------------------------------------------------------

    def step(self, dt=DT):
        self.tick += 1
        grid = self.grid
        for e in self.entities.values():
            if e.ctl:
                if e.flags & F_DEAD:
                    e.vx = e.vy = 0.0
                else:
                    e.vx = e.mv[0] * e.speed
                    e.vy = e.mv[1] * e.speed
                ax, ay = e.aim
                if ax or ay:
                    e.facing = math.atan2(ay - e.y, ax - e.x)
                elif e.vx or e.vy:
                    e.facing = math.atan2(e.vy, e.vx)
            if e.ttl:
                e.ttl -= 1
                if e.ttl <= 0:
                    self.remove(e.id)
                    continue
            if e.vx or e.vy:
                nx, ny, hit = physics.move_circle(grid, e.x, e.y,
                                                  e.vx * dt, e.vy * dt, e.r)
                e.x = nx
                e.y = ny
                if hit & 1:
                    e.vx = 0.0
                if hit & 2:
                    e.vy = 0.0
        return self.tick

    # --- снапшоты ----------------------------------------------------------

    def snapshot_full(self, remember=True):
        """Полный снапшот: хвост сообщения (без головы с ack)."""
        enc = proto.encode_entity
        rows = []
        sent = {} if remember else None
        for e in self.entities.values():
            a = enc(e)
            rows.append(a)
            if remember:
                sent[e.id] = a
        if remember:
            self._sent = sent
            self._removed = []
        return proto.snap_tail(self.tick, True, rows)

    def snapshot_delta(self):
        """Дельта: изменившиеся сущности + список удалённых rm (5.2)."""
        enc = proto.encode_entity
        sent = self._sent
        rows = []
        for eid, e in self.entities.items():
            a = enc(e)
            if sent.get(eid) != a:
                rows.append(a)
                sent[eid] = a
        rm = self._removed
        self._removed = []
        return proto.snap_tail(self.tick, False, rows, rm)

    def stats(self):
        return {"tick": self.tick, "entities": len(self.entities)}
