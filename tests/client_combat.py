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
  3. Замах виден РАНЬШЕ удара. Печатается, на каком тике изменился кадр и на
     каком пришло событие `hit`; первое обязано быть раньше. Это и есть
     смысл замаха: 8 тиков (0.267 с, 4.2) на то, чтобы среагировать.
  4. Полоска здоровья отражает СНАПШОТ, а не событие. Приходит снапшот с
     упавшим hp и БЕЗ события `hit` — полоска всё равно верна; приходит
     событие `hit` без снапшота — полоска не шевелится (5.2 прямо
     запрещает держать состояние на событиях).
  5. Смерть видна: бит DEAD поднят — кадр изменился, на экране «ТЫ ДУХ».
  6. Стоимость кадра не выросла втрое против той, что была до этой работы.
     Сцена ОДНА И ТА ЖЕ, замеры спина к спине, рядом uptime.
  7. Проверка 2 и 3 КРАСНЕЮТ, если перестать рисовать замах.
  8. Ноль ошибок в консоли.

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

ЧЕГО ЗДЕСЬ НЕ ХВАТАЕТ (честно)

  Урон по игроку и его смерть подаются НАСТОЯЩИМ сообщением снапшота (5.2)
  в net._onMessage — тем же входом, куда приходит провод, как это уже
  сделано в client_fog.py для врага в тумане. Причина: на этаже есть враги,
  но бить игрока им пока нечем (ИИ — этап 2b). Проверяется ровно то, за что
  отвечает клиент: пришло hp в снапшоте — нарисуй полоску; пришёл бит DEAD
  — нарисуй духа. Когда у врагов появится ИИ, тот же кадр поедет тем же
  путём, и менять здесь будет нечего.

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


def swing(tab, box, hold=0.10, key="KeyF"):
    """Один удар с записью кадров и снапшотов. Возвращает всё снятое."""
    base = settle(tab, box)
    tab.js("window.__zza.watch(%d,%d,%d,%d,%d)"
           % (round(box[0]), round(box[1]), round(box[2]), round(box[3]),
              WATCH_FRAMES))
    tab.js("window.__zza.track(%d)" % TRACK_SNAPS)
    t_press = tab.js("performance.now()")
    tab.page.keyboard.down(key)
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
    return {"base": base, "frames": frames, "tracks": tracks,
            "press": t_press, "box": box}


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
        if o["kind"] != K_ENEMY or (o["flags"] & F_DEAD):
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

    with Server() as srv, sync_playwright() as pw:
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
        # 12 кл/с и в кадре опроса из питона может уже не существовать, а
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

        # Кадр ИМЕННО на замахе: жмём атаку и, пока горит WINDUP, читаем
        # пиксели. Ждём бита, а не секунд.
        t.page.keyboard.down("KeyF")
        t.page.wait_for_function("(window.__zza.me().flags & 4) !== 0",
                                 timeout=4000)
        during = px(t, *box)
        t.page.keyboard.up("KeyF")
        lit_base = base["n"] - base["dark"]
        lit_during = during["n"] - during["dark"]
        note("коробка замаха", "%dx%d пикселей вокруг бойца, порог яркости %d"
             % (round(box[2]), round(box[3]), LIT_T))
        check(lit_during - lit_base >= MIN_LIT_PX,
              "на замахе кадр изменился, и не на один пиксель",
              "ярких пикселей %d -> %d, прибавка %d (нужно >= %d; площадь "
              "сектора 110 градусов радиусом 1.2 клетки = 3184 пикселя), "
              "самый яркий канал %d -> %d, средняя яркость %.1f -> %.1f"
              % (lit_base, lit_during, lit_during - lit_base, MIN_LIT_PX,
                 base["maxCh"], during["maxCh"], base["mean"], during["mean"]))
        time.sleep(0.8)

        # ============ 3. ЗАМАХ РАНЬШЕ УДАРА ==============================
        print()
        print("  --- 3. замах виден раньше удара ----------------------------")
        # Бьём НАСТОЯЩЕГО врага: только так бывает настоящее событие hit.
        near = nearest_enemy(t)
        hit_ev = None
        s = None
        if near is None:
            check(False, "на этаже нашёлся враг, по которому можно ударить",
                  "сущностей kind=2 в мире нет")
        else:
            d0, foe = near
            note("ближайший враг", "id %d на (%.1f, %.1f), до него %.1f клетки"
                 % (foe["id"], foe["x"], foe["y"], d0))
            walk_to(t, level, (int(foe["x"]), int(foe["y"])), keys,
                    WALK_BUDGET, near=1.0)
            keys.release()
            time.sleep(0.6)
            foe_now = None
            for o in t.js("window.__zza.others()"):
                if o["id"] == foe["id"]:
                    foe_now = o
            m1 = me(t)
            d1 = math.hypot(foe_now["x"] - m1["x"], foe_now["y"] - m1["y"]) \
                if foe_now else 1e9
            aim_at(t, foe_now["x"], foe_now["y"]) if foe_now else None
            time.sleep(0.5)
            note("подошли к врагу", "расстояние %.2f клетки (достать можно с "
                 "%.2f: 1.2 от центра плюс радиус цели 0.35, 4.2)"
                 % (d1, 1.2 + 0.35))

            box = self_box(t)
            s = swing(t, box)
            idx, f = first_change(s["frames"], s["base"]["hash"])
            rise, fall = windup_window(s["tracks"])
            evs = [e for e in t.js("window.__zza.evLog()")
                   if e["k"] == FX_HIT and e["now"] >= s["press"]]
            hit_ev = evs[0] if evs else None

            if idx < 0 or rise is None:
                check(False, "удар записан: кадр изменился и WINDUP поднялся",
                      "кадров записано %d, изменение %s, подъём WINDUP %s"
                      % (len(s["frames"]), idx, rise))
            else:
                dt_frame = f["now"] - s["press"]
                note("нажатие -> кадр",
                     "кадр изменился через %.0f мс после нажатия, на кадре "
                     "номер %d из %d; показанный тик %.2f"
                     % (dt_frame, idx, len(s["frames"]), f["rt"]))
                note("замах в снапшотах",
                     "WINDUP горит с тика %d по тик %d включительно (%d тиков "
                     "при %d по 4.2); удар приходит на тике %s"
                     % (rise, (fall - 1) if fall else -1,
                        (fall - rise) if fall else -1, MELEE_WINDUP_TICKS,
                        str(fall)))
                if hit_ev is not None:
                    note("событие hit",
                         "тик %d, бил id %d, получил id %d, урон %d; пришло "
                         "через %.0f мс после нажатия"
                         % (hit_ev["tick"], hit_ev["a"], hit_ev["b"],
                            hit_ev["dmg"], hit_ev["now"] - s["press"]))
                    ok = f["now"] < hit_ev["now"] and f["rt"] < hit_ev["tick"]
                    check(ok,
                          "кадр с замахом изменился РАНЬШЕ, чем пришло hit",
                          "кадр на тике %.2f (через %.0f мс), hit на тике %d "
                          "(через %.0f мс) — запас %.0f мс, то есть %.1f тика"
                          % (f["rt"], dt_frame, hit_ev["tick"],
                             hit_ev["now"] - s["press"],
                             hit_ev["now"] - f["now"],
                             (hit_ev["now"] - f["now"]) / TICK_MS))
                else:
                    # Врага не задели (он мог стоять чуть дальше дуги).
                    # Момент удара всё равно известен точно: сервер снимает
                    # бит WINDUP ровно на тике удара (combat._land_melee).
                    check(fall is not None and f["rt"] < fall,
                          "кадр с замахом изменился РАНЬШЕ момента удара",
                          "события hit не было (дуга прошла мимо), но момент "
                          "удара виден по снятию бита WINDUP: кадр на тике "
                          "%.2f, удар на тике %s, запас %.1f тика = %.0f мс"
                          % (f["rt"], str(fall),
                             (fall - f["rt"]) if fall else -1,
                             (fall - f["rt"]) * TICK_MS if fall else -1))

        # ============ 4. ПОЛОСКА ЗДОРОВЬЯ — ОТ СОСТОЯНИЯ =================
        print()
        print("  --- 4. полоска здоровья от снапшота, а не от события -------")
        keys.release()
        time.sleep(0.5)
        bar = t.js("window.__zza.hpBar()")
        # Две пробы: в левой трети полоски и в правой пятой. Полная полоска
        # зелёная (#7fd18a, r=127) на обеих, короткая — красная (#d16a6a,
        # r=209) на правой.
        q = max(6, int(bar["h"] * 0.5))
        left = (bar["x"] + bar["w"] * 0.25 - q, bar["y"] + 3, q * 2, bar["h"] - 6)
        right = (bar["x"] + bar["w"] * 0.85 - q, bar["y"] + 3, q * 2, bar["h"] - 6)
        m1 = me(t)
        full_l, full_r = px(t, *left), px(t, *right)
        note("полоска своего здоровья",
             "прямоугольник %dx%d в точке (%d,%d); пробы по %dx%d в 25%% и "
             "85%% длины" % (round(bar["w"]), round(bar["h"]), round(bar["x"]),
                             round(bar["y"]), round(left[2]), round(left[3])))
        note("hp %d/%d — полоска полная" % (m1["hp"], m1["hpMax"]),
             "самый яркий красный: слева %d, справа %d (зелёная часть #7fd18a "
             "даёт 127)" % (full_l["maxR"], full_r["maxR"]))

        # Урон СНАПШОТОМ и БЕЗ события hit: полоска обязана быть верной.
        tick = t.js("window.__zza.tick()")
        hurt = 60
        wire(t, snap_msg(tick, [ent_row(m1["id"], K_PLAYER, m1["x"], m1["y"],
                                        hp=hurt, hp_max=m1["hpMax"],
                                        flags=0, facing=m1["facing"])]))
        time.sleep(0.4)
        ev_before = t.js("window.__zza.evs()")
        hurt_l, hurt_r = px(t, *left), px(t, *right)
        m2 = me(t)
        check(m2["hp"] == hurt and hurt_r["maxR"] >= HP_RED_T
              and hurt_l["maxR"] < HP_RED_T,
              "полоска показывает урон из СНАПШОТА, события hit не было",
              "hp %d -> %d; самый яркий красный справа %d -> %d (порог %d, "
              "пустая часть #d16a6a даёт 209), слева %d -> %d (осталось "
              "зелёным). Событий за это время не пришло ни одного: было %d, "
              "стало %d" % (m1["hp"], m2["hp"], full_r["maxR"], hurt_r["maxR"],
                            HP_RED_T, full_l["maxR"], hurt_l["maxR"],
                            ev_before, t.js("window.__zza.evs()")))

        # Теперь наоборот: приходит событие hit и НИЧЕГО больше. Полоска не
        # имеет права шевельнуться — 5.2 запрещает держать состояние на ev.
        before = px(t, *right)
        t.js("window.__zza.wire(%s)" % json.dumps(json.dumps(
            {"t": "ev", "tick": tick + 1, "k": "hit", "a": 900001,
             "b": m1["id"], "dmg": 35, "x": m1["x"], "y": m1["y"]})))
        time.sleep(0.4)
        after = px(t, *right)
        m3 = me(t)
        check(m3["hp"] == hurt and after["maxR"] == before["maxR"],
              "событие hit БЕЗ снапшота полоску не двигает (5.2)",
              "пришло hit на 35 урона: hp в мире %d (осталось %d), самый "
              "яркий красный в пробе %d -> %d — полоска не шевельнулась"
              % (m3["hp"], hurt, before["maxR"], after["maxR"]))

        # ============ 5. СМЕРТЬ ВИДНА ====================================
        print()
        print("  --- 5. смерть видна ----------------------------------------")
        centre = (VIEW_W * 0.5 - 300, VIEW_H * 0.5 - 60, 600, 120)
        alive_box = px(t, *centre)
        wire(t, snap_msg(tick + 2, [ent_row(m1["id"], K_PLAYER, m1["x"],
                                            m1["y"], hp=0, hp_max=m1["hpMax"],
                                            flags=F_DEAD,
                                            facing=m1["facing"])]))
        time.sleep(0.5)
        dead_box = px(t, *centre)
        m4 = me(t)
        check((m4["flags"] & F_DEAD) != 0 and
              dead_box["hash"] != alive_box["hash"] and
              dead_box["mean"] > alive_box["mean"] + 5,
              "свой персонаж стал духом — кадр изменился, на экране «ТЫ ДУХ»",
              "flags %d (бит DEAD = 1); коробка %dx%d по центру кадра: ярких "
              "пикселей %d -> %d, средняя яркость %.1f -> %.1f, самый яркий "
              "канал %d -> %d"
              % (m4["flags"], round(centre[2]), round(centre[3]),
                 alive_box["n"] - alive_box["dark"],
                 dead_box["n"] - dead_box["dark"],
                 alive_box["mean"], dead_box["mean"],
                 alive_box["maxCh"], dead_box["maxCh"]))
        # Полоски своего здоровья у духа нет — вместо неё надпись.
        ghost_bar = px(t, *right)
        note("полоска здоровья у духа",
             "самый яркий красный в пробе %d (у живого с уроном было %d): "
             "полоска убрана, на её месте надпись по центру кадра"
             % (ghost_bar["maxR"], hurt_r["maxR"]))

        # Возвращаем себя к жизни тем же путём и проверяем, что кадр вернулся.
        wire(t, snap_msg(tick + 3, [ent_row(m1["id"], K_PLAYER, m1["x"],
                                            m1["y"], hp=m1["hpMax"],
                                            hp_max=m1["hpMax"], flags=0,
                                            facing=m1["facing"])]))
        time.sleep(0.5)
        back = px(t, *centre)
        check(back["hash"] == alive_box["hash"],
              "живой кадр вернулся тем же, каким был до смерти",
              "хэш коробки %d -> %d -> %d (первый и третий совпали)"
              % (alive_box["hash"], dead_box["hash"], back["hash"]))

        # ============ 6. СТОИМОСТЬ КАДРА =================================
        print()
        print("  --- 6. стоимость кадра: до и после, одна сцена -------------")
        # Сцена строится ОДИН РАЗ и не меняется между замерами: вокруг игрока
        # восемь врагов (двое на замахе, двое в рывке, четверо раненые),
        # шесть снарядов в воздухе и дюжина вспышек. Это заметно гуще, чем
        # бывает в комнате, — замер нарочно недобрый.
        m5 = me(t)
        ents = [ent_row(m5["id"], K_PLAYER, m5["x"], m5["y"], hp=70,
                        hp_max=100, flags=F_WINDUP, facing=m5["facing"])]
        rm = []
        for i in range(8):
            a = i * math.pi / 4
            eid = 910000 + i
            rm.append(eid)
            fl = F_WINDUP if i < 2 else (F_DASH if i < 4 else 0)
            ents.append(ent_row(eid, K_ENEMY, m5["x"] + math.cos(a) * 1.7,
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
        wire(t, snap_msg(tick + 4, ents))
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
        ratio = max(ms_on1, ms_on2) / max(1e-9, ms_off)
        check(ratio <= MS_RATIO_MAX,
              "стоимость кадра с боем не выросла втрое",
              "отношение %.2f (порог %.1f). Абсолютные миллисекунды здесь — "
              "SwiftShader, числом целевой машины они НЕ являются; осмысленно "
              "только отношение, оба замера сделаны подряд одним рендером на "
              "одной сцене" % (ratio, MS_RATIO_MAX))

        # Сцену убираем: дальше проверке нужен обычный кадр.
        wire(t, snap_msg(tick + 5, [ent_row(m5["id"], K_PLAYER, m5["x"],
                                            m5["y"], hp=100, hp_max=100,
                                            flags=0, facing=m5["facing"])],
                         rm=rm))
        time.sleep(0.4)

        # ============ 7. ПРОВЕРКА УМЕЕТ КРАСНЕТЬ =========================
        print()
        print("  --- 7. умеет ли краснеть -----------------------------------")
        # Тот же удар, но рендеру запрещено рисовать замах. Проверка 2
        # обязана перестать видеть прибавку ярких пикселей, а проверка 3 —
        # изменение кадра в окне замаха.
        t.js("window.__zza.setWindup(false)")
        time.sleep(0.5)
        box = self_box(t)
        base_off = settle(t, box)
        t.page.keyboard.down("KeyF")
        t.page.wait_for_function("(window.__zza.me().flags & 4) !== 0",
                                 timeout=4000)
        during_off = px(t, *box)
        t.page.keyboard.up("KeyF")
        gain_off = (during_off["n"] - during_off["dark"]) - \
                   (base_off["n"] - base_off["dark"])
        check(gain_off < MIN_LIT_PX,
              "без отрисовки замаха проверка 2 КРАСНЕЕТ",
              "прибавка ярких пикселей на замахе %d < %d — то есть проверка "
              "такой клиент НЕ пропускает (с отрисовкой прибавка была %d)"
              % (gain_off, MIN_LIT_PX, lit_during - lit_base))
        time.sleep(0.8)
        s_off = swing(t, box)
        idx_off, f_off = first_change(s_off["frames"], s_off["base"]["hash"])
        rise_off, fall_off = windup_window(s_off["tracks"])
        seen_in_windup = (idx_off >= 0 and rise_off is not None and
                          fall_off is not None and
                          f_off["rt"] < fall_off)
        check(not seen_in_windup,
              "без отрисовки замаха проверка 3 КРАСНЕЕТ",
              "WINDUP горел с тика %s по %s, а кадр в этом окне не менялся "
              "вовсе (первое изменение: %s) — сказать «замах видно раньше "
              "удара» стало не о чем"
              % (str(rise_off), str((fall_off - 1) if fall_off else None),
                 ("тик %.2f" % f_off["rt"]) if idx_off >= 0 else "его нет"))
        t.js("window.__zza.setWindup(true)")
        time.sleep(0.3)
        check(t.js("window.__zza.getWindup()") is True and
              t.js("window.__zza.getCombat()") is True,
              "отрисовка боя возвращена во включённое состояние")

        # ============ 8. КОНСОЛЬ =========================================
        print()
        errs = t.errors()
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
