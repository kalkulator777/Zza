#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка опыта, уровней и цены смерти (DESIGN.md 8.4a, 8.5, дыра 11.8).

Что здесь проверяется и почему именно так.

  1. ОПЫТ НАЧИСЛЯЕТСЯ И РАВЕН hp_max ЦЕЛИ. Не «какое-то число за убийство»,
     а ровно цена цели в ударах: рубака 40, стрелок 25, босс 400 (4.2).
     Проверяется через combat.damage — единую точку урона, — то есть
     убийство ударом, снарядом и тараном обязано стоить одинаково.
  2. КРИВАЯ РАСТЁТ ЛИНЕЙНО, И ЭТО ФОРМА, А НЕ ЧИСЛО. Уровень L стоит
     XP_STEP*L. Печатается, сколько уровней даёт квадратично растущий приток
     опыта (8.1: врагов 8 + 4*(этаж-1)) — число уровней обязано выйти
     ЛИНЕЙНЫМ по этажу, иначе к 20-му этажу их вчетверо больше, чем к 10-му.
  3. УРОВЕНЬ ДАЁТ ВЫБОР. Вокруг игрока ложатся LEVEL_SLOTS предметов РАЗНЫХ
     видов, все в пределах подбора (PICK_R), и они ЕГО: напарник их взять не
     может. Взял один — остальные погасли, как на алтаре.
  4. УРОВЕНЬ ЛЕЧИТ НА LEVEL_HEAL И НЕ БОЛЬШЕ. Число сверено с Жатвой: за то
     же число убийств уровень обязан лечить МЕНЬШЕ одного стака Жатвы,
     иначе Жатву не возьмёт никто.
  5. АЛТАРИ ОСТАЛИСЬ. Уровень — второй источник, а не замена: на этаже
     по-прежнему лежат алтари, и апгрейд с алтаря складывается с апгрейдом
     с уровня.
  6. ЦЕНА СМЕРТИ. Воскрешение тратит общий заряд группы и даёт половину
     здоровья; заряды кончились — дух остаётся духом; босс заряд возвращает.
     (Спуск и лестница — в tests/floor_descend.py, здесь только цена.)
  7. ЦЕНА В ТИКЕ. Опыт живёт в единой точке урона и в очереди; меряется на
     этаже из 200 сущностей, с опытом и без.

Карты свои, маленькие (раздел 10: проверка боя, берущая карту у генератора,
краснеет от чужой работы). Генератор берётся только там, где проверяются
алтари на настоящем этаже.

ПОДСАДКИ. Файл умеет краснеть по требованию: `python3 tests/xp_check.py
--break=xp|level|revive` ломает ровно одну охраняемую вещь и обязан дать
красное. Это не режим игры, это доказательство, что проверка не зеленеет на
сломанном коде (правило 7).

Запуск:  python3 tests/xp_check.py
Выход: 0 — зелено, 1 — красно.
"""

import argparse
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import ai, boss, combat, gen, items, physics, proto   # noqa: E402
from server import world as world_mod                             # noqa: E402

W = world_mod
DT = world_mod.DT
CLOCK = time.perf_counter
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


# --- арена -----------------------------------------------------------------

def open_room(w=28, h=14):
    g = physics.Grid(w, h)
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            g.set(x, y, physics.TILE_FLOOR)
    return g


def make_world(grid=None):
    return W.World(grid or open_room(), seed=1, floor=1, spawns=[(2.5, 2.5)])


def add_player(w, x, y, hp=100, facing=0.0):
    return w.spawn(W.K_PLAYER, x, y, r=W.R_PLAYER, speed=W.SPEED_RUN,
                   ctl=True, hp=hp, hp_max=100, facing=facing)


def add_enemy(w, x, y, hp=40, ranged=False):
    kind = W.K_ENEMY_RANGED if ranged else W.K_ENEMY
    return w.spawn(kind, x, y, r=ai.ENEMY_R, speed=0.0, hp=hp, hp_max=hp)


def kill_enemies(w, e, n, hp=40, ranged=False):
    """Убить n врагов рукой игрока e, прогоняя настоящий тик между ними."""
    for _ in range(n):
        t = add_enemy(w, e.x + 0.6, e.y, hp=hp, ranged=ranged)
        combat.damage(w, t, t.hp, e)
        w.step()


def items_on(w, owner=None):
    out = []
    for t in w.entities.values():
        if not items.is_item(t.kind):
            continue
        if owner is not None and t.owner != owner:
            continue
        out.append(t)
    return out


# --- 1. опыт равен цене цели ----------------------------------------------

def test_xp_is_price():
    print("\n--- 1. опыт за убийство равен hp_max цели (4.2) ---")
    w = make_world()
    e = add_player(w, 8.5, 6.5)
    before = e.xp
    t = add_enemy(w, 9.2, 6.5, hp=ai.MELEE_HP)
    combat.damage(w, t, t.hp, e)
    got_melee = e.xp - before
    check(got_melee == ai.MELEE_HP,
          "рубака стоит %d опыта — ровно своё здоровье" % ai.MELEE_HP,
          "начислено %d" % got_melee)

    w = make_world()
    e = add_player(w, 8.5, 6.5)
    t = add_enemy(w, 9.2, 6.5, hp=ai.RANGED_HP, ranged=True)
    combat.damage(w, t, t.hp, e)
    check(e.xp == ai.RANGED_HP,
          "стрелок стоит %d — дешевле рубаки ровно во столько, "
          "во сколько меньше бить" % ai.RANGED_HP,
          "начислено %d, отношение %.2f"
          % (e.xp, ai.MELEE_HP / float(ai.RANGED_HP or 1)))

    w = make_world()
    e = add_player(w, 8.5, 6.5)
    b = w.spawn(W.K_BOSS, 10.5, 6.5, r=0.5, hp=boss.BOSS_HP,
                hp_max=boss.BOSS_HP)
    combat.damage(w, b, b.hp, e)
    lvl_from_boss = e.lvl
    check(e.xp + items.xp_total_for(e.lvl) == boss.BOSS_HP,
          "босс стоит %d — столько же, сколько %.1f рядовых"
          % (boss.BOSS_HP, boss.BOSS_HP / 35.0),
          "опыт %d + за %d уровней %d"
          % (e.xp, lvl_from_boss, items.xp_total_for(lvl_from_boss)))

    # НЕ ЗА ПОПАДАНИЕ, А ЗА ДОБИВАНИЕ: тот же довод, что у Жатвы (8.4).
    w = make_world()
    e = add_player(w, 8.5, 6.5)
    t = add_enemy(w, 9.2, 6.5, hp=100)
    combat.damage(w, t, 20, e)
    half = e.xp
    combat.damage(w, t, 80, e)
    check(half == 0 and e.xp == 100,
          "опыт даёт ДОБИВАНИЕ, а не попадание",
          "после 20 из 100: %d, после добивания: %d" % (half, e.xp))

    # дух не растёт (8.5)
    w = make_world()
    e = add_player(w, 8.5, 6.5)
    combat.kill(w, e)
    kill_enemies(w, e, 3)
    check(e.xp == 0 and e.lvl == 0,
          "дух опыта не получает: он не дерётся (8.5)",
          "xp %d, уровень %d" % (e.xp, e.lvl))


# --- 2. форма кривой -------------------------------------------------------

def floor_xp(floor):
    """Весь опыт этажа, если зачистить его целиком (8.1 + ai.count_for)."""
    n = ai.count_for(floor)
    # средний рядовой: две трети рубак, треть стрелков (ai.RANGED_EVERY)
    avg = ((ai.RANGED_EVERY - 1) * ai.MELEE_HP + ai.RANGED_HP) / float(ai.RANGED_EVERY)
    xp = n * avg
    if boss.on_floor(floor):
        xp += boss.BOSS_HP
    return xp


def lvl_at(xp):
    """Сколько уровней даёт столько опыта."""
    lvl = 0
    left = xp
    while left >= items.level_cost(lvl):
        left -= items.level_cost(lvl)
        lvl += 1
    return lvl


def test_curve_shape():
    print("\n--- 2. форма кривой: приток квадратичный, уровни линейные ---")
    avg = ((ai.RANGED_EVERY - 1) * ai.MELEE_HP + ai.RANGED_HP) / float(ai.RANGED_EVERY)
    note("опыт цели = hp_max; средний рядовой %.1f hp; шаг порога %d (=%.1f рядовых)"
         % (avg, items.XP_STEP, items.XP_STEP / avg),
         "уровень L стоит %d*L, то есть %.0f*L рядовых"
         % (items.XP_STEP, items.XP_STEP / avg))
    cum = 0.0
    rows = []
    for f in range(1, 21):
        cum += floor_xp(f)
        rows.append((f, cum, lvl_at(cum)))
    for f, cum, lv in rows:
        if f in (1, 5, 10, 15, 20):
            note("этаж %2d: полная зачистка даёт %6.0f опыта -> %2d уровней"
                 % (f, cum, lv))
    l5 = dict((f, lv) for f, _c, lv in rows)[5]
    l10 = dict((f, lv) for f, _c, lv in rows)[10]
    l20 = dict((f, lv) for f, _c, lv in rows)[20]
    # ЛИНЕЙНОСТЬ — ЭТО И ЕСТЬ ПРОВЕРКА. При ПОСТОЯННОМ пороге число уровней
    # росло бы как квадрат этажа: 20-й дал бы вчетверо больше 10-го. Порог
    # ниже выведен от этого: отношение обязано быть около 2, а не около 4.
    k = l20 / float(l10 or 1)
    check(1.6 <= k <= 2.6,
          "уровней к 20-му этажу против 10-го: %d / %d = %.2f — рост ЛИНЕЙНЫЙ"
          % (l20, l10, k),
          "постоянный порог дал бы %.2f (квадрат), порог падал бы ниже 1.6"
          % ((floor_sum(20) / floor_sum(10)) if floor_sum(10) else 0))
    k2 = l10 / float(l5 or 1)
    check(1.6 <= k2 <= 2.8,
          "и к 10-му против 5-го: %d / %d = %.2f" % (l10, l5, k2))
    check(l10 <= 3 * l5 and l20 <= 2.5 * l10,
          "изобилия по построению нет: уровни не ускоряются с глубиной",
          "5-й %d, 10-й %d, 20-й %d уровней (полная зачистка — ВЕРХНЯЯ оценка)"
          % (l5, l10, l20))


def floor_sum(upto):
    return sum(floor_xp(f) for f in range(1, upto + 1))


# --- 3. уровень даёт выбор -------------------------------------------------

def test_level_offers_choice():
    print("\n--- 3. уровень даёт ВЫБОР, а не подарок ---")
    w = make_world()
    e = add_player(w, 8.5, 6.5, hp=100)
    mate = add_player(w, 8.5, 7.5, hp=100)
    kill_enemies(w, e, 3)               # 3*40 = 120 >= порог первого уровня
    check(e.lvl == 1, "три рубаки дают первый уровень (порог %d опыта)"
          % items.level_cost(0), "уровень %d, опыт %d" % (e.lvl, e.xp))
    lying = items_on(w)
    check(len(lying) == items.LEVEL_SLOTS,
          "на уровне легло %d предметов" % items.LEVEL_SLOTS,
          "видов %s" % sorted(t.kind for t in lying))
    check(len(set(t.kind for t in lying)) == len(lying),
          "предметы РАЗНЫХ видов: выбор, а не три одинаковых",
          "виды %s" % [items.NAMES[items.up_of(t.kind)] for t in lying])
    dists = sorted(round(((t.x - e.x) ** 2 + (t.y - e.y) ** 2) ** 0.5, 2)
                   for t in lying)
    check(all(d <= items.PICK_R for d in dists),
          "весь выбор лежит В ПРЕДЕЛАХ подбора (%.2f): за своим уровнем "
          "ходить не надо" % items.PICK_R,
          "расстояния %s" % dists)
    check(all(t.owner == e.id for t in lying),
          "выбор ЧЕЙ-ТО: напарник его не возьмёт",
          "owner %s при id игрока %d" % (sorted(set(t.owner for t in lying)),
                                         e.id))
    mate.x, mate.y = e.x, e.y           # напарник встал прямо в выбор
    check(items.nearest_item(w, mate) is None,
          "напарник, стоящий В ЧУЖОМ выборе, не видит ни одного предмета",
          "ему видно %d" % len(items_on(w, owner=mate.id)))

    # взял один — остальные погасли, как на алтаре (8.4)
    items.request_pickup(w, e)
    w.step()
    check(items.total(e) == 1 and not items_on(w),
          "взял один — два других погасли (цена выбора, как на алтаре)",
          "набор %s, на полу осталось %d"
          % (items.describe(e), len(items_on(w))))

    # долг не теряется: два уровня подряд — два выбора по очереди
    w = make_world()
    e = add_player(w, 8.5, 6.5)
    kill_enemies(w, e, 8)               # 320 опыта = уровни 1 и 2
    check(e.lvl == 2, "восемь рубак дают второй уровень", "уровень %d" % e.lvl)
    check(len(items_on(w)) == items.LEVEL_SLOTS and e.lvl_owed == 1,
          "на полу ОДИН выбор, второй ждёт долгом (не девять кружков в куче)",
          "лежит %d, долг %d" % (len(items_on(w)), e.lvl_owed))
    items.request_pickup(w, e)
    w.step()
    w.step()
    check(len(items_on(w)) == items.LEVEL_SLOTS and e.lvl_owed == 0,
          "взял первый — второй выбор выложился сам, уровень не потерян",
          "лежит %d, долг %d, набор %s"
          % (len(items_on(w)), e.lvl_owed, items.describe(e)))


# --- 4. лечение ------------------------------------------------------------

def test_level_heals():
    print("\n--- 4. уровень восстанавливает здоровье ---")
    w = make_world()
    e = add_player(w, 8.5, 6.5, hp=20)
    hp0 = e.hp
    kill_enemies(w, e, 3)
    check(e.hp == hp0 + items.LEVEL_HEAL,
          "уровень вернул %d hp" % items.LEVEL_HEAL,
          "hp %d -> %d" % (hp0, e.hp))

    w = make_world()
    e = add_player(w, 8.5, 6.5, hp=90)
    kill_enemies(w, e, 3)
    check(e.hp == e.hp_max,
          "выше потолка не лечит", "hp 90 -> %d из %d" % (e.hp, e.hp_max))

    # ВЫВОД ЧИСЛА, А НЕ ЕГО ПОВТОРЕНИЕ. Уровень L стоит XP_STEP*L опыта, то
    # есть XP_STEP*L/35 = 3L рядовых. Жатва за те же убийства даёт
    # HARVEST_HEAL за каждое. Самый выгодный уровень — первый.
    avg = ((ai.RANGED_EVERY - 1) * ai.MELEE_HP + ai.RANGED_HP) / float(ai.RANGED_EVERY)
    kills_1 = items.level_cost(0) / avg
    per_kill = items.LEVEL_HEAL / kills_1
    note("лечение уровня в пересчёте на убийство",
         "первый уровень: %.1f убийств -> %.1f hp за убийство; "
         "один стак Жатвы даёт %d" % (kills_1, per_kill, items.HARVEST_HEAL))
    check(per_kill < items.HARVEST_HEAL,
          "уровень лечит МЕНЬШЕ одного стака Жатвы за те же убийства — "
          "иначе Жатву не возьмёт никто (8.4)",
          "%.1f против %d hp за убийство, запас x%.2f"
          % (per_kill, items.HARVEST_HEAL, items.HARVEST_HEAL / per_kill))
    check(items.LEVEL_HEAL <= items.REVIVE_SHARE * 100,
          "уровень не отыгрывает смерть целиком: лечит %d, смерть стоит %d hp"
          % (items.LEVEL_HEAL, int(100 * items.REVIVE_SHARE)),
          "иначе смерть снова бесплатна, только другим путём")


# --- 5. алтари остались ----------------------------------------------------

def test_altars_stay():
    print("\n--- 5. алтари на месте: уровень ДОПОЛНЯЕТ, а не заменяет ---")
    fl = gen.generate(4242, 3, gen.ROOM_W, gen.ROOM_H)
    w = W.World(fl.grid, seed=4242, floor=3, spawns=fl.spawns,
                stairs=fl.stairs)
    e = w.spawn_player("A", 0)
    w.spawn_player("B", 1)
    items.populate(w, fl)
    altar = [t for t in items_on(w) if not t.owner]
    check(len(altar) >= items.ALTAR_SLOTS,
          "на этаже по-прежнему лежат алтари (8.4)",
          "предметов на алтарях %d, алтарей %d"
          % (len(altar), items.altar_count(2)))
    check(all(t.owner == 0 for t in altar),
          "предмет с алтаря ОБЩИЙ: за него группа и торгуется",
          "owner %s" % sorted(set(t.owner for t in altar)))
    # апгрейд с алтаря и апгрейд с уровня складываются
    items.grant(e, items.U_HARVEST, 1)
    kill_enemies(w, e, 3)
    pool = items_on(w, owner=e.id)
    check(len(pool) == items.LEVEL_SLOTS,
          "уровень выложил свой выбор ПОВЕРХ этажа с алтарями",
          "своих предметов %d, чужих на полу %d"
          % (len(pool), len(items_on(w)) - len(pool)))
    items.request_pickup(w, e)
    w.step()
    check(items.total(e) == 2,
          "набор с алтаря и набор с уровня складываются в один",
          "набор %s" % items.describe(e))


# --- 6. цена смерти --------------------------------------------------------

def test_death_costs():
    print("\n--- 6. смерть стоит общего заряда группы (8.5, дыра 11.8) ---")
    note("зарядов у группы на старте %d, босс возвращает %d, "
         "воскрешение даёт долю %.2f"
         % (items.REVIVE_BASE, items.REVIVE_PER_BOSS, items.REVIVE_SHARE))
    fl = gen.generate(4243, 1, gen.ROOM_W, gen.ROOM_H)
    w = W.World(fl.grid, seed=4243, floor=1, spawns=fl.spawns,
                stairs=fl.stairs)
    a = w.spawn_player("A", 0)
    b = w.spawn_player("B", 1)
    check(w.revives == items.REVIVE_BASE,
          "счётчик заряжен на старте забега", "зарядов %d" % w.revives)
    combat.kill(w, b)
    fl2 = gen.generate(4243, 2, gen.ROOM_W, gen.ROOM_H)
    w.enter_floor(2, fl2.grid, fl2.spawns, fl2.stairs)
    check(not (b.flags & W.F_DEAD)
          and b.hp == int(b.hp_max * items.REVIVE_SHARE),
          "воскрешение вернуло ПОЛОВИНУ здоровья, а не полное",
          "hp %d из %d" % (b.hp, b.hp_max))
    check(w.revives == items.REVIVE_BASE - 1,
          "и стоило группе заряда", "зарядов %d" % w.revives)

    # счётчик до нуля
    spent = 0
    fl_n = 2
    while w.revives > 0 and spent < 10:
        combat.kill(w, b)
        fl_n += 1
        f = gen.generate(4243, fl_n, gen.ROOM_W, gen.ROOM_H)
        w.enter_floor(fl_n, f.grid, f.spawns, f.stairs)
        spent += 1
    combat.kill(w, b)
    fl_n += 1
    f = gen.generate(4243, fl_n, gen.ROOM_W, gen.ROOM_H)
    w.enter_floor(fl_n, f.grid, f.spawns, f.stairs)
    check(w.revives == 0 and (b.flags & W.F_DEAD),
          "счётчик пуст — воскрешения нет, дух остаётся духом",
          "зарядов %d, флаги %d, hp %d" % (w.revives, b.flags, b.hp))
    check(len(w.alive_players()) == 1,
          "но группа идёт дальше: живой не заперт духом (8.1)",
          "живых %d из 2" % len(w.alive_players()))
    check(a.ctl and not (a.flags & W.F_DEAD),
          "дух по-прежнему управляем и летает — серого экрана нет (8.5)",
          "дух ctl=%s flags=%d" % (b.ctl, b.flags))

    # босс возвращает заряд
    bo = w.spawn(W.K_BOSS, a.x + 2.0, a.y, r=0.5, hp=10, hp_max=10)
    combat.damage(w, bo, 10, a)
    check(w.revives == items.REVIVE_PER_BOSS,
          "босс вернул группе заряд (8.1: босса нельзя обойти)",
          "зарядов 0 -> %d" % w.revives)

    # ...и тогда дух воскресает на следующем этаже
    fl_n += 1
    f = gen.generate(4243, fl_n, gen.ROOM_W, gen.ROOM_H)
    w.enter_floor(fl_n, f.grid, f.spawns, f.stairs)
    check(not (b.flags & W.F_DEAD) and w.revives == 0,
          "на возвращённый заряд дух воскрес: смерть не окончательна, "
          "пока группа бьёт боссов",
          "флаги %d, hp %d, зарядов %d" % (b.flags, b.hp, w.revives))


# --- 7. цена в тике --------------------------------------------------------

def test_tick_cost():
    print("\n--- 7. цена в тике (раздел 2.2: бюджет 12 мс медиана, 25 p99) ---")
    g = open_room(64, 48)
    w = W.World(g, seed=7, floor=1, spawns=[(2.5, 2.5)])
    players = [add_player(w, 4.5 + i, 4.5) for i in range(6)]
    n = 0
    y = 6.5
    x = 4.5
    while n < 194:
        e = ai.make_enemy(w, x, y, ai.AI_MELEE if n % 3 else ai.AI_RANGED)
        e.hp = e.hp_max = 40
        n += 1
        x += 1.2
        if x > 60:
            x = 4.5
            y += 1.2
    note("состав", "%d сущностей, из них игроков %d" % (len(w.entities),
                                                        len(players)))
    for _ in range(30):
        w.step()
    ts = []
    for _ in range(300):
        t0 = CLOCK()
        w.step()
        ts.append((CLOCK() - t0) * 1000.0)
    ts.sort()
    med = statistics.median(ts)
    p99 = ts[int(len(ts) * 0.99)]
    note("тик, мс", "медиана %.3f, p99 %.3f, max %.3f" % (med, p99, ts[-1]))
    note("uptime рядом с замером (правило 9)", uptime_line())
    check(med <= 12.0 and p99 <= 25.0,
          "тик в бюджете (медиана <= 12, p99 <= 25)",
          "запас x%.1f по медиане и x%.1f по p99" % (12.0 / med, 25.0 / p99))


# --- подсадки --------------------------------------------------------------

def sabotage(what):
    """Сломать ровно одну охраняемую вещь. Доказательство правила 7."""
    if what == "xp":
        # опыт не начисляется вовсе
        items.add_xp = lambda w, e, amount: 0
    elif what == "level":
        # уровень есть, апгрейда нет: выбор не выкладывается
        items.resolve_levels = lambda w: 0
        items.offer_items = lambda w, e: 0
    elif what == "revive":
        # воскрешение снова бесплатное и с полным здоровьем
        items.spend_revive = lambda w: True
        items.REVIVE_SHARE = 1.0
    else:
        raise SystemExit("неизвестная подсадка: %s" % what)
    print("  ПОДСАДКА: %s — проверка ОБЯЗАНА покраснеть" % what)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--break", dest="brk", default="",
                    help="подсадка: xp | level | revive")
    args = ap.parse_args()

    print("=" * 72)
    print("Приёмка опыта, уровней и цены смерти (8.4a, 8.5, 11.8)")
    print("  " + uptime_line())
    print("=" * 72)
    if args.brk:
        sabotage(args.brk)

    test_xp_is_price()
    test_curve_shape()
    test_level_offers_choice()
    test_level_heals()
    test_altars_stay()
    test_death_costs()
    test_tick_cost()

    print()
    if FAILS:
        print("КРАСНО: %d" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
