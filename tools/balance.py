#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Гоняет ботов друг против друга и печатает винрейты. Tornado не нужен.

    python3 tools/balance.py              # быстрый прогон
    python3 tools/balance.py 8 matrix     # 8 сидов и матрица матчапов
    python3 tools/balance.py smoke        # все пары x все арены + 2v2: ищем падения
    python3 tools/balance.py 8 matrix -j1 # без параллелизма (для отладки)

Все герои берутся из HERO_ORDER, так что новые подхватываются сами.
"""

import collections
import itertools
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.game import const as C           # noqa: E402
from server.game.arena import ARENAS         # noqa: E402
from server.game.bots import Bot, MELEE, PREF_RANGE  # noqa: E402
from server.game.heroes import HERO_ORDER    # noqa: E402
from server.game.world import World          # noqa: E402

ARENA_IDS = list(ARENAS)


def run_match(team0, team1, arena, seed, stocks=3, limit=150, level=2, snap=False):
    """Гоняет матч N на N и возвращает мир после конца. team* — списки id героев."""
    ps = []
    pid = 0
    for team, heroes in ((0, team0), (1, team1)):
        for h in heroes:
            pid += 1
            ps.append({"pid": pid, "name": "P%d" % pid, "team": team,
                       "hero": h, "bot": True})
    w = World(arena, ps, stocks=stocks, time_limit=limit)
    bots = {p["pid"]: Bot(p["pid"], level, seed=seed * 31 + p["pid"]) for p in ps}
    n = 0
    cap = 60 * (limit + 10)
    while w.state != C.ST_OVER and n < cap:
        for b in bots.values():
            b.think(w)
        w.step()
        if snap:
            w.snapshot()      # заодно проверяем, что снапшот собирается
        else:
            w.events.clear()
        n += 1
    return w


def duel(h0, h1, arena, seed, stocks=3, limit=150):
    return run_match([h0], [h1], arena, seed, stocks=stocks, limit=limit)


# --------------------------------------------------------------- сбор данных
def _job(args):
    """Одна дуэль. Возвращает только примитивы — чтобы ездило между процессами."""
    a, b, arena, seed = args
    w = duel(a, b, arena, seed)
    return (a, b, w.winner, w.time,
            w.fighters[1].dmg_dealt, w.fighters[2].dmg_dealt,
            1 if (w.state != C.ST_OVER or w.time >= 149.0) else 0)


def _map(fn, tasks, jobs):
    if jobs <= 1:
        return [fn(t) for t in tasks]
    import multiprocessing
    with multiprocessing.Pool(jobs) as pool:
        return pool.map(fn, tasks, chunksize=4)


def balance(seeds, want_matrix, jobs):
    tasks = []
    for seed in range(seeds):
        for a, b in itertools.product(HERO_ORDER, HERO_ORDER):
            if a == b:
                continue
            arena = ARENA_IDS[(seed + HERO_ORDER.index(a) + HERO_ORDER.index(b)) % len(ARENA_IDS)]
            tasks.append((a, b, arena, seed))

    results = _map(_job, tasks, jobs)

    wins = collections.Counter()
    games = collections.Counter()
    dmg = collections.defaultdict(list)
    times = []
    timeouts = 0
    matrix = collections.defaultdict(lambda: [0, 0])

    for a, b, winner, t, d0, d1, to in results:
        games[a] += 1
        games[b] += 1
        times.append(t)
        timeouts += to
        matrix[(a, b)][1] += 1
        if winner == 0:
            wins[a] += 1
            matrix[(a, b)][0] += 1
        elif winner == 1:
            wins[b] += 1
        dmg[a].append(d0)
        dmg[b].append(d1)

    print("матчей: %d   средняя длина: %.0f с   медиана: %.0f с   до таймаута: %d"
          % (len(results), statistics.mean(times), statistics.median(times), timeouts))
    print("\n%-10s%9s%10s" % ("герой", "винрейт", "ср. урон"))
    worst = []
    for h in HERO_ORDER:
        wr = wins[h] / max(1, games[h]) * 100
        worst.append((wr, h))
        flag = "" if 42 <= wr <= 58 else "   <-- вне 42..58"
        print("%-10s%8.0f%%%10.0f%s" % (h, wr, statistics.mean(dmg[h]), flag))

    if want_matrix:
        # симметризуем: клетка (a,b) = доля побед a над b на обеих сторонах
        print("\nматрица (строка бьёт столбец, обе стороны усреднены):")
        print("     " + "".join("%7s" % h[:5] for h in HERO_ORDER))
        for a in HERO_ORDER:
            row = "%-5s" % a[:5]
            for b in HERO_ORDER:
                if a == b:
                    row += "      ·"
                    continue
                wa, na = matrix[(a, b)]
                wb, nb = matrix[(b, a)]
                tot = na + nb
                won = wa + (nb - wb)
                row += "%6.0f%%" % (won / max(1, tot) * 100)
            print(row)

    spread = max(w for w, _ in worst) - min(w for w, _ in worst)
    print("\nразброс винрейтов: %.0f п.п." % spread)


# ------------------------------------------------------------------- smoke
def _smoke_job(args):
    kind, t0, t1, arena, seed = args
    try:
        w = run_match(t0, t1, arena, seed, stocks=2, limit=150, snap=True)
    except Exception as exc:                                  # noqa: BLE001
        import traceback
        return (kind, t0, t1, arena, "%s: %s" % (type(exc).__name__, exc),
                traceback.format_exc(), 0.0, 0)
    to = 1 if (w.state != C.ST_OVER or w.time >= 149.0) else 0
    return (kind, t0, t1, arena, None, None, w.time, to)


def smoke(jobs):
    tasks = []
    # 1v1: все упорядоченные пары x все арены
    for arena in ARENA_IDS:
        for a, b in itertools.product(HERO_ORDER, HERO_ORDER):
            tasks.append(("1v1", [a], [b], arena, 5))
    # зеркалки — отдельно, они ловят рекурсию в on_take/on_deal
    for arena in ARENA_IDS:
        for a in HERO_ORDER:
            tasks.append(("mirror", [a, a], [a, a], arena, 7))
    # 2v2: каждый герой в паре со следующим против пары со сдвигом
    n = len(HERO_ORDER)
    for arena in ARENA_IDS:
        for i in range(n):
            for k in (1, 3, 5):
                t0 = [HERO_ORDER[i], HERO_ORDER[(i + k) % n]]
                t1 = [HERO_ORDER[(i + 2) % n], HERO_ORDER[(i + k + 4) % n]]
                tasks.append(("2v2", t0, t1, arena, 11 + k))

    print("smoke: %d матчей (1v1 все пары x %d арен, зеркалки 2v2, 2v2 комбинации)"
          % (len(tasks), len(ARENA_IDS)))
    results = _map(_smoke_job, tasks, jobs)

    fails = [r for r in results if r[4] is not None]
    timeouts = sum(r[7] for r in results)
    times = [r[6] for r in results if r[4] is None]
    for kind, t0, t1, arena, err, tb, _, _ in fails[:5]:
        print("\nПАДЕНИЕ [%s] %s vs %s на %s: %s\n%s" % (kind, t0, t1, arena, err, tb))
    for kind, t0, t1, arena, err, _tb, t, to in results:
        if to:
            print("НЕ ДОИГРАН [%s] %s vs %s на %s (%.0f с)" % (kind, t0, t1, arena, t))
    print("\nпадений: %d   не доиграно до конца: %d   средняя длина: %.0f с"
          % (len(fails), timeouts, statistics.mean(times) if times else 0))
    if not fails and not timeouts:
        print("ок: ни одного исключения, все матчи закончились по стокам")
    return 1 if (fails or timeouts) else 0


def main():
    argv = sys.argv[1:]
    jobs = os.cpu_count() or 1
    for a in list(argv):
        if a.startswith("-j"):
            jobs = max(1, int(a[2:] or 1))
            argv.remove(a)

    # герои без настроек бота играли бы овощами и портили замер
    missing = [h for h in HERO_ORDER if h not in PREF_RANGE]
    if missing:
        print("ВНИМАНИЕ: нет PREF_RANGE для %s — бот сыграет за них овощем" % missing)
    if not (MELEE & set(HERO_ORDER)):
        print("ВНИМАНИЕ: в MELEE нет ни одного героя из ростера")

    if "smoke" in argv:
        sys.exit(smoke(jobs))

    seeds = 3
    for a in argv:
        if a.isdigit():
            seeds = int(a)
    balance(seeds, "matrix" in argv, jobs)


if __name__ == "__main__":
    main()
