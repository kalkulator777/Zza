# -*- coding: utf-8 -*-
"""Tornado-приложение: статика, API поиска игр, WebSocket."""

import json
import os

import tornado.web

from .rooms import RoomManager
from .ws import GameSocket

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(BASE, "static")


class IndexHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header("Cache-Control", "no-store")

    def get(self):
        # Отдаём как есть, без шаблонизатора: в HTML/JS могут быть фигурные скобки.
        with open(os.path.join(STATIC, "index.html"), "rb") as fh:
            self.set_header("Content-Type", "text/html; charset=utf-8")
            self.write(fh.read())


class GamesHandler(tornado.web.RequestHandler):
    def initialize(self, mgr, disco, port):
        self.mgr = mgr
        self.disco = disco
        self.port = port

    def set_default_headers(self):
        self.set_header("Cache-Control", "no-store")
        self.set_header("Content-Type", "application/json; charset=utf-8")

    def get(self):
        local = []
        for b in self.mgr.list_open():
            b = dict(b)
            b["remote"] = False
            b["host"] = "этот компьютер"
            b["url"] = "/?join=" + b["code"]
            local.append(b)
        out = {
            "local": local,
            "lan": self.disco.games() if self.disco else [],
            "discovery": bool(self.disco and self.disco.enabled),
            "peers": self.disco.peer_count() if self.disco else 0,
            "port": self.port,
        }
        self.write(json.dumps(out, ensure_ascii=False))


class CatalogHandler(tornado.web.RequestHandler):
    def initialize(self, mgr):
        self.mgr = mgr

    def get(self):
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.write(json.dumps(self.mgr.catalog(), ensure_ascii=False))


def make_app(port, mgr, disco=None):
    app = tornado.web.Application(
        [
            (r"/", IndexHandler),
            (r"/ws", GameSocket, {"mgr": mgr, "disco": disco}),
            (r"/api/games", GamesHandler, {"mgr": mgr, "disco": disco, "port": port}),
            (r"/api/catalog", CatalogHandler, {"mgr": mgr}),
            (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": STATIC}),
        ],
        template_path=STATIC,
        static_path=STATIC,
        websocket_ping_interval=15,
        websocket_ping_timeout=15,
        debug=False,
    )
    app.mgr = mgr
    return app
