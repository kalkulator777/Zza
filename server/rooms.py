# -*- coding: utf-8 -*-
"""Комнаты: лобби, подбор героев, игровой цикл."""

import json
import os
import random
import string
import time

from tornado.ioloop import IOLoop

from .game import const as C
from .game import heroes as H
from .game.arena import ARENA_LIST
from .game.bots import Bot
from .game.world import World

# Замер сквозной задержки: с ZZA_PROBE=1 сервер вкладывает в снапшот два числа —
# сколько ввод пролежал в очереди до тика и сколько тик ждал отправки снапшота.
# Нужно только для tools/latency.py, в обычной игре выключено.
PROBE = os.environ.get("ZZA_PROBE") == "1"

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MODES = {"1v1": 2, "2v2": 4}
BOT_NAMES = ["Болт", "Шестерня", "Ржавый", "Тумблер", "Кувалда", "Искра", "Обрез", "Пружина"]


class Player:
    __slots__ = ("pid", "name", "team", "hero", "ready", "conn", "is_bot",
                 "bot_level", "bot", "held", "pending", "spectator", "last_seen",
                 "probe_t")

    def __init__(self, pid, name, conn=None, is_bot=False, bot_level=2):
        self.pid = pid
        self.name = name
        self.team = 0
        self.hero = "rezak"
        self.ready = False
        self.conn = conn
        self.is_bot = is_bot
        self.bot_level = bot_level
        self.bot = None
        self.held = 0
        self.pending = 0
        self.spectator = False
        self.last_seen = time.time()
        self.probe_t = 0.0

    def info(self):
        return {"pid": self.pid, "name": self.name, "team": self.team,
                "hero": self.hero, "ready": self.ready, "bot": self.is_bot,
                "level": self.bot_level, "spec": self.spectator,
                "online": self.conn is not None or self.is_bot}


class Room:
    def __init__(self, manager, code, name, mode="1v1", arena="plato", stocks=3):
        self.mgr = manager
        self.code = code
        self.name = name
        self.mode = mode if mode in MODES else "1v1"
        self.arena = arena
        self.stocks = stocks
        self.players = {}
        self.order = []
        self.host = None
        self.state = "lobby"
        self.world = None
        self.bots = {}
        self.loop = None
        self._next = 0.0
        self._snap_c = 0
        self._probe_dq = None
        self._probe_t0 = 0.0
        self.chat = []
        self.result = None
        self.created = time.time()

    # ---------------------------------------------------------------- helpers
    @property
    def cap(self):
        return MODES[self.mode]

    def active_players(self):
        return [self.players[p] for p in self.order
                if p in self.players and not self.players[p].spectator]

    def humans(self):
        return [p for p in self.players.values() if not p.is_bot]

    def free_slots(self):
        return max(0, self.cap - len(self.active_players()))

    def brief(self):
        return {
            "code": self.code, "name": self.name, "mode": self.mode,
            "arena": self.arena, "state": self.state,
            "players": len(self.active_players()), "cap": self.cap,
            "humans": len([p for p in self.active_players() if not p.is_bot]),
            "spectators": len([p for p in self.players.values() if p.spectator]),
        }

    def lobby_state(self):
        return {
            "t": "room",
            "code": self.code, "name": self.name, "mode": self.mode,
            "arena": self.arena, "stocks": self.stocks, "state": self.state,
            "host": self.host, "cap": self.cap,
            "players": [self.players[p].info() for p in self.order if p in self.players],
            "chat": self.chat[-40:],
            "result": self.result,
        }

    # ---------------------------------------------------------------- сеть
    def send(self, pid, msg):
        p = self.players.get(pid)
        if p and p.conn:
            p.conn.send(msg)

    def broadcast(self, msg, skip=None):
        # один json.dumps на всех, а не на каждого: снапшот уходит 30-60 раз
        # в секунду сразу четверым, и сериализовать его четырежды незачем
        raw = json.dumps(msg, ensure_ascii=False)
        for p in list(self.players.values()):
            if p.conn and p.pid != skip:
                p.conn.send_raw(raw)

    def push_lobby(self):
        self.broadcast(self.lobby_state())

    def say(self, author, text):
        text = str(text)[:220]
        self.chat.append({"n": author, "m": text, "ts": round(time.time(), 1)})
        self.chat = self.chat[-60:]
        self.broadcast({"t": "chat", "n": author, "m": text})

    # ------------------------------------------------------------ участники
    def add(self, pid, name, conn, spectator=False):
        p = self.players.get(pid)
        if p is None:
            # слот и команду считаем ДО вставки в список, иначе игрок учитывает сам себя
            as_spec = bool(spectator) or self.free_slots() <= 0 or self.state == "match"
            team = self._auto_team()
            p = Player(pid, name, conn)
            p.spectator = as_spec
            if not as_spec:
                p.team = team
            self.players[pid] = p
            self.order.append(pid)
            self.say("", f"{name} — {'смотрит' if as_spec else 'в комнате'}")
        else:
            p.conn = conn
            p.name = name
        if self.host is None or self.host not in self.players:
            self.host = pid
        self._balance_hero(p)
        self.push_lobby()
        if self.state == "match" and self.world is not None:
            self.send(pid, self.match_start_msg())
        return p

    def _auto_team(self, exclude=None):
        c = [0, 0]
        for p in self.active_players():
            if exclude is not None and p.pid == exclude:
                continue
            c[p.team] += 1
        half = self.cap // 2
        # встаём в ту команду, где меньше народу — иначе все сваливаются в одну
        if c[0] <= c[1] and c[0] < half:
            return 0
        if c[1] < half:
            return 1
        if c[0] < half:
            return 0
        return 0 if c[0] <= c[1] else 1

    def _balance_hero(self, p):
        used = {q.hero for q in self.players.values() if q is not p and not q.spectator}
        if p.hero in used:
            for h in H.HERO_ORDER:
                if h not in used:
                    p.hero = h
                    return

    def remove(self, pid):
        p = self.players.pop(pid, None)
        if p is None:
            return
        if pid in self.order:
            self.order.remove(pid)
        self.say("", f"{p.name} вышел")
        if self.state == "match" and self.world and pid in self.world.fighters:
            # оставляем бойца под управлением бота, чтобы матч не сломался
            self.bots[pid] = Bot(pid, 2)
            self.world.fighters[pid].is_bot = True
            self.players[pid] = Player(pid, p.name + " (бот)", None, is_bot=True)
            self.players[pid].team = p.team
            self.players[pid].hero = p.hero
            self.order.append(pid)
        if self.host == pid:
            hs = [q.pid for q in self.players.values() if not q.is_bot and q.conn]
            self.host = hs[0] if hs else None
        if not self.humans() or all(q.conn is None for q in self.humans()):
            self.close()
        else:
            self.push_lobby()

    def add_bot(self, level=2):
        if self.free_slots() <= 0:
            return
        pid = self.mgr.next_pid()
        used = {p.name for p in self.players.values()}
        name = next((n for n in BOT_NAMES if n not in used), "Бот")
        p = Player(pid, name, None, is_bot=True, bot_level=level)
        p.team = self._auto_team()
        p.ready = True
        self.players[pid] = p
        self.order.append(pid)
        self._balance_hero(p)
        self.push_lobby()

    def kick(self, pid):
        p = self.players.get(pid)
        if not p:
            return
        if p.conn:
            p.conn.send({"t": "kicked"})
            p.conn.room = None
        self.remove(pid)

    def close(self):
        self.stop_loop()
        for p in list(self.players.values()):
            if p.conn:
                p.conn.room = None
                p.conn.send({"t": "room_closed"})
        self.mgr.rooms.pop(self.code, None)

    # -------------------------------------------------------------- лобби
    def set_hero(self, pid, hero):
        p = self.players.get(pid)
        if not p or hero not in H.HEROES or self.state == "match":
            return
        for q in self.players.values():
            if q.pid != pid and not q.spectator and q.hero == hero:
                return  # герой занят
        p.hero = hero
        self.push_lobby()

    def set_team(self, pid, team):
        p = self.players.get(pid)
        if not p or self.state == "match":
            return
        team = int(team) & 1
        if len([q for q in self.active_players() if q.team == team and q.pid != pid]) >= self.cap // 2:
            return
        p.team = team
        self.push_lobby()

    def set_spectator(self, pid, spec):
        p = self.players.get(pid)
        if not p or self.state == "match":
            return
        if not spec and self.free_slots() <= 0:
            return
        p.spectator = bool(spec)
        p.ready = False
        if not spec:
            p.team = self._auto_team(exclude=pid)
            self._balance_hero(p)
        self.push_lobby()

    def set_ready(self, pid, v):
        p = self.players.get(pid)
        if p and not p.spectator:
            p.ready = bool(v)
            self.push_lobby()

    def configure(self, pid, mode=None, arena=None, stocks=None, name=None):
        if pid != self.host or self.state == "match":
            return
        if mode in MODES:
            self.mode = mode
            while len(self.active_players()) > self.cap:
                extra = [p for p in self.active_players() if p.is_bot]
                if extra:
                    self.remove(extra[-1].pid)
                else:
                    self.active_players()[-1].spectator = True
            for p in self.active_players():
                p.team = 0
            c = [0, 0]
            half = self.cap // 2
            for p in self.active_players():
                t = 0 if c[0] < half else 1
                p.team = t
                c[t] += 1
        if arena:
            self.arena = arena
        if stocks:
            self.stocks = max(1, min(9, int(stocks)))
        if name:
            self.name = str(name)[:28]
        self.push_lobby()

    # -------------------------------------------------------------- матч
    def can_start(self):
        act = self.active_players()
        if len(act) < 2:
            return False, "Нужно минимум двое (можно добавить бота)"
        t0 = [p for p in act if p.team == 0]
        t1 = [p for p in act if p.team == 1]
        if not t0 or not t1:
            return False, "В каждой команде должен быть хотя бы один боец"
        if any(not p.ready for p in act if not p.is_bot):
            return False, "Не все готовы"
        return True, ""

    def start(self, pid):
        if pid != self.host or self.state == "match":
            return
        ok, why = self.can_start()
        if not ok:
            self.send(pid, {"t": "error", "m": why})
            return
        plist = [{"pid": p.pid, "name": p.name, "team": p.team,
                  "hero": p.hero, "bot": p.is_bot} for p in self.active_players()]
        self.world = World(self.arena, plist, stocks=self.stocks)
        self.bots = {p.pid: Bot(p.pid, p.bot_level) for p in self.active_players() if p.is_bot}
        self.state = "match"
        self.result = None
        self.broadcast(self.match_start_msg())
        self.push_lobby()
        self.start_loop()

    def match_start_msg(self):
        return {
            "t": "match_start",
            "arena": {k: self.world.arena[k] for k in
                      ("id", "name", "platforms", "blast", "bg", "accent")},
            "stocks": self.stocks,
            "players": [{"pid": f.pid, "name": f.name, "team": f.team,
                         "hero": f.hero.id, "bot": f.is_bot}
                        for f in self.world.fighters.values()],
        }

    def start_loop(self):
        self.stop_loop()
        self._next = IOLoop.current().time() + C.DT
        self.loop = IOLoop.current().add_timeout(self._next, self._tick)

    def stop_loop(self):
        if self.loop is not None:
            try:
                IOLoop.current().remove_timeout(self.loop)
            except Exception:
                pass
            self.loop = None

    def _tick(self):
        """Тик по абсолютной сетке времени.

        Раньше здесь был будильник на 0.85*DT с накопителем: он просыпался
        чаще, чем нужно, и делал шаг, только когда накопитель дорастал до DT.
        Сетка от этого плыла — соседние тики отстояли то на 14, то на 28 мс,
        и снапшоты уходили рывками (медиана 31 мс, p90 46 мс на лупбэке).
        Клиенту приходилось держать буфер под худший разрыв. Теперь у каждого
        тика есть свой момент в будущем, и мы просто не даём ему уехать.
        """
        if self.state != "match" or self.world is None:
            self.loop = None
            return
        io = IOLoop.current()
        now = io.time()
        steps = 0
        while now >= self._next and steps < 6:
            self._next += C.DT
            steps += 1
            self._step_once()
            if self.state != "match":
                self.loop = None
                return
        if now >= self._next:
            # отстали безнадёжно (машина висела) — не догоняем, переставляем сетку
            self._next = now + C.DT
        self.loop = io.add_timeout(self._next, self._tick)

    def _step_once(self):
        w = self.world
        t_step = time.perf_counter() if PROBE else 0.0
        for pid, p in self.players.items():
            f = w.fighters.get(pid)
            if f is None:
                continue
            if p.is_bot:
                b = self.bots.get(pid)
                if b:
                    b.think(w)
            else:
                f.inp = p.held | p.pending
                p.pending = 0
                if PROBE and p.probe_t:
                    self._probe_dq = (t_step - p.probe_t) * 1000.0
                    self._probe_t0 = t_step
                    p.probe_t = 0.0
        w.step()
        self._snap_c += 1
        if self._snap_c >= C.SNAP_EVERY or w.events:
            self._snap_c = 0
            snap = w.snapshot()
            if PROBE and self._probe_dq is not None:
                snap["_dq"] = round(self._probe_dq, 3)
                snap["_ds"] = round((time.perf_counter() - self._probe_t0) * 1000.0, 3)
                self._probe_dq = None
            self.broadcast(snap)
        if w.state == C.ST_OVER and w.state_t <= 0:
            self.finish()

    def finish(self):
        w = self.world
        self.result = {
            "winner": w.winner,
            "board": w.scoreboard(),
            "time": round(w.time, 1),
            "arena": w.arena["name"],
        }
        self.state = "lobby"
        self.stop_loop()
        self.world = None
        self.bots = {}
        for p in self.players.values():
            if not p.is_bot:
                p.ready = False
        self.broadcast({"t": "match_end", "result": self.result})
        self.push_lobby()

    def abort(self, pid):
        if pid != self.host or self.state != "match":
            return
        self.state = "lobby"
        self.stop_loop()
        self.world = None
        self.bots = {}
        for p in self.players.values():
            if not p.is_bot:
                p.ready = False
        self.broadcast({"t": "match_abort"})
        self.push_lobby()

    def input(self, pid, held, pressed, ax, ay):
        p = self.players.get(pid)
        if p is None:
            return
        k = int(held) & 0x3FF
        if PROBE and k != p.held and not p.probe_t:
            p.probe_t = time.perf_counter()
        p.held = k
        p.pending |= int(pressed) & 0x3FF
        p.last_seen = time.time()
        if self.world:
            f = self.world.fighters.get(pid)
            if f is not None:
                f.aim_x = max(-1.5, min(1.5, float(ax)))
                f.aim_y = max(-1.5, min(1.5, float(ay)))


class RoomManager:
    def __init__(self):
        self.rooms = {}
        self._pid = 0

    def next_pid(self):
        self._pid += 1
        return self._pid

    def new_code(self):
        while True:
            c = "".join(random.choice(CODE_ALPHABET) for _ in range(4))
            if c not in self.rooms:
                return c

    def create(self, name, mode="1v1", arena="plato", stocks=3):
        code = self.new_code()
        r = Room(self, code, name or f"Игра {code}", mode, arena, stocks)
        self.rooms[code] = r
        return r

    def get(self, code):
        return self.rooms.get((code or "").upper().strip())

    def list_open(self):
        return [r.brief() for r in self.rooms.values() if r.humans()]

    def catalog(self):
        return {"heroes": H.catalog(), "arenas": ARENA_LIST,
                "modes": [{"id": k, "cap": v} for k, v in MODES.items()]}
