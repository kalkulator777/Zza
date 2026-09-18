#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка этапа 0c, пункт 1: ДВА БРАУЗЕРА РЕАЛЬНО ИГРАЮТ.

Поднимает настоящий play.py и открывает две вкладки headless Chromium.
Обе проходят настоящий путь человека: имя -> создать/войти по коду ->
«Готов» -> игра. Дальше каждая жмёт свои клавиши, и проверяется главное:
КАЖДАЯ ВИДИТ СУЩНОСТЬ ДРУГОЙ НА СВОЁМ ЭКРАНЕ, в тех же координатах.

Заодно:
  * стены держат (бег в стену не выносит за карту и не залипает);
  * список комнат в меню показывает созданную комнату;
  * в консоли браузера нет ошибок ни на одном экране — меню, лобби, игра.

Запуск:
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/client_two_browsers.py
Выход: 0 — зелено, 1 — красно.
"""

import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client_common import (BROWSER_ARGS, CHROMIUM, Server, Tab, check,   # noqa: E402
                           clear_dist, note, summary, uptime)

# Допуск на совпадение координат между экранами.
#
# Откуда: оба клиента рисуют мир с задержкой 2 тика (DESIGN.md 6.1) от
# ОДНОГО И ТОГО ЖЕ потока снапшотов, то есть систематического сдвига между
# ними нет — остаётся только разъезд часов рендера. Часы догоняют цель
# темпом не быстрее ±10 % (RATE_CLAMP в state.js), и разойтись они могут не
# больше чем на джиттер доставки, который в петле — доли тика. Берём один
# полный тик бега: 5.0 кл/с / 30 Гц = 0.167 клетки, и удваиваем на джиттер
# петли и разное время съёма показаний из двух вкладок.
POS_TOL = 0.34


def find_by_id(lst, eid):
    for e in lst:
        if e["id"] == eid:
            return e
    return None


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("нет пакета playwright: pip install playwright "
              "(БЕЗ playwright install — браузер лежит в /opt/pw-browsers)")
        return 2

    print("=== этап 0c, пункт 1: два браузера играют вместе ===")
    uptime()

    with Server() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM, headless=True,
                                     args=BROWSER_ARGS)
        a = Tab(browser, "A").open(srv.url)
        b = Tab(browser, "B").open(srv.url)

        check(a.screen() == "menu" and b.screen() == "menu",
              "обе вкладки открыли меню", "A=%s B=%s" % (a.screen(), b.screen()))

        # --- комната ---------------------------------------------------
        room = a.create_room("Аня")
        check(bool(room) and len(room) == 4, "A создала комнату", "код %s" % room)

        # список комнат в меню второй вкладки должен её увидеть
        b.page.click("#refreshBtn")
        time.sleep(0.4)
        listing = b.js("document.getElementById('roomlist').textContent")
        check(room in listing, "список комнат в меню показывает новую комнату",
              listing.strip()[:80])

        room_b = b.join_room("Борис", room)
        check(room_b == room, "B вошла в ту же комнату по коду", "B в %s" % room_b)

        players = a.js("document.getElementById('players').textContent")
        check("Аня" in players and "Борис" in players,
              "лобби у A показывает обоих игроков", " ".join(players.split()))

        # --- старт: без ready не придёт ни level, ни снапшотов (5.1) ----
        a.ready()
        time.sleep(0.3)
        check(a.screen() == "lobby",
              "одной готовности мало — игра ещё не началась", "A на экране " + a.screen())
        b.ready()
        a.wait_game()
        b.wait_game()
        check(a.screen() == "game" and b.screen() == "game",
              "обе готовы — игра началась у обеих")

        sa0, sb0 = a.self_pos(), b.self_pos()
        check(sa0 is not None and sb0 is not None and sa0["id"] != sb0["id"],
              "каждая вкладка опознала свою сущность",
              "A id=%s, B id=%s" % (sa0 and sa0["id"], sb0 and sb0["id"]))

        # --- движение: расходятся в разные стороны ---------------------
        a.hold(["KeyD"], 1.0)      # A вправо

        # Пока B бежит, A обязана видеть её движение, а не замерший силуэт:
        # снимаем два кадра прямо посреди бега.
        b.page.keyboard.down("KeyS")
        time.sleep(0.3)
        seen1 = find_by_id(a.others(), sb0["id"])
        time.sleep(0.5)
        seen2 = find_by_id(a.others(), sb0["id"])
        b.page.keyboard.up("KeyS")
        moved = (seen1 and seen2 and
                 math.hypot(seen2["x"] - seen1["x"], seen2["y"] - seen1["y"]))
        check(bool(moved) and moved > 1.0,
              "A видит движение B прямо во время бега, а не застывший силуэт",
              "чужая сущность проехала %.3f клетки за 0.5 с" % (moved or 0.0))

        time.sleep(0.4)            # дать снапшотам и интерполяции устояться

        sa, sb = a.self_pos(), b.self_pos()
        note("A видит себя", "id=%d  x=%.3f  y=%.3f" % (sa["id"], sa["x"], sa["y"]))
        note("B видит себя", "id=%d  x=%.3f  y=%.3f" % (sb["id"], sb["x"], sb["y"]))

        check(abs(sa["x"] - sa0["x"]) > 2.0,
              "A действительно сдвинулась от нажатия клавиш",
              "x: %.3f -> %.3f" % (sa0["x"], sa["x"]))
        check(abs(sb["y"] - sb0["y"]) > 2.0,
              "B действительно сдвинулась от нажатия клавиш",
              "y: %.3f -> %.3f" % (sb0["y"], sb["y"]))

        # --- главное: каждая видит другого -----------------------------
        b_sees_a = find_by_id(a.others(), sb["id"])      # в мире A ищем сущность B
        a_sees_b = find_by_id(b.others(), sa["id"])      # в мире B ищем сущность A

        ok = b_sees_a is not None
        check(ok, "на экране A есть сущность игрока B",
              ("A видит B в x=%.3f y=%.3f" % (b_sees_a["x"], b_sees_a["y"])) if ok
              else "сущности B в мире A нет")
        ok2 = a_sees_b is not None
        check(ok2, "на экране B есть сущность игрока A",
              ("B видит A в x=%.3f y=%.3f" % (a_sees_b["x"], a_sees_b["y"])) if ok2
              else "сущности A в мире B нет")

        if ok and ok2:
            d_b = math.hypot(b_sees_a["x"] - sb["x"], b_sees_a["y"] - sb["y"])
            d_a = math.hypot(a_sees_b["x"] - sa["x"], a_sees_b["y"] - sa["y"])
            print()
            print("  КООРДИНАТЫ ИЗ ОБОИХ БРАУЗЕРОВ (клетки):")
            print("    вкладка A (Аня,  ent %d): свой   x=%.3f y=%.3f" % (sa["id"], sa["x"], sa["y"]))
            print("    вкладка A:                чужой  x=%.3f y=%.3f (ent %d)" % (b_sees_a["x"], b_sees_a["y"], b_sees_a["id"]))
            print("    вкладка B (Борис, ent %d): свой   x=%.3f y=%.3f" % (sb["id"], sb["x"], sb["y"]))
            print("    вкладка B:                чужой  x=%.3f y=%.3f (ent %d)" % (a_sees_b["x"], a_sees_b["y"], a_sees_b["id"]))
            print("    расхождение B-глазами-A: %.3f кл,  A-глазами-B: %.3f кл,  допуск %.2f" % (d_b, d_a, POS_TOL))
            print()
            check(d_b <= POS_TOL, "A видит B там же, где B видит себя",
                  "расхождение %.3f клетки" % d_b)
            check(d_a <= POS_TOL, "B видит A там же, где A видит себя",
                  "расхождение %.3f клетки" % d_a)
            check(math.hypot(sa["x"] - sb["x"], sa["y"] - sb["y"]) > 2.0,
                  "игроки разошлись, а не стоят в одной точке",
                  "между ними %.3f клетки" % math.hypot(sa["x"] - sb["x"], sa["y"] - sb["y"]))

        # --- стены держат ----------------------------------------------
        # Сколько клеток влево можно пройти, считаем ПО КАРТЕ, которую
        # прислал сервер: так проверка не зависит от того, какой генератор
        # этажа стоит сегодня.
        level = a.js("window.__zza.level()")
        before = a.self_pos()
        # Берём ближайшую стену по осям: так проверка и быстрая, и точная.
        wall = None
        for title, (wdx, wdy), wkeys in (("влево", (-1, 0), ["KeyA"]),
                                         ("вправо", (1, 0), ["KeyD"]),
                                         ("вверх", (0, -1), ["KeyW"]),
                                         ("вниз", (0, 1), ["KeyS"])):
            d = clear_dist(level, before["x"], before["y"], wdx, wdy, 18.0)
            if d >= 1.5 and d < 18.0 and (wall is None or d < wall[3]):
                wall = (title, (wdx, wdy), wkeys, d)
        if wall is None:
            check(False, "нашлась стена, в которую можно упереться")
        else:
            title, (wdx, wdy), wkeys, want = wall
            a.hold(wkeys, want / 5.0 + 1.0)       # бег 5 кл/с (4.2) + запас
            time.sleep(0.4)
            after = a.self_pos()
            went = (after["x"] - before["x"]) * wdx + (after["y"] - before["y"]) * wdy
            drift = abs((after["x"] - before["x"]) * wdy - (after["y"] - before["y"]) * wdx)
            check(after["x"] >= 1.30 and after["y"] >= 1.30 and
                  after["x"] <= level["w"] - 1.30 and after["y"] <= level["h"] - 1.30,
                  "A не вышла за рамку карты",
                  "x=%.3f y=%.3f, рамка + радиус = 1.35" % (after["x"], after["y"]))
            check(abs(went - want) < 0.30,
                  "стена остановила A ровно там, где она нарисована",
                  "бег %s: прошла %.3f клетки, по карте свободно %.3f"
                  % (title, went, want))
            check(drift < 0.2, "бег в стену не увёл по другой оси",
                  "снос %.3f клетки" % drift)

        # --- отладочный оверлей ----------------------------------------
        hud = a.js("document.getElementById('hud').textContent")
        note("оверлей отладки", " ".join(hud.split()))
        check(all(w in hud for w in ("fps", "задержка", "сущностей", "тик")),
              "оверлей отладки показывает fps, задержку, сущностей и тик")

        # --- консоль ----------------------------------------------------
        for tab in (a, b):
            errs = tab.errors()
            check(not errs, "в консоли вкладки %s нет ошибок (меню, лобби, игра)" % tab.label,
                  "; ".join(errs[:3]) if errs else "0 сообщений уровня error")

        # --- флаг OFFLINE (4.3, бит 2) ----------------------------------
        # Закрываем вкладку B: её сущность обязана остаться в мире A, но
        # с битом OFFLINE, чтобы напарник гас, а не стоял столбом.
        b.close()
        t0 = time.time()
        off = None
        while time.time() - t0 < 3.0:
            off = find_by_id(a.others(), sb["id"])
            if off is not None and (off["flags"] & 2):
                break
            time.sleep(0.1)
        check(off is not None, "сущность отвалившегося игрока не исчезла мгновенно")
        check(off is not None and (off["flags"] & 2) != 0,
              "A видит у B флаг OFFLINE (бит 2) и гасит его",
              "flags=%s" % (off and off["flags"]))

        note("трафик", "A: снапшотов %d, тик сервера %d"
             % (a.js("window.__zza.snaps()"), a.js("window.__zza.tick()")))
        uptime()

        a.close()
        browser.close()

    return summary()


if __name__ == "__main__":
    sys.exit(main())
