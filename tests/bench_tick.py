#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Стенд тика сервера: DESIGN.md 2.2 (медиана <= 12 мс, p99 <= 25 мс при
6 игроках и 200 сущностях) и 2.3 (трафик <= 200 КБ/с на клиента).

Сети нет: меряется ровно room.tick() — применение ввода, шаг мира с
физикой, сборка дельты и сериализация с рассылкой на 6 игроков.

Чего в замере НЕТ и почему:
  * ИИ врагов (этап 2, кода ещё нет) — тик подорожает, запас виден ниже;
  * tornado и сокеты — по условию 10 «без сети»;
  * подкрутка направлений у сущностей-бродяг — это леса стенда, не сервер,
    поэтому она вынесена за таймер.

Запуск:  python3 tests/bench_tick.py [число_тиков]
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

from server import physics, proto, world as world_mod   # noqa: E402
from server.room import Player, Room                    # noqa: E402

N_PLAYERS = 6
N_MOBS = 200           # сверх игроков: считаем по верхней границе
TICKS = 300
WARMUP = 60

TARGET_MEDIAN_MS = 12.0
TARGET_P99_MS = 25.0
TRAFFIC_BUDGET_KBS = 200.0


class NullConn(object):
    """Соединение-пустышка: до сокета не доходим, но строку получаем."""

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


def loadavg1():
    try:
        return os.getloadavg()[0]
    except Exception:
        return -1.0


def pct(vals, q):
    v = sorted(vals)
    i = min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))
    return v[i]


def build(seed=42):
    room = Room("BNCH", seed=seed)
    rnd = random.Random(seed)
    conns = []
    for i in range(N_PLAYERS):
        c = NullConn()
        conns.append(c)
        p = Player(i + 1, "Игрок%d" % (i + 1), c)
        room.add(p)                   # вошли в лобби, сущностей ещё нет
    room.start()                      # мир рождается здесь, из настроек
    w = room.world
    for p in room.players.values():
        p.needs_full = False          # полный уже «отправлен»
    # бродяги: 200 сущностей, которые действительно двигаются и стукаются
    mobs = []
    for _ in range(N_MOBS):
        while True:
            x = rnd.uniform(2.0, w.grid.w - 2.0)
            y = rnd.uniform(2.0, w.grid.h - 2.0)
            if not physics.circle_hits(w.grid, x, y, 0.3):
                break
        a = rnd.uniform(0, 6.283)
        e = w.spawn(world_mod.K_ENEMY, x, y, r=0.3, hp=30, hp_max=30)
        e.vx = 3.0 * __import__("math").cos(a)
        e.vy = 3.0 * __import__("math").sin(a)
        mobs.append(e)
    w.snapshot_full()                 # исходное состояние «уже у клиентов»
    return room, mobs, conns, rnd


def steer(mobs, rnd):
    """Леса стенда: упёршимся в стену выдать новое направление."""
    for e in mobs:
        if e.vx == 0.0 and e.vy == 0.0:
            a = rnd.uniform(0, 6.283)
            e.vx = 3.0 * __import__("math").cos(a)
            e.vy = 3.0 * __import__("math").sin(a)


def feed_inputs(room, rnd, seq):
    for p in room.players.values():
        p.pending = {"seq": seq, "mv": (rnd.uniform(-1, 1), rnd.uniform(-1, 1)),
                     "aim": (0.0, 0.0), "btn": 0}


def main():
    ticks = int(sys.argv[1]) if len(sys.argv) > 1 else TICKS
    room, mobs, conns, rnd = build()
    n_ent = len(room.world.entities)

    print("=" * 66)
    print("Стенд тика сервера. %d игроков, %d сущностей всего (из них %d бродяг)."
          % (N_PLAYERS, n_ent, len(mobs)))
    print("Карта %dx%d, тик %d Гц, замер %d тиков (прогрев %d)."
          % (room.world.grid.w, room.world.grid.h, world_mod.TICK_HZ, ticks, WARMUP))
    print("=" * 66)

    for i in range(WARMUP):
        steer(mobs, rnd)
        feed_inputs(room, rnd, i)
        room.tick()

    samples = []
    phase_step = []
    phase_snap = []
    clock = time.perf_counter
    for i in range(ticks):
        steer(mobs, rnd)
        feed_inputs(room, rnd, WARMUP + i)
        t0 = clock()
        room.tick()
        samples.append((clock() - t0) * 1000.0)
        # разбивка по фазам — отдельным прогоном, чтобы не портить основной замер
        if i % 10 == 0:
            t1 = clock()
            room.world.step()
            phase_step.append((clock() - t1) * 1000.0)
            t2 = clock()
            room.world.snapshot_delta()
            phase_snap.append((clock() - t2) * 1000.0)

    med = statistics.median(samples)
    p99 = pct(samples, 0.99)
    la = loadavg1()

    print("\n--- 2.2 бюджет тика ---")
    print("  медиана   %7.3f мс   (цель <= %.0f, запас x%.1f)"
          % (med, TARGET_MEDIAN_MS, TARGET_MEDIAN_MS / med if med else 0))
    print("  p99       %7.3f мс   (цель <= %.0f, запас x%.1f)"
          % (p99, TARGET_P99_MS, TARGET_P99_MS / p99 if p99 else 0))
    print("  среднее   %7.3f мс   min %.3f   max %.3f"
          % (statistics.fmean(samples), min(samples), max(samples)))
    print("  доля бюджета тика 33.3 мс: медиана %.1f%%, p99 %.1f%%"
          % (med / 33.33 * 100, p99 / 33.33 * 100))
    print("  из них: шаг мира с физикой ~%.3f мс, сборка дельты ~%.3f мс"
          % (statistics.median(phase_step), statistics.median(phase_snap)))
    print("  вердикт: %s" % ("ПРОХОДИМ" if med <= TARGET_MEDIAN_MS and p99 <= TARGET_P99_MS
                             else "НЕ ПРОХОДИМ"))
    print("  uptime: %s" % uptime_line())
    if la > 2.0:
        print("  ВНИМАНИЕ: loadavg за минуту %.2f > 2 — машина занята кем-то ещё,"
              % la)
        print("            число замера ГРЯЗНОЕ, за чистое его выдавать нельзя.")
    else:
        print("  loadavg за минуту %.2f (<= 2) — замеру можно верить." % la)

    # --- 2.3 трафик ---
    w = room.world
    full = proto.snap_with_ack(w.snapshot_full(remember=False), 12345)
    deltas = []
    for i in range(60):
        steer(mobs, rnd)
        feed_inputs(room, rnd, 9000 + i)
        room.apply_inputs()
        w.step()
        deltas.append(len(proto.snap_with_ack(w.snapshot_delta(), 12345)))
    dmed = statistics.median(deltas)
    hp_max_bytes = sum(len(str(e.hp_max)) + 1 for e in w.entities.values())

    print("\n--- 2.3 трафик на клиента, %d сущностей ---" % n_ent)
    print("  полный снапшот      %6d байт  -> при 30 Гц %.1f КБ/с"
          % (len(full), len(full) * 30 / 1024.0))
    print("  дельта, медиана     %6d байт  -> при 30 Гц %.1f КБ/с"
          % (dmed, dmed * 30 / 1024.0))
    print("  дельта, максимум    %6d байт  -> при 30 Гц %.1f КБ/с"
          % (max(deltas), max(deltas) * 30 / 1024.0))
    print("  бюджет 2.3          %6.0f КБ/с" % TRAFFIC_BUDGET_KBS)
    print("  на сущность: полный %.1f байт, дельта %.1f байт"
          % (len(full) / n_ent, dmed / max(1, len(deltas) and n_ent)))
    print("  поле hp_max стоит   %6d байт на полный снапшот (%.1f%%)"
          % (hp_max_bytes, hp_max_bytes * 100.0 / len(full)))
    # то же самое, но как оно реально уходит в браузер: permessage-deflate
    # включён в server/app.py (GameSocket.get_compression_options)
    import zlib
    raw = full.encode()
    zlib.compress(raw, 1)                       # прогрев, без него меряется холодный вызов
    zt = time.perf_counter()
    for _ in range(50):
        zfull = len(zlib.compress(raw, 1))
    zms = (time.perf_counter() - zt) * 1000.0 / 50.0
    print("  с permessage-deflate (уровень 1, как в app.py):")
    print("    полный снапшот    %6d байт  -> при 30 Гц %.1f КБ/с, сжатие %.3f мс"
          % (zfull, zfull * 30 / 1024.0, zms))
    print("    цена на 6 игроков (контекст сжатия у каждого свой): %.2f мс к тику"
          % (zms * N_PLAYERS))
    print("    вердикт со сжатием: %s"
          % ("ПРОХОДИМ" if zfull * 30 / 1024.0 <= TRAFFIC_BUDGET_KBS else "НЕ ПРОХОДИМ"))

    kbs = dmed * 30 / 1024.0
    print("  вердикт по дельте БЕЗ сжатия: %s" % ("ПРОХОДИМ" if kbs <= TRAFFIC_BUDGET_KBS
                                       else "НЕ ПРОХОДИМ (%.0f КБ/с)" % kbs))
    print("  ВАЖНО: здесь двигаются ВСЕ %d сущностей каждый тик — это худший"
          % n_ent)
    print("  случай, в котором дельта вырождается в полный снапшот.")

    print("\n  байт на игрока за прогон (счётчик комнаты):")
    for p in list(room.players.values())[:2]:
        print("    pid %d: %d сообщений, %d байт" % (p.pid, p.msgs_out, p.bytes_out))
    return 0 if (med <= TARGET_MEDIAN_MS and p99 <= TARGET_P99_MS) else 1


if __name__ == "__main__":
    sys.exit(main())
