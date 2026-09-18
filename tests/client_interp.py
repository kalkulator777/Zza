#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка этапа 0c, пункты 2 и 3: интерполяция даёт плавность, и проверка
этой плавности умеет краснеть.

Что меряется. Клиент при равномерном беге записывает позицию своего
персонажа на каждом кадре. По записи считаются два числа:

  шаг_i      = |p_i - p_{i-1}|              (как просит приёмка)
  скорость_i = |p_i - p_{i-1}| / dt_i       (то же, но поделённое на длину
                                             самого кадра)

ПОРОГ СТАВИТСЯ НА ВТОРОЕ, и вот почему. Сырое отношение max/median шага
смешивает две разные вещи: качество интерполяции и пейсинг кадров
браузера. Если браузер выдал один кадр вдвое длиннее соседних (в этом
контейнере софтверный рендер, такое бывает), персонаж честно проедет за
него вдвое больше — и отношение станет 2, хотя интерполяция идеальна.
Деление на dt убирает пейсинг и оставляет ровно то, о чём говорит
DESIGN.md 6.1. Сырое отношение печатается рядом как данные.

И ЕЩЁ ОДНА ПОПРАВКА, ЗАРАБОТАННАЯ ПРОГОНОМ. Знаменателем берётся не
медиана, а нижний квартиль. Причина найдена красным прогоном пункта 3:
при ровно двух кадрах на тик (60 fps поверх 30 Гц) у СЛОМАННОГО клиента
нулевых кадров ровно половина, медиана садится на ненулевой шаг, и
отношение max/median выходит 1.01 — то есть «зелёное» на насмерть рваной
картинке. Нижний квартиль в этом случае равен нулю, и отношение честно
уходит в бесконечность. При работающей интерполяции квартиль и медиана
различаются меньше чем на процент, так что порог от этого не меняется.

ОТКУДА ПОРОГ 2.0 (не подобран под прогон):
  * при работающей интерполяции показанная скорость = скорость мира,
    умноженная на темп часов рендера. Темп в state.js зажат в ±10 %
    (RATE_CLAMP), значит худшее отношение max/median = 1.10/0.90 = 1.22;
  * при выключенной интерполяции кадры между тиками дают ноль. На 60 fps
    поверх 30 Гц нулевых кадров половина, на целевых 100 fps — две трети;
    медиана падает в ноль, отношение уходит в бесконечность, и даже если
    медиана случайно попадёт на ненулевой шаг, отношение будет не меньше
    числа кадров на тик, то есть >= 2 на 60 fps и >= 3.3 на 100 fps;
  * 2.0 — геометрическая середина между 1.22 и 3.33, запас в обе стороны
    в 1.6 раза.

Второй критерий, прямое прочтение 6.1: доля кадров с НУЛЕВЫМ шагом. При
работающей интерполяции во время равномерного бега таких кадров не должно
быть вовсе (порог 5 % — на случай, если запись зацепила момент остановки).

Пункт 3 приёмки («проверка умеет краснеть») выполняется прямо здесь: тот
же замер повторяется с выключенной интерполяцией (рисуем последний
снапшот как есть), и проверка обязана его завалить.

Запуск:
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/client_interp.py
Выход: 0 — зелено, 1 — красно.
"""

import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client_common import (BROWSER_ARGS, CHROMIUM, OPP_KEY, Server, Tab,   # noqa: E402
                           check, ensure_runway, note, summary, uptime)

FRAMES = 100          # столько кадров записывает приёмка
SETTLE_S = 0.4        # разгон до равномерной скорости перед записью
RATIO_MAX = 2.0       # см. вывод в шапке
ZERO_MAX = 0.05
NEED_CELLS = 11.0     # 0.4 c разгона + 100 кадров при 60 fps = 10.3 клетки + запас


def measure(samples):
    steps, speeds = [], []
    for i in range(1, len(samples)):
        t0, x0, y0 = samples[i - 1]
        t1, x1, y1 = samples[i]
        dt = (t1 - t0) / 1000.0
        d = math.hypot(x1 - x0, y1 - y0)
        steps.append(d)
        speeds.append(d / dt if dt > 0 else 0.0)
    dur = (samples[-1][0] - samples[0][0]) / 1000.0
    med_s = statistics.median(steps)
    med_v = statistics.median(speeds)
    q1_v = sorted(speeds)[max(0, int(len(speeds) * 0.25) - 1)]
    return {
        "n": len(samples),
        "dur": dur,
        "fps": (len(samples) - 1) / dur if dur > 0 else 0.0,
        "step_ratio": (max(steps) / med_s) if med_s > 0 else float("inf"),
        "speed_ratio": (max(speeds) / med_v) if med_v > 0 else float("inf"),
        "q_ratio": (max(speeds) / q1_v) if q1_v > 0 else float("inf"),
        "speed_med": med_v,
        "zero": sum(1 for d in steps if d < 1e-9) / len(steps),
        "dist": math.hypot(samples[-1][1] - samples[0][1],
                           samples[-1][2] - samples[0][2]),
    }


def _f(v):
    return "inf" if v == float("inf") else "%.2f" % v


def fmt(m):
    return ("кадров %d за %.2f с (%.1f fps), проехал %.2f кл;\n"
            "      max/median шага      = %s   (как просит приёмка)\n"
            "      max/median скорости  = %s   (шаг, делённый на длину кадра)\n"
            "      max/кв25  скорости   = %s   <- ПОРОГ СТАВИТСЯ СЮДА\n"
            "      кадров с нулевым шагом %.1f%%"
            % (m["n"], m["dur"], m["fps"], m["dist"],
               _f(m["step_ratio"]), _f(m["speed_ratio"]), _f(m["q_ratio"]),
               m["zero"] * 100))


def record(tab, keys, frames):
    for k in keys:
        tab.page.keyboard.down(k)
    try:
        time.sleep(SETTLE_S)
        tab.js("window.__zza.record(%d)" % frames)
        t0 = time.time()
        while tab.js("window.__zza.recording()") and time.time() - t0 < 20:
            time.sleep(0.05)
        s = tab.js("window.__zza.samples()")
    finally:
        for k in keys:
            tab.page.keyboard.up(k)
    return s


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("нет пакета playwright: pip install playwright "
              "(БЕЗ playwright install — браузер лежит в /opt/pw-browsers)")
        return 2

    print("=== этап 0c, пункты 2 и 3: интерполяция (DESIGN.md 6.1) ===")
    uptime()

    with Server() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM, headless=True,
                                     args=BROWSER_ARGS)
        t = Tab(browser, "A").open(srv.url)
        room = t.create_room("Замер")
        t.ready()
        t.wait_game()
        print("  комната %s, игра пошла" % room)

        # Задержка рендера (6.1). «Два тика» — это ВЕРХНИЙ край, а не
        # постоянная величина, и вот почему: интерполяция идёт между
        # снапшотами tick-2 и tick-1, значит показанное состояние отстаёт
        # от свежего снапшота ровно на 1..2 тика, в среднем на 1.5 (33..67
        # мс, в среднем 50). Усредняем по секунде: на редкой выборке пила
        # даёт алиасинг и среднее гуляет.
        time.sleep(0.5)
        lags = []
        for _ in range(40):
            lags.append(t.js("window.__zza.tick() - window.__zza.app.world.renderTick"))
            time.sleep(0.025)
        lag = statistics.mean(lags)
        note("задержка рендера", "среднее %.2f тика (%.0f мс), разброс %.2f..%.2f"
             % (lag, lag * 1000.0 / 30, min(lags), max(lags)))
        check(1.2 <= lag <= 2.4,
              "клиент рисует между снапшотами tick-2 и tick-1 (6.1)",
              "среднее отставание %.2f тика; теория даёт пилу 1..2, среднее 1.5"
              % lag)

        # Ищем направление, в котором точно есть чистый разбег: в комнате
        # есть столбы, и упереться в столб посреди замера — это испортить
        # замер, а не поймать баг.
        level = t.js("window.__zza.level()")
        title, (dx, dy), keys, dist = ensure_runway(t, level, NEED_CELLS)
        me = t.self_pos()
        note("разбег", "%s от (%.2f, %.2f): чисто %.1f клетки (нужно %.1f)"
             % (title, me["x"], me["y"], dist, NEED_CELLS))
        if dist < NEED_CELLS:
            check(False, "нашёлся чистый разбег для равномерного бега",
                  "максимум %.1f клетки" % dist)
            return summary()

        # --- пункт 2: интерполяция включена ----------------------------
        t.js("window.__zza.setInterp(true)")
        on = measure(record(t, keys, FRAMES))
        print()
        print("  ИНТЕРПОЛЯЦИЯ ВКЛЮЧЕНА: " + fmt(on))
        uptime()
        check(on["q_ratio"] <= RATIO_MAX,
              "плавность: max/кв25 скорости в пределах порога",
              "%s <= %.2f" % (_f(on["q_ratio"]), RATIO_MAX))
        check(on["zero"] <= ZERO_MAX,
              "ни один кадр не стоит на месте при равномерном беге",
              "нулевых кадров %.1f%% <= %.0f%%" % (on["zero"] * 100, ZERO_MAX * 100))
        check(on["dist"] > NEED_CELLS * 0.4,
              "замер сделан на настоящем равномерном беге, а не на месте",
              "проехал %.2f клетки" % on["dist"])

        # --- пункт 3: то же с выключенной интерполяцией -----------------
        # Возвращаемся к стене-независимой точке: бежим обратно тем же
        # путём, чтобы разбег снова был чистым.
        t.js("window.__zza.setInterp(false)")
        t.hold([OPP_KEY[k] for k in keys], SETTLE_S + on["dur"] + 0.2)
        time.sleep(0.3)
        title2, _, keys2, dist2 = ensure_runway(t, level, NEED_CELLS)
        me2 = t.self_pos()
        note("разбег для красного прогона", "%s от (%.2f, %.2f): чисто %.1f клетки"
             % (title2, me2["x"], me2["y"], dist2))
        off = measure(record(t, keys2, FRAMES))
        print()
        print("  ИНТЕРПОЛЯЦИЯ ВЫКЛЮЧЕНА: " + fmt(off))
        uptime()

        bad_ratio = off["q_ratio"] > RATIO_MAX
        bad_zero = off["zero"] > ZERO_MAX
        why = []
        if bad_ratio:
            why.append("max/кв25 = %s > %.1f" % (_f(off["q_ratio"]), RATIO_MAX))
        if bad_zero:
            why.append("нулевых кадров %.1f%% > %.0f%%" % (off["zero"] * 100, ZERO_MAX * 100))
        if not why:
            why.append("НИ ОДИН критерий не сработал — проверка бесполезна")
        check(bad_ratio or bad_zero,
              "проверка плавности КРАСНЕЕТ на выключенной интерполяции",
              "; ".join(why))
        note("для сравнения", "заявленное приёмкой max/median шага на сломанном "
             "клиенте = %s — само по себе оно НЕ ловит поломку при 2 кадрах "
             "на тик" % _f(off["step_ratio"]))

        # вернуть как было
        t.js("window.__zza.setInterp(true)")
        check(t.js("window.__zza.getInterp()") is True,
              "интерполяция возвращена во включённое состояние")

        errs = t.errors()
        check(not errs, "в консоли нет ошибок за весь замер",
              "; ".join(errs[:3]) if errs else "0 сообщений уровня error")

        t.close()
        browser.close()

    print()
    print("  Порог %.1f выведен из RATE_CLAMP=±10%% в state.js (худшее "
          "1.10/0.90=1.22)" % RATIO_MAX)
    print("  и из числа кадров на тик при отсутствии интерполяции "
          "(>=2 на 60 fps, >=3.3 на 100 fps).")
    print("  Знаменатель — нижний квартиль, а не медиана: при ровно двух кадрах")
    print("  на тик медиана сломанного клиента садится на ненулевой шаг и даёт 1.0.")
    return summary()


if __name__ == "__main__":
    sys.exit(main())
