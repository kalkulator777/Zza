#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка узкого проёма: одна зажатая клавиша проводит тело сквозь дверь
шириной в одну клетку — и НЕ проводит его сквозь глухую стену.

Зачем. Тело круглое, R=0.35 (4.1/4.3), проём ровно одна клетка: центр
проходит только в полосе шириной 1-2R = 0.30 клетки. Промахнулся на 0.16 —
move_circle гасит скорость по оси, а второй оси нет (клавиша одна), и
сущность стоит вечно. Человек дёрнет поперёк и не заметит, а враг этапа 2
(8.3: катится по градиенту карты расстояний, скорость почти всегда вдоль
одной оси) встанет в первом же проёме навсегда.

Карта своя, маленькая, а не из генератора — раздел 10 контракта: проверка
физики, которая берёт карту у gen.py, краснеет от чужой работы.

ЧТО ПРОВЕРЯЕТСЯ

  1. ПРОЁМ ПРОХОДИТСЯ. Стена с проёмом в одну клетку, четыре стороны
     подхода, поперечный промах гоняется по всей клетке проёма. Одна
     зажатая клавиша, подруливания нет. Требуется 100 % прохождений из
     всей клетки проёма: полоса захвата — вся клетка (1.00), а не 0.30.
  2. СКВОЗЬ СТЕНУ НЕ ПРОЛЕЗАЕТ. Та же карта, проём заделан: за 5 с ни одна
     попытка не оказывается по ту сторону. Обязательная пара к пункту 1 —
     без неё «починку» можно было бы сделать, просто выключив стены.
  3. УГЛЫ НЕ СРЕЗАЮТСЯ. Диагональный зажим (две стены углом к углу, прохода
     для круга нет ни при каком смещении) не проходится.
  4. МИМО ПРОЁМА НЕ ЗАСАСЫВАЕТ. Подход по ряду, соседнему с проёмом (центр
     тела в клетке стены, а не в клетке двери) прохода не даёт: подталкивание
     дотягивает ровно до края своей клетки и ни клеткой дальше.
  5. СКОЛЬЖЕНИЕ ВДОЛЬ СТЕНЫ ЦЕЛО. Бег по диагонали в стену по-прежнему
     превращается в бег вдоль стены с полной скоростью свободной оси.

Запуск:  python3 tests/physics_gap.py
Выход: 0 — зелено, 1 — красно.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import physics                    # noqa: E402

TICK_HZ = 30                 # 4.1
DT = 1.0 / TICK_HZ
SPEED = 5.0                  # 4.2, бег игрока
R = 0.35                     # 4.1/4.3, радиус тела игрока
BUDGET_S = 5.0               # потолок на одну попытку
STEPS = int(BUDGET_S * TICK_HZ)

# Промахи поперёк: вся клетка проёма, от края до края, шагом 0.05.
# 0.05 клетки = 2.4 пикселя при 48 px/клетка (4.1) — мельче, чем видно
# глазом, и в 3.3 раза мельче старой полосы прохода 0.30.
OFFSETS = [round(0.025 + 0.05 * i, 3) for i in range(20)]

FAILS = []


def check(ok, title, detail=""):
    print(("  ЗЕЛЕНО  " if ok else "  КРАСНО  ") + title +
          (("  | " + detail) if detail else ""))
    if not ok:
        FAILS.append(title)
    return ok


def note(title, detail=""):
    print("  данные   " + title + (("  | " + detail) if detail else ""))


# --- карта ----------------------------------------------------------------

W = H = 15
WALL_AT = 7          # стена по всей линии, проём — ровно одна клетка
GAP_AT = 7


def make_map(gap=True, vertical=True, pinch=False):
    """Зал 15x15 в кольце стен, поперёк — сплошная стена с проёмом.

    vertical=True  — стена по столбцу WALL_AT (проходится по X);
    vertical=False — стена по строке WALL_AT (проходится по Y).
    pinch=True     — вместо проёма диагональный зажим: проёма нет ни в
                     одном ряду, но две стены сходятся углом к углу.
    """
    g = physics.Grid(W, H)
    for y in range(1, H - 1):
        for x in range(1, W - 1):
            g.set(x, y, physics.TILE_FLOOR)
    for i in range(1, W - 1):
        if vertical:
            g.set(WALL_AT, i, physics.TILE_WALL)
        else:
            g.set(i, WALL_AT, physics.TILE_WALL)
    if pinch:
        # стена сдвинута на клетку в половине карты: получается вход
        # «угол к углу», в который круг не пролезает ни при каком смещении
        for i in range(1, GAP_AT + 1):
            if vertical:
                g.set(WALL_AT, i, physics.TILE_FLOOR)
                g.set(WALL_AT + 1, i, physics.TILE_WALL)
            else:
                g.set(i, WALL_AT, physics.TILE_FLOOR)
                g.set(i, WALL_AT + 1, physics.TILE_WALL)
    elif gap:
        if vertical:
            g.set(WALL_AT, GAP_AT, physics.TILE_FLOOR)
        else:
            g.set(GAP_AT, WALL_AT, physics.TILE_FLOOR)
    return g


# --- прогон одной попытки -------------------------------------------------

DIRS = (("вправо", (1, 0)), ("влево", (-1, 0)),
        ("вниз", (0, 1)), ("вверх", (0, -1)))


def start_point(d, across):
    """Точка старта: 4 клетки не доходя до стены, промах поперёк — across."""
    dx, dy = d
    if dx:
        x = WALL_AT - 4.0 if dx > 0 else WALL_AT + 5.0
        return x, GAP_AT + across
    y = WALL_AT - 4.0 if dy > 0 else WALL_AT + 5.0
    return GAP_AT + across, y


def crossed(d, x, y):
    """Тело оказалось по ту сторону линии стены."""
    dx, dy = d
    if dx > 0:
        return x > WALL_AT + 1.0
    if dx < 0:
        return x < WALL_AT
    if dy > 0:
        return y > WALL_AT + 1.0
    return y < WALL_AT


def run(grid, d, across, steps=STEPS):
    """Одна зажатая клавиша. Возвращает (прошёл, секунды, x, y)."""
    dx, dy = d
    x, y = start_point(d, across)
    if physics.circle_hits(grid, x, y, R):
        return None, 0.0, x, y          # старт в стене — попытка не считается
    vx, vy = dx * SPEED, dy * SPEED
    for i in range(steps):
        x, y, hit = physics.move_circle(grid, x, y, vx * DT, vy * DT, R)
        # ровно то, что делает world.step: упор в стену гасит ось
        if hit & 1:
            vx = 0.0
        if hit & 2:
            vy = 0.0
        # клавиша зажата — ввод восстанавливает скорость на следующем тике
        vx, vy = dx * SPEED, dy * SPEED
        if crossed(d, x, y):
            return True, (i + 1) * DT, x, y
    return False, steps * DT, x, y


def sweep(grid, offsets=OFFSETS, steps=STEPS):
    """Все четыре стороны подхода на всех промахах. (ок, всего, времена)."""
    ok = 0
    total = 0
    times = []
    worst = None
    for title, d in DIRS:
        vertical_wall = bool(d[0])
        for off in offsets:
            g = grid(vertical_wall)
            res, secs, x, y = run(g, d, off, steps)
            if res is None:
                continue
            total += 1
            if res:
                ok += 1
                times.append(secs)
            elif worst is None:
                worst = (title, off, x, y)
    return ok, total, times, worst


def main():
    print("=== узкий проём: одна клавиша, тело R=%.2f, проём 1.00 клетки ==="
          % R)
    print("  полоса свободного прохода без подталкивания: 1-2R = %.2f клетки"
          % (1.0 - 2 * R))
    print("  попыток на сторону %d, потолок попытки %.0f с (%d тиков)"
          % (len(OFFSETS), BUDGET_S, STEPS))

    # --- 1. проём проходится ---------------------------------------------
    ok, total, times, worst = sweep(lambda v: make_map(gap=True, vertical=v))
    rate = 100.0 * ok / max(1, total)
    det = "прошло %d из %d (%.0f%%)" % (ok, total, rate)
    if times:
        det += ", время %.2f..%.2f с (медиана %.2f)" % (
            min(times), max(times), sorted(times)[len(times) // 2])
    if worst:
        det += "; первый провал: %s, промах %.3f, застряло на (%.2f, %.2f)" % worst
    check(ok == total, "проём в одну клетку проходится с ОДНОЙ клавишей", det)

    # --- 2. сквозь стену не пролезает ------------------------------------
    ok2, total2, _t, _ = sweep(lambda v: make_map(gap=False, vertical=v))
    check(ok2 == 0, "глухая стена не пропускает за %.0f с" % BUDGET_S,
          "прошло %d из %d попыток" % (ok2, total2))

    # --- 3. углы не срезаются --------------------------------------------
    ok3, total3, _t, _ = sweep(lambda v: make_map(pinch=True, vertical=v))
    check(ok3 == 0, "диагональный зажим (прохода нет) не проходится",
          "прошло %d из %d попыток" % (ok3, total3))

    # --- 4. мимо проёма не засасывает ------------------------------------
    # центр тела в СОСЕДНЕЙ клетке — подталкивание туда дотянуться не должно
    off_cell = [round(-0.975 + 0.05 * i, 3) for i in range(20)]
    ok4, total4, _t, _ = sweep(lambda v: make_map(gap=True, vertical=v),
                               offsets=off_cell)
    check(ok4 == 0,
          "подход по соседней клетке в проём не засасывает (углы не срезаются)",
          "прошло %d из %d попыток" % (ok4, total4))

    # --- 5. скольжение вдоль стены цело ----------------------------------
    g = make_map(gap=False, vertical=True)
    x, y = WALL_AT - 1.0, 3.5
    y0 = y
    n = 30
    for _ in range(n):
        x, y, hit = physics.move_circle(g, x, y, SPEED * DT, SPEED * DT, R)
    want = SPEED * DT * n
    got = y - y0
    check(abs(got - want) < 0.05 and x <= WALL_AT - R,
          "бег по диагонали в стену — это бег вдоль стены, а не залипание",
          "вдоль стены прошло %.3f клетки за %d тиков (свободный ход %.3f), "
          "по нормали упёрлось на x=%.3f (стена на %d)"
          % (got, n, want, x, WALL_AT))

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
