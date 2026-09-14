# -*- coding: utf-8 -*-
"""Обнаружение серверов в локальной сети: UDP-маяк на порту 8001.

Каждый сервер раз в BEACON_INTERVAL шлёт широковещательный пакет со своим
именем, портом и загрузкой, и слушает такие же пакеты соседей. Список живых
серверов уходит клиенту в поле ``servers`` события ``rooms`` (§9) и по
``GET /api/servers``.

Всё сделано на asyncio-датаграммах: ни одного потока, ни одной блокировки
цикла событий. Сеть, где широковещание запрещено, не считается ошибкой —
маяк молча продолжает слушать, а сервер работает как обычно.
"""

import json
import socket
import time
import uuid

from . import config


# --- определение собственных адресов ---------------------------------------

# Адреса-зонды из разных диапазонов: «подключаем» к ним UDP-сокет и смотрим,
# какой локальный адрес выбрало ядро под этот маршрут. Данные не отправляются,
# DNS не спрашивается (все зонды — литеральные IP), блокировки нет.
_PROBE_TARGETS = ('8.8.8.8', '192.168.255.255', '10.255.255.255', '172.31.255.255')


def probe_address(target):
    """Локальный адрес, с которого пошёл бы пакет к ``target``, или None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.connect((target, config.DISCOVERY_PORT))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def local_addresses():
    """Адреса этой машины в локальных сетях, по которым к ней можно зайти.

    Возвращает список без дублей и без loopback; первым идёт адрес маршрута
    по умолчанию — его и надо диктовать соседям.
    """
    found = []
    for target in _PROBE_TARGETS:
        addr = probe_address(target)
        if not addr or addr.startswith('127.') or addr == '0.0.0.0':
            continue
        if addr not in found:
            found.append(addr)
    return found


def broadcast_targets(addresses):
    """Куда слать маяк: общий 255.255.255.255 и широковещательные адреса /24.

    Маску сети без сторонних библиотек не узнать, поэтому для каждого своего
    адреса берём broadcast ближайшей /24 — в домашних и офисных сетях это
    попадание, а в остальных случаях работает общий 255.255.255.255.
    """
    targets = ['255.255.255.255']
    for addr in addresses:
        parts = addr.split('.')
        if len(parts) != 4:
            continue
        subnet = '.'.join(parts[:3]) + '.255'
        if subnet not in targets:
            targets.append(subnet)
    return targets


# --- протокол датаграмм -----------------------------------------------------

class _BeaconProtocol(object):
    """asyncio.DatagramProtocol: приём чужих маяков.

    Наследование от asyncio.DatagramProtocol не требуется — достаточно набора
    методов; так модуль не тянет asyncio ради одного базового класса.
    """

    def __init__(self, owner):
        self._owner = owner

    def connection_made(self, transport):
        self._owner._on_transport(transport)

    def datagram_received(self, data, addr):
        self._owner._on_beacon(data, addr)

    def error_received(self, exc):
        # ICMP «порт недоступен» в ответ на широковещание — обычное дело,
        # роняет только невнимательных.
        pass

    def connection_lost(self, exc):
        self._owner._on_transport(None)


# --- сам маяк ---------------------------------------------------------------

class Discovery(object):
    """UDP-маяк и список живых серверов сети."""

    def __init__(self, server_name, port, stats_provider=None, log=None):
        self.server_name = server_name
        self.port = int(port)
        self.server_id = uuid.uuid4().hex       # чтобы не считать соседом себя
        self._stats = stats_provider or (lambda: (0, 0))
        self._log = log or (lambda message: None)
        self._peers = {}                        # id -> запись о соседе
        self._transport = None
        self._loop = None
        self._timer = None
        self._targets = broadcast_targets(local_addresses())
        self._send_failed = False               # ругаемся на запрет ровно один раз
        self._running = False

    def set_stats_provider(self, provider):
        """Кто сообщает загрузку сервера для маяка: () -> (комнат, игроков).

        Ставится отдельным вызовом, потому что менеджер комнат создаётся после
        маяка: ему нужен список соседей из этого же объекта.
        """
        self._stats = provider

    # --- жизненный цикл ----------------------------------------------------

    async def start(self, loop):
        """Поднять сокет и запустить периодическую рассылку.

        Ошибка привязки (порт занят, сети нет) не фатальна: сервер работает
        дальше, просто без списка соседей.
        """
        self._loop = loop
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, 'SO_REUSEPORT'):
                # Чтобы два сервера на одной машине оба слышали маяки.
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setblocking(False)
            sock.bind(('', config.DISCOVERY_PORT))
        except OSError as exc:
            sock.close()
            self._log('маяк не поднялся (%s) — обнаружение серверов выключено' % exc)
            return False
        try:
            await loop.create_datagram_endpoint(lambda: _BeaconProtocol(self), sock=sock)
        except OSError as exc:
            sock.close()
            self._log('маяк не поднялся (%s) — обнаружение серверов выключено' % exc)
            return False
        self._running = True
        self._tick()
        return True

    def stop(self):
        """Остановить рассылку и закрыть сокет."""
        self._running = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _on_transport(self, transport):
        self._transport = transport

    # --- периодическая работа ----------------------------------------------

    def _tick(self):
        """Один шаг маяка: разослать себя и выкинуть протухших соседей."""
        if not self._running:
            return
        self._sweep()
        self._send_beacon()
        self._timer = self._loop.call_later(config.BEACON_INTERVAL, self._tick)

    def _send_beacon(self):
        transport = self._transport
        if transport is None:
            return
        rooms, players = self._safe_stats()
        payload = json.dumps({
            'm': config.BEACON_MAGIC,
            'v': config.BEACON_VERSION,
            'id': self.server_id,
            'name': self.server_name,
            'port': self.port,
            'rooms': rooms,
            'players': players,
        }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        sent = False
        for target in self._targets:
            try:
                transport.sendto(payload, (target, config.DISCOVERY_PORT))
                sent = True
            except OSError:
                continue
        if not sent and not self._send_failed:
            self._send_failed = True
            self._log('широковещание в этой сети недоступно — '
                      'соседи не увидят сервер автоматически')

    def _safe_stats(self):
        """Загрузка сервера для маяка; поставщик статистики не должен ронять маяк."""
        try:
            rooms, players = self._stats()
            return int(rooms), int(players)
        except Exception:
            return 0, 0

    def _sweep(self):
        """Вытеснение серверов, чей маяк не приходил дольше BEACON_TTL."""
        deadline = time.monotonic() - config.BEACON_TTL
        dead = [key for key, item in self._peers.items() if item['seen'] < deadline]
        for key in dead:
            del self._peers[key]

    # --- приём чужих маяков -------------------------------------------------

    def _on_beacon(self, data, addr):
        """Разбор пакета от соседа. Мусор из сети не должен ничего ронять."""
        if not data or len(data) > config.BEACON_MAX_SIZE:
            return
        try:
            info = json.loads(data.decode('utf-8'))
        except Exception:
            return
        if not isinstance(info, dict):
            return
        if info.get('m') != config.BEACON_MAGIC or info.get('v') != config.BEACON_VERSION:
            return
        server_id = info.get('id')
        if not isinstance(server_id, str) or not server_id or server_id == self.server_id:
            return
        port = info.get('port')
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            return
        name = info.get('name')
        if not isinstance(name, str) or not name:
            name = addr[0]
        elif len(name) > config.NAME_MAX_LEN * 2:
            name = name[:config.NAME_MAX_LEN * 2]
        rooms = info.get('rooms')
        players = info.get('players')
        if not isinstance(rooms, int) or isinstance(rooms, bool) or rooms < 0:
            rooms = 0
        if not isinstance(players, int) or isinstance(players, bool) or players < 0:
            players = 0
        if server_id not in self._peers and len(self._peers) >= config.MAX_KNOWN_SERVERS:
            # Потолок списка: иначе любой желающий набьёт его выдуманными id.
            return
        self._peers[server_id] = {
            'name': name,
            'url': 'http://%s:%d/' % (addr[0], port),
            'players': players,
            'rooms': rooms,
            'seen': time.monotonic(),
        }

    # --- наружу -------------------------------------------------------------

    def servers(self):
        """Список соседей для поля ``servers`` события rooms (§9)."""
        self._sweep()
        out = [{'name': item['name'], 'url': item['url'],
                'players': item['players'], 'rooms': item['rooms']}
               for item in self._peers.values()]
        out.sort(key=lambda item: (item['name'], item['url']))
        return out
