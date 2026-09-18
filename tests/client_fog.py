#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Туман войны на экране клиента (DESIGN.md 4.4, 5.2, 7.2).

Сервер считает туман и шлёт его полем `vis`. Эта проверка отвечает на один
вопрос: ВИДНА ЛИ темнота в кадре и ведёт ли она себя как туман.

Меряются настоящие пиксели настоящей канвы (getImageData через
window.__zza.pixels), а не модель мира: «видно» — это то, что попало в
кадр. Браузер — тот же Chromium из /opt/pw-browsers, сервер — настоящий
play.py, вкладка входит в игру кнопками и бежит клавишами.

ЧТО ПРОВЕРЯЕТСЯ

  1. Доля ЧЁРНЫХ пикселей кадра падает по ходу пробежки: вкладка бежит к
     лестнице и ВОЗВРАЩАЕТСЯ в исходную точку, так что сравниваются два
     кадра с одной и той же камерой. Туман, который не рассеивается, — не
     туман.
  2. В неразведанной зоне не нарисовано ничего: берём стену за пределами
     открытой области и показываем, что там ровный чёрный, а не стена.
  3. Сущность на клетке «открыто, но сейчас не видно» в кадр не попадает, а
     такая же сущность на освещённой клетке — попадает. Сервер шлёт всех
     (4.4: снапшот один на комнату), прятать невидимых обязан клиент.
  4. Проверка 1 КРАСНЕЕТ на клиенте, который игнорирует `vis` (каким он и
     был до этой работы): тот же замер, те же две точки пробежки.
  5. Стоимость кадра не выросла втрое: stats().ms с туманом и без, спина к
     спине, на одной и той же сцене.
  6. Лестница вниз (тайл 2) заметна: она не красится как обычный пол.
  7. Ноль ошибок в консоли.

ОТКУДА ПОРОГИ (выведены из величин, не подобраны под прогон)

  * «Чёрный пиксель» — максимальный канал <= 10 из 255. Неразведанная
    клетка красится непрозрачным #000, то есть ровно (0,0,0), и виньетка
    (она только УМНОЖАЕТ на затемнение) оставляет её нулём. Самый тёмный
    РАЗВЕДАННЫЙ пиксель — боковина стены #1b2028 в памяти группы, в
    дальнем углу экрана под виньеткой: (27,32,40)*0.38 + (8,13,30)*0.62 =
    (15,20,34), под виньеткой ~*0.47 = (7,10,16), максимальный канал 16.
    Порог 10 лежит между 0 и 16 с запасом 1.6 раза в обе стороны.
  * Падение доли черноты — не меньше 5 процентных пунктов. Одна клетка на
    экране 1920x1080 при 48 px/клетка это 48*48/(1920*1080) = 0.11 п.п.,
    значит 5 п.п. = 45 вновь запомненных клеток на экране. Пробежка на два
    десятка клеток с радиусом обзора 10 (server/vis.py) открывает заведомо
    больше, а замер делается В ТОЙ ЖЕ ТОЧКЕ, куда вкладка возвращается
    после пробежки: камера тогда не участвует в разнице вовсе.
    Снизу порог защищён тем, что кадр стоящего клиента
    ДЕТЕРМИНИРОВАН: два замера подряд совпадают до пикселя, и это здесь же
    проверяется, — то есть шум замера равен нулю, а не 5 п.п.
  * Заметность лестницы — отношение самого яркого красного канала в клетке
    лестницы к такому же в соседней клетке пола, не меньше 2.0. Кромка
    лестницы #f0c96a даёт r=240, пол #23293a даёт r=35, то есть настоящее
    отношение около 6.9 — запас 3.4 раза.
  * Стоимость кадра — не больше чем втрое. Порог задан заданием; печатается
    само отношение, а рядом uptime: абсолютные миллисекунды из контейнера с
    софтверным рендером (SwiftShader) НЕ являются числом целевой машины и
    за него не выдаются. Осмысленно только отношение — оба замера сделаны
    одним и тем же софтверным рендером подряд.

Запуск:
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/client_fog.py
Выход: 0 — зелено, 1 — красно.
"""

import collections
import json
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client_common import (BROWSER_ARGS, CHROMIUM, Server, Tab, check,
                           clear_dist, note, summary, uptime)

# --- размеры и пороги -----------------------------------------------------

VIEW_W, VIEW_H = 1920, 1080    # 2.1a: целевое разрешение контракта
DARK_T = 10                    # «чёрный пиксель»: максимальный канал <= 10
DROP_MIN = 0.05                # на сколько обязана упасть доля черноты
STAIRS_RATIO = 2.0             # во сколько раз лестница краснее пола
MS_RATIO_MAX = 3.0             # «не выросла втрое»
WALK_BUDGET = 60.0             # с, потолок на дорогу (замерено: доходит за 12..32 с)
MS_FRAMES = 150                # кадров в каждом замере по rAF (данные)
BENCH_DRAWS = 300              # вызовов draw() в одной пачке замера

VIS_DARK, VIS_SEEN, VIS_LIT = 0, 1, 2
TILE_WALL, TILE_FLOOR, TILE_STAIRS = 0, 1, 2


# --- ходьба по карте ------------------------------------------------------

class Keys(object):
    """Клавиши держатся зажатыми, как у человека, а не долбятся по 10 Гц."""

    def __init__(self, tab):
        self.tab = tab
        self.held = set()

    def set(self, want):
        want = set(want)
        for k in self.held - want:
            self.tab.page.keyboard.up(k)
        for k in want - self.held:
            self.tab.page.keyboard.down(k)
        self.held = want

    def release(self):
        self.set([])


# Полоса нечувствительности по поперечной оси. Тело игрока круглое, R=0.35
# (4.2), коридор шириной ровно в клетку: от середины можно уйти на 0.15
# клетки, дальше край тела цепляет соседний ряд и physics.move_circle
# обнуляет скорость по этой оси. Поэтому клавиша поперечной оси жмётся по
# АБСОЛЮТНОМУ промаху, а не по отношению к продольному: первый вариант
# сравнивал оси между собой, при промахе 0.18 против хода 0.63 поперечную
# клавишу не жал — и персонаж стоял в проёме вечно (замерено).
BAND = 0.10


def keys_for(dx, dy, band=BAND):
    ax, ay = abs(dx), abs(dy)
    kx = "KeyD" if dx > 0 else ("KeyA" if dx < 0 else None)
    ky = "KeyS" if dy > 0 else ("KeyW" if dy < 0 else None)
    out = []
    if kx and ax > band:
        out.append(kx)
    if ky and ay > band:
        out.append(ky)
    if not out:
        out = [k for k in (kx, ky) if k]
    return out


def bfs_path(level, src, dst):
    """Кратчайший путь по проходимым тайлам (всё, что не 0 — не стена, 4.1)."""
    w, h, t = level["w"], level["h"], level["tiles"]
    prev = {src: None}
    q = collections.deque([src])
    while q:
        c = q.popleft()
        if c == dst:
            break
        x, y = c
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and t[ny * w + nx] != TILE_WALL \
                    and (nx, ny) not in prev:
                prev[(nx, ny)] = c
                q.append((nx, ny))
    if dst not in prev:
        return None
    path, c = [], dst
    while c is not None:
        path.append(c)
        c = prev[c]
    path.reverse()
    return path


def pick_wp(level, mx, my, path):
    """Самая дальняя точка пути, до которой видно по прямой без стены.

    Иначе персонаж цепляется плечом за угол: путь-то по клеткам, а тело
    круглое (R_PLAYER в client_common).
    """
    best = path[1] if len(path) > 1 else path[0]
    for i in range(1, min(7, len(path))):
        wx, wy = path[i][0] + 0.5, path[i][1] + 0.5
        dx, dy = wx - mx, wy - my
        d = math.hypot(dx, dy)
        if d < 1e-6:
            continue
        if clear_dist(level, mx, my, dx / d, dy / d, d + 0.1) >= d - 0.05:
            best = path[i]
    return best


def walk_to(tab, level, target, keys, budget):
    """Дойти до клетки target. Возвращает (дошёл, расстояние, секунды, рывков).

    Две поправки, заработанные прогоном (обе — про настоящую физику, а не
    про проверку):

    * продвижение считается по ДЛИНЕ ОСТАВШЕГОСЯ ПУТИ, а не по расстоянию по
      прямой: на карте из комнат и коридоров честный обход стены регулярно
      уводит ОТ цели, и первый вариант объявлял это застреванием;
    * персонаж намертво встаёт плечом об угол. Тело круглое, R=0.35 (4.2), а
      коридор шириной ровно в клетку: от середины можно уйти только на 0.15
      клетки, дальше край тела цепляет соседний ряд. physics.move_circle в
      этот момент обнуляет скорость по оси, и при одной зажатой клавише
      персонаж стоит вечно — замерено: 27 секунд в одной точке. Человек в
      этом месте просто дёргает поперёк, и мы делаем то же.
    """
    t0 = time.time()
    best = 10 ** 9
    last_gain = t0
    dist = 1e9
    nudges = 0
    px = py = None
    pt = t0
    side = 1
    while time.time() - t0 < budget:
        me = tab.self_pos()
        if me is None:
            break
        mx, my = me["x"], me["y"]
        dist = math.hypot(mx - (target[0] + 0.5), my - (target[1] + 0.5))
        if dist < 0.9:
            keys.release()
            return True, dist, time.time() - t0, nudges
        path = bfs_path(level, (int(mx), int(my)), target)
        if not path:
            break
        if len(path) < best:
            best = len(path)
            last_gain = time.time()
        if time.time() - last_gain > 8.0:
            break                      # путь не сокращается — дальше незачем
        wp = pick_wp(level, mx, my, path)
        dx, dy = wp[0] + 0.5 - mx, wp[1] + 0.5 - my
        moved = 1e9 if px is None else math.hypot(mx - px, my - py)
        if moved < 0.08 and time.time() - pt > 0.45:
            nudges += 1
            keys.set(keys_for(-dy * side, dx * side))   # поперёк курса
            time.sleep(0.2)
            side = -side
            px, py, pt = mx, my, time.time()
            continue
        if moved >= 0.08:
            px, py, pt = mx, my, time.time()
        keys.set(keys_for(dx, dy))
        time.sleep(0.08)
    keys.release()
    return False, dist, time.time() - t0, nudges


# --- чтение кадра ---------------------------------------------------------

def frame_dark(tab):
    """Доля чёрных пикселей всего кадра."""
    r = tab.js("window.__zza.pixels(0,0,0,0,%d)" % DARK_T)
    return r["frac"], r


def box(tab, x, y, w, h):
    return tab.js("window.__zza.pixels(%d,%d,%d,%d,%d)" % (x, y, w, h, DARK_T))


def fog_of(tab):
    return tab.js("window.__zza.fog()")


def cam(tab):
    return tab.js("({x: window.__zza.app.render.camX, "
                  "y: window.__zza.app.render.camY, "
                  "t: window.__zza.app.render.tilePx})")


def on_screen(c, size, tx, ty, margin_tiles=1.5):
    """Экранный центр клетки, если она не у самого края кадра."""
    sx = (tx + 0.5 - c["x"]) * c["t"]
    sy = (ty + 0.5 - c["y"]) * c["t"]
    m = margin_tiles * c["t"]
    if sx < m or sy < m or sx > size["w"] - m or sy > size["h"] - m:
        return None
    return sx, sy


def find_cell(fog, c, size, want, tile=None, away_from_dark=0, level=None,
              not_near=None, min_cells=0.0):
    """Клетка в нужном состоянии тумана, видимая на экране.

    away_from_dark — сколько клеток вокруг обязаны быть в том же состоянии
    (нужно для честной точки «за пределами открытой области»: рядом с
    границей света мягкая кайма, и там чернота уже не абсолютная).
    """
    w, h, cells = fog["w"], fog["h"], fog["cells"]
    best = None
    for ty in range(h):
        for tx in range(w):
            if cells[ty * w + tx] != want:
                continue
            if tile is not None and level["tiles"][ty * w + tx] != tile:
                continue
            p = on_screen(c, size, tx, ty)
            if p is None:
                continue
            if not_near is not None and \
                    math.hypot(tx + 0.5 - not_near[0], ty + 0.5 - not_near[1]) < min_cells:
                continue
            if away_from_dark:
                ok = True
                for dy in range(-away_from_dark, away_from_dark + 1):
                    for dx in range(-away_from_dark, away_from_dark + 1):
                        nx, ny = tx + dx, ty + dy
                        v = cells[ny * w + nx] if (0 <= nx < w and 0 <= ny < h) else want
                        if v != want:
                            ok = False
                            break
                    if not ok:
                        break
                if not ok:
                    continue
            # чем ближе к центру кадра, тем меньше мешает виньетка
            d = math.hypot(p[0] - size["w"] / 2, p[1] - size["h"] / 2)
            if best is None or d < best[0]:
                best = (d, tx, ty, p[0], p[1])
    return None if best is None else best[1:]


def enemy_wire(tick, eid, x, y):
    """Настоящее сообщение снапшота из 5.2 с одним врагом (kind=2).

    Подаётся в net._onMessage — ровно туда же, куда приходит провод.
    Порядок полей — 4.3, ровно десять и ровно в этом порядке.
    """
    return json.dumps({"t": "snap", "ack": 0, "tick": tick, "full": False,
                       "e": [[eid, 2, round(x, 3), round(y, 3),
                              0.0, 0.0, 100, 100, 0, 0.0]]})


def frame_ms(tab, reps=5, n=BENCH_DRAWS):
    """Стоимость одного draw() на текущей сцене, медиана по reps замерам.

    Замер идёт пачкой из n вызовов подряд: performance.now() в браузере
    огрублён до 0.1 мс, а весь кадр стоит меньше кванта — на одиночных
    кадрах отношение «с туманом / без» вырождается в 0.1/0.0 и ничего не
    значит. Пачка из %d вызовов даёт разрешение на два порядка лучше.
    """
    out = []
    for _ in range(reps):
        out.append(tab.js("window.__zza.benchDraw(%d)" % n))
        time.sleep(0.05)
    return statistics.median(out), out


def ring_ms(tab, frames):
    """То же, но по НАСТОЯЩИМ кадрам rAF — как данные, не как порог."""
    tab.js("window.__zza.msRecord(true)")
    t0 = time.time()
    while time.time() - t0 < 12:
        time.sleep(0.15)
        if tab.js("window.__zza.msSamples().length") >= frames:
            break
    s = tab.js("window.__zza.msSamples()")
    tab.js("window.__zza.msRecord(false)")
    s = [v for v in s if v is not None]
    if not s:
        return None, 0
    return sum(s) / len(s), len(s)


# --- прогон ---------------------------------------------------------------

def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("нет пакета playwright: pip install playwright "
              "(БЕЗ playwright install — браузер лежит в /opt/pw-browsers)")
        return 2

    print("=== туман войны в кадре клиента (DESIGN.md 4.4, 5.2, 7.2) ===")
    print("  разрешение вкладки %dx%d, 48 px/клетка — как в 4.1"
          % (VIEW_W, VIEW_H))
    uptime()

    with Server() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM, headless=True,
                                     args=BROWSER_ARGS)
        t = Tab(browser, "A", width=VIEW_W, height=VIEW_H).open(srv.url)
        room = t.create_room("Туман")
        t.ready()
        t.wait_game()
        print("  комната %s, игра пошла" % room)

        # Ждём первый снапшот с полем vis: без него проверять нечего.
        t.page.wait_for_function(
            "window.__zza.fog() && window.__zza.fog().msgs > 0", timeout=10000)
        time.sleep(0.6)
        level = t.js("window.__zza.level()")
        size = t.js("window.__zza.canvasSize()")
        f0 = fog_of(t)
        n0 = collections.Counter(f0["cells"])
        note("карта", "%dx%d, лестница на %s"
             % (level["w"], level["h"],
                next(("(%d,%d)" % (i % level["w"], i // level["w"])
                      for i, v in enumerate(level["tiles"]) if v == TILE_STAIRS),
                     "нет")))
        note("туман сразу после входа",
             "не открыто %d, память %d, видно сейчас %d (сообщений vis %d)"
             % (n0[VIS_DARK], n0[VIS_SEEN], n0[VIS_LIT], f0["msgs"]))

        # --- метод: кадр стоящего клиента детерминирован -------------------
        a = frame_dark(t)[1]
        time.sleep(0.25)
        b = frame_dark(t)[1]
        check(a["dark"] == b["dark"],
              "два замера одного и того же кадра совпадают до пикселя",
              "чёрных %d и %d из %d — шум замера равен нулю"
              % (a["dark"], b["dark"], a["n"]))

        # --- ЧИСЛО 1: чернота в начале ------------------------------------
        c_start = cam(t)
        dark_on_start = a["frac"]
        t.js("window.__zza.setFog(false)")
        time.sleep(0.25)
        dark_off_start = frame_dark(t)[0]
        t.js("window.__zza.setFog(true)")
        time.sleep(0.25)
        print()
        print("  НАЧАЛО: чёрных пикселей %.1f%% (с туманом) / %.1f%% "
              "(клиент игнорирует vis)"
              % (dark_on_start * 100, dark_off_start * 100))

        # --- пробежка: туда к лестнице и обратно --------------------------
        # ОБРАТНО — принципиально. Доля черноты кадра зависит не только от
        # тумана, но и от того, где стоит камера: закончив в тесном коридоре
        # посреди скалы, клиент честно покажет почти чёрный экран даже после
        # большой разведки (замерено: 4.7 п.п. падения на такой концовке).
        # Возврат в ТУ ЖЕ точку убирает камеру из числа переменных: кадр тот
        # же самый, разница между замерами — ровно то, что группа запомнила.
        stairs = next(((i % level["w"], i // level["w"])
                       for i, v in enumerate(level["tiles"]) if v == TILE_STAIRS),
                      None)
        keys = Keys(t)
        me0 = t.self_pos()
        home = (int(me0["x"]), int(me0["y"]))
        if stairs is None:
            check(False, "на этаже есть лестница вниз (тайл 2)")
            reached, gap, secs, nudges = False, 0.0, 0.0, 0
        else:
            reached, gap, secs, nudges = walk_to(t, level, stairs, keys,
                                                 WALK_BUDGET)
        time.sleep(0.6)
        me1 = t.self_pos()
        ran = math.hypot(me1["x"] - me0["x"], me1["y"] - me0["y"])
        note("пробежка туда", "от (%.1f, %.1f) до (%.1f, %.1f), по прямой %.1f "
                              "клетки, %.1f с, до лестницы %.2f клетки, "
                              "рывков поперёк %d"
             % (me0["x"], me0["y"], me1["x"], me1["y"], ran, secs, gap, nudges))

        f1 = fog_of(t)
        n1 = collections.Counter(f1["cells"])
        note("туман после пробежки",
             "не открыто %d (было %d), память %d (было %d), видно сейчас %d"
             % (n1[VIS_DARK], n0[VIS_DARK], n1[VIS_SEEN], n0[VIS_SEEN],
                n1[VIS_LIT]))
        check(n1[VIS_DARK] > 0 and n1[VIS_SEEN] > 0 and n1[VIS_LIT] > 0,
              "на карте есть все три состояния клетки (5.2)",
              "не открыто %d, память %d, видно сейчас %d"
              % (n1[VIS_DARK], n1[VIS_SEEN], n1[VIS_LIT]))

        # --- пункт 6: лестница заметна ------------------------------------
        if stairs is not None and reached:
            c = cam(t)
            p = on_screen(c, size, stairs[0], stairs[1], margin_tiles=1.0)
            fnow = fog_of(t)
            state = fnow["cells"][stairs[1] * fnow["w"] + stairs[0]]
            if p is None:
                check(False, "лестница попала в кадр", "камера её не захватила")
            else:
                q = int(c["t"] * 0.30)
                st_box = box(t, p[0] - q, p[1] - q, q * 2, q * 2)
                # соседняя клетка пола того же освещения — эталон «обычный пол»
                ref = None
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                               (2, 0), (-2, 0), (0, 2), (0, -2)):
                    nx, ny = stairs[0] + dx, stairs[1] + dy
                    if level["tiles"][ny * level["w"] + nx] != TILE_FLOOR:
                        continue
                    if fnow["cells"][ny * fnow["w"] + nx] != state:
                        continue
                    pp = on_screen(c, size, nx, ny, margin_tiles=1.0)
                    if pp is None:
                        continue
                    ref = box(t, pp[0] - q, pp[1] - q, q * 2, q * 2)
                    ref_cell = (nx, ny)
                    break
                if ref is None:
                    check(False, "рядом с лестницей нашёлся обычный пол для сравнения")
                else:
                    ratio = st_box["maxR"] / max(1.0, float(ref["maxR"]))
                    note("лестница", "клетка %s, состояние тумана %d; "
                         "эталон пола %s" % (str(stairs), state, str(ref_cell)))
                    check(ratio >= STAIRS_RATIO,
                          "лестница вниз не красится как обычный пол",
                          "самый яркий красный: лестница %d, пол рядом %d, "
                          "отношение %.1f (нужно >= %.1f)"
                          % (st_box["maxR"], ref["maxR"], ratio, STAIRS_RATIO))
        else:
            check(False, "вкладка дошла до лестницы за отведённое время",
                  "не дошла %.2f клетки за %.1f с — проверка заметности "
                  "лестницы не состоялась" % (gap, secs))

        # --- возврат в исходную точку -------------------------------------
        back_ok, back_gap, back_secs, back_nudges = walk_to(
            t, level, home, keys, WALK_BUDGET)
        time.sleep(0.8)
        me2 = t.self_pos()
        c_end = cam(t)
        note("возврат в исходную точку",
             "(%.1f, %.1f) -> (%.1f, %.1f), промах %.2f клетки, %.1f с, "
             "рывков поперёк %d; камера сдвинулась на %.2f клетки"
             % (me1["x"], me1["y"], me2["x"], me2["y"], back_gap, back_secs,
                back_nudges,
                math.hypot(c_end["x"] - c_start["x"], c_end["y"] - c_start["y"])))
        check(back_gap < 1.5,
              "вкладка вернулась в ту же точку — кадр сравнивается с самим собой",
              "промах %.2f клетки (порог 1.5)" % back_gap)

        # --- ЧИСЛО 1: чернота после ---------------------------------------
        dark_on_end = frame_dark(t)[0]
        t.js("window.__zza.setFog(false)")
        time.sleep(0.25)
        dark_off_end = frame_dark(t)[0]
        t.js("window.__zza.setFog(true)")
        time.sleep(0.25)
        print()
        print("  ПОСЛЕ:  чёрных пикселей %.1f%% (с туманом) / %.1f%% "
              "(клиент игнорирует vis)"
              % (dark_on_end * 100, dark_off_end * 100))
        uptime()

        drop_on = dark_on_start - dark_on_end
        drop_off = dark_off_start - dark_off_end
        check(drop_on >= DROP_MIN,
              "туман рассеивается: в ТОЙ ЖЕ точке черноты в кадре стало меньше",
              "%.1f%% -> %.1f%%, падение %.1f п.п. (нужно >= %.0f п.п.)"
              % (dark_on_start * 100, dark_on_end * 100,
                 drop_on * 100, DROP_MIN * 100))

        # --- пункт 4: та же проверка на клиенте, игнорирующем vis ----------
        check(drop_off < DROP_MIN,
              "проверка КРАСНЕЕТ на клиенте, который игнорирует vis",
              "на нём та же пробежка даёт %.1f%% -> %.1f%%, падение %.1f п.п. "
              "< %.0f п.п. — то есть проверка его НЕ пропускает"
              % (dark_off_start * 100, dark_off_end * 100,
                 drop_off * 100, DROP_MIN * 100))

        # --- пункт 2: в неразведанной зоне не нарисовано ничего ------------
        c = cam(t)
        f2 = fog_of(t)
        spot = find_cell(f2, c, size, VIS_DARK, tile=TILE_WALL,
                         away_from_dark=2, level=level)
        if spot is None:
            check(False, "нашлась стена в неразведанной зоне внутри кадра")
        else:
            tx, ty, sx, sy = spot
            dark_box = box(t, sx - 8, sy - 8, 17, 17)
            t.js("window.__zza.setFog(false)")
            time.sleep(0.25)
            lit_box = box(t, sx - 8, sy - 8, 17, 17)
            t.js("window.__zza.setFog(true)")
            time.sleep(0.25)
            note("точка за пределами открытой области",
                 "клетка (%d,%d) — стена, до ближайшей открытой клетки >2; "
                 "экран (%d,%d)" % (tx, ty, sx, sy))
            check(dark_box["maxCh"] <= DARK_T and dark_box["frac"] == 1.0,
                  "в неразведанной клетке ровный чёрный, а не стена",
                  "самый яркий канал в коробке 17x17 = %d (порог %d), "
                  "чёрных пикселей %.0f%%; с игнором vis там же %d — "
                  "то есть стена там нарисована и именно её туман и прячет"
                  % (dark_box["maxCh"], DARK_T, dark_box["frac"] * 100,
                     lit_box["maxCh"]))
            check(lit_box["maxCh"] > DARK_T,
                  "без тумана в той же точке видна стена (проверка не пустая)",
                  "самый яркий канал %d > %d" % (lit_box["maxCh"], DARK_T))

        # --- пункт 3: враг не светится сквозь темноту ----------------------
        # Сервер этапа 0c врагов пока не заводит, поэтому враг подаётся
        # НАСТОЯЩИМ сообщением снапшота (5.2) в net._onMessage — тем же
        # входом, куда приходит провод. Проверяется ровно то, за что отвечает
        # клиент: снапшот принёс сущность, клиент решает, рисовать её или нет.
        me = t.self_pos()
        tick = t.js("window.__zza.tick()")
        seen_cell = find_cell(f2, c, size, VIS_SEEN, tile=TILE_FLOOR,
                              level=level, not_near=(me["x"], me["y"]),
                              min_cells=2.0)
        lit_cell = find_cell(f2, c, size, VIS_LIT, tile=TILE_FLOOR,
                             level=level, not_near=(me["x"], me["y"]),
                             min_cells=2.0)
        if seen_cell is None or lit_cell is None:
            check(False, "нашлись клетки «память» и «видно сейчас» внутри кадра",
                  "память %s, свет %s" % (seen_cell, lit_cell))
        else:
            tx, ty, sx, sy = seen_cell
            half = int(c["t"] * 0.7)
            before = box(t, sx - half, sy - half, half * 2, half * 2)
            t.js("window.__zza.wire(%s)" % json.dumps(
                enemy_wire(tick, 900001, tx + 0.5, ty + 0.5)))
            time.sleep(0.4)
            after = box(t, sx - half, sy - half, half * 2, half * 2)
            st = t.js("window.__zza.stats()")
            note("враг подан в клетку памяти",
                 "(%d,%d), сущностей в мире %d, скрыто туманом %d"
                 % (tx, ty, t.js("window.__zza.ents()"), st["hidden"]))
            check(after["maxR"] == before["maxR"] and after["dark"] == before["dark"],
                  "враг на клетке «открыто, но не видно» в кадр не попал",
                  "коробка %dx%d вокруг него не изменилась ни на пиксель: "
                  "самый яркий красный %d -> %d, чёрных %d -> %d"
                  % (half * 2, half * 2, before["maxR"], after["maxR"],
                     before["dark"], after["dark"]))
            check(st["hidden"] >= 1,
                  "клиент сам сообщает, что спрятал сущность",
                  "stats().hidden = %d" % st["hidden"])

            # А теперь такой же враг на освещённой клетке — он обязан быть
            # виден, иначе проверка выше зеленела бы на клиенте, который
            # просто не рисует врагов вовсе.
            tx2, ty2, sx2, sy2 = lit_cell
            before2 = box(t, sx2 - half, sy2 - half, half * 2, half * 2)
            t.js("window.__zza.wire(%s)" % json.dumps(
                enemy_wire(tick, 900002, tx2 + 0.5, ty2 + 0.5)))
            time.sleep(0.4)
            after2 = box(t, sx2 - half, sy2 - half, half * 2, half * 2)
            check(after2["maxR"] >= before2["maxR"] + 60,
                  "такой же враг на освещённой клетке в кадр попал",
                  "клетка (%d,%d): самый яркий красный %d -> %d "
                  "(враг #d16a6a это r=209)"
                  % (tx2, ty2, before2["maxR"], after2["maxR"]))

        # --- пункт 5: стоимость кадра --------------------------------------
        # Сцена одна и та же: персонаж стоит, туман не меняется, камера не
        # едет. Разница между замерами — ровно блит маски тумана и отсев
        # сущностей.
        print()
        keys.release()
        time.sleep(0.5)
        ms_on1, s1 = frame_ms(t)
        t.js("window.__zza.setFog(false)")
        time.sleep(0.3)
        ms_off, s2 = frame_ms(t)
        t.js("window.__zza.setFog(true)")
        time.sleep(0.3)
        ms_on2, s3 = frame_ms(t)
        rep = t.js("window.__zza.stats().fogRepaints")
        print("  СТОИМОСТЬ КАДРА (одна сцена, персонаж стоит; медиана по 5 "
              "пачкам из %d вызовов draw()):" % BENCH_DRAWS)
        print("     с туманом  %.4f мс   без тумана  %.4f мс   с туманом снова "
              "%.4f мс" % (ms_on1, ms_off, ms_on2))
        print("     разброс по пачкам: %s | %s | %s"
              % (" ".join("%.4f" % v for v in s1),
                 " ".join("%.4f" % v for v in s2),
                 " ".join("%.4f" % v for v in s3)))
        uptime()
        ratio = max(ms_on1, ms_on2) / max(1e-9, ms_off)
        note("перерисовок маски тумана за весь прогон", str(rep))
        # rAF-замер оставляем как данные: он огрублён квантом таймера и
        # порогом служить не может, но показывает, что пачка не врёт.
        raf_on, kn = ring_ms(t, MS_FRAMES)
        note("для сравнения, среднее по настоящим кадрам rAF",
             "%.3f мс по %d кадрам (квант performance.now() 0.1 мс — отсюда и "
             "пачки)" % (raf_on, kn))
        check(ratio <= MS_RATIO_MAX,
              "стоимость кадра с туманом не выросла втрое",
              "отношение %.2f (порог %.1f). Абсолютные миллисекунды здесь — "
              "SwiftShader, числом целевой машины они НЕ являются; осмысленно "
              "только отношение, оба замера сделаны подряд одним рендером"
              % (ratio, MS_RATIO_MAX))

        # Отдельно — цена ПЕРЕСЧЁТА маски: он случается не каждый кадр, а
        # только на дельте vis. server/vis.py пересчитывает обзор, когда
        # игрок перешёл в другую клетку: при беге 5 кл/с это 5 раз в
        # секунду, а не 100. Отсюда и вклад в секунду.
        bf = t.js("window.__zza.benchFog(120)")
        note("пересчёт маски тумана",
             "%.4f мс на пересчёт при кадре %.4f мс; на бегу это 5 пересчётов "
             "в секунду (обзор считается на смене клетки, server/vis.py), то "
             "есть %.3f мс в секунду — %.2f%% от секунды"
             % (bf["repaint"], bf["frame"], bf["repaint"] * 5,
                bf["repaint"] * 5 / 10.0))

        check(t.js("window.__zza.getFog()") is True,
              "туман возвращён во включённое состояние")

        errs = t.errors()
        check(not errs, "в консоли нет ошибок за весь прогон",
              "; ".join(errs[:3]) if errs else "0 сообщений уровня error")

        # --- данные для отчёта: что видно в кадре при радиусе обзора 10 ----
        c = cam(t)
        fend = fog_of(t)
        tw, th = size["w"] / c["t"], size["h"] / c["t"]
        cnt = collections.Counter()
        for ty in range(int(c["y"]), int(c["y"] + th) + 1):
            for tx in range(int(c["x"]), int(c["x"] + tw) + 1):
                if 0 <= tx < fend["w"] and 0 <= ty < fend["h"]:
                    cnt[fend["cells"][ty * fend["w"] + tx]] += 1
                else:
                    cnt[VIS_DARK] += 1
        tot = sum(cnt.values())
        note("клеток в кадре при %dx%d" % (size["w"], size["h"]),
             "всего %d (%.0fx%.0f): не открыто %.0f%%, память %.0f%%, "
             "видно сейчас %.0f%% — радиус обзора 10 клеток (server/vis.py)"
             % (tot, tw, th, cnt[VIS_DARK] * 100.0 / tot,
                cnt[VIS_SEEN] * 100.0 / tot, cnt[VIS_LIT] * 100.0 / tot))

        t.close()
        browser.close()

    print()
    print("  Пороги: чёрный пиксель = максимальный канал <= %d (неразведанная "
          "клетка это ровно (0,0,0)," % DARK_T)
    print("  самый тёмный разведанный пиксель под виньеткой ~16); падение "
          "черноты >= %.0f п.п. (одна клетка" % (DROP_MIN * 100))
    print("  на экране 1920x1080 = 0.11 п.п., то есть 45 клеток); лестница "
          "краснее пола вдвое при настоящих 6.9.")
    return summary()


if __name__ == "__main__":
    sys.exit(main())
