#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точка входа: один процесс — и сервер, и игрок.

    python3 run.py                 # сервер + Firefox на localhost
    python3 run.py --no-browser    # только сервер
    python3 run.py --port 8000
    python3 run.py --name "Комп Васи"
    python3 run.py --records /srv/racing/records.json   # где хранить рекорды
    python3 run.py --no-records                         # не хранить их вовсе
    python3 run.py --physics shadow    # рядом с гонкой крутится мир Rapier
    python3 run.py --physics rapier    # гонку считает Rapier (§12.24)

Запустивший получает ссылку с токеном хоста; остальные заходят на
http://<ip-этой-машины>:<порт>. Адреса, которые надо диктовать соседям,
печатаются при старте крупно и по одному на строку.

Комнату создаёт любой подключившийся: прежнее ограничение «только хозяин»
держалось на недоразумении и снято вместе с ключом --guest-rooms.
"""

import argparse
import asyncio
import os
import secrets
import signal
import socket
import sys
import webbrowser

# Чтобы `game` и `server` импортировались независимо от текущего каталога.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import tornado.log

from game import rapier_host
from server import config
from server.app import ServerContext, make_app
from server.discovery import Discovery, local_addresses
from server.records import RecordStore
from server.room import ContentLibrary, RoomManager, simulation_is_stub


def parse_args(argv=None):
    """Разбор аргументов командной строки (§2)."""
    parser = argparse.ArgumentParser(
        prog='run.py',
        description='Браузерные гонки для локальной сети: сервер и игра в одном процессе.')
    parser.add_argument('--port', type=int, default=config.DEFAULT_PORT,
                        help='порт HTTP и WebSocket (по умолчанию %d)' % config.DEFAULT_PORT)
    parser.add_argument('--bind', default=config.DEFAULT_BIND,
                        help='адрес для прослушивания (по умолчанию все интерфейсы)')
    parser.add_argument('--no-browser', action='store_true',
                        help='не открывать браузер')
    parser.add_argument('--name', default=None,
                        help='имя сервера, видное соседям по сети')
    parser.add_argument('--content', default=os.path.join(BASE_DIR, 'content'),
                        help='каталог с трассами и машинами (по умолчанию ./content)')
    parser.add_argument('--records', default=os.path.join(BASE_DIR, config.RECORDS_FILE),
                        help='файл рекордов кругов (по умолчанию ./%s)'
                             % config.RECORDS_FILE)
    parser.add_argument('--no-records', action='store_true',
                        help='не хранить рекорды кругов: ничего не читать и '
                             'не писать на диск')
    # Физика (§12.22). Умолчание — classic: старая арифметика раздела 6,
    # ни wasmtime, ни модуля физики процесс при ней не касается.
    parser.add_argument('--physics', choices=rapier_host.BACKENDS,
                        default=rapier_host.backend(),
                        help='какая физика считает гонку: classic — прежняя '
                             '(умолчание), shadow — прежняя плюс мир Rapier '
                             'рядом, для замеров и сверки хэша, rapier — '
                             'гонку считает Rapier (бонусов, потока и '
                             'происшествий при нём нет, §12.24)')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('порт вне диапазона 1..65535')
    if args.name is None:
        args.name = 'Гонки на %s' % socket.gethostname()
    # Флаг живёт в окружении, а не в аргументах: физика лежит в game/ и про
    # разбор командной строки знать не должна.
    os.environ[rapier_host.ENV_VAR] = args.physics
    return args


def print_banner(args, host_url, addresses, notes):
    """Крупно и понятно: куда идти хозяину и что диктовать соседям."""
    line = '=' * 64
    print('')
    print(line)
    print('  ГОНКИ ЗАПУЩЕНЫ:  %s' % args.name)
    print(line)
    print('')
    print('  ВЫ (хозяин сервера):')
    print('')
    print('      %s' % host_url)
    print('')
    local_only = all(a.startswith('127.') or a == 'localhost' for a in addresses)
    if addresses and local_only:
        print('  Сервер слушает только этот компьютер (--bind %s):' % args.bind)
        print('  соседи по сети подключиться не смогут.')
        print('')
    elif addresses:
        print('  ДИКТУЙТЕ СОСЕДЯМ ПО СЕТИ:')
        print('')
        for address in addresses:
            print('      >>>   %s:%d   <<<' % (address, args.port))
        print('')
    else:
        print('  Адрес в локальной сети определить не удалось:')
        print('  похоже, машина сейчас без сети. Соседи подключиться не смогут.')
        print('')
    print('  Обнаружение других серверов: UDP %d' % config.DISCOVERY_PORT)
    print('  Рекорды кругов: %s' % (args.records if not args.no_records else 'выключены'))
    print('  Комнату может создать любой, кто зашёл на этот сервер')
    if args.physics == rapier_host.RAPIER:
        print('  Физика: ГОНКУ СЧИТАЕТ RAPIER (§12.24).')
        print('          Бонусов, потока машин и происшествий в этом режиме нет.')
    elif args.physics != rapier_host.CLASSIC:
        print('  Физика: %s — мир Rapier крутится РЯДОМ с гонкой (§12.22)' % args.physics)
    for note in notes:
        print('  ! %s' % note)
    print('')
    print('  Остановить: Ctrl+C')
    print(line)
    print('', flush=True)


def open_browser(url):
    """Открыть Firefox на странице игры; если его нет — браузер по умолчанию."""
    try:
        browser = webbrowser.get('firefox')
    except webbrowser.Error:
        browser = None
    try:
        if browser is not None:
            browser.open_new_tab(url)
        else:
            webbrowser.open_new_tab(url)
    except Exception as exc:
        print('  Браузер открыть не удалось (%s), откройте вручную: %s' % (exc, url),
              flush=True)


async def serve(args):
    """Поднять всё хозяйство и ждать Ctrl+C."""
    tornado.log.enable_pretty_logging()
    loop = asyncio.get_running_loop()
    notes = []

    def log(message):
        print('  [сервер] %s' % message, flush=True)

    content = ContentLibrary(args.content, log=log).load()
    notes.extend(content.issues)

    # Рекорды кругов. Чтение и разбор здесь могут не удаться как угодно —
    # RecordStore.load() ничего наружу не бросает: игра обязана стартовать
    # даже с испорченным или недоступным файлом.
    records = RecordStore(args.records, enabled=not args.no_records,
                          log=log).load()
    if args.no_records:
        notes.append('рекорды отключены ключом --no-records')
    elif not records.enabled:
        notes.append('рекорды только в памяти: файл %s записать не удастся'
                     % args.records)

    discovery = Discovery(args.name, args.port, log=log)
    manager = RoomManager(content, servers_provider=discovery.servers, log=log,
                          records=records)
    if simulation_is_stub():
        notes.append('game/sim.py ещё нет: работает временная заглушка симуляции '
                     '(тик идёт, машины не едут)')
    discovery.set_stats_provider(manager.stats)

    host_token = secrets.token_urlsafe(config.TOKEN_BYTES)
    ctx = ServerContext(
        manager=manager,
        content=content,
        host_token=host_token,
        server_name=args.name,
        port=args.port,
        static_dir=os.path.join(BASE_DIR, 'static'),
        discovery=discovery,
        log=log,
    )
    application = make_app(ctx)
    try:
        http_server = application.listen(args.port, address=args.bind or None)
    except OSError as exc:
        print('Не удалось занять порт %d: %s' % (args.port, exc), file=sys.stderr)
        return 1

    manager.start()
    await discovery.start(loop)

    addresses = local_addresses() if not args.bind else [args.bind]
    host_url = 'http://localhost:%d/?host=%s' % (args.port, host_token)
    print_banner(args, host_url, addresses, notes)

    if not args.no_browser:
        loop.call_later(0.25, open_browser, host_url)

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Платформа без поддержки — останется обычный KeyboardInterrupt.
            pass
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass

    print('\n  Остановка...', flush=True)
    discovery.stop()
    manager.stop()
    # Несохранённые рекорды дописываем синхронно: цикл уже никому не нужен,
    # а терять рекорд из-за штатной остановки сервера незачем.
    records.close()
    http_server.stop()
    await http_server.close_all_connections()
    print('  Сервер остановлен.', flush=True)
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        return asyncio.run(serve(args))
    except KeyboardInterrupt:
        print('\n  Сервер остановлен.', flush=True)
        return 0


if __name__ == '__main__':
    sys.exit(main())
