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

import collections
import math
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
    """Настоящий play.py в отдельном процессе.

    env — ДОБАВКА к окружению процесса, а не замена ему. По умолчанию пусто,
    то есть поведение ровно такое, каким было: чужие проверки, которые зовут
    Server() без аргументов, получают тот же сервер, что и раньше.

    Зачем это есть: server/ai.py читает ZZA_ENEMIES при импорте, и стенду,
    который меряет не выживание, а что-нибудь другое (туман в кадре,
    плавность), враги только мешают — одинокий ходок под обстрелом гибнет,
    и красной становится исправная проверка исправной игры. Выключатель
    стоит в сервере и ставится стендом осознанно, по одному месту на стенд.
    """

    def __init__(self, port=None, env=None):
        self.port = port or free_port()
        self.proc = None
        self.log = ""
        self.env = dict(env) if env else {}

    def __enter__(self):
        penv = None
        if self.env:
            penv = dict(os.environ)
            penv.update(self.env)
            print("  сервер:  окружение " +
                  ", ".join("%s=%s" % kv for kv in sorted(self.env.items())))
        self.proc = subprocess.Popen(
            [sys.executable, "play.py", "--port", str(self.port), "--no-browser"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=penv)
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

TILE_WALL, TILE_FLOOR, TILE_STAIRS = 0, 1, 2

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
            if tiles[ty * w + tx] == TILE_WALL:
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


OPP_KEY = {"KeyW": "KeyS", "KeyS": "KeyW", "KeyA": "KeyD", "KeyD": "KeyA"}


def ensure_runway(tab, level, need, speed=5.0):
    """Встать так, чтобы впереди было не меньше need клеток чистого бега.

    На карте из комнат и коридоров (этап 1) разбег может не влезть в
    комнату целиком, зато влезает, если сначала отойти к дальней стене.
    Возвращает (название, (dx,dy), клавиши, чистая длина).
    """
    me = tab.self_pos()
    best = pick_run(level, me["x"], me["y"], need)
    if best[3] >= need:
        return best
    title, (dx, dy), keys, _d = best
    back = clear_dist(level, me["x"], me["y"], -dx, -dy, need)
    if back > 0.2:
        tab.hold([OPP_KEY[k] for k in keys], back / speed + 0.25)
        time.sleep(0.25)
    me = tab.self_pos()
    return pick_run(level, me["x"], me["y"], need)


# --- ходьба по карте (общая для client_fog.py и client_combat.py) ---------
#
# Переехало сюда из client_fog.py целиком, вместе с заработанными прогонами
# поправками. Второй копии этого кода быть не должно: он весь состоит из
# лечения настоящих граблей физики, и разойдись копии — вторая проверка
# начнёт мигать там, где первая уже вылечена.

WALK_STALL = 8.0               # с без сокращения пути — дальше незачем

# Полоса нечувствительности по поперечной оси. Тело игрока круглое, R=0.35
# (4.2), коридор шириной ровно в клетку: от середины можно уйти на 0.15
# клетки, дальше край тела цепляет соседний ряд и physics.move_circle
# обнуляет скорость по этой оси. Поэтому клавиша поперечной оси жмётся по
# АБСОЛЮТНОМУ промаху, а не по отношению к продольному: первый вариант
# сравнивал оси между собой, при промахе 0.18 против хода 0.63 поперечную
# клавишу не жал — и персонаж стоял в проёме вечно (замерено).
BAND = 0.10


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


def bfs_path(level, src, dst, avoid=()):
    """Кратчайший путь по проходимым тайлам (всё, что не 0 — не стена, 4.1).

    avoid — клетки, на которые ходить нельзя, хотя стеной они не являются.
    Ровно одна такая клетка есть на каждом этаже: ЛЕСТНИЦА. Дойти до неё —
    значит сменить этаж (room.stairs_ready), а проверка, которая меняет под
    собой этаж, меряет уже не то, что начинала мерить.
    """
    w, h, t = level["w"], level["h"], level["tiles"]
    avoid = set(avoid) - {src, dst}
    prev = {src: None}
    q = collections.deque([src])
    while q:
        c = q.popleft()
        if c == dst:
            break
        x, y = c
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and t[ny * w + nx] != TILE_WALL \
                    and (nx, ny) not in prev and (nx, ny) not in avoid:
                prev[(nx, ny)] = c
                q.append((nx, ny))
    if dst not in prev:
        # Обход не нашёлся — например, лестница стоит в единственном
        # коридоре. Тогда идём как придётся: пусть лучше проверка пройдёт
        # рядом с лестницей, чем встанет намертво.
        if avoid:
            return bfs_path(level, src, dst)
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
    круглое (R_PLAYER выше).
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


def walk_to(tab, level, target, keys, budget, probe=None, enough=None,
            avoid=(), near=0.9):
    """Идти к клетке target. Возвращает (дошёл, расстояние, секунды, рывков).

    ЧТО ЗДЕСЬ ПОРОГ, А ЧТО ПРЕДОХРАНИТЕЛЬ. Дорога кончается по одному из
    трёх: дошли; путь не сокращается WALK_STALL секунд; вызванный
    снаружи enough() сказал «нужное уже снято». budget — только
    предохранитель от зависания. Это и есть лечение мигания: ни одно из
    условий не спрашивает, УСПЕЛА ли машина за отведённое время.

    probe() зовётся по дороге (снять то, что видно только в пути),
    enough() — можно ли уже разворачиваться.

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
        if probe is not None:
            probe()
        if enough is not None and enough():
            keys.release()
            return True, dist, time.time() - t0, nudges
        me = tab.self_pos()
        if me is None:
            break
        mx, my = me["x"], me["y"]
        dist = math.hypot(mx - (target[0] + 0.5), my - (target[1] + 0.5))
        if dist < near:
            keys.release()
            return True, dist, time.time() - t0, nudges
        path = bfs_path(level, (int(mx), int(my)), target, avoid)
        if not path:
            break
        if len(path) < best:
            best = len(path)
            last_gain = time.time()
        if time.time() - last_gain > WALK_STALL:
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


def summary():
    print()
    if FAILS:
        print("КРАСНО: %d" % len(FAILS))
        for f in FAILS:
            print("   * " + f)
        return 1
    print("ЗЕЛЕНО: всё сошлось")
    return 0
