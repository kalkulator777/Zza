#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка этапа 0a: два клиента играют вместе, и стены держат.

Поднимает настоящий сервер в этом же процессе на свободном порту и
подключается к нему двумя настоящими WebSocket-клиентами (tornado умеет
клиентом). Никаких заглушек: трафик идёт через сокет, через proto и через
комнату.

Каждый клиент держит своё зеркало мира, собранное ТОЛЬКО из снапшотов
(полный + дельты). Значит, проверяется заодно и дельта-протокол.

Запуск:  python3 tests/two_clients.py
Выход:   0 — зелено, 1 — красно.
"""

import asyncio
import json
import math
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

from tornado.httpserver import HTTPServer          # noqa: E402
from tornado.websocket import websocket_connect    # noqa: E402

from server import proto, room as room_mod, world as world_mod  # noqa: E402
from server.app import make_app                    # noqa: E402
from server.room import Rooms                      # noqa: E402

R = world_mod.R_PLAYER
SPEED = world_mod.SPEED_RUN
HZ = world_mod.TICK_HZ

FAILS = []


def check(ok, title, detail=""):
    print(("  ЗЕЛЕНО  " if ok else "  КРАСНО  ") + title + (("  | " + detail) if detail else ""))
    if not ok:
        FAILS.append(title)
    return ok


class Client(object):
    """Клиент: зеркало мира собирается только из снапшотов."""

    def __init__(self, name):
        self.name = name
        self.ws = None
        self.pid = 0
        self.room = ""
        self.ents = {}
        self.tick = 0
        self.ack = 0
        self.seq = 0
        self.tiles = None
        self.rooms_msg = None
        self.pong_msg = None
        self.w = self.h = 0
        self.bytes = 0
        self.snaps = 0
        self.full_bytes = 0
        self.delta_bytes = 0
        self._task = None

    async def connect(self, port):
        self.ws = await websocket_connect("ws://127.0.0.1:%d/ws" % port)
        self._task = asyncio.ensure_future(self._reader())

    async def _reader(self):
        while True:
            msg = await self.ws.read_message()
            if msg is None:
                return
            self.bytes += len(msg.encode("utf-8"))
            m = json.loads(msg)
            t = m.get("t")
            if t == "snap":
                self.tick = m["tick"]
                self.ack = m["ack"]
                self.snaps += 1
                if m["full"]:
                    self.ents = {a[0]: a for a in m["e"]}
                    self.full_bytes = len(msg.encode("utf-8"))
                else:
                    for a in m["e"]:
                        self.ents[a[0]] = a
                    for i in m.get("rm", ()):
                        self.ents.pop(i, None)
                    self.delta_bytes += len(msg.encode("utf-8"))
            elif t == "welcome":
                self.pid = m["pid"]
            elif t == "joined":
                self.pid = m["pid"]
                self.room = m["room"]
            elif t == "roomlist":
                self.rooms_msg = m["rooms"]
            elif t == "pong":
                self.pong_msg = m
            elif t == "level":
                self.w, self.h = m["w"], m["h"]
                self.tiles = proto.rle_decode(m["tiles"])

    def send(self, obj):
        self.ws.write_message(json.dumps(obj))

    def move(self, dx, dy):
        self.seq += 1
        self.send({"t": "input", "seq": self.seq, "mv": [dx, dy],
                   "aim": [0, 0], "btn": 0})
        return self.seq

    async def hello_join(self, room="", ready=True):
        """hello + join. С ready=False останавливается в лобби."""
        self.send({"t": "hello", "name": self.name, "ver": 1})
        self.send({"t": "join", "room": room, "name": self.name})
        for _ in range(200):
            await asyncio.sleep(0.02)
            if self.room:
                break
        if not self.room:
            return False
        if not ready:
            return True
        return await self.go_ready()

    async def go_ready(self):
        """5.1 ready: на 0a это и есть команда «начали»."""
        self.send({"t": "ready", "v": True})
        for _ in range(200):
            await asyncio.sleep(0.02)
            if self.tiles is not None and self.ents:
                return True
        return False

    async def wait_ack(self, seq, limit=2.0):
        n = int(limit / 0.01)
        for _ in range(n):
            if self.ack >= seq:
                return True
            await asyncio.sleep(0.01)
        return False

    def me(self):
        return self.ents.get(self.my_ent)

    def solid(self, tx, ty):
        if tx < 0 or ty < 0 or tx >= self.w or ty >= self.h:
            return True
        return self.tiles[ty * self.w + tx] == 0

    async def hold(self, dx, dy, seconds):
        """Держать направление указанное время, подкармливая сервер 30 Гц."""
        t_end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < t_end:
            self.move(dx, dy)
            await asyncio.sleep(1.0 / HZ)


def pos(a):
    return (a[2], a[3])


def clear_run(c, x, y, dx, dy, dist):
    """Свободен ли путь длиной dist из (x,y) в направлении (dx,dy)."""
    steps = int(dist * 8) + 1
    for i in range(steps + 1):
        px = x + dx * dist * i / steps
        py = y + dy * dist * i / steps
        for ty in (int(math.floor(py - R)), int(math.floor(py + R))):
            for tx in (int(math.floor(px - R)), int(math.floor(px + R))):
                if c.solid(tx, ty):
                    return False
    return True


def wall_edge_left(c, x, y):
    """Где встанет круг, если ехать влево из (x,y): граница первой стены + R."""
    rows = range(int(math.floor(y - R)), int(math.floor(y + R)) + 1)
    tx = int(math.floor(x - R))
    while tx >= -1:
        if any(c.solid(tx, ty) for ty in rows):
            return (tx + 1) + R
        tx -= 1
    return None


def inside_wall(c, x, y):
    """Режет ли круг (x,y,R) сплошной тайл — то есть пролез ли он в стену."""
    r2 = R * R
    for ty in range(int(math.floor(y - R)), int(math.floor(y + R)) + 1):
        for tx in range(int(math.floor(x - R)), int(math.floor(x + R)) + 1):
            if not c.solid(tx, ty):
                continue
            cx = min(max(x, tx), tx + 1.0)
            cy = min(max(y, ty), ty + 1.0)
            if (x - cx) ** 2 + (y - cy) ** 2 < r2 - 1e-4:
                return True
    return False


async def scenario_together(a, b):
    print("\n[1] Два клиента в одной комнате видят движение друг друга")
    print("    комната %s: A pid=%d, B pid=%d" % (a.room, a.pid, b.pid))

    if not check(a.my_ent in b.ents and b.my_ent in a.ents,
                 "каждый видит сущность другого в снапшоте",
                 "A видит %s, B видит %s" % (sorted(a.ents), sorted(b.ents))):
        return
    a0 = pos(a.ents[a.my_ent])
    b0 = pos(b.ents[b.my_ent])

    # направления выбираем по тайлам, чтобы столб генератора не испортил замер
    dirs = [(0, 1), (0, -1), (1, 0), (-1, 0)]
    da = next((d for d in dirs if clear_run(a, a0[0], a0[1], d[0], d[1], 5.0)), (0, 1))
    db = next((d for d in dirs if clear_run(b, b0[0], b0[1], d[0], d[1], 5.0)), (0, 1))

    t0 = a.tick
    await asyncio.gather(a.hold(da[0], da[1], 1.5), b.hold(db[0], db[1], 1.5))
    s = a.move(0, 0)
    b.move(0, 0)
    await a.wait_ack(s)
    t1 = a.tick              # тик, на котором стоп уже применён
    await asyncio.sleep(0.15)   # дать последнему снапшоту дойти

    a_in_b = pos(b.ents[a.my_ent])      # где A по мнению B
    b_in_a = pos(a.ents[b.my_ent])      # где B по мнению A
    a_in_a = pos(a.ents[a.my_ent])
    b_in_b = pos(b.ents[b.my_ent])

    expect = SPEED * (t1 - t0) / HZ
    da_moved = math.dist(a0, a_in_b)
    db_moved = math.dist(b0, b_in_a)

    print("    A: старт (%.3f, %.3f) -> конец (%.3f, %.3f), направление %s" %
          (a0[0], a0[1], a_in_a[0], a_in_a[1], da))
    print("    B: старт (%.3f, %.3f) -> конец (%.3f, %.3f), направление %s" %
          (b0[0], b0[1], b_in_b[0], b_in_b[1], db))
    print("    A глазами B: (%.3f, %.3f), путь %.3f клетки" % (a_in_b[0], a_in_b[1], da_moved))
    print("    B глазами A: (%.3f, %.3f), путь %.3f клетки" % (b_in_a[0], b_in_a[1], db_moved))
    print("    ожидание пути при 5.0 кл/с за %d тиков: %.3f клетки" % (t1 - t0, expect))

    check(da_moved > 1.0, "B видит движение A", "прошёл %.3f клетки" % da_moved)
    check(db_moved > 1.0, "A видит движение B", "прошёл %.3f клетки" % db_moved)
    check(math.dist(a_in_a, a_in_b) < 0.002 and math.dist(b_in_b, b_in_a) < 0.002,
          "оба зеркала совпадают по координатам",
          "расхождение %.4f и %.4f" % (math.dist(a_in_a, a_in_b), math.dist(b_in_b, b_in_a)))
    check(abs(da_moved - expect) < 0.5 and abs(db_moved - expect) < 0.5,
          "путь совпадает со скоростью 4.2 (5.0 кл/с)",
          "A %.3f, B %.3f, ждали %.3f" % (da_moved, db_moved, expect))


async def scenario_wall(a):
    print("\n[2] Стена в лоб: за 2 секунды не пролезает")
    p0 = pos(a.ents[a.my_ent])
    edge = wall_edge_left(a, p0[0], p0[1])
    await a.hold(-1, 0, 2.0)
    s = a.move(0, 0)
    await a.wait_ack(s)
    await asyncio.sleep(0.15)
    p1 = pos(a.ents[a.my_ent])
    print("    старт x=%.3f y=%.3f, упор рассчитан по тайлам: x=%.3f" % (p0[0], p0[1], edge))
    print("    через 2 с: x=%.3f y=%.3f, свободного хода было %.3f клетки" %
          (p1[0], p1[1], p0[0] - edge))
    print("    заступ за расчётную границу: %.4f клетки" % (edge - p1[0]))
    check(p1[0] >= edge - 0.01, "не прошёл сквозь стену",
          "x=%.4f, граница %.4f" % (p1[0], edge))
    check(abs(p1[0] - edge) < 0.02, "встал вплотную, а не за клетку до",
          "зазор %.4f клетки" % abs(p1[0] - edge))
    check(not inside_wall(a, p1[0], p1[1]), "круг не режет ни одного тайла стены")
    check(abs(p1[1] - p0[1]) < 0.01, "по свободной оси не сдвинулся",
          "dy=%.4f" % (p1[1] - p0[1]))


async def scenario_slide(a):
    print("\n[3] Стена под углом: скользит вдоль, а не залипает")
    # прижаться к западной стене и поехать вниз-влево
    await a.hold(-1, 0, 0.7)
    s = a.move(0, 0)
    await a.wait_ack(s)
    await asyncio.sleep(0.1)
    p0 = pos(a.ents[a.my_ent])
    t0 = a.tick
    k = 1.0 / math.sqrt(2.0)
    await a.hold(-k, k, 2.0)
    s = a.move(0, 0)
    await a.wait_ack(s)
    t1 = a.tick
    await asyncio.sleep(0.15)
    p1 = pos(a.ents[a.my_ent])
    dy = p1[1] - p0[1]
    free = SPEED * k * (t1 - t0) / HZ
    print("    у стены: (%.3f, %.3f) -> (%.3f, %.3f) за %d тиков" %
          (p0[0], p0[1], p1[0], p1[1], t1 - t0))
    print("    ход вдоль стены dy=%.3f клетки, свободный ход по этой оси был бы %.3f" % (dy, free))
    print("    ход в стену dx=%.4f (ожидается 0)" % (p1[0] - p0[0]))
    # порог: скольжение обязано давать почти весь свободный ход по своей оси.
    # 10% запаса — на округление координат (5.2) и на ±1 тик в учёте времени.
    # Скольжение засчитывается, только если игрок при этом ПРИЖАТ к стене.
    # Без второго условия проверка зеленеет на коде, где стен нет вовсе:
    # дирижёр поймал это подсадкой solid() -> False, там свободный полёт дал
    # dy=7.189 и «ЗЕЛЕНО едет вдоль стены». Одна проверка охраняла ничего.
    pinned = abs(p1[0] - p0[0]) < 0.02
    check(dy > free * 0.9 and pinned, "едет вдоль стены, будучи прижат к ней",
          "dy=%.3f, свободный ход %.3f (порог 90%%), смещение в стену %.4f"
          % (dy, free, p1[0] - p0[0]))
    check(abs(p1[0] - p0[0]) < 0.02, "в стену не продвинулся",
          "dx=%.4f" % (p1[0] - p0[0]))
    check(not inside_wall(a, p1[0], p1[1]), "круг не режет ни одного тайла стены")


async def scenario_garbage(a):
    print("\n[4] Мусор от клиента сервер не роняет")
    before = a.tick
    for junk in ('не json', '[]', '{"t":"input","mv":"вверх"}', '{"t":"input","mv":[1e999,0]}',
                 '{"t":"input","mv":[NaN,0]}', '{"t":"нетакого"}', '{}', '"строка"',
                 '{"t":"input","seq":-5,"mv":[99,99],"btn":4095}', '{"t":"join","room":12345}'):
        a.ws.write_message(junk)
    await asyncio.sleep(0.4)
    p = pos(a.ents[a.my_ent])
    ok_finite = math.isfinite(p[0]) and math.isfinite(p[1])
    check(a.tick > before, "сервер продолжает тикать", "тик %d -> %d" % (before, a.tick))
    check(ok_finite and not inside_wall(a, p[0], p[1]),
          "координаты остались числами и вне стен", "(%.3f, %.3f)" % p)


async def scenario_flood(a):
    """5.1: сервер обязан пережить 120 Гц от кривого клиента, применив последний."""
    print("\n[5] Кривой клиент шлёт ввод 120 Гц — применяется последний")
    s = a.move(0, 0)
    await a.wait_ack(s)
    await asyncio.sleep(0.1)
    p0 = pos(a.ents[a.my_ent])
    t0 = a.tick
    sent = 0
    t_end = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < t_end:
        a.move(0, 1)
        sent += 1
        await asyncio.sleep(1.0 / 120.0)
    s = a.move(0, 0)
    await a.wait_ack(s)
    t1 = a.tick
    await asyncio.sleep(0.15)
    p1 = pos(a.ents[a.my_ent])
    went = math.dist(p0, p1)
    expect = SPEED * (t1 - t0) / HZ
    print("    отправлено %d команд ввода за 1 с (%.0f Гц), тиков прошло %d"
          % (sent, sent, t1 - t0))
    print("    прошёл %.3f клетки, по скорости 4.2 должен %.3f" % (went, expect))
    check(abs(went - expect) < 0.5, "лишний ввод не ускорил игрока",
          "прошёл %.3f, ждали %.3f" % (went, expect))
    check(a.tick > t0, "сервер жив после потока ввода")


async def scenario_lobby(a):
    """5.1/5.2: rooms -> roomlist, ping -> pong."""
    print("\n[6] Список комнат и ping/pong")
    a.rooms_msg = None
    a.pong_msg = None
    a.send({"t": "rooms"})
    a.send({"t": "ping", "id": 7, "ct": 1699999999.123})
    for _ in range(100):
        await asyncio.sleep(0.02)
        if a.rooms_msg is not None and a.pong_msg is not None:
            break
    rl = a.rooms_msg or []
    pg = a.pong_msg or {}
    print("    roomlist: %s" % rl)
    print("    pong: %s" % pg)
    check(any(r.get("id") == a.room for r in rl), "своя комната есть в списке")
    check(bool(rl) and rl[0].get("players", 0) >= 1 and "floor" in rl[0],
          "в списке есть число игроков и этаж")
    check(pg.get("id") == 7 and abs(pg.get("ct", 0) - 1699999999.123) < 1e-6,
          "pong вернул id и клиентское время без искажений")
    check(isinstance(pg.get("st"), float), "pong принёс серверное время")


async def scenario_reconnect(a, port):
    print("\n[7] Переподключение: возвращается в ту же комнату и ту же сущность")
    room, ent, p_before = a.room, a.my_ent, pos(a.ents[a.my_ent])
    a.ws.close()
    await asyncio.sleep(0.4)
    c = Client(a.name)
    await c.connect(port)
    ok = await c.hello_join(room)
    if not ok:
        check(False, "переподключение прошло")
        return
    c.my_ent = next((i for i, e in c.ents.items() if e[1] == 1 and i == ent), None)
    p_after = pos(c.ents[ent]) if ent in c.ents else None
    print("    до обрыва: сущность %d в (%.3f, %.3f)" % (ent, p_before[0], p_before[1]))
    print("    после: комната %s, сущность %s в %s" %
          (c.room, ent in c.ents, ("(%.3f, %.3f)" % p_after) if p_after else "нет"))
    check(c.room == room, "та же комната", c.room)
    check(p_after is not None and math.dist(p_before, p_after) < 0.01,
          "та же сущность на том же месте")
    c.ws.close()


async def main():
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
    print("сервер на порту %d, тик %d Гц" % (port, HZ))

    a = Client("Аня")
    b = Client("Боря")
    await a.connect(port)
    if not await a.hello_join("", ready=False):
        check(False, "первый клиент вошёл в комнату")
        return 1

    print("\n[0] Фазы комнаты: лобби без мира -> игра по готовности")
    lob = rooms.get(a.room)
    print("    после join: фаза %r, мир %r, сущностей %d"
          % (lob.phase, lob.world, len(a.ents)))
    check(lob.phase == room_mod.LOBBY and lob.world is None,
          "комната рождается в лобби и БЕЗ мира", "фаза %r" % lob.phase)
    check(lob.tick() is False, "тик лобби не считает (процессор не ест)")
    check(a.tiles is None and not a.ents,
          "в лобби клиенту не шлётся ни уровень, ни снапшот")
    if not await a.go_ready():
        check(False, "по готовности игра началась")
        return 1
    print("    после ready: фаза %r, мир %dx%d, seed %d, настройки %s"
          % (lob.phase, lob.world.grid.w, lob.world.grid.h,
             lob.settings.seed, lob.settings.describe()))
    check(lob.phase == room_mod.PLAYING and lob.world is not None,
          "ready перевёл комнату в игру и создал мир")
    check(lob.world.seed == lob.settings.seed,
          "мир создан из настроек комнаты, а не из воздуха",
          "seed мира %d, seed настроек %d" % (lob.world.seed, lob.settings.seed))
    await b.connect(port)
    if not await b.hello_join(a.room):
        check(False, "второй клиент вошёл в ту же комнату")
        return 1
    r = rooms.get(a.room)
    same = (a.room == b.room and r is not None
            and a.pid in r.players and b.pid in r.players)
    if not check(same, "оба клиента оказались в одной комнате",
                 "A в %r, B в %r" % (a.room, b.room)):
        print("\nдальше проверять нечего: игроки в разных мирах")
        rooms.stop(); ticker.cancel()
        print("\nКРАСНО: " + "; ".join(FAILS))
        return 1
    a.my_ent = r.players[a.pid].ent_id
    b.my_ent = r.players[b.pid].ent_id
    await asyncio.sleep(0.2)

    await scenario_together(a, b)
    await scenario_wall(a)
    await scenario_slide(a)
    await scenario_garbage(a)
    await scenario_flood(a)
    await scenario_lobby(a)
    await scenario_reconnect(a, port)

    print("\n[8] Байты (справочно, при %d сущностях)" % len(b.ents))
    print("    B: снапшотов %d, всего %d байт, полный снапшот %d байт" %
          (b.snaps, b.bytes, b.full_bytes))
    print("    средняя дельта %.1f байт" % (b.delta_bytes / max(1, b.snaps - 1)))

    rooms.stop()
    ticker.cancel()
    b.ws.close()
    print("\n" + ("ВСЁ ЗЕЛЕНО" if not FAILS else "КРАСНО: " + "; ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
