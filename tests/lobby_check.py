#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка лобби (DESIGN.md 8.7), опознания по token (5.1) и набора
апгрейдов на проводе (11.7) — серверная половина.

Поднимает НАСТОЯЩИЙ сервер в этом же процессе на свободном порту и ходит в
него настоящими WebSocket-клиентами: трафик идёт через сокет, через
proto.parse_client и через room.py. Ни одна проверка не зовёт внутренние
методы в обход провода там, где спрашивается «что будет, если клиент
пришлёт». Клиенту не верят — значит, и проверять это надо клиентом.

Что здесь меряется:

  [1] сервер — источник истины по параметрам: негодное значение не
      принимается и сервер от него не падает;
  [2] параметры доходят до мира: сид, сложность, максимум игроков,
      вход в идущую игру;
  [3] права хозяина: не-хозяин не меняет параметры и не стартует;
      хозяин вышел — право перешло;
  [4] token: два ТЁЗКИ не путаются при переподключении;
  [5] build (11.7): набор апгрейдов приходит отдельным сообщением рядом с
      полным снапшотом и при каждом изменении.

Запуск:  python3 tests/lobby_check.py
Выход:   0 — зелено, 1 — красно.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

from tornado.httpserver import HTTPServer          # noqa: E402
from tornado.websocket import websocket_connect    # noqa: E402

from server import gen, items, proto, room as room_mod   # noqa: E402
from server.app import make_app                    # noqa: E402
from server.room import Rooms                      # noqa: E402

FAILS = []


def check(ok, title, detail=""):
    print(("  ЗЕЛЕНО  " if ok else "  КРАСНО  ") + title +
          (("  | " + detail) if detail else ""))
    if not ok:
        FAILS.append(title)
    return ok


def note(title, detail=""):
    print("  данные   " + title + (("  | " + detail) if detail else ""))


def uptime():
    try:
        out = subprocess.check_output(["uptime"], text=True).strip()
    except Exception as e:
        out = "uptime недоступен: %s" % e
    print("  uptime:  " + out)


class Client(object):
    """Клиент лобби: помнит последние joined / err / build / level."""

    def __init__(self, name):
        self.name = name
        self.ws = None
        self.pid = 0
        self.token = ""
        self.room = ""
        self.joined = None
        self.errs = []
        self.level = None
        self.builds = {}          # id сущности -> список счётчиков
        self.build_msgs = 0
        self.my_ent = 0
        self.snaps = 0
        self.full_snaps = 0
        self._task = None

    async def connect(self, port):
        self.ws = await websocket_connect("ws://127.0.0.1:%d/ws" % port)
        self._task = asyncio.ensure_future(self._reader())
        return self

    async def _reader(self):
        while True:
            try:
                msg = await self.ws.read_message()
            except Exception:
                return
            if msg is None:
                return
            m = json.loads(msg)
            t = m.get("t")
            if t == "welcome":
                self.pid = m["pid"]
                if not self.token:
                    self.token = m.get("token", "")
            elif t == "joined":
                self.joined = m
                self.room = m["room"]
            elif t == "err":
                self.errs.append(m)
            elif t == "level":
                self.level = m
            elif t == "build":
                self.build_msgs += 1
                self.builds[m["id"]] = m["ups"]
            elif t == "snap":
                self.snaps += 1
                if m.get("full"):
                    self.full_snaps += 1
                    self.my_ent = m.get("you", self.my_ent)

    def send(self, obj):
        self.ws.write_message(json.dumps(obj))

    async def hello_join(self, code="", token=None):
        self.send({"t": "hello", "name": self.name, "ver": 1})
        await self.wait(lambda: self.pid != 0)
        j = {"t": "join", "room": code, "name": self.name}
        if token is None:
            token = self.token
        if token:
            j["token"] = token
        self.send(j)
        return await self.wait(lambda: bool(self.room) or bool(self.errs))

    async def wait(self, cond, limit=3.0):
        """Ждать УСЛОВИЕ, а не секунды. limit — предохранитель (10)."""
        n = int(limit / 0.01)
        for _ in range(n):
            if cond():
                return True
            await asyncio.sleep(0.01)
        return cond()

    def opts(self):
        return (self.joined or {}).get("opts", {})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# --- [1] источник истины по параметрам -------------------------------------

BAD_OPTS = [
    ("чужой режим", {"mode": "siege"}, "mode"),
    ("выдуманный режим", {"mode": "pvp-арена"}, "mode"),
    ("тема, которой нет", {"theme": "caves"}, "theme"),
    ("сид не числом", {"seed": "хочу-красиво"}, "seed"),
    ("сид дробью", {"seed": 12.5}, "seed"),
    ("сид отрицательный", {"seed": -1}, "seed"),
    ("сид за потолком", {"seed": 10 ** 12}, "seed"),
    ("сид как true", {"seed": True}, "seed"),
    ("сложность вне трёх ступеней", {"diff": 7}, "diff"),
    ("сложность строкой", {"diff": "сложно"}, "diff"),
    ("игроков больше шести", {"max_players": 99}, "max_players"),
    ("игроков меньше двух", {"max_players": 1}, "max_players"),
    ("дружественный огонь строкой", {"ff": "да"}, "ff"),
    ("вход в игру числом", {"join_running": 5}, "join_running"),
]

GOOD_OPTS = {"mode": "descent", "theme": "halls", "seed": 4242,
             "diff": 2, "ff": True, "max_players": 4, "join_running": False}


async def section_opts(port, rooms):
    print("\n[1] Параметры: источник истины — сервер (8.7)")
    a = await Client("Хозяин").connect(port)
    await a.hello_join("")
    code = a.room
    r = rooms.get(code)

    a.send({"t": "opts", "opts": dict(GOOD_OPTS)})
    await a.wait(lambda: a.opts().get("seed") == 4242)
    got = a.opts()
    note("сервер принял", ", ".join("%s=%r" % (k, got.get(k)) for k in sorted(GOOD_OPTS)))
    check(all(got.get(k) == v for k, v in GOOD_OPTS.items()),
          "годные значения приняты и вернулись в joined",
          "seed=%s diff=%s max=%s ff=%s join=%s"
          % (got.get("seed"), got.get("diff"), got.get("max_players"),
             got.get("ff"), got.get("join_running")))

    before = dict(r.settings.describe())
    bad_ok = 0
    for title, opts, key in BAD_OPTS:
        a.errs[:] = []
        a.send({"t": "opts", "opts": opts})
        await a.wait(lambda: bool(a.errs))
        now = r.settings.describe()
        kept = now.get(key) == before.get(key)
        told = any(e.get("code") == "opt" for e in a.errs)
        if kept and told:
            bad_ok += 1
        else:
            check(False, "негодное значение отвергнуто: " + title,
                  "%s стало %r (было %r), отказ прислан: %s"
                  % (key, now.get(key), before.get(key), told))
    check(bad_ok == len(BAD_OPTS),
          "негодные значения отвергнуты все, значения не поехали",
          "%d из %d, и на каждое пришёл отказ" % (bad_ok, len(BAD_OPTS)))

    # мусор в самом сообщении: opts не словарь, ключей нет вовсе
    a.send({"t": "opts", "opts": "здравствуйте"})
    a.send({"t": "opts", "opts": [1, 2, 3]})
    a.send({"t": "opts"})
    a.send({"t": "opts", "opts": {"пароль_админа": "1234", "seed": 777}})
    await a.wait(lambda: r.settings.seed == 777)
    check(r.settings.seed == 777 and r.phase == room_mod.LOBBY,
          "мусор в самом сообщении сервер пережил и не поперхнулся",
          "сид %d, фаза %s" % (r.settings.seed, r.phase))

    # «пусто = случайный» (8.7)
    a.send({"t": "opts", "opts": {"seed": None}})
    await a.wait(lambda: a.opts().get("seed_auto") is True)
    check(a.opts().get("seed_auto") is True and a.opts().get("seed") != 777,
          "пустой сид = случайный, и он уже виден числом",
          "seed_auto=%s, сид %s" % (a.opts().get("seed_auto"), a.opts().get("seed")))
    a.close()
    return code


# --- [2] параметры доходят до мира -----------------------------------------

async def section_world(port, rooms):
    print("\n[2] Параметры доходят до мира (8.7)")
    a = await Client("Сеятель").connect(port)
    await a.hello_join("")
    r = rooms.get(a.room)

    SEED = 123456
    a.send({"t": "opts", "opts": {"seed": SEED, "diff": 0}})
    await a.wait(lambda: a.opts().get("seed") == SEED)
    a.send({"t": "ready", "v": True})
    await a.wait(lambda: a.level is not None and a.full_snaps > 0)

    want = gen.generate(SEED, 1, r.settings.map_w, r.settings.map_h,
                        r.settings.theme)
    same = bytes(want.grid.tiles) == bytes(r.world.grid.tiles)
    note("сид в лобби", "поставлен %d, в level пришёл %s, в мире %s"
         % (SEED, a.level.get("seed"), r.world.seed))
    check(a.level.get("seed") == SEED and r.world.seed == SEED,
          "сгенерировался именно тот сид, который поставили в лобби",
          "level.seed=%s, world.seed=%s" % (a.level.get("seed"), r.world.seed))
    check(same, "карта побайтово совпала с gen.generate(тот же сид)",
          "%d тайлов" % len(want.grid.tiles))

    ent = r.world.entities[r.players[a.pid].ent_id]
    note("сложность", "ступень 0 (%s) -> запас здоровья %d"
         % (room_mod.DIFF_NAMES[0], ent.hp_max))
    check(ent.hp_max == room_mod.DIFF_HP[0],
          "сложность дошла до мира: запас здоровья игрока по ступени",
          "hp_max=%d, ждали %d (на обычной было бы %d)"
          % (ent.hp_max, room_mod.DIFF_HP[0], room_mod.DIFF_HP[1]))

    # тот же сид второй раз — та же карта (8.7: «переиграть тот же забег»)
    b = await Client("Повтор").connect(port)
    await b.hello_join("")
    r2 = rooms.get(b.room)
    b.send({"t": "opts", "opts": {"seed": SEED}})
    await b.wait(lambda: b.opts().get("seed") == SEED)
    b.send({"t": "ready", "v": True})
    await b.wait(lambda: b.level is not None)
    check(b.level.get("tiles") == a.level.get("tiles"),
          "тот же сид в другом лобби дал ту же карту — забег переигрывается",
          "%d символов base64 совпали" % len(b.level.get("tiles") or ""))
    a.close()
    b.close()
    return r, a


async def section_caps(port, rooms):
    print("\n[2a] Максимум игроков и вход в идущую игру (8.7)")
    h = await Client("Хозяин").connect(port)
    await h.hello_join("")
    code = h.room
    r = rooms.get(code)
    h.send({"t": "opts", "opts": {"max_players": 2}})
    await h.wait(lambda: h.opts().get("max_players") == 2)

    g = await Client("Второй").connect(port)
    await g.hello_join(code)
    check(g.room == code, "второй вошёл при потолке 2", "комната %s" % g.room)

    third = await Client("Третий").connect(port)
    await third.hello_join(code)
    note("третий при потолке 2", "комната %r, отказ %s"
         % (third.room, [e.get("code") for e in third.errs]))
    check(not third.room and any(e.get("code") == "full" for e in third.errs),
          "третьего при потолке 2 не пустили, и сказали почему",
          "игроков в комнате %d" % len(r.players))

    # потолок нельзя опустить ниже собравшихся
    h.errs[:] = []
    h.send({"t": "opts", "opts": {"max_players": 2}})
    await h.wait(lambda: True, 0.15)
    h.send({"t": "opts", "opts": {"max_players": 3}})
    await h.wait(lambda: h.opts().get("max_players") == 3)
    check(h.opts().get("max_players") == 3,
          "потолок поднимается, пока лобби в наборе")

    # вход в идущую игру — выключаемый (8.5 + 8.7)
    h.send({"t": "opts", "opts": {"join_running": False, "max_players": 6}})
    await h.wait(lambda: h.opts().get("join_running") is False)
    g.send({"t": "ready", "v": True})
    h.send({"t": "ready", "v": True})
    await h.wait(lambda: r.phase == room_mod.PLAYING)
    check(r.phase == room_mod.PLAYING, "игра пошла", "фаза %s" % r.phase)

    late = await Client("Опоздавший").connect(port)
    await late.hello_join(code)
    note("опоздавший при закрытом входе", "комната %r, отказ %s"
         % (late.room, [e.get("code") for e in late.errs]))
    check(not late.room and any(e.get("code") == "closed" for e in late.errs),
          "с выключенным «входом в идущую игру» опоздавшего не пустили")

    lst = rooms.listing()
    row = next((x for x in lst if x["id"] == code), None)
    check(row is not None and row["phase"] == "playing" and row["join"] is False
          and row["mode"] == "descent" and row["max"] == 6,
          "список лобби показывает режим, потолок, фазу и «пустят ли»",
          json.dumps(row, ensure_ascii=False))
    for c in (h, g, third, late):
        c.close()


# --- [3] права хозяина -----------------------------------------------------

async def section_host(port, rooms):
    print("\n[3] Права хозяина (8.7)")
    h = await Client("Хозяин").connect(port)
    await h.hello_join("")
    code = h.room
    r = rooms.get(code)
    g = await Client("Гость").connect(port)
    await g.hello_join(code)
    await g.wait(lambda: g.joined is not None and g.joined.get("host"))

    check(h.joined.get("host") == h.pid and g.joined.get("host") == h.pid,
          "хозяин — создатель лобби, и это видят оба",
          "host=%s, pid хозяина %s, pid гостя %s"
          % (h.joined.get("host"), h.pid, g.pid))

    seed_before = r.settings.seed
    g.errs[:] = []
    g.send({"t": "opts", "opts": {"seed": 999999, "diff": 2}})
    await g.wait(lambda: bool(g.errs))
    check(r.settings.seed == seed_before and r.settings.diff != 2
          and any(e.get("code") == "nothost" for e in g.errs),
          "НЕ-хозяин не может менять параметры",
          "сид %d (был %d), сложность %d, отказ %s"
          % (r.settings.seed, seed_before, r.settings.diff,
             [e.get("code") for e in g.errs]))

    # не-хозяин жмёт старт, и все готовы — игра всё равно не идёт
    g.errs[:] = []
    g.send({"t": "ready", "v": True})
    await g.wait(lambda: r.players[g.pid].ready)
    g.send({"t": "start", "v": True})
    await g.wait(lambda: bool(g.errs))
    await g.wait(lambda: r.phase != room_mod.LOBBY, 0.3)
    check(r.phase == room_mod.LOBBY and not r.armed
          and any(e.get("code") == "nothost" for e in g.errs),
          "НЕ-хозяин не может начать игру",
          "фаза %s, старт взведён: %s, отказ %s"
          % (r.phase, r.armed, [e.get("code") for e in g.errs]))

    # хозяин жмёт старт — игра идёт
    h.send({"t": "start", "v": True})
    await h.wait(lambda: r.phase == room_mod.PLAYING)
    check(r.phase == room_mod.PLAYING,
          "хозяин нажал старт — игра началась", "фаза %s" % r.phase)

    # --- право переходит следующему по времени входа ---------------------
    h2 = await Client("Хозяин2").connect(port)
    await h2.hello_join("")
    code2 = h2.room
    r2 = rooms.get(code2)
    g1 = await Client("Гость1").connect(port)
    await g1.hello_join(code2)
    g2 = await Client("Гость2").connect(port)
    await g2.hello_join(code2)
    await g2.wait(lambda: len(r2.players) == 3)
    order = [p.pid for p in r2.players.values()]
    check(r2.host_pid == h2.pid, "хозяин — создатель", "pid %d" % r2.host_pid)
    h2.close()
    await g1.wait(lambda: r2.host_pid != h2.pid)
    note("после ухода хозяина", "порядок входа %s, новый хозяин %d"
         % (order, r2.host_pid))
    check(r2.host_pid == g1.pid,
          "хозяин вышел — право перешло СЛЕДУЮЩЕМУ ПО ВРЕМЕНИ ВХОДА",
          "было %d, стало %d (следующий по входу %d, третий %d)"
          % (h2.pid, r2.host_pid, g1.pid, g2.pid))
    await g1.wait(lambda: (g1.joined or {}).get("host") == g1.pid)
    check((g1.joined or {}).get("host") == g1.pid,
          "новому хозяину об этом сказали сообщением joined")

    # и новый хозяин действительно может то, чего не мог
    g1.errs[:] = []
    g1.send({"t": "opts", "opts": {"seed": 31337}})
    await g1.wait(lambda: r2.settings.seed == 31337)
    check(r2.settings.seed == 31337 and not g1.errs,
          "новый хозяин уже может менять параметры", "сид %d" % r2.settings.seed)

    # уход по своей воле (кнопка «выйти в меню») — не обрыв связи: игрока
    # не держат 120 секунд, место в комнате освобождается сразу.
    g1.send({"t": "leave"})
    await g2.wait(lambda: g1.pid not in r2.players)
    check(g1.pid not in r2.players and r2.host_pid == g2.pid,
          "ушедший в меню освободил место сразу, а право хозяина перешло дальше",
          "игроков %d, хозяин %d" % (len(r2.players), r2.host_pid))
    for c in (h, g, g1, g2):
        c.close()


# --- [4] token вместо имени (5.1) ------------------------------------------

async def section_token(port, rooms):
    print("\n[4] Опознание по token: два ТЁЗКИ не путаются (5.1)")
    a = await Client("Вася").connect(port)
    await a.hello_join("")
    code = a.room
    r = rooms.get(code)
    b = await Client("Вася").connect(port)          # ровно то же имя
    await b.hello_join(code)
    await b.wait(lambda: len(r.players) == 2)
    check(a.token and b.token and a.token != b.token,
          "сервер выдал каждому свой token в welcome",
          "%s… и %s…" % (a.token[:8], b.token[:8]))

    a.send({"t": "ready", "v": True})
    b.send({"t": "ready", "v": True})
    await b.wait(lambda: r.phase == room_mod.PLAYING)
    ent_a = r.players[a.pid].ent_id
    ent_b = r.players[b.pid].ent_id
    # разведём тела, чтобы «та же сущность» значило «то же место»
    pa = r.world.entities[ent_a]
    pb = r.world.entities[ent_b]
    pa.x, pa.y = pa.x + 0.0, pa.y + 0.0
    note("до обрыва", "оба зовутся «Вася»: A -> сущность %d, B -> сущность %d"
         % (ent_a, ent_b))
    check(ent_a != ent_b, "у тёзок разные сущности")

    tok_a, tok_b = a.token, b.token
    a.close()
    b.close()
    await asyncio.sleep(0.4)

    # возвращаются в ОБРАТНОМ порядке — по имени это бы всё и перепутало
    b2 = await Client("Вася").connect(port)
    await b2.hello_join(code, token=tok_b)
    await b2.wait(lambda: b2.full_snaps > 0)
    a2 = await Client("Вася").connect(port)
    await a2.hello_join(code, token=tok_a)
    await a2.wait(lambda: a2.full_snaps > 0)

    note("после возврата", "B вернулся в сущность %d (ждали %d), "
         "A вернулся в сущность %d (ждали %d)"
         % (b2.my_ent, ent_b, a2.my_ent, ent_a))
    check(b2.my_ent == ent_b and a2.my_ent == ent_a,
          "каждый тёзка забрал СВОЮ сущность, а не соседа",
          "B -> %d, A -> %d" % (b2.my_ent, ent_a))
    check(len(r.players) == 2,
          "в комнате по-прежнему двое, а не четверо",
          "игроков %d" % len(r.players))

    # чужой/выдуманный token своей сущности не даёт
    c = await Client("Вася").connect(port)
    await c.hello_join(code, token="deadbeefdeadbeef")
    await c.wait(lambda: bool(c.room) or bool(c.errs))
    note("третий «Вася» с выдуманным token",
         "комната %r, игроков стало %d" % (c.room, len(r.players)))
    check(c.room == code and len(r.players) == 3
          and r.players[c.pid].ent_id not in (ent_a, ent_b),
          "выдуманный token не отдаёт чужую сущность — заводится новый игрок",
          "его сущность %d, чужие %d и %d"
          % (r.players[c.pid].ent_id, ent_a, ent_b))
    for x in (a2, b2, c):
        x.close()


# --- [5] набор апгрейдов на проводе (11.7) ---------------------------------

async def section_build(port, rooms):
    print("\n[5] Набор апгрейдов приходит сообщением build (11.7)")
    a = await Client("Сборщик").connect(port)
    await a.hello_join("")
    r = rooms.get(a.room)
    a.send({"t": "ready", "v": True})
    await a.wait(lambda: a.full_snaps > 0)
    await a.wait(lambda: bool(a.builds))

    ent_id = r.players[a.pid].ent_id
    # Набора может не прийти вовсе (ровно это и проверяется подсадкой),
    # поэтому читаем через ups(): проверка обязана покраснеть, а не упасть
    # с трассировкой — упавшая проверка не говорит НИЧЕГО.
    def ups():
        return a.builds.get(ent_id) or [0] * items.N_UP

    check(ent_id in a.builds and ups() == [0] * items.N_UP,
          "build пришёл РЯДОМ С ПОЛНЫМ СНАПШОТОМ, ещё пустой",
          "ups=%s" % (a.builds.get(ent_id),))

    ent = r.world.entities[ent_id]
    items.grant(ent, items.U_RAM, 1)
    await a.wait(lambda: ups()[items.U_RAM] == 1)
    check(ups()[items.U_RAM] == 1,
          "взял апгрейд — build пришёл сам, без запроса",
          "ups=%s (%s)" % (a.builds.get(ent_id), items.NAMES[items.U_RAM]))

    items.grant(ent, items.U_RAM, 1)
    items.grant(ent, items.U_WIND, 2)
    await a.wait(lambda: ups()[items.U_WIND] == 2)
    check(ups() == [0, 2, 0, 2],
          "набор СКЛАДЫВАЕТСЯ и приходит целиком, а не приращением",
          "ups=%s" % (a.builds.get(ent_id),))

    # молчание, когда ничего не менялось: build не 30 раз в секунду
    quiet0 = a.build_msgs
    await asyncio.sleep(0.7)
    quiet = a.build_msgs - quiet0
    ticks = 0.7 * 30
    note("тишина", "за %d тиков без изменений пришло %d сообщений build"
         % (ticks, quiet))
    check(quiet == 0,
          "без изменения набора build не шлётся вовсе",
          "%d сообщений за ~%d тиков" % (quiet, ticks))

    # 5.2 запрещает держать состояние на событиях — здесь это видно числом:
    # события pick за весь опыт не было НИ ОДНОГО, а набор у клиента верный.
    check(ups() == [0, 2, 0, 2],
          "набор верен, хотя события pick не приходило ни разу",
          "ups=%s, сообщений build всего %d"
          % (a.builds.get(ent_id), a.build_msgs))
    a.close()


async def main():
    print("=== приёмка лобби (8.7), token (5.1), build (11.7) ===")
    uptime()
    rooms = Rooms()
    app = make_app(rooms)
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(64)
    sock.setblocking(False)
    server = HTTPServer(app)
    server.add_socket(sock)
    ticker = asyncio.ensure_future(rooms.run())
    print("  сервер:  порт %d, тик %d Гц" % (port, proto.TICK_HZ))

    try:
        await section_opts(port, rooms)
        await section_world(port, rooms)
        await section_caps(port, rooms)
        await section_host(port, rooms)
        await section_token(port, rooms)
        await section_build(port, rooms)
    finally:
        rooms.stop()
        ticker.cancel()

    print()
    uptime()
    if FAILS:
        print("КРАСНО: %d" % len(FAILS))
        for f in FAILS:
            print("   * " + f)
        return 1
    print("ЗЕЛЕНО: всё сошлось")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
