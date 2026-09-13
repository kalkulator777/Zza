#!/usr/bin/env python3
"""ZZA — сетевые гонки в локальной сети.

Запуск:
    python3 main.py

Поднимает сервер и открывает браузер. Остальным игрокам копия папки не нужна:
они просто заходят по адресу, который скрипт напечатает, — меню, лобби и сама
гонка целиком в браузере.

Полезные ключи:
    --port 8000        другой порт
    --no-browser       не открывать браузер (например, для выделенной машины)
    --host 127.0.0.1   слушать только себя
    --snapshot-hz 20   реже слать состояние (если сеть внезапно окажется узкой)
"""

import argparse
import json
import logging
import os
import socket
import sys

VERSION = "1.0"
BASE = os.path.dirname(os.path.abspath(__file__))

# Tornado ищем сначала рядом с собой, потом в системе. На машинах без интернета
# положить папку tornado рядом с main.py — самый простой способ его поставить.
for extra in (BASE, os.path.join(BASE, "vendor"), os.path.join(BASE, "lib")):
    if os.path.isdir(extra) and extra not in sys.path:
        sys.path.insert(0, extra)

try:
    import tornado
    import tornado.ioloop
except ImportError:
    sys.stderr.write(
        "\n  Не найден Tornado.\n\n"
        "  Положите папку 'tornado' рядом с main.py (или в подпапку vendor/),\n"
        "  либо установите его: pip3 install tornado\n\n"
        f"  Искал в: {BASE}, {os.path.join(BASE, 'vendor')}, "
        f"{os.path.join(BASE, 'lib')} и в системных путях.\n\n")
    sys.exit(1)

sys.path.insert(0, BASE)

from server.app import make_app          # noqa: E402
from server.hub import Hub               # noqa: E402
from server.records import Records       # noqa: E402
from server.track import load_tracks     # noqa: E402


def local_ips():
    """Все адреса, по которым до нас, скорее всего, достучатся из локалки.

    Показываем списком, а не угадываем один: на машине может быть и Wi-Fi, и
    кабель, и виртуальные интерфейсы.
    """
    found = []

    # Основной маршрут наружу. Пакетов не шлём, соединение UDP не устанавливается.
    for probe in ("10.255.255.255", "192.168.1.1", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.2)
            s.connect((probe, 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip not in found:
                found.append(ip)
                break
        except OSError:
            pass
        finally:
            s.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except (OSError, socket.gaierror):
        pass

    return found


def banner(ips, port, tracks, warnings):
    line = "=" * 64
    print("\n" + line)
    print(f"  ZZA {VERSION} — гонки в локальной сети")
    print(line)
    if ips:
        print("\n  Играть с других компьютеров:\n")
        for ip in ips:
            print(f"      http://{ip}:{port}")
    else:
        print("\n  Не удалось определить адрес в сети.")
        print("  Посмотрите его командой:  ip -4 addr")
    print(f"\n  На этом компьютере:  http://localhost:{port}")
    print(f"\n  Трасс загружено: {len(tracks)}    Python: {sys.version.split()[0]}"
          f"    Tornado: {tornado.version}")
    for w in warnings:
        print(f"\n  ! {w}")
    print("\n  Остановить: Ctrl+C")
    print(line + "\n")


def open_browser(port):
    import threading
    import webbrowser

    def go():
        try:
            webbrowser.open(f"http://localhost:{port}")
        except Exception:  # noqa: BLE001 — без браузера сервер всё равно работает
            pass

    threading.Timer(0.7, go).start()


def main():
    ap = argparse.ArgumentParser(description="ZZA — сетевые гонки")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--hz", type=int, default=60, help="частота симуляции")
    ap.add_argument("--snapshot-hz", type=int, default=30, help="частота снапшотов")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    warnings = []
    if sys.version_info < (3, 8):
        warnings.append(f"Python {sys.version.split()[0]} — нужен 3.8 или новее")

    tracks, errors = load_tracks(os.path.join(BASE, "shared", "tracks"))
    for e in errors:
        warnings.append(f"трасса не загрузилась — {e}")
    if not tracks:
        sys.stderr.write("\n  Нет ни одной рабочей трассы в shared/tracks — играть не на чем.\n\n")
        return 1

    with open(os.path.join(BASE, "shared", "physics.json"), encoding="utf-8") as f:
        phys = json.load(f)

    records = Records(os.path.join(BASE, "data", "records.json"))
    hub = Hub(tracks, phys, os.path.join(BASE, "shared", "powerups.json"),
              records, hz=args.hz, snapshot_hz=args.snapshot_hz)

    app = make_app(hub, BASE, VERSION, debug=args.debug)
    try:
        app.listen(args.port, address=args.host)
    except OSError as exc:
        sys.stderr.write(
            f"\n  Не удалось занять порт {args.port}: {exc}\n\n"
            "  Либо игра уже запущена, либо порт занят другой программой.\n"
            f"  Попробуйте другой:  python3 main.py --port {args.port + 1}\n\n")
        return 1

    hub.start()
    ips = local_ips()
    banner(ips, args.port, tracks, warnings)

    if not args.no_browser:
        open_browser(args.port)

    loop = tornado.ioloop.IOLoop.current()
    try:
        loop.start()
    except KeyboardInterrupt:
        print("\n  Останавливаюсь...")
    finally:
        hub.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
