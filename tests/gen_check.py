#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка генератора этажа: связность, детерминизм, цена (этап 1).

Что проверяется (числами, а не словами):

  1. СВЯЗНОСТЬ на 1000 этажах с разными сидами. Обход ведёт САМ ТЕСТ,
     своим стеком, а не gen.distance_map: проверка не должна пользоваться
     той же функцией, что и проверяемый код, иначе общая ошибка сойдётся
     сама с собой. Требуется:
       * каждая проходимая клетка достижима от точки входа;
       * лестница достижима;
       * в каждой комнате есть достижимая клетка (без этого «100%
         достижимо» можно получить, отрезав все комнаты и объявив
         достижимым один чулан);
       * сторож связности gen.Floor.repairs не срабатывал ни разу
         (связность обязана быть от конструкции, а не от починки);
       * точки появления свободны для круга игрока r=0.35.
  2. ДЕТЕРМИНИЗМ: один сид дважды — md5 карты совпадает побайтово;
     разные сиды — разные карты.
  3. ЦЕНА генерации: медиана и p99 времени, рядом uptime.

Запуск:  python3 tests/gen_check.py [число_этажей]
"""

import hashlib
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

from server import gen, physics          # noqa: E402

N_FLOORS = 1000
MAP_W = 64
MAP_H = 48
R_PLAYER = 0.35
MIN_STAIRS_DIST = 20      # клеток пути от входа до лестницы, вывод — ниже

_fails = []


def check(ok, name, detail=""):
    if not ok:
        _fails.append(name)
    print("  [%s] %s%s" % ("ok" if ok else "ПРОВАЛ", name,
                           ("   " + detail) if detail else ""))
    return ok


def uptime_line():
    try:
        return subprocess.run(["uptime"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return "uptime недоступен"


def loadavg1():
    try:
        return os.getloadavg()[0]
    except Exception:
        return -1.0


def pct(vals, q):
    v = sorted(vals)
    i = min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))
    return v[i]


def flood(grid, start):
    """Свой обход в ширину по 4 соседям. Возвращает bytearray 0/1."""
    w, h, tiles = grid.w, grid.h, grid.tiles
    seen = bytearray(w * h)
    sx, sy = start
    i0 = sy * w + sx
    if tiles[i0] == gen.TILE_WALL:
        return seen
    seen[i0] = 1
    stack = [i0]
    while stack:
        i = stack.pop()
        x = i % w
        for j, ok in ((i - 1, x > 0), (i + 1, x < w - 1),
                      (i - w, i >= w), (i + w, i + w < w * h)):
            if ok and not seen[j] and tiles[j] != gen.TILE_WALL:
                seen[j] = 1
                stack.append(j)
    return seen


def one_floor(seed, floor=1):
    t0 = time.perf_counter()
    fl = gen.generate(seed, floor, MAP_W, MAP_H)
    gen_ms = (time.perf_counter() - t0) * 1000.0
    g = fl.grid
    w = g.w
    seen = flood(g, fl.entry)
    passable = 0
    reached = 0
    for i, v in enumerate(g.tiles):
        if v != gen.TILE_WALL:
            passable += 1
            if seen[i]:
                reached += 1
    # В каждой комнате обязана быть хоть одна ДОСТИЖИМАЯ клетка. Центр брать
    # нельзя: на нём законно может стоять столб (12% этажей), и проверка
    # краснела бы на исправной карте.
    rooms_ok = True
    for r in fl.rooms:
        hit = False
        for ty in range(r[1], r[3] + 1):
            row = ty * w
            for tx in range(r[0], r[2] + 1):
                if seen[row + tx]:
                    hit = True
                    break
            if hit:
                break
        if not hit:
            rooms_ok = False
            break
    st = fl.stairs
    stairs_ok = (st is not None
                 and g.tiles[st[1] * w + st[0]] == gen.TILE_STAIRS
                 and bool(seen[st[1] * w + st[0]]))
    # Спуск обязан быть спуском, а не ступенькой у входа (8.1). Порог 20
    # клеток выведен из скорости бега 5 кл/с (4.2): меньше 20 клеток — это
    # меньше 4 секунд хода, то есть этаж пройден раньше, чем начат.
    # Наблюдённый минимум на 1000 этажах — 45 клеток, запас к порогу 2.2x.
    far_ok = fl.stairs_room != fl.entry_room and fl.stairs_dist >= MIN_STAIRS_DIST
    spawn_ok = True
    for x, y in fl.spawns:
        if physics.circle_hits(g, x, y, R_PLAYER):
            spawn_ok = False
        if not seen[int(y) * w + int(x)]:
            spawn_ok = False
    return fl, {
        "all_reached": passable == reached,
        "rooms_ok": rooms_ok,
        "stairs_ok": stairs_ok,
        "far_ok": far_ok,
        "stairs_dist": fl.stairs_dist,
        "spawn_ok": spawn_ok,
        "repairs": fl.repairs,
        "passable": passable,
        "rooms": len(fl.rooms),
        "gen_ms": gen_ms,
    }


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else N_FLOORS
    print("=" * 70)
    print("Приёмка генератора этажа. %d этажей %dx%d, тема %r."
          % (n, MAP_W, MAP_H, gen.DEFAULT_THEME))
    print("=" * 70)

    good_all = 0
    good_rooms = 0
    good_stairs = 0
    good_spawn = 0
    good_far = 0
    repairs = 0
    dists = []
    rooms_n = []
    pass_n = []
    times = []
    hashes = {}
    clock = time.perf_counter

    for k in range(n):
        seed = 1000 + k * 7919          # разные сиды, ряд фиксирован
        fl, r = one_floor(seed)
        times.append(r["gen_ms"])          # чистая генерация, без обхода проверки
        good_all += r["all_reached"]
        good_rooms += r["rooms_ok"]
        good_stairs += r["stairs_ok"]
        good_spawn += r["spawn_ok"]
        good_far += r["far_ok"]
        dists.append(r["stairs_dist"])
        repairs += r["repairs"]
        rooms_n.append(r["rooms"])
        pass_n.append(r["passable"])
        hashes[hashlib.md5(bytes(fl.grid.tiles)).hexdigest()] = seed

    print("\n--- 1. Связность, %d этажей ---" % n)
    check(good_all == n, "весь проходимый пол достижим от точки входа",
          "%d/%d = %.2f%%" % (good_all, n, good_all * 100.0 / n))
    check(good_rooms == n, "в каждой комнате есть достижимая клетка",
          "%d/%d = %.2f%%" % (good_rooms, n, good_rooms * 100.0 / n))
    check(good_stairs == n, "лестница на месте и достижима",
          "%d/%d = %.2f%%" % (good_stairs, n, good_stairs * 100.0 / n))
    check(good_spawn == n, "круг игрока r=%.2f влезает во все точки появления"
          % R_PLAYER, "%d/%d = %.2f%%" % (good_spawn, n, good_spawn * 100.0 / n))
    check(good_far == n,
          "лестница не в стартовой комнате и дальше %d клеток пути"
          % MIN_STAIRS_DIST,
          "%d/%d; путь вход->лестница: минимум %d, медиана %d, максимум %d"
          % (good_far, n, min(dists), statistics.median(dists), max(dists)))
    check(repairs == 0, "сторож связности не срабатывал (связность от конструкции)",
          "срабатываний: %d" % repairs)
    print("  комнат:          медиана %d, минимум %d, максимум %d"
          % (statistics.median(rooms_n), min(rooms_n), max(rooms_n)))
    print("  проходимых клеток: медиана %d, минимум %d, максимум %d  (из %d клеток карты)"
          % (statistics.median(pass_n), min(pass_n), max(pass_n), MAP_W * MAP_H))

    print("\n--- 2. Детерминизм по сиду ---")
    a = gen.generate(20240918, 3, MAP_W, MAP_H)
    b = gen.generate(20240918, 3, MAP_W, MAP_H)
    ha = hashlib.md5(bytes(a.grid.tiles)).hexdigest()
    hb = hashlib.md5(bytes(b.grid.tiles)).hexdigest()
    check(ha == hb, "один сид дважды — та же карта побайтово",
          "md5 %s == %s" % (ha[:16], hb[:16]))
    check(a.stairs == b.stairs and a.spawns == b.spawns and a.entry == b.entry,
          "лестница, вход и точки появления те же",
          "лестница %s, вход %s" % (a.stairs, a.entry))
    c = gen.generate(20240919, 3, MAP_W, MAP_H)
    hc = hashlib.md5(bytes(c.grid.tiles)).hexdigest()
    check(ha != hc, "соседний сид — другая карта", "md5 %s != %s" % (ha[:16], hc[:16]))
    d = gen.generate(20240918, 4, MAP_W, MAP_H)
    hd = hashlib.md5(bytes(d.grid.tiles)).hexdigest()
    check(ha != hd, "тот же сид, следующий этаж — другая карта",
          "md5 %s != %s" % (ha[:16], hd[:16]))
    check(len(hashes) == n, "все %d карт различны" % n,
          "различных md5: %d" % len(hashes))
    # Классическая поломка детерминизма — обход множества или словаря:
    # его порядок зависит от PYTHONHASHSEED, то есть от запуска. Проверяем
    # это единственным честным способом — двумя процессами.
    code = ("import sys,hashlib;sys.path.insert(0,%r);"
            "from server import gen;h=hashlib.md5();"
            "[ (h.update(bytes(f.grid.tiles)),h.update(repr(f.stairs).encode()),"
            "h.update(repr(f.spawns).encode())) for f in "
            "(gen.generate(sd,2,%d,%d) for sd in (1,2,3,777,20240918)) ];"
            "print(h.hexdigest())" % (ROOT, MAP_W, MAP_H))
    outs = []
    for hs in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=hs)
        outs.append(subprocess.run([sys.executable, "-c", code], env=env,
                                   capture_output=True, text=True,
                                   timeout=60).stdout.strip())
    check(len(set(outs)) == 1 and outs[0],
          "карта не зависит от PYTHONHASHSEED (нет обхода множеств по хешу)",
          "хеши: %s" % ", ".join(o[:12] for o in outs))
    spawn_uniq = min(len(set(gen.generate(1000 + k * 7919, 1, MAP_W, MAP_H).spawns))
                     for k in range(0, 200))
    check(spawn_uniq == gen.MAX_SPAWNS,
          "все %d точек появления различны (на 200 этажах)" % gen.MAX_SPAWNS,
          "минимум различных: %d" % spawn_uniq)

    print("\n--- 3. Цена генерации (чистая gen.generate, без обхода проверки) ---")
    gmed = statistics.median(times)
    g99 = pct(times, 0.99)
    print("  медиана %7.2f мс, p99 %7.2f мс, максимум %7.2f мс"
          % (gmed, g99, max(times)))
    print("  генерация происходит один раз при старте комнаты, не в тике;")
    print("  бюджет 2.6 — 5 с от запуска до играбельного экрана, запас x%.0f"
          % (5000.0 / max(gmed, 1e-9)))
    print("  uptime: %s" % uptime_line())
    la = loadavg1()
    if la > 2.0:
        print("  ВНИМАНИЕ: loadavg %.2f > 2 — машина занята, число ГРЯЗНОЕ." % la)
    else:
        print("  loadavg %.2f (<= 2) — замеру можно верить." % la)

    print("\n--- размеры карты: крайние случаи ---")
    for (w, h) in ((24, 18), (64, 48), (96, 72), (128, 128)):
        t0 = clock()
        fl, r = 0, 0
        f2 = gen.generate(777, 1, w, h)
        ms = (clock() - t0) * 1000.0
        seen = flood(f2.grid, f2.entry)
        pas = sum(1 for v in f2.grid.tiles if v != gen.TILE_WALL)
        rea = sum(1 for i, v in enumerate(f2.grid.tiles)
                  if v != gen.TILE_WALL and seen[i])
        ok = (pas == rea and f2.repairs == 0)
        check(ok, "%3dx%3d: комнат %2d, пола %5d, связно, %.1f мс"
              % (w, h, len(f2.rooms), pas, ms))

    print("\n" + "=" * 70)
    if _fails:
        print("ПРОВАЛЕНО: %s" % ", ".join(_fails))
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
