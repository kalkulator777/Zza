#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«Дурак» — запуск одним файлом.

Запускают все одинаково: python3 durak.py
Программа сама поднимает сервер, сама открывает браузер и сама находит
в локальной сети остальных — в меню видны открытые комнаты на всех
компьютерах, где запущен «Дурак». Договариваться, кто сервер, не нужно.

Полезные ключи:
  --port 8888        порт (у всех должен совпадать, если ищем друг друга)
  --no-browser       не открывать браузер
  --no-discovery     не искать соседей по сети
  --name "Кухня"     как этот компьютер подписан в списке
  --broadcast 192.168.1.255   если общий широковещательный адрес режется
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import tornado.ioloop
except ImportError:
    sys.exit("Не найден tornado. Положите папку tornado рядом с этим файлом "
             "или установите: pip install --no-index tornado-*.whl")

import server  # noqa: E402
from discovery import DEFAULT_UDP_PORT, Discovery, local_ipv4  # noqa: E402

log = logging.getLogger("durak")


def rooms_snapshot():
    """Что рассказываем соседям о своих комнатах."""
    rooms = []
    for room in sorted(server.HUB.rooms.values(), key=lambda r: r.created):
        rooms.append({
            "id": room.id,
            "title": room.title[:30],
            "players": len(room.members),
            "max": room.settings.max_players,
            "in_game": room.in_game,
            "settings": room.settings.to_dict(),
        })
    return rooms


def port_is_free(port, host="0.0.0.0") -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def print_banner(port, disc):
    print("")
    print("  ♠ Дурак ♥  — игра запущена")
    print("")
    print("  Это окно закрывать нельзя, пока играете. Остановить — Ctrl+C.")
    print("")
    print("  Открыть игру:  http://localhost:%d/" % port)
    for ip in local_ipv4():
        print("  С других машин: http://%s:%d/" % (ip, port))
    if disc is None:
        print("  Поиск игр в сети выключен.")
    elif not disc.enabled:
        print("  Поиск игр в сети не поднялся (%s) — адреса вводите вручную."
              % disc.error)
    else:
        print("  Соседей по сети ищем сами — просто запустите этот же файл у всех.")
        print("  Объявления уходят на: %s" % ", ".join(disc.targets))
    print("")


def main():
    parser = argparse.ArgumentParser(
        description="«Дурак» по локальной сети: сервер, браузер и поиск соседей")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--name", default=None,
                        help="как подписать этот компьютер в списке игр")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-discovery", action="store_true")
    parser.add_argument("--broadcast", default=None,
                        help="адреса рассылки через запятую, если общий "
                             "широковещательный режется (напр. 192.168.1.255)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    url = "http://localhost:%d/" % args.port

    if not port_is_free(args.port, args.host):
        print("Похоже, «Дурак» на этом компьютере уже запущен — открываю его.")
        if not args.no_browser:
            webbrowser.open(url)
        return

    app = server.make_app(args.debug)
    app.listen(args.port, address=args.host)

    disc = None
    if not args.no_discovery:
        targets = None
        if args.broadcast:
            targets = [a.strip() for a in args.broadcast.split(",") if a.strip()]
        disc = Discovery(http_port=args.port, udp_port=args.udp_port,
                         name=args.name, targets=targets)
        disc.set_rooms(rooms_snapshot())
        disc.start()
        server.HUB.peers_provider = disc.snapshot

        last = {"sig": ""}

        def refresh_peers():
            disc.set_rooms(rooms_snapshot())
            sig = disc.signature()
            if sig != last["sig"]:
                last["sig"] = sig
                server.HUB.push_lobby()

        tornado.ioloop.PeriodicCallback(refresh_peers, 1000).start()

    tornado.ioloop.PeriodicCallback(server.HUB.cleanup, 60_000).start()
    print_banner(args.port, disc)

    if not args.no_browser:
        tornado.ioloop.IOLoop.current().call_later(
            0.7, lambda: webbrowser.open(url))

    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        print("\nИгра остановлена")
    finally:
        if disc is not None:
            disc.stop()


if __name__ == "__main__":
    main()
