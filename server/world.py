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
from . import vis as vis_mod

TICK_HZ = 30
DT = 1.0 / TICK_HZ

# kind
K_PLAYER = 1
K_ENEMY = 2
K_PROP = 3
K_SHOT = 4

# flags (4.3). DEAD и OFFLINE — из контракта; WINDUP и DASH добавлены на
# этапе 2a и вынесены в отчёт как правка 4.3. Почему битом, а не событием:
# 5.2 запрещает держать состояние на ev, а замах игрок обязан ВИДЕТЬ все
# 0.25 с подряд — потерянный пакет превратил бы его в удар из ниоткуда.
# Цена нулевая: поле flags уже в снапшоте, значения 0..15 — тот же один-два
# символа.
F_DEAD = 1
F_OFFLINE = 2
F_WINDUP = 4        # идёт замах ближней атаки (4.2)
F_DASH = 8          # идёт рывок, урон не проходит (4.2)

# 4.2: скорости в клетках в секунду
SPEED_RUN = 5.0
SPEED_SHOT = 12.0

R_PLAYER = 0.35   # радиус игрока: проходит в проём в одну клетку


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


# Импорт боя стоит НИЖЕ констант намеренно. combat.py читает отсюда TICK_HZ и
# SPEED_SHOT (числа 4.1/4.2 живут в одном месте, а не в двух), а world.py
# вызывает combat в тике — импорт взаимный. В Python 3.11 он работает в обе
# стороны ровно при одном условии: к моменту, когда исполняется тело
# combat.py, нужные ему имена в world уже определены. Отсюда и место строки.
# Обратный порядок (import combat первым) тоже проверен: тогда world
# выполняется целиком, а combat получает уже готовый модуль.
from . import combat        # noqa: E402


class Entity(object):
    # Первые десять полей — контрактные (4.3), они и уходят в снапшот.
    # Остальное серверное: в снапшот не попадает никогда.
    __slots__ = ("id", "kind", "x", "y", "vx", "vy", "hp", "hp_max",
                 "flags", "facing", "r", "speed", "ctl", "mv", "btn",
                 "aim", "name", "ttl", "team",
                 # бой (combat.py): все времена — АБСОЛЮТНЫЕ номера тиков,
                 # а не счётчики. Счётчик надо уменьшать ровно один раз за
                 # тик и ровно в одном месте, иначе неуязвимость рывка
                 # съезжает на тик в ту или другую сторону; сравнение с
                 # world.tick съехать не может.
                 "atk_hit", "atk_ready", "dash_end", "dash_ready",
                 "dash_dx", "dash_dy", "inv_end", "shot_ready", "shot_q",
                 "owner", "dmg")

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
        # команда: 1 игроки, 2 враги, 0 нейтрал (реквизит). Бьют друг друга
        # только разные команды, поэтому дружественного огня нет по
        # построению, а не по проверке "если это игрок".
        self.team = 1 if kind == K_PLAYER else (2 if kind == K_ENEMY else 0)
        self.atk_hit = 0        # тик, на котором прилетит удар (0 — нет замаха)
        self.atk_ready = 0      # тик, с которого можно бить снова
        self.dash_end = 0       # последний тик рывка (0 — не в рывке)
        self.dash_ready = 0     # тик, с которого можно рвануть снова
        self.dash_dx = 0.0
        self.dash_dy = 0.0
        self.inv_end = 0        # последний тик неуязвимости
        self.shot_ready = 0
        self.shot_q = 0         # выстрел заказан, родится в combat.resolve
        self.owner = 0          # для снаряда: чей
        self.dmg = 0            # для снаряда: сколько снимает


class World(object):
    def __init__(self, grid, seed=1, floor=1, spawns=None, stairs=None):
        self.grid = grid
        self.seed = seed
        self.floor = floor
        self.spawns = spawns or [(2.5, 2.5)]
        self.stairs = stairs          # (tx,ty) лестницы вниз (8.1)
        # Туман — ОДИН на комнату (4.4): считается здесь, в снапшот уходит
        # одной строкой на всех, а не на каждого игрока (11.2).
        self.fog = vis_mod.Fog(grid)
        self.tick = 0
        self.entities = {}
        self._next_id = 1
        self._sent = {}        # id -> последний отправленный массив
        self._removed = []     # id, удалённые с прошлой дельты
        self.events = []       # (tick, kind, kw) -> proto.ev, вынимает комната

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
        gone = None
        for e in self.entities.values():
            # кнопки и боевые таймеры — до движения: рывок выставляет скорость
            if e.ctl or e.atk_hit or e.dash_end:
                combat.begin(self, e)
            if e.ctl:
                if e.dash_end:
                    pass                      # скорость рывка выставил combat
                else:
                    e.vx = e.mv[0] * e.speed
                    e.vy = e.mv[1] * e.speed
                if not e.atk_hit:
                    # на замахе направление заморожено: дуга ударит ровно там,
                    # где клиент её рисовал все 8 тиков (4.2)
                    ax, ay = e.aim
                    if ax or ay:
                        e.facing = math.atan2(ay - e.y, ax - e.x)
                    elif e.vx or e.vy:
                        e.facing = math.atan2(e.vy, e.vx)
            if e.ttl:
                e.ttl -= 1
                if e.ttl <= 0:
                    # убирать ВНУТРИ цикла нельзя: словарь меняется во время
                    # итерации. До снарядов ttl никто не ставил, и это не
                    # стреляло; со снарядами стрельнуло бы в первую же минуту.
                    if gone is None:
                        gone = []
                    gone.append(e.id)
                    continue
            if e.vx or e.vy:
                if e.ctl and (e.flags & F_DEAD):
                    # дух летает (8.5): стены ему не преграда, зато он не
                    # светит туман (viewers) и не трогает бой
                    e.x = _clamp(e.x + e.vx * dt, 0.5, grid.w - 0.5)
                    e.y = _clamp(e.y + e.vy * dt, 0.5, grid.h - 0.5)
                    continue
                # 4.2a: снаряду подталкивание на углах ВЫКЛЮЧЕНО —
                # прямая линия входит в смысл снаряда
                nx, ny, hit = physics.move_circle(grid, e.x, e.y,
                                                  e.vx * dt, e.vy * dt, e.r,
                                                  e.kind != K_SHOT)
                e.x = nx
                e.y = ny
                if hit:
                    if e.kind == K_SHOT:
                        if gone is None:      # снаряд гибнет о стену (4.2a)
                            gone = []
                        gone.append(e.id)
                        # точка гибели уходит событием: клиенту — искра о
                        # стену, проверке — число, которое иначе не увидеть
                        # (сущности к концу тика уже нет)
                        self.event("boom", a=e.owner, b=e.id,
                                   x=proto.r3(e.x), y=proto.r3(e.y))
                        continue
                    if hit & 1:
                        e.vx = 0.0
                    if hit & 2:
                        e.vy = 0.0
        if gone:
            for eid in gone:
                self.remove(eid)
        combat.resolve(self, dt)
        self.fog.update(self.viewers())
        return self.tick

    # --- события (5.2, сообщение ev) ---------------------------------------

    MAX_EVENTS = 64

    def event(self, kind, **kw):
        """Сложить событие; вынимает и рассылает комната, раз в тик.

        ОТКУДА ПОТОЛОК. Список вычерпывается каждый тик, значит потолок — это
        "сколько ev уходит клиенту за тик". Одно событие на проводе ~70 байт,
        64 события = 4.5 КБ в тик = 134 КБ/с сырых поверх снапшота — при
        бюджете 2.3 в 200 КБ/с это влезает только со сжатием, и это уже
        потолок, а не рабочий режим. Реальный худший случай считается так:
        6 игроков бьют раз в 14 тиков плюс 200 врагов этапа 2b с замахом
        0.5 с — это 13 событий в тик, запас к потолку впятеро.
        Вторая работа потолка — предохранитель: мир можно шагать и без
        комнаты (стенд, проверка), тогда вынимать события некому, и без
        потолка список рос бы весь прогон. Терять ev контракт разрешает
        прямым текстом (5.2), состояния на них нет.
        """
        if len(self.events) < self.MAX_EVENTS:
            self.events.append((self.tick, kind, kw))

    # --- смена этажа (8.1) -------------------------------------------------

    def enter_floor(self, floor, grid, spawns, stairs):
        """Новый этаж в том же мире: id не переиспользуются (4.3).

        Мир не пересоздаётся именно поэтому: новый World начал бы нумерацию
        сущностей с единицы, и id стал бы уникален в пределах ЭТАЖА, а не
        забега, как требует 4.3.
        """
        self.floor = floor
        self.grid = grid
        self.spawns = spawns or [(2.5, 2.5)]
        self.stairs = stairs
        if grid.w * grid.h == self.fog.n:
            self.fog.grid = grid
            self.fog.forget_all()     # 4.4: память группы забывается целиком
        else:
            self.fog = vis_mod.Fog(grid)
        i = 0
        for e in list(self.entities.values()):
            if e.kind != K_PLAYER:
                self.remove(e.id)     # враги и снаряды остались этажом выше
                continue
            sx, sy = self.spawns[i % len(self.spawns)]
            i += 1
            e.x, e.y = physics.free_spot(grid, sx, sy, e.r)
            e.vx = e.vy = 0.0
            e.mv = (0.0, 0.0)
            e.btn = 0
            if e.flags & F_DEAD:
                # 8.5: воскресает на следующем этаже бесплатно
                e.flags &= ~F_DEAD
                e.hp = e.hp_max
            combat.reset(e)
        # всем уйдёт полный снапшот, дельте не от чего отсчитываться
        self._sent = {}
        self._removed = []
        return self.floor

    def viewers(self):
        """Кто светит в туман: живые игроки (4.4). Мёртвый не разведчик."""
        out = []
        for e in self.entities.values():
            if e.kind == K_PLAYER and not (e.flags & F_DEAD):
                out.append((e.id, e.x, e.y))
        return out

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
        # входящему нужен весь туман целиком, а не изменения
        return proto.snap_tail(self.tick, True, rows, None,
                               self.fog.full_encoded())

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
        # дельта тумана — одна на комнату; None, если туман не менялся
        return proto.snap_tail(self.tick, False, rows, rm,
                               self.fog.delta_encoded())

    def alive_players(self):
        """Живые игроки, которых ждут на лестнице (8.1).

        Отвалившийся не в счёт: 8.5 превращает его в камень, а камень не
        ходит. Иначе один упавший интернет держал бы группу 120 с.
        """
        out = []
        for e in self.entities.values():
            if e.kind != K_PLAYER:
                continue
            if e.flags & (F_DEAD | F_OFFLINE):
                continue
            out.append(e)
        return out

    def on_stairs(self, e):
        """Стоит ли сущность на клетке лестницы (тайл 2, 4.1)."""
        st = self.stairs
        if st is None:
            return False
        return int(e.x) == st[0] and int(e.y) == st[1]

    def stats(self):
        return {"tick": self.tick, "entities": len(self.entities)}
