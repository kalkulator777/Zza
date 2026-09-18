#!/usr/bin/env python3
"""
Функциональная проверка стендов рендера bench/bench2d.html и
bench/bench3d.html (DESIGN.md, раздел 10 «Проверки»).

Не меряет fps (число из этого контейнера — мусор, см. DESIGN.md 2.1/CLAUDE.md).
Проверяет только то, что можно проверить без реального GPU целевой машины:

  1. страница грузится без ошибок навигации;
  2. канва ненулевого размера и совпадает с запрошенным разрешением;
  3. цикл кадров действительно идёт — счётчик window.__bench.frames растёт
     между двумя замерами (а не просто нарисован один раз и встал);
  4. в консоли браузера нет сообщений уровня error и не было necaught
     page-ошибок.

bench2d.html — классический скрипт, открывается напрямую через file://
(именно так его откроет человек двойным щелчком).

bench3d.html — ES-модуль (three.js), а Chromium запрещает
`<script type="module">` из file:// по CORS (проверено: без сервера
получаем "Cross origin requests are only supported for protocol schemes:
...", хотя сеть тут ни при чём — это чисто политика браузера). Поэтому
для него этот скрипт сам поднимает игрушечный HTTP-сервер на 127.0.0.1
(loopback, без интернета) на время проверки и глушит его в конце.
Об этом же — предупреждение в bench/README.md.

Запуск:
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/bench_pages.py

Требует пакет playwright (`pip install playwright`, БЕЗ `playwright
install` — браузер уже лежит в /opt/pw-browsers/chromium, скачивать не
нужно и некуда на целевой машине). Если playwright недоступен, скрипт
прямо об этом говорит и завершается с ненулевым кодом, а не притворяется
зелёным.
"""

import http.server
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = REPO_ROOT / "bench"
CHROMIUM = os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium")

FAIL = []
OK = []


def report(name, ok, detail=""):
    (OK if ok else FAIL).append(name)
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


def start_local_server():
    """Игрушечный статик-сервер на loopback — только чтобы обойти CORS
    Chromium для type=module. Никакой игровой логики, никакого интернета."""
    os.chdir(REPO_ROOT)
    handler = http.server.SimpleHTTPRequestHandler
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def check_page(playwright, url, want_w, want_h, label):
    """Возвращает True/False, печатает по одной строке на под-проверку."""
    console_errors = []
    page_errors = []

    browser = playwright.chromium.launch(
        executable_path=CHROMIUM,
        headless=True,
        args=["--use-gl=swiftshader"],
    )
    try:
        page = browser.new_page(viewport={"width": want_w, "height": want_h})
        page.on(
            "console",
            lambda m: console_errors.append(m.text) if m.type == "error" else None,
        )
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        nav_ok = True
        nav_detail = ""
        try:
            page.goto(url, wait_until="load", timeout=20000)
        except Exception as e:  # noqa: BLE001 — репортим любую причину как FAIL
            nav_ok = False
            nav_detail = str(e)
        report(f"{label}: страница загрузилась", nav_ok, nav_detail)
        if not nav_ok:
            return False

        # канва ненулевого размера и нужного разрешения
        size = page.evaluate(
            "() => { const c = document.getElementById('c');"
            " return c ? [c.width, c.height] : null; }"
        )
        size_ok = bool(size) and size[0] == want_w and size[1] == want_h and size[0] > 0 and size[1] > 0
        report(f"{label}: канва {want_w}x{want_h}", size_ok, f"получено {size}")

        # цикл кадров идёт: счётчик растёт между двумя замерами
        f1 = page.evaluate("() => window.__bench ? window.__bench.frames : -1")
        page.wait_for_timeout(1200)
        f2 = page.evaluate("() => window.__bench ? window.__bench.frames : -1")
        loop_ok = f1 >= 0 and f2 > f1
        report(f"{label}: цикл кадров идёт (счётчик тиков растёт)", loop_ok, f"{f1} -> {f2}")

        # без ошибок в консоли
        console_ok = len(console_errors) == 0 and len(page_errors) == 0
        detail = ""
        if not console_ok:
            detail = "console: " + "; ".join(console_errors[:5]) + " | pageerror: " + "; ".join(page_errors[:5])
        report(f"{label}: в консоли нет ошибок", console_ok, detail)

        page.close()
        return nav_ok and size_ok and loop_ok and console_ok
    finally:
        browser.close()


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "FAIL: пакет playwright не установлен в этом окружении "
            "(pip install playwright; браузер уже есть в /opt/pw-browsers, "
            "`playwright install` запускать не нужно)."
        )
        return 2

    if not os.path.exists(CHROMIUM):
        print(f"FAIL: не найден chromium по пути {CHROMIUM}")
        return 2

    httpd, port = start_local_server()
    try:
        print(f"(uptime рядом с замером — параллельная нагрузка портит числа)")
        subprocess.run(["uptime"], check=False)

        with sync_playwright() as p:
            ok2d = check_page(
                p,
                f"file://{BENCH_DIR}/bench2d.html?n=200&q=low",
                1920, 1080,
                "bench2d.html (file://)",
            )
            ok3d = check_page(
                p,
                f"http://127.0.0.1:{port}/bench/bench3d.html?n=200&q=low",
                1920, 1080,
                "bench3d.html (http://127.0.0.1, только чтобы обойти CORS ESM)",
            )
    finally:
        httpd.shutdown()

    print()
    print(f"итого: OK={len(OK)} FAIL={len(FAIL)}")
    if FAIL:
        print("провалились:")
        for name in FAIL:
            print("  - " + name)
        return 1
    print("всё зелёное.")
    return 0 if (ok2d and ok3d) else 1


if __name__ == "__main__":
    sys.exit(main())
