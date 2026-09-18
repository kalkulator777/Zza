#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка тумана войны: общий на команду, со стенами, и цена в тике.

Что проверяется:

  5а. ОБЗОР ОБЩИЙ, А НЕ ПЕРСОНАЛЬНЫЙ (4.4): игрок A уходит в дальний угол,
      и клетки, открытые A, видны в общем тумане, пока B стоит на месте у
      входа; убираем A — клетки остаются «открыты, но не видны».
  5б. СТЕНА ПЕРЕКРЫВАЕТ: клетка за стеной не видна никому, хотя по прямому
      расстоянию она ближе, чем видимые клетки в открытую сторону.
      Это та проверка, которую обязан заваливать «круг по радиусу».
   -  ДЕЛЬТА СХОДИТСЯ: клиентская копия, собранная только из vis-дельт,
      совпадает с состоянием сервера на каждом тике.
   -  КОДЕР: разреженная сборка RLE в vis.py даёт БАЙТ В БАЙТ то же, что
      proto.rle_encode над полным массивом с 255 на неизменившихся клетках.
   4.  ЦЕНА В ТИКЕ: тот же мир (6 игроков, 200 сущностей), два прогона —
      с туманом и с заглушкой вместо тумана. Разница и есть цена 4.4.

Запуск:  python3 tests/vis_check.py [число_тиков]
"""

import math
import os
import random
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

from server import gen, physics, proto, vis           # noqa: E402
from server import world as world_mod                 # noqa: E402
from server.room import Player, Room                  # noqa: E402

N_PLAYERS = 6
N_MOBS = 200
TICKS = 300
WARMUP = 60
SEED = 424242

OLD_MEDIAN_MS = 0.95      # число этапа 0a, до тумана
OLD_P99_MS = 1.15
BUDGET_MEDIAN_MS = 12.0   # 2.2
BUDGET_P99_MS = 25.0

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


class NullConn(object):
    __slots__ = ("last", "n")

    def __init__(self):
        self.last = ""
        self.n = 0

    def send_str(self, text):
        self.last = text
        self.n += 1


class NullFog(object):
    """Заглушка «тумана нет»: ровно то, чем был сервер до этапа 1."""

    __slots__ = ("grid",)

    def __init__(self, grid):
        self.grid = grid

    def update(self, viewers):
        return None

    def full_encoded(self):
        return None

    def delta_encoded(self):
        return None


# --- 5а. общий обзор --------------------------------------------------------

def test_team_fog():
    print("\n--- 5а. Обзор ОБЩИЙ на команду (4.4) ---")
    fl = gen.generate(SEED, 1, 64, 48)
    g = fl.grid
    fog = vis.Fog(g)
    ax, ay = fl.spawns[0]
    # B стоит у входа, A уходит в дальний угол — на лестницу
    bx, by = ax, ay
    fog.update([(1, ax, ay), (2, bx, by)])
    near_lit = fog.lit_count()

    sx, sy = fl.stairs
    fog.update([(1, sx + 0.5, sy + 0.5), (2, bx, by)])
    lit_a = set(fog._fov[1])
    lit_b = set(fog._fov[2])

    check(not (lit_a & lit_b),
          "A у лестницы и B у входа смотрят в разные места",
          "у A %d клеток, у B %d, общих %d" % (len(lit_a), len(lit_b),
                                               len(lit_a & lit_b)))
    only_a = sorted(lit_a - lit_b)
    ok = all(fog.state[i] == vis.VIS_LIT for i in only_a)
    check(ok, "клетки, которые видит ТОЛЬКО A, горят в общем тумане команды",
          "таких клеток %d" % len(only_a))
    check(fog.at(sx, sy) == vis.VIS_LIT,
          "лестница в дальнем углу освещена, пока B стоит у входа",
          "состояние клетки лестницы %d" % fog.at(sx, sy))

    # A погиб: его клетки обязаны стать «открыты, но не видны», а не погаснуть
    fog.update([(2, bx, by)])
    st = [fog.state[i] for i in only_a]
    check(all(v == vis.VIS_SEEN for v in st),
          "A выбыл — его клетки стали «открыты, но не видны», а не забылись",
          "VIS_SEEN у %d из %d" % (sum(1 for v in st if v == vis.VIS_SEEN),
                                   len(st)))
    check(fog.at(int(bx), int(by)) == vis.VIS_LIT,
          "клетка под B осталась видимой", "")
    print("    (для справки: оба у входа — %d горящих клеток)" % near_lit)


# --- 5б. стены перекрывают --------------------------------------------------

def test_walls_block():
    print("\n--- 5б. Стена перекрывает обзор (8.2, 11.4) ---")
    # Своя маленькая карта: зал 21x11, поперёк — стена с одним проёмом.
    w, h = 21, 11
    g = physics.Grid(w, h)
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            g.set(x, y, physics.TILE_FLOOR)
    wall_x = 10
    for y in range(1, h - 1):
        g.set(wall_x, y, physics.TILE_WALL)
    g.set(wall_x, 5, physics.TILE_FLOOR)          # проём ровно напротив игрока

    fog = vis.Fog(g, radius=10)
    px, py = 4, 5
    fog.update([(1, px + 0.5, py + 0.5)])

    # Клетки за стеной, и все — ВНУТРИ радиуса обзора: иначе проверка
    # зеленела бы просто потому, что до них далеко, и подсадку «круг без
    # стен» не поймала бы.
    behind = [(11, 3), (12, 2), (11, 8), (12, 7), (12, 8), (13, 3)]
    far = [p for p in behind if math.hypot(p[0] - 4, p[1] - 5) > vis.VIEW_RADIUS]
    check(not far, "все проверяемые клетки за стеной внутри радиуса обзора",
          "вне радиуса: %s" % (far or "ни одной"))
    seen_behind = [p for p in behind if fog.is_lit(*p)]
    check(not seen_behind,
          "клетки за стеной не видны никому",
          "видны вопреки стене: %s (расстояния %s)"
          % (seen_behind or "ни одной",
             [round(math.hypot(p[0] - 4, p[1] - 5), 1) for p in behind]))

    check(fog.is_lit(12, 5) and fog.is_lit(13, 5),
          "сквозь проём видно по прямой", "клетки (12,5),(13,5)")
    # контрольная точка: та же дистанция, но в открытую сторону
    check(fog.is_lit(4, 1) and fog.is_lit(1, 5),
          "в открытую сторону на том же расстоянии видно", "")
    d_behind = math.hypot(12 - px, 8 - py)
    d_open = math.hypot(1 - px, 5 - py)
    print("    расстояние до невидимой клетки за стеной %.1f, до видимой в"
          " открытую сторону %.1f — дело в стене, а не в радиусе"
          % (d_behind, d_open))
    lit = fog.lit_count()
    total = sum(1 for v in g.tiles if v != 0)
    check(lit < total,
          "видна не вся карта (иначе тумана нет)",
          "горит %d клеток из %d проходимых" % (lit, total))


# --- дельта и кодер ---------------------------------------------------------

def test_delta_and_encoder():
    print("\n--- Дельта тумана: сходимость и кодер ---")
    fl = gen.generate(SEED + 1, 1, 64, 48)
    g = fl.grid
    n = g.w * g.h
    fog = vis.Fog(g)
    client = bytearray(proto.rle_decode(fog.full_encoded()))   # пустой туман
    late = None                                                # вошедший позже

    def apply(buf, s):
        raw = proto.rle_decode(s)
        for i, v in enumerate(raw):
            if v != vis.VIS_KEEP:
                buf[i] = v

    check(len(client) == n, "полный туман декодируется в %d клеток" % n,
          "получено %d" % len(client))

    x, y = fl.spawns[0]
    rnd = random.Random(7)
    bad = 0
    bad_late = 0
    identical = 0
    checked = 0
    for t in range(400):
        ang = rnd.uniform(0, 6.283)
        x, y, _hit = physics.move_circle(g, x, y, math.cos(ang) * 0.4,
                                         math.sin(ang) * 0.4, 0.35)
        fog.update([(1, x, y)])
        # порядок как в room.broadcast_snapshot: сперва дельта всем,
        # потом полный — тому, кто только что вошёл
        d = fog.delta_encoded()
        if d is not None:
            # то же самое, собранное в лоб: полный массив с 255 там, где
            # клиент уже знает верное значение, и proto.rle_encode поверх
            buf = bytearray([vis.VIS_KEEP]) * n
            st = fog.state
            for i in range(n):
                if client[i] != st[i]:
                    buf[i] = st[i]
            if proto.rle_encode(bytes(buf)) == d:
                identical += 1
            checked += 1
            apply(client, d)
            if late is not None:
                apply(late, d)
        if bytes(client) != bytes(fog.state):
            bad += 1
        if t == 200:
            # вошедший посреди партии получает полный туман
            late = bytearray(proto.rle_decode(fog.full_encoded()))
        if late is not None and bytes(late) != bytes(fog.state):
            bad_late += 1

    check(bad == 0, "клиентская копия из дельт совпадает с сервером на всех тиках",
          "расхождений: %d из 400" % bad)
    check(late is not None and bad_late == 0,
          "вошедший посреди партии получает туман целиком и дальше живёт дельтами",
          "расхождений после входа: %d" % bad_late)
    check(checked and identical == checked,
          "разреженный RLE = proto.rle_encode байт в байт",
          "совпало %d из %d дельт" % (identical, checked))
    print("    экономия: обзор пересчитан %d раз на %d тиков (%.0f%%)"
          % (fog.casts, fog.updates, fog.casts * 100.0 / fog.updates))


# --- 4. цена в тике ---------------------------------------------------------

def build(with_fog):
    room = Room("VISC", seed=SEED)
    rnd = random.Random(SEED)
    for i in range(N_PLAYERS):
        room.add(Player(i + 1, "Игрок%d" % (i + 1), NullConn()))
    room.start()
    w = room.world
    if not with_fog:
        w.fog = NullFog(w.grid)
    for p in room.players.values():
        p.needs_full = False
    mobs = []
    for _ in range(N_MOBS):
        while True:
            x = rnd.uniform(2.0, w.grid.w - 2.0)
            y = rnd.uniform(2.0, w.grid.h - 2.0)
            if not physics.circle_hits(w.grid, x, y, 0.3):
                break
        a = rnd.uniform(0, 6.283)
        e = w.spawn(world_mod.K_ENEMY, x, y, r=0.3, hp=30, hp_max=30)
        e.vx = 3.0 * math.cos(a)
        e.vy = 3.0 * math.sin(a)
        mobs.append(e)
    w.snapshot_full()
    return room, mobs, rnd


def steer(mobs, rnd):
    for e in mobs:
        if e.vx == 0.0 and e.vy == 0.0:
            a = rnd.uniform(0, 6.283)
            e.vx = 3.0 * math.cos(a)
            e.vy = 3.0 * math.sin(a)


def feed(room, seq, i):
    """Игроки бегут по прямой и меняют направление раз в 40 тиков.

    Это ХУДШИЙ случай для тумана: бег по прямой на 5 кл/с меняет клетку
    каждые 6 тиков у каждого из шести игроков, то есть обзор пересчитывается
    примерно раз в тик. Случайное дёрганье (как в bench_tick) давало бы
    число красивее и честным не было бы.
    """
    k = 0
    for p in room.players.values():
        a = ((i // 40) * 1.7 + k * 1.05)
        p.pending = {"seq": seq, "mv": (math.cos(a), math.sin(a)),
                     "aim": (0.0, 0.0), "btn": 0}
        k += 1


def run_ticks(room, mobs, rnd, ticks):
    samples = []
    clock = time.perf_counter
    for i in range(WARMUP):
        steer(mobs, rnd)
        feed(room, i, i)
        room.tick()
    for i in range(ticks):
        steer(mobs, rnd)
        feed(room, WARMUP + i, WARMUP + i)
        t0 = clock()
        room.tick()
        samples.append((clock() - t0) * 1000.0)
    return samples


def test_tick_cost(ticks):
    print("\n--- 4. Цена обзора в тике: %d игроков, %d сущностей ---"
          % (N_PLAYERS, N_MOBS + N_PLAYERS))
    off = run_ticks(*build(False), ticks=ticks)
    room_on, mobs_on, rnd_on = build(True)
    on = run_ticks(room_on, mobs_on, rnd_on, ticks)
    # третий прогон, снова без тумана: если «первый прогон всегда дороже»,
    # это видно тут, и цену тумана на разогрев не спишешь
    off_again = run_ticks(*build(False), ticks=ticks)

    m_off, p_off = statistics.median(off), pct(off, 0.99)
    m_on, p_on = statistics.median(on), pct(on, 0.99)
    fog = room_on.world.fog
    print("  без тумана (как на этапе 0a): медиана %6.3f мс, p99 %6.3f мс"
          % (m_off, p_off))
    print("  с туманом:                    медиана %6.3f мс, p99 %6.3f мс"
          % (m_on, p_on))
    print("  разница:                      медиана %+6.3f мс (x%.2f), p99 %+6.3f мс (x%.2f)"
          % (m_on - m_off, m_on / m_off, p_on - p_off, p_on / p_off))
    print("  без тумана, повторный прогон: медиана %6.3f мс — разогрев ни при чём"
          % statistics.median(off_again))
    print("  для сверки с этапом 0a (карта была другая): было %.2f / %.2f мс"
          % (OLD_MEDIAN_MS, OLD_P99_MS))
    print("  обзор пересчитан %d раз за %d тиков (%.2f пересчёта на тик)"
          % (fog.casts, fog.updates, fog.casts / float(fog.updates)))

    # цена одного пересчёта — чтобы худший случай считался, а не гадался
    g = room_on.world.grid
    spots = [(int(e.x), int(e.y)) for e in room_on.world.entities.values()][:40]
    t0 = time.perf_counter()
    for tx, ty in spots:
        vis.field_of_view(g, tx, ty)
    one = (time.perf_counter() - t0) * 1000.0 / max(1, len(spots))
    print("  один пересчёт обзора: %.3f мс, то есть худший тик (все %d игроков"
          % (one, N_PLAYERS))
    print("  сменили клетку разом) дороже обычного на %.2f мс" % (one * N_PLAYERS))

    check(m_on <= BUDGET_MEDIAN_MS, "медиана в бюджете 2.2",
          "%.3f мс из %.0f, запас x%.1f" % (m_on, BUDGET_MEDIAN_MS,
                                            BUDGET_MEDIAN_MS / m_on))
    check(p_on <= BUDGET_P99_MS, "p99 в бюджете 2.2",
          "%.3f мс из %.0f, запас x%.1f" % (p_on, BUDGET_P99_MS,
                                            BUDGET_P99_MS / p_on))
    ratio = m_on / m_off
    if ratio > 2.0:
        print("  ВНИМАНИЕ: тик подорожал больше чем вдвое (x%.2f)." % ratio)
    la = loadavg1()
    print("  uptime: %s" % uptime_line())
    if la > 2.0:
        print("  ВНИМАНИЕ: loadavg %.2f > 2 — машина занята, числа ГРЯЗНЫЕ." % la)
    else:
        print("  loadavg %.2f (<= 2) — замеру можно верить." % la)

    # трафик: сколько байт добавляет поле vis
    w = room_on.world
    tails = []
    vis_bytes = []
    for i in range(60):
        steer(mobs_on, rnd_on)
        feed(room_on, 9000 + i, 9000 + i)
        room_on.apply_inputs()
        w.step()
        d = w.fog.delta_encoded()
        vis_bytes.append(len(d) if d else 0)
    print("  поле vis в дельте: медиана %d байт, максимум %d байт"
          " -> при 30 Гц %.1f КБ/с в худшем случае"
          % (statistics.median(vis_bytes), max(vis_bytes),
             max(vis_bytes) * 30 / 1024.0))
    full = w.fog.full_encoded()
    print("  полный туман (входящему): %d байт" % len(full))


def main():
    ticks = int(sys.argv[1]) if len(sys.argv) > 1 else TICKS
    print("=" * 70)
    print("Приёмка тумана войны (DESIGN.md 4.4, 11.2). Радиус обзора %d клеток."
          % vis.VIEW_RADIUS)
    print("=" * 70)
    test_team_fog()
    test_walls_block()
    test_delta_and_encoder()
    test_tick_cost(ticks)
    print("\n" + "=" * 70)
    if _fails:
        print("ПРОВАЛЕНО: %s" % "; ".join(_fails))
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
