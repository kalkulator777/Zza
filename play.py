#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zza — точка входа.

Запуск:  python3 play.py  [--port 8765] [--no-browser]

Поднимает сервер, печатает все локальные адреса машины (чтобы хост мог
продиктовать коллеге) и пытается открыть браузер.
"""

import argparse
import asyncio
import errno
import os
import socket
import sys
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "vendor"))   # tornado лежит рядом
sys.path.insert(0, ROOT)

DEFAULT_PORT = 8765


def local_ips():
    """Все IPv4-адреса машины, кроме петли. Без внешних зависимостей."""
    ips = []

    def add(ip):
        if ip and not ip.startswith("127.") and ip not in ips:
            ips.append(ip)

    # 1. штатный путь: адрес, с которого ушёл бы пакет наружу
    for probe in ("8.8.8.8", "192.168.1.1", "10.0.0.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 9))
            add(s.getsockname()[0])
        except Exception:
            pass
        finally:
            s.close()

    # 2. все интерфейсы через ioctl (Linux; Astra — Linux)
    try:
        import fcntl
        import struct
        for _idx, name in socket.if_nameindex():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                packed = struct.pack("256s", name.encode()[:15])
                ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, packed)[20:24])
                add(ip)
            except Exception:
                pass
            finally:
                s.close()
    except Exception:
        pass

    # 3. последний рубеж: имя машины
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except Exception:
        pass
    return ips


def bind_or_explain(port):
    """Занять порт заранее, чтобы объяснить по-человечески, если занят."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
    except OSError as e:
        s.close()
        if e.errno in (errno.EADDRINUSE, errno.EACCES):
            print()
            if e.errno == errno.EACCES:
                print("Порт %d занять не дали: нужны права администратора." % port)
            else:
                print("Порт %d уже занят — на этой машине уже что-то слушает его." % port)
                print("Скорее всего, игра уже запущена в другом окне.")
            print("Что делать:")
            print("  * закрыть то, что занимает порт, и запустить снова, или")
            print("  * запустить на другом порту:   python3 play.py --port %d" % (port + 1))
            print("Коллегам тогда диктовать адрес с новым портом.")
            print()
            return None
        raise
    s.listen(128)
    s.setblocking(False)
    return s


def banner(port, ips):
    print()
    print("=" * 58)
    print("  Zza — сервер поднят. Тик 30 Гц.")
    print("=" * 58)
    print("  На этой машине:   http://localhost:%d/" % port)
    if ips:
        print("  Коллегам в локалке (диктовать любой):")
        for ip in ips:
            print("      http://%s:%d/" % (ip, port))
        print("  Скачать папку игры: http://%s:%d/download" % (ips[0], port))
    else:
        print("  Внешних адресов не нашлось: сеть не настроена или только петля.")
        print("  По IP подключиться не выйдет — см. DESIGN.md 11.3.")
    print("  Остановить — Ctrl+C.")
    print("=" * 58)
    print()


async def serve(sock, port, open_browser):
    from server.app import make_app
    from server.room import Rooms

    rooms = Rooms()
    app = make_app(rooms)
    server = __import__("tornado.httpserver", fromlist=["HTTPServer"]).HTTPServer(app)
    server.add_socket(sock)
    tick = asyncio.ensure_future(rooms.run())
    if open_browser:
        try:
            webbrowser.open("http://localhost:%d/" % port)
        except Exception:
            pass
    try:
        await tick
    except asyncio.CancelledError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Zza — кооперативный рогалик")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="порт сервера (по умолчанию %d)" % DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true",
                    help="не открывать браузер")
    args = ap.parse_args(argv)

    sock = bind_or_explain(args.port)
    if sock is None:
        return 2
    banner(args.port, local_ips())
    try:
        asyncio.run(serve(sock, args.port, not args.no_browser))
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
