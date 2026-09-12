#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Гоняет ботов друг против друга и печатает винрейты. Tornado не нужен.

    python3 tools/balance.py            # быстрый прогон
    python3 tools/balance.py 6 matrix   # 6 сидов и матрица матчапов
"""

import collections
import itertools
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.game import const as C           # noqa: E402
from server.game.arena import ARENAS         # noqa: E402
from server.game.bots import Bot             # noqa: E402
from server.game.heroes import HERO_ORDER    # noqa: E402
from server.game.world import World          # noqa: E402

ARENA_IDS = list(ARENAS)


def duel(h0, h1, arena, seed, stocks=3, limit=150):
    ps = [{"pid": 1, "name": "A", "team": 0, "hero": h0, "bot": True},
          {"pid": 2, "name": "B", "team": 1, "hero": h1, "bot": True}]
    w = World(arena, ps, stocks=stocks, time_limit=limit)
    bots = {p["pid"]: Bot(p["pid"], 2, seed=seed * 31 + p["pid"]) for p in ps}
    n = 0
    while w.state != C.ST_OVER and n < 60 * (limit + 10):
        for b in bots.values():
            b.think(w)
        w.step()
        w.events.clear()
        n += 1
    return w


def main():
    seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    want_matrix = "matrix" in sys.argv

    wins = collections.Counter()
    games = collections.Counter()
    dmg = collections.defaultdict(list)
    times = []
    matrix = collections.defaultdict(lambda: [0, 0])

    for seed in range(seeds):
        for a, b in itertools.product(HERO_ORDER, HERO_ORDER):
            if a == b:
                continue
            arena = ARENA_IDS[(seed + HERO_ORDER.index(a) + HERO_ORDER.index(b)) % len(ARENA_IDS)]
            w = duel(a, b, arena, seed)
            games[a] += 1
            games[b] += 1
            times.append(w.time)
            matrix[(a, b)][1] += 1
            if w.winner == 0:
                wins[a] += 1
                matrix[(a, b)][0] += 1
            elif w.winner == 1:
                wins[b] += 1
            dmg[a].append(w.fighters[1].dmg_dealt)
            dmg[b].append(w.fighters[2].dmg_dealt)

    print(f"матчей: {sum(games.values()) // 2}   "
          f"средняя длина: {statistics.mean(times):.0f} с   "
          f"медиана: {statistics.median(times):.0f} с")
    print(f"\n{'герой':10}{'винрейт':>9}{'ср. урон':>10}")
    for h in HERO_ORDER:
        print(f"{h:10}{wins[h] / max(1, games[h]) * 100:8.0f}%{statistics.mean(dmg[h]):10.0f}")

    if want_matrix:
        print("\nматрица (строка бьёт столбец):")
        print("     " + "".join(f"{h[:5]:>7}" for h in HERO_ORDER))
        for a in HERO_ORDER:
            row = f"{a[:5]:5}"
            for b in HERO_ORDER:
                if a == b:
                    row += "      ·"
                    continue
                win, n = matrix[(a, b)]
                row += f"{win / max(1, n) * 100:6.0f}%"
            print(row)


if __name__ == "__main__":
    main()
