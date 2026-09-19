#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка пингов на карте (DESIGN.md 8.8) — серверная половина.

Поднимает НАСТОЯЩИЙ сервер в этом же процессе на свободном порту и ходит в
него настоящими WebSocket-клиентами: трафик идёт через сокет, через
proto.parse_client и через room.py. Ни одна проверка не зовёт внутренние
методы в обход провода там, где спрашивается «что будет, если клиент
пришлёт»: клиенту не верят — значит и проверять надо клиентом.

Что здесь меряется:

  [1] пинг доходит ДО ВСЕХ, с точкой, видом и АВТОРОМ;
  [2] биты пинга не доезжают до сущности — мир про пинг не знает;
  [3] спам: клиент, жмущий пинг каждый тик, не заваливает остальных;
  [4] пинг ГАСНЕТ, и пока жив — ПОВТОРЯЕТСЯ (5.2: событие теряется);
  [5] вошедший в идущую партию сразу видит живые пинги;
  [6] клиенту не верят: точка пинга режется по карте;
  [7] пингует и ДУХ (8.5) — больше ему сказать нечем;
  [8] спуск на этаж гасит пинги прошлого этажа.

Запуск:  python3 tests/ping_server.py
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

from server import proto, room as room_mod         # noqa: E402
from server import world as world_mod              # noqa: E402
from server.app import make_app                    # noqa: E402
from server.room import Rooms                      # noqa: E402

FAILS = []

TICK = 1.0 / proto.TICK_HZ


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
    """Клиент, которому из всего протокола интересны пинги."""

    def __init__(self, name):
        self.name = name
        self.ws = None
        self.pid = 0
        self.token = ""
        self.room = ""
        self.my_ent = 0
        self.full_snaps = 0
        self.tick = 0
        self.seq = 0
        self.pings = []           # все ev k=ping, включая повторы
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
                self.room = m["room"]
            elif t == "snap":
                self.tick = m.get("tick", self.tick)
                if m.get("full"):
                    self.full_snaps += 1
                    self.my_ent = m.get("you", self.my_ent)
            elif t == "ev" and m.get("k") == "ping":
                self.pings.append(m)

    # --- уникальные пинги: повтор одного и того же — это НЕ второй пинг ---
    def keys(self):
        return [(p["a"], p["tick"]) for p in self.pings]

    def uniq(self):
        seen = []
        for k in self.keys():
            if k not in seen:
                seen.append(k)
        return seen

    def send(self, obj):
        self.ws.write_message(json.dumps(obj))

    def ping(self, x, y, danger=False):
        """Пинг ровно так, как его шлёт клиент: ввод с битом и прицелом."""
        self.seq += 1
        bit = proto.BTN_PING_DANGER if danger else proto.BTN_PING
        self.send({"t": "input", "seq": self.seq, "mv": [0, 0],
                   "aim": [x, y], "btn": bit})

    def input(self, btn=0, mv=(0, 0), aim=(0, 0)):
        self.seq += 1
        self.send({"t": "input", "seq": self.seq, "mv": list(mv),
                   "aim": list(aim), "btn": btn})

    async def hello_join(self, code=""):
        self.send({"t": "hello", "name": self.name, "ver": 1})
        await self.wait(lambda: self.pid != 0)
        self.send({"t": "join", "room": code, "name": self.name,
                   "token": self.token})
        return await self.wait(lambda: bool(self.room))

    async def wait(self, cond, limit=4.0):
        """Ждать УСЛОВИЕ, а не секунды. limit — предохранитель (правило 10)."""
        for _ in range(int(limit / 0.01)):
            if cond():
                return True
            await asyncio.sleep(0.01)
        return cond()

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


async def start_game(port, rooms, names):
    """Комната из len(names) игроков, все готовы, игра идёт."""
    cs = []
    code = ""
    for n in names:
        c = await Client(n).connect(port)
        await c.hello_join(code)
        code = c.room
        cs.append(c)
    for c in cs:
        c.send({"t": "ready", "v": True})
    for c in cs:
        await c.wait(lambda c=c: c.full_snaps > 0)
    return rooms.get(code), cs


# --- [1] пинг доходит до всех ----------------------------------------------

async def section_see(port, rooms):
    print("\n[1] Пинг виден ВСЕМ, с точкой, видом и автором")
    r, (a, b) = await start_game(port, rooms, ["Аня", "Борис"])
    ent = r.world.entities[r.players[a.pid].ent_id]

    x, y = round(ent.x + 3.0, 3), round(ent.y + 2.0, 3)
    a.ping(x, y)
    await b.wait(lambda: len(b.pings) > 0)
    await a.wait(lambda: len(a.pings) > 0)

    got = b.pings[0] if b.pings else {}
    note("сообщение на проводе", json.dumps(got, ensure_ascii=False))
    check(bool(a.pings), "автор видит свой пинг (эхо сервера, а не догадка клиента)",
          "у автора %d сообщений" % len(a.pings))
    check(got.get("k") == "ping" and got.get("t") == "ev",
          "пинг — это обычное ev (5.2), а не свой вид сообщения",
          "t=%s k=%s" % (got.get("t"), got.get("k")))
    check(abs(got.get("x", -99) - x) < 0.002 and abs(got.get("y", -99) - y) < 0.002,
          "точка пинга дошла до второго игрока",
          "послано (%.3f, %.3f), пришло (%s, %s)"
          % (x, y, got.get("x"), got.get("y")))
    check(got.get("a") == ent.id and got.get("nm") == "Аня",
          "ВИДНО, КТО ПОСТАВИЛ: id сущности и имя автора",
          "a=%s (сущность Ани %d), nm=%r" % (got.get("a"), ent.id, got.get("nm")))
    check(got.get("u") == proto.PING_HERE, "вид пинга — «сюда»",
          "u=%s" % got.get("u"))

    # Второй пинг ищется по ТИКУ ПОСТАНОВКИ, а не «первый пришедший»: живой
    # первый пинг всё это время повторяется (5.2), и его повтор прилетел бы
    # раньше нового. Проверка, которая берёт b.pings[0], меряла бы повтор.
    t1 = got.get("tick")
    x2, y2 = round(ent.x - 4.0, 3), round(ent.y, 3)
    await asyncio.sleep(room_mod.PING_GAP * TICK + 0.05)   # ограничитель
    a.ping(x2, y2, danger=True)
    await b.wait(lambda: any(p["tick"] != t1 for p in b.pings))
    fresh = [p for p in b.pings if p["tick"] != t1]
    got2 = fresh[0] if fresh else {}
    check(got2.get("u") == proto.PING_DANGER,
          "второй вид пинга — «опасность» — отличается полем u",
          "u=%s, точка (%s, %s)" % (got2.get("u"), got2.get("x"), got2.get("y")))
    for c in (a, b):
        c.close()


# --- [2] биты пинга не доезжают до сущности --------------------------------

async def section_not_world(port, rooms):
    print("\n[2] Мир про пинг не знает: биты снимаются в комнате")
    r, (a,) = await start_game(port, rooms, ["Одиночка"])
    ent = r.world.entities[r.players[a.pid].ent_id]

    a.ping(ent.x + 1, ent.y)
    await a.wait(lambda: len(a.pings) > 0)
    await asyncio.sleep(3 * TICK)
    note("btn сущности после пинга", "btn=%d" % ent.btn)
    check(ent.btn & proto.BTN_PING_ANY == 0,
          "бит пинга в сущность НЕ попал (world/combat его не видят)",
          "ent.btn=%d, маска пинга=%d" % (ent.btn, proto.BTN_PING_ANY))

    # Пинг вместе с настоящей кнопкой: чужой бит обязан доехать целым.
    a.seq += 1
    a.send({"t": "input", "seq": a.seq, "mv": [0, 0], "aim": [ent.x + 1, ent.y],
            "btn": proto.BTN_PING | proto.BTN_ATTACK})
    await asyncio.sleep(3 * TICK)
    check(ent.btn & proto.BTN_ATTACK != 0 and ent.btn & proto.BTN_PING_ANY == 0,
          "пинг вместе с ударом: удар доехал, пинг — нет",
          "ent.btn=%d (удар=%d)" % (ent.btn, proto.BTN_ATTACK))
    a.close()


# --- [3] спам ---------------------------------------------------------------

async def section_spam(port, rooms):
    print("\n[3] Спам пингами: клиент жмёт каждый тик")
    r, (a, b) = await start_game(port, rooms, ["Спамер", "Жертва"])
    ent = r.world.entities[r.players[a.pid].ent_id]

    # (а) пачка в один тик. 5.1: сервер применяет ПОСЛЕДНИЙ ввод за тик —
    #     значит 200 сообщений подряд не дают и двух пингов.
    b.pings[:] = []
    made0, drop0 = r.pings_made, r.pings_dropped
    for i in range(200):
        a.ping(ent.x + i * 0.01, ent.y)
    await asyncio.sleep(0.25)
    burst = len(b.uniq())
    note("пачка 200 сообщений в один заход",
         "новых пингов у соседа %d, принято сервером %d, отвергнуто %d"
         % (burst, r.pings_made - made0, r.pings_dropped - drop0))
    check(burst <= 1, "200 сообщений подряд дают не больше одного пинга",
          "новых пингов %d" % burst)

    # (б) честные 30 Гц: клиент шлёт пинг КАЖДЫЙ тик секунду подряд.
    b.pings[:] = []
    made0, drop0 = r.pings_made, r.pings_dropped
    sent = 0
    t_end = asyncio.get_event_loop().time() + 1.0
    while asyncio.get_event_loop().time() < t_end:
        a.ping(ent.x + 2, ent.y)
        sent += 1
        await asyncio.sleep(TICK)
    await asyncio.sleep(0.2)
    made = r.pings_made - made0
    drop = r.pings_dropped - drop0
    uniq = len(b.uniq())
    msgs = len(b.pings)
    note("секунда спама", "послано %d, принято %d, отвергнуто %d, "
                          "у соседа новых пингов %d (сообщений %d)"
         % (sent, made, drop, uniq, msgs))
    # Потолок выведен: PING_GAP тиков между принятыми, значит за секунду
    # их не больше 30/PING_GAP. Плюс один на границу окна замера.
    cap = proto.TICK_HZ / room_mod.PING_GAP + 1
    check(uniq <= cap,
          "ограничитель держит: не больше %g новых пингов в секунду" % cap,
          "пришло %d при пороге %g (вывод: 30 Гц / %d тиков)"
          % (uniq, cap, room_mod.PING_GAP))
    check(len(r._pings) <= room_mod.PING_PER_PLAYER,
          "один спамер держит не больше %d живых пингов" % room_mod.PING_PER_PLAYER,
          "живых в комнате %d" % len(r._pings))
    check(drop >= sent - made - 2,
         "отвергнутое посчитано, а не потеряно молча",
         "отвергнуто %d из %d посланных" % (drop, sent))
    for c in (a, b):
        c.close()


# --- [4] срок жизни и повтор ------------------------------------------------

async def section_life(port, rooms):
    print("\n[4] Пинг гаснет; пока жив — повторяется (5.2: ev теряется)")
    r, (a, b) = await start_game(port, rooms, ["Аня", "Борис"])
    ent = r.world.entities[r.players[a.pid].ent_id]
    b.pings[:] = []

    t0 = r.world.tick
    a.ping(ent.x + 1, ent.y + 1)
    await b.wait(lambda: len(b.pings) > 0)
    first = b.pings[0]

    await asyncio.sleep(1.0)
    n1 = len(b.pings)
    # Повтор за секунду: 30 тиков / PING_REPEAT.
    want = proto.TICK_HZ / room_mod.PING_REPEAT
    note("повторы", "за 1.0 с пришло %d сообщений про ОДИН пинг (ожидание ~%g)"
         % (n1, want + 1))
    check(len(b.uniq()) == 1, "повтор — это тот же пинг, а не новый",
          "уникальных (автор, тик) %d, сообщений %d" % (len(b.uniq()), n1))
    check(n1 >= 2, "живой пинг повторяется: потеря одного ev не съедает пинг",
          "%d сообщений за секунду, повтор раз в %d тиков"
          % (n1, room_mod.PING_REPEAT))
    check(all(p["tick"] == first["tick"] for p in b.pings),
          "в повторе едет тик ПОСТАНОВКИ — по нему клиент знает остаток жизни",
          "tick=%d во всех %d сообщениях" % (first["tick"], n1))

    # гаснет
    ok = await b.wait(lambda: not r._pings, limit=6.0)
    lived = r.world.tick - t0
    n2 = len(b.pings)
    await asyncio.sleep(0.7)
    after = len(b.pings) - n2
    note("срок жизни", "пинг жил %d тиков (%.2f с) при пороге %d (%.2f с)"
         % (lived, lived * TICK, room_mod.PING_LIFE, room_mod.PING_LIFE * TICK))
    check(ok and abs(lived - room_mod.PING_LIFE) <= room_mod.PING_REPEAT + 1,
          "пинг погас на своём сроке, а не висит вечно",
          "прожил %d тиков, срок %d, допуск %d (шаг проверки — повтор)"
          % (lived, room_mod.PING_LIFE, room_mod.PING_REPEAT + 1))
    check(after == 0, "после смерти пинга сервер о нём молчит",
          "за 0.7 с после смерти пришло %d сообщений" % after)
    for c in (a, b):
        c.close()


# --- [5] вошедший в идущую партию ------------------------------------------

async def section_join(port, rooms):
    print("\n[5] Вошедший в идущую партию сразу видит живые пинги")
    r, (a,) = await start_game(port, rooms, ["Первый"])
    ent = r.world.entities[r.players[a.pid].ent_id]
    a.ping(ent.x + 2, ent.y - 1, danger=True)
    await a.wait(lambda: len(a.pings) > 0)

    c = await Client("Опоздавший").connect(port)
    await c.hello_join(r.code)
    c.send({"t": "ready", "v": True})
    got = await c.wait(lambda: len(c.pings) > 0, limit=3.0)
    note("вошедший", "сообщений про пинги %d, живых на сервере %d"
         % (len(c.pings), len(r._pings)))
    check(got, "вошедший получил живой пинг, а не ждал следующего повтора",
          "пингов у вошедшего %d" % len(c.pings))
    if c.pings:
        check(c.pings[0].get("nm") == "Первый",
              "вошедшему видно, КТО поставил", "nm=%r" % c.pings[0].get("nm"))
    a.close()
    c.close()


# --- [6] клиенту не верят ---------------------------------------------------

async def section_trust(port, rooms):
    print("\n[6] Клиенту не верят: точка пинга режется по карте (5.1)")
    r, (a,) = await start_game(port, rooms, ["Кривой"])
    w, h = r.world.grid.w, r.world.grid.h
    a.pings[:] = []
    a.ping(1e9, -1e9)
    await a.wait(lambda: len(a.pings) > 0)
    g = a.pings[0]
    note("пинг в бесконечность", "просили (1e9, -1e9), карта %dx%d, вышло (%s, %s)"
         % (w, h, g.get("x"), g.get("y")))
    check(0 <= g.get("x", -1) <= w and 0 <= g.get("y", -1) <= h,
          "точка пинга лежит на карте, чего бы клиент ни прислал",
          "(%s, %s) в пределах 0..%d, 0..%d" % (g.get("x"), g.get("y"), w, h))

    # NaN режется ещё в proto (5.1) — пинга не будет вовсе, но и падения тоже
    a.pings[:] = []
    a.seq += 1
    a.ws.write_message('{"t":"input","seq":%d,"mv":[0,0],"aim":[NaN,1],"btn":32}'
                       % a.seq)
    await asyncio.sleep(0.3)
    check(r.phase == room_mod.PLAYING,
          "NaN в точке пинга сервер не роняет", "фаза комнаты %s" % r.phase)
    a.close()


# --- [7] дух пингует --------------------------------------------------------

async def section_ghost(port, rooms):
    print("\n[7] Дух пингует (8.5): больше ему сказать нечем")
    r, (a, b) = await start_game(port, rooms, ["Покойник", "Живой"])
    ent = r.world.entities[r.players[a.pid].ent_id]
    ent.flags |= world_mod.F_DEAD
    b.pings[:] = []
    a.ping(ent.x + 3, ent.y)
    got = await b.wait(lambda: len(b.pings) > 0, limit=2.0)
    check(got, "пинг мёртвого игрока дошёл до живого",
         "flags=%d, пингов у живого %d" % (ent.flags, len(b.pings)))
    for c in (a, b):
        c.close()


# --- [8] спуск гасит пинги --------------------------------------------------

async def section_floor(port, rooms):
    print("\n[8] Спуск на этаж гасит пинги прошлого этажа")
    r, (a,) = await start_game(port, rooms, ["Спускающийся"])
    ent = r.world.entities[r.players[a.pid].ent_id]
    a.ping(ent.x + 1, ent.y + 1)
    await a.wait(lambda: len(a.pings) > 0)
    before = len(r._pings)
    floor0 = r.floor
    r.descend()
    await asyncio.sleep(0.2)
    note("этаж", "было пингов %d на этаже %d, стало %d на этаже %d"
         % (before, floor0, len(r._pings), r.floor))
    check(before > 0 and not r._pings,
          "пинги прошлого этажа погашены: их координаты на новом ничего не значат",
          "живых после спуска %d" % len(r._pings))
    a.close()


async def main():
    print("=== приёмка пингов на карте (8.8), серверная половина ===")
    uptime()
    print("  числа контракта: жизнь %d тиков (%.2f с), ограничитель %d тиков, "
          "повтор %d тиков, на игрока %d, на комнату %d"
          % (room_mod.PING_LIFE, room_mod.PING_LIFE * TICK, room_mod.PING_GAP,
             room_mod.PING_REPEAT, room_mod.PING_PER_PLAYER, room_mod.PING_MAX))
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
        await section_see(port, rooms)
        await section_not_world(port, rooms)
        await section_spam(port, rooms)
        await section_life(port, rooms)
        await section_join(port, rooms)
        await section_trust(port, rooms)
        await section_ghost(port, rooms)
        await section_floor(port, rooms)
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
