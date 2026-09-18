#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Свет факела в кадре клиента (DESIGN.md 4.1, 7.2).

Отвечает на один вопрос: ВИДНО ЛИ, что игрок несёт огонь. До этой работы в
кадре не было ни одного места, которое ярче оттого, что там игрок: виньетка
только ВЫЧИТАЕТ к краям, а её центр полностью прозрачен. Замер дирижёра на
живом кадре 1920x1080: освещённый пол 58.8, память группы 33.7,
неразведанное 2.0 — то есть свет отличался от памяти в 1.7 раза.

ЧТО ПРОВЕРЯЕТСЯ

  1. Освещённый пол ЗАМЕТНО ярче пола в памяти группы.
  2. Та же проверка КРАСНЕЕТ с выключённым светом (setTorch(false)) — то
     есть на клиенте, каким он был до этой работы. Замер идёт спина к спине
     на ОДНОЙ сцене: это же и есть честное «до и после».
  3. Туман по-прежнему читается: неразведанное осталось чёрным (порог тот
     же, что в tests/client_fog.py), а память — заметно светлее черноты.
     Второе — не украшение: выиграть отношение из пункта 1, ПРИТУШИВ память,
     так же легко, как осветив свет, и это была бы порча тумана.
  4. Стоимость кадра не выросла: benchDrawFlush (с принудительным сливом)
     со светом и без, спина к спине, на одной сцене.
  5. Стрелок отличим от рубаки по пикселям.
  6. Незнакомый kind клиент не роняет.
  7. Ноль ошибок в консоли.

ОТКУДА ПОРОГИ (выведены из величин, не подобраны под прогон)

  * ЧЕМ МЕРИТЬ ЯРКОСТЬ. Старые 58.8 / 33.7 / 2.0 сняты средним по
    МАКСИМАЛЬНОМУ каналу. Это не яркость, а детектор черноты: у холодного
    пола #23293a максимальный канал — синий, а огонь тёплый, и синего в нём
    меньше всего. Поэтому главным числом здесь стоит Rec.709
    (0.2126 R + 0.7152 G + 0.0722 B) — стандартная яркость; среднее по
    максимальному каналу печатается рядом, чтобы прогон был сравним со
    старым замером.
  * ОТНОШЕНИЕ «ОСВЕЩЕНО / ПАМЯТЬ» >= 2.2. Наименьшая разница освещённости,
    которую глаз читает как ДРУГОЙ свет, а не как другую поверхность, —
    вдвое (одна ступень экспозиции). Замеренные 1.7 лежат НИЖЕ ступени, и
    ровно поэтому подземелье читалось одним ровным полумраком. Порог = одна
    ступень плюс 10% запаса, чтобы проверка охраняла явление, а не свою
    границу: 2.2.
  * ПАМЯТЬ НЕ ТЕМНЕЕ 20 по максимальному каналу. Порог черноты в этом
    проекте — 10 (tests/client_fog.py: неразведанная клетка красится
    непрозрачным #000). Память обязана оставаться вдвое выше линии черноты,
    иначе «помню стену» превращается в «темнота».
  * НЕРАЗВЕДАННОЕ <= 10 по максимальному каналу — тот же порог черноты и
    ровно то же его обоснование.
  * СТОИМОСТЬ КАДРА <= 1.25. Свет не добавляет в кадр ни одного вызова и ни
    одного пикселя: он живёт В МАСКЕ ТУМАНА (7.2), которая и так блитится
    одним drawImage. Честное ожидание — 1.00, а 1.25 — запас на разброс
    медианы по пачкам в контейнере с софтверным рендером (сам разброс
    печатается рядом). Абсолютные миллисекунды отсюда числом целевой машины
    НЕ являются.
  * СИЛУЭТЫ РАЗЛИЧАЮТСЯ ВТРОЕ ПО ЗАПОЛНЕНИЮ >= 1.5. Рубака — круг площадью
    pi*r^2 = 3.14 r^2, стрелок — наконечник площадью 1.21 r^2 (render2d.js,
    ARROW_*), то есть по построению 2.6 раза. Обводка наконечника и
    сглаживание съедают часть; порог 1.5 оставляет запас 1.7 раза от
    построения и 1.3 от ожидаемого замера.

ВРАГОВ ЗДЕСЬ НЕТ: стенд поднимает play.py с ZZA_ENEMIES=0 (раздел 10).
Проверка меряет свет в кадре стоящего клиента, а живой враг убивает
одинокого ходока, и дух перестаёт светить туман (4.4) — мерить станет
нечего. Рубака и стрелок для пункта 5 подаются НАСТОЯЩИМ сообщением
снапшота в net._onMessage, тем же входом, куда приходит провод.

Запуск:
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/client_light.py
Выход: 0 — зелено, 1 — красно.
"""

import json
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client_common import (BROWSER_ARGS, CHROMIUM, Keys, Server, Tab, check,
                           bfs_path, note, summary, uptime, walk_to)

VIEW_W, VIEW_H = 1920, 1080    # 2.1a: целевое разрешение контракта
TILE_PX = 48                   # 4.1: базовый масштаб

DARK_T = 10                    # порог черноты, тот же, что в client_fog.py
LIGHT_RATIO_MIN = 2.2          # «освещено / память» по Rec.709
SEEN_MIN = 20                  # память не имеет права стать темнотой
MS_RATIO_MAX = 1.25            # стоимость кадра «со светом / без»
SHAPE_RATIO_MIN = 1.5          # круг рубаки против наконечника стрелка
BODY_T = 150                   # «пиксель тела»: тело врага даёт 209,
#                                самый светлый освещённый пол — 79.5
BODY_NEAR = 7.0                # не дальше этого от игрока: радиус обзора 10
#                                (4.1), и клетка на самом пределе может
#                                погаснуть между чтением тумана и кадром

BENCH_DRAWS = 120              # кадров в пачке замера со сливом
BENCH_REPS = 5

WALK_BUDGET = 160.0            # ПРЕДОХРАНИТЕЛЬ от зависания, не порог
# Разведка идёт ДВУМЯ ногами, и это не украшение. Память группы — это
# клетки, которые открыты, но сейчас не видны, то есть дальше радиуса обзора
# (10 клеток, 4.1) или за стеной. Одна нога в 13 клеток оставляет в кадре
# 13 таких клеток пола — мерить среднее по тринадцати клеткам можно, но
# сцена получается вырожденной. Нога в 18 клеток и возврат на 8 оставляют
# позади полосу открытого коридора за пределом обзора, и она влезает в кадр.
WALK_LEG = 18                  # клеток в первой ноге разведки
WALK_BACK = 8                  # на сколько клеток вернуться во второй
# СКОЛЬКО КЛЕТОК ХВАТАЕТ ДЛЯ ЧЕСТНОГО СРЕДНЕГО — ЭТО ВОПРОС ПРО ПИКСЕЛИ, А НЕ
# ПРО КЛЕТКИ. Раньше здесь стояло 20 при масштабе 48 px/клетку: 20 * 48^2 =
# 46 000 пикселей, 2.2% кадра 1920x1080. Базовый масштаб стал 64 px (4.1),
# клетка выросла в 1.78 раза по площади, и те же 2% кадра — это уже 10
# клеток. Полоса памяти в кадре тоже сузилась и по построению: она лежит
# между радиусом обзора (10 клеток) и кромкой кадра (15 клеток при 64 px
# против 20 при 48), то есть по коридору шириной в клетку её всего пять.
# Замерено на живом кадре: освещено 79, память 14, не открыто 39.
MIN_CELLS = 10                 # клеток пола в каждом состоянии тумана

VIS_DARK, VIS_SEEN, VIS_LIT = 0, 1, 2
TILE_WALL, TILE_FLOOR, TILE_STAIRS = 0, 1, 2
K_PLAYER, K_ENEMY, K_SHOT = 1, 2, 4
K_ENEMY_RANGED = 5             # state.js: пятое значение kind
K_UNKNOWN = 47                 # такого вида нет ни у кого


# --- мелкая обвязка -------------------------------------------------------

def cam(tab):
    return tab.js("({x: window.__zza.app.render.camX, "
                  "y: window.__zza.app.render.camY, "
                  "t: window.__zza.app.render.tilePx})")


def fog_of(tab):
    return tab.js("window.__zza.fog()")


def boxes_mean(tab, boxes, thresh=DARK_T):
    """Пачка коробок за один заход в браузер (ui.js: boxesMean)."""
    if not boxes:
        return []
    return tab.js("window.__zza.boxesMean(%s,%d)"
                  % (json.dumps([[int(round(v)) for v in b] for b in boxes]),
                     thresh))


def snap_wire(tick, ents):
    """Снапшот 5.2. Сущность — массив из десяти полей ровно в порядке 4.3."""
    return json.dumps({"t": "snap", "ack": 0, "tick": tick, "full": False,
                       "e": ents})


def ent_row(eid, kind, x, y, facing=0.0, hp=40, hp_max=40, flags=0):
    return [eid, kind, round(x, 3), round(y, 3), 0.0, 0.0,
            hp, hp_max, flags, round(facing, 2)]


def flush_ms(tab, n=BENCH_DRAWS, reps=BENCH_REPS):
    """Стоимость кадра СО СЛИВОМ. Без слива меряется подача команд, а не
    закраска (7.2): канва отложенная, и кадр 1920x1080 «стоит» 0.01 мс."""
    out = []
    for _ in range(reps):
        out.append(tab.js("window.__zza.benchDrawFlush(%d)" % n))
        time.sleep(0.05)
    return statistics.median(out), out


# --- выбор клеток пола ----------------------------------------------------

def floor_cells(level, fog, c, size, avoid_pts, margin=2.0, keep=1.8):
    """Клетки пола, годные для замера яркости, разложенные по состояниям.

    Годная клетка: это пол; под ней НЕ стена (иначе в коробку лезет
    вертикальный выступ соседней стены, render2d.js WALL_RISE); она целиком
    на экране с запасом; и она не ближе keep клеток к любой сущности —
    тело, тень и полоска здоровья это не пол.
    """
    w, h, tiles = level["w"], level["h"], level["tiles"]
    cells = fog["cells"]
    out = {VIS_DARK: [], VIS_SEEN: [], VIS_LIT: []}
    m = margin * c["t"]
    for ty in range(h - 1):
        for tx in range(w):
            i = ty * w + tx
            if tiles[i] != TILE_FLOOR:
                continue
            if tiles[(ty + 1) * w + tx] == TILE_WALL:
                continue
            sx = (tx + 0.5 - c["x"]) * c["t"]
            sy = (ty + 0.5 - c["y"]) * c["t"]
            if sx < m or sy < m or sx > size["w"] - m or sy > size["h"] - m:
                continue
            bad = False
            for px, py in avoid_pts:
                if math.hypot(tx + 0.5 - px, ty + 0.5 - py) < keep:
                    bad = True
                    break
            if bad:
                continue
            v = cells[i]
            if v in out:
                out[v].append((tx, ty, sx, sy))
    return out


def measure_classes(tab, cells, c, size):
    """Средняя яркость пола по состояниям тумана.

    Коробка — центральные 0.5 клетки: края клетки трогать нельзя, там
    сетка пола (COL.grid) и мягкая кайма тумана от растяжения маски.
    """
    q = c["t"] * 0.25
    flat, index = [], []
    for v in (VIS_LIT, VIS_SEEN, VIS_DARK):
        for (tx, ty, sx, sy) in cells[v]:
            flat.append((sx - q, sy - q, q * 2, q * 2))
            index.append((v, sx, sy))
    got = boxes_mean(tab, flat)
    acc = {}
    for (v, sx, sy), r in zip(index, got):
        a = acc.setdefault(v, {"mean": [], "lum": [], "rad": []})
        a["mean"].append(r["mean"])
        a["lum"].append(r["lum"])
        a["rad"].append(math.hypot(sx - size["w"] / 2, sy - size["h"] / 2)
                        / c["t"])
    # Ключи ВСЕГДА все три, даже если класс пуст. Пустой класс — это
    # красная проверка композиции кадра ниже, а не KeyError посреди замера:
    # упавшая проверка не говорит ничего (правило 10 про ложные красные —
    # тот же довод, что и про падения).
    out = {}
    for v in (VIS_LIT, VIS_SEEN, VIS_DARK):
        a = acc.get(v)
        if not a:
            out[v] = {"n": 0, "mean": 0.0, "lum": 0.0, "rad": 0.0}
            continue
        out[v] = {"n": len(a["mean"]),
                  "mean": sum(a["mean"]) / len(a["mean"]),
                  "lum": sum(a["lum"]) / len(a["lum"]),
                  "rad": sum(a["rad"]) / len(a["rad"])}
    return out


def show(title, st):
    def one(v, name):
        if v not in st:
            return "%s нет" % name
        d = st[v]
        return ("%s %d кл.: яркость %.1f, максканал %.1f, средний радиус "
                "%.1f кл." % (name, d["n"], d["lum"], d["mean"], d["rad"]))
    note(title, "; ".join((one(VIS_LIT, "освещено"), one(VIS_SEEN, "память"),
                           one(VIS_DARK, "не открыто"))))


# --- прогон ---------------------------------------------------------------

def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("нет пакета playwright: pip install playwright "
              "(БЕЗ playwright install — браузер лежит в /opt/pw-browsers)")
        return 2

    print("=== свет факела в кадре клиента (DESIGN.md 4.1, 7.2) ===")
    print("  разрешение вкладки %dx%d, %d px/клетка — как в 4.1"
          % (VIEW_W, VIEW_H, TILE_PX))
    uptime()

    with Server(env={"ZZA_ENEMIES": "0"}) as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM, headless=True,
                                     args=BROWSER_ARGS)
        t = Tab(browser, "A", width=VIEW_W, height=VIEW_H).open(srv.url)
        room = t.create_room("Свет")
        t.ready()
        t.wait_game()
        t.page.wait_for_function(
            "window.__zza.fog() && window.__zza.fog().msgs > 0", timeout=10000)
        time.sleep(0.6)
        print("  комната %s, игра пошла" % room)

        level = t.js("window.__zza.level()")
        size = t.js("window.__zza.canvasSize()")
        floor0 = level["floor"]
        stairs = next(((i % level["w"], i // level["w"])
                       for i, v in enumerate(level["tiles"])
                       if v == TILE_STAIRS), None)
        note("карта", "%dx%d, этаж %d, лестница %s"
             % (level["w"], level["h"], floor0, stairs))

        # --- разведка: без памяти группы мерить нечего --------------------
        # Сразу после входа всё, что открыто, — оно же и освещено. Память
        # появляется только после того, как ходок ушёл с места. Лестницу
        # обходим стороной: наступить на неё — сменить этаж посреди замера
        # (4.4 обнуляет туман), ровно та мина, что вылечена в client_fog.py.
        keys = Keys(t)
        avoid = {stairs} if stairs else set()
        me = t.self_pos()
        src = (int(me["x"]), int(me["y"]))

        def pick(frm, want):
            """Проходимая клетка примерно в want клетках от frm."""
            w, h, tiles = level["w"], level["h"], level["tiles"]
            best, out = None, None
            for ty in range(h):
                for tx in range(w):
                    if tiles[ty * w + tx] != TILE_FLOOR:
                        continue
                    if stairs and math.hypot(tx - stairs[0],
                                             ty - stairs[1]) < 4:
                        continue
                    d = math.hypot(tx - frm[0], ty - frm[1])
                    if best is not None and abs(d - want) >= abs(best - want):
                        continue
                    if bfs_path(level, frm, (tx, ty), avoid):
                        best, out = d, (tx, ty)
            return out

        # ЗАМЕР ДЕЛАЕТСЯ ТАМ, ГДЕ В КАДРЕ ЕСТЬ ВСЕ ТРИ СОСТОЯНИЯ, а не там,
        # где кончился маршрут. Раздел 10: проверка обязана ждать ТО ЧИСЛО,
        # которое она меряет. Ноги остались прежние, но после каждой
        # считается состав кадра, и если он уже годится — дальше не идём.
        #
        # ПОЧЕМУ ЭТО ПОНАДОБИЛОСЬ. Кадр при базовых 64 px (4.1) — это
        # 30x16.9 клетки против 40x22.5 при 48, а радиус обзора прежний,
        # 10 клеток. Вернувшийся в разведанный коридор ходок теперь видит
        # в кадре ТОЛЬКО разведанное: замерено, «не открыто» 0 клеток из
        # 175, и исправный свет краснел на составе сцены. Конец первой ноги
        # — это, наоборот, кромка разведки: позади память, впереди чернота.
        cells = None
        for leg, (frm, want) in enumerate(((src, WALK_LEG),
                                           (src, WALK_BACK))):
            tgt = pick(frm, want)
            ok, gap, secs, nudges = walk_to(t, level, tgt, keys, WALK_BUDGET,
                                            avoid=avoid)
            keys.release()
            time.sleep(0.9)
            me = t.self_pos()
            note("разведка, нога %d" % (leg + 1),
                 "цель %s (%.0f клеток от начала), пришёл в (%.1f, %.1f) за "
                 "%.1f с (дошёл: %s, промах %.2f, рывков поперёк %d)"
                 % (tgt, want, me["x"], me["y"], secs,
                    "да" if ok else "нет", gap, nudges))
            ents_now = [(e["x"], e["y"]) for e in t.others()]
            ents_now.append((me["x"], me["y"]))
            cells = floor_cells(level, fog_of(t), cam(t), size, ents_now)
            note("состав кадра после ноги %d" % (leg + 1),
                 "освещено %d, память %d, не открыто %d (нужно >= %d каждого)"
                 % (len(cells[VIS_LIT]), len(cells[VIS_SEEN]),
                    len(cells[VIS_DARK]), MIN_CELLS))
            if all(len(cells[v]) >= MIN_CELLS
                   for v in (VIS_LIT, VIS_SEEN, VIS_DARK)):
                break
        lvl_now = t.js("window.__zza.level()")
        check(lvl_now["floor"] == floor0,
              "этаж под замером не сменился (ходок не наступил на лестницу)",
              "этаж %d, был %d" % (lvl_now["floor"], floor0))

        # --- кадр стоящего клиента детерминирован -------------------------
        a1 = t.js("window.__zza.pixels(0,0,0,0,%d)" % DARK_T)
        time.sleep(0.25)
        a2 = t.js("window.__zza.pixels(0,0,0,0,%d)" % DARK_T)
        check(a1["hash"] == a2["hash"] and a1["dark"] == a2["dark"],
              "кадр стоящего клиента детерминирован — шум замера равен нулю",
              "два замера подряд: хэш %d и %d, чёрных %d и %d из %d"
              % (a1["hash"], a2["hash"], a1["dark"], a2["dark"], a1["n"]))

        # ============ 1. СВЕТ ЕСТЬ СВЕТ ==================================
        print()
        print("  --- 1. освещено против памяти ------------------------------")
        c = cam(t)
        fog = fog_of(t)
        ents = [(e["x"], e["y"]) for e in t.others()]
        ents.append((me["x"], me["y"]))
        cells = floor_cells(level, fog, c, size, ents)
        note("клеток пола в кадре",
             "освещено %d, память %d, не открыто %d"
             % (len(cells[VIS_LIT]), len(cells[VIS_SEEN]),
                len(cells[VIS_DARK])))
        enough = all(len(cells[v]) >= MIN_CELLS
                     for v in (VIS_LIT, VIS_SEEN, VIS_DARK))
        check(enough, "в кадре есть все три состояния тумана, и не по одной клетке",
              "освещено %d, память %d, не открыто %d (нужно >= %d каждого)"
              % (len(cells[VIS_LIT]), len(cells[VIS_SEEN]),
                 len(cells[VIS_DARK]), MIN_CELLS))

        on = measure_classes(t, cells, c, size)
        show("СО СВЕТОМ", on)
        t.js("window.__zza.setTorch(false)")
        time.sleep(0.4)
        off = measure_classes(t, cells, c, size)
        show("БЕЗ СВЕТА (клиент, каким он был)", off)
        t.js("window.__zza.setTorch(true)")
        time.sleep(0.4)

        r_on = on[VIS_LIT]["lum"] / max(1e-9, on[VIS_SEEN]["lum"])
        r_off = off[VIS_LIT]["lum"] / max(1e-9, off[VIS_SEEN]["lum"])
        m_on = on[VIS_LIT]["mean"] / max(1e-9, on[VIS_SEEN]["mean"])
        m_off = off[VIS_LIT]["mean"] / max(1e-9, off[VIS_SEEN]["mean"])
        print()
        print("  ОСВЕЩЕНО / ПАМЯТЬ:  со светом %.2f   без света %.2f   "
              "(Rec.709)" % (r_on, r_off))
        print("  то же по максимальному каналу (старая мерка): %.2f -> %.2f"
              % (m_off, m_on))
        uptime()

        check(r_on >= LIGHT_RATIO_MIN,
              "освещённый пол заметно ярче памяти группы",
              "%.2f при пороге %.2f (одна ступень экспозиции плюс 10%%); "
              "яркость %.1f против %.1f"
              % (r_on, LIGHT_RATIO_MIN, on[VIS_LIT]["lum"],
                 on[VIS_SEEN]["lum"]))
        check(r_off < LIGHT_RATIO_MIN,
              "проверка КРАСНЕЕТ на клиенте без добавленного света",
              "на той же сцене без света отношение %.2f < %.2f — то есть "
              "проверка его НЕ пропускает" % (r_off, LIGHT_RATIO_MIN))

        # ============ 2. ТУМАН ПО-ПРЕЖНЕМУ ЧИТАЕТСЯ ======================
        print()
        print("  --- 2. туман не испорчен -----------------------------------")
        check(on[VIS_DARK]["mean"] <= DARK_T,
              "неразведанное осталось чёрным",
              "средний максимальный канал %.2f при пороге черноты %d "
              "(без света было %.2f)"
              % (on[VIS_DARK]["mean"], DARK_T, off[VIS_DARK]["mean"]))
        check(on[VIS_SEEN]["mean"] >= SEEN_MIN,
              "память не притушена ради отношения",
              "средний максимальный канал %.1f при пороге %d (вдвое выше "
              "линии черноты %d); без света на тех же клетках %.1f"
              % (on[VIS_SEEN]["mean"], SEEN_MIN, DARK_T,
                 off[VIS_SEEN]["mean"]))
        check(abs(on[VIS_SEEN]["lum"] - off[VIS_SEEN]["lum"]) < 1.0
              and abs(on[VIS_DARK]["lum"] - off[VIS_DARK]["lum"]) < 1.0,
              "свет не тронул ни память, ни черноту — только освещённое",
              "память %.2f -> %.2f, чернота %.2f -> %.2f; освещённое "
              "%.1f -> %.1f"
              % (off[VIS_SEEN]["lum"], on[VIS_SEEN]["lum"],
                 off[VIS_DARK]["lum"], on[VIS_DARK]["lum"],
                 off[VIS_LIT]["lum"], on[VIS_LIT]["lum"]))

        # ============ 3. СТОИМОСТЬ КАДРА =================================
        print()
        print("  --- 3. стоимость кадра -------------------------------------")
        ms_on1, s1 = flush_ms(t)
        t.js("window.__zza.setTorch(false)")
        time.sleep(0.3)
        ms_off, s2 = flush_ms(t)
        t.js("window.__zza.setTorch(true)")
        time.sleep(0.3)
        ms_on2, s3 = flush_ms(t)
        print("  СТОИМОСТЬ КАДРА СО СЛИВОМ (одна сцена, персонаж стоит; "
              "медиана по %d пачкам из %d кадров):" % (BENCH_REPS, BENCH_DRAWS))
        print("     со светом %.4f мс   без света %.4f мс   со светом снова "
              "%.4f мс" % (ms_on1, ms_off, ms_on2))
        print("     разброс по пачкам: %s | %s | %s"
              % (" ".join("%.4f" % v for v in s1),
                 " ".join("%.4f" % v for v in s2),
                 " ".join("%.4f" % v for v in s3)))
        uptime()
        ratio = max(ms_on1, ms_on2) / max(1e-9, ms_off)
        bf_on = t.js("window.__zza.benchFog(120)")
        t.js("window.__zza.setTorch(false)")
        time.sleep(0.2)
        bf_off = t.js("window.__zza.benchFog(120)")
        t.js("window.__zza.setTorch(true)")
        time.sleep(0.2)
        note("пересчёт маски тумана",
             "со светом %.4f мс, без света %.4f мс. Свет едет за игроком, "
             "поэтому маска пересчитывается ещё и на шаг в полклетки: при "
             "беге 5 кл/с это 10 раз в секунду на игрока сверх пяти на "
             "дельте vis, то есть %.2f мс в секунду — %.2f%% от секунды"
             % (bf_on["repaint"], bf_off["repaint"], bf_on["repaint"] * 15,
                bf_on["repaint"] * 15 / 10.0))
        check(ratio <= MS_RATIO_MAX,
              "стоимость кадра от света не выросла",
              "отношение %.2f при пороге %.2f. Свет живёт в маске тумана и "
              "не добавляет в кадр ни вызова, ни пикселя — ожидание 1.00. "
              "Абсолютные миллисекунды здесь SwiftShader, числом целевой "
              "машины они НЕ являются" % (ratio, MS_RATIO_MAX))

        # ============ 4. СТРЕЛОК ОТЛИЧИМ ОТ РУБАКИ =======================
        print()
        print("  --- 4. стрелок против рубаки -------------------------------")
        c = cam(t)
        fog = fog_of(t)
        me = t.self_pos()
        cells = floor_cells(level, fog, c, size, [(me["x"], me["y"])],
                            margin=2.0, keep=2.2)
        # Врагов надо ставить в НАДЁЖНО освещённые клетки рядом с игроком:
        # у кромки света маска тумана растянута билинейно и захватывает
        # клетку краем (7.2), а клетка на пределе обзора может погаснуть
        # между чтением тумана и кадром — и тогда клиент честно спрячет
        # врага (5.2), а проверка решит, что он нарисован неправильно.
        # Условие: все четыре соседа тоже VIS_LIT, и до игрока не дальше
        # BODY_NEAR клеток.
        lw, lcells = fog["w"], fog["cells"]
        def solid_lit(tx, ty):
            for nx, ny in ((tx + 1, ty), (tx - 1, ty), (tx, ty + 1),
                           (tx, ty - 1)):
                if lcells[ny * lw + nx] != VIS_LIT:
                    return False
            return True
        lit = [q for q in cells[VIS_LIT]
               if solid_lit(q[0], q[1])
               and math.hypot(q[0] + 0.5 - me["x"],
                              q[1] + 0.5 - me["y"]) <= BODY_NEAR]
        lit.sort(key=lambda q: math.hypot(q[0] + 0.5 - me["x"],
                                          q[1] + 0.5 - me["y"]))
        pair = None
        for i in range(len(lit)):
            for j in range(i + 1, len(lit)):
                if math.hypot(lit[i][0] - lit[j][0],
                              lit[i][1] - lit[j][1]) >= 2.0:
                    pair = (lit[i], lit[j])
                    break
            if pair:
                break
        if pair is None:
            check(False, "нашлись две освещённые клетки пола под двух врагов")
        else:
            (ax, ay, asx, asy), (bx, by, bsx, bsy) = pair
            half = c["t"] * 0.35 * 1.3     # радиус тела 0.35 клетки * 1.3
            tick = t.js("window.__zza.tick()")
            b0 = boxes_mean(t, [(asx - half, asy - half, half * 2, half * 2),
                                (bsx - half, bsy - half, half * 2, half * 2)],
                            BODY_T)
            t.js("window.__zza.wire(%s)" % json.dumps(snap_wire(tick, [
                ent_row(900101, K_ENEMY, ax + 0.5, ay + 0.5, 0.0),
                ent_row(900102, K_ENEMY_RANGED, bx + 0.5, by + 0.5, 0.0)])))
            time.sleep(0.45)
            b1 = boxes_mean(t, [(asx - half, asy - half, half * 2, half * 2),
                                (bsx - half, bsy - half, half * 2, half * 2)],
                            BODY_T)
            n_melee = b1[0]["bright"] - b0[0]["bright"]
            n_arch = b1[1]["bright"] - b0[1]["bright"]
            shape = n_melee / max(1.0, float(n_arch))
            st = t.js("window.__zza.stats()")
            note("коробки вокруг тел",
                 "%dx%d пикселей, порог тела %d; рубака в (%d,%d), стрелок "
                 "в (%d,%d); сущностей в мире %d, скрыто туманом %d"
                 % (round(half * 2), round(half * 2), BODY_T, ax, ay, bx, by,
                    st["ents"], st["hidden"]))
            check(n_melee > 0 and n_arch > 0,
                  "оба врага вообще нарисованы",
                  "пикселей тела: рубака %d, стрелок %d (до подачи %d и %d)"
                  % (n_melee, n_arch, b0[0]["bright"], b0[1]["bright"]))
            check(shape >= SHAPE_RATIO_MIN,
                  "силуэты рубаки и стрелка различаются, и не на пиксель",
                  "заполнение коробки: рубака %d пикселей, стрелок %d, "
                  "отношение %.2f при пороге %.2f (по построению круг против "
                  "наконечника это 3.14/1.21 = 2.6)"
                  % (n_melee, n_arch, shape, SHAPE_RATIO_MIN))

            # ============ 5. НЕЗНАКОМЫЙ kind НЕ РОНЯЕТ КЛИЕНТ ============
            # Сервер может начать слать новое значение раньше, чем клиент про
            # него узнает, и наоборот — пока сервер шлёт обоих врагов одним
            # kind = 2. Ни то, ни другое не имеет права уронить кадр.
            errs_before = len(t.errors())
            tick = t.js("window.__zza.tick()")
            t.js("window.__zza.wire(%s)" % json.dumps(snap_wire(tick, [
                ent_row(900103, K_UNKNOWN, ax + 0.5, ay + 1.5, 0.0)])))
            time.sleep(0.45)
            st = t.js("window.__zza.stats()")
            check(len(t.errors()) == errs_before and st["ents"] > 0,
                  "незнакомый kind клиент не роняет",
                  "подан kind=%d: ошибок в консоли не прибавилось, кадр "
                  "нарисован (сущностей %d, вызовов %d)"
                  % (K_UNKNOWN, st["ents"], st["drawCalls"]))

        # ============ 6. КОНСОЛЬ =========================================
        print()
        errs = t.errors()
        check(not errs, "ноль ошибок в консоли браузера",
              "ошибок %d%s" % (len(errs),
                               (": " + "; ".join(errs[:3])) if errs else ""))
        t.close()
        browser.close()

    return summary()


if __name__ == "__main__":
    sys.exit(main())
