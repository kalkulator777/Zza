#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Трёхмерный бэкенд рендера в НАСТОЯЩЕМ браузере (DESIGN.md 7.1, 7.3).

Что здесь проверяется и почему именно так.

1. Игра ИГРАЕТСЯ в 3D. Вкладка входит в комнату, переключается на
   трёхмерный бэкенд кнопкой клиента, бежит — и кадр меняется. Ноль ошибок
   в консоли. «Кадр меняется» проверяется хэшем пикселей настоящей канвы, а
   не тем, что в модели мира поехали координаты: модель может ехать при
   намертво чёрном экране, и ровно это и надо поймать.

2. ТУМАН РАБОТАЕТ И В 3D, всеми тремя состояниями (4.4, 5.2):
   * доля чёрных пикселей в кадре исходной точки ПАДАЕТ после разведки —
     тем же способом, что в tests/client_fog.py: ходок уходит, открывает
     клетки, ВОЗВРАЩАЕТСЯ, и кадр сравнивается сам с собой;
   * враг, поданный на НЕОСВЕЩЁННУЮ клетку, в кадр не попадает ни одним
     пикселем, а такой же враг на освещённой — попадает. Второе не
     украшение: без него первое зеленело бы на клиенте, который не рисует
     врагов вообще.

3. ВЫЗОВОВ ОТРИСОВКИ МАЛО. Цель 7.3 — не больше 40 при любом числе
   сущностей. Меряется при 8 и при 200 врагах: если бы вместо InstancedMesh
   рисовалось по вызову на тело, второе число было бы за две сотни.

4. СТОИМОСТЬ КАДРА, 3D против 2D, НА ОДНОЙ СЦЕНЕ СПИНА К СПИНЕ. Замер идёт
   через benchDrawFlush (7.2): без принудительного слива меряется подача
   команд, а не закраска. ЭТО ДАННЫЕ, А НЕ ПОРОГ. В контейнере SwiftShader,
   то есть софтверный растеризатор; абсолютные миллисекунды отсюда мусор
   (CLAUDE.md), а отношение показывает только порядок величины и тоже не
   является замером 2.1 — тот делается на целевой машине стендом bench/.

5. ПЕРЕКЛЮЧЕНИЕ БЭКЕНДОВ НЕ РОНЯЕТ КЛИЕНТ: туда и обратно несколько раз,
   ноль ошибок, оба бэкенда после этого рисуют непустой кадр.

ВРАГИ ЗДЕСЬ ВЫКЛЮЧЕНЫ (ZZA_ENEMIES=0) — тем же приёмом и по той же
причине, что в tests/client_fog.py: проверка меряет туман и кадр, а не
выживание. Живые враги превращают её в замер того, успел ли одинокий ходок
дойти живым; мёртвый ходок становится духом, дух туман не светит (4.4), и
красной становится совершенно исправная картинка. Врагов, которые нужны
пунктам 2 и 3, проверка подаёт САМА — настоящим сообщением снапшота в
net._onMessage, то есть ровно тем путём, каким их подаёт провод.
"""

import json
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client_common import (BROWSER_ARGS, CHROMIUM, Keys, Server, Tab, check,
                           note, summary, uptime, walk_to)

VIEW_W, VIEW_H = 1920, 1080    # 2.1a: целевое разрешение контракта
TILE_PX = 48                   # 4.1: базовый масштаб рендера
DARK_T = 10                    # «чёрный пиксель»: максимальный канал <= 10

VIS_DARK, VIS_SEEN, VIS_LIT = 0, 1, 2
TILE_WALL, TILE_FLOOR, TILE_STAIRS = 0, 1, 2

# --- ПОРОГ ПАДЕНИЯ ЧЕРНОТЫ: ВЫВОД -----------------------------------------
#
# Камера трёхмерного бэкенда ортографическая под углом 52°, поэтому клетка
# на экране занимает 48 x 48*sin(52°) = 48 x 37.8 = 1814 пикселей, то есть
# 1814/(1920*1080) = 0.0875 п.п. кадра. Порог падения 5 п.п. — это 57
# клеток; проверка не уходит с маршрута, пока в кадре исходной точки не
# откроется ENOUGH_NEW клеток, и запас тут полуторный (не тройной, как в
# 2D: там часть новых клеток гасила виньетка, а здесь виньетки нет вовсе).
DROP_MIN = 0.05
ENOUGH_NEW = 90

# 7.3 называет 40 целью для ЛЮБОГО числа сущностей.
DRAW_CALLS_MAX = 40

# Потолок на дорогу — ПРЕДОХРАНИТЕЛЬ ОТ ЗАВИСАНИЯ, а не порог проверки:
# настоящее условие остановки у walk_to это «путь перестал сокращаться».
# Этаж 64x48, диагональ 80 клеток, бег 5 кл/с (4.2) — 16 с чистого хода;
# запас на порядок на обход стен и тормоза headless-браузера.
WALK_BUDGET = 160.0

BENCH_DRAWS = 40               # вызовов draw() в одной пачке замера
BENCH_REPS = 5


# --- чтение кадра ---------------------------------------------------------

def frame(tab):
    return tab.js("window.__zza.pixels(0,0,0,0,%d)" % DARK_T)


def box(tab, x, y, w, h):
    return tab.js("window.__zza.pixels(%d,%d,%d,%d,%d)"
                  % (x, y, w, h, DARK_T))


def stats(tab):
    return tab.js("window.__zza.stats()")


# --- проекция: спрашиваем у САМОГО рендера, а не списываем в питон --------
#
# У трёхмерного бэкенда экранное место клетки считается его собственной
# формулой (ортокамера под углом). Списать её сюда значило бы завести
# вторую истину о раскладке — и проверка начала бы краснеть от правки
# угла камеры, ничего не говоря о тумане.

SCREEN_CELLS_JS = """(() => {
  const z = window.__zza, L = z.level(), c = z.canvasSize(), r = z.app.render;
  const m = %(margin)f * %(tile)d;
  const out = [];
  for (let y = 0; y < L.h; y++) for (let x = 0; x < L.w; x++) {
    const p = r.worldToScreen(x + 0.5, y + 0.5, %(hgt)f);
    if (p[0] < m || p[0] > c.w - m || p[1] < m || p[1] > c.h - m) continue;
    out.push([y * L.w + x, Math.round(p[0]), Math.round(p[1])]);
  }
  return out;
})()"""


def screen_cells(tab, margin=1.5, hgt=0.0):
    """[(индекс клетки, экранный x, экранный y)] для клеток внутри кадра."""
    return tab.js(SCREEN_CELLS_JS % {"margin": margin, "tile": TILE_PX,
                                     "hgt": hgt})


def enemy_wire(tick, eid, x, y, kind=2):
    """Настоящее сообщение снапшота из 5.2. Поля — 4.3, ровно десять."""
    return json.dumps({"t": "snap", "ack": 0, "tick": tick, "full": False,
                       "e": [[eid, kind, round(x, 3), round(y, 3),
                              0.0, 0.0, 100, 100, 0, 0.0]]})


def wire_many(tab, tick, rows):
    """Пачка врагов одним сообщением — как их и шлёт сервер (5.2)."""
    e = [[eid, 2, round(x, 3), round(y, 3), 0.0, 0.0, 100, 100, 0, 0.0]
         for eid, x, y in rows]
    tab.js("window.__zza.wire(%s)" % json.dumps(
        json.dumps({"t": "snap", "ack": 0, "tick": tick, "full": False,
                    "e": e})))


def bench_flush(tab, reps=BENCH_REPS, n=BENCH_DRAWS):
    """Стоимость одного кадра СО СЛИВОМ, медиана по reps пачкам (7.2)."""
    out = []
    for _ in range(reps):
        out.append(tab.js("window.__zza.benchDrawFlush(%d)" % n))
        time.sleep(0.05)
    return statistics.median(out), out


# --- разбор карты ---------------------------------------------------------

def bfs_dist(level, start, blocked):
    """Расстояния в шагах по проходимым клеткам от start."""
    import collections
    w, h, t = level["w"], level["h"], level["tiles"]
    dist = {start: 0}
    q = collections.deque([start])
    while q:
        c = q.popleft()
        x, y = c
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if not (0 <= nx < w and 0 <= ny < h):
                continue
            if t[ny * w + nx] == TILE_WALL or (nx, ny) in blocked:
                continue
            if (nx, ny) in dist:
                continue
            dist[(nx, ny)] = dist[c] + 1
            q.append((nx, ny))
    return dist


def stairs_cells(level):
    w, t = level["w"], level["tiles"]
    return {(i % w, i // w) for i, v in enumerate(t) if v == TILE_STAIRS}


# --- прогон ---------------------------------------------------------------

def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("нет пакета playwright: pip install playwright "
              "(БЕЗ playwright install — браузер лежит в /opt/pw-browsers)")
        return 2

    print("=== трёхмерный бэкенд рендера (DESIGN.md 7.1, 7.3) ===")
    print("  разрешение вкладки %dx%d, %d px/клетка — как в 4.1"
          % (VIEW_W, VIEW_H, TILE_PX))
    uptime()

    with Server(env={"ZZA_ENEMIES": "0"}) as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM, headless=True,
                                     args=BROWSER_ARGS)
        t = Tab(browser, "A", width=VIEW_W, height=VIEW_H).open(srv.url)
        room = t.create_room("Три-дэ")
        t.ready()
        t.wait_game()
        t.page.wait_for_function(
            "window.__zza.fog() && window.__zza.fog().msgs > 0", timeout=10000)
        time.sleep(0.6)
        print("  комната %s, игра пошла" % room)

        # --- пункт 0: по умолчанию двумерный -------------------------------
        #
        # Отдельной проверкой, потому что это обязательство перед чужими
        # проверками: client_fog, client_combat, client_light, client_interp
        # и client_two_browsers гоняют двумерный бэкенд и про кнопку не
        # знают вовсе.
        check(t.js("window.__zza.getBackend()") == "2d",
              "по умолчанию работает ДВУМЕРНЫЙ бэкенд",
              "getBackend() = %r, рендер %r"
              % (t.js("window.__zza.getBackend()"),
                 t.js("window.__zza.backendName()")))

        # --- пункт 1: игра играется в 3D -----------------------------------
        print()
        t0 = time.time()
        t.js("window.__zza.setBackend('3d')")
        t.page.wait_for_function("window.__zza.getBackend()==='3d'", timeout=15000)
        time.sleep(0.5)
        load_s = time.time() - t0
        check(t.js("window.__zza.backendName()") == "three.js",
              "бэкенд переключился на three.js",
              "поднялся за %.2f с (вместе с загрузкой vendor/three.*)" % load_s)
        check(t.js("window.__zza.screen()") == "game" and
              t.js("window.__zza.self() !== null"),
              "клиент остался в игре и видит свою сущность")

        f0 = frame(t)
        check(f0["bright"] > f0["n"] * 0.02,
              "кадр не пустой: карта и свет нарисованы",
              "светлее порога черноты %.1f%% пикселей, самый яркий канал %d"
              % (100.0 * f0["bright"] / f0["n"], f0["maxCh"]))

        # Бежим — и кадр обязан поменяться. Хэш куска канвы, а не координаты
        # в модели: модель едет и при чёрном экране.
        h_before = f0["hash"]
        me0 = t.self_pos()
        t.hold(["KeyD"], 0.45)
        time.sleep(0.35)
        me1 = t.self_pos()
        f1 = frame(t)
        moved = math.hypot(me1["x"] - me0["x"], me1["y"] - me0["y"])
        if moved < 0.3:                      # упёрся в стену — пробуем назад
            t.hold(["KeyA"], 0.45)
            time.sleep(0.35)
            me1 = t.self_pos()
            f1 = frame(t)
            moved = math.hypot(me1["x"] - me0["x"], me1["y"] - me0["y"])
        check(moved > 0.3 and f1["hash"] != h_before,
              "персонаж бежит, и кадр в трёхмерном бэкенде меняется",
              "прошёл %.2f клетки, хэш кадра %08x -> %08x"
              % (moved, h_before, f1["hash"]))

        st = stats(t)
        note("кадр 3D", "сущностей %d, drawCalls %d, треугольников %d"
             % (st["ents"], st["drawCalls"], st.get("tris", -1)))

        # --- пункт 2а: туман — чернота падает после разведки ----------------
        print()
        level = t.js("window.__zza.level()")
        stairs = stairs_cells(level)
        keys = Keys(t)

        # Кадр исходной точки: запоминаем клетки, которые в него попадают,
        # и туман на них. Всё, что откроется ВНЕ этого кадра, на замер не
        # влияет вовсе — ровно тот же принцип, что в client_fog.new_in_view.
        cells = screen_cells(t, margin=0.0)
        idx = [c[0] for c in cells]
        fog0 = t.js("window.__zza.fog()")
        t.js("window.__t3 = {cells: %s, fog0: %s}"
             % (json.dumps(idx),
                json.dumps([fog0["cells"][i] for i in idx])))
        dark0 = frame(t)
        home = (int(t.self_pos()["x"]), int(t.self_pos()["y"]))
        note("кадр исходной точки",
             "клеток в кадре %d, чёрных пикселей %.1f%%"
             % (len(idx), 100.0 * dark0["frac"]))

        opened_js = """(() => {
          const f = window.__zza.fog().cells, s = window.__t3;
          let n = 0;
          for (let i = 0; i < s.cells.length; i++)
            if (s.fog0[i] === 0 && f[s.cells[i]] !== 0) n++;
          return n;
        })()"""

        state = {"n": 0, "t": 0.0}

        def probe():
            now = time.time()
            if now - state["t"] < 0.4:
                return
            state["t"] = now
            state["n"] = t.js(opened_js)

        def enough():
            return state["n"] >= ENOUGH_NEW

        # Цель — самая дальняя клетка в 18 шагах, лестницу обходим: наступить
        # на неё значит сменить этаж посреди замера (туман обнулится, карта
        # станет другой, кадр исходной точки исчезнет вместе с этажом).
        dist = bfs_dist(level, home, stairs)
        far = max(dist.items(), key=lambda kv: min(kv[1], 18))
        target = far[0]
        ok_out, d_out, s_out, _n = walk_to(t, level, target, keys, WALK_BUDGET,
                                           probe=probe, enough=enough,
                                           avoid=stairs)
        probe()
        note("разведка", "ушли к (%d,%d), %.1f с, открылось в кадре %d клеток"
             % (target[0], target[1], s_out, state["n"]))
        ok_back, d_back, s_back, _n2 = walk_to(t, level, home, keys,
                                               WALK_BUDGET, avoid=stairs)
        keys.release()
        time.sleep(0.6)
        dark1 = frame(t)
        note("возврат", "вернулись на (%d,%d), промах %.2f клетки, %.1f с"
             % (home[0], home[1], d_back, s_back))
        drop = dark0["frac"] - dark1["frac"]
        check(state["n"] >= ENOUGH_NEW,
              "разведано достаточно клеток В КАДРЕ исходной точки",
              "открылось %d при нужных %d (порог падения выведен из этого "
              "числа: %d клеток * 0.0875 п.п. = %.1f п.п.)"
              % (state["n"], ENOUGH_NEW, ENOUGH_NEW, ENOUGH_NEW * 0.0875))
        check(drop >= DROP_MIN,
              "доля чёрных пикселей упала после разведки",
              "%.1f%% -> %.1f%%, падение %.1f п.п. при пороге %.1f"
              % (100.0 * dark0["frac"], 100.0 * dark1["frac"],
                 100.0 * drop, 100.0 * DROP_MIN))

        # Три состояния тумана обязаны РАЗЛИЧАТЬСЯ на глаз. 4.1 меряет это
        # отношением яркости освещённого пола к памяти по Rec.709 и
        # называет 2.20 порогом (наименьшая разница, которую глаз читает
        # как другой свет, — вдвое, одна ступень экспозиции).
        fogn = t.js("window.__zza.fog()")
        cells2 = screen_cells(t, margin=1.5)
        lit_b, seen_b, dark_b = [], [], []
        tiles = level["tiles"]
        for i, sx, sy in cells2:
            if tiles[i] != TILE_FLOOR:
                continue
            v = fogn["cells"][i]
            b = [sx - 8, sy - 6, 16, 12]
            if v == VIS_LIT:
                lit_b.append(b)
            elif v == VIS_SEEN:
                seen_b.append(b)
            else:
                dark_b.append(b)
        if lit_b and seen_b:
            res = t.js("window.__zza.boxesMean(%s,%d)"
                       % (json.dumps(lit_b[:200] + seen_b[:200] + dark_b[:200]),
                          DARK_T))
            nl, ns = len(lit_b[:200]), len(seen_b[:200])
            lum_lit = sum(r["lum"] for r in res[:nl]) / nl
            lum_seen = sum(r["lum"] for r in res[nl:nl + ns]) / ns
            ratio = lum_lit / max(1e-6, lum_seen)
            dk = res[nl + ns:]
            dark_max = max((r["maxCh"] for r in dk), default=0)
            check(ratio >= 2.20,
                  "свет факела: освещённый пол ярче памяти (Rec.709)",
                  "%.2f против памяти при пороге 2.20 "
                  "(яркость %.1f и %.1f, клеток %d и %d)"
                  % (ratio, lum_lit, lum_seen, nl, ns))
            check(dark_max <= DARK_T,
                  "неразведанное не рисуется вовсе: абсолютная чернота",
                  "самый яркий канал на %d неразведанных клетках = %d "
                  "при пороге черноты %d" % (len(dk), dark_max, DARK_T))
        else:
            check(False, "нашлись клетки «видно сейчас» и «память» в кадре",
                  "освещённых %d, памяти %d" % (len(lit_b), len(seen_b)))

        # --- пункт 2б: враг в темноте в кадр не попадает (5.2) -------------
        print()
        cells3 = screen_cells(t, margin=2.0, hgt=0.30)
        me = t.self_pos()
        dark_pick = lit_pick = None
        for i, sx, sy in cells3:
            tx, ty = i % level["w"], i // level["w"]
            if tiles[i] == TILE_WALL:
                continue
            if math.hypot(tx + 0.5 - me["x"], ty + 0.5 - me["y"]) < 2.0:
                continue
            v = fogn["cells"][i]
            if v != VIS_LIT and dark_pick is None:
                # Клетка целиком в темноте: соседи тоже не освещены, иначе
                # коробка захватит кромку света и «не изменилась» станет
                # неправдой не из-за врага.
                if all(fogn["cells"][(ty + dy) * level["w"] + tx + dx] != VIS_LIT
                       for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                       if 0 <= tx + dx < level["w"] and 0 <= ty + dy < level["h"]):
                    dark_pick = (tx, ty, sx, sy)
            if v == VIS_LIT and tiles[i] == TILE_FLOOR and lit_pick is None:
                lit_pick = (tx, ty, sx, sy)
            if dark_pick and lit_pick:
                break

        if dark_pick is None or lit_pick is None:
            check(False, "нашлись неосвещённая и освещённая клетки в кадре",
                  "темнота %s, свет %s" % (dark_pick, lit_pick))
        else:
            half = 50
            tick = t.js("window.__zza.tick()")
            tx, ty, sx, sy = dark_pick
            before = box(t, sx - half, sy - half, half * 2, half * 2)
            t.js("window.__zza.wire(%s)" % json.dumps(
                enemy_wire(tick, 900001, tx + 0.5, ty + 0.5)))
            time.sleep(0.4)
            after = box(t, sx - half, sy - half, half * 2, half * 2)
            sth = stats(t)
            check(after["hash"] == before["hash"] and
                  after["maxR"] == before["maxR"],
                  "враг на НЕОСВЕЩЁННОЙ клетке в кадр не попал",
                  "клетка (%d,%d), коробка %dx%d не изменилась ни на пиксель: "
                  "хэш %08x, самый яркий красный %d; скрыто туманом %d"
                  % (tx, ty, half * 2, half * 2, after["hash"],
                     after["maxR"], sth["hidden"]))
            check(sth["hidden"] >= 1,
                  "клиент сам сообщает, что спрятал сущность",
                  "stats().hidden = %d" % sth["hidden"])

            tx2, ty2, sx2, sy2 = lit_pick
            before2 = box(t, sx2 - half, sy2 - half, half * 2, half * 2)
            t.js("window.__zza.wire(%s)" % json.dumps(
                enemy_wire(tick, 900002, tx2 + 0.5, ty2 + 0.5)))
            time.sleep(0.4)
            after2 = box(t, sx2 - half, sy2 - half, half * 2, half * 2)
            check(after2["maxR"] >= before2["maxR"] + 60,
                  "такой же враг на ОСВЕЩЁННОЙ клетке в кадр попал",
                  "клетка (%d,%d): самый яркий красный %d -> %d "
                  "(рубака #d16a6a это r=209)"
                  % (tx2, ty2, before2["maxR"], after2["maxR"]))

        # --- пункт 3: вызовы отрисовки (7.3) --------------------------------
        print()
        lit_cells = [(i % level["w"], i // level["w"]) for i, _sx, _sy in cells3
                     if fogn["cells"][i] == VIS_LIT and tiles[i] != TILE_WALL]
        if not lit_cells:
            lit_cells = [(int(me["x"]), int(me["y"]))]
        calls = {}
        for n in (8, 200):
            tick = t.js("window.__zza.tick()")
            rows = []
            for k in range(n):
                cx, cy = lit_cells[k % len(lit_cells)]
                rows.append((910000 + k, cx + 0.5 + (k // len(lit_cells)) * 0.07,
                             cy + 0.5))
            wire_many(t, tick, rows)
            time.sleep(0.35)
            t.js("window.__zza.benchDraw(2)")
            s = stats(t)
            calls[n] = s
            check(s["drawCalls"] <= DRAW_CALLS_MAX,
                  "вызовов отрисовки не больше %d при %d врагах"
                  % (DRAW_CALLS_MAX, n),
                  "drawCalls %d, сущностей в мире %d, нарисовано %d, "
                  "треугольников %d"
                  % (s["drawCalls"], s["ents"], s["ents"] - s["hidden"],
                     s.get("tris", -1)))
        note("InstancedMesh работает",
             "8 врагов -> %d вызовов, 200 врагов -> %d вызовов "
             "(по вызову на тело было бы за две сотни)"
             % (calls[8]["drawCalls"], calls[200]["drawCalls"]))

        # --- пункт 4: стоимость кадра, 3D против 2D спина к спине -----------
        print()
        ms3, s3 = bench_flush(t)
        t.js("window.__zza.setBackend('2d')")
        t.page.wait_for_function("window.__zza.getBackend()==='2d'", timeout=10000)
        time.sleep(0.4)
        ms2, s2 = bench_flush(t)
        t.js("window.__zza.setBackend('3d')")
        t.page.wait_for_function("window.__zza.getBackend()==='3d'", timeout=10000)
        time.sleep(0.4)
        ms3b, s3b = bench_flush(t)
        note("стоимость кадра СО СЛИВОМ, одна сцена (%d сущностей)"
             % calls[200]["ents"],
             "3D %.3f мс, 2D %.3f мс, 3D снова %.3f мс; отношение 3D/2D = %.2f"
             % (ms3, ms2, ms3b, (ms3 + ms3b) / 2 / max(1e-9, ms2)))
        note("ЭТО НЕ ЗАМЕР 2.1",
             "здесь SwiftShader (софтверный растеризатор), абсолютные "
             "миллисекунды отсюда мусор, а отношение показывает порядок "
             "величины на ПРОЦЕССОРЕ, а не на видеокарте. Замер 2.1 — "
             "стендом bench/ на целевой машине")
        uptime()

        # --- пункт 5: переключение туда-обратно не роняет клиент ------------
        print()
        seq_ok = True
        detail = []
        for i in range(3):
            for name in ("2d", "3d"):
                t.js("window.__zza.setBackend('%s')" % name)
                t.page.wait_for_function(
                    "window.__zza.getBackend()==='%s'" % name, timeout=10000)
                time.sleep(0.25)
                f = frame(t)
                if t.js("window.__zza.screen()") != "game" or \
                        t.js("window.__zza.self()") is None or \
                        f["bright"] <= f["n"] * 0.01:
                    seq_ok = False
                detail.append("%s:%.0f%%" % (name, 100.0 * f["bright"] / f["n"]))
        check(seq_ok, "переключение бэкендов туда и обратно (6 раз) — клиент жив",
              "светлых пикселей после каждого: " + " ".join(detail))

        # Мышь после перевешивания ввода обязана попадать туда же: прицел
        # считает бэкенд, а слушает канва — перепутать их значит получить
        # игру, в которой клавиши работают, а мышь нет.
        t.js("window.__zza.setBackend('3d')")
        t.page.wait_for_function("window.__zza.getBackend()==='3d'", timeout=10000)
        time.sleep(0.3)
        aim = t.js("(() => { const r = window.__zza.app.render;"
                   " const c = window.__zza.canvasSize();"
                   " const w = r.screenToWorld(c.w/2, c.h/2);"
                   " const s = r.worldToScreen(w[0], w[1], 0.30);"
                   " return [w[0], w[1], s[0], s[1], c.w/2, c.h/2]; })()")
        check(abs(aim[2] - aim[4]) < 1.0 and abs(aim[3] - aim[5]) < 1.0,
              "прицел обратим: экран -> мир -> экран возвращается в ту же точку",
              "центр кадра (%.0f,%.0f) -> клетка (%.2f,%.2f) -> (%.1f,%.1f)"
              % (aim[4], aim[5], aim[0], aim[1], aim[2], aim[3]))

        # --- ошибки в консоли -----------------------------------------------
        print()
        errs = t.errors()
        check(not errs, "ноль ошибок в консоли за весь прогон",
              "; ".join(errs[:4]) if errs else "консоль чистая")

        t.close()
        browser.close()

    return summary()


if __name__ == "__main__":
    sys.exit(main())
