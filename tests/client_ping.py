#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка пингов на карте (DESIGN.md 8.8) — клиентская половина.

ДВА НАСТОЯЩИХ БРАУЗЕРА в одной игре. Одна вкладка ставит пинг так, как его
ставит человек — мышью и клавишей, — вторая смотрит на СВОИ ПИКСЕЛИ. Меряется
кадр, а не модель клиента: «видно» — это то, что попало на канву.

Что здесь меряется:

  [1] пинг одной вкладки виден в КАДРЕ другой (пиксели до и после);
  [2] видно, КТО поставил: ширина имени в кадре сходится с шириной строки,
      померенной тем же шрифтом, и меняется вместе с автором;
  [3] пинг виден в НЕРАЗВЕДАННОЙ черноте — это решение, см. 8.8 в отчёте;
  [4] пинг вне кадра даёт стрелку, и она показывает ТУДА (угол в градусах);
  [5] пинг гаснет, и кадр возвращается к прежнему побитно (хэш);
  [6] спам пингами не ломает игру у соседа;
  [7] стоимость кадра с пингами и без — спина к спине, со сливом.

Запуск:
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/client_ping.py
    python3 tests/client_ping.py --case=see     # только [1], для подсадки
Выход: 0 — зелено, 1 — красно.
"""

import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client_common import (BROWSER_ARGS, CHROMIUM, Server, Tab, check,   # noqa: E402
                           note, summary, uptime, FAILS, Keys, walk_to)

# --- имена авторов. Разной ДЛИНЫ намеренно: по ширине имени в кадре видно,
# что рисуется именно автор, а не «какая-то подпись».
NAME_A = "Аня"
NAME_B = "Ляпкин-Тяпкин"

PING_FONT = "15px system-ui, sans-serif"
TEXT_PAD = 6          # textSprite в render2d.js добавляет 6 px к ширине

# Геометрия метки в кадре (render2d._drawPings), в долях клетки:
#   значок       — на 0.92 клетки выше точки, полуразмер 0.28;
#   имя          — ещё на 0.42 клетки выше значка.
# Полоса имени берётся ВЫШЕ значка целиком, чтобы в ширину имени не попал
# сам значок: при коротком имени («Аня» это 30 px) значок шириной 33 px
# перебил бы измерение.
TILE = 64
BADGE_DY = int(0.92 * TILE)          # 58
NAME_DY = BADGE_DY + int(0.42 * TILE)  # 84
NAME_STRIP = (-16, -4)               # полоса относительно центра имени

BRIGHT = 90        # порог «яркого» пикселя, как в tests/client_combat.py:
                   # самый светлый пол даёт 79.5 (4.1), запас 1.13

# Срок жизни пинга — из контракта через сервер, а не переписан числом.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import room as room_mod            # noqa: E402
PING_LIFE_S = room_mod.PING_LIFE / 30.0
PING_GAP_S = room_mod.PING_GAP / 30.0


def wait_for(fn, limit, step=0.05):
    """Ждать УСЛОВИЕ. limit — предохранитель (правило 10), не порог."""
    t0 = time.time()
    while time.time() - t0 < limit:
        v = fn()
        if v:
            return v, time.time() - t0
        time.sleep(step)
    return fn(), time.time() - t0


def text_width(tab, text):
    """Ширина строки ТЕМ ЖЕ шрифтом, каким её рисует render2d.js."""
    return tab.js(
        "(() => { const c = document.createElement('canvas').getContext('2d');"
        " c.font = %r; return c.measureText(%r).width; })()"
        % (PING_FONT, text))


def cols_bright(tab, x0, y0, w, h, thresh=BRIGHT):
    """Ярких пикселей по СТОЛБЦАМ полосы. Одна ходка в браузер."""
    cols = [[x0 + i, y0, 1, h] for i in range(int(w))]
    out = tab.js("window.__zza.boxesMean(%r, %d)" % (cols, thresh))
    return [b["bright"] for b in out]


def span_delta(x0, before, after):
    """Ширина того, что ПОЯВИЛОСЬ в полосе: [левый, правый, ширина, столбцов].

    Считается по РАЗНОСТИ, а не по «сколько ярких сейчас». Причина прямая:
    в полосе над меткой запросто стоит верх стены (#666f88, максимальный
    канал 136 — ярче порога 90), и требование «до пинга полоса пуста» не
    выполняется на половине карты. Разность же видит ровно то, что
    дорисовал пинг, каким бы ни был фон.
    """
    lit = [i for i in range(len(before)) if after[i] - before[i] > 0]
    if not lit:
        return None
    return (x0 + lit[0], x0 + lit[-1], lit[-1] - lit[0] + 1, len(lit))


def grid_bright(tab, cell=64):
    """Ярких пикселей по сетке кадра. Одна ходка в браузер, не сотня.

    В 3D сетка КРУПНЕЕ, и это не лень: у WebGL кадр не хранится, и чтение
    каждой коробки заставляет бэкенд перерисовать сцену целиком (см.
    render3d.readPixels). При сетке 64 px полный обход стоил 3.3 с — дольше,
    чем живёт пинг (4.0 с), и проверка честно меряла уже погасшую метку.
    """
    if tab.js("window.__zza.getBackend()") == "3d":
        cell = max(cell, 128)
    size = tab.js("window.__zza.canvasSize()")
    boxes = []
    for y in range(0, size["h"], cell):
        for x in range(0, size["w"], cell):
            boxes.append([x, y, min(cell, size["w"] - x), min(cell, size["h"] - y)])
    out = tab.js("window.__zza.boxesMean(%r, %d)" % (boxes, BRIGHT))
    return boxes, [b["bright"] for b in out]


def centroid(boxes, before, after):
    """Центр тяжести ПРИБАВКИ ярких пикселей. Так видно, ГДЕ появилось новое."""
    sx = sy = sw = 0.0
    for i, b in enumerate(boxes):
        d = after[i] - before[i]
        if d <= 0:
            continue
        sx += (b[0] + b[2] / 2.0) * d
        sy += (b[1] + b[3] / 2.0) * d
        sw += d
    if sw <= 0:
        return None, 0
    return (sx / sw, sy / sw), sw


def ang(dx, dy):
    return math.degrees(math.atan2(dy, dx))


def ang_err(a, b):
    d = (a - b + 180.0) % 360.0 - 180.0
    return abs(d)


def ping_by_mouse(tab, wx, wy, key="KeyV"):
    """Пинг РУКАМИ: мышь наводится на точку мира, жмётся клавиша.

    НАЖАТИЕ ПОДТВЕРЖДАЕТСЯ СЧЁТЧИКОМ input.js, и вот зачем. Вкладок две, а
    фокус в браузере один: синтетическое нажатие изредка не доезжает до той
    вкладки, которой адресовано (замерено: 1 промах на 2 прогона). Это
    свойство playwright, а не игры, и молчаливый промах превращался бы в
    красную проверку исправного клиента. Повтор идёт ТОЛЬКО когда сам
    клиент говорит «нажатия не было»; если нажатие дошло, а пинга нет —
    это уже настоящая поломка, и она покраснеет.
    """
    s = aim_at(tab, wx, wy)
    return s, press_ping(tab, key)


def aim_at(tab, wx, wy):
    """Навести мышь на точку мира и дать прицелу уехать на сервер.

    ОТДЕЛЬНО ОТ НАЖАТИЯ намеренно: прицел меняет facing СВОЕЙ сущности, а
    facing виден соседу (4.3). Замер «кадр вернулся к прежнему побитно»
    обязан снимать эталон уже ПОСЛЕ наводки, иначе он поймает поворот
    соседа и объявит это следом пинга — замерено, расхождение 2 пикселя.
    """
    s = tab.js("window.__zza.worldToScreen(%r, %r)" % (wx, wy))
    tab.page.mouse.move(s[0], s[1])
    time.sleep(0.15)
    return s


def press_ping(tab, key="KeyV"):
    """Нажать клавишу пинга и дождаться, что пинг УШЁЛ на сервер.

    Ждём не «клавиша засчитана», а «сообщение ушло»: между ними лежит опрос
    30 Гц, и проверка, которая шла дальше сразу после нажатия, гонялась с
    опросом наперегонки.
    """
    n0 = tab.js("window.__zza.pingsSent()")
    tries = 0
    for _ in range(4):
        tries += 1
        tab.page.keyboard.press(key)
        got, _dt = wait_for(lambda: tab.js("window.__zza.pingsSent()") > n0, 0.6)
        if got:
            break
    return tries


def wait_clear(*tabs):
    """Дождаться пустого экрана: живой пинг соседа портит любой эталон."""
    ok, dt = wait_for(lambda: all(not t.js("window.__zza.pings()") for t in tabs),
                      PING_LIFE_S + 3.0)
    time.sleep(0.45)
    return ok, dt


def ping_raw(author, watcher, wx, wy, kind=0, limit=4.0):
    """Пинг мимо мыши (за кадром мышью не ткнуть) — и дождаться, что дошёл.

    Ждёт ИМЕННО ЭТУ точку: ограничитель сервера (0.5 с на игрока) отвергает
    пинг, поставленный сразу за предыдущим, и проверка, которая просто
    «послала и пошла дальше», мерила бы чужой прежний пинг.
    """
    t0 = time.time()
    tries = 0
    while time.time() - t0 < limit:
        tries += 1
        n0 = author.js("window.__zza.pingsSent()")
        author.js("window.__zza.pingAt(%r, %r, %d)" % (wx, wy, kind))
        wait_for(lambda: author.js("window.__zza.pingsSent()") > n0, 0.6)
        got, _ = wait_for(
            lambda: [p for p in watcher.js("window.__zza.pings()")
                     if abs(p["x"] - wx) < 0.01 and abs(p["y"] - wy) < 0.01],
            PING_GAP_S + 0.3)
        if got:
            return True, tries
        time.sleep(PING_GAP_S)
    return False, tries


def pick_point(a, b, want_margin=150):
    """Точка, которая видна ОБОИМ и не жмётся к краю кадра ни у одного.

    Берётся середина между игроками: она заведомо ближе к каждому, чем они
    друг к другу, а значит внутри кадра у обоих, если они вообще рядом.
    """
    pa, pb = a.self_pos(), b.self_pos()
    cands = [((pa["x"] + pb["x"]) / 2, (pa["y"] + pb["y"]) / 2)]
    for dx, dy in ((2, 0), (-2, 0), (0, 2), (0, -2), (2, 2), (-2, -2)):
        cands.append((pa["x"] + dx, pa["y"] + dy))
    for wx, wy in cands:
        ok = True
        for t in (a, b):
            s = t.js("window.__zza.worldToScreen(%r, %r)" % (wx, wy))
            sz = t.js("window.__zza.canvasSize()")
            if not (want_margin <= s[0] <= sz["w"] - want_margin and
                    want_margin <= s[1] <= sz["h"] - want_margin):
                ok = False
                break
        if ok:
            return wx, wy
    return None


def bring_together(a, b, level):
    """Свести вкладки поближе, если разбросало по этажу.

    Приёмка пингов меряет ПИКСЕЛИ у соседа, а сосед должен видеть точку
    пинга. Расстояние — это условие задачи, а не то, что здесь проверяется.
    """
    pa, pb = a.self_pos(), b.self_pos()
    d = math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"])
    if d <= 5.0:
        return d
    k = Keys(b)
    walk_to(b, level, (int(pa["x"]), int(pa["y"])), k, 30.0, near=2.0)
    k.release()
    time.sleep(0.5)
    pa, pb = a.self_pos(), b.self_pos()
    return math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"])


def box_at(tab, wx, wy, r=90):
    s = tab.js("window.__zza.worldToScreen(%r, %r)" % (wx, wy))
    return [int(s[0] - r), int(s[1] - r), 2 * r, 2 * r], s


# =========================================================================
# [1] пинг одной вкладки виден в кадре другой
# =========================================================================

def case_see(a, b, point):
    print("\n[1] Пинг вкладки A виден В КАДРЕ вкладки B")
    wx, wy = point
    box, sb = box_at(b, wx, wy)
    before = b.js("window.__zza.pixels(%d,%d,%d,%d,%d)" % tuple(box + [BRIGHT]))

    sa, tries = ping_by_mouse(a, wx, wy)
    note("как ставили", "вкладка A: мышь в (%.0f, %.0f) на своей канве, "
         "клавиша V (нажатий playwright %d)" % (sa[0], sa[1], tries))
    check(a.js("window.__zza.pingPresses()") >= 1,
          "клавиша V дошла до input.js вкладки A",
          "нажатий засчитано %d" % a.js("window.__zza.pingPresses()"))

    got, dt = wait_for(lambda: b.js("window.__zza.pings()"), 3.0)
    check(bool(got), "пинг дошёл до вкладки B", "за %.2f с, записей %d"
          % (dt, len(got or [])))
    if not got:
        return None
    p = got[0]
    check(abs(p["x"] - wx) < 0.01 and abs(p["y"] - wy) < 0.01,
          "точка пинга у B совпала с точкой, куда тыкала A",
          "A тыкала (%.3f, %.3f), B получила (%.3f, %.3f)"
          % (wx, wy, p["x"], p["y"]))
    check(p["nm"] == NAME_A, "у пинга есть автор", "nm=%r" % p["nm"])

    mine, _ = wait_for(lambda: a.js("window.__zza.pings()"), 1.5)
    check(bool(mine), "автор видит свой пинг у себя в кадре тоже",
          "записей у A %d" % len(mine or []))

    time.sleep(0.25)
    after = b.js("window.__zza.pixels(%d,%d,%d,%d,%d)" % tuple(box + [BRIGHT]))
    st = b.js("window.__zza.stats()")
    print("    КОРОБКА %dx%d ВОКРУГ ТОЧКИ ПИНГА, ЭКРАН ВКЛАДКИ B:" % (box[2], box[3]))
    print("      было:  ярких %5d  maxCh %3d  средняя %6.2f  хэш %s"
          % (before["bright"], before["maxCh"], before["mean"], before["hash"]))
    print("      стало: ярких %5d  maxCh %3d  средняя %6.2f  хэш %s"
          % (after["bright"], after["maxCh"], after["mean"], after["hash"]))
    # Порог выведен из размера метки, а не подобран: кольцо радиусом
    # 0.3..0.64 клетки обводкой 4.5 px даёт не меньше 2*pi*19*4 = 477 px,
    # значок — 33x33 с заливкой, имя — десятки. Берём 200: вчетверо ниже
    # самой тонкой части, то есть «нарисовано хоть что-то похожее на метку».
    grew = after["bright"] - before["bright"]
    check(grew >= 200,
          "в кадре B на месте пинга ПОЯВИЛИСЬ пиксели",
          "ярких прибавилось %d при пороге 200 (вывод: одно кольцо метки "
          "это >=477 px)" % grew)
    # Хэш кадра здесь — ДАННЫЕ, а не проверка. Он меняется и без пинга:
    # наводка мыши у соседа поворачивает его facing (4.3), и это видно в
    # кадре. Проверка на хэше стоит там, где она умеет краснеть, — в [5]:
    # «с пингом кадр был другим» против «после смерти он побитно прежний».
    note("хэш коробки", "%s -> %s" % (before["hash"], after["hash"]))
    check(st.get("pings", 0) >= 1, "рендер B отчитался, что нарисовал метку",
          "pings=%s pingsOff=%s" % (st.get("pings"), st.get("pingsOff")))
    return {"box": box, "before": before, "after": after, "s": sb}


# =========================================================================
# [2] видно, КТО поставил
# =========================================================================

def name_strip(sx, sy):
    return (int(sx - 200), int(sy - NAME_DY + NAME_STRIP[0]), 400,
            NAME_STRIP[1] - NAME_STRIP[0])


def clean_spot(a, b, point, tabs, want=170):
    """Точка, которая видна обеим вкладкам с запасом от края кадра.

    Отступ 170 px — это метка целиком: имя стоит на 84 px выше точки, его
    спрайт высотой 28 px, плюс запас. Пингануть себе под ноги нельзя:
    полоса имени тогда приходится на СОСЕДА, и мерилась бы его полоска
    здоровья (замерено: 198 px «имени» там, где строка занимает 35).
    """
    wx0, wy0 = point
    for d in (3.0, 4.0, 5.0):
        for dx, dy in ((0, -d), (d, 0), (-d, 0), (0, d), (d, -d), (-d, -d)):
            wx, wy = wx0 + dx, wy0 + dy
            good = True
            for t in tabs:
                s = t.js("window.__zza.worldToScreen(%r, %r)" % (wx, wy))
                sz = t.js("window.__zza.canvasSize()")
                if not (want <= s[0] <= sz["w"] - want and
                        want <= s[1] <= sz["h"] - want):
                    good = False
                    break
            if good:
                return (wx, wy), b.js("window.__zza.worldToScreen(%r, %r)"
                                      % (wx, wy))
    return None, None


def case_author(a, b, point):
    print("\n[2] Видно, КТО поставил: имя автора в кадре")
    wait_clear(a, b)
    pt, sb = clean_spot(a, b, point, (a, b))
    if pt is None:
        check(False, "нашлась точка, видная обеим вкладкам с запасом от края")
        return None
    wx, wy = pt
    st = name_strip(sb[0], sb[1])
    aim_at(a, wx, wy)          # facing соседа меняется ДО эталона
    time.sleep(0.35)
    before = cols_bright(b, *st)
    press_ping(a)
    wait_for(lambda: b.js("window.__zza.pings()"), 3.0)
    time.sleep(0.3)
    after = cols_bright(b, *st)
    want_a = text_width(b, NAME_A) + TEXT_PAD
    sp = span_delta(st[0], before, after)
    note("точка для замера имени", "(%.2f, %.2f), полоса %dx%d px над значком"
         % (wx, wy, st[2], st[3]))
    ok = sp is not None
    if ok:
        print("    ПОЛОСА ИМЕНИ НАД ЗНАЧКОМ, ЭКРАН B (автор %s):" % NAME_A)
        print("      прибавка ярких в столбцах x=%d..%d, ширина %d px; "
              "measureText(%r) = %.1f px"
              % (sp[0], sp[1], sp[2], NAME_A, want_a))
        # Допуск 30%: спрайт имени несёт тень со сдвигом 1 px и отступ 3 px,
        # а измеряется он по ЯРКИМ пикселям, то есть без тени.
        check(abs(sp[2] - want_a) <= 0.30 * want_a + 6,
              "ширина имени в кадре сходится с шириной строки «%s»" % NAME_A,
              "в кадре %d px, ожидание %.1f px, допуск %.1f"
              % (sp[2], want_a, 0.30 * want_a + 6))
    else:
        check(False, "имя автора нарисовано над меткой")

    # --- а теперь пингует ВТОРОЙ, и подпись обязана СМЕНИТЬСЯ -------------
    # Это и есть «видно, КТО поставил»: не «есть какая-то подпись», а
    # «подпись меняется вместе с автором». Имена взяты разной длины
    # намеренно, и разница читается прямо в пикселях кадра.
    wait_clear(a, b)
    pt2, sa2 = clean_spot(b, a, point, (a, b))
    if pt2 is None or sp is None:
        return sp
    st2 = name_strip(sa2[0], sa2[1])
    aim_at(b, pt2[0], pt2[1])
    time.sleep(0.35)
    before2 = cols_bright(a, *st2)
    press_ping(b)
    wait_for(lambda: a.js("window.__zza.pings()"), 3.0)
    time.sleep(0.3)
    after2 = cols_bright(a, *st2)
    sp2 = span_delta(st2[0], before2, after2)
    want_b = text_width(a, NAME_B) + TEXT_PAD
    got = a.js("window.__zza.pings()")
    if sp2 is None:
        check(False, "имя второго автора нарисовано над его меткой")
        return sp
    print("    ТА ЖЕ ПОЛОСА, ЭКРАН A (автор %s):" % NAME_B)
    print("      прибавка ярких в столбцах x=%d..%d, ширина %d px; "
          "measureText(%r) = %.1f px" % (sp2[0], sp2[1], sp2[2], NAME_B, want_b))
    print("      имя в кадре:  «%s» %d px  против  «%s» %d px  (отношение %.2f)"
          % (NAME_A, sp[2], NAME_B, sp2[2], sp2[2] / max(1, sp[2])))
    check(abs(sp2[2] - want_b) <= 0.30 * want_b + 6,
          "ширина имени в кадре сходится с шириной строки «%s»" % NAME_B,
          "в кадре %d px, ожидание %.1f px" % (sp2[2], want_b))
    # Отношение ожидаемых ширин, а не «больше вдвое на глаз».
    ratio = want_b / want_a
    check(sp2[2] > sp[2] * (1 + (ratio - 1) * 0.5),
          "подпись МЕНЯЕТСЯ вместе с автором, а не нарисована раз и навсегда",
          "%d px против %d px при ожидаемом отношении %.2f" % (sp2[2], sp[2], ratio))
    check(bool(got) and got[0]["nm"] == NAME_B,
          "клиент A знает автора второго пинга по имени",
          "nm=%r" % (got[0]["nm"] if got else None))
    return sp


# =========================================================================
# [3] пинг виден в неразведанной черноте
# =========================================================================

def case_dark(a, b):
    print("\n[3] Пинг виден в НЕРАЗВЕДАННОЙ черноте (решение 8.8)")
    fog = b.js("window.__zza.fog()")
    size = b.js("window.__zza.canvasSize()")
    if not fog:
        note("тумана нет", "пропуск")
        return
    best = None
    for ty in range(fog["h"]):
        for tx in range(fog["w"]):
            if fog["cells"][ty * fog["w"] + tx] != 0:
                continue
            wx, wy = tx + 0.5, ty + 0.5
            s = b.js("window.__zza.worldToScreen(%r, %r)" % (wx, wy))
            if not (150 <= s[0] <= size["w"] - 150 and
                    150 <= s[1] <= size["h"] - 150):
                continue
            sa = a.js("window.__zza.worldToScreen(%r, %r)" % (wx, wy))
            sza = a.js("window.__zza.canvasSize()")
            if not (0 <= sa[0] <= sza["w"] and 0 <= sa[1] <= sza["h"]):
                continue
            best = (wx, wy, s)
            break
        if best:
            break
    if best is None:
        note("неразведанной клетки в кадре обоих нет", "пропуск")
        return
    wx, wy, s = best
    box = [int(s[0] - 70), int(s[1] - 70), 140, 140]
    wait_clear(a, b)                 # эталон черноты снимается на чистом кадре
    aim_at(a, wx, wy)
    before = b.js("window.__zza.pixels(%d,%d,%d,%d,%d)" % tuple(box + [BRIGHT]))
    press_ping(a, "KeyC")            # «опасность» — в темноту именно она
    arrived, _ = wait_for(lambda: b.js("window.__zza.pings()"), 3.0)
    note("что ушло и что пришло",
         "ушло с A %d (последний прицел %s), пришло к B %d"
         % (a.js("window.__zza.pingsSent()"), a.js("window.__zza.lastPingSent()"),
            len(arrived or [])))
    time.sleep(0.25)
    after = b.js("window.__zza.pixels(%d,%d,%d,%d,%d)" % tuple(box + [BRIGHT]))
    print("    КЛЕТКА (%d, %d), ТУМАН = 0 (не открыта), КОРОБКА 140x140 У B:"
          % (int(wx), int(wy)))
    print("      было:  ярких %5d  maxCh %3d  тёмных %5d (%.1f%%)"
          % (before["bright"], before["maxCh"], before["dark"],
             100.0 * before["frac"]))
    print("      стало: ярких %5d  maxCh %3d  тёмных %5d (%.1f%%)"
          % (after["bright"], after["maxCh"], after["dark"],
             100.0 * after["frac"]))
    check(before["bright"] == 0 and before["maxCh"] <= 10,
          "клетка ДО пинга — ровная чернота (5.2 не нарушен)",
          "ярких %d, максимальный канал %d при пороге черноты 10"
          % (before["bright"], before["maxCh"]))
    check(after["bright"] - before["bright"] >= 200,
          "пинг в неразведанной черноте ВИДЕН: его ставил человек, а не сервер",
          "ярких прибавилось %d" % (after["bright"] - before["bright"]))


# =========================================================================
# [4] пинг вне кадра: стрелка и направление
# =========================================================================

def case_arrow(a, b, level):
    print("\n[4] Пинг ВНЕ КАДРА даёт стрелку, и она показывает туда")
    me = b.self_pos()
    size = b.js("window.__zza.canvasSize()")
    # Цель — дальний угол карты: он заведомо за кадром (кадр 20x11 клеток
    # при 64 px, карта 64x48) и заведомо в пределах карты.
    corners = [(1.5, 1.5), (level["w"] - 1.5, 1.5),
               (1.5, level["h"] - 1.5), (level["w"] - 1.5, level["h"] - 1.5)]
    wx, wy = max(corners, key=lambda c: math.hypot(c[0] - me["x"], c[1] - me["y"]))
    s = b.js("window.__zza.worldToScreen(%r, %r)" % (wx, wy))
    check(not (0 <= s[0] <= size["w"] and 0 <= s[1] <= size["h"]),
          "точка пинга действительно ЗА кадром вкладки B",
          "экранные координаты точки (%.0f, %.0f) при кадре %dx%d"
          % (s[0], s[1], size["w"], size["h"]))

    # Кадр должен быть ЧИСТЫМ: живая метка пульсирует, и её дыхание попало
    # бы в «прибавку ярких пикселей» наравне со стрелкой.
    wait_clear(a, b)
    boxes, before = grid_bright(b)
    # Мышью за кадр не ткнуть — идём сырым путём клиента (см. __zza.pingAt).
    ok, tries = ping_raw(a, b, wx, wy, 0)
    check(ok, "пинг за кадром дошёл до B", "попыток %d" % tries)
    wait_for(lambda: b.js("window.__zza.stats()").get("pingsOff", 0) > 0, 3.0)
    time.sleep(0.25)
    st = b.js("window.__zza.stats()")       # ДО обхода сетки: он долгий
    _, after = grid_bright(b)
    check(st.get("pingsOff", 0) >= 1 and st.get("pings", 0) == 0,
          "рендер B считает пинг ВНЕшним и рисует стрелку, а не метку",
          "меток %s, стрелок %s" % (st.get("pings"), st.get("pingsOff")))

    c, weight = centroid(boxes, before, after)
    if c is None:
        check(False, "в кадре B появились пиксели стрелки", "прибавки нет")
        return
    mes = b.js("window.__zza.worldToScreen(%r, %r)" % (me["x"], me["y"]))
    want = ang(s[0] - mes[0], s[1] - mes[1])
    got = ang(c[0] - mes[0], c[1] - mes[1])
    err = ang_err(want, got)
    print("    ИГРОК B НА ЭКРАНЕ (%.0f, %.0f); ПИНГ ЗА КАДРОМ В (%.0f, %.0f)"
          % (mes[0], mes[1], s[0], s[1]))
    print("      центр прибавки ярких пикселей: (%.0f, %.0f), вес %d px"
          % (c[0], c[1], weight))
    print("      направление на пинг %7.1f°, направление на стрелку %7.1f°,"
          " ошибка %.1f°" % (want, got, err))
    # Допуск 20°: сетка замера 64 px даёт на радиусе ~300 px до 6.1°,
    # подпись «N кл» и имя смещены от острия на 15 px поперёк луча — ещё
    # до 2.9°. Сумма 9°, берём вдвое.
    check(err <= 20.0, "стрелка указывает на пинг",
          "ошибка %.1f° при допуске 20°" % err)
    dist = math.hypot(wx - me["x"], wy - me["y"])
    note("расстояние до пинга", "%.1f клетки — оно же подписано у стрелки" % dist)


# =========================================================================
# [5] пинг гаснет
# =========================================================================

def case_fade(a, b, point):
    print("\n[5] Пинг гаснет, и кадр возвращается к прежнему")
    wx, wy = point
    box, _ = box_at(b, wx, wy)
    # Ждём, пока погаснет ВСЁ, и только потом снимаем эталон: живой пинг
    # соседа испортил бы хэш «до».
    wait_clear(a, b)
    aim_at(a, wx, wy)          # facing соседа меняется ЗДЕСЬ, а не в эталоне
    time.sleep(0.45)
    base = b.js("window.__zza.pixels(%d,%d,%d,%d,%d)" % tuple(box + [BRIGHT]))

    t0 = time.time()
    press_ping(a)
    got, dt = wait_for(lambda: b.js("window.__zza.pings()"), 3.0)
    if not got:
        check(False, "пинг для замера срока жизни дошёл",
              "ушло с A %d, последний прицел %s, у A живых %d"
              % (a.js("window.__zza.pingsSent()"),
                 a.js("window.__zza.lastPingSent()"),
                 len(a.js("window.__zza.pings()"))))
        return
    time.sleep(0.3)
    lit = b.js("window.__zza.pixels(%d,%d,%d,%d,%d)" % tuple(box + [BRIGHT]))
    gone, _ = wait_for(lambda: not b.js("window.__zza.pings()"),
                       PING_LIFE_S + 4.0)
    lived = time.time() - t0
    time.sleep(0.5)          # дать кадру отрисоваться уже без метки
    end = b.js("window.__zza.pixels(%d,%d,%d,%d,%d)" % tuple(box + [BRIGHT]))
    print("    ОДНА И ТА ЖЕ КОРОБКА У B, ТРИ МОМЕНТА:")
    print("      до пинга:   ярких %5d  хэш %s" % (base["bright"], base["hash"]))
    print("      с пингом:   ярких %5d  хэш %s" % (lit["bright"], lit["hash"]))
    print("      после:      ярких %5d  хэш %s" % (end["bright"], end["hash"]))
    print("      прожил %.2f с при сроке %.2f с (%d тиков, room.PING_LIFE)"
          % (lived, PING_LIFE_S, room_mod.PING_LIFE))
    check(bool(gone), "пинг погас сам, без чьего-либо участия",
          "прожил %.2f с" % lived)
    # Допуск: срок считается по ТИКАМ сервера, а меряется секундами питона
    # через две границы процесса. 0.6 с — это 18 тиков, чуть больше одного
    # окна повтора (15 тиков): раньше клиент о смерти и не узнает.
    check(abs(lived - PING_LIFE_S) <= 0.6,
          "срок жизни сошёлся с контрактом",
          "%.2f с против %.2f с, допуск 0.60 с" % (lived, PING_LIFE_S))
    check(end["hash"] == base["hash"],
          "кадр после смерти пинга ПОБИТНО тот же, что был до него",
          "хэш %s == %s, ярких %d == %d"
          % (end["hash"], base["hash"], end["bright"], base["bright"]))
    check(lit["hash"] != base["hash"], "а с пингом он был другим",
          "%s != %s" % (lit["hash"], base["hash"]))


# =========================================================================
# [6] спам
# =========================================================================

def case_spam(a, b, point):
    print("\n[6] Спам: вкладка A шлёт пинг КАЖДЫЙ ТИК")
    wx, wy = point
    wait_clear(a, b)
    seen0 = b.js("window.__zza.pingsSeen()")
    frames0 = b.js("window.__zza.app.frames")
    errs0 = len(b.errors())
    t0 = time.time()
    n = 0
    while time.time() - t0 < 2.0:
        a.js("window.__zza.pingAt(%r, %r, 0)" % (wx, wy))
        n += 1
        time.sleep(1.0 / 30)
    dt = time.time() - t0
    time.sleep(0.4)
    seen = b.js("window.__zza.pingsSeen()") - seen0
    live = len(b.js("window.__zza.pings()"))
    frames = b.js("window.__zza.app.frames") - frames0
    print("    ЗА %.2f с ВКЛАДКА A ПОСЛАЛА %d ПИНГОВ (%.1f/с):" % (dt, n, n / dt))
    print("      у B новых пингов %d (%.1f/с), живых сейчас %d, "
          "кадров нарисовано %d" % (seen, seen / dt, live, frames))
    cap = dt / PING_GAP_S + 1
    check(seen <= cap,
          "ограничитель сервера держит: у соседа не больше %.1f новых пингов" % cap,
          "пришло %d при пороге %.1f (вывод: один пинг в %.2f с)"
          % (seen, cap, PING_GAP_S))
    check(live <= room_mod.PING_PER_PLAYER,
          "живых пингов от одного спамера не больше %d" % room_mod.PING_PER_PLAYER,
          "живых %d" % live)
    check(frames > 30, "кадры у соседа продолжают идти",
          "%d кадров за %.2f с" % (frames, dt + 0.4))
    check(len(b.errors()) == errs0, "в консоли соседа не появилось ошибок",
          "ошибок было %d, стало %d" % (errs0, len(b.errors())))


# =========================================================================
# [7] стоимость кадра
# =========================================================================

def case_cost(a, b, point):
    print("\n[7] Стоимость кадра: с пингами и без, спина к спине, со сливом")
    wx, wy = point
    # Набить экран пингами: по PING_PER_PLAYER с каждой вкладки — это и есть
    # потолок для двух игроков (room.PING_PER_PLAYER).
    wait_clear(a, b)
    for i in range(room_mod.PING_PER_PLAYER):
        for t, dx in ((a, -1.5), (b, 1.5)):
            ping_raw(t, b, wx + dx, wy + i * 1.5, i & 1)
        time.sleep(PING_GAP_S + 0.12)
    time.sleep(0.3)
    live = len(b.js("window.__zza.pings()"))
    st_on = b.js("(() => { window.__zza.setPingDraw(true);"
                 " const m = window.__zza.benchDrawFlush(40);"
                 " return {ms: m, st: window.__zza.stats()}; })()")
    st_off = b.js("(() => { window.__zza.setPingDraw(false);"
                  " const m = window.__zza.benchDrawFlush(40);"
                  " return {ms: m, st: window.__zza.stats()}; })()")
    on, off = [], []
    for _ in range(5):
        on.append(b.js("(() => { window.__zza.setPingDraw(true);"
                       " return window.__zza.benchDrawFlush(40); })()"))
        off.append(b.js("(() => { window.__zza.setPingDraw(false);"
                        " return window.__zza.benchDrawFlush(40); })()"))
    b.js("window.__zza.setPingDraw(true)")
    mon, moff = statistics.median(on), statistics.median(off)
    size = b.js("window.__zza.canvasSize()")
    print("    КАДР %dx%d, ЖИВЫХ ПИНГОВ %d, 40 КАДРОВ НА ЗАМЕР, 5 ЗАМЕРОВ:"
          % (size["w"], size["h"], live))
    print("      с пингами: %s" % "  ".join("%.3f" % v for v in on))
    print("      без:       %s" % "  ".join("%.3f" % v for v in off))
    print("      медиана с пингами %.3f мс, без %.3f мс, разница %+.3f мс"
          % (mon, moff, mon - moff))
    print("      drawCalls с пингами %d, без %d (на пинг %.1f)"
          % (st_on["st"]["drawCalls"], st_off["st"]["drawCalls"],
             (st_on["st"]["drawCalls"] - st_off["st"]["drawCalls"]) / max(1, live)))
    uptime()
    print("    ЭТО SWIFTSHADER В КОНТЕЙНЕРЕ. Число говорит только об ОТНОШЕНИИ")
    print("    двух замеров одной сцены; fps целевой машины отсюда не берётся.")
    check(live >= 1, "пинги на экране во время замера есть", "живых %d" % live)
    # Порог выведен: метка это 6 команд канвы на пинг против ~200 команд
    # кадра (сущности, тайлы, туман, виньетка). Даже вчетверо дороже
    # ожидаемого это меньше 15%.
    check(mon <= moff * 1.15 + 0.05,
          "пинги не удорожают кадр больше чем на 15%",
          "%.3f против %.3f мс, отношение %.3f" % (mon, moff, mon / max(1e-9, moff)))
    if live:
        print("      цена одного пинга: %+.4f мс" % ((mon - moff) / live))


# =========================================================================
# [8] тот же пинг во втором бэкенде (7.1: бэкендов два, игра одна)
# =========================================================================

def case_3d(a, b, point):
    print("\n[8] Пинг в трёхмерном бэкенде (7.1)")
    wait_clear(a, b)
    b.js("window.__zza.setBackend('3d')")
    try:
        b.page.wait_for_function("window.__zza.getBackend()==='3d'", timeout=20000)
    except Exception as e:
        check(False, "трёхмерный бэкенд поднялся", str(e)[:80])
        return
    time.sleep(1.0)
    note("бэкенд вкладки B", b.js("window.__zza.backendName()"))
    pt, sb = clean_spot(a, b, point, (a, b))
    if pt is None:
        pt, sb = point, b.js("window.__zza.worldToScreen(%r, %r)" % point)
    wx, wy = pt
    box = [int(sb[0] - 90), int(sb[1] - 90), 180, 180]
    before = b.js("window.__zza.pixels(%d,%d,%d,%d,%d)" % tuple(box + [BRIGHT]))
    ok, tries = ping_raw(a, b, wx, wy, 1)
    time.sleep(0.4)
    after = b.js("window.__zza.pixels(%d,%d,%d,%d,%d)" % tuple(box + [BRIGHT]))
    st = b.js("window.__zza.stats()")
    print("    КОРОБКА 180x180 ВОКРУГ ТОЧКИ, ЭКРАН B В 3D:")
    print("      было:  ярких %5d  maxCh %3d" % (before["bright"], before["maxCh"]))
    print("      стало: ярких %5d  maxCh %3d" % (after["bright"], after["maxCh"]))
    check(ok, "пинг дошёл до трёхмерной вкладки", "попыток %d" % tries)
    check(after["bright"] - before["bright"] >= 200,
          "в трёхмерном кадре метка НАРИСОВАНА",
          "ярких прибавилось %d, рендер отчитался pings=%s"
          % (after["bright"] - before["bright"], st.get("pings")))
    # стрелка в 3D
    me = b.self_pos()
    lvl = a.js("window.__zza.level()")
    corners = [(1.5, 1.5), (lvl["w"] - 1.5, 1.5), (1.5, lvl["h"] - 1.5),
               (lvl["w"] - 1.5, lvl["h"] - 1.5)]
    fx, fy = max(corners, key=lambda c: math.hypot(c[0] - me["x"], c[1] - me["y"]))
    wait_clear(a, b)
    boxes, gb = grid_bright(b)
    ok2, _ = ping_raw(a, b, fx, fy, 0)
    time.sleep(0.4)
    st2 = b.js("window.__zza.stats()")      # ДО обхода сетки: он долгий
    _, ga = grid_bright(b)
    c, weight = centroid(boxes, gb, ga)
    check(ok2 and st2.get("pingsOff", 0) >= 1,
          "пинг за кадром в 3D тоже даёт стрелку",
          "меток %s, стрелок %s" % (st2.get("pings"), st2.get("pingsOff")))
    if c is not None:
        mes = b.js("window.__zza.worldToScreen(%r, %r)" % (me["x"], me["y"]))
        s2 = b.js("window.__zza.worldToScreen(%r, %r)" % (fx, fy))
        want = ang(s2[0] - mes[0], s2[1] - mes[1])
        got = ang(c[0] - mes[0], c[1] - mes[1])
        print("      направление на пинг %7.1f°, на стрелку %7.1f°, ошибка %.1f°"
              % (want, got, ang_err(want, got)))
        check(ang_err(want, got) <= 20.0, "стрелка в 3D указывает на пинг",
              "ошибка %.1f° при допуске 20°" % ang_err(want, got))
    b.js("window.__zza.setBackend('2d')")
    try:
        b.page.wait_for_function("window.__zza.getBackend()==='2d'", timeout=10000)
    except Exception:
        pass


def forbidden_in_hot_path():
    """7.2: ни shadowBlur, ни filter, ни globalCompositeOperation."""
    print("\n[9] Запрещённого 7.2 в рисовании пингов нет")
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "static", "render2d.js"),
        encoding="utf-8").read()
    i = src.find("_drawPings(view, now)")
    j = src.find("_drawSelfHud(view)")
    part = src[src.find("_pingBadge(cx"):j] if i > 0 else ""
    bad = [w for w in ("shadowBlur", "filter", "globalCompositeOperation")
           if w in part]
    check(i > 0 and not bad,
          "в горячем пути пингов нет shadowBlur/filter/globalCompositeOperation",
          "проверено %d символов кода, найдено: %s" % (len(part), bad or "ничего"))


def main():
    only = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--case="):
            only = arg.split("=", 1)[1]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("нет пакета playwright: pip install playwright "
              "(БЕЗ playwright install — браузер лежит в /opt/pw-browsers)")
        return 2

    print("=== приёмка пингов на карте (8.8): два браузера ===")
    uptime()
    print("  срок жизни пинга %.2f с (%d тиков), ограничитель %.2f с "
          "(%d тиков) — числа берутся из server/room.py, а не переписаны"
          % (PING_LIFE_S, room_mod.PING_LIFE, PING_GAP_S, room_mod.PING_GAP))

    # Врагов выключаем: стенд меряет ПИНГИ, а не выживание. Одинокий ходок
    # под обстрелом гибнет, становится духом, и красной делается исправная
    # проверка исправной игры (правило про ZZA_ENEMIES).
    with Server(env={"ZZA_ENEMIES": "0"}) as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM, headless=True,
                                     args=BROWSER_ARGS)
        a = Tab(browser, "A").open(srv.url)
        b = Tab(browser, "B").open(srv.url)
        room = a.create_room(NAME_A)
        b.join_room(NAME_B, room)
        a.ready()
        b.ready()
        a.wait_game()
        b.wait_game()
        time.sleep(0.8)
        level = a.js("window.__zza.level()")
        d = bring_together(a, b, level)
        note("вкладки в игре", "комната %s, между игроками %.2f клетки" % (room, d))

        point = pick_point(a, b)
        if point is None:
            check(False, "нашлась точка, видная обеим вкладкам",
                  "между игроками %.2f клетки" % d)
            return summary()
        note("точка пинга", "(%.2f, %.2f) в клетках" % point)

        case_see(a, b, point)
        if only == "see":
            print()
            uptime()
            return summary()
        case_author(a, b, point)
        case_dark(a, b)
        case_arrow(a, b, level)
        case_fade(a, b, point)
        case_spam(a, b, point)
        case_cost(a, b, point)
        case_3d(a, b, point)
        forbidden_in_hot_path()

        print()
        for t, nm in ((a, "A"), (b, "B")):
            errs = t.errors()
            check(not errs, "в консоли вкладки %s нет ошибок" % nm,
                  "; ".join(errs[:3]) if errs else "ошибок нет")
        a.close()
        b.close()

    print()
    uptime()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
