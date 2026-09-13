#!/usr/bin/env python3
"""Проверка трасс. Запуск: python3 tools/validate_track.py [файл ...]

Без аргументов проверяет всё в shared/tracks. Ловит то, что иначе всплывёт
только в игре: самопересечение дороги, слишком узкие или широкие места,
боксы и декор не на своём месте, слишком короткий или длинный круг.
"""

import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.track import Track  # noqa: E402

MIN_LAP = 4000.0
MAX_LAP = 60000.0
MIN_HALFWIDTH = 45.0
MAX_HALFWIDTH = 190.0
MAX_TURN_DEG = 58.0


def check(path):
    problems = []
    notes = []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    t = Track(data)

    if not (MIN_LAP <= t.length <= MAX_LAP):
        problems.append(f"длина круга {t.length:.0f} px вне разумного "
                        f"({MIN_LAP:.0f}..{MAX_LAP:.0f})")

    mn, mx = min(t.hw), max(t.hw)
    if mn < MIN_HALFWIDTH:
        problems.append(f"самое узкое место {mn*2:.0f} px — шесть машин не разъедутся")
    if mx > MAX_HALFWIDTH:
        problems.append(f"самое широкое место {mx*2:.0f} px — дорога превращается в поле")

    # Резкость поворотов: на ломаной с шагом 28 px угол между соседними
    # сегментами больше ~58° означает, что машина туда просто не впишется.
    worst = 0.0
    for i in range(t.nseg):
        a, b, c = i, (i + 1) % t.n, (i + 2) % t.n
        ax, ay = t.px[b] - t.px[a], t.py[b] - t.py[a]
        bx, by = t.px[c] - t.px[b], t.py[c] - t.py[b]
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        if la < 1e-6 or lb < 1e-6:
            continue
        cosv = max(-1.0, min(1.0, (ax * bx + ay * by) / (la * lb)))
        ang = math.degrees(math.acos(cosv))
        worst = max(worst, ang)
    if worst > MAX_TURN_DEG:
        problems.append(f"есть излом {worst:.0f}° — поворот нереально пройти, "
                        f"разнесите опорные точки")

    # Самопересечение: дорога не должна накладываться сама на себя, иначе
    # чекпоинты и подсчёт кругов начнут врать.
    hits = 0
    step = 2
    for i in range(0, t.nseg, step):
        for j in range(i + 6, t.nseg, step):
            need = (t.hw[i] + t.hw[j]) * 0.85
            # Точки, близкие ВДОЛЬ трассы, обязаны быть близки и в пространстве —
            # это просто соседние участки одной дороги, а не пересечение.
            # Раньше здесь стоял порог в шагах ломаной, и он незаметно
            # ограничивал ширину дороги примерно 196 px.
            arc = abs(t.cum[i] - t.cum[j])
            if t.closed:
                arc = min(arc, t.length - arc)
            if arc < need * 1.7:
                continue
            d = math.hypot(t.px[i] - t.px[j], t.py[i] - t.py[j])
            if d < need:
                hits += 1
    if hits:
        problems.append(f"дорога пересекает сама себя примерно в {hits} местах")

    # Боксы должны лежать на дороге
    off = 0
    for b in t.boxes:
        seg = t.query_global(b["x"], b["y"])
        q = t.query(b["x"], b["y"], seg)
        if abs(q[2]) > q[3] - 20.0:
            off += 1
    if off:
        problems.append(f"{off} боксов с бонусами лежат не на дороге")

    # Декор не должен стоять посреди трассы
    ondeck = 0
    for d in t.decor:
        seg = t.query_global(d["x"], d["y"])
        q = t.query(d["x"], d["y"], seg)
        if abs(q[2]) < q[3] + 12.0:
            ondeck += 1
    if ondeck:
        problems.append(f"{ondeck} объектов декора стоят на дороге")

    lap_s = t.length / 330.0
    notes.append(f"круг {t.length:.0f} px ≈ {lap_s:.0f} с, "
                 f"{t.laps} круга ≈ {t.laps*lap_s/60:.1f} мин")
    notes.append(f"ширина {min(t.hw)*2:.0f}..{max(t.hw)*2:.0f} px, "
                 f"резкость до {worst:.0f}°")
    notes.append(f"боксов {len(t.boxes)}, декора {len(t.decor)}, "
                 f"чекпоинтов {len(t.checkpoints)}")
    return t, problems, notes


def main(argv):
    paths = argv[1:]
    if not paths:
        d = os.path.join(ROOT, "shared", "tracks")
        paths = [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".json")]

    bad = 0
    for p in paths:
        name = os.path.basename(p)
        try:
            t, problems, notes = check(p)
        except Exception as exc:  # noqa: BLE001
            print(f"[СЛОМАНА] {name}: {exc}")
            bad += 1
            continue
        mark = "OK " if not problems else "ПЛОХО"
        print(f"[{mark}] {name} — {t.name}")
        for n in notes:
            print(f"         {n}")
        for pr in problems:
            print(f"    !    {pr}")
            bad += 1
    print()
    print("Все трассы в порядке." if not bad else f"Проблем: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
