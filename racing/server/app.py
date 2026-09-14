# -*- coding: utf-8 -*-
"""Tornado Application: страница, статика, /api/servers и WebSocket /ws.

Здесь сходятся все внешние входы сервера, поэтому здесь же и проверяется
всё, что приходит от клиента: тип кадра, разбор JSON, наличие и тип каждого
поля, принадлежность игрока к комнате и право на действие. Кривой или
враждебный пакет обязан закончиться событием error или молчанием, но не
падением сервера и не влиянием на чужую комнату.
"""

import json
import mimetypes
import os
import secrets
import time

import tornado.web
import tornado.websocket
from tornado.ioloop import IOLoop
from tornado.log import app_log

from game import protocol

from . import config
from .player import Player, sanitize_text

# Firefox не примет ES-модуль, отданный как application/javascript у старых
# сборок mimetypes, поэтому типы проставляем явно ещё на импорте.
for _ext, _mime in config.STATIC_MIME.items():
    mimetypes.add_type(_mime, _ext)


# --- общий контекст сервера --------------------------------------------------

class ServerContext(object):
    """Всё, что нужно хендлерам: комнаты, контент, токен хоста, соседи."""

    def __init__(self, manager, content, host_token, guest_rooms,
                 server_name, port, static_dir, discovery=None, log=None):
        self.manager = manager
        self.content = content
        self.host_token = host_token
        self.guest_rooms = bool(guest_rooms)
        self.server_name = server_name
        self.port = port
        self.static_dir = static_dir
        self.discovery = discovery
        self.log = log or (lambda message: app_log.info(message))
        self._next_id = 0

    def next_player_id(self):
        self._next_id += 1
        return self._next_id

    def servers(self):
        return self.discovery.servers() if self.discovery is not None else []


# --- HTTP --------------------------------------------------------------------

class StaticHandler(tornado.web.StaticFileHandler):
    """Статика с правильными MIME-типами и без залипшего кэша.

    Тип содержимого берётся из таблицы config.STATIC_MIME: `.js` обязан
    отдаваться как text/javascript, иначе Firefox откажется грузить модуль.
    """

    def get_content_type(self):
        ext = os.path.splitext(self.absolute_path or '')[1].lower()
        mime = config.STATIC_MIME.get(ext)
        if mime is not None:
            if mime.startswith('text/') or mime in ('application/json',):
                return mime + '; charset=utf-8'
            return mime
        return tornado.web.StaticFileHandler.get_content_type(self)

    def set_extra_headers(self, path):
        # Разработка идёт на живом сервере: ревалидация по Etag вместо
        # кэша на сутки, иначе коллеги будут ловить вчерашний main.js.
        self.set_header('Cache-Control', 'no-cache')


_MISSING_INDEX_PAGE = (
    '<!doctype html><meta charset="utf-8">'
    '<title>Гонки</title>'
    '<body style="font:16px sans-serif;padding:2rem">'
    '<h1>Сервер поднят, страницы ещё нет</h1>'
    '<p>Ожидается файл <code>static/index.html</code> — его пишет исполнитель [ui].</p>'
    '<p>WebSocket <code>/ws</code> и <code>/api/servers</code> уже работают.</p>'
)


class IndexHandler(tornado.web.RequestHandler):
    """Корень: отдаёт static/index.html."""

    def initialize(self, ctx):
        self.ctx = ctx

    def get(self):
        path = os.path.join(self.ctx.static_dir, 'index.html')
        if not os.path.isfile(path):
            self.set_status(503)
            self.set_header('Content-Type', 'text/html; charset=utf-8')
            self.finish(_MISSING_INDEX_PAGE)
            return
        with open(path, 'rb') as fp:
            body = fp.read()
        self.set_header('Content-Type', 'text/html; charset=utf-8')
        self.set_header('Cache-Control', 'no-cache')
        self.finish(body)


class ServersHandler(tornado.web.RequestHandler):
    """GET /api/servers — список серверов, найденных UDP-маяком."""

    def initialize(self, ctx):
        self.ctx = ctx

    def get(self):
        rooms, players = self.ctx.manager.stats()
        self.set_header('Cache-Control', 'no-store')
        self.write({
            'name': self.ctx.server_name,
            'port': self.ctx.port,
            'rooms': rooms,
            'players': players,
            'servers': self.ctx.servers(),
        })


# --- WebSocket ---------------------------------------------------------------

class GameSocket(tornado.websocket.WebSocketHandler):
    """Одно соединение игрока: JSON-события и бинарный ввод."""

    def initialize(self, ctx):
        self.ctx = ctx
        self.player = None
        self.said_hello = False
        self._hello_timer = None
        self._window = 0.0
        self._text_count = 0
        self._input_count = 0
        self._flood_marked = False
        self._strikes = 0

    # --- служебное Tornado --------------------------------------------------

    def check_origin(self, origin):
        # Игра живёт в локальной сети и открывается по разным адресам одного
        # сервера, проверять Origin бессмысленно.
        return True

    def get_compression_options(self):
        # Сжатие выключено: пакеты и так маленькие, а CPU дороже трафика.
        return config.COMPRESSION

    def open(self):
        self.set_nodelay(True)
        self.player = Player(self, self.ctx.next_player_id(),
                             secrets.token_hex(config.TOKEN_BYTES))
        self._window = time.monotonic()
        self._hello_timer = IOLoop.current().call_later(config.HELLO_TIMEOUT,
                                                        self._drop_silent)

    def on_close(self):
        if self._hello_timer is not None:
            IOLoop.current().remove_timeout(self._hello_timer)
            self._hello_timer = None
        player = self.player
        if player is not None:
            player.connected = False
            if self.said_hello:
                self.ctx.manager.detach(player)
            self.player = None

    def _drop_silent(self):
        """Соединение молчит и не представилось — закрываем."""
        self._hello_timer = None
        if not self.said_hello:
            self.close(1008, 'no hello')

    # --- бюджет сообщений ---------------------------------------------------

    def _budget_ok(self, is_input):
        """Скользящее окно в секунду: флуд отбрасываем, упорный — отключаем."""
        now = time.monotonic()
        if now - self._window >= 1.0:
            self._window = now
            self._text_count = 0
            self._input_count = 0
            self._flood_marked = False
        if is_input:
            self._input_count += 1
            over = self._input_count > config.INPUT_BUDGET
        else:
            self._text_count += 1
            over = self._text_count > config.TEXT_EVENT_BUDGET
        if not over:
            return True
        if not self._flood_marked:
            self._flood_marked = True
            self._strikes += 1
            if self._strikes >= config.FLOOD_STRIKES:
                self.close(1008, 'flood')
        return False

    # --- приём ---------------------------------------------------------------

    def on_message(self, message):
        """Бинарный кадр — ввод, текстовый — JSON-событие (§5)."""
        if self.player is None:
            return
        try:
            if isinstance(message, (bytes, bytearray, memoryview)):
                if self._budget_ok(True):
                    self._on_input(message)
                return
            if self._budget_ok(False):
                self._on_text(message)
        except Exception:
            # Ни одно сообщение из сети не имеет права уронить сервер.
            app_log.exception('сообщение от игрока #%d обработано с ошибкой',
                              self.player.player_id if self.player else -1)

    def _on_input(self, data):
        parsed = protocol.decode_input(data)
        if parsed is None:
            return                      # битый пакет — молча игнорируем
        room = self.player.room
        if room is None:
            return
        seq, buttons = parsed
        room.set_input(self.player, seq, buttons)

    def _on_text(self, message):
        player = self.player
        try:
            data = json.loads(message)
        except Exception:
            player.send_error('bad_json', 'это не JSON')
            return
        if not isinstance(data, dict):
            player.send_error('bad_event', 'событие должно быть объектом')
            return
        kind = data.get('t')
        if not isinstance(kind, str):
            player.send_error('bad_event', 'нет поля t')
            return
        handler = HANDLERS.get(kind)
        if handler is None:
            player.send_error('unknown_event', 'неизвестное событие %r' % kind[:32])
            return
        if not self.said_hello and kind != 'hello':
            player.send_error('no_hello', 'сначала hello')
            return
        handler(self, data)

    # --- события клиент -> сервер (§9) --------------------------------------

    def _ev_hello(self, data):
        player = self.player
        if self.said_hello:
            player.send_error('already', 'hello уже был')
            return
        name = sanitize_text(data.get('name'), config.NAME_MAX_LEN)
        if not name:
            name = 'Гонщик %d' % player.player_id
        player.name = name
        token = data.get('host_token')
        player.is_host = (
            bool(self.ctx.host_token)
            and isinstance(token, str)
            and secrets.compare_digest(token, self.ctx.host_token)
        )
        self.said_hello = True
        if self._hello_timer is not None:
            IOLoop.current().remove_timeout(self._hello_timer)
            self._hello_timer = None
        self.ctx.manager.attach(player)
        player.send_event({
            't': 'welcome',
            # Слот выдаётся при входе в комнату, а welcome приходит раньше:
            # здесь он почти всегда null, боевое значение — в room.you (§12.6).
            'slot': player.slot if player.slot >= 0 else None,
            'slot_token': player.slot_token,
            'is_host': player.is_host,
            'server_name': self.ctx.server_name,
            'content': self.ctx.content.welcome_content,
        })
        player.send_event(self.ctx.manager.rooms_event())
        player.start_ping(time.monotonic())

    def _ev_list_rooms(self, data):
        self.player.send_event(self.ctx.manager.rooms_event())

    def _ev_create_room(self, data):
        player = self.player
        if not self.ctx.guest_rooms and not player.is_host:
            player.send_error('not_host', 'комнаты создаёт хозяин сервера')
            return
        self.ctx.manager.create_room(player, data.get('name'), data.get('settings'))

    def _ev_join_room(self, data):
        self.ctx.manager.join_room(self.player, data.get('room_id'))

    def _ev_leave_room(self, data):
        self.ctx.manager.leave_room(self.player)

    def _ev_set_car(self, data):
        player = self.player
        if player.room is None:
            player.send_error('no_room', 'вы не в комнате')
            return
        player.room.set_car(player, data.get('car_id'), data.get('color'))

    def _ev_set_ready(self, data):
        player = self.player
        if player.room is None:
            player.send_error('no_room', 'вы не в комнате')
            return
        player.room.set_ready(player, data.get('ready'))

    def _ev_update_settings(self, data):
        player = self.player
        if player.room is None:
            player.send_error('no_room', 'вы не в комнате')
            return
        player.room.update_settings(player, data.get('settings'))

    def _ev_start_race(self, data):
        player = self.player
        if player.room is None:
            player.send_error('no_room', 'вы не в комнате')
            return
        player.room.start_race(player)

    def _ev_chat(self, data):
        player = self.player
        if player.room is None:
            player.send_error('no_room', 'вы не в комнате')
            return
        player.room.add_chat(player, data.get('text'))

    def _ev_pong(self, data):
        player = self.player
        if player.note_pong(data.get('t0'), time.monotonic()) and player.room is not None:
            player.room.note_ping_change()


# Таблица событий: имя из контракта -> метод. Отдельным словарём, чтобы
# разбор не лез в getattr по строке из сети.
HANDLERS = {
    'hello': GameSocket._ev_hello,
    'list_rooms': GameSocket._ev_list_rooms,
    'create_room': GameSocket._ev_create_room,
    'join_room': GameSocket._ev_join_room,
    'leave_room': GameSocket._ev_leave_room,
    'set_car': GameSocket._ev_set_car,
    'set_ready': GameSocket._ev_set_ready,
    'update_settings': GameSocket._ev_update_settings,
    'start_race': GameSocket._ev_start_race,
    'chat': GameSocket._ev_chat,
    'pong': GameSocket._ev_pong,
}


# --- сборка приложения -------------------------------------------------------

# Каталоги статики, которые отдаются и с корня: index.html подключает модули
# относительными путями (`./js/main.js`), поэтому они обязаны лежать на корне.
_ASSET_DIRS = 'js|css|vendor|img|fonts|assets'
_ASSET_EXT = 'js|mjs|css|json|ico|png|jpg|jpeg|svg|wasm|map|txt'


def make_app(ctx):
    """Собрать Tornado Application со всеми хендлерами."""
    static = {'path': ctx.static_dir}
    handlers = [
        (r'/', IndexHandler, {'ctx': ctx}),
        (r'/ws', GameSocket, {'ctx': ctx}),
        (r'/api/servers', ServersHandler, {'ctx': ctx}),
        (r'/static/(.*)', StaticHandler, static),
        (r'/((?:%s)/.*)' % _ASSET_DIRS, StaticHandler, static),
        (r'/([^/]+\.(?:%s))' % _ASSET_EXT, StaticHandler, static),
    ]
    return tornado.web.Application(
        handlers,
        websocket_ping_interval=config.WS_PING_INTERVAL,
        websocket_ping_timeout=config.WS_PING_TIMEOUT,
        websocket_max_message_size=config.WS_MAX_MESSAGE_SIZE,
        compress_response=False,
        serve_traceback=False,
    )
