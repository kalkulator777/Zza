#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка спуска по лестнице (DESIGN.md 8.1, 4.4, 5.2, 8.5).

Что проверяется.

  1. ОДИН НЕ УТАСКИВАЕТ ГРУППУ. Пока на лестнице не все живые, этаж тот же —
     и это главная проверка файла: без неё спуск превращается в кнопку
     "утащить напарника", которую жмёт любой, кто добежал первым.
  2. ВСТАЛИ ВСЕ — ЭТАЖ СМЕНИЛСЯ. Номер +1, сид комнаты ТОТ ЖЕ, карта другая.
  3. ТУМАН ЗАБЫВАЕТСЯ ЦЕЛИКОМ (4.4). Печатаются открытые и освещённые клетки
     до и после: до — сколько успели открыть, после — ноль.
  4. СОСТОЯНИЕ ПЕРЕЕЗЖАЕТ. Здоровье живого переносится как есть; мёртвый
     воскресает на новом этаже бесплатно (8.5) с полным здоровьем.
  5. КЛИЕНТ УЗНАЁТ ПО ПРОТОКОЛУ (5.2). Уходит level с новым floor, следом
     полный снапшот с full:true и you.
  6. МЁРТВОГО НЕ ЖДУТ. Дух на лестницу встать не обязан, иначе группа
     заперта на этаже до конца забега.
  7. ОТВАЛИВШЕГОСЯ НЕ ЖДУТ. 8.5 превращает его в камень; камень не ходит, а
     окно переподключения — 120 с.

Условие остановки везде — число тиков, а не секунды: раздел 10 запрещает
порог, который на загруженной машине превращается в ложный красный.

Запуск:  python3 tests/floor_descend.py
Выход: 0 — зелено, 1 — красно.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import combat, gen                       # noqa: E402
from server import world as world_mod                # noqa: E402
from server.room import Player, Room, RoomSettings   # noqa: E402

W = world_mod
FAILS = []


def check(ok, title, detail=""):
    print(("  ЗЕЛЕНО  " if ok else "  КРАСНО  ") + title +
          (("  | " + detail) if detail else ""))
    if not ok:
        FAILS.append(title)
    return ok


def note(title, detail=""):
    print("  данные   " + title + (("  | " + detail) if detail else ""))


class Conn(object):
    """Соединение-пустышка: до сокета не доходим, но сообщения копим."""

    def __init__(self):
        self.msgs = []

    def send_str(self, text):
        self.msgs.append(text)

    def last_of(self, t):
        for s in reversed(self.msgs):
            if ('"t":"%s"' % t) in s:
                return json.loads(s)
        return None

    def types_after(self, n):
        out = []
        for s in self.msgs[n:]:
            try:
                out.append(json.loads(s)["t"])
            except Exception:       # noqa: BLE001
                pass
        return out


def build(n_players=2, seed=20250918):
    room = Room("STRS", settings=RoomSettings(seed=seed))
    conns = []
    for i in range(n_players):
        c = Conn()
        conns.append(c)
        room.add(Player(i + 1, "Игрок%d" % (i + 1), c))
    room.start()
    return room, conns


def ents(room):
    return [room.world.entities[p.ent_id] for p in room.players.values()]


def put_on_stairs(room, e):
    st = room.world.stairs
    e.x = st[0] + 0.5
    e.y = st[1] + 0.5
    e.vx = e.vy = 0.0
    e.mv = (0.0, 0.0)


def main():
    print("=" * 70)
    print("Приёмка спуска по лестнице. Мир настоящий (лестницу ставит gen.py),")
    print("но ходьба заменена телепортом: меряется правило спуска, а не бег.")
    print("=" * 70)

    room, conns = build(2)
    w = room.world
    floor0 = room.floor
    seed0 = room.settings.seed
    tiles0 = bytes(w.grid.tiles)
    a, b = ents(room)
    st = w.stairs
    note("этаж %d" % floor0, "карта %dx%d, лестница (%d,%d), сид %d"
         % (w.grid.w, w.grid.h, st[0], st[1], seed0))

    # немного походить, чтобы туману было что помнить
    for _ in range(12):
        room.tick()
    seen_before = w.fog.seen_count()
    lit_before = w.fog.lit_count()

    # --- 1. один на лестнице — этажа нет --------------------------------
    put_on_stairs(room, a)
    for _ in range(10):
        room.tick()
    check(room.floor == floor0 and not room.stairs_ready(),
          "один на лестнице группу не утаскивает (8.1)",
          "на лестнице %d из %d живых, этаж %d (был %d)"
          % (sum(1 for e in ents(room) if w.on_stairs(e)),
             len(w.alive_players()), room.floor, floor0))
    check(w.on_stairs(a) and not w.on_stairs(b),
          "первый стоит на лестнице, второй нет (условие проверки живо)",
          "первый (%.2f,%.2f), второй (%.2f,%.2f), лестница (%d,%d)"
          % (a.x, a.y, b.x, b.y, st[0], st[1]))

    # --- 2. встали все — этаж сменился -----------------------------------
    a.hp = 37                      # здоровью положено переехать как есть
    n_msgs = [len(c.msgs) for c in conns]
    put_on_stairs(room, a)
    put_on_stairs(room, b)
    max_id_before = max(room.world.entities)  # для проверки ниже
    room.tick()
    seen_after = w.fog.seen_count()
    lit_after = w.fog.lit_count()

    check(room.floor == floor0 + 1,
          "все живые на лестнице — этаж сменился",
          "этаж %d -> %d" % (floor0, room.floor))
    check(room.settings.seed == seed0 and w.seed == seed0,
          "сид комнаты тот же (8.1: забег один, этажи разные)",
          "сид %d -> %d" % (seed0, room.settings.seed))
    check(bytes(w.grid.tiles) != tiles0,
          "карта нового этажа другая",
          "совпало байт %d из %d"
          % (sum(1 for i, v in enumerate(w.grid.tiles) if tiles0[i] == v),
             len(tiles0)))
    note("туман до спуска", "открыто %d, освещено %d" % (seen_before, lit_before))
    note("туман сразу после", "открыто %d, освещено %d" % (seen_after, lit_after))
    check(seen_before > 0 and seen_after == 0 and lit_after == 0,
          "туман забыт целиком (4.4: память группы живёт до смены этажа)",
          "открыто %d -> %d, освещено %d -> %d"
          % (seen_before, seen_after, lit_before, lit_after))
    room.tick()
    note("туман через тик на новом этаже",
         "открыто %d, освещено %d" % (w.fog.seen_count(), w.fog.lit_count()))
    check(w.fog.seen_count() > 0,
          "на новом этаже туман начинает открываться заново",
          "открыто %d клеток" % w.fog.seen_count())

    # --- 3. состояние переехало ------------------------------------------
    a2, b2 = ents(room)
    check(a2.hp == 37 and a2.id == a.id,
          "здоровье переносится на новый этаж как есть",
          "hp %d -> %d, id сущности %d (id не переиспользуются, 4.3)"
          % (37, a2.hp, a2.id))
    check(not w.on_stairs(a2) and not w.on_stairs(b2),
          "игроки появились в стартовой комнате, а не на новой лестнице",
          "игроки (%.2f,%.2f) и (%.2f,%.2f), новая лестница (%d,%d), "
          "путь до неё %d клеток"
          % (a2.x, a2.y, b2.x, b2.y, w.stairs[0], w.stairs[1],
             gen.generate(seed0, room.floor, room.settings.map_w,
                          room.settings.map_h, room.settings.theme).stairs_dist))
    # Раньше здесь стояло «на новый этаж переехали только игроки». С этапа 2b
    # это ложно по замыслу: на новом этаже есть свои враги. Смысл проверки
    # («старое не переезжает») жив, и проверяется он через id: они не
    # переиспользуются (4.3), значит у всего, что родилось на новом этаже,
    # id больше любого старого.
    check(all(e.id > max_id_before
              for e in w.entities.values() if e.kind != W.K_PLAYER),
          "старые сущности на новый этаж не переехали",
          "сущностей %d" % len(w.entities))

    # --- 4. клиент узнал по протоколу ------------------------------------
    lvl = conns[0].last_of("level")
    types = conns[0].types_after(n_msgs[0])
    snap_full = None
    for s in conns[0].msgs[n_msgs[0]:]:
        m = json.loads(s)
        if m["t"] == "snap" and m.get("full"):
            snap_full = m
            break
    note("сообщения клиенту в тик спуска", "%s" % types)
    check(lvl is not None and lvl["floor"] == floor0 + 1,
          "клиенту ушло level с новым номером этажа (5.2)",
          "floor=%s" % (lvl and lvl["floor"]))
    check(types[:1] == ["level"] and snap_full is not None
          and "you" in snap_full,
          "порядок обязателен: сперва level, потом полный снапшот с you",
          "порядок %s, you=%s" % (types, snap_full and snap_full.get("you")))

    # --- 5. мёртвого не ждут, он воскресает ------------------------------
    room2, conns2 = build(2, seed=777001)
    w2 = room2.world
    a3, b3 = ents(room2)
    floor_before = room2.floor
    combat.damage(w2, b3, b3.hp_max)
    check(b3.flags & W.F_DEAD, "напарник мёртв (подготовка)",
          "hp=%d flags=%d" % (b3.hp, b3.flags))
    b3.x, b3.y = 2.5, 2.5          # дух вообще в другой стороне
    put_on_stairs(room2, a3)
    room2.tick()
    a4 = room2.world.entities[[p for p in room2.players.values()][0].ent_id]
    b4 = room2.world.entities[[p for p in room2.players.values()][1].ent_id]
    check(room2.floor == floor_before + 1,
          "мёртвого на лестнице не ждут (8.5)",
          "живых на лестнице 1 из 1, этаж %d -> %d"
          % (floor_before, room2.floor))
    check(not (b4.flags & W.F_DEAD) and b4.hp == b4.hp_max,
          "мёртвый воскресает на следующем этаже бесплатно (8.5)",
          "flags %d, hp %d из %d" % (b4.flags, b4.hp, b4.hp_max))
    check(a4.dash_end == 0 and a4.atk_hit == 0 and a4.inv_end == 0,
          "боевые таймеры этаж не переживают",
          "dash_end=%d atk_hit=%d inv_end=%d"
          % (a4.dash_end, a4.atk_hit, a4.inv_end))

    # --- 6. отвалившегося не ждут ----------------------------------------
    room3, conns3 = build(2, seed=777002)
    w3 = room3.world
    a5, b5 = ents(room3)
    floor_before = room3.floor
    room3.disconnect([p for p in room3.players.values()][1])
    put_on_stairs(room3, a5)
    room3.tick()
    check(room3.floor == floor_before + 1,
          "отвалившегося не ждут: камень не ходит (8.5)",
          "этаж %d -> %d, флаги напарника %d"
          % (floor_before, room3.floor, b5.flags))

    # --- 7. все мертвы — спуска нет --------------------------------------
    room4, conns4 = build(2, seed=777003)
    w4 = room4.world
    floor_before = room4.floor
    for e in ents(room4):
        combat.damage(w4, e, e.hp_max)
        put_on_stairs(room4, e)
    for _ in range(5):
        room4.tick()
    check(room4.floor == floor_before,
          "группа духов вниз не спускается (8.1: это конец забега, не спуск)",
          "этаж %d, живых %d" % (room4.floor, len(w4.alive_players())))

    print()
    if FAILS:
        print("КРАСНО: %d" % len(FAILS))
        for f in FAILS:
            print("   * " + f)
        return 1
    print("ЗЕЛЕНО: всё сошлось")
    return 0


if __name__ == "__main__":
    sys.exit(main())
