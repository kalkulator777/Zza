"""Tornado-приложение: статика, справочные ручки и один WebSocket."""

import json
import os

import tornado.web
import tornado.websocket


class NoCacheStatic(tornado.web.StaticFileHandler):
    """Кэш браузера тут только мешает: игру обновляют на серверной машине и
    ждут, что у всех сразу станет новая версия."""

    def set_extra_headers(self, path):
        self.set_header("Cache-Control", "no-store, must-revalidate")


class IndexHandler(tornado.web.RequestHandler):
    def initialize(self, root):
        self.root = root

    def get(self):
        self.set_header("Cache-Control", "no-store, must-revalidate")
        with open(os.path.join(self.root, "index.html"), encoding="utf-8") as f:
            self.write(f.read())


class TracksHandler(tornado.web.RequestHandler):
    def initialize(self, hub):
        self.hub = hub

    def get(self, tid=None):
        self.set_header("Content-Type", "application/json; charset=utf-8")
        if tid:
            t = self.hub.tracks.get(tid)
            if t is None:
                self.set_status(404)
                return self.write({"error": "нет такой трассы"})
            return self.write(json.dumps(t.client_payload()))
        self.write(json.dumps([
            {"id": t.id, "name": t.name, "author": t.author, "laps": t.laps,
             "length": round(t.length), "difficulty": t.difficulty}
            for t in self.hub.tracks.values()
        ]))


class StatusHandler(tornado.web.RequestHandler):
    """Живая справка о сервере — чтобы с чужого компьютера было видно, что он
    вообще отвечает, ещё до загрузки игры."""

    def initialize(self, hub, version):
        self.hub = hub
        self.version = version

    def get(self):
        st = self.hub.stats
        avg = (st["busy"] / st["ticks"]) if st["ticks"] else 0.0
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.write(json.dumps({
            "ok": True,
            "version": self.version,
            "tracks": len(self.hub.tracks),
            "lobbies": len(self.hub.lm.lobbies),
            "sessions": len(self.hub.sessions),
            "racing": sum(1 for l in self.hub.lm.lobbies.values() if l.state == "racing"),
            "tickMsAvg": round(avg, 3),
            "tickMsPeak": round(st["peak_ms"], 3),
        }, ensure_ascii=False))


class GameSocket(tornado.websocket.WebSocketHandler):
    def initialize(self, hub):
        self.hub = hub
        self.session = None

    def check_origin(self, origin):
        # Играют из локальной сети, страница отдаётся этим же сервером.
        # Проверка источника здесь только мешала бы заходу по IP.
        return True

    def open(self):
        self.set_nodelay(True)   # гонка: 10 мс склейки Нейгла заметны
        self.hub.on_open(self)

    def on_message(self, message):
        self.hub.on_message(self, message)

    def on_close(self):
        self.hub.on_close(self)


def make_app(hub, root, version, debug=False):
    static = os.path.join(root, "static")
    shared = os.path.join(root, "shared")
    return tornado.web.Application(
        [
            (r"/", IndexHandler, {"root": static}),
            (r"/ws", GameSocket, {"hub": hub}),
            (r"/api/status", StatusHandler, {"hub": hub, "version": version}),
            (r"/api/tracks", TracksHandler, {"hub": hub}),
            (r"/api/track/([A-Za-z0-9_-]+)", TracksHandler, {"hub": hub}),
            (r"/shared/(.*)", NoCacheStatic, {"path": shared}),
            (r"/(.*)", NoCacheStatic, {"path": static}),
        ],
        websocket_ping_interval=20,
        websocket_ping_timeout=60,
        websocket_max_message_size=65536,
        compress_response=True,
        debug=debug,
    )
