#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZZA ARENA — запуск одной командой.

    python3 play.py

Поднимает локальный сервер, открывает браузер и сам находит игры,
запущенные коллегами в той же локальной сети. Отдельного «сервера» нет:
у кого открыто меню — тот и может создать игру, остальные к ней подключаются.
"""

import argparse
import os
import socket
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import tornado.ioloop
except ImportError:
    sys.stderr.write(
        "\n  Не найден tornado.\n"
        "  Установите его одним из способов:\n"
        "      pip3 install --user tornado\n"
        "      sudo apt install python3-tornado\n\n")
    sys.exit(1)

from server.app import make_app                       # noqa: E402
from server.discovery import Discovery, local_ips     # noqa: E402
from server.rooms import RoomManager                  # noqa: E402

DEFAULT_PORT = 8777


def pick_port(preferred):
    for p in [preferred] + list(range(preferred, preferred + 25)):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise SystemExit("Не нашлось свободного порта рядом с %d" % preferred)


def open_browser(url):
    for name in ("firefox", "firefox-esr", None):
        try:
            b = webbrowser.get(name) if name else webbrowser.get()
            b.open_new(url)
            return True
        except Exception:
            continue
    try:
        webbrowser.open_new(url)
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="ZZA ARENA — файтинг для локальной сети")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="порт (по умолчанию 8777)")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    ap.add_argument("--no-discovery", action="store_true", help="выключить поиск игр по сети")
    ap.add_argument("--nick", default=os.environ.get("USER") or "Игрок", help="имя игрока по умолчанию")
    args = ap.parse_args()

    port = pick_port(args.port)
    mgr = RoomManager()

    disco = None
    if not args.no_discovery:
        disco = Discovery(port, mgr.list_open, args.nick)

    app = make_app(port, mgr, disco)
    app.listen(port, address="0.0.0.0")

    if disco is not None:
        disco.start()

    ips = local_ips()
    url = "http://127.0.0.1:%d/" % port
    print("")
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║              Z Z A   A R E N A               ║")
    print("  ╚══════════════════════════════════════════════╝")
    print("  Открыто здесь :  %s" % url)
    for ip in ips:
        print("  Для коллег    :  http://%s:%d/" % (ip, port))
    if disco is not None and disco.enabled:
        print("  Поиск игр     :  включён (UDP 48777)")
    elif disco is not None:
        print("  Поиск игр     :  ОТКЛЮЧЁН (%s) — подключайтесь по адресу вручную" % disco.error)
    print("  Остановить    :  Ctrl+C")
    print("")

    if not args.no_browser:
        open_browser(url)

    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        print("\n  Пока.")
    finally:
        if disco is not None:
            disco.stop()


if __name__ == "__main__":
    main()
