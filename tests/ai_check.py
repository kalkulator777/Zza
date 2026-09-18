#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка врагов: путь по градиенту, шум, зрение через туман, замах.

Что здесь проверяется и почему именно так.

  1. ГРАДИЕНТ ВЕДЁТ ВНИЗ И ПРОВОДИТ В ДВЕРЬ. Карта своя, маленькая (правило
     10: проверка физики не берёт карту у генератора). Меряется то, что
     8.3 называет предусловием этапа: враг, катящийся по градиенту, у самой
     двери едет строго вдоль оси — ровно залипающий случай 4.2a.
  2. ВРАГИ ДОХОДЯТ. Главное число участка. Карта — настоящая, из генератора,
     и это осознанно: проверяется поведение НА НАСТОЯЩЕЙ ГЕОМЕТРИИ, а порог
     при этом от генератора не зависит — требуется 100% дошедших, а
     связность пола гарантирована построением BSP (см. gen.py) и отдельно
     проверена в gen_check.py. Потолок по времени — В ТИКАХ и выведен из
     длины пути (правило 10: порог не может быть временем на загруженной
     машине). Не дошедший НАЗЫВАЕТСЯ ПОИМЁННО: клетка, где встал.
  3. ЦЕНА ВОЛНЫ. Одна многоисточниковая против шести персональных, на
     настоящем этаже 64x48. Числа 8.3: 0.241 против 1.542 мс.
  4. ШУМ — ВОЛНА, А НЕ КРУГ. Ключевая пара на ОДНОЙ И ТОЙ ЖЕ паре точек:
     враг в 3.0 клетки по прямой, но в 17 клетках по полу, ближней атаки НЕ
     слышит (6 < 17), а выстрел слышит (18 >= 17). Круг радиуса 6 услышал
     бы ближнюю атаку сквозь стену — вот это и есть разница.
  5. ВРАГ ВИДИТ ТУМАНОМ, А НЕ ЛУЧОМ. За стеной не видит, в прямой видимости
     видит. Рядом — цена решения: байт тумана против луча на каждого врага.
  6. ЗАМАХ ЧИТАЕМ. Игрок, нажавший рывок по появлению бита F_WINDUP, из-под
     удара уходит; неподвижный — получает. Числа в тиках.
  7. ВРАГ УМИРАЕТ И РАССТАВЛЕН ПО 8.1: не в стартовой комнате, числом от
     глубины этажа.

Запуск:  python3 tests/ai_check.py [число_этажей_в_п.2]
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

from server import ai, combat, gen, nav, physics, vis as vis_mod   # noqa: E402
from server import world as world_mod                             # noqa: E402

W = world_mod
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


# --- свои карты (правило 10) ----------------------------------------------

def hall(w, h):
    g = physics.Grid(w, h)
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            g.set(x, y, physics.TILE_FLOOR)
    return g


def split_hall(w=26, h=12, wx=12, door_y=1):
    """Зал, разрезанный стеной по столбцу wx, с единственной дверью door_y.

    Ради этой карты всё и затевалось: по прямой через стену — три клетки,
    по полу — семнадцать. Круг по радиусу и волна по полу здесь расходятся
    так, что перепутать нельзя.
    """
    g = hall(w, h)
    for y in range(1, h - 1):
        g.set(wx, y, physics.TILE_WALL)
    g.set(wx, door_y, physics.TILE_FLOOR)
    return g


def make_world(grid):
    return W.World(grid, seed=1, floor=1, spawns=[(2.5, 2.5)])


def add_player(w, x, y, hp=100):
    return w.spawn(W.K_PLAYER, x, y, r=W.R_PLAYER, speed=W.SPEED_RUN,
                   ctl=True, hp=hp, hp_max=hp)


def wake(e, w, ticks=10 ** 6):
    e.ai_alert = w.tick + ticks
    return e


# --- 1. градиент ----------------------------------------------------------

def t_gradient():
    print("\n--- 1. градиент: ведёт вниз и проводит в дверь (8.3 + 4.2a) ---")
    g = split_hall(w=26, h=12, wx=12, door_y=5)
    w = make_world(g)
    p = add_player(w, 3.5, 5.5, hp=10 ** 6)
    field = w.nav
    field.rebuild(g, [(int(p.x), int(p.y))], 0)

    # (а) чисто арифметика: из каждой клетки пола шаг строго уменьшает dist
    bad = 0
    worst = None
    for ty in range(g.h):
        for tx in range(g.w):
            d0 = field.at(tx, ty)
            if d0 <= 0:
                continue
            gx, gy, _ = field.step_dir(tx + 0.5, ty + 0.5)
            if gx == 0.0 and gy == 0.0:
                bad += 1
                worst = (tx, ty, d0)
                continue
            nx = int(tx + 0.5 + gx * 0.75)
            ny = int(ty + 0.5 + gy * 0.75)
            if not (0 <= field.at(nx, ny) < d0):
                bad += 1
                worst = (tx, ty, d0)
    check(bad == 0, "из каждой клетки пола шаг строго уменьшает расстояние",
          "клеток-исключений %d%s" % (bad, "" if worst is None
                                      else ", худшая %s" % (worst,)))

    # (б) живое тело: враг из дальнего угла проходит единственную дверь
    e = ai.make_enemy(w, 22.5, 9.5, ai.AI_MELEE)
    wake(e, w)
    start_d = field.at(22, 9)
    through = 0
    for t in range(600):
        w.step()
        if e.x < 12.0:
            through = t + 1
            break
    check(through > 0, "враг прошёл дверь в одну клетку (без 4.2a — залип бы)",
          "тиков %d, путь по волне %d клеток, встал бы на (12,5)"
          % (through, start_d))
    note("клеток волны за секунду",
         "%.2f при скорости тела %.2f кл/с — больше, потому что волна "
         "четырёхсвязная, а тело ходит и по диагонали"
         % ((start_d - field.at(int(e.x), int(e.y))) / max(1, through) * 30.0,
            ai.MELEE_SPEED))


# --- 2. враги доходят -----------------------------------------------------

ARRIVE = 1.6            # клетки: удар достаёт на 1.2 + 0.35 = 1.55 (4.2)
CAP_FACTOR = 4          # предохранитель, а не порог: запас вчетверо
STALL_TICKS = 90        # 3 с без прогресса по волне — считаем, что встал


def t_reach(n_floors=30):
    print("\n--- 2. ВРАГИ ДОХОДЯТ (главное число участка) ---")
    print("  по одному ближнему врагу в КАЖДУЮ комнату этажа, кроме стартовой;")
    print("  дошёл = подошёл на %.1f клетки, то есть в дальность своего удара."
          % ARRIVE)
    total = 0
    arrived = 0
    times = []
    paths = []
    ratios = []
    stuck = []
    t_wall = CLOCK()
    for k in range(n_floors):
        seed = 1000 + k
        fl = gen.generate(seed, 1, 64, 48)
        w = make_world(fl.grid)
        w.stairs = fl.stairs
        p = add_player(w, fl.entry[0] + 0.5, fl.entry[1] + 0.5, hp=10 ** 7)
        field = nav.Field(fl.grid)
        field.rebuild(fl.grid, [(fl.entry[0], fl.entry[1])], 0)
        mobs = []
        for i, r in enumerate(fl.rooms):
            if i == fl.entry_room:
                continue
            cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
            d0 = field.at(cx, cy)
            if d0 <= 0:
                continue
            if physics.circle_hits(fl.grid, cx + 0.5, cy + 0.5, ai.ENEMY_R):
                continue
            e = ai.make_enemy(w, cx + 0.5, cy + 0.5, ai.AI_MELEE)
            wake(e, w)
            mobs.append([e, d0, (cx, cy), 0, d0, 0])   # ent, d0, старт, t, best, stall
        if not mobs:
            continue
        longest = max(m[1] for m in mobs)
        # ПРЕДОХРАНИТЕЛЬ В ТИКАХ, выведенный из длины пути: путь / скорость
        # это идеальное время, вчетверо — запас на двери, углы и толчею.
        cap = int(longest / ai.MELEE_SPEED * W.TICK_HZ) * CAP_FACTOR + 60
        left = len(mobs)
        for t in range(cap):
            w.step()
            for m in mobs:
                if m[3]:
                    continue
                e = m[0]
                if math.hypot(e.x - p.x, e.y - p.y) <= ARRIVE:
                    m[3] = t + 1
                    left -= 1
                    continue
                d = w.nav.at(int(e.x), int(e.y))
                if 0 <= d < m[4]:
                    m[4] = d
                    m[5] = 0
                else:
                    m[5] += 1
            if left == 0:
                break
        for e, d0, start, tt, best, stall in mobs:
            total += 1
            paths.append(d0)
            if tt:
                arrived += 1
                times.append(tt)
                ratios.append(tt / (d0 / ai.MELEE_SPEED * W.TICK_HZ))
            else:
                stuck.append((seed, start, (round(e.x, 2), round(e.y, 2)),
                              d0, best, stall))
    dur = CLOCK() - t_wall
    frac = arrived * 100.0 / max(1, total)
    check(arrived == total,
          "доля дошедших = 100%%  (%d из %d врагов, %d этажей)"
          % (arrived, total, n_floors),
          "получилось %.2f%%" % frac)
    if times:
        note("время в пути",
             "медиана %d тиков (%.2f с), p99 %d, максимум %d"
             % (statistics.median(times), statistics.median(times) / 30.0,
                pct(times, 0.99), max(times)))
        note("длина пути по волне",
             "медиана %d клеток, максимум %d" % (statistics.median(paths),
                                                 max(paths)))
        note("тиков на идеальные (путь/скорость)",
             "медиана x%.2f, худший x%.2f, предохранитель x%d"
             % (statistics.median(ratios), max(ratios), CAP_FACTOR))
    if stuck:
        print("  НЕ ДОШЛИ — поимённо, клетка, где встали:")
        for seed, start, where, d0, best, stall in stuck[:20]:
            print("    сид %d: вышел из %s, встал в %s, путь был %d, "
                  "дошёл до %d, без прогресса %d тиков"
                  % (seed, start, where, d0, best, stall))
    note("прогон", "%d этажей, %.1f с" % (n_floors, dur))


# --- 3. цена волны --------------------------------------------------------

def t_wave_cost(reps=60):
    print("\n--- 3. цена волны на настоящем этаже (8.3) ---")
    fl = gen.generate(20250918, 1, 64, 48)
    g = fl.grid
    floor_cells = sum(1 for v in g.tiles if v != physics.TILE_WALL)
    # шесть игроков по разным комнатам — как в замере 8.3
    pts = []
    for i, r in enumerate(fl.rooms):
        if len(pts) >= 6:
            break
        pts.append(((r[0] + r[2]) // 2, (r[1] + r[3]) // 2))
    one = []
    many = []
    noise = []
    for _ in range(reps):
        t0 = CLOCK()
        nav.distance_map(g, pts)
        one.append((CLOCK() - t0) * 1000.0)
        t0 = CLOCK()
        for q in pts:
            nav.distance_map(g, [q])
        many.append((CLOCK() - t0) * 1000.0)
        t0 = CLOCK()
        nav.noise_wave(g, [pts[0]], W.NOISE_SHOT)
        noise.append((CLOCK() - t0) * 1000.0)
    m1 = statistics.median(one)
    m6 = statistics.median(many)
    mn = statistics.median(noise)
    note("этаж", "64x48, проходимых клеток %d, источников %d"
         % (floor_cells, len(pts)))
    note("одна многоисточниковая волна",
         "медиана %.3f мс (8.3 обещает 0.241)" % m1)
    note("шесть персональных волн",
         "медиана %.3f мс (8.3 обещает 1.542)" % m6)
    note("волна шума, предел %d клеток" % W.NOISE_SHOT,
         "медиана %.3f мс (8.3 обещает 0.046)" % mn)
    check(m1 < m6, "одна волна дешевле шести персональных",
          "вшестеро по замыслу, вышло в %.1f раза" % (m6 / m1 if m1 else 0))
    # На тик это делится ещё на NAV_PERIOD: волна считается раз в 6 тиков.
    note("в пересчёте на тик",
         "%.3f мс (одна волна раз в %d тиков)" % (m1 / nav.NAV_PERIOD,
                                                  nav.NAV_PERIOD))
    note("uptime", uptime_line())
    try:
        la = os.getloadavg()[0]
        note("loadavg за минуту", "%.2f — %s"
             % (la, "замеру можно верить" if la <= 2.0
                else "МАШИНА ЗАНЯТА, число грязное"))
    except Exception:
        pass


# --- 4. шум ---------------------------------------------------------------

def t_noise():
    print("\n--- 4. шум: волна по полу, а не круг по радиусу (8.2) ---")
    g = split_hall(w=26, h=12, wx=12, door_y=1)
    # три точки, посчитанные руками по карте:
    #   игрок (10.5, 8.5) — тайл (10,8), у самой стены
    #   близкий враг (13.5, 8.5) — тайл (13,8): 3.0 по прямой, 17 по полу
    #   дальний враг (22.5, 8.5) — тайл (22,8): 12.0 по прямой, 26 по полу
    probe = nav.distance_map(g, [(10, 8)])
    d_near = probe[8 * g.w + 13]
    d_far = probe[8 * g.w + 22]
    line_near = 3.0
    line_far = 12.0
    note("геометрия карты",
         "ближний враг: по прямой %.1f, по полу %d; дальний: %.1f и %d"
         % (line_near, d_near, line_far, d_far))
    note("громкости 8.2", "выстрел %d, ближняя атака %d, рывок %d"
         % (W.NOISE_SHOT, W.NOISE_MELEE, W.NOISE_DASH))

    def run(action):
        w = make_world(g)
        p = add_player(w, 10.5, 8.5, hp=10 ** 6)
        p.facing = 0.0
        near = ai.make_enemy(w, 13.5, 8.5, ai.AI_MELEE)
        far = ai.make_enemy(w, 22.5, 8.5, ai.AI_MELEE)
        w.step()                       # туман посчитан, шумов ещё нет
        near.ai_alert = 0
        far.ai_alert = 0
        if action == "shot":
            p.btn = 8                  # BTN_SHOOT: снаряд (см. combat.begin)
        elif action == "melee":
            p.btn = 1                  # BTN_ATTACK
        elif action == "dash":
            p.btn = 2
            p.mv = (0.0, -1.0)
        w.step()
        p.btn = 0
        p.mv = (0.0, 0.0)
        w.step()                       # ai.step разбирает шум прошлого тика
        return (near.ai_alert > w.tick, far.ai_alert > w.tick, w.noise_waves)

    n_shot, f_shot, waves_shot = run("shot")
    n_melee, f_melee, _ = run("melee")
    n_dash, f_dash, _ = run("dash")

    check(n_shot, "ВЫСТРЕЛ слышно за стеной, из-за двери",
          "враг в %d клетках по полу поднят (громкость %d)"
          % (d_near, W.NOISE_SHOT))
    check(not f_shot, "выстрел НЕ слышно за пределом громкости",
          "враг в %d клетках по полу не поднят (громкость %d)"
          % (d_far, W.NOISE_SHOT))
    check(not n_melee, "БЛИЖНЮЮ АТАКУ не слышно сквозь стену",
          "враг в %.1f клетки ПО ПРЯМОЙ и %d ПО ПОЛУ не поднят; круг радиуса "
          "%d его бы поднял" % (line_near, d_near, W.NOISE_MELEE))
    check(not n_dash and not f_dash, "рывок почти беззвучен",
          "громкость %d, не поднят никто" % W.NOISE_DASH)
    check(waves_shot == 1, "на все источники одной громкости — ОДНА волна",
          "волн за выстрел: %d" % waves_shot)

    # ближняя атака слышна тем, кто и так рядом — иначе шум не шум
    w = make_world(g)
    p = add_player(w, 10.5, 8.5, hp=10 ** 6)
    room = ai.make_enemy(w, 6.5, 8.5, ai.AI_MELEE)    # 4 клетки по полу
    w.step()
    room.ai_alert = 0
    p.btn = 1
    w.step()
    p.btn = 0
    w.step()
    check(room.ai_alert > w.tick, "ближнюю атаку слышно в своей комнате",
          "враг в 4 клетках по полу поднят (громкость %d)" % W.NOISE_MELEE)

    # и главное следствие: услышал — ДОШЁЛ, а не постоял и забыл
    w = make_world(g)
    p = add_player(w, 10.5, 8.5, hp=10 ** 7)
    far2 = ai.make_enemy(w, 13.5, 8.5, ai.AI_MELEE)
    w.step()
    far2.ai_alert = 0
    p.btn = 8
    w.step()
    p.btn = 0
    got = 0
    for t in range(600):
        w.step()
        if math.hypot(far2.x - p.x, far2.y - p.y) <= ARRIVE:
            got = t + 1
            break
    check(got > 0, "поднятый выстрелом враг ДОХОДИТ до стрелявшего",
          "%d тиков (%.1f с) на %d клеток пути; запас ALERT_TICKS = %d"
          % (got, got / 30.0, d_near, ai.ALERT_TICKS))


# --- 5. зрение через туман ------------------------------------------------

def t_sight():
    print("\n--- 5. враг видит туманом, а не лучом (4.4) ---")
    g = split_hall(w=26, h=12, wx=12, door_y=1)
    w = make_world(g)
    p = add_player(w, 10.5, 8.5, hp=10 ** 6)
    behind = ai.make_enemy(w, 13.5, 8.5, ai.AI_MELEE)   # 3.0 по прямой, за стеной
    seen = ai.make_enemy(w, 7.5, 8.5, ai.AI_MELEE)      # 3.0 по прямой, в зале
    # ДВА шага, а не один, и это не магия: ai.step стоит в начале тика, а
    # fog.update — в конце, значит ИИ читает туман ПРОШЛОГО тика. Отставание
    # ровно один тик (33 мс) и намеренное: считать туман дважды за тик
    # дороже, чем знать про врага на 33 мс позже.
    w.step()
    w.step()
    lit_behind = w.fog.at(int(behind.x), int(behind.y))
    lit_seen = w.fog.at(int(seen.x), int(seen.y))
    check(lit_behind != vis_mod.VIS_LIT and behind.ai_alert <= w.tick,
          "враг ЗА СТЕНОЙ игрока не видит",
          "3.0 клетки по прямой, состояние тумана на его клетке %d (LIT=%d)"
          % (lit_behind, vis_mod.VIS_LIT))
    check(lit_seen == vis_mod.VIS_LIT and seen.ai_alert > w.tick,
          "враг В ПРЯМОЙ ВИДИМОСТИ игрока видит",
          "3.0 клетки, состояние тумана %d" % lit_seen)

    d0 = math.hypot(behind.x - p.x, behind.y - p.y)
    for _ in range(60):
        w.step()
    d1 = math.hypot(behind.x - p.x, behind.y - p.y)
    check(abs(d1 - d0) < 1e-6, "невидящий враг и не двигается",
          "за 60 тиков сместился на %.4f клетки" % abs(d1 - d0))

    # --- цена решения ---
    fl = gen.generate(777, 1, 64, 48)
    gw = make_world(fl.grid)
    ps = []
    for i, r in enumerate(fl.rooms[:6]):
        ps.append(add_player(gw, (r[0] + r[2]) // 2 + 0.5,
                             (r[1] + r[3]) // 2 + 0.5))
    mobs = ai.populate(gw, fl)
    while len(mobs) < 200:
        r = fl.rooms[len(mobs) % len(fl.rooms)]
        x = (r[0] + r[2]) // 2 + 0.5
        y = (r[1] + r[3]) // 2 + 0.5
        mobs.append(ai.make_enemy(gw, x, y, ai.AI_MELEE))
    gw.step()
    n = len(mobs)
    grid = fl.grid
    state = gw.fog.state
    W_ = grid.w
    reps = 200
    t0 = CLOCK()
    for _ in range(reps):
        acc = 0
        for e in mobs:
            if state[int(e.y) * W_ + int(e.x)] == vis_mod.VIS_LIT:
                acc += 1
    fog_ms = (CLOCK() - t0) * 1000.0 / reps
    reps2 = 20
    t0 = CLOCK()
    for _ in range(reps2):
        acc = 0
        for e in mobs:
            for p2 in ps:
                if ai.line_clear(grid, e.x, e.y, p2.x, p2.y):
                    acc += 1
                    break
    ray_ms = (CLOCK() - t0) * 1000.0 / reps2
    note("цена на тик, %d врагов, %d игроков" % (n, len(ps)),
         "байт тумана %.3f мс, луч на каждого врага %.3f мс, в %.0f раз дороже"
         % (fog_ms, ray_ms, ray_ms / fog_ms if fog_ms else 0))
    note("что это значит для 2.2",
         "медиана тика сейчас ~1.3 мс при бюджете 12; лучи добавили бы "
         "%.1f мс, то есть %.0f%% бюджета" % (ray_ms, ray_ms / 12.0 * 100))
    check(ray_ms > fog_ms * 5,
          "луч на каждого врага заметно дороже байта тумана",
          "%.3f против %.3f мс" % (ray_ms, fog_ms))


# --- 6. замах читаем ------------------------------------------------------

def t_windup():
    print("\n--- 6. замах врага читаем: рывок из-под удара (4.2) ---")
    note("числа 4.2", "замах врага %d тиков (%.3f с, вилка 12-18); рывок %d "
                      "тиков, %.3f клетки; удар достаёт %.2f клетки"
         % (ai.MELEE_WIND, ai.MELEE_WIND / 30.0, combat.DASH_TICKS,
            combat.DASH_SPEED * combat.DASH_TICKS * W.DT,
            combat.MELEE_REACH + W.R_PLAYER))

    def duel(dodge):
        g = hall(24, 12)
        w = make_world(g)
        p = add_player(w, 10.5, 5.5, hp=100)
        e = ai.make_enemy(w, 11.9, 5.5, ai.AI_MELEE)
        wake(e, w)
        saw = 0
        pressed = 0
        hit_at = 0
        for t in range(60):
            if saw and dodge and not pressed:
                p.btn = 2                      # BTN_DASH
                p.mv = (1.0, 0.0)              # прочь от врага
                pressed = w.tick + 1
            w.step()
            p.btn = 0
            if not saw and (e.flags & W.F_WINDUP):
                saw = w.tick
            if p.hp < 100:
                hit_at = w.tick
                break
        return saw, pressed, hit_at, p, e

    saw, _, hit_at, p, e = duel(False)
    check(saw > 0 and hit_at > 0, "неподвижный игрок удар получает",
          "бит WINDUP с тика %d, урон на тике %d — это %d тиков форы"
          % (saw, hit_at, hit_at - saw))
    check(hit_at - saw >= 11,
          "между видимым замахом и уроном не меньше 11 тиков",
          "фактически %d (замах %d, минус тик на появление бита)"
          % (hit_at - saw, ai.MELEE_WIND))

    saw2, pressed2, hit2, p2, e2 = duel(True)
    dist = math.hypot(p2.x - e2.x, p2.y - e2.y)
    check(hit2 == 0, "игрок, нажавший рывок по биту WINDUP, удар не получает",
          "нажал на тике %d (бит с %d), к удару отъехал на %.2f клетки при "
          "дальности удара %.2f" % (pressed2, saw2, dist,
                                    combat.MELEE_REACH + W.R_PLAYER))
    note("запас на реакцию",
         "%d тиков (%.0f мс) от появления бита до удара; рывок занимает %d "
         "тиков (%.0f мс)" % (ai.MELEE_WIND - 1, (ai.MELEE_WIND - 1) / 30.0 * 1000,
                              combat.DASH_TICKS, combat.DASH_TICKS / 30.0 * 1000))
    note("для сравнения", "замах ИГРОКА %d тиков (4.2)" % combat.MELEE_WINDUP_TICKS)


# --- 7. стрелок, смерть, расстановка --------------------------------------

def t_ranged():
    print("\n--- 7. второй вид: стрелок держит дистанцию и стреляет ---")
    g = hall(40, 12)
    w = make_world(g)
    p = add_player(w, 6.5, 5.5, hp=10 ** 6)
    e = ai.make_enemy(w, 20.5, 5.5, ai.AI_RANGED)
    wake(e, w)
    shots = 0
    wound = 0
    dists = []
    for t in range(240):
        w.step()
        d = abs(e.x - p.x)
        dists.append(d)
        if e.flags & W.F_WINDUP:
            wound += 1
        for ev in w.events:
            if ev[1] == "shot" and ev[2].get("a") == e.id:
                shots += 1
        del w.events[:]
    tail = dists[90:]
    check(shots >= 2, "стрелок стреляет", "выстрелов за 8 с: %d (откат %d тиков)"
          % (shots, ai.RANGED_CD))
    check(all(ai.RANGED_NEAR - 1.0 <= d <= ai.RANGED_FAR + 1.0 for d in tail),
          "стрелок держит полосу %.1f..%.1f клетки"
          % (ai.RANGED_NEAR, ai.RANGED_FAR),
          "фактически %.2f..%.2f" % (min(tail), max(tail)))
    check(wound > 0, "перед выстрелом виден замах (бит WINDUP)",
          "тиков с битом %d, замах %d тиков" % (wound, ai.RANGED_WIND))

    # пятится, если подойти вплотную
    w2 = make_world(g)
    p2 = add_player(w2, 6.5, 5.5, hp=10 ** 6)
    e2 = ai.make_enemy(w2, 9.0, 5.5, ai.AI_RANGED)
    wake(e2, w2)
    d0 = abs(e2.x - p2.x)
    for _ in range(45):
        w2.step()
    check(abs(e2.x - p2.x) > d0 + 0.5, "стрелок пятится от подошедшего вплотную",
          "было %.2f, стало %.2f" % (d0, abs(e2.x - p2.x)))


def t_death_and_spawn():
    print("\n--- 8. враг умирает; расстановка по 8.1 ---")
    g = hall(24, 12)
    w = make_world(g)
    p = add_player(w, 10.5, 5.5)
    e = ai.make_enemy(w, 11.5, 5.5, ai.AI_MELEE)
    eid = e.id
    hp0 = e.hp
    p.facing = 0.0
    hits = 0
    for _ in range(200):
        if eid not in w.entities:
            break
        p.btn = 1
        w.step()
        p.btn = 0
        if w.entities.get(eid) is None:
            break
        hits = hp0 - w.entities[eid].hp
    check(eid not in w.entities, "враг умирает от ударов игрока и убирается",
          "hp %d, урон удара %d, снято %d" % (hp0, combat.MELEE_DMG, hits))

    counts = []
    for fl_n in (1, 2, 5, 11, 20):
        fl = gen.generate(4242, fl_n, 64, 48)
        w2 = make_world(fl.grid)
        mobs = ai.populate(w2, fl)
        ex0, ey0, ex1, ey1 = fl.rooms[fl.entry_room]
        inside = sum(1 for e in mobs
                     if ex0 <= int(e.x) <= ex1 and ey0 <= int(e.y) <= ey1)
        counts.append((fl_n, len(mobs), inside))
    check(all(c[2] == 0 for c in counts), "в стартовой комнате врагов нет (8.1)",
          "по этажам: " + ", ".join("этаж %d: %d в стартовой" % (c[0], c[2])
                                    for c in counts))
    check(counts[0][1] < counts[-1][1], "врагов больше с глубиной (8.1)",
          ", ".join("этаж %d: %d" % (c[0], c[1]) for c in counts))

    # детерминизм: один сид — один бестиарий
    fl = gen.generate(99, 3, 64, 48)
    a = [(round(e.x, 4), round(e.y, 4), e.ai) for e in ai.populate(make_world(fl.grid), fl)]
    b = [(round(e.x, 4), round(e.y, 4), e.ai) for e in ai.populate(make_world(fl.grid), fl)]
    check(a == b and len(a) > 0, "один сид — та же расстановка (8.7)",
          "%d врагов, совпали" % len(a))


# --- главная --------------------------------------------------------------

def main():
    n_floors = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print("=" * 70)
    print("Приёмка врагов: 8.3 путь, 8.2 шум, 4.4 зрение, 4.2 замах.")
    print("=" * 70)
    t_gradient()
    t_reach(n_floors)
    t_wave_cost()
    t_noise()
    t_sight()
    t_windup()
    t_ranged()
    t_death_and_spawn()
    print("\n" + "=" * 70)
    if FAILS:
        print("КРАСНО, %d проверок:" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
