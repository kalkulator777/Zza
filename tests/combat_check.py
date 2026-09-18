#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка боя: рывок, неуязвимость, замах, снаряд, урон, смерть.

Что здесь проверяется и почему именно так.

  1. РЫВОК ОТМЕРЯЕТ СВОЁ РАССТОЯНИЕ. 4.2 обещает 14 кл/с на 0.18 с. Тик
     сервера 1/30 с, значит 0.18 с — это 5.4 тика, число непредставимое.
     Проверка требует совпадения с ТИКОВОЙ величиной (14 * 5 * 1/30) в ноль
     и отдельно печатает, насколько это разошлось с 2.52 клетки из контракта.
     Порог на расхождение — один тик рывка (0.467 клетки): больше взяться
     неоткуда, если скорость взята из 4.2 как есть.
  2. НЕУЯЗВИМОСТЬ ДЛИТСЯ РОВНО РЫВОК. Урон подаётся на каждом тике от начала
     рывка; окно, в котором он не проходит, обязано совпасть с длительностью
     рывка тик в тик. Не "примерно столько" — ровно, с обеих границ.
  3. СНАРЯД ЛЕТИТ ПО ПРЯМОЙ И ГИБНЕТ О СТЕНУ (4.2a). Отклонение от прямой
     обязано быть НУЛЁМ: подталкивание на углах снаряду выключено. Вторая
     карта — с проёмом у самой линии полёта: именно на ней подталкивание,
     если его включить, сдвигает снаряд и он уходит в дверь.
  4. ЗАМАХ ВИДНО. Между нажатием и уроном обязано пройти MELEE_WINDUP_TICKS
     тиков, и это не ноль. Мгновенный удар — это другая игра.
  5. СМЕРТЬ РАБОТАЕТ. hp до нуля -> бит DEAD (4.3); мёртвый не получает и не
     наносит урона, туман им не светит (4.4, 8.5), но летает сквозь стены.
  6. СОБЫТИЯ ev УХОДЯТ (5.2) — попадание, смерть, выстрел.

Карта своя, маленькая, а не из генератора — раздел 10 контракта: проверка
физики и боя, которая берёт карту у gen.py, краснеет от чужой работы.

Запуск:  python3 tests/combat_check.py
Выход: 0 — зелено, 1 — красно.
"""

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import combat, physics, proto, vis as vis_mod   # noqa: E402
from server import world as world_mod                       # noqa: E402

DT = world_mod.DT
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


# --- карты ----------------------------------------------------------------

def open_room(w=28, h=14):
    """Пустой зал в кольце стен: чистая арифметика движения, без геометрии."""
    g = physics.Grid(w, h)
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            g.set(x, y, physics.TILE_FLOOR)
    return g


def wall_at(w=28, h=14, wx=16, gap_y=None):
    """Тот же зал, поперёк — сплошная стена по столбцу wx.

    gap_y — одна клетка пола в этой стене (проём). Нужен, чтобы поймать
    подталкивание: снаряд, летящий у самой кромки проёма, с подталкиванием
    свернёт в дверь, без него — умрёт о косяк.
    """
    g = open_room(w, h)
    for y in range(1, h - 1):
        g.set(wx, y, physics.TILE_WALL)
    if gap_y is not None:
        g.set(wx, gap_y, physics.TILE_FLOOR)
    return g


def make_world(grid):
    return world_mod.World(grid, seed=1, floor=1, spawns=[(2.5, 2.5)])


def add_player(w, x, y, facing=0.0, hp=100):
    e = w.spawn(W.K_PLAYER, x, y, r=W.R_PLAYER, speed=W.SPEED_RUN, ctl=True,
                hp=hp, hp_max=100, facing=facing)
    return e


def add_enemy(w, x, y, hp=100, r=0.35):
    return w.spawn(W.K_ENEMY, x, y, r=r, speed=0.0, hp=hp, hp_max=hp)


def press(w, e, btn, mv=(0.0, 0.0)):
    """Нажать кнопку ровно на один тик и шагнуть."""
    e.btn = btn
    e.mv = mv
    w.step()
    e.btn = 0


# --- 1. рывок: расстояние и длительность ----------------------------------

def test_dash():
    print("\n--- 1. рывок (4.2: %.1f кл/с, %.2f с, откат %.1f с) ---"
          % (combat.DASH_SPEED, combat.DASH_TIME_S, combat.DASH_CD_S))
    w = make_world(open_room())
    e = add_player(w, 4.5, 6.5)
    x0 = e.x
    # mv пустой: бега нет вовсе, значит всё пройденное — это рывок и только он
    press(w, e, proto.BTN_DASH)
    moving = 1
    steps = [e.x - x0]
    prev = e.x
    for _ in range(20):
        w.step()
        d = e.x - prev
        prev = e.x
        steps.append(d)
        if d > 1e-9:
            moving += 1
    dist = e.x - x0
    want_tick = combat.DASH_SPEED * combat.DASH_TICKS * DT
    want_contract = combat.DASH_SPEED * combat.DASH_TIME_S
    one_tick = combat.DASH_SPEED * DT

    note("длительность", "%d тиков = %.4f с (контракт %.2f с = %.1f тика)"
         % (moving, moving * DT, combat.DASH_TIME_S,
            combat.DASH_TIME_S / DT))
    note("расстояние", "%.4f клетки (тиковая величина %.4f, контракт %.4f)"
         % (dist, want_tick, want_contract))
    check(moving == combat.DASH_TICKS,
          "рывок длится ровно DASH_TICKS тиков",
          "прошло %d тиков, ждали %d" % (moving, combat.DASH_TICKS))
    check(abs(dist - want_tick) < 1e-9,
          "путь рывка = скорость * длительность в тиках",
          "%.6f против %.6f, расхождение %.2e" % (dist, want_tick,
                                                  abs(dist - want_tick)))
    check(abs(dist - want_contract) <= one_tick,
          "путь рывка сходится с 4.2 с точностью до тика",
          "|%.4f - %.4f| = %.4f <= один тик рывка %.4f"
          % (dist, want_contract, abs(dist - want_contract), one_tick))
    check(abs(e.y - 6.5) < 1e-9, "рывок не уводит вбок",
          "y %.6f -> %.6f" % (6.5, e.y))

    # откат: 4.2 обещает 0.9 с. За тик до него рывок не даётся, на нём — даётся.
    # Меряется тем же, чем всё остальное: сдвигом за тик. Шаг рывка 0.4667
    # клетки, шаг стоящего на месте — ноль, спутать нельзя.
    def dash_at(tick_no):
        """Нажать рывок ровно на тике tick_no. Вернуть сдвиг за этот тик."""
        w2 = make_world(open_room())
        e2 = add_player(w2, 4.5, 6.5)
        press(w2, e2, proto.BTN_DASH)          # первый рывок на тике 1
        while w2.tick < tick_no - 1:
            w2.step()
        x_before = e2.x
        press(w2, e2, proto.BTN_DASH)
        return e2.x - x_before, w2.tick

    first = 1
    early, t_early = dash_at(first + combat.DASH_CD_TICKS - 1)
    late, t_late = dash_at(first + combat.DASH_CD_TICKS)
    step_dash = combat.DASH_SPEED * DT
    note("первый рывок на тике %d, откат %d тиков" % (first, combat.DASH_CD_TICKS),
         "повтор на тике %d сдвинул на %.4f, на тике %d — на %.4f "
         "(шаг рывка %.4f)" % (t_early, early, t_late, late, step_dash))
    check(abs(early) < 1e-9 and abs(late - step_dash) < 1e-9,
          "откат рывка %.2f с = %d тиков соблюдён"
          % (combat.DASH_CD_S, combat.DASH_CD_TICKS),
          "за тик до отката сдвиг %.4f (ждали 0), на откате %.4f (ждали %.4f)"
          % (early, late, step_dash))


# --- 2. неуязвимость рывка -------------------------------------------------

def test_dash_iframes():
    print("\n--- 2. неуязвимость в рывке (4.2: уход из-под замаха) ---")
    DMG = 10
    passed_at = []
    blocked_at = []
    for k in range(1, 10):
        w = make_world(open_room())
        e = add_player(w, 4.5, 6.5)
        press(w, e, proto.BTN_DASH)          # рывок начался на этом тике
        for _ in range(k - 1):
            w.step()
        hp0 = e.hp
        combat.damage(w, e, DMG)
        if e.hp < hp0:
            passed_at.append(k)
        else:
            blocked_at.append(k)
    want_blocked = list(range(1, combat.DASH_TICKS + 1))
    note("урон подавался на тиках", "1..9 после начала рывка")
    note("не прошёл на тиках", "%s (это %.4f с)"
         % (blocked_at, len(blocked_at) * DT))
    note("прошёл на тиках", "%s" % passed_at)
    check(blocked_at == want_blocked,
          "неуязвимость длится РОВНО длительность рывка, тик в тик",
          "не прошло на %s, ждали %s" % (blocked_at, want_blocked))

    # то же самое числами из приёмки: в момент рывка и через 0.2 с после
    w = make_world(open_room())
    e = add_player(w, 4.5, 6.5)
    press(w, e, proto.BTN_DASH)
    hp_before = e.hp
    combat.damage(w, e, DMG)
    hp_in_dash = e.hp
    for _ in range(int(round(0.2 / DT)) - 1):
        w.step()
    combat.damage(w, e, DMG)
    hp_after = e.hp
    note("hp в момент рывка", "%d -> %d" % (hp_before, hp_in_dash))
    note("hp через 0.2 с (%d тиков) от начала рывка" % int(round(0.2 / DT)),
         "%d -> %d" % (hp_in_dash, hp_after))
    check(hp_in_dash == hp_before and hp_after == hp_in_dash - DMG,
          "урон в рывке не проходит, урон через 0.2 с проходит",
          "%d / %d / %d" % (hp_before, hp_in_dash, hp_after))


# --- 3. снаряд -------------------------------------------------------------

def _fly(w, shooter, limit=80):
    """Выстрелить и вести снаряд до гибели. (точки, точка гибели, тики).

    Точка гибели берётся из события boom, а не из последней замеченной
    позиции: сущность к концу тика уже убрана, и "последняя, которую видно" —
    это точка ЗА тик до стены, то есть на 0.4 клетки мимо. Ровно тот случай,
    когда проверка меряет не то число, которое ей нужно (раздел 10).
    """
    w.events[:] = []
    press(w, shooter, proto.BTN_SHOOT)
    sid = None
    for e in w.entities.values():
        if e.kind == W.K_SHOT:
            sid = e.id
    if sid is None:
        return [], None, 0
    pts = [(w.entities[sid].x, w.entities[sid].y)]
    ticks = 0
    last = pts[0]
    for _ in range(limit):
        w.step()
        ticks += 1
        s = w.entities.get(sid)
        if s is None:
            break
        last = (s.x, s.y)
        pts.append(last)
    for _t, kind, kw in w.events:
        if kind == "boom" and kw.get("b") == sid:
            last = (kw["x"], kw["y"])
            pts.append(last)
    return pts, last, ticks


def test_shot():
    print("\n--- 3. снаряд (4.2: %.0f кл/с; 4.2a: подталкивание выключено) ---"
          % combat.SHOT_SPEED)
    WX = 16
    # (а) в лоб в глухую стену
    w = make_world(wall_at(wx=WX))
    p = add_player(w, 4.5, 6.5, facing=0.0)
    pts, last, ticks = _fly(w, p)
    dev = max(abs(y - pts[0][1]) for x, y in pts) if pts else -1.0
    wall_face = WX - combat.SHOT_R
    note("полёт", "%d точек, %d тиков, от x=%.3f до x=%.3f"
         % (len(pts), ticks, pts[0][0], last[0]))
    note("шаг за тик", "%.4f клетки (подшагов в move_circle: %d)"
         % (combat.SHOT_SPEED * DT,
            int(combat.SHOT_SPEED * DT / combat.SHOT_R) + 1))
    check(dev == 0.0, "отклонение от прямой равно НУЛЮ (assist=False)",
          "максимум |y - y0| = %.1e клетки" % dev)
    check(abs(last[0] - wall_face) < 2e-3,
          "снаряд гибнет вплотную к стене",
          "гибель на x=%.6f, кромка стены x=%d, касание при x=%.6f "
          "(радиус снаряда %.2f), разница %.2e"
          % (last[0], WX, wall_face, combat.SHOT_R, abs(last[0] - wall_face)))
    check(w.entities.get(1) is not None and
          not any(e.kind == W.K_SHOT for e in w.entities.values()),
          "снаряд убран из мира после столкновения",
          "сущностей осталось %d" % len(w.entities))

    # (б) у самой кромки проёма — тут подталкивание, если его включить, видно
    GAP = 6
    w = make_world(wall_at(wx=WX, gap_y=GAP))
    # центр линии полёта на 0.02 ниже клетки проёма: тело с подталкиванием
    # свернуло бы в дверь, снаряд обязан идти прямо и умереть о косяк
    p = add_player(w, 4.5, GAP + 0.02, facing=0.0)
    pts, last, ticks = _fly(w, p)
    y0 = pts[0][1]
    dev = max(abs(y - y0) for x, y in pts)
    note("проём в стене", "клетка (%d,%d); линия полёта y=%.3f, свободная "
         "полоса для снаряда y=%.2f..%.2f"
         % (WX, GAP, y0, GAP + combat.SHOT_R, GAP + 1 - combat.SHOT_R))
    check(dev == 0.0,
          "у кромки проёма снаряд НЕ подталкивается (4.2a)",
          "максимум |y - y0| = %.1e клетки за %d тиков полёта" % (dev, ticks))
    check(last[0] < WX,
          "снаряд не протискивается в дверь, до которой не долетел прямо",
          "погиб на x=%.3f (стена на x=%d)" % (last[0], WX))

    # (в) снаряд бьёт врага и не бьёт своих
    w = make_world(open_room())
    p = add_player(w, 4.5, 6.5, facing=0.0)
    mate = add_player(w, 6.5, 6.5)
    foe = add_enemy(w, 9.5, 6.5, hp=100)
    mate_hp0 = mate.hp
    pts, last, ticks = _fly(w, p)
    check(mate.hp == mate_hp0 and foe.hp == 100 - combat.SHOT_DMG,
          "снаряд снимает %d у врага и ноль у своего" % combat.SHOT_DMG,
          "напарник %d -> %d, враг 100 -> %d" % (mate_hp0, mate.hp, foe.hp))

    # (г) снаряд живёт не вечно
    w = make_world(open_room(w=120, h=14))
    p = add_player(w, 4.5, 6.5, facing=0.0)
    pts, last, ticks = _fly(w, p, limit=200)
    note("дальность", "%d тиков = %.2f клетки (потолок %d тиков = %.1f клетки)"
         % (ticks, combat.SHOT_SPEED * ticks * DT, combat.SHOT_TICKS,
            combat.SHOT_RANGE))
    check(ticks <= combat.SHOT_TICKS + 1,
          "снаряд растворяется по ttl, а не летит вечно",
          "прожил %d тиков при потолке %d" % (ticks, combat.SHOT_TICKS))


# --- 4. замах --------------------------------------------------------------

def test_windup():
    print("\n--- 4. замах ближней атаки (4.2: %.2f с) ---" % combat.MELEE_WINDUP_S)
    w = make_world(open_room())
    p = add_player(w, 6.5, 6.5, facing=0.0)
    foe = add_enemy(w, 7.5, 6.5, hp=100)
    hp0 = foe.hp
    press(w, p, proto.BTN_ATTACK)
    t_press = w.tick
    hp_same_tick = foe.hp
    flags_during = p.flags
    flag_during = bool(flags_during & W.F_WINDUP)
    t_hit = 0
    for _ in range(30):
        w.step()
        if foe.hp != hp0:
            t_hit = w.tick
            break
    gap = t_hit - t_press if t_hit else -1
    note("нажатие на тике", "%d, урон на тике %d" % (t_press, t_hit))
    note("замах", "%d тиков = %.4f с (контракт %.2f с = %.1f тика)"
         % (gap, gap * DT, combat.MELEE_WINDUP_S, combat.MELEE_WINDUP_S / DT))
    check(hp_same_tick == hp0, "в тик нажатия урона ещё нет",
          "hp %d -> %d" % (hp0, hp_same_tick))
    check(flag_during, "на замахе горит бит F_WINDUP (клиенту есть что рисовать)",
          "flags на замахе = %d (F_WINDUP = %d)" % (flags_during, W.F_WINDUP))
    check(gap == combat.MELEE_WINDUP_TICKS and gap > 0,
          "между началом атаки и уроном ровно %d тиков, а не ноль"
          % combat.MELEE_WINDUP_TICKS,
          "прошло %d тиков (%.4f с)" % (gap, gap * DT))
    check(abs(gap * DT - combat.MELEE_WINDUP_S) <= DT,
          "замах сходится с 4.2 с точностью до тика",
          "|%.4f - %.2f| = %.4f <= %.4f"
          % (gap * DT, combat.MELEE_WINDUP_S,
             abs(gap * DT - combat.MELEE_WINDUP_S), DT))
    check(not (p.flags & W.F_WINDUP), "после удара бит F_WINDUP снят",
          "flags после удара = %d" % p.flags)

    # дуга: за спиной не достаёт
    w = make_world(open_room())
    p = add_player(w, 6.5, 6.5, facing=0.0)
    back = add_enemy(w, 5.6, 6.5, hp=100)
    far = add_enemy(w, 6.5 + combat.MELEE_REACH + 0.35 + 0.3, 6.5, hp=100)
    p.aim = (60.0, 6.5)                 # смотрим строго вправо
    press(w, p, proto.BTN_ATTACK)
    for _ in range(combat.MELEE_WINDUP_TICKS + 1):
        w.step()
    check(back.hp == 100 and far.hp == 100,
          "удар идёт по дуге перед собой, а не по кругу",
          "за спиной %d (ждали 100), за дальностью %d (ждали 100)"
          % (back.hp, far.hp))


# --- 5. урон и смерть ------------------------------------------------------

def test_death():
    print("\n--- 5. смерть (4.3: бит DEAD; 8.5: дух) ---")
    w = make_world(open_room())
    p = add_player(w, 6.5, 6.5)
    w.step()
    lit_alive = w.fog.lit_count()
    seen_alive = w.fog.seen_count()
    hp0 = p.hp
    got = combat.damage(w, p, 100)
    note("урон", "%d hp: %d -> %d" % (got, hp0, p.hp))
    check(p.hp == 0 and (p.flags & W.F_DEAD),
          "здоровье до нуля ставит бит DEAD (flags & 1)",
          "hp=%d flags=%d" % (p.hp, p.flags))
    check(p.id in w.entities,
          "мёртвый игрок остаётся в мире духом, а не исчезает",
          "сущностей %d" % len(w.entities))
    again = combat.damage(w, p, 25)
    check(again == 0 and p.hp == 0, "мёртвый урона не получает",
          "второй удар снял %d, hp=%d" % (again, p.hp))

    # туман: мёртвый не светит (4.4)
    w.step()
    lit_dead = w.fog.lit_count()
    seen_dead = w.fog.seen_count()
    note("туман при живом", "освещено %d, открыто %d" % (lit_alive, seen_alive))
    note("туман при мёртвом", "освещено %d, открыто %d" % (lit_dead, seen_dead))
    check(lit_alive > 0 and lit_dead == 0,
          "мёртвый туман не светит (4.4)",
          "освещено было %d, стало %d" % (lit_alive, lit_dead))
    check(seen_dead == seen_alive,
          "память группы от смерти не стирается (гаснет только свет)",
          "открыто было %d, стало %d" % (seen_alive, seen_dead))

    # мёртвый не наносит урона
    foe = add_enemy(w, 7.3, 6.5, hp=100)
    p.aim = (60.0, 6.5)
    for _ in range(combat.MELEE_WINDUP_TICKS + 3):
        press(w, p, proto.BTN_ATTACK)
    check(foe.hp == 100 and not (p.flags & W.F_WINDUP),
          "мёртвый урона не наносит и даже не замахивается",
          "hp врага %d, flags духа %d" % (foe.hp, p.flags))

    # дух летает: стены ему не преграда (8.5)
    w2 = make_world(wall_at(wx=16))
    g2 = add_player(w2, 14.5, 6.5)
    combat.damage(w2, g2, 500)
    g2.mv = (1.0, 0.0)
    for _ in range(30):
        w2.step()
    check(g2.x > 16.0, "дух пролетает сквозь стену (8.5)",
          "улетел на x=%.2f, стена на x=16" % g2.x)

    # враг (не игрок) от смерти убирается из мира
    w3 = make_world(open_room())
    p3 = add_player(w3, 6.5, 6.5)
    foe3 = add_enemy(w3, 7.2, 6.5, hp=combat.MELEE_DMG)
    p3.aim = (60.0, 6.5)
    press(w3, p3, proto.BTN_ATTACK)
    for _ in range(combat.MELEE_WINDUP_TICKS + 2):
        w3.step()
    check(foe3.id not in w3.entities,
          "мёртвый враг (не игрок) убирается из мира",
          "сущностей %d, hp врага %d" % (len(w3.entities), foe3.hp))

    # урон применяется к ЛЮБОЙ сущности, а не только к игроку
    w4 = make_world(open_room())
    prop = w4.spawn(W.K_PROP, 5.5, 5.5, r=0.3, hp=40, hp_max=40, team=2)
    n = combat.damage(w4, prop, 15)
    check(n == 15 and prop.hp == 25,
          "урон применяется к любой сущности с hp, не только к игроку",
          "реквизит kind=%d: 40 -> %d" % (prop.kind, prop.hp))


# --- 6. события ev ---------------------------------------------------------

def test_events():
    print("\n--- 6. события ev (5.2) ---")
    w = make_world(open_room())
    p = add_player(w, 6.5, 6.5, facing=0.0)
    foe = add_enemy(w, 7.3, 6.5, hp=combat.MELEE_DMG)
    p.aim = (60.0, 6.5)
    press(w, p, proto.BTN_ATTACK)
    for _ in range(combat.MELEE_WINDUP_TICKS + 2):
        w.step()
    kinds = [k for _t, k, _kw in w.events]
    note("после ближней атаки", "%s" % kinds)
    ok_hit = "hit" in kinds
    ok_die = "die" in kinds
    w.events[:] = []
    press(w, p, proto.BTN_SHOOT)
    kinds2 = [k for _t, k, _kw in w.events]
    note("после выстрела", "%s" % kinds2)
    check(ok_hit and ok_die and "shot" in kinds2,
          "попадание, смерть и выстрел уходят событием ev",
          "%s + %s" % (kinds, kinds2))
    # событие обязано быть кодируемым протоколом
    bad = []
    for tick, kind, kw in w.events:
        try:
            proto.ev(tick, kind, **kw)
        except Exception as ex:      # noqa: BLE001
            bad.append("%s: %s" % (kind, ex))
    check(not bad, "событие кодируется proto.ev без исключений", "; ".join(bad))


def main():
    print("=" * 70)
    print("Приёмка боя. Тик %d Гц, DT=%.5f с. Карты свои, не из генератора."
          % (world_mod.TICK_HZ, DT))
    print("Квантование 30 Гц: рывок %.2f с -> %d тиков, замах %.2f с -> %d тиков."
          % (combat.DASH_TIME_S, combat.DASH_TICKS,
             combat.MELEE_WINDUP_S, combat.MELEE_WINDUP_TICKS))
    print("=" * 70)
    test_dash()
    test_dash_iframes()
    test_shot()
    test_windup()
    test_death()
    test_events()
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
