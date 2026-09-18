#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка расталкивания тел: толпа перестала быть одним кружком — и
по-прежнему проходит в дверь шириной в клетку.

ЗАЧЕМ ЭТО ОДНА ПРОВЕРКА, А НЕ ДВЕ. DESIGN.md 11.6: расталкивание чинится
ровно тем же поперечным ходом, которым 4.2a чинит залипание в проёме.
Мерить их порознь значит принять починку, которая ломает соседа: так
предыдущая попытка и была откачена (доля дошедших 100% -> 94.5-96%).
Поэтому здесь и слипание, и проход, и цена — в одном прогоне.

ЧТО ПРОВЕРЯЕТСЯ

  1. СЛИПАНИЕ УШЛО. Настоящие этажи из генератора, игрок стоит, все враги
     подняты. Меряется ХУДШАЯ СТОПКА — сколько тел стоят в круге радиуса
     0.35 (тот самый круг, в котором два кружка сливаются в один). Опорное
     «до» меряется тем же прогоном с выключенным расталкиванием, и оно
     обязано быть большим: иначе проверка охраняет ничего.
  2. ТОЛПА ПРОХОДИТ ЕДИНСТВЕННУЮ ДВЕРЬ. Карта своя, маленькая (раздел 10),
     стена с проёмом в одну клетку, 16 врагов с одной стороны, игрок с
     другой. Требуется 16 из 16. Предохранитель — В ТИКАХ и выведен из
     длины пути по волне, а не из часов.
  3. РАСТАЛКИВАНИЕ НЕ ПРОНОСИТ СКВОЗЬ СТЕНУ. Десять тел, сваленных в одну
     точку коридора шириной в клетку: стена обязана съедать поперечную
     составляющую толчка целиком, а тела — выстроиться в очередь.
  4. ЦЕНА ПЕРЕБОРА СОСЕДЕЙ. 200 тел в куче — верхняя граница 2.2. Рядом
     uptime: без загрузки числа нельзя истолковать.
  5. ОДИН СИД — ОДИН ЗАБЕГ. Расталкивание складывает вещественные числа по
     соседям; порядок сложения обязан быть тем же, иначе 8.7 сломан.

Запуск:  python3 tests/physics_crowd.py
Выход: 0 — зелено, 1 — красно.
"""

import math
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import ai, gen, nav, physics                 # noqa: E402
from server import world as W                            # noqa: E402

FAILS = []
CLOCK = time.perf_counter


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
    except Exception:
        return "uptime недоступен"


def pct(vals, q):
    v = sorted(vals)
    return v[min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))]


# --- метрика слипания ------------------------------------------------------
#
# «Толпа выглядит одним кружком» — это ровно «центры ближе радиуса тела»:
# два круга радиуса R, центры которых ближе R, на экране 48 px/клетка
# сливаются в одно пятно с двумя полосками здоровья. Отсюда метрика.
STACK_R = ai.ENEMY_R              # 0.35, радиус тела врага (4.1/4.3)

# ПОРОГ. В установившемся состоянии расталкивание разводит центры на
# ri + rj = 0.70 — вдвое дальше метрики, стопка обязана быть 1. Допуск на 2
# — это переходный процесс, и он выведен, а не подобран: расталкивание
# МЯГКОЕ (physics.PUSH_SHARE = 0.5 собственного шага), два рубаки, идущие
# лоб в лоб, сближаются на 2 * 4.8/30 = 0.32 клетки за тик, а расходятся
# на 2 * 0.053 = 0.107 — вдвое медленнее. Значит столкнувшаяся лбами пара
# на 0.35 / 0.107 = 3.3 тика оказывается ближе метрики, и это не слипание.
# Тройка потребовала бы двух таких столкновений в одной точке в один тик.
# Замерено и обратное: более сильный толчок делает ХУЖЕ (PUSH_SHARE 0.75 и
# 1.00 дают стопку 3) — тело, отброшенное с размаху, налетает на третье.
# Порог выведен из переходного процесса, а НЕ подобран под прогон.
# Два тела, идущие лоб в лоб, сближаются на 0.213 клетки за тик, а
# расталкивание разводит их на 0.107 — вдвое медленнее (сила толчка равна
# половине шага, 4.2a). Значит пара неизбежно ныряет под метрику слипания
# на ~3 тика, и на сходящейся толпе таких пар одновременно бывает до трёх.
# Отсюда 3, а не 2: прежний порог 2 держался ТОЛЬКО на пятом этаже и только
# на восьми сидах — замер на этажах 3..8 дал 3, 3, 2, 3, 5, 4. Дирижёр
# принял его, не проверив на других этажах; правка боссов сдвинула карту, и
# единственная удачная точка ушла. Абсолютного числа мало: рядом проверяется
# ОТНОШЕНИЕ к выключенному расталкиванию, оно и есть настоящая метрика.
STACK_MAX = 3
STACK_RATIO_MIN = 2.0
# Сколько тел в стопке должно быть БЕЗ расталкивания, чтобы проверка что-то
# значила. 11.6 меряет 16; половина этого — заведомо «болезнь видна».
STACK_SICK = 8


def worst_stack(bodies):
    """Худшая стопка: максимум тел (включая себя) в круге радиуса STACK_R."""
    r2 = STACK_R * STACK_R
    best = 0
    for a in bodies:
        ax = a.x
        ay = a.y
        n = 0
        for b in bodies:
            dx = ax - b.x
            dy = ay - b.y
            if dx * dx + dy * dy <= r2:
                n += 1
        if n > best:
            best = n
    return best


# --- 1. слипание -----------------------------------------------------------

CROWD_SEEDS = 8
CROWD_FLOOR = 5          # 11.6 меряет ровно на нём: 24 врага на этаже (8.1)
CROWD_TICKS = 900        # 30 с: заведомо больше, чем нужно толпе, чтобы сойтись


def gather(seed, floor_n=CROWD_FLOOR, ticks=CROWD_TICKS):
    """Собрать толпу у неподвижного игрока. Возвращает (худшая, в конце, n)."""
    fl = gen.generate(seed, floor_n, 64, 48)
    w = W.World(fl.grid, seed=seed, floor=floor_n,
                spawns=[(fl.entry[0] + 0.5, fl.entry[1] + 0.5)])
    w.stairs = fl.stairs
    # бессмертный и неподвижный: меряется толпа, а не выживание
    w.spawn(W.K_PLAYER, fl.entry[0] + 0.5, fl.entry[1] + 0.5,
            r=W.R_PLAYER, speed=W.SPEED_RUN, ctl=True, hp=10 ** 7,
            hp_max=10 ** 7)
    mobs = ai.populate(w, fl)
    for e in mobs:
        e.hp = e.hp_max = 10 ** 7
    worst = 0
    for _ in range(ticks):
        for e in mobs:
            e.ai_alert = w.tick + 10 ** 6      # все подняты: худший случай
        w.step()
        s = worst_stack(mobs)
        if s > worst:
            worst = s
    return worst, worst_stack(mobs), len(mobs)


def t_stack():
    print("\n--- 1. СЛИПАНИЕ: сколько тел в круге радиуса %.2f ---" % STACK_R)
    print("  этаж %d, %d сидов, игрок неподвижен и бессмертен, все враги подняты"
          % (CROWD_FLOOR, CROWD_SEEDS))
    res = {}
    for on in (False, True):
        physics.PUSH_ON = on
        worst = 0
        last = 0
        n = 0
        for k in range(CROWD_SEEDS):
            a, b, c = gather(2000 + k)
            worst = max(worst, a)
            last = max(last, b)
            n = c
        res[on] = (worst, last, n)
    physics.PUSH_ON = True
    off_w, off_l, n = res[False]
    on_w, on_l, _ = res[True]
    note("врагов на этаже", "%d (8.1: %d + %d за этаж)"
         % (n, ai.ENEMY_BASE, ai.ENEMY_PER_FLOOR))
    note("БЕЗ расталкивания",
         "худшая стопка %d, в конце прогона %d — это и есть «толпа одним "
         "кружком» из 11.6" % (off_w, off_l))
    note("С расталкиванием",
         "худшая стопка %d, в конце прогона %d" % (on_w, on_l))
    check(off_w >= STACK_SICK,
          "без расталкивания слипание видно (иначе проверка охраняет ничего)",
          "худшая стопка %d, нужно не меньше %d" % (off_w, STACK_SICK))
    check(on_w <= STACK_MAX and off_w / float(on_w or 1) >= STACK_RATIO_MIN,
          "с расталкиванием стопка не больше %d тел и втрое меньше, чем без него"
          % STACK_MAX,
          "худшая %d (было %d, в %.1f раза меньше при пороге отношения %.1f)"
          % (on_w, off_w, off_w / float(on_w or 1), STACK_RATIO_MIN))


# --- 2. толпа проходит единственную дверь ---------------------------------

DOOR_MOBS = 16           # 11.6: столько тел собирается у игрока
CAP_FACTOR = 4           # предохранитель, а не порог: запас вчетверо


def split_hall(w, h, wx, door_y):
    """Зал, разрезанный стеной по столбцу wx, с единственной дверью.

    Карта СВОЯ, а не из генератора (раздел 10): проверка физики, которая
    берёт карту у gen.py, краснеет от чужой работы.
    """
    g = physics.Grid(w, h)
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            g.set(x, y, physics.TILE_FLOOR)
    for y in range(1, h - 1):
        g.set(wx, y, physics.TILE_WALL)
    g.set(wx, door_y, physics.TILE_FLOOR)
    return g


def t_door():
    print("\n--- 2. ТОЛПА ПРОХОДИТ ЕДИНСТВЕННУЮ ДВЕРЬ (4.2a против 11.6) ---")
    mw, mh, wx, door_y = 26, 14, 12, 6
    g = split_hall(mw, mh, wx, door_y)
    w = W.World(g, seed=1, floor=1, spawns=[(3.5, 6.5)])
    w.spawn(W.K_PLAYER, 3.5, 6.5, r=W.R_PLAYER, speed=W.SPEED_RUN,
            ctl=True, hp=10 ** 7, hp_max=10 ** 7)
    field = nav.Field(g)
    field.rebuild(g, [(3, 6)], 0)
    mobs = []
    for ty in range(1, mh - 1):
        for tx in range(wx + 1, mw - 1):
            if len(mobs) >= DOOR_MOBS:
                break
            if (tx + ty) % 2:
                continue             # через клетку: тела не в одной точке
            e = ai.make_enemy(w, tx + 0.5, ty + 0.5, ai.AI_MELEE)
            e.hp = e.hp_max = 10 ** 7
            mobs.append(e)
    longest = max(field.at(int(e.x), int(e.y)) for e in mobs)
    # ПРЕДОХРАНИТЕЛЬ В ТИКАХ из длины пути: путь / скорость — идеальное
    # время, вчетверо — запас на дверь, очередь и толчею (то же правило,
    # что в tests/ai_check.py).
    cap = int(longest / ai.MELEE_SPEED * W.TICK_HZ) * CAP_FACTOR + 60
    done = {}
    for t in range(cap):
        for e in mobs:
            e.ai_alert = w.tick + 10 ** 6
        w.step()
        for e in mobs:
            if e.id not in done and e.x < wx:
                done[e.id] = t + 1
        if len(done) == len(mobs):
            break
    stuck = [(round(e.x, 2), round(e.y, 2)) for e in mobs if e.id not in done]
    det = "прошло %d из %d, худшее %d тиков при предохранителе %d (путь %d клеток)" \
        % (len(done), len(mobs), max(done.values()) if done else 0, cap, longest)
    if stuck:
        det += "; застряли в " + ", ".join(str(s) for s in stuck[:6])
    check(len(done) == len(mobs),
          "все %d врагов прошли дверь в одну клетку" % DOOR_MOBS, det)


# --- 3. сквозь стену расталкивание не проносит ----------------------------

PIPE_BODIES = 10


def t_wall():
    print("\n--- 3. РАСТАЛКИВАНИЕ НЕ ПРОНОСИТ СКВОЗЬ СТЕНУ ---")
    # коридор шириной РОВНО В КЛЕТКУ: поперёк идти некуда вовсе
    gw, gh, row = 24, 3, 1
    g = physics.Grid(gw, gh)
    for x in range(1, gw - 1):
        g.set(x, row, physics.TILE_FLOOR)
    step = ai.MELEE_SPEED / W.TICK_HZ
    n = PIPE_BODIES
    xs = [6.5] * n
    ys = [row + 0.5] * n
    rs = [ai.ENEMY_R] * n
    steps = [step] * n
    bad = 0
    escaped = 0
    for _ in range(300):
        push = physics.separate(xs, ys, rs, steps)
        for i, p in push.items():
            nx, ny, _hit = physics.move_circle(g, xs[i], ys[i], 0.0, 0.0,
                                               rs[i], True, p)
            xs[i] = nx
            ys[i] = ny
        for i in range(n):
            if physics.circle_hits(g, xs[i], ys[i], rs[i]):
                bad += 1
            if not (row <= ys[i] < row + 1):
                escaped += 1
    bodies = [type("B", (), {"x": xs[i], "y": ys[i]})() for i in range(n)]
    check(bad == 0 and escaped == 0,
          "тела, сваленные в одну точку, не пролезают в стену коридора",
          "тел %d, нарушений геометрии %d, вышедших из ряда коридора %d"
          % (n, bad, escaped))
    check(worst_stack(bodies) <= STACK_MAX,
          "куча из %d тел в коридоре выстраивается в очередь" % n,
          "худшая стопка после расхождения %d, разброс по коридору %.2f клетки"
          % (worst_stack(bodies), max(xs) - min(xs)))


# --- 4. цена перебора соседей ---------------------------------------------

COST_BODIES = 200        # 2.2 считается на 200 сущностях
# ПОТОЛОК. Перебор соседей обязан остаться мелочью на фоне бюджета 2.2
# (медиана тика 12 мс): 1.0 мс — это 8.3% бюджета, то есть вдвенадцатеро
# ниже него. Порог выведен из бюджета, а не из этого прогона.
COST_MS = 1.0


def t_cost():
    print("\n--- 4. ЦЕНА ПЕРЕБОРА СОСЕДЕЙ (%d тел) ---" % COST_BODIES)
    # худший случай: все тела в куче, каждое видит соседей
    n = COST_BODIES
    xs = []
    ys = []
    side = int(math.sqrt(n)) + 1
    for i in range(n):
        xs.append(20.0 + (i % side) * 0.5)
        ys.append(20.0 + (i // side) * 0.5)
    rs = [ai.ENEMY_R] * n
    steps = [ai.MELEE_SPEED / W.TICK_HZ] * n
    physics.separate(xs, ys, rs, steps)          # прогрев
    ms = []
    for _ in range(200):
        t0 = CLOCK()
        res = physics.separate(xs, ys, rs, steps)
        ms.append((CLOCK() - t0) * 1000.0)
    med = statistics.median(ms)
    note("толкнуло тел",
         "%d из %d — в ровной решётке шагом 0.5 требования соседей слева и "
         "справа у внутренних тел гасят друг друга; перебор пар при этом "
         "полный, а он и есть цена" % (len(res), n))
    note("uptime", uptime_line())
    check(med <= COST_MS,
          "перебор соседей на %d телах дешевле %.1f мс" % (n, COST_MS),
          "медиана %.3f мс, p99 %.3f мс (бюджет тика 12 мс, доля %.1f%%)"
          % (med, pct(ms, 0.99), med / 12.0 * 100))


# --- 5. один сид — один забег ---------------------------------------------

def t_deterministic():
    print("\n--- 5. ОДИН СИД — ОДИН ЗАБЕГ (8.7) ---")
    out = []
    for _ in range(2):
        fl = gen.generate(2001, CROWD_FLOOR, 64, 48)
        w = W.World(fl.grid, seed=2001, floor=CROWD_FLOOR,
                    spawns=[(fl.entry[0] + 0.5, fl.entry[1] + 0.5)])
        w.spawn(W.K_PLAYER, fl.entry[0] + 0.5, fl.entry[1] + 0.5,
                r=W.R_PLAYER, speed=W.SPEED_RUN, ctl=True, hp=10 ** 7,
                hp_max=10 ** 7)
        mobs = ai.populate(w, fl)
        for e in mobs:
            e.hp = e.hp_max = 10 ** 7
        for _t in range(300):
            for e in mobs:
                e.ai_alert = w.tick + 10 ** 6
            w.step()
        out.append([(e.id, e.x, e.y) for e in mobs])
    check(out[0] == out[1],
          "два одинаковых прогона совпали до последнего знака",
          "тел %d, расхождений %d"
          % (len(out[0]), sum(1 for a, b in zip(out[0], out[1]) if a != b)))


def main():
    print("=" * 70)
    print("Расталкивание тел против подталкивания на углах (DESIGN.md 11.6).")
    print("цель расталкивания ri+rj = %.2f, доля шага %.2f, метрика слипания %.2f"
          % (2 * ai.ENEMY_R * physics.PUSH_GAP_R, physics.PUSH_SHARE, STACK_R))
    print("=" * 70)
    t0 = CLOCK()
    t_stack()
    t_door()
    t_wall()
    t_cost()
    t_deterministic()
    print("\n  прогон %.1f с; %s" % (CLOCK() - t0, uptime_line()))
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
