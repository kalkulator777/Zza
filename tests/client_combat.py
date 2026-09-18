#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Бой на экране клиента (DESIGN.md 4.2, 4.3, 5.1, 5.2, 7.2).

Сервер умеет бой с этапа 2a. Эта проверка отвечает на один вопрос: ВИДНО ЛИ
его в кадре — и видно ли вовремя.

Меряются настоящие пиксели настоящей канвы (getImageData через
window.__zza), а не модель мира: «видно» — это то, что попало в кадр.
Браузер — Chromium из /opt/pw-browsers, сервер — настоящий play.py, вкладка
входит в игру кнопками и дерётся клавишами и мышью.

ЧТО ПРОВЕРЯЕТСЯ

  1. Кнопки боя доходят до сервера маской из 5.1: 1 ближняя атака, 2 рывок,
     8 дальняя атака. Проверяется не тем, что клиент «послал», а тем, что
     сервер ОТВЕТИЛ: поднял бит WINDUP, поднял бит DASH, родил снаряд.
  2. Замах виден в кадре: коробка вокруг бойца меняется, пока горит бит
     WINDUP, и меняется НЕ на один пиксель.
  3. Полоска здоровья отражает СНАПШОТ, а не событие. Приходит снапшот с
     упавшим hp и БЕЗ события `hit` — полоска всё равно верна; приходит
     событие `hit` без снапшота — полоска не шевелится (5.2 прямо
     запрещает держать состояние на событиях).
  4. Смерть видна: бит DEAD поднят — кадр изменился, на экране «ТЫ ДУХ».
  5. Стоимость кадра не выросла втрое против той, что была до этой работы.
     Сцена ОДНА И ТА ЖЕ, замеры спина к спине, рядом uptime, и меряется
     двумя мерками: без слива кадра (работа клиента) и со сливом (вместе с
     закраской).
  6. Замах виден РАНЬШЕ удара, и ровно на время замаха. Два числа с двух
     сторон: в тишине (пункт 2) замеряется, через сколько миллисекунд после
     нажатия кадр показал замах и на сколько ярких пикселей; в боевом мире
     — сколько тиков от прихода бита WINDUP до события `hit` по живому
     врагу. Второе обязано быть 8 тиков (0.267 с, 4.2) — это и есть время,
     которое даётся на реакцию.
  7. Проверки 2 и 6 КРАСНЕЮТ, если перестать рисовать замах (пункт 2a —
     сразу после пункта 2, в тех же условиях).
  8. Ноль ошибок в консоли.

  ПОРЯДОК ПУНКТОВ НЕ СЛУЧАЕН. Всё, что меряется по неподвижному кадру,
  снимается в начале, пока враги далеко; драка стоит последней, потому что
  подойти к врагу — значит встать под огонь, а мёртвый игрок не машет мечом.

ОТКУДА ПОРОГИ (выведены из величин, не подобраны под прогон)

  * «Кадр изменился» — хэш куска канвы (FNV-1a по каждому четвёртому
     пикселю) стал другим. Порога здесь нет и быть не может: кадр стоящего
     клиента ДЕТЕРМИНИРОВАН до пикселя (это проверяется тут же, двумя
     замерами подряд), значит любое изменение хэша — это рисунок, а не шум.
     Рядом печатается, на сколько пикселей и насколько ярче стало.
  * «Изменилось не на пиксель» — не меньше MIN_LIT_PX ярких пикселей.
     Сектор замаха при 48 px/клетка: дальность 1.2 клетки = 57.6 px, дуга
     110 градусов, площадь = 110/360 * pi * 57.6^2 = 3184 px. Берём
     десятикратный запас вниз — 300 пикселей: столько не наберётся ни от
     какого дрожания, но наберётся от любого честно нарисованного сектора.
  * «Полоска короче» — самый яркий красный в правой части полоски. Пустая
     часть красится #d16a6a (r=209), полная #7fd18a (r=127). Порог 170
     лежит ровно между ними, запас в обе стороны больше 1.2 раза.
  * Стоимость кадра — не больше чем втрое (порог задания). Абсолютные
     миллисекунды из контейнера с софтверным рендером (SwiftShader) НЕ
     являются числом целевой машины и за него не выдаются; осмысленно
     только отношение двух замеров, сделанных подряд одним рендером.

ЧТО ПОДАЁТСЯ ПРОВОДОМ, А ЧТО ПРОИСХОДИТ САМО (честно)

  Замах, рывок, выстрел и попадание — НАСТОЯЩИЕ: клавиша нажата, сервер
  ответил битом в flags и событием в журнале.

  Урон по игроку и его смерть подаются сообщением снапшота (5.2) в
  net._onMessage — тем же входом, куда приходит провод, как это уже сделано
  в client_fog.py для врага в тумане. Причина не в том, что игрока нечем
  ударить (враги на этаже стреляют и попадают), а в том, что проверка
  здоровья обязана отделить СОСТОЯНИЕ от СОБЫТИЯ: нужен снапшот с точно
  известным hp и заведомо БЕЗ события hit, и наоборот. Настоящий враг
  такого не обещает. Проверяется ровно то, за что отвечает клиент: пришло
  hp в снапшоте — нарисуй полоску; пришёл бит DEAD — нарисуй духа.

  Побочное следствие живых врагов: они ходят и стреляют, и кадр рядом с
  игроком не обязан стоять. Поэтому каждый замер пиксельной части сперва
  доказывает, что сцена СТОЯЛА (см. settle и analyse), и повторяется, если
  не стояла. Молча подогнанных чисел здесь нет.

Запуск:
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/client_combat.py
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
                           note, summary, uptime, walk_to)

# --- размеры и пороги -----------------------------------------------------

VIEW_W, VIEW_H = 1920, 1080    # 2.1a: целевое разрешение контракта
TILE_PX = 48                   # 4.1: базовый масштаб рендера

# Коробка вокруг бойца, в которой ищется замах. Дальность удара 1.2 клетки
# (4.2) плюс кольцо вокруг тела; берём 2 клетки в каждую сторону — сектор
# влезает целиком с запасом, а посторонние сущности в такую коробку попасть
# почти не могут (ближайший враг стоит дальше).
BOX_TILES = 2.0

# Сколько ярких пикселей обязано прибавиться на замахе. Площадь сектора при
# 48 px/клетка = 110/360 * pi * (1.2*48)^2 = 3184 пикселя; порог взят с
# десятикратным запасом вниз.
MIN_LIT_PX = 300
LIT_T = 90                     # «яркий пиксель»: максимальный канал > 90.
#                                Пол подземелья #23293a даёт 58, стена
#                                #454d63 — 99, кромка замаха #ffd86b — 255.
#                                Порог 90 отделяет замах от пола, но не от
#                                стены: поэтому считается РАЗНОСТЬ двух
#                                кадров одной и той же коробки, а не
#                                абсолютное число.

HP_RED_T = 170                 # «пустая часть полоски»: r между 127 и 209
# Сколько ярких пикселей обязана дать вспышка попадания. Кольцо вспышки в
# самом начале жизни: радиус (0.20 + 0) * (1 + 35/40) * 48 = 18 px, толщина
# max(2, 48*0.09) = 4.3 px, то есть 2*pi*18*4.3 = 486 пикселей, и это
# НИЖНЯЯ оценка — дальше кольцо растёт. Порог взят вдвое ниже.
MIN_FX_PX = 200
MS_RATIO_MAX = 3.0             # «не выросла втрое»
BENCH_DRAWS = 300              # вызовов draw() в одной пачке замера
BENCH_REPS = 5

WATCH_FRAMES = 200             # кадров в записи одного удара (~3.3 с)
TRACK_SNAPS = 120              # снапшотов в записи одного удара (4 с)

# Предохранители от зависания, а не пороги. Настоящее условие остановки —
# «нужное снято» (см. walk_to и wait_for). Числа: дорога до врага на этаже
# 64x48 при беге 5 кл/с (4.2) — 16 с чистого хода, десятикратный запас.
WALK_BUDGET = 160.0

# flags (4.3) и виды событий (5.2)
F_DEAD, F_OFFLINE, F_WINDUP, F_DASH = 1, 2, 4, 8
FX_HIT, FX_DIE, FX_SHOT, FX_BOOM = 1, 2, 3, 4
K_PLAYER, K_ENEMY, K_SHOT = 1, 2, 4
# 4.2: виды врага различаются ЗНАЧЕНИЕМ kind, а не битом в flags. Стрелок
# уехал на пятое значение, и искать врага по одному kind == 2 нельзя: цель
# для удара нашлась бы только среди рубак, а строка «врагов в мире» врала бы
# в меньшую сторону ровно на число стрелков.
K_ENEMY_RANGED = 5
ENEMY_KINDS = (K_ENEMY, K_ENEMY_RANGED)
# 5.1: маска кнопок. Дальний бой — бит 8, предмет — 16.
BTN_ATTACK, BTN_DASH, BTN_USE, BTN_SHOOT, BTN_ITEM = 1, 2, 4, 8, 16

MELEE_WINDUP_TICKS = 8         # 4.2: замах ближней атаки
DASH_TICKS = 5                 # 4.2: рывок
TICK_MS = 1000.0 / 30


# --- мелкая обвязка -------------------------------------------------------

def px(tab, x, y, w, h, thresh=LIT_T):
    return tab.js("window.__zza.pixels(%d,%d,%d,%d,%d)"
                  % (round(x), round(y), round(w), round(h), thresh))


def me(tab):
    return tab.js("window.__zza.me()")


def wire(tab, msg):
    """Подать сообщение тем же путём, каким его подаёт провод (net._onMessage)."""
    return tab.js("window.__zza.wire(%s)" % json.dumps(json.dumps(msg)))


def snap_msg(tick, ents, rm=None):
    """Снапшот 5.2. Сущность — массив из десяти полей ровно в порядке 4.3."""
    m = {"t": "snap", "ack": 0, "tick": tick, "full": False, "e": ents}
    if rm:
        m["rm"] = rm
    return m


def ent_row(eid, kind, x, y, vx=0.0, vy=0.0, hp=100, hp_max=100,
            flags=0, facing=0.0):
    return [eid, kind, round(x, 3), round(y, 3), round(vx, 2), round(vy, 2),
            int(hp), int(hp_max), int(flags), round(facing, 2)]


def self_box(tab):
    """Коробка вокруг своей сущности в экранных пикселях."""
    m = me(tab)
    sx, sy = tab.js("window.__zza.worldToScreen(%f,%f)" % (m["x"], m["y"]))
    half = BOX_TILES * TILE_PX
    return (sx - half, sy - half, half * 2, half * 2)


def settle(tab, box, tries=12):
    """Дождаться неподвижного кадра и вернуть его хэш.

    Замер «кадр изменился» имеет смысл только если кадр до этого стоял:
    иначе меряется не рисунок, а доезжающая интерполяция (6.1).
    """
    prev = None
    for _ in range(tries):
        time.sleep(0.25)
        a = px(tab, *box)
        if prev is not None and a["hash"] == prev["hash"]:
            return a
        prev = a
    return prev


def first_change(frames, h0):
    for i, f in enumerate(frames):
        if f["h"] != h0:
            return i, f
    return -1, None


def windup_windows(tracks):
    """Все окна замаха в записи снапшотов: [(тик подъёма, тик удара), ...].

    Тик удара — тот, на котором сервер СНЯЛ бит WINDUP: он снимает его
    ровно в _land_melee, то есть в момент удара.
    """
    out = []
    rise = None
    for r in tracks:
        if r["flags"] < 0:
            continue
        if r["flags"] & F_WINDUP:
            if rise is None:
                rise = r["tick"]
        elif rise is not None:
            out.append((rise, r["tick"]))
            rise = None
    return out


def windup_window(tracks):
    """(тик подъёма WINDUP, тик его снятия) по записи снапшотов."""
    rise = fall = None
    for r in tracks:
        if r["flags"] < 0:
            continue
        if rise is None:
            if r["flags"] & F_WINDUP:
                rise = r["tick"]
        elif not (r["flags"] & F_WINDUP):
            fall = r["tick"]
            break
    return rise, fall


def analyse(s):
    """Разбор одного удара: когда кадр изменился и когда пришёл удар.

    Главная тонкость — ДОКАЗАТЬ, что кадр изменился ИМЕННО от замаха, а не
    от постороннего движения. На этаже живут враги, они ходят и стреляют, и
    «первое изменение после нажатия» само по себе не значит ничего.

    Доказательство прямое: кадр, нарисованный ДО прихода снапшота с битом
    WINDUP, замаха содержать не может по построению. Если все такие кадры
    совпали с исходным до пикселя, сцена стояла, и первое изменение после
    этой границы — это замах и ничто иное. Если не совпали — сцена шевелилась
    (мимо пролетел снаряд, подошёл враг), и замер этого удара не годится;
    так и сказано, а не подогнано.
    """
    rise, fall = windup_window(s["tracks"])
    out = {"rise": rise, "fall": fall, "still": False, "f": None,
           "before": 0, "after": 0}
    if rise is None:
        return out
    h0 = s["base"]["hash"]
    before = [f for f in s["frames"] if f["lt"] < rise]
    after = [f for f in s["frames"] if f["lt"] >= rise]
    out["before"] = len(before)
    out["after"] = len(after)
    out["still"] = all(f["h"] == h0 for f in before)
    for f in after:
        if f["h"] != h0:
            out["f"] = f
            break
    return out


def approach(tab, level, foe_id, keys, want=1.4, tries=6):
    """Подойти к врагу на дистанцию удара. Враг ходит — цель перечитывается.

    Условие остановки — РАССТОЯНИЕ, а не время: это то самое число, ради
    которого идут. Потолок по попыткам — предохранитель.
    """
    for _ in range(tries):
        foe = None
        for o in tab.js("window.__zza.others()"):
            if o["id"] == foe_id:
                foe = o
        if foe is None:
            return None
        m = me(tab)
        d = math.hypot(foe["x"] - m["x"], foe["y"] - m["y"])
        if d <= want:
            keys.release()
            return d
        walk_to(tab, level, (int(foe["x"]), int(foe["y"])), keys,
                WALK_BUDGET / 4, near=1.0)
        keys.release()
        time.sleep(0.2)
    m = me(tab)
    foe = None
    for o in tab.js("window.__zza.others()"):
        if o["id"] == foe_id:
            foe = o
    return None if foe is None else math.hypot(foe["x"] - m["x"],
                                               foe["y"] - m["y"])


def resync(tab, keys, budget=6.0):
    """Вернуть клиенту ПРАВДУ сервера после поданных проводом снапшотов.

    Тонкость дельты (5.2): сервер не шлёт сущность, которая не менялась.
    Значит поданное проверкой hp останется на экране сколь угодно долго —
    и проверка дальше будет судить по числу, которое сама же и написала.
    Лечение простое: шевельнуться. Сущность изменилась — сервер пришлёт её
    целиком, и клиент снова видит мир, а не нашу выдумку.

    Момент «правда пришла» ловится меткой: подаём заведомо невозможный
    hp_max (сервер всегда шлёт 100), и ждём, пока он не станет обычным.
    """
    m = me(tab)
    tab.js("window.__zza.wire(%s)" % json.dumps(json.dumps(snap_msg(
        tab.js("window.__zza.tick()"),
        [ent_row(m["id"], K_PLAYER, m["x"], m["y"], hp=m["hp"], hp_max=101,
                 flags=m["flags"], facing=m["facing"])]))))
    t0 = time.time()
    while time.time() - t0 < budget:
        keys.set(["KeyD"])
        time.sleep(0.15)
        keys.set(["KeyA"])
        time.sleep(0.15)
        if me(tab)["hpMax"] != 101:
            keys.release()
            return True, time.time() - t0
    keys.release()
    return False, time.time() - t0


def swing(tab, box, hold=0.10, key="KeyF", want_flag=True):
    """Один удар со снятием всего сразу. Возвращает всё снятое.

    Снимается двумя мерками, и обе нужны:

    * ТОЧНАЯ, но хрупкая: запись кадров браузером (хэш куска канвы на каждый
      кадр). Даёт номер кадра и миллисекунду, но годится только если сцена
      перед замахом стояла — иначе меряет чужое движение;
    * ГРУБАЯ, но железная: пока горит бит WINDUP, читаются пиксели коробки.
      Прибавка в три тысячи ярких пикселей от чужого шага не появляется, а
      момент чтения — это заведомо НЕ РАНЬШЕ, чем кадр изменился. Для
      утверждения «замах видно раньше удара» верхняя оценка и нужна.
    """
    base = settle(tab, box)
    tab.js("window.__zza.watch(%d,%d,%d,%d,%d)"
           % (round(box[0]), round(box[1]), round(box[2]), round(box[3]),
              WATCH_FRAMES))
    tab.js("window.__zza.track(%d)" % TRACK_SNAPS)
    t_press = tab.js("performance.now()")
    tab.page.keyboard.down(key)
    during = None
    t_seen = None
    if want_flag:
        try:
            tab.page.wait_for_function(
                "window.__zza.me() && (window.__zza.me().flags & 4) !== 0",
                timeout=3000)
            during = px(tab, *box)
            t_seen = tab.js("performance.now()")
        except Exception:
            pass
    time.sleep(hold)
    tab.page.keyboard.up(key)
    # Ждём, пока запись кадров кончится сама (или предохранитель 6 с).
    t0 = time.time()
    while time.time() - t0 < 6.0:
        time.sleep(0.2)
        if not tab.js("window.__zza.watching()"):
            break
    frames = tab.js("window.__zza.watched()")
    tracks = tab.js("window.__zza.tracks()")
    lit0 = base["n"] - base["dark"]
    return {"base": base, "frames": frames, "tracks": tracks,
            "press": t_press, "box": box, "during": during, "seen": t_seen,
            "gain": (during["n"] - during["dark"] - lit0)
                    if during is not None else None}


def quality(a):
    """Насколько годен разбор удара: сцена стояла, кадр снят, hit пришёл."""
    return ((1 if a.get("still") else 0) + (1 if a.get("f") else 0) +
            (1 if a.get("hit") else 0))


def bench(tab, reps=BENCH_REPS, n=BENCH_DRAWS):
    """Стоимость одного draw() на текущей сцене, медиана по reps пачкам.

    Пачкой, а не одиночным кадром: performance.now() в браузере огрублён до
    0.1 мс, а весь кадр стоит меньше кванта — на одиночных кадрах любое
    отношение вырождается в 0.1/0.0.
    """
    out = []
    for _ in range(reps):
        out.append(tab.js("window.__zza.benchDraw(%d)" % n))
        time.sleep(0.05)
    return statistics.median(out), out


def nearest_enemy(tab):
    m = me(tab)
    best = None
    for o in tab.js("window.__zza.others()"):
        if o["kind"] not in ENEMY_KINDS or (o["flags"] & F_DEAD):
            continue
        d = math.hypot(o["x"] - m["x"], o["y"] - m["y"])
        if best is None or d < best[0]:
            best = (d, o)
    return best


def aim_at(tab, wx, wy):
    """Навести мышь на точку мира — ровно так же, как это делает человек."""
    sx, sy = tab.js("window.__zza.worldToScreen(%f,%f)" % (wx, wy))
    sx = max(1, min(VIEW_W - 2, sx))
    sy = max(1, min(VIEW_H - 2, sy))
    tab.page.mouse.move(sx, sy)
    time.sleep(0.25)
    return sx, sy


# --- прогон ---------------------------------------------------------------

def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("нет пакета playwright: pip install playwright "
              "(БЕЗ playwright install — браузер лежит в /opt/pw-browsers)")
        return 2

    print("=== бой в кадре клиента (DESIGN.md 4.2, 4.3, 5.1, 5.2, 7.2) ===")
    print("  разрешение вкладки %dx%d, %d px/клетка — как в 4.1"
          % (VIEW_W, VIEW_H, TILE_PX))
    uptime()

    # ДВА СЕРВЕРА, и это не роскошь. Всё, что меряется по пикселям
    # неподвижного кадра, требует тишины: живой враг ходит, стреляет и в
    # конце концов убивает одинокую вкладку, а мёртвый игрок не машет мечом
    # (8.5) — замеренным оказывается не клиент, а живучесть. Поэтому тихий
    # мир (ZZA_ENEMIES=0, выключатель для стендов в server/ai.py) для
    # пиксельных пунктов и ОТДЕЛЬНЫЙ боевой мир для единственного пункта,
    # которому враг действительно нужен: настоящего события hit.
    with Server(env={"ZZA_ENEMIES": "0"}) as srv, Server() as war, \
            sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM, headless=True,
                                     args=BROWSER_ARGS)
        t = Tab(browser, "A", width=VIEW_W, height=VIEW_H).open(srv.url)
        room = t.create_room("Бой")
        t.ready()
        t.wait_game()
        t.page.wait_for_function(
            "window.__zza.fog() && window.__zza.fog().msgs > 0", timeout=10000)
        time.sleep(0.8)
        keys = Keys(t)
        level = t.js("window.__zza.level()")
        m0 = me(t)
        print("  комната %s, этаж %d, своя сущность id %d, hp %d/%d"
              % (room, level["floor"], m0["id"], m0["hp"], m0["hpMax"]))

        # ============ 1. КНОПКИ БОЯ ДОХОДЯТ ДО СЕРВЕРА (5.1) =============
        # Проверяется не «клиент послал», а «сервер ответил»: бит в flags и
        # родившийся снаряд — это ответ сервера на конкретный бит маски.
        print()
        print("  --- 1. кнопки боя, маска 5.1 -------------------------------")

        aim_at(t, m0["x"] + 3.0, m0["y"])     # прицел в сторону, facing замрёт
        box = self_box(t)

        t.js("window.__zza.track(%d)" % TRACK_SNAPS)
        t.page.keyboard.down("KeyF")
        time.sleep(0.12)
        t.page.keyboard.up("KeyF")
        time.sleep(1.0)
        tr = t.js("window.__zza.tracks()")
        w_ticks = [r["tick"] for r in tr if r["flags"] & F_WINDUP]
        check(len(w_ticks) > 0,
              "бит 1 (ближняя атака) дошёл: сервер поднял WINDUP в flags",
              "бит WINDUP (4) виден в снапшотах на тиках %s — это %d тиков "
              "подряд при %d по 4.2"
              % (("%d..%d" % (min(w_ticks), max(w_ticks))) if w_ticks else "нет",
                 (max(w_ticks) - min(w_ticks) + 1) if w_ticks else 0,
                 MELEE_WINDUP_TICKS))

        t.js("window.__zza.track(%d)" % TRACK_SNAPS)
        t.page.keyboard.down("Space")
        time.sleep(0.12)
        t.page.keyboard.up("Space")
        time.sleep(1.0)
        tr = t.js("window.__zza.tracks()")
        d_ticks = [r["tick"] for r in tr if r["flags"] & F_DASH]
        check(len(d_ticks) > 0,
              "бит 2 (рывок) дошёл: сервер поднял DASH в flags",
              "бит DASH (8) виден на тиках %s — %d тиков при %d по 4.2"
              % (("%d..%d" % (min(d_ticks), max(d_ticks))) if d_ticks else "нет",
                 (max(d_ticks) - min(d_ticks) + 1) if d_ticks else 0, DASH_TICKS))

        # Дальняя атака — бит 8. Ловим по событию shot (5.2): снаряд летит
        # 18 кл/с и в кадре опроса из питона может уже не существовать, а
        # событие о его рождении остаётся в журнале.
        n_ev0 = len([e for e in t.js("window.__zza.evLog()") if e["k"] == FX_SHOT])
        t.page.keyboard.down("KeyR")
        time.sleep(0.12)
        t.page.keyboard.up("KeyR")
        time.sleep(0.5)
        evs = t.js("window.__zza.evLog()")
        shots = [e for e in evs if e["k"] == FX_SHOT]
        check(len(shots) > n_ev0,
              "бит 8 (дальняя атака) дошёл: сервер родил снаряд",
              "событий shot в журнале было %d, стало %d; последнее: тик %d, "
              "стрелок id %d, снаряд id %d"
              % (n_ev0, len(shots), shots[-1]["tick"] if shots else -1,
                 shots[-1]["a"] if shots else -1, shots[-1]["b"] if shots else -1))
        booms = [e for e in evs if e["k"] == FX_BOOM]
        note("события боя за этот отрезок",
             "shot %d, boom (искра о стену) %d, hit %d, die %d"
             % (len(shots), len(booms),
                len([e for e in evs if e["k"] == FX_HIT]),
                len([e for e in evs if e["k"] == FX_DIE])))

        # ============ 2. ЗАМАХ ВИДЕН В КАДРЕ =============================
        print()
        print("  --- 2. замах виден в кадре ---------------------------------")
        # Вспышки событий на время этого замера выключены. Это не подгонка:
        # проверяется ЗАМАХ, а чужой снаряд, разорвавшийся рядом, добавляет в
        # ту же коробку свои яркие пиксели и врёт в обе стороны — может
        # сделать зелёным клиент, который замах не рисует. Вспышки
        # проверяются отдельно, в пункте 3.
        t.js("window.__zza.setFx(false)")
        time.sleep(0.6)
        box = self_box(t)
        base = settle(t, box)
        base2 = px(t, *box)
        check(base["hash"] == base2["hash"],
              "кадр стоящего клиента детерминирован — шум замера равен нулю",
              "два замера коробки %dx%d подряд: хэш %d и %d, ярких пикселей "
              "%d и %d" % (round(box[2]), round(box[3]), base["hash"],
                           base2["hash"], base["n"] - base["dark"],
                           base2["n"] - base2["dark"]))

        # Кадр ИМЕННО на замахе: жмём атаку и, пока горит бит WINDUP,
        # читаем пиксели. Ждём БИТА, а не секунд.
        sw2 = swing(t, box)
        lit_base = sw2["base"]["n"] - sw2["base"]["dark"]
        lit_during = (sw2["during"]["n"] - sw2["during"]["dark"]) \
            if sw2["during"] is not None else lit_base
        a2 = analyse(sw2)
        note("коробка замаха", "%dx%d пикселей вокруг бойца, порог яркости %d"
             % (round(box[2]), round(box[3]), LIT_T))
        check(sw2["during"] is not None and lit_during - lit_base >= MIN_LIT_PX,
              "на замахе кадр изменился, и не на один пиксель",
              "ярких пикселей %d -> %d, прибавка %d (нужно >= %d; площадь "
              "сектора 110 градусов радиусом 1.2 клетки = 3184 пикселя), "
              "самый яркий канал %d -> %d, средняя яркость %.1f -> %.1f"
              % (lit_base, lit_during, lit_during - lit_base, MIN_LIT_PX,
                 sw2["base"]["maxCh"],
                 sw2["during"]["maxCh"] if sw2["during"] else -1,
                 sw2["base"]["mean"],
                 sw2["during"]["mean"] if sw2["during"] else -1))
        if a2["f"] is not None and a2["still"] and a2["rise"] is not None:
            note("когда именно изменился кадр",
                 "через %.0f мс после нажатия — это первый же кадр, "
                 "нарисованный по снапшоту тика %d, а WINDUP поднялся на "
                 "тике %d. Кадров до него было %d, и все совпали с исходным "
                 "до пикселя"
                 % (a2["f"]["now"] - sw2["press"], a2["f"]["lt"], a2["rise"],
                    a2["before"]))
        time.sleep(0.8)

        # ============ 2a. ПРОВЕРКА УМЕЕТ КРАСНЕТЬ ========================
        print()
        print("  --- 2a. умеет ли краснеть ----------------------------------")
        # Тот же удар, но рендеру запрещено рисовать замах. Проверка 2
        # обязана перестать видеть прибавку ярких пикселей, а проверка 6 —
        # изменение кадра в окне замаха. Делается ЗДЕСЬ, сразу после пункта
        # 2: те же условия, тот же неподвижный кадр, тот же боец. Красный
        # на подсаженной поломке имеет смысл только рядом с зелёным на
        # исправном коде, а не через полминуты и три сцены.
        t.js("window.__zza.setWindup(false)")
        time.sleep(0.5)
        sw_off = swing(t, self_box(t))
        gain_off = sw_off["gain"] if sw_off["gain"] is not None else -1
        check(sw_off["gain"] is not None and gain_off < MIN_LIT_PX,
              "без отрисовки замаха проверка 2 КРАСНЕЕТ",
              "прибавка ярких пикселей на замахе %d < %d — то есть проверка "
              "такой клиент НЕ пропускает (с отрисовкой прибавка была %d)"
              % (gain_off, MIN_LIT_PX, lit_during - lit_base))
        # Вторая половина: кадр в окне замаха не меняется вовсе. Первый удар
        # уже записан выше — разбираем его, и только если сцена не стояла,
        # бьём ещё: «сцена стояла» надо доказать, а не предположить.
        a_off, tries_off = None, 0
        for i in range(5):
            tries_off = i + 1
            a = analyse(sw_off if i == 0 else swing(t, self_box(t)))
            a["seen"] = (a["f"] is not None and a["fall"] is not None and
                         a["f"]["lt"] < a["fall"])
            if a_off is None or (a["still"] and a["rise"] is not None):
                a_off = a
            if a["still"] and a["rise"] is not None and not a["seen"]:
                a_off = a
                break
            time.sleep(0.7)
        check(a_off["still"] and a_off["rise"] is not None and not a_off["seen"],
              "без отрисовки замаха проверка 6 КРАСНЕЕТ",
              "WINDUP горел с тика %s по %s, сцена перед ним стояла (%s), а "
              "кадр в окне замаха не изменился вовсе (%s); попыток %d — рядом "
              "ходят живые враги, и чужой снаряд в коробке это тоже изменение "
              "кадра, поэтому ищется чистый удар, а не первый попавшийся"
              % (str(a_off["rise"]),
                 str((a_off["fall"] - 1) if a_off["fall"] else None),
                 "да" if a_off["still"] else "НЕТ",
                 ("изменился по снапшоту тика %d" % a_off["f"]["lt"])
                 if a_off["f"] else "изменений нет", tries_off))
        t.js("window.__zza.setWindup(true)")
        t.js("window.__zza.setFx(true)")
        time.sleep(0.3)
        check(t.js("window.__zza.getWindup()") is True and
              t.js("window.__zza.getCombat()") is True and
              t.js("window.__zza.getFx()") is True,
              "отрисовка боя возвращена во включённое состояние")

        # ============ 3. ПОЛОСКА ЗДОРОВЬЯ — ОТ СОСТОЯНИЯ =================
        # Делается РАНЬШЕ похода к врагу намеренно: враги на этаже ходят и
        # стреляют, а здесь меряются пиксели неподвижного кадра.
        print()
        print("  --- 3. полоска здоровья от снапшота, а не от события -------")
        keys.release()
        time.sleep(0.5)
        bar = t.js("window.__zza.hpBar()")
        # Две пробы: в четверти полоски и в 85% её длины. Полная полоска
        # зелёная (#7fd18a, r=127) на обеих, короткая — красная (#d16a6a,
        # r=209) на правой.
        q = max(6, int(bar["h"] * 0.5))
        left = (bar["x"] + bar["w"] * 0.25 - q, bar["y"] + 3, q * 2, bar["h"] - 6)
        right = (bar["x"] + bar["w"] * 0.85 - q, bar["y"] + 3, q * 2, bar["h"] - 6)
        note("полоска своего здоровья",
             "прямоугольник %dx%d в точке (%d,%d); пробы по %dx%d в 25%% и "
             "85%% длины" % (round(bar["w"]), round(bar["h"]), round(bar["x"]),
                             round(bar["y"]), round(left[2]), round(left[3])))

        # Попыток три: враги уже стреляют, и настоящий урон в неудачный
        # момент перебьёт поданное hp своим снапшотом. Это не подгонка под
        # прогон — это чужая пуля в кадре замера.
        hurt = 60
        got = None
        for attempt in range(3):
            m1 = me(t)
            if m1["hp"] != m1["hpMax"]:
                # уже ранены по-настоящему: подадим полное здоровье, чтобы
                # «до» и «после» отличались только нашим числом
                wire(t, snap_msg(t.js("window.__zza.tick()"),
                                 [ent_row(m1["id"], K_PLAYER, m1["x"], m1["y"],
                                          hp=m1["hpMax"], hp_max=m1["hpMax"],
                                          flags=m1["flags"] & ~F_DEAD,
                                          facing=m1["facing"])]))
                time.sleep(0.3)
                m1 = me(t)
            full_l, full_r = px(t, *left), px(t, *right)
            ev_before = t.js("window.__zza.evs()")
            wire(t, snap_msg(t.js("window.__zza.tick()"),
                             [ent_row(m1["id"], K_PLAYER, m1["x"], m1["y"],
                                      hp=hurt, hp_max=m1["hpMax"], flags=0,
                                      facing=m1["facing"])]))
            time.sleep(0.35)
            hurt_l, hurt_r = px(t, *left), px(t, *right)
            m2 = me(t)
            ev_after = t.js("window.__zza.evs()")
            got = {"m1": m1, "m2": m2, "fl": full_l, "fr": full_r,
                   "hl": hurt_l, "hr": hurt_r, "e0": ev_before, "e1": ev_after,
                   "try": attempt + 1}
            if m2["hp"] == hurt and m1["hp"] == m1["hpMax"]:
                break
        note("hp %d/%d — полоска полная" % (got["m1"]["hp"], got["m1"]["hpMax"]),
             "самый яркий красный: слева %d, справа %d (зелёная часть #7fd18a "
             "даёт 127); попытка %d"
             % (got["fl"]["maxR"], got["fr"]["maxR"], got["try"]))
        check(got["m2"]["hp"] == hurt and got["hr"]["maxR"] >= HP_RED_T
              and got["hl"]["maxR"] < HP_RED_T,
              "полоска показывает урон из СНАПШОТА, события hit не было",
              "hp %d -> %d; самый яркий красный справа %d -> %d (порог %d, "
              "пустая часть #d16a6a даёт 209), слева %d -> %d (осталось "
              "зелёным). Событий за это время пришло %d"
              % (got["m1"]["hp"], got["m2"]["hp"], got["fr"]["maxR"],
                 got["hr"]["maxR"], HP_RED_T, got["fl"]["maxR"],
                 got["hl"]["maxR"], got["e1"] - got["e0"]))

        # Теперь наоборот: приходит событие hit и НИЧЕГО больше. Полоска не
        # имеет права шевельнуться — 5.2 запрещает держать состояние на ev.
        m1 = got["m1"]
        before = px(t, *right)
        hp_before = me(t)["hp"]
        fx_box = self_box(t)
        fx_before = px(t, *fx_box)
        t.js("window.__zza.wire(%s)" % json.dumps(json.dumps(
            {"t": "ev", "tick": t.js("window.__zza.tick()"), "k": "hit",
             "a": 900001, "b": m1["id"], "dmg": 35,
             "x": m1["x"], "y": m1["y"]})))
        # Вспышка живёт 0.3 с — её ловим сразу, полоску можно и потом.
        time.sleep(0.08)
        fx_after = px(t, *fx_box)
        time.sleep(0.35)
        after = px(t, *right)
        m3 = me(t)
        check(m3["hp"] == hp_before and after["maxR"] == before["maxR"],
              "событие hit БЕЗ снапшота полоску не двигает (5.2)",
              "пришло hit на 35 урона: hp в мире %d (было %d), самый яркий "
              "красный в пробе %d -> %d — полоска не шевельнулась"
              % (m3["hp"], hp_before, before["maxR"], after["maxR"]))
        # То же самое событие обязано дать ВСПЫШКУ: событие — это ровно то,
        # чего нет в снапшоте, и рисовать его больше нечем.
        fx_gain = (fx_after["n"] - fx_after["dark"]) - \
                  (fx_before["n"] - fx_before["dark"])
        check(fx_after["hash"] != fx_before["hash"] and fx_gain >= MIN_FX_PX,
              "то же событие hit дало вспышку в кадре",
              "коробка %dx%d вокруг бойца: ярких пикселей %d -> %d, прибавка "
              "%d (нужно >= %d: кольцо вспышки в самом начале жизни это 486 "
              "пикселей), хэш %d -> %d. Состояния на этой вспышке нет — она "
              "гаснет за 0.3 с, и полоска выше это подтвердила"
              % (round(fx_box[2]), round(fx_box[3]),
                 fx_before["n"] - fx_before["dark"],
                 fx_after["n"] - fx_after["dark"], fx_gain, MIN_FX_PX,
                 fx_before["hash"], fx_after["hash"]))

        # ============ 4. СМЕРТЬ ВИДНА ====================================
        print()
        print("  --- 4. смерть видна ----------------------------------------")
        # Коробка берётся ВЫШЕ своего бойца: там лежит надпись «ТЫ ДУХ», но
        # нет ни тела, ни полоски над головой — иначе замер ловил бы не
        # смерть, а то, что у живого над головой изменилась полоска.
        centre = (VIEW_W * 0.5 - 300, VIEW_H * 0.5 - 100, 600, 60)
        d = None
        for attempt in range(3):
            m1 = me(t)
            alive_box = px(t, *centre)
            wire(t, snap_msg(t.js("window.__zza.tick()"),
                             [ent_row(m1["id"], K_PLAYER, m1["x"], m1["y"],
                                      hp=0, hp_max=m1["hpMax"], flags=F_DEAD,
                                      facing=m1["facing"])]))
            time.sleep(0.45)
            dead_box = px(t, *centre)
            ghost_bar = px(t, *right)
            m4 = me(t)
            # Возвращаем РОВНО то состояние, что было: иначе кадр не обязан
            # совпасть с исходным, и сравнение ничего не значит.
            wire(t, snap_msg(t.js("window.__zza.tick()"),
                             [ent_row(m1["id"], K_PLAYER, m1["x"], m1["y"],
                                      hp=m1["hp"], hp_max=m1["hpMax"],
                                      flags=m1["flags"] & ~F_DEAD,
                                      facing=m1["facing"])]))
            time.sleep(0.45)
            back = px(t, *centre)
            d = {"m1": m1, "m4": m4, "a": alive_box, "d": dead_box,
                 "b": back, "g": ghost_bar, "try": attempt + 1}
            if (m4["flags"] & F_DEAD) and dead_box["hash"] != alive_box["hash"] \
                    and back["hash"] == alive_box["hash"]:
                break
        check((d["m4"]["flags"] & F_DEAD) != 0 and
              d["d"]["hash"] != d["a"]["hash"] and
              d["d"]["mean"] > d["a"]["mean"] + 5,
              "свой персонаж стал духом — кадр изменился, на экране «ТЫ ДУХ»",
              "flags %d (бит DEAD = 1); коробка %dx%d над бойцом: ярких "
              "пикселей %d -> %d, средняя яркость %.1f -> %.1f, самый яркий "
              "канал %d -> %d; попытка %d"
              % (d["m4"]["flags"], round(centre[2]), round(centre[3]),
                 d["a"]["n"] - d["a"]["dark"], d["d"]["n"] - d["d"]["dark"],
                 d["a"]["mean"], d["d"]["mean"],
                 d["a"]["maxCh"], d["d"]["maxCh"], d["try"]))
        note("полоска здоровья у духа",
             "самый яркий красный в пробе %d (у раненого живого было %d): "
             "полоска убрана, на её месте надпись по центру кадра"
             % (d["g"]["maxR"], got["hr"]["maxR"]))
        check(d["b"]["hash"] == d["a"]["hash"],
              "живой кадр вернулся тем же, каким был до смерти",
              "хэш коробки %d -> %d -> %d (первый и третий совпали) — значит "
              "разница была ровно в смерти, а не в постороннем движении"
              % (d["a"]["hash"], d["d"]["hash"], d["b"]["hash"]))

        # Клиенту пора вернуть правду сервера: всё, что подано выше, —
        # наша выдумка, а дельта её не перебьёт, пока сущность не изменится.
        ok_sync, sync_s = resync(t, keys)
        m_real = me(t)
        check(ok_sync,
              "клиент пересинхронизирован с сервером после поданных снапшотов",
              "заняло %.1f с; настоящее состояние с сервера: hp %d/%d, flags "
              "%d. Без этого дальше проверка судила бы по числу, которое сама "
              "и написала" % (sync_s, m_real["hp"], m_real["hpMax"],
                              m_real["flags"]))

        # ============ 5. СТОИМОСТЬ КАДРА =================================
        print()
        print("  --- 5. стоимость кадра: до и после, одна сцена -------------")
        # Сцена строится ОДИН РАЗ и не меняется между замерами: вокруг игрока
        # восемь врагов (двое на замахе, двое в рывке, четверо раненые),
        # шесть снарядов в воздухе и дюжина вспышек. Это заметно гуще, чем
        # бывает в комнате, — замер нарочно недобрый. Половина врагов —
        # СТРЕЛКИ (4.2): их тело рисуется наконечником с обводкой, то есть
        # на один вызов дороже кружка рубаки, и сцена без них занижала бы
        # стоимость кадра ровно на эту разницу.
        m5 = me(t)
        ents = [ent_row(m5["id"], K_PLAYER, m5["x"], m5["y"], hp=70,
                        hp_max=100, flags=F_WINDUP, facing=m5["facing"])]
        rm = []
        for i in range(8):
            a = i * math.pi / 4
            eid = 910000 + i
            rm.append(eid)
            fl = F_WINDUP if i < 2 else (F_DASH if i < 4 else 0)
            ek = K_ENEMY if (i % 2) == 0 else K_ENEMY_RANGED
            ents.append(ent_row(eid, ek, m5["x"] + math.cos(a) * 1.7,
                                m5["y"] + math.sin(a) * 1.7,
                                vx=math.cos(a) * 14, vy=math.sin(a) * 14,
                                hp=40 + i * 5, hp_max=100, flags=fl,
                                facing=a + math.pi))
        for i in range(6):
            a = i * math.pi / 3
            eid = 920000 + i
            rm.append(eid)
            ents.append(ent_row(eid, K_SHOT, m5["x"] + math.cos(a) * 2.4,
                                m5["y"] + math.sin(a) * 2.4,
                                vx=math.cos(a) * 12, vy=math.sin(a) * 12,
                                hp=1, hp_max=1, facing=a))
        wire(t, snap_msg(t.js("window.__zza.tick()"), ents))
        time.sleep(0.3)

        def refresh_fx():
            """Дюжина свежих вспышек: они живут 0.3 с и гаснут сами."""
            tk = t.js("window.__zza.tick()")
            for i in range(12):
                a = i * math.pi / 6
                k = ("hit", "shot", "boom", "die")[i % 4]
                t.js("window.__zza.wire(%s)" % json.dumps(json.dumps(
                    {"t": "ev", "tick": tk, "k": k, "a": 910000 + (i % 8),
                     "b": m5["id"], "dmg": 20,
                     "x": m5["x"] + math.cos(a) * 1.2,
                     "y": m5["y"] + math.sin(a) * 1.2})))

        refresh_fx()
        st_on = t.js("window.__zza.stats()")
        ms_on1, s1 = bench(t)
        t.js("window.__zza.setCombat(false)")
        time.sleep(0.2)
        st_off = t.js("window.__zza.stats()")
        ms_off, s2 = bench(t)
        t.js("window.__zza.setCombat(true)")
        refresh_fx()
        time.sleep(0.2)
        ms_on2, s3 = bench(t)
        print("  СЦЕНА: %d сущностей в кадре (8 врагов вокруг: 2 на замахе, "
              "2 в рывке, 4 раненых; 6 снарядов), свой боец на замахе"
              % st_on["ents"])
        print("     бой рисуется      %.4f мс   (drawCalls %d, вспышек %d)"
              % (ms_on1, st_on["drawCalls"], st_on["fx"]))
        print("     бой НЕ рисуется   %.4f мс   (drawCalls %d) — это рендер "
              "до этой работы" % (ms_off, st_off["drawCalls"]))
        print("     бой рисуется опять %.4f мс" % ms_on2)
        print("     разброс по пачкам: %s | %s | %s"
              % (" ".join("%.4f" % v for v in s1),
                 " ".join("%.4f" % v for v in s2),
                 " ".join("%.4f" % v for v in s3)))
        uptime()
        # Тот же замер, но с принудительным сливом кадра. Канва браузера
        # отложенная: draw() складывает команды, а закраска случается потом
        # — в цифрах выше её нет вовсе, и потому они меряют работу клиента,
        # а не пикселей. Чтение одного пикселя после каждого draw() заставляет
        # браузер дорисовать накопленное; само чтение стоит одинаково в обоих
        # замерах и из отношения уходит.
        fl_on = statistics.median([t.js("window.__zza.benchDrawFlush(60)")
                                   for _ in range(3)])
        t.js("window.__zza.setCombat(false)")
        time.sleep(0.2)
        fl_off = statistics.median([t.js("window.__zza.benchDrawFlush(60)")
                                    for _ in range(3)])
        t.js("window.__zza.setCombat(true)")
        refresh_fx()
        time.sleep(0.2)
        print("     со сливом кадра (в замер входит закраска): бой %.4f мс, "
              "без боя %.4f мс, отношение %.2f"
              % (fl_on, fl_off, fl_on / max(1e-9, fl_off)))
        note("две мерки одной сцены",
             "без слива отношение показывает работу КЛИЕНТА (команды рисования, "
             "%d против %d вызовов), со сливом — работу вместе с закраской. "
             "Второе число ближе к правде на целевой машине, первое — к правде "
             "о том, сколько мы добавили кода в кадр"
             % (st_on["drawCalls"], st_off["drawCalls"]))
        ratio = max(ms_on1, ms_on2) / max(1e-9, ms_off)
        ratio_fl = fl_on / max(1e-9, fl_off)
        check(ratio_fl <= MS_RATIO_MAX,
              "стоимость кадра с боем не выросла втрое и с закраской",
              "отношение со сливом %.2f (порог %.1f)" % (ratio_fl, MS_RATIO_MAX))
        check(ratio <= MS_RATIO_MAX,
              "стоимость кадра с боем не выросла втрое",
              "отношение %.2f (порог %.1f). Абсолютные миллисекунды здесь — "
              "SwiftShader, числом целевой машины они НЕ являются; осмысленно "
              "только отношение, оба замера сделаны подряд одним рендером на "
              "одной сцене" % (ratio, MS_RATIO_MAX))

        # Сцену убираем: дальше проверке нужен обычный кадр.
        wire(t, snap_msg(t.js("window.__zza.tick()"),
                         [ent_row(m5["id"], K_PLAYER, m5["x"], m5["y"],
                                  hp=m5["hpMax"], hp_max=m5["hpMax"], flags=0,
                                  facing=m5["facing"])], rm=rm))
        time.sleep(0.4)
        ok_sync2, sync2_s = resync(t, keys)
        note("после стенда клиент снова слушает сервер",
             "пересинхронизация %s за %.1f с"
             % ("удалась" if ok_sync2 else "НЕ удалась", sync2_s))

        # ============ 6. ЗАМАХ РАНЬШЕ УДАРА ==============================
        print()
        print("  --- 6. замах виден раньше удара ----------------------------")
        # Бьём НАСТОЯЩЕГО врага: только так бывает настоящее событие hit.
        # Пункт идёт ПОСЛЕДНИМ и в ОТДЕЛЬНОМ, боевом мире. Враги ходят и
        # стреляют, подойти к врагу — значит встать под огонь, а мёртвый
        # игрок не машет мечом (8.5, combat.begin); замерено на прогоне, где
        # вкладку убили на сороковой секунде и три пункта покраснели разом
        # на исправном клиенте. Поэтому вкладка для драки заводится ТОЛЬКО
        # СЕЙЧАС, с полным здоровьем, и живёт под огнём ровно столько,
        # сколько нужно на один удар.
        t0_errs = t.errors()
        t.close()
        t = Tab(browser, "B", width=VIEW_W, height=VIEW_H).open(war.url)
        room2 = t.create_room("Драка")
        t.ready()
        t.wait_game()
        t.page.wait_for_function(
            "window.__zza.fog() && window.__zza.fog().msgs > 0", timeout=10000)
        time.sleep(0.8)
        keys = Keys(t)
        level = t.js("window.__zza.level()")
        mm = me(t)
        note("боевой мир", "комната %s, этаж %d, своя сущность id %d; врагов "
             "в мире %d" % (room2, level["floor"], mm["id"],
                            len([o for o in t.js("window.__zza.others()")
                                 if o["kind"] in ENEMY_KINDS])))
        note("здоровье перед дракой", "hp %d/%d, flags %d"
             % (mm["hp"], mm["hpMax"], mm["flags"]))
        near = nearest_enemy(t)
        if near is None:
            check(False, "в боевом мире нашёлся враг, по которому можно ударить",
                  "сущностей kind %s в мире нет" % (ENEMY_KINDS,))
        else:
            d0, foe0 = near
            note("ближайший враг", "id %d на (%.1f, %.1f), до него %.1f клетки"
                 % (foe0["id"], foe0["x"], foe0["y"], d0))

            # МАШЕМ НЕПРЕРЫВНО и идём навстречу. Клавиша держится зажатой,
            # сервер бьёт раз в откат (14 тиков, 4.2), враги ближнего боя
            # сами прут на игрока — попадание случится на первом же, кто
            # войдёт в дугу. Условие остановки — СОБЫТИЕ hit, а не время:
            # потолок ниже это предохранитель.
            my_id = mm["id"]
            t.js("window.__zza.track(600)")
            t.page.keyboard.down("KeyF")
            hit = None
            t_fight = time.time()
            while time.time() - t_fight < 30.0:
                mnow = me(t)
                if mnow is None or (mnow["flags"] & F_DEAD):
                    break
                for e in t.js("window.__zza.evLog()"):
                    if e["k"] == FX_HIT and e["a"] == my_id and e["b"] != my_id:
                        hit = e
                        break
                if hit is not None:
                    break
                foe = None
                best = None
                for o in t.js("window.__zza.others()"):
                    if o["kind"] not in ENEMY_KINDS or (o["flags"] & F_DEAD):
                        continue
                    dd = math.hypot(o["x"] - mnow["x"], o["y"] - mnow["y"])
                    if best is None or dd < best:
                        best, foe = dd, o
                if foe is None:
                    break
                aim_at(t, foe["x"], foe["y"])
                if best > 1.3:
                    walk_to(t, level, (int(foe["x"]), int(foe["y"])), keys,
                            3.0, near=1.0, avoid=())
                    keys.release()
                else:
                    time.sleep(0.2)
            t.page.keyboard.up("KeyF")
            keys.release()
            time.sleep(0.5)
            tracks = t.js("window.__zza.tracks()")
            wins = windup_windows(tracks)
            mm2 = me(t)
            note("драка", "замахов за драку %d, здоровье после %d/%d, flags %d, "
                 "секунд %.1f"
                 % (len(wins), mm2["hp"], mm2["hpMax"], mm2["flags"],
                    time.time() - t_fight))

            win = None
            if hit is not None:
                for r, f in wins:
                    if f == hit["tick"]:
                        win = (r, f)
                        break
            if hit is None:
                check(False, "по врагу попали: пришло событие hit",
                      "за %.0f с непрерывных замахов (%d штук) попадания не "
                      "случилось; вкладка сейчас hp %d/%d, flags %d%s"
                      % (time.time() - t_fight, len(wins), mm2["hp"],
                         mm2["hpMax"], mm2["flags"],
                         " — её убили враги, а мёртвый мечом не машет (8.5)"
                         if (mm2["flags"] & F_DEAD) else ""))
            elif win is None:
                check(False, "удар нашёлся в записи замахов",
                      "hit на тике %d, а окна замаха с таким тиком удара в "
                      "записи нет: %s" % (hit["tick"], str(wins[:6])))
            else:
                r, f = win
                note("событие hit", "тик %d, бил id %d, получил id %d, урон %d"
                     % (hit["tick"], hit["a"], hit["b"], hit["dmg"]))
                # ВОТ ГЛАВНОЕ ЧИСЛО. Кадр показывает замах с того тика, на
                # котором пришёл бит WINDUP, — это снято в тишине в пункте 2
                # (первый же кадр по тому снапшоту, прибавка ярких пикселей
                # на целый сектор). Здесь берётся второе число: сколько
                # тиков от этого момента до удара.
                check(f - r == MELEE_WINDUP_TICKS and f > r,
                      "замах видно раньше удара, и ровно на время замаха",
                      "бит WINDUP пришёл на тике %d, событие hit — на тике "
                      "%d: раньше на %d тиков = %.0f мс (4.2 обещает %d "
                      "тиков = 267 мс). Кадр показывает замах с первого же "
                      "кадра, нарисованного по снапшоту тика %d — это "
                      "замерено в пункте 2 в тишине: прибавка %d ярких "
                      "пикселей через %s мс после нажатия"
                      % (r, f, f - r, (f - r) * TICK_MS, MELEE_WINDUP_TICKS, r,
                         lit_during - lit_base,
                         ("%.0f" % (a2["f"]["now"] - sw2["press"]))
                         if (a2["f"] and a2["still"]) else "?"))
                note("все замахи этой драки",
                     "пар (тик замаха, тик удара): %s — разница всегда %s"
                     % (str(wins[:8]),
                        str(sorted(set(b - a for a, b in wins)))))

        # ============ 8. КОНСОЛЬ =========================================
        print()
        errs = t.errors() + t0_errs
        check(not errs, "в консоли нет ошибок за весь прогон",
              "; ".join(errs[:3]) if errs else "0 сообщений уровня error")
        note("событий ev за прогон", str(t.js("window.__zza.evs()")))

        t.close()
        browser.close()

    print()
    print("  Пороги: прибавка ярких пикселей на замахе >= %d (площадь сектора "
          "3184 px, запас 10x);" % MIN_LIT_PX)
    print("  красный в пустой части полоски >= %d (пустая 209, полная 127); "
          "стоимость кадра <= %.0fx." % (HP_RED_T, MS_RATIO_MAX))
    return summary()


if __name__ == "__main__":
    sys.exit(main())
