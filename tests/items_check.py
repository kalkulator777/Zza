#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка предметов и апгрейдов (DESIGN.md 8.4, 4.3, 5.1, 5.2, 8.1, 8.5).

Что здесь проверяется и почему именно так.

  1. АЛТАРЬ ЛЕЖИТ И СТОИТ ДЕНЕГ. Три предмета РАЗНЫХ видов в одной комнате,
     и комната выбрана по КРЮКУ с дороги вниз (gen._pick_altar). Печатается
     сам крюк в клетках пути и его доля от пути до лестницы: без числа
     «цена» — это слово.
  2. ПОДБОР ИДЁТ ЧЕРЕЗ НАСТОЯЩИЙ ПРОТОКОЛ. Не вызовом items.take(), а
     строкой JSON с btn=16 (5.1), которая проходит proto.parse_client, потом
     room.apply_inputs, потом тик. Смотрим снапшот до и после: предмет там
     был и перестал быть, и ушло событие pick (5.2).
  3. ВЗЯЛ ОДИН — ОСТАЛЬНЫЕ ПОГАСЛИ. Это и есть цена: 2 из 3 апгрейдов этажа
     теряются навсегда. Без этого предмет — подарок, а не решение.
  4. КАЖДЫЙ АПГРЕЙД МЕНЯЕТ БОЙ, И ЭТО ЧИСЛО. Для каждого печатается «было ->
     стало, во столько раз». Ноль -> не ноль печатается как правило, а не
     как раз: 20 из 0 не делится.
  5. СКЛАДЫВАЮТСЯ. Два одинаковых дают удвоение (Ветер — обратную
     пропорцию, см. его строку), разные сочетаются и не затирают друг друга.
     Это требование 8.4 прямым текстом, и это главная проверка файла.
  6. ПЕРЕЖИВАЮТ СПУСК. 8.5 переносит здоровье, 8.4 держит апгрейды внутри
     ЗАБЕГА, а забег — это все этажи. Числа до и после спуска.
  7. БАЛАНС НЕ СЛОМАН ОЧЕВИДНО. Игрок с типичным набором умирает от того же
     числа ударов подряд, что и голый, и в толпе всё равно гибнет. Печатается
     за сколько ударов и за сколько секунд.
  8. ЦЕНА В ТИКЕ. Таран — единственный апгрейд, который стоит перебора по
     сущностям; меряется на 200 врагах, с ним и без него.

Карты для боя — свои, маленькие (раздел 10: проверка боя, которая берёт
карту у генератора, краснеет от чужой работы). Настоящий генератор берётся
только там, где проверяется сам генератор и спуск.

Запуск:  python3 tests/items_check.py
Выход: 0 — зелено, 1 — красно.
"""

import json
import math
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import ai, combat, gen, items, physics, proto   # noqa: E402
from server import world as world_mod                       # noqa: E402
from server.room import Player, Room, RoomSettings          # noqa: E402

W = world_mod
DT = world_mod.DT
FAILS = []


def check(ok, title, detail=""):
    print(("  ЗЕЛЕНО  " if ok else "  КРАСНО  ") + title +
          (("  | " + detail) if detail else ""))
    if not ok:
        FAILS.append(title)
    return ok


def note(title, detail=""):
    print("  данные   " + title + (("  | " + detail) if detail else ""))


def uptime_line():
    try:
        return subprocess.run(["uptime"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:               # noqa: BLE001
        return "uptime недоступен"


def ratio(before, after):
    """«во сколько раз», честно про ноль: из нуля не делится."""
    if before:
        return "в %.2f раза" % (float(after) / before)
    return "было 0 — это правило, а не проценты"


# --- карты и арена ---------------------------------------------------------

def open_room(w=28, h=14):
    g = physics.Grid(w, h)
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            g.set(x, y, physics.TILE_FLOOR)
    return g


def make_world(grid=None):
    return W.World(grid or open_room(), seed=1, floor=1, spawns=[(2.5, 2.5)])


def add_player(w, x, y, facing=0.0, hp=100, ups=None):
    e = w.spawn(W.K_PLAYER, x, y, r=W.R_PLAYER, speed=W.SPEED_RUN, ctl=True,
                hp=hp, hp_max=100, facing=facing)
    for u, n in (ups or []):
        items.grant(e, u, n)
    return e


def add_enemy(w, x, y, hp=40, ranged=False):
    kind = W.K_ENEMY_RANGED if ranged else W.K_ENEMY
    return w.spawn(kind, x, y, r=ai.ENEMY_R, speed=0.0, hp=hp, hp_max=hp)


def press(w, e, btn, mv=(0.0, 0.0), ticks=1):
    e.btn = btn
    e.mv = mv
    for _ in range(ticks):
        w.step()
    e.btn = 0


def set_name(u):
    return items.NAMES[u]


# --- 1. алтарь: где лежит и чем за него платят ----------------------------

def test_altar():
    print("\n--- 1. алтарь: предметы лежат в подземелье и стоят крюка (8.4) ---")
    detours = []
    shares = []
    sizes = []
    kinds_seen = set()
    no_altar = 0
    for seed in range(1, 41):
        for floor in (1, 2, 3):
            fl = gen.generate(seed, floor, 64, 48)
            if fl.altar is None:
                no_altar += 1
                continue
            detours.append(fl.altar_detour)
            shares.append(fl.altar_detour / float(max(1, fl.stairs_dist)))
            sizes.append(len(fl.altar_slots))
    check(no_altar == 0, "алтарь есть на каждом этаже",
          "этажей 120, без алтаря %d" % no_altar)
    check(min(sizes) == items.ALTAR_SLOTS,
          "мест под предметы ровно %d на каждом этаже" % items.ALTAR_SLOTS,
          "минимум по 120 этажам %d" % min(sizes))
    note("крюк за апгрейд, клеток пути",
         "медиана %.0f, мин %d, макс %d (бег 5 кл/с => медиана %.1f с туда-обратно)"
         % (statistics.median(detours), min(detours), max(detours),
            statistics.median(detours) / W.SPEED_RUN))
    check(max(shares) <= 1.0 + 1e-9,
          "крюк не больше самой дороги вниз (потолок _pick_altar)",
          "доля от пути до лестницы: медиана %.2f, макс %.2f"
          % (statistics.median(shares), max(shares)))
    check(statistics.median(detours) >= 20.0,
          "крюк не декоративный: за апгрейд платят временем",
          "медиана %.0f клеток = %.1f с бега в одну сторону"
          % (statistics.median(detours), statistics.median(detours) / W.SPEED_RUN))

    # что именно лежит
    room = Room("ALTR", settings=RoomSettings(seed=777))
    room.add(Player(1, "Один", _Conn()))
    room.start()
    w = room.world
    its = [e for e in w.entities.values() if items.is_item(e.kind)]
    kinds_seen = sorted(e.kind for e in its)
    ups = sorted(items.up_of(e.kind) for e in its)
    check(len(its) == items.ALTAR_SLOTS,
          "в мире лежит %d предмета" % items.ALTAR_SLOTS,
          "kind на проводе: %s (%s)"
          % (kinds_seen, ", ".join(set_name(u) for u in ups)))
    check(len(set(ups)) == len(ups),
          "предметы алтаря РАЗНЫЕ: выбор, а не три одинаковых подарка",
          "виды %s" % [set_name(u) for u in ups])
    check(all(e.alt == its[0].alt and e.alt for e in its),
          "все три — один алтарь (общий номер)", "alt=%d" % its[0].alt)

    # детерминизм (8.7): тот же сид — тот же выбор
    room2 = Room("ALT2", settings=RoomSettings(seed=777))
    room2.add(Player(1, "Один", _Conn()))
    room2.start()
    its2 = sorted((e.kind, round(e.x, 3), round(e.y, 3))
                  for e in room2.world.entities.values()
                  if items.is_item(e.kind))
    its1 = sorted((e.kind, round(e.x, 3), round(e.y, 3)) for e in its)
    check(its1 == its2, "один сид — тот же алтарь на том же месте (8.7)",
          "%d предметов совпали" % len(its1))
    return room


class _Conn(object):
    """Соединение-пустышка: сокета нет, сообщения копим."""

    def __init__(self):
        self.msgs = []

    def send_str(self, text):
        self.msgs.append(text)

    def msgs_of(self, t, since=0):
        out = []
        for s in self.msgs[since:]:
            if ('"t":"%s"' % t) in s:
                try:
                    out.append(json.loads(s))
                except Exception:       # noqa: BLE001
                    pass
        return out


# --- 2. подбор через настоящий протокол (5.1 бит 16) ----------------------

def test_pickup_protocol():
    print("\n--- 2. подбор: кнопка предмета (btn бит 16) -> сервер -> снапшот ---")
    room = Room("PICK", settings=RoomSettings(seed=777))
    conn = _Conn()
    p = Player(1, "Один", conn)
    room.add(p)
    room.start()
    w = room.world
    e = w.entities[p.ent_id]
    its = [t for t in w.entities.values() if items.is_item(t.kind)]
    target = min(its, key=lambda t: t.id)

    # снапшот ДО: предмет на проводе есть
    snap_before = json.loads(proto.snap(w.tick, 0, True,
                                        [proto.encode_entity(t)
                                         for t in w.entities.values()]))
    on_wire = [row for row in snap_before["e"] if items.is_item(row[1])]
    check(len(on_wire) == items.ALTAR_SLOTS,
          "предмет виден в снапшоте как обычная сущность (4.3)",
          "строк с kind предмета: %d, пример %s" % (len(on_wire), on_wire[0]))

    # один обычный тик: предметы должны сначала уехать клиенту, иначе
    # снимать их из дельты (rm) не с чего — клиент их и не видел
    room.tick()
    # встали на предмет и нажали кнопку — РОВНО как клиент
    e.x, e.y = target.x, target.y
    raw = json.dumps({"t": "input", "seq": 5, "mv": [0, 0], "aim": [0, 0],
                      "btn": proto.BTN_ITEM})
    m = proto.parse_client(raw)
    check(m is not None and m["btn"] == proto.BTN_ITEM,
          "бит 16 доходит через proto.parse_client (5.1)",
          "btn на входе %d, после разбора %s" % (proto.BTN_ITEM,
                                                 m and m["btn"]))
    n_msgs = len(conn.msgs)
    p.pending = m
    room.tick()

    u = items.up_of(target.kind)
    check(items.count(e, u) == 1,
          "апгрейд применён к владельцу",
          "%s: было 0, стало %d" % (set_name(u), items.count(e, u)))
    picks = conn.msgs_of("ev", n_msgs)
    picks = [x for x in picks if x.get("k") == "pick"]
    check(len(picks) == 1, "ушло событие pick (5.2)",
          "%s" % (picks[0] if picks else None))
    if picks:
        check(picks[0].get("u") == u and picks[0].get("n") == 1,
              "событие несёт вид апгрейда и сколько стало",
              "u=%s n=%s" % (picks[0].get("u"), picks[0].get("n")))
    left = [t for t in w.entities.values() if items.is_item(t.kind)]
    check(not left,
          "ЦЕНА: взял один — два других апгрейда этажа погасли",
          "было %d, осталось %d" % (items.ALTAR_SLOTS, len(left)))
    snaps = conn.msgs_of("snap", n_msgs)
    rm = []
    for s in snaps:
        rm.extend(s.get("rm", []))
    check(sorted(rm) == sorted(t.id for t in its),
          "снапшот показал исчезновение всех трёх (rm, 5.2)",
          "rm=%s" % sorted(rm))

    # дух предметов не берёт (8.5)
    room2 = Room("GHST", settings=RoomSettings(seed=777))
    p2 = Player(1, "Дух", _Conn())
    room2.add(p2)
    room2.start()
    w2 = room2.world
    g = w2.entities[p2.ent_id]
    it2 = min((t for t in w2.entities.values() if items.is_item(t.kind)),
              key=lambda t: t.id)
    g.x, g.y = it2.x, it2.y
    combat.kill(w2, g)
    p2.pending = proto.parse_client(raw)
    room2.tick()
    check(items.total(g) == 0 and
          len([t for t in w2.entities.values() if items.is_item(t.kind)]) == 3,
          "дух предметов не берёт (8.5: он и урона не наносит)",
          "набор духа %s, предметов на месте %d"
          % (items.describe(g),
             len([t for t in w2.entities.values() if items.is_item(t.kind)])))
    return True


# --- 3. каждый апгрейд числами --------------------------------------------

def measure_harvest(stacks):
    """Сколько ударов рубаки игрок выдерживает, вычищая этаж из 8 врагов.

    Последовательность жёсткая и одинаковая для всех прогонов: получил удар
    (20, 4.2) -> убил врага (2 удара по 20 в 40 hp). Считаем удары, пока жив.
    Это и есть «эффективный запас здоровья за этаж».
    """
    w = make_world()
    e = add_player(w, 8.5, 6.5, ups=[(items.U_HARVEST, stacks)] if stacks else [])
    taken = 0
    kills = 0
    while e.hp > 0:
        combat.damage(w, e, combat.MELEE_DMG, None, 0)
        taken += 1
        if e.hp <= 0:
            break
        if kills < 8:                   # этаж — это 8 врагов (8.1)
            t = add_enemy(w, 9.2, 6.5, hp=ai.MELEE_HP)
            combat.damage(w, t, combat.MELEE_DMG, e)
            combat.damage(w, t, combat.MELEE_DMG, e)
            kills += 1
    return taken, kills


def measure_heal_per_kill(stacks):
    """Сколько hp возвращает ОДНО убийство. Прямая мерка складывания."""
    w = make_world()
    e = add_player(w, 8.5, 6.5, hp=10,
                   ups=[(items.U_HARVEST, stacks)] if stacks else [])
    t = add_enemy(w, 9.2, 6.5, hp=combat.MELEE_DMG)
    hp0 = e.hp
    combat.damage(w, t, combat.MELEE_DMG, e)
    return e.hp - hp0


def measure_ram(stacks, n_bodies=3):
    """Урон одного рывка сквозь строй. Возвращает (суммарный, по телам)."""
    w = make_world()
    e = add_player(w, 4.5, 6.5, ups=[(items.U_RAM, stacks)] if stacks else [])
    bodies = [add_enemy(w, 5.4 + i * 0.8, 6.5, hp=100) for i in range(n_bodies)]
    e.mv = (1.0, 0.0)
    press(w, e, proto.BTN_DASH, mv=(1.0, 0.0))
    for _ in range(combat.DASH_TICKS + 2):
        w.step()
    dmg = [b.hp_max - b.hp for b in bodies]
    return sum(dmg), dmg


def measure_dash_cd(stacks):
    """Откат рывка НА САМОМ ДЕЛЕ: через сколько тиков разрешён следующий.

    Считается от тика, на котором рывок начался (combat.begin сравнивает
    tick >= dash_ready), поэтому это ровно то число, которое стоит в
    items.dash_cd, — и если они разойдутся, разойдутся и эти строки.
    """
    w = make_world()
    e = add_player(w, 4.5, 6.5, ups=[(items.U_WIND, stacks)] if stacks else [])
    press(w, e, proto.BTN_DASH, mv=(1.0, 0.0))
    return e.dash_ready - w.tick


def measure_ricochet(stacks):
    """Выстрел в стену перед собой: вернулся ли он и достал ли того, кто сзади."""
    # ГЕОМЕТРИЯ ВЫВЕДЕНА ИЗ ДАЛЬНОСТИ: снаряд живёт 14 клеток (4.2), значит
    # «до стены и обратно» обязано в них уложиться, иначе красным станет ttl,
    # а не рикошет. Стена на x=23, стрелок на 20.5 (2.5 клетки), цель на 16.5
    # (ещё 4.0): путь 6.5 клетки, запас вдвое.
    w = make_world(open_room(24, 12))
    e = add_player(w, 20.5, 5.5, facing=0.0,
                   ups=[(items.U_RICO, stacks)] if stacks else [])
    behind = add_enemy(w, 16.5, 5.5, hp=100)
    e.aim = (22.0, 5.5)
    press(w, e, proto.BTN_SHOOT)
    booms = 0
    for _ in range(combat.SHOT_TICKS + 4):
        w.step()
        for tick, kind, kw in w.events:
            if kind == "boom":
                booms += 1
        del w.events[:]
    return behind.hp_max - behind.hp, booms


def test_each_upgrade():
    print("\n--- 3. каждый апгрейд: что было, что стало, во сколько раз ---")

    # --- Жатва
    base_taken, base_kills = measure_harvest(0)
    one_taken, _ = measure_harvest(1)
    two_taken, _ = measure_harvest(2)
    note("Жатва: убийство лечит на %d hp за стак" % items.HARVEST_HEAL,
         "этаж из 8 рубак, удар рубаки %d (4.2)" % combat.MELEE_DMG)
    check(one_taken > base_taken,
         "Жатва: ударов рубаки выдержано за этаж  %d -> %d  (%s)"
          % (base_taken, one_taken, ratio(base_taken, one_taken)))
    check(two_taken > one_taken,
          "Жатва СКЛАДЫВАЕТСЯ: два стака  %d -> %d ударов  (%s к голому)"
          % (one_taken, two_taken, ratio(base_taken, two_taken)))
    h1 = measure_heal_per_kill(1)
    h2 = measure_heal_per_kill(2)
    check(h2 == 2 * h1 == 2 * items.HARVEST_HEAL,
          "второй стак Жатвы даёт РОВНО вдвое: %d -> %d hp за убийство (%s)"
          % (h1, h2, ratio(h1, h2)),
          "мерка прямая: «ударов за этаж» сверху упирается в потолок hp_max "
          "и в 8 врагов этажа, поэтому складывание считается по лечению")

    # --- Таран
    b0, d0 = measure_ram(0)
    b1, d1 = measure_ram(1)
    b2, d2 = measure_ram(2)
    note("Таран: рывок бьёт тела на пути на %d за стак" % items.RAM_DMG,
         "ближний удар для сравнения %d (4.2)" % combat.MELEE_DMG)
    check(b0 == 0 and b1 > 0,
          "Таран: урон рывка сквозь 3 тела  %d -> %d  (%s)"
          % (b0, b1, ratio(b0, b1)),
          "по телам %s -> %s" % (d0, d1))
    check(b2 == 2 * b1,
          "Таран СКЛАДЫВАЕТСЯ: два стака  %d -> %d  (%s)"
          % (b1, b2, ratio(b1, b2)), "по телам %s" % d2)
    check(all(x == items.RAM_DMG for x in d1),
          "рывок бьёт каждое тело РОВНО РАЗ, а не каждый тик",
          "5 тиков рывка, снято по %s (не %d)"
          % (d1, items.RAM_DMG * combat.DASH_TICKS))
    check(d2[0] >= ai.MELEE_HP,
          "два стака Тарана убивают рубаку одним рывком (40 hp, 4.2)",
          "снято %d при hp рубаки %d" % (d2[0], ai.MELEE_HP))

    # --- Ветер
    c0 = measure_dash_cd(0)
    c1 = measure_dash_cd(1)
    c2 = measure_dash_cd(2)
    note("Ветер: откат рывка делится на (1 + стаки), пол %d тиков"
         % items.DASH_CD_FLOOR,
         "база 4.2 — %d тиков = %.2f с" % (combat.DASH_CD_TICKS,
                                           combat.DASH_CD_TICKS * DT))
    check(c1 < c0,
          "Ветер: откат рывка  %d -> %d тиков (%.2f -> %.2f с), рывков в с %.2f -> %.2f"
          % (c0, c1, c0 * DT, c1 * DT, 1.0 / (c0 * DT), 1.0 / (c1 * DT)))
    check(c2 < c1,
          "Ветер СКЛАДЫВАЕТСЯ: два стака  %d -> %d тиков, рывков в с %.2f -> %.2f (%s)"
          % (c1, c2, 1.0 / (c1 * DT), 1.0 / (c2 * DT),
             ratio(1.0 / (c0 * DT), 1.0 / (c2 * DT))))
    check(abs((1.0 / (c2 * DT) - 1.0 / (c1 * DT)) -
              (1.0 / (c1 * DT) - 1.0 / (c0 * DT))) < 0.35,
          "складывается ЛИНЕЙНО по частоте рывков, а не по откату",
          "прибавка рывков в секунду: +%.2f и +%.2f"
          % (1.0 / (c1 * DT) - 1.0 / (c0 * DT),
             1.0 / (c2 * DT) - 1.0 / (c1 * DT)))

    # --- Рикошет
    r0, boom0 = measure_ricochet(0)
    r1, boom1 = measure_ricochet(1)
    note("Рикошет: снаряд отскакивает %d раза за стак вместо гибели о стену"
         % items.RICO_BOUNCES,
         "урон снаряда %d, дальность %.0f клеток (4.2)"
         % (combat.SHOT_DMG, combat.SHOT_RANGE))
    check(r0 == 0 and r1 == combat.SHOT_DMG,
          "Рикошет: выстрел в стену перед собой достаёт того, кто СЗАДИ  %d -> %d урона"
          % (r0, r1), "%s" % ratio(r0, r1))
    check(boom0 == 1 and boom1 == 0,
          "выстрел в стену перестал быть потерей: гибели о стену %d -> %d"
          % (boom0, boom1))
    w = make_world(open_room(24, 12))
    e = add_player(w, 20.5, 5.5, ups=[(items.U_RICO, 2)])
    e.aim = (22.0, 5.5)
    press(w, e, proto.BTN_SHOOT)
    shot = [t for t in w.entities.values() if t.kind == W.K_SHOT]
    check(shot and shot[0].bounce == items.RICO_BOUNCES * 2,
          "Рикошет СКЛАДЫВАЕТСЯ: отскоков у снаряда %d -> %d"
          % (items.RICO_BOUNCES, shot[0].bounce if shot else -1),
          "%s" % ratio(items.RICO_BOUNCES, shot[0].bounce if shot else 0))
    return True


# --- 4. разные апгрейды сочетаются, а не затирают друг друга --------------

def test_mixed():
    print("\n--- 4. разные апгрейды сочетаются (8.4: складываются, не заменяют) ---")
    mix = [(items.U_HARVEST, 1), (items.U_RAM, 1), (items.U_WIND, 1)]
    w = make_world()
    e = add_player(w, 4.5, 6.5, hp=50, ups=mix)
    check(items.describe(e) == [(0, 1), (1, 1), (3, 1)],
          "три разных апгрейда лежат рядом, ни один не затёрт",
          "%s" % [(set_name(u), n) for u, n in items.describe(e)])

    bodies = [add_enemy(w, 5.4 + i * 0.8, 6.5, hp=20) for i in range(2)]
    hp0 = e.hp
    press(w, e, proto.BTN_DASH, mv=(1.0, 0.0))
    for _ in range(combat.DASH_TICKS + 2):
        w.step()
    killed = sum(1 for b in bodies if b.hp <= 0)
    cd_left = e.dash_ready - w.tick          # сколько ещё откатываться
    cd = items.dash_cd(e, combat.DASH_CD_TICKS)
    check(killed == 2,
          "Таран сработал в смеси: рывок убил обоих (20 hp тела)",
          "убито %d из 2" % killed)
    check(e.hp == hp0 + 2 * items.HARVEST_HEAL,
          "Жатва сработала в той же смеси: убийства ТАРАНОМ лечат",
          "hp %d -> %d (+%d за два убийства)"
          % (hp0, e.hp, e.hp - hp0))
    check(cd <= combat.DASH_CD_TICKS // 2 + 1 and cd_left < cd,
          "Ветер сработал в той же смеси: откат %d тиков вместо %d"
          % (cd, combat.DASH_CD_TICKS),
          "на момент замера до следующего рывка оставалось %d тиков" % cd_left)
    # разные виды не мешают друг другу считаться
    check(items.dash_dmg(e) == items.RAM_DMG and
          items.kill_heal(e) == items.HARVEST_HEAL and
          items.shot_bounces(e) == 0,
          "чужой апгрейд не добавляет и не отнимает у соседнего",
          "таран %d, жатва %d, рикошет %d (Рикошета не брали)"
          % (items.dash_dmg(e), items.kill_heal(e), items.shot_bounces(e)))
    return True


# --- 5. накопленное переживает спуск (8.1, 8.5) ---------------------------

def test_descend():
    print("\n--- 5. накопленное переживает спуск (8.4 внутри ЗАБЕГА, не этажа) ---")
    room = Room("DEEP", settings=RoomSettings(seed=4242))
    conn = _Conn()
    p = Player(1, "Один", conn)
    room.add(p)
    room.start()
    w = room.world
    e = w.entities[p.ent_id]
    items.grant(e, items.U_RAM, 2)
    items.grant(e, items.U_WIND, 1)
    items.grant(e, items.U_HARVEST, 1)
    e.hp = 61
    before = items.describe(e)
    cd_before = items.dash_cd(e, combat.DASH_CD_TICKS)
    ram_before = items.dash_dmg(e)
    heal_before = items.kill_heal(e)
    floor_before = room.floor
    n_items_before = len([t for t in w.entities.values()
                          if items.is_item(t.kind)])

    # встали на лестницу и спустились штатным путём
    e.x, e.y = w.stairs[0] + 0.5, w.stairs[1] + 0.5
    room.tick()
    check(room.floor == floor_before + 1, "спустились",
          "этаж %d -> %d" % (floor_before, room.floor))
    after = items.describe(e)
    check(after == before, "набор апгрейдов тот же после спуска",
          "%s -> %s" % ([(set_name(u), n) for u, n in before],
                        [(set_name(u), n) for u, n in after]))
    check(items.dash_cd(e, combat.DASH_CD_TICKS) == cd_before and
          items.dash_dmg(e) == ram_before and
          items.kill_heal(e) == heal_before,
          "эффекты те же числом, а не только записью",
          "откат %d тиков, таран %d, жатва %d — до и после совпали"
          % (cd_before, ram_before, heal_before))
    # и они ДЕЙСТВИТЕЛЬНО работают на новом этаже
    w2 = room.world
    b = add_enemy(w2, e.x + 0.9, e.y, hp=100)
    e.mv = (1.0, 0.0)
    press(w2, e, proto.BTN_DASH, mv=(1.0, 0.0))
    for _ in range(combat.DASH_TICKS + 2):
        w2.step()
    check(b.hp_max - b.hp == ram_before,
          "Таран бьёт на новом этаже ровно так же",
          "снято %d (ожидали %d)" % (b.hp_max - b.hp, ram_before))
    w2.remove(b.id)
    n_items_after = len([t for t in w2.entities.values()
                         if items.is_item(t.kind)])
    check(n_items_before == items.ALTAR_SLOTS and
          n_items_after == items.ALTAR_SLOTS,
          "на новом этаже свой алтарь, старые предметы не переехали",
          "предметов было %d, стало %d" % (n_items_before, n_items_after))

    # смерть набор тоже не отнимает: дух воскресает со своим
    hp_keep = items.describe(e)
    combat.kill(room.world, e)
    fl2 = room.floor
    fl_gen = gen.generate(room.settings.seed, fl2 + 1, 64, 48)
    room.settings.floor = fl2 + 1
    room.world.enter_floor(fl2 + 1, fl_gen.grid, fl_gen.spawns, fl_gen.stairs)
    check(items.describe(e) == hp_keep and not (e.flags & W.F_DEAD),
          "смерть апгрейды не отнимает: дух воскресает со своим набором",
          "%s, hp %d" % ([(set_name(u), n) for u, n in items.describe(e)], e.hp))
    return True


# --- 6. баланс: неуязвимым игрок не становится ----------------------------

def hits_to_die(ups):
    w = make_world()
    e = add_player(w, 8.5, 6.5, ups=ups)
    n = 0
    while e.hp > 0 and n < 100:
        combat.damage(w, e, combat.MELEE_DMG, None, 0)
        n += 1
    return n


def crowd_fight(ups, n_enemies, max_ticks=3600, dash=True):
    """Игрок в кольце рубак. Обе стороны бьют по своим часам 4.2.

    ВРАГИ НЕ ОТСТАЮТ: каждый тик они переставляются на кольцо радиуса
    MELEE_STOP вокруг игрока. Это НАРОЧНО худший случай, а не модель ИИ:
    настоящий рубака (3.2 кл/с) медленнее игрока (5.0) и в реальном бою
    отстаёт, а рывок от него и вовсе уносит. Если бы враги стояли на месте,
    игрок с Тараном просто улетал бы из кольца и добивал их поодиночке —
    проверка «не становится ли он неуязвимым» мерила бы кайтинг, а не
    неуязвимость. Здесь убежать нельзя вовсе.

    Условие остановки — ПРОГРЕСС (кто-то умер), а не секунды: раздел 10.
    Возвращает (жив ли игрок, тиков, убито врагов, hp игрока).
    """
    w = make_world(open_room(40, 24))
    e = add_player(w, 20.5, 12.5, ups=ups)
    foes = []
    for i in range(n_enemies):
        a = 2.0 * math.pi * i / n_enemies
        foes.append(add_enemy(w, 20.5 + ai.MELEE_STOP * math.cos(a),
                              12.5 + ai.MELEE_STOP * math.sin(a),
                              hp=ai.MELEE_HP))
    t = 0
    while t < max_ticks:
        alive = [f for f in foes if f.hp > 0 and not (f.flags & W.F_DEAD)]
        if not alive or e.hp <= 0:
            break
        near = min(alive, key=lambda f: (f.x - e.x) ** 2 + (f.y - e.y) ** 2)
        e.aim = (near.x, near.y)
        e.btn = proto.BTN_ATTACK | (proto.BTN_DASH if dash else 0)
        for i, f in enumerate(alive):
            a = 2.0 * math.pi * i / len(alive)
            f.x = e.x + ai.MELEE_STOP * math.cos(a)
            f.y = e.y + ai.MELEE_STOP * math.sin(a)
            f.facing = math.atan2(e.y - f.y, e.x - f.x)
            if not f.atk_hit and w.tick >= f.atk_ready:
                combat.start_melee(w, f, ai.MELEE_WIND, ai.MELEE_CD)
        w.step()
        del w.events[:]
        t += 1
    killed = sum(1 for f in foes if f.hp <= 0)
    return e.hp > 0, t, killed, e.hp


def test_balance():
    print("\n--- 6. баланс: игрок с набором апгрейдов не становится неуязвимым ---")
    typical = [(items.U_HARVEST, 2), (items.U_RAM, 1), (items.U_WIND, 1)]
    note("типичный набор за 4 этажа", "%s"
         % ", ".join("%s x%d" % (set_name(u), n) for u, n in typical))
    bare = hits_to_die([])
    full = hits_to_die(typical)
    check(bare == full == 5,
          "ударов рубаки ПОДРЯД до смерти: %d без набора, %d с набором"
          % (bare, full),
          "100 hp / %d урона (4.2); ни один апгрейд не даёт брони и hp"
          % combat.MELEE_DMG)
    for n in (1, 2, 3, 5, 8):
        alive_b, t_b, k_b, hp_b = crowd_fight([], n)
        alive_f, t_f, k_f, hp_f = crowd_fight(typical, n)
        note("кольцо из %d рубак" % n,
             "голый: %s, %d убито, %.1f с, hp %d | с набором: %s, %d убито, %.1f с, hp %d"
             % ("выжил" if alive_b else "погиб", k_b, t_b * DT, hp_b,
                "выжил" if alive_f else "погиб", k_f, t_f * DT, hp_f))
        if n == 8:
            check(not alive_f,
                  "с полным набором игрок всё равно гибнет в толпе из 8",
                  "прожил %.1f с, успел убить %d" % (t_f * DT, k_f))
        if n == 1:
            check(alive_f, "в честной дуэли 1 на 1 набор даёт победу",
                  "hp на выходе %d из 100" % hp_f)

    # откуда взялось выживание: из ИГРЫ, а не из цифр в наборе
    alive_nd, t_nd, k_nd, hp_nd = crowd_fight(typical, 5, dash=False)
    check(not alive_nd,
          "тот же набор БЕЗ рывка гибнет уже в кольце из 5",
          "прожил %.1f с, убил %d — выживание даёт игра, а не сложенные числа"
          % (t_nd * DT, k_nd))

    # --- потолок неуязвимости: самый злой набор, какой вообще собирается ---
    print("  -- доля времени в неуязвимости рывка по стакам Ветра "
          "(потолок 1/%d отката, см. items.DASH_INV_SHARE) --"
          % items.DASH_INV_SHARE)
    shares = []
    for k in range(0, 5):
        e = add_player(make_world(), 4.5, 4.5,
                       ups=[(items.U_WIND, k)] if k else [])
        cd = items.dash_cd(e, combat.DASH_CD_TICKS)
        inv = items.dash_inv(combat.DASH_INV_TICKS, cd)
        shares.append(inv / float(cd))
        note("Ветер x%d" % k,
             "откат %2d тиков, рывков в с %.2f, неуязвимость %d тиков = %.0f%% времени"
             % (cd, 30.0 / cd, inv, 100.0 * inv / cd))
    check(max(shares) <= 1.0 / items.DASH_INV_SHARE + 1e-9,
          "неуязвимость не занимает больше половины отката ни при каком Ветре",
          "максимум %.0f%% (без ограничения было бы 71%% и кольцо из 12 "
          "выходило бы живым и со 100 hp — замерено)" % (100.0 * max(shares)))
    worst = [(items.U_WIND, 3), (items.U_RAM, 1), (items.U_HARVEST, 2)]
    note("самый злой набор за 6 этажей",
         "%s" % ", ".join("%s x%d" % (set_name(u), n) for u, n in worst))
    for n in (5, 8, 12):
        alive_w, t_w, k_w, hp_w = crowd_fight(worst, n)
        note("кольцо из %d рубак, злой набор" % n,
             "%s, убито %d, %.1f с, hp %d"
             % ("выжил" if alive_w else "погиб", k_w, t_w * DT, hp_w))
        if n == 8:
            check(not alive_w,
                  "даже самый злой набор гибнет в кольце из 8",
                  "прожил %.1f с, убил %d из 8" % (t_w * DT, k_w))
    return True


# --- 7. цена в тике -------------------------------------------------------

def test_cost():
    print("\n--- 7. цена в тике (2.2) ---")
    # Таран — единственный апгрейд с перебором по сущностям
    def run(ups, n_mobs=200, ticks=120):
        w = make_world(open_room(64, 48))
        e = add_player(w, 32.5, 24.5, ups=ups)
        rnd = 12345
        for i in range(n_mobs):
            rnd = (rnd * 1103515245 + 12345) & 0x7FFFFFFF
            x = 2.0 + (rnd % 6000) / 100.0
            rnd = (rnd * 1103515245 + 12345) & 0x7FFFFFFF
            y = 2.0 + (rnd % 4400) / 100.0
            add_enemy(w, x, y, hp=1000)
        samples = []
        clock = time.perf_counter
        for i in range(ticks):
            e.btn = proto.BTN_DASH
            e.mv = (1.0 if (i // 20) % 2 == 0 else -1.0, 0.0)
            t0 = clock()
            w.step()
            samples.append((clock() - t0) * 1000.0)
            del w.events[:]
        return statistics.median(samples)

    m0 = run([])
    m1 = run([(items.U_RAM, 1), (items.U_WIND, 3)])
    note("шаг мира, 200 врагов, игрок рвётся без остановки",
         "без Тарана %.3f мс, с Тараном+Ветром %.3f мс, разница %+.3f мс"
         % (m0, m1, m1 - m0))
    check(m1 - m0 < 1.0,
          "Таран стоит меньше 1 мс на 200 сущностях (бюджет тика 12 мс, 2.2)",
          "прибавка %+.3f мс = %.1f%% бюджета" % (m1 - m0, (m1 - m0) / 12.0 * 100))

    # вторая волна в генераторе — раз на этаж, не в тике
    t0 = time.perf_counter()
    for seed in range(20):
        gen.generate(seed + 1, 1, 64, 48)
    t_gen = (time.perf_counter() - t0) / 20.0 * 1000.0
    fl = gen.generate(1, 1, 64, 48)
    t1 = time.perf_counter()
    for _ in range(20):
        gen.distance_map(fl.grid, [fl.stairs])
    t_wave = (time.perf_counter() - t1) / 20.0 * 1000.0
    note("цена алтаря в генераторе",
         "вся генерация этажа %.2f мс, из них вторая волна (от лестницы) %.2f мс"
         % (t_gen, t_wave))
    check(t_gen < 50.0,
          "генерация этажа с алтарём укладывается в 2.6 (5 с до экрана)",
          "%.2f мс на этаж, запас x%.0f" % (t_gen, 5000.0 / t_gen))
    print("  uptime: %s" % uptime_line())
    return True


def main():
    print("=" * 70)
    print("Приёмка предметов и апгрейдов (8.4). Апгрейдов в наборе: %d — %s"
          % (items.N_UP, ", ".join(items.NAMES)))
    print("=" * 70)
    test_altar()
    test_pickup_protocol()
    test_each_upgrade()
    test_mixed()
    test_descend()
    test_balance()
    test_cost()
    print("\n" + "=" * 70)
    if FAILS:
        print("КРАСНО: %d" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
