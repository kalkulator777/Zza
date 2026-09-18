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
  8. Этаж под замером не сменился. Это не украшение отчёта: лестница
     работает (8.1), а дошедший до неё ходок меняет этаж, обнуляя туман
     ПОСРЕДИ замера — см. STAIRS_KEEP.

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

ВРАГОВ В ЭТОЙ ПРОВЕРКЕ НЕТ: стенд поднимает play.py с ZZA_ENEMIES=0. Туман
меряется в кадре стоящего клиента, а живой враг — это движение в кадре и
смерть ходока посреди пробежки (дух туман не светит, 4.4). Бой проверяется
своей проверкой, tests/client_combat.py, и вот ей враги нужны.

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

from client_common import (BROWSER_ARGS, CHROMIUM, Keys, Server, Tab, check,
                           bfs_path, clear_dist, note, pick_wp, summary,
                           uptime, walk_to, WALK_STALL)

# --- размеры и пороги -----------------------------------------------------

VIEW_W, VIEW_H = 1920, 1080    # 2.1a: целевое разрешение контракта
DARK_T = 10                    # «чёрный пиксель»: максимальный канал <= 10
DROP_MIN = 0.05                # на сколько обязана упасть доля черноты
STAIRS_RATIO = 2.0             # во сколько раз лестница краснее пола
MS_RATIO_MAX = 3.0             # «не выросла втрое»

# Потолок на дорогу — ПРЕДОХРАНИТЕЛЬ ОТ ЗАВИСАНИЯ, а не порог проверки.
# Настоящее условие остановки — «путь перестал сокращаться» (WALK_STALL):
# оно не зависит от загрузки машины. Загруженная машина идёт медленнее, но
# идёт, а прежний потолок 60 с был именно порогом — headless-браузер под
# посторонней нагрузкой в него не укладывался, и проверка краснела на
# исправной игре (замер дирижёра: 1 красный из 4). Само число: этаж 64x48,
# диагональ 80 клеток, бег 5 кл/с (4.2) — 16 с чистого хода; десятикратный
# запас на обход стен, рывки поперёк и тормоза браузера.
WALK_BUDGET = 160.0
WALK_STALL = 8.0               # с без сокращения пути — дальше незачем

# Сколько клеток обязано открыться В КАДРЕ ИСХОДНОЙ ТОЧКИ, чтобы идти
# дальше было уже незачем. Одна клетка на экране 1920x1080 при 48 px/клетка
# это 48*48/(1920*1080) = 0.111 п.п. черноты, порог падения DROP_MIN = 5
# п.п. — это 45 клеток. Берём тройной запас: часть новых клеток попадает
# под виньетку и осветляет кадр меньше, чем на полную клетку.
ENOUGH_NEW_CELLS = 135

# --- НЕ НАСТУПИТЬ НА ЛЕСТНИЦУ ---------------------------------------------
#
# Мина, найденная при разборе: ходок шёл К ЛЕСТНИЦЕ как к цели, а лестница
# теперь работает. Дошёл до самой клетки — room.stairs_ready видит, что все
# живые на лестнице, и меняет этаж ПОСРЕДИ ЗАМЕРА: туман обнуляется (4.4),
# карта другая, кадр исходной точки исчез вместе с этажом. Проверка при этом
# проходила случайно — ровно потому, что до лестницы обычно не доходили.
#
# Лечение в три пояса, и первый из них — тот же принцип, что уже вылечил эту
# проверку раньше: ЖДАТЬ НАДО ЧИСЛО, А НЕ СОБЫТИЕ. Лестница здесь нужна как
# число (отношение яркости), а не как место, и снимается оно за десяток
# клеток до неё.
#
#   1) нога «к лестнице» кончается в тот момент, когда число СНЯТО;
#   2) цель этой ноги — не сама лестница, а точка пути в STAIRS_KEEP клетках
#      от неё: даже если снять не удалось вовсе, ходок останавливается в
#      стороне;
#   3) все дороги проверки обходят клетку лестницы стороной (walk_to(avoid)),
#      и в конце проверяется, что этаж под замером не сменился.
#
# Откуда STAIRS_KEEP = 2.0. walk_to объявляет приход в цель на расстоянии
# near = 0.9 клетки от её центра, значит тело встанет не ближе 2.0 - 0.9 =
# 1.1 клетки от центра лестницы. Самая дальняя точка клетки лестницы от её
# центра — угол, 0.707 клетки; чтобы оказаться НА клетке (world.on_stairs
# сравнивает int(x), int(y)), центру тела надо подойти к центру клетки ближе
# 0.707. Запас 1.1 / 0.707 = 1.56 раза.
STAIRS_KEEP = 2.0
MS_FRAMES = 150                # кадров в каждом замере по rAF (данные)
BENCH_DRAWS = 300              # вызовов draw() в одной пачке замера

VIS_DARK, VIS_SEEN, VIS_LIT = 0, 1, 2
TILE_WALL, TILE_FLOOR, TILE_STAIRS = 0, 1, 2


# Ходьба по карте (Keys, bfs_path, pick_wp, walk_to) переехала в
# client_common.py: тем же ходоком теперь пользуется tests/client_combat.py,
# а двух копий кода, который весь состоит из лечения настоящих граблей
# физики, быть не должно.

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


def new_in_view(fog0, fog1, c, size):
    """Сколько клеток открылось заново ВНУТРИ кадра с камерой c.

    Ровно та величина, из которой сделан порог падения черноты: клетки вне
    кадра исходной точки на разницу двух замеров не влияют вовсе.
    """
    w, h = fog1["w"], fog1["h"]
    a, b = fog0["cells"], fog1["cells"]
    n = 0
    for ty in range(h):
        for tx in range(w):
            i = ty * w + tx
            if a[i] == VIS_DARK and b[i] != VIS_DARK and \
                    on_screen(c, size, tx, ty, margin_tiles=0.0) is not None:
                n += 1
    return n


def nearest_dark(level, fog, c, size, mx, my):
    """Ближайшая проходимая клетка, которая ещё не разведана И попадает в
    кадр исходной точки.

    Это и есть цель добора: падение черноты считается в кадре ДОМА, и
    клетки вне этого кадра на него не влияют вовсе — сколько бы их ни
    открыли на том берегу карты.
    """
    w, h, t = level["w"], level["h"], level["tiles"]
    cells = fog["cells"]
    src = (int(mx), int(my))
    seen = {src}
    q = collections.deque([src])
    while q:
        cx, cy = q.popleft()
        i = cy * w + cx
        if cells[i] == VIS_DARK and (cx, cy) != src and \
                on_screen(c, size, cx, cy, margin_tiles=0.0) is not None:
            return (cx, cy)
        for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
            if 0 <= nx < w and 0 <= ny < h and t[ny * w + nx] != TILE_WALL \
                    and (nx, ny) not in seen:
                seen.add((nx, ny))
                q.append((nx, ny))
    return None


def measure_stairs(tab, level, size, stairs, keys):
    """Снять «лестница краснее пола», если лестница сейчас в кадре.

    Зовётся ПО ДОРОГЕ, а не после прихода, и в этом всё лечение мигания.
    Раньше замер стоял за условием «вкладка дошла до лестницы за отведённое
    время»: не успел headless-браузер под посторонней нагрузкой — красная
    проверка заметности лестницы, хотя клиент рисует её правильно. А чтобы
    увидеть лестницу, доходить до неё не надо совсем: радиус обзора 10
    клеток (server/vis.py), кадр 30x16.9 клетки при базовых 64 px (4.1) —
    она появляется на экране за несколько клеток до прихода.

    Возвращает dict с числами или None, если снять пока нечего.
    """
    if on_screen(cam(tab), size, stairs[0], stairs[1], margin_tiles=1.0) is None:
        return None
    # Меряются пиксели, значит камера обязана стоять: на ходу коробка
    # уезжает с клетки между двумя вызовами в браузер.
    keys.release()
    time.sleep(0.35)
    c = cam(tab)
    p = on_screen(c, size, stairs[0], stairs[1], margin_tiles=1.0)
    if p is None:
        return None
    fnow = fog_of(tab)
    state = fnow["cells"][stairs[1] * fnow["w"] + stairs[0]]
    if state == VIS_DARK:
        return None                    # не разведана — рисовать и нечего
    q = int(c["t"] * 0.30)
    st_box = box(tab, p[0] - q, p[1] - q, q * 2, q * 2)
    # соседняя клетка пола того же освещения — эталон «обычный пол»
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
        ref = box(tab, pp[0] - q, pp[1] - q, q * 2, q * 2)
        return {"ratio": st_box["maxR"] / max(1.0, float(ref["maxR"])),
                "st": st_box["maxR"], "ref": ref["maxR"],
                "state": state, "ref_cell": (nx, ny)}
    return None


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

    # ВРАГИ ЗДЕСЬ ВЫКЛЮЧЕНЫ, и это часть метода, а не поблажка. Проверка
    # меряет туман в кадре: ходок уходит на два десятка клеток и
    # ВОЗВРАЩАЕТСЯ в ту же точку, чтобы кадр сравнивался сам с собой. Живые
    # враги (server/ai.py) превращают этот замер в замер выживания: ходока
    # расстреливают по дороге, он становится духом, а дух туман не светит
    # (4.4) — и четыре проверки краснеют разом на совершенно исправном
    # тумане. Выключатель стоит в сервере и читается один раз при импорте.
    with Server(env={"ZZA_ENEMIES": "0"}) as srv, sync_playwright() as pw:
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
        floor0 = level["floor"]

        def short_of_stairs(mx, my):
            """Точка пути к лестнице, не ближе STAIRS_KEEP клеток от неё.

            Считается по ТОМУ ЖЕ пути, которым пошёл бы ходок: берём путь до
            лестницы и отрезаем хвост. Так направление остаётся прежним (а
            лестница проверке нужна именно как направление), а наступить на
            неё становится нечем.
            """
            if stairs is None:
                return None
            path = bfs_path(level, (int(mx), int(my)), stairs)
            if not path:
                return None
            for c in reversed(path):
                if math.hypot(c[0] - stairs[0], c[1] - stairs[1]) >= STAIRS_KEEP:
                    return c
            return path[0]           # мы и так рядом — никуда не идём

        # Клетка лестницы для всех дорог проверки — стена: по ней не ходят.
        avoid = () if stairs is None else (stairs,)

        # Замер лестницы снимается ПО ДОРОГЕ, а разворот делается, когда
        # снято всё нужное, — ни то, ни другое не спрашивает, успела ли
        # машина добежать (см. measure_stairs и WALK_BUDGET).
        got = {}
        clk = {"probe": 0.0, "enough": 0.0, "last": False}

        def probe():
            if stairs is None or "ratio" in got:
                return
            if time.time() - clk["probe"] < 0.5:
                return
            clk["probe"] = time.time()
            m = measure_stairs(t, level, size, stairs, keys)
            if m:
                got.update(m)
                got["at"] = time.time()

        def enough():
            """Снято ли всё, ради чего идут: число лестницы и клетки у дома.

            Ответ кэшируется на секунду: считать его — это тащить весь
            туман из браузера, а звать его приходится в каждом обороте
            шага.
            """
            if "ratio" not in got:
                return False
            if time.time() - clk["enough"] < 1.0:
                return clk["last"]
            clk["enough"] = time.time()
            got["new_cells"] = new_in_view(f0, fog_of(t), c_start, size)
            clk["last"] = got["new_cells"] >= ENOUGH_NEW_CELLS
            return clk["last"]

        # ДОРОГА ТУДА. Лестница здесь — НАПРАВЛЕНИЕ, а не цель: сама по себе
        # она проверке не нужна, нужны два числа — отношение яркости
        # лестницы к полу (снимается по дороге, как только лестница попала в
        # кадр) и столько заново открытых клеток В КАДРЕ ИСХОДНОЙ ТОЧКИ,
        # чтобы падение черноты было заведомо выше порога. Поэтому дорога
        # кончается, когда снято и то, и другое, а не когда ходок дошёл.
        #
        # Замерено, зачем это нужно: приход к лестнице НЕ означает, что у
        # дома открылось достаточно. Пять прогонов подряд — 45, 192, 136,
        # 186, 141 клетки, падение черноты 4.6, 21.4, 14.9, 17.0, 14.4 п.п.
        # Тот самый прогон с 45 клетками — красный при пороге 5 п.п., и
        # ходок в нём ДОШЁЛ до лестницы (0.67 клетки). Мигало не «не успел
        # дойти», а «дошёл не туда, куда смотрит камера замера».
        if stairs is None:
            check(False, "на этаже есть лестница вниз (тайл 2)")
            reached, gap, secs, nudges = False, 0.0, 0.0, 0
        else:
            t_walk = time.time()
            reached, gap, secs, nudges = False, 0.0, 0.0, 0
            legs = []
            stairs_tried = False
            while time.time() - t_walk < WALK_BUDGET:
                if "ratio" not in got and not stairs_tried:
                    me = t.self_pos()
                    tgt = short_of_stairs(me["x"], me["y"])
                    why = "в сторону лестницы, не доходя %.0f клеток" % STAIRS_KEEP
                    if tgt is None:
                        stairs_tried = True
                        continue
                elif enough():
                    break
                else:
                    me = t.self_pos()
                    tgt = nearest_dark(level, fog_of(t), c_start, size,
                                       me["x"], me["y"])
                    why = "добор клеток у дома"
                    if tgt is None:
                        break          # у дома разведано всё, что доступно
                left = WALK_BUDGET - (time.time() - t_walk)
                # Нога «в сторону лестницы» кончается, как только число
                # лестницы снято: дальше идти незачем, а подходить к самой
                # лестнице — вредно (см. STAIRS_KEEP).
                stop = (lambda: ("ratio" in got) or enough()) \
                    if why.startswith("в сторону") else enough
                reached, gap, secs, n = walk_to(t, level, tgt, keys, left,
                                                probe, stop, avoid)
                nudges += n
                legs.append("%s %s%s" % (why, tgt, "" if reached else " (не дошёл)"))
                if why.startswith("в сторону"):
                    stairs_tried = True
            note("дорога туда", "лестница снята %s, заново открыто в кадре "
                 "исходной точки %s клетки (хватает %d), %.1f с, ног %d: %s"
                 % ("да" if "ratio" in got else "НЕТ",
                    got.get("new_cells", "?"), ENOUGH_NEW_CELLS,
                    time.time() - t_walk, len(legs), "; ".join(legs[:6])))
        time.sleep(0.6)
        me1 = t.self_pos()
        ran = math.hypot(me1["x"] - me0["x"], me1["y"] - me0["y"])
        note("пробежка туда", "от (%.1f, %.1f) до (%.1f, %.1f), по прямой %.1f "
                              "клетки, последняя нога %.1f с, до её цели %.2f "
                              "клетки, рывков поперёк %d"
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
        # Числа сняты ПО ДОРОГЕ (measure_stairs), а не после прихода: сюда
        # приход лестницы не входит вовсе. Если по дороге снять не удалось —
        # пробуем ещё раз отсюда, вдруг стоим прямо на ней.
        if stairs is not None and "ratio" not in got:
            m = measure_stairs(t, level, size, stairs, keys)
            if m:
                got.update(m)
        if stairs is None:
            pass                                   # уже покраснело выше
        elif "ratio" not in got:
            check(False, "лестница вниз хоть раз попала в кадр за пробежку",
                  "лестница на %s, ходок кончил дорогу в %.2f клетки от "
                  "последней цели" % (str(stairs), gap))
        else:
            note("лестница", "клетка %s, состояние тумана %d; эталон пола %s"
                 % (str(stairs), got["state"], str(got["ref_cell"])))
            check(got["ratio"] >= STAIRS_RATIO,
                  "лестница вниз не красится как обычный пол",
                  "самый яркий красный: лестница %d, пол рядом %d, "
                  "отношение %.1f (нужно >= %.1f)"
                  % (got["st"], got["ref"], got["ratio"], STAIRS_RATIO))

        # --- возврат в исходную точку -------------------------------------
        back_ok, back_gap, back_secs, back_nudges = walk_to(
            t, level, home, keys, WALK_BUDGET, avoid=avoid)
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

        # Третий пояс против мины с лестницей. Смена этажа обнуляет туман
        # (4.4) и меняет карту: замер «сколько черноты ушло в кадре исходной
        # точки» после неё не значит ничего, и молчать об этом нельзя.
        lvl_now = t.js("window.__zza.level()")
        me_now = t.self_pos()
        d_st = 1e9 if stairs is None else math.hypot(
            me_now["x"] - (stairs[0] + 0.5), me_now["y"] - (stairs[1] + 0.5))
        check(lvl_now["floor"] == floor0,
              "этаж под замером не сменился (ходок не наступил на лестницу)",
              "этаж %d, был %d; до лестницы сейчас %.2f клетки (тело на клетке "
              "лестницы при < %.3f)" % (lvl_now["floor"], floor0, d_st, 0.707))

        # --- ЧИСЛО 1: чернота после ---------------------------------------
        # Здоровье печатается рядом: враги на этаже уже есть, и в тот день,
        # когда у них появится ИИ, упавшее hp объяснит красный кадр быстрее,
        # чем его будут искать в тумане (красная кайма по краю кадра при
        # hp <= 50% — это 11% пикселей, и она не чёрная).
        hp_end = t.js("window.__zza.me()")
        note("здоровье вкладки на замере", "hp %s/%s, flags %s"
             % (hp_end["hp"], hp_end["hpMax"], hp_end["flags"]))
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
