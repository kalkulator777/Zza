#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Общая обвязка проверок клиента (этап 0c).

Поднимает НАСТОЯЩИЙ play.py отдельным процессом (а не сервер в этом же
интерпретаторе): проверяется то же, что запустит человек, включая раздачу
static/ и WebSocket через tornado.

Браузер — headless Chromium из /opt/pw-browsers (PLAYWRIGHT_BROWSERS_PATH).
`playwright install` не запускать: на целевой машине интернета нет, браузер
кладётся рядом.

Здесь же — печать `uptime` рядом с любым замером: без загрузки машины
числа нельзя истолковать (CLAUDE.md, правило 8).
"""

import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMIUM = os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")

# --disable-gpu-vsync снимает привязку rAF к «монитору» контейнера, но НЕ
# снимает потолок кадров: получается ровный поток ~60 fps, на котором
# пейсинг кадров не шумит. --disable-frame-rate-limit брать нельзя: rAF
# тогда зовётся чаще, чем обновляется его же timestamp, соседние кадры
# получают одинаковое время, и любой замер по времени вырождается.
BROWSER_ARGS = ["--use-gl=swiftshader", "--disable-gpu-vsync"]

FAILS = []


def check(ok, title, detail=""):
    print(("  ЗЕЛЕНО  " if ok else "  КРАСНО  ") + title +
          (("  | " + detail) if detail else ""))
    if not ok:
        FAILS.append(title)
    return ok


def note(title, detail=""):
    print("  данные   " + title + (("  | " + detail) if detail else ""))


def uptime():
    try:
        out = subprocess.check_output(["uptime"], text=True).strip()
    except Exception as e:
        out = "uptime недоступен: %s" % e
    print("  uptime:  " + out)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server(object):
    """Настоящий play.py в отдельном процессе."""

    def __init__(self, port=None):
        self.port = port or free_port()
        self.proc = None
        self.log = ""

    def __enter__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "play.py", "--port", str(self.port), "--no-browser"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        # ждём, пока порт начнёт отвечать (2.6: старт должен быть быстрым)
        t0 = time.time()
        while time.time() - t0 < 10:
            if self.proc.poll() is not None:
                raise RuntimeError("play.py умер на старте")
            try:
                s = socket.create_connection(("127.0.0.1", self.port), 0.2)
                s.close()
                print("  сервер:  play.py на порту %d, поднялся за %.2f с"
                      % (self.port, time.time() - t0))
                return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("play.py не поднялся за 10 с")

    def __exit__(self, *a):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.log = self.proc.communicate(timeout=5)[0] or ""
            except Exception:
                self.proc.kill()
        return False

    @property
    def url(self):
        return "http://127.0.0.1:%d/" % self.port


class Tab(object):
    """Одна вкладка браузера, играющая в игру через настоящий интерфейс.

    Никаких обходных путей: кнопки нажимаются, клавиши нажимаются. Из
    страницы читаются только показания через window.__zza (это крючок для
    проверок, игровая логика через него не идёт).
    """

    def __init__(self, browser, label, width=1280, height=720):
        self.label = label
        self.console_errors = []
        self.page_errors = []
        self.ctx = browser.new_context(viewport={"width": width, "height": height})
        self.page = self.ctx.new_page()
        self.page.on("console", self._console)
        self.page.on("pageerror", lambda e: self.page_errors.append(str(e)))

    def _console(self, m):
        if m.type in ("error",):
            self.console_errors.append(m.text)

    def open(self, url):
        self.page.goto(url)
        self.page.wait_for_function("window.__zza !== undefined", timeout=10000)
        return self

    def js(self, expr):
        return self.page.evaluate(expr)

    def screen(self):
        return self.js("window.__zza.screen()")

    def create_room(self, name):
        self.page.fill("#name", name)
        self.page.click("#createBtn")
        self.page.wait_for_function("window.__zza.screen()==='lobby'", timeout=10000)
        return self.js("window.__zza.room()")

    def join_room(self, name, code):
        self.page.fill("#name", name)
        self.page.fill("#code", code)
        self.page.click("#joinBtn")
        self.page.wait_for_function("window.__zza.screen()==='lobby'", timeout=10000)
        return self.js("window.__zza.room()")

    def ready(self):
        self.page.click("#readyBtn")

    def wait_game(self, timeout=10000):
        self.page.wait_for_function("window.__zza.screen()==='game'", timeout=timeout)
        # ждём первый снапшот со своей сущностью
        self.page.wait_for_function("window.__zza.self() !== null", timeout=timeout)

    def hold(self, keys, seconds):
        for k in keys:
            self.page.keyboard.down(k)
        time.sleep(seconds)
        for k in keys:
            self.page.keyboard.up(k)

    def self_pos(self):
        return self.js("window.__zza.self()")

    def others(self):
        return self.js("window.__zza.others()")

    def errors(self):
        return self.console_errors + ["pageerror: " + e for e in self.page_errors]

    def close(self):
        try:
            self.ctx.close()
        except Exception:
            pass


# --- разбор карты: направление, в котором точно есть чистый разбег --------

DIRS = [
    ("вправо", (1, 0), ["KeyD"]),
    ("вниз", (0, 1), ["KeyS"]),
    ("вверх", (0, -1), ["KeyW"]),
    ("влево", (-1, 0), ["KeyA"]),
    ("вправо-вниз", (0.7071, 0.7071), ["KeyD", "KeyS"]),
    ("вправо-вверх", (0.7071, -0.7071), ["KeyD", "KeyW"]),
    ("влево-вниз", (-0.7071, 0.7071), ["KeyA", "KeyS"]),
    ("влево-вверх", (-0.7071, -0.7071), ["KeyA", "KeyW"]),
]
R_PLAYER = 0.35


def _blocked(level, x, y):
    w, h, tiles = level["w"], level["h"], level["tiles"]
    for ty in (int((y - R_PLAYER) // 1), int((y + R_PLAYER) // 1)):
        for tx in (int((x - R_PLAYER) // 1), int((x + R_PLAYER) // 1)):
            if tx < 0 or ty < 0 or tx >= w or ty >= h:
                return True
            if tiles[ty * w + tx] == 0:       # TILE_WALL
                return True
    return False


def clear_dist(level, x, y, dx, dy, limit=20.0):
    """Сколько клеток можно пробежать из (x,y) в направлении (dx,dy)."""
    step = 0.1
    d = 0.0
    while d < limit:
        d += step
        if _blocked(level, x + dx * d, y + dy * d):
            return d - step
    return limit


def pick_run(level, x, y, need):
    """Направление с чистым разбегом не меньше need клеток."""
    best = None
    for title, (dx, dy), keys in DIRS:
        d = clear_dist(level, x, y, dx, dy, need + 2.0)
        if best is None or d > best[3]:
            best = (title, (dx, dy), keys, d)
        if d >= need:
            return (title, (dx, dy), keys, d)
    return best


def summary():
    print()
    if FAILS:
        print("КРАСНО: %d" % len(FAILS))
        for f in FAILS:
            print("   * " + f)
        return 1
    print("ЗЕЛЕНО: всё сошлось")
    return 0
