#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка лобби в ДВУХ БРАУЗЕРАХ (DESIGN.md 8.7) и HUD набора (11.7).

Ничего не делается в обход интерфейса: кнопки нажимаются, поля
заполняются, второй игрок находит лобби в СПИСКЕ и входит кнопкой из
списка, а не по заранее известному коду. Из страницы читаются только
показания через window.__zza.

Что здесь меряется:

  [1] два браузера собираются в лобби и начинают игру настоящими кнопками;
  [2] параметры, поставленные в лобби, доходят до мира (сид и сложность —
      числом на экране игрока);
  [3] не-хозяин не может ни менять параметры, ни стартовать — и не может
      даже мимо интерфейса, сырым сообщением;
  [4] HUD набора апгрейдов верен, ДАЖЕ ЕСЛИ событие pick до клиента не
      дошло (11.7: набор приходит сообщением build, а не из событий).

Враги выключены (ZZA_ENEMIES=0, DESIGN.md 10): здесь меряется лобби и
набор, а не выживание одинокого ходока по дороге к алтарю.

Запуск:  python3 tests/client_lobby_two.py
Выход:   0 — зелено, 1 — красно.
"""

import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

from tests.client_common import (BROWSER_ARGS, CHROMIUM, Keys, Server,  # noqa: E402
                                 Tab, TILE_STAIRS, check, note, summary,
                                 uptime, walk_to)

# Сид, который ставится в лобби руками. Любое число из разрешённых; важно
# ровно одно — что в игре окажется ИМЕННО ОНО, а не то, что сервер выдумал
# сам при создании комнаты.
SEED = 4242
DIFF_EASY = 0            # «Прогулка» — room.DIFF_HP[0]
HP_EASY = 140            # room.DIFF_HP[0]; расходятся — красное, и правильно

# Предметы на алтаре: список kind берётся У СЕРВЕРА (items.ITEM_KINDS, 4.3),
# а не переписывается диапазоном. Диапазон 6..9 был верен ровно до пятого
# апгрейда: его kind — 11 (десятка занята боссом), и «предметы — это 6..9»
# молча перестало бы находить треть алтаря.
from server import items as items_mod                                # noqa: E402
ITEM_KINDS = set(items_mod.ITEM_KINDS)

WALK_BUDGET = 90.0       # ПРЕДОХРАНИТЕЛЬ, не порог: дорога кончается по
                         # приходу или по отсутствию прогресса (10).


def find_items(tab):
    return [e for e in tab.others() if e["kind"] in ITEM_KINDS]


def stairs_cells(level):
    w, h, t = level["w"], level["h"], level["tiles"]
    return [(i % w, i // w) for i in range(w * h) if t[i] == TILE_STAIRS]


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("нет пакета playwright: pip install playwright "
              "(БЕЗ playwright install — браузер лежит в /opt/pw-browsers)")
        return 2

    print("=== лобби на двоих: список, параметры, права хозяина, набор ===")
    uptime()

    with Server(env={"ZZA_ENEMIES": "0"}) as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM, headless=True,
                                     args=BROWSER_ARGS)
        a = Tab(browser, "A").open(srv.url)
        b = Tab(browser, "B").open(srv.url)

        # --- [1] хозяин создаёт лобби --------------------------------------
        print("\n[1] Один создаёт лобби, другой находит его в СПИСКЕ")
        room = a.create_room("Аня")
        check(bool(room) and len(room) == 4, "A создала лобби кнопкой",
              "код %s" % room)
        check(a.js("window.__zza.isHost()"),
              "создатель лобби — хозяин (8.7)",
              "host=%s, свой pid=%s"
              % (a.js("window.__zza.lobby().host"), a.js("window.__zza.pid()")))

        # B НЕ жмёт «обновить»: список обновляется сам, пока человек в меню.
        t0 = time.time()
        b.page.wait_for_function(
            "code => document.getElementById('roomlist').textContent.includes(code)",
            arg=room, timeout=10000)
        listing = b.js("document.getElementById('roomlist').textContent")
        check(True, "B увидел лобби в списке САМ, без нажатия «обновить»",
              "через %.1f с: %s" % (time.time() - t0, " ".join(listing.split())))
        row = b.js("window.__zza.rooms()")
        r0 = row[0] if row else {}
        check(r0.get("id") == room and r0.get("mode") == "descent"
              and r0.get("players") == 1 and r0.get("phase") == "lobby",
              "в списке видно код, режим, число игроков и что игра ещё не идёт",
              "%s" % (r0,))

        b.page.fill("#name", "Борис")
        b.page.click("#roomlist .room:has-text('%s') button" % room)
        b.page.wait_for_function("window.__zza.screen()==='lobby'", timeout=10000)
        check(b.js("window.__zza.room()") == room,
              "B вошёл КНОПКОЙ ИЗ СПИСКА, а не по набранному коду",
              "B в лобби %s" % b.js("window.__zza.room()"))
        check(not b.js("window.__zza.isHost()"),
              "вошедший вторым хозяином не стал")

        a.page.wait_for_function("window.__zza.lobby().players.length===2",
                                 timeout=5000)
        pl = a.js("document.getElementById('players').textContent")
        check("Аня" in pl and "Борис" in pl and "хозяин" in pl,
              "в лобби у A виден состав, готовность каждого и хозяин",
              " ".join(pl.split()))

        # --- [2] права хозяина ---------------------------------------------
        print("\n[2] Параметры и старт — только у хозяина (8.7)")
        dis = b.js("['optSeed','optDiff','optMax','optFF','optJoin','optSeedBtn']"
                   ".map(i => document.getElementById(i).disabled)")
        check(all(dis), "у не-хозяина параметры в интерфейсе не трогаются",
              "disabled: %s" % dis)
        check(b.js("document.getElementById('startBtn').style.display") == 'none',
              "кнопки «Начать игру» у не-хозяина нет вовсе")

        seed_before = b.js("window.__zza.lobby().opts.seed")
        # Мимо интерфейса — как сделал бы кривой или злой клиент.
        b.js("window.__zza.sendOpts({seed: 999999, diff: 2, max_players: 6})")
        b.js("window.__zza.sendStart(true)")
        time.sleep(0.5)
        lob = b.js("window.__zza.lobby()")
        check(lob["opts"]["seed"] == seed_before and lob["opts"]["diff"] != 2,
              "сырое сообщение от НЕ-хозяина сервер не принял",
              "сид %s (был %s), сложность %s"
              % (lob["opts"]["seed"], seed_before, lob["opts"]["diff"]))
        check(a.screen() == "lobby" and b.screen() == "lobby"
              and not lob["armed"],
              "не-хозяин не начал игру", "обе вкладки на экране лобби")
        check("хозя" in (b.js("window.__zza.status()") or "").lower(),
              "не-хозяину сказали, почему отказ",
              b.js("window.__zza.status()"))

        # мусор от хозяина сервер тоже не берёт
        a.js("window.__zza.sendOpts({mode:'siege', seed:'не-число', diff:99})")
        time.sleep(0.4)
        oa = a.js("window.__zza.lobby().opts")
        check(oa["mode"] == "descent" and oa["diff"] != 99
              and isinstance(oa["seed"], int),
              "негодные значения сервер не принял и у ХОЗЯИНА",
              "режим %s, сложность %s, сид %s"
              % (oa["mode"], oa["diff"], oa["seed"]))

        # --- [3] хозяин ставит параметры настоящими элементами -------------
        print("\n[3] Хозяин ставит сид и сложность — оба видят одно и то же")
        a.page.fill("#optSeed", str(SEED))
        a.page.click("#optSeedBtn")
        a.page.select_option("#optDiff", str(DIFF_EASY))
        b.page.wait_for_function("s => window.__zza.lobby().opts.seed === s",
                                 arg=SEED, timeout=5000)
        ob = b.js("window.__zza.lobby().opts")
        check(ob["seed"] == SEED and ob["diff"] == DIFF_EASY,
              "не-хозяин увидел чужой выбор сразу, из сообщения сервера",
              "сид %s, сложность %s" % (ob["seed"], ob["diff"]))
        check(b.js("document.getElementById('optSeed').value") == str(SEED),
              "и увидел его в тех же полях, что у хозяина")

        # --- [4] старт кнопкой хозяина -------------------------------------
        print("\n[4] Готовность и старт")
        b.ready()
        time.sleep(0.4)
        check(a.screen() == "lobby" and b.screen() == "lobby",
              "готовности одного гостя мало — игра не началась",
              "A=%s B=%s" % (a.screen(), b.screen()))
        pa = a.js("window.__zza.lobby().players")
        check(any(p[1] == "Борис" and p[2] for p in pa),
              "хозяин видит, что гость отметился", "%s" % (pa,))

        a.page.click("#startBtn")          # кнопка хозяина, 8.7
        a.wait_game()
        b.wait_game()
        check(a.screen() == "game" and b.screen() == "game",
              "хозяин нажал «Начать игру» — игра началась у обоих")

        # --- [5] параметры доехали до мира ---------------------------------
        print("\n[5] Параметры лобби доехали до мира")
        la = a.js("window.__zza.level()")
        lb = b.js("window.__zza.level()")
        note("уровень у A", "сид %s, %dx%d" % (la["seed"], la["w"], la["h"]))
        check(la["seed"] == SEED and lb["seed"] == SEED,
              "сгенерировался ИМЕННО поставленный сид, а не случайный",
              "A видит сид %s, B видит %s, ставили %s"
              % (la["seed"], lb["seed"], SEED))
        check(la["tiles"] == lb["tiles"],
              "оба играют на одной и той же карте",
              "%d тайлов совпали" % len(la["tiles"]))
        me_a = a.js("window.__zza.me()")
        me_b = b.js("window.__zza.me()")
        note("здоровье", "A %d/%d, B %d/%d"
             % (me_a["hp"], me_a["hpMax"], me_b["hp"], me_b["hpMax"]))
        check(me_a["hpMax"] == HP_EASY and me_b["hpMax"] == HP_EASY,
              "сложность «%s» дошла до мира: запас здоровья %d вместо 100"
              % ("Прогулка", HP_EASY),
              "A hp_max=%d, B hp_max=%d" % (me_a["hpMax"], me_b["hpMax"]))

        # --- [6] HUD набора при ПОТЕРЯННОМ событии pick (11.7) -------------
        print("\n[6] HUD набора: событие pick потеряно, набор всё равно верен")
        check(a.js("window.__zza.myBuild()").count(0) == items_mod.N_UP,
              "в начале забега набор пуст, и клиент это знает из build",
              "ups=%s" % a.js("window.__zza.myBuild()"))

        # Клиент ПЕРЕСТАЁТ получать события вовсе: 5.2 разрешает ev теряться,
        # и HUD, собранный из ev, здесь бы и соврал.
        a.js("window.__zza.setDropEv(true)")
        evs_before = len(a.js("window.__zza.evLog()"))

        items = find_items(a)
        me = a.self_pos()
        note("алтарь", "предметов на этаже %d" % len(items))
        if not items:
            check(False, "алтарь этажа нашёлся в снапшоте")
            return summary()
        items.sort(key=lambda e: math.hypot(e["x"] - me["x"], e["y"] - me["y"]))
        tgt = items[0]
        cell = (int(tgt["x"]), int(tgt["y"]))
        note("идём к предмету", "kind %d в клетке %s, по прямой %.1f клетки"
             % (tgt["kind"], cell,
                math.hypot(tgt["x"] - me["x"], tgt["y"] - me["y"])))

        keys = Keys(a)
        got, dist, secs, nudges = walk_to(a, la, cell, keys, WALK_BUDGET,
                                          avoid=stairs_cells(la), near=0.55)
        keys.release()
        note("дорога", "дошёл=%s, осталось %.2f клетки, %.1f с, дёрганий %d"
             % (got, dist, secs, nudges))
        check(got, "ходок дошёл до предмета", "осталось %.2f клетки" % dist)

        a.page.keyboard.down("KeyQ")       # Q — взять предмет (5.1, бит 16)
        try:
            # Ждём ЧИСЛО, которое меряем (набор у клиента), а не событие,
            # которое его обычно приносит (10). Не дождались — это красное,
            # а не трассировка: упавшая проверка не говорит ничего.
            a.page.wait_for_function(
                "() => window.__zza.myBuild().some(n => n > 0)", timeout=6000)
        except Exception:
            pass
        a.page.keyboard.up("KeyQ")

        build = a.js("window.__zza.myBuild()") or []
        hud = a.js("window.__zza.buildHud()")
        evs = a.js("window.__zza.evLog()")
        picks = [e for e in evs if e.get("k") == "pick"]
        note("после подбора", "ups=%s, HUD %r, событий у клиента %d (было %d)"
             % (build, hud, len(evs), evs_before))
        check(sum(build) == 1,
              "набор пришёл сообщением build и сложился", "ups=%s" % build)
        check(not picks,
              "события pick клиент не видел ВООБЩЕ — и это не помешало",
              "событий вида pick у клиента %d" % len(picks))
        check(hud.startswith("набор:") and len(hud) > len("набор: "),
              "HUD в игре показывает, что собрано", "%r" % hud)
        names = a.js("window.__zza.lobby().limits.ups")
        took = names[build.index(max(build))] if sum(build) else None
        check(bool(took) and took in hud,
              "в HUD написан ТОТ апгрейд, который взят",
              "взят %r, в HUD %r" % (took, hud))

        # --- [7] консоль -----------------------------------------------------
        print("\n[7] Консоль")
        check(not a.errors(), "в консоли вкладки A нет ошибок",
              "; ".join(a.errors())[:200] or "0 сообщений уровня error")
        check(not b.errors(), "в консоли вкладки B нет ошибок",
              "; ".join(b.errors())[:200] or "0 сообщений уровня error")

        a.close()
        b.close()
    uptime()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
