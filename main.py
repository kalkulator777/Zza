#!/usr/bin/env python3
"""Рисовашки — игра в рисование для локальной сети.

Запуск: python3 main.py
Откроется браузер, дальше всё в нём: создать лобби или найти чужое.
"""

import argparse
import logging
import os
import socket
import sys
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# tornado лежит папкой рядом с main.py — интернета на машинах нет, ставить нечем
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    import tornado.ioloop
except ImportError:
    sys.stderr.write(
        "\nНе найден tornado.\n"
        "Положите папку tornado рядом с main.py (или установите пакет) и запустите снова.\n\n"
    )
    raise SystemExit(1)

from app.discovery import DiscoveryResponder
from app.rooms import RoomManager
from app.server import make_app

PORT_RANGE = range(8770, 8790)


def pick_port(preferred=None):
    """Первый свободный порт: на одной машине может быть запущено несколько копий."""
    candidates = [preferred] if preferred else list(PORT_RANGE)
    for port in candidates:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    raise SystemExit("Не нашёл свободный порт в диапазоне %s" % (candidates,))


def main():
    parser = argparse.ArgumentParser(description="Рисовашки")
    parser.add_argument("--port", type=int, default=None, help="занять конкретный порт")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--quiet", action="store_true", help="меньше логов")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    port = pick_port(args.port)
    manager = RoomManager()
    manager.set_port(port)
    app = make_app(manager, BASE_DIR)
    app.listen(port)

    responder = DiscoveryResponder(manager, port)
    responder.start()

    url = "http://127.0.0.1:%d/" % port
    print("\n  Рисовашки запущены: %s" % url)
    print("  Для остальных в сети: %s:%d" % (manager.local_ip(), port))
    print("  Остановить — Ctrl+C\n")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            print("  Браузер не открылся сам — откройте ссылку выше вручную.")

    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        print("\n  Пока!")


if __name__ == "__main__":
    main()
