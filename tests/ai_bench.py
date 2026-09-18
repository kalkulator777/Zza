#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Стенд тика с врагами: DESIGN.md 2.2 (медиана <= 12 мс, p99 <= 25 мс при
6 игроках и 200 сущностях).

Отличие от tests/bench_tick.py: там 200 бродяг с прямой скоростью и без ИИ,
здесь 200 НАСТОЯЩИХ врагов — карта расстояний, градиент, туман, замахи,
шум от стрельбы игроков. То есть то, чего в опорном замере этапа 1 не было
и о чём он прямо писал «тик подорожает, запас виден ниже».

Меряются два режима, и оба честные:

  * СПЯЩИЕ — реальный ранний этаж: игроки в одном углу, враги по всей карте
    в темноте. Враг вне освещённой клетки стоит трёх операций и выходит;
  * РАЗБУЖЕННЫЕ — худший случай: ВСЕ 200 врагов подняты и катятся к игрокам,
    игроки при этом стреляют, то есть каждый выстрел ещё и считает волну
    шума. Такого на настоящем этаже не бывает (200 врагов на этаж не
    ставится вовсе, потолок 48), это верхняя граница.

Запуск:  python3 tests/ai_bench.py [число_тиков]
"""

import os
import random
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

from server import ai, gen, nav, physics, proto      # noqa: E402
from server import world as world_mod                # noqa: E402
from server.room import Player, Room                 # noqa: E402

N_PLAYERS = 6
N_MOBS = 200
TICKS = 300
WARMUP = 60
TARGET_MEDIAN_MS = 12.0
TARGET_P99_MS = 25.0
CLOCK = time.perf_counter


class NullConn(object):
    __slots__ = ("last", "n")

    def __init__(self):
        self.last = ""
        self.n = 0

    def send_str(self, text):
        self.last = text
        self.n += 1


def uptime_line():
    try:
        return subprocess.run(["uptime"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return "uptime недоступен"


def pct(vals, q):
    v = sorted(vals)
    return v[min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))]


def build(seed=4242, n_mobs=N_MOBS):
    room = Room("AIBN", seed=seed)
    for i in range(N_PLAYERS):
        room.add(Player(i + 1, "Игрок%d" % (i + 1), NullConn()))
    room.start()                       # мир, этаж и штатная расстановка (8.1)
    w = room.world
    for p in room.players.values():
        p.needs_full = False
    fl_rooms = [r for r in gen.generate(seed, 1, 64, 48).rooms]
    rnd = random.Random(seed ^ 0xA1)
    mobs = [e for e in w.entities.values() if e.kind == world_mod.K_ENEMY]
    # добиваем до N_MOBS: 2.2 меряется на 200 сущностях, а штатный потолок
    # расстановки 48 — стенд обязан брать верхнюю границу контракта
    while len(mobs) < n_mobs:
        r = fl_rooms[rnd.randrange(len(fl_rooms))]
        x = rnd.randint(r[0], r[2]) + 0.5
        y = rnd.randint(r[1], r[3]) + 0.5
        if physics.circle_hits(w.grid, x, y, ai.ENEMY_R):
            continue
        kind = ai.AI_RANGED if len(mobs) % ai.RANGED_EVERY == ai.RANGED_EVERY - 1 \
            else ai.AI_MELEE
        mobs.append(ai.make_enemy(w, x, y, kind))
    # И игроки, И враги бессмертны: 2.2 меряется на 200 сущностях, а не на
    # том, сколько их доживёт до конца прогона. Со смертными врагами к
    # трёхсотому тику их остаётся 139, и число выходит заниженным — то есть
    # враньё в свою пользу.
    for p in room.players.values():
        e = w.entities.get(p.ent_id)
        if e is not None:
            e.hp = e.hp_max = 10 ** 7
    for e in mobs:
        e.hp = e.hp_max = 10 ** 7
    w.snapshot_full()
    return room, mobs


def feed(room, rnd, seq, shoot):
    for p in room.players.values():
        btn = 0
        if shoot:
            btn = 8 if (seq % 20) < 2 else (1 if (seq % 20) < 4 else 0)
        room.players[p.pid].pending = {
            "seq": seq, "mv": (rnd.uniform(-1, 1), rnd.uniform(-1, 1)),
            "aim": (0.0, 0.0), "btn": btn}


def run(label, awake, shoot, ticks):
    room, mobs = build()
    w = room.world
    rnd = random.Random(7)
    for i in range(WARMUP):
        if awake:
            for e in mobs:
                e.ai_alert = w.tick + 10 ** 6
        feed(room, rnd, i, shoot)
        room.tick()
    samples = []
    ai_ms = []
    waves0 = w.nav.waves
    noise0 = w.noise_waves
    for i in range(ticks):
        if awake:
            for e in mobs:
                e.ai_alert = w.tick + 10 ** 6
        feed(room, rnd, WARMUP + i, shoot)
        t0 = CLOCK()
        room.tick()
        samples.append((CLOCK() - t0) * 1000.0)
        if i % 7 == 0:
            t1 = CLOCK()
            ai.step(w, world_mod.DT)
            ai_ms.append((CLOCK() - t1) * 1000.0)
    med = statistics.median(samples)
    p99 = pct(samples, 0.99)
    alive = sum(1 for e in w.entities.values()
                if e.kind == world_mod.K_ENEMY)
    print("\n--- %s ---" % label)
    print("  сущностей в мире %d (врагов живых %d), игроков %d"
          % (len(w.entities), alive, N_PLAYERS))
    print("  медиана   %7.3f мс   (цель <= %.0f, запас x%.1f)"
          % (med, TARGET_MEDIAN_MS, TARGET_MEDIAN_MS / med if med else 0))
    print("  p99       %7.3f мс   (цель <= %.0f, запас x%.1f)"
          % (p99, TARGET_P99_MS, TARGET_P99_MS / p99 if p99 else 0))
    print("  среднее   %7.3f мс   min %.3f   max %.3f"
          % (statistics.fmean(samples), min(samples), max(samples)))
    print("  из них ai.step ~%.3f мс (%.0f%% тика)"
          % (statistics.median(ai_ms),
             statistics.median(ai_ms) / med * 100 if med else 0))
    print("  волн пути %d (раз в %d тиков), волн шума %d за %d тиков"
          % (w.nav.waves - waves0, nav.NAV_PERIOD,
             w.noise_waves - noise0, ticks))
    print("  вердикт: %s" % ("ПРОХОДИМ" if med <= TARGET_MEDIAN_MS
                             and p99 <= TARGET_P99_MS else "НЕ ПРОХОДИМ"))
    return med, p99


def main():
    ticks = int(sys.argv[1]) if len(sys.argv) > 1 else TICKS
    print("=" * 70)
    print("Стенд тика с врагами. %d игроков, %d врагов, карта 64x48, %d тиков."
          % (N_PLAYERS, N_MOBS, ticks))
    print("Бюджет 2.2: медиана <= %.0f мс, p99 <= %.0f мс."
          % (TARGET_MEDIAN_MS, TARGET_P99_MS))
    print("=" * 70)
    ok = True
    m1, p1 = run("СПЯЩИЕ: враги в темноте, игроки не стреляют", False, False, ticks)
    ok = ok and m1 <= TARGET_MEDIAN_MS and p1 <= TARGET_P99_MS
    m2, p2 = run("РАЗБУЖЕННЫЕ: все 200 катятся к игрокам, игроки стреляют",
                 True, True, ticks)
    ok = ok and m2 <= TARGET_MEDIAN_MS and p2 <= TARGET_P99_MS
    print("\n  цена ИИ в худшем случае: %+.3f мс к медиане (%.3f -> %.3f)"
          % (m2 - m1, m1, m2))
    print("  uptime: %s" % uptime_line())
    try:
        la = os.getloadavg()[0]
        print("  loadavg за минуту %.2f — %s"
              % (la, "замеру можно верить" if la <= 2.0
                 else "МАШИНА ЗАНЯТА, число грязное"))
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
