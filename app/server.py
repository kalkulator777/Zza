"""HTTP и WebSocket."""

import json
import logging
import os

import tornado.escape
import tornado.web
import tornado.websocket
from tornado.ioloop import IOLoop

from app import discovery

log = logging.getLogger("server")


class NoCacheStatic(tornado.web.StaticFileHandler):
    """Игра раздаётся по локалке и часто правится — кэш только мешает."""

    def set_extra_headers(self, path):
        self.set_header("Cache-Control", "no-store")


class PageHandler(tornado.web.RequestHandler):
    def initialize(self, manager, base_dir, page):
        self.manager = manager
        self.base_dir = base_dir
        self.page = page

    def set_default_headers(self):
        self.set_header("Cache-Control", "no-store")

    def render_page(self):
        with open(os.path.join(self.base_dir, "static", self.page), encoding="utf-8") as fh:
            self.write(fh.read())


class IndexHandler(PageHandler):
    def get(self):
        self.render_page()


class RoomPageHandler(PageHandler):
    def get(self, code):
        if self.manager.get(code) is None:
            self.redirect("/?err=" + tornado.escape.url_escape("Лобби %s не найдено" % code.upper()))
            return
        self.render_page()


class ApiHandler(tornado.web.RequestHandler):
    def initialize(self, manager):
        self.manager = manager

    def set_default_headers(self):
        self.set_header("Cache-Control", "no-store")
        self.set_header("Content-Type", "application/json; charset=utf-8")

    def respond(self, payload):
        self.write(json.dumps(payload, ensure_ascii=False))


class CreateHandler(ApiHandler):
    def post(self):
        room = self.manager.create()
        self.respond({"code": room.code})


class RoomsHandler(ApiHandler):
    def get(self):
        self.respond({"rooms": self.manager.public_rooms(),
                      "host": self.request.host})


class ScanHandler(ApiHandler):
    async def get(self):
        rooms = await IOLoop.current().run_in_executor(None, discovery.scan)
        self.respond({"rooms": rooms})


class GameSocket(tornado.websocket.WebSocketHandler):
    def initialize(self, manager):
        self.manager = manager
        self.room = None
        self.player = None

    def check_origin(self, origin):
        return True  # локальная сеть, заходят по IP хоста

    def open(self):
        self.set_nodelay(True)

    def send(self, message):
        try:
            self.write_message(json.dumps(message, ensure_ascii=False))
        except tornado.websocket.WebSocketClosedError:
            pass

    def close_with_reason(self, text):
        self.send({"t": "kicked", "text": text})
        self.close()

    def on_message(self, raw):
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if not isinstance(message, dict):
            return

        if self.player is None:
            if message.get("t") != "join":
                return
            self.do_join(message)
            return

        if message.get("t") == "ping":
            self.send({"t": "pong"})
            return
        self.room.handle(self.player, message)

    def do_join(self, message):
        room = self.manager.get(message.get("code"))
        if room is None:
            self.close_with_reason("Лобби не найдено — возможно, хост закрыл игру")
            return
        player, error = room.join(message.get("nick"), message.get("token"), self)
        if player is None:
            self.close_with_reason(error)
            return
        self.room = room
        self.player = player
        self.send({"t": "joined", "token": player.token, "you": player.public()})
        self.send(room.room_snapshot())
        for line in room.chat[-40:]:
            self.send(line)
        if room.mode is not None:
            self.send(room.mode.state_for(player, with_canvas=True))
        room.broadcast_room()

    def on_close(self):
        if self.room is not None and self.player is not None:
            if self.player.conn is self:
                self.room.disconnect(self.player)


def make_app(manager, base_dir):
    manager_arg = {"manager": manager}
    page_args = {"manager": manager, "base_dir": base_dir}
    return tornado.web.Application([
        (r"/", IndexHandler, dict(page="index.html", **page_args)),
        (r"/room/([A-Za-z0-9]+)", RoomPageHandler, dict(page="room.html", **page_args)),
        (r"/api/create", CreateHandler, manager_arg),
        (r"/api/rooms", RoomsHandler, manager_arg),
        (r"/api/scan", ScanHandler, manager_arg),
        (r"/ws", GameSocket, manager_arg),
        (r"/static/(.*)", NoCacheStatic, {"path": os.path.join(base_dir, "static")}),
    ], websocket_ping_interval=25)
