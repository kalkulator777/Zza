# -*- coding: utf-8 -*-
"""HTTP-раздача, WebSocket и /download на tornado (DESIGN.md 3, 5)."""

import io
import os
import time
import zipfile

import tornado.web
import tornado.websocket

from . import proto
from . import room as room_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "static")
VENDOR_DIR = os.path.join(ROOT, "vendor")

MAX_WS_MESSAGE = 64 * 1024     # ввод — десятки байт; всё крупнее это не игра

# что не кладём в zip папки игры
ZIP_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".idea",
                 ".pytest_cache", "pw-browsers"}
ZIP_SKIP_EXT = {".pyc", ".pyo", ".zip", ".log"}


class IndexHandler(tornado.web.RequestHandler):
    def get(self):
        path = os.path.join(STATIC_DIR, "index.html")
        if not os.path.exists(path):
            self.set_status(500)
            self.write("нет static/index.html")
            return
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.set_header("Cache-Control", "no-cache")
        with open(path, "rb") as f:
            self.write(f.read())


class NoCacheStatic(tornado.web.StaticFileHandler):
    """Игру правят и перезагружают страницу — кэш только мешает."""

    def set_extra_headers(self, path):
        self.set_header("Cache-Control", "no-cache")


class DownloadHandler(tornado.web.RequestHandler):
    """Zip папки игры на лету: чтобы коллега скачал её с машины хоста.

    Сам архив не попадает внутрь себя (он не файл на диске, а поток),
    __pycache__, .git и прочий мусор — тоже.
    """

    async def get(self):
        buf = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for dirpath, dirnames, filenames in os.walk(ROOT):
                dirnames[:] = [d for d in dirnames if d not in ZIP_SKIP_DIRS]
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in ZIP_SKIP_EXT:
                        continue
                    full = os.path.join(dirpath, fn)
                    if not os.path.isfile(full) or os.path.islink(full):
                        continue
                    rel = os.path.relpath(full, ROOT)
                    z.write(full, os.path.join("Zza", rel))
                    count += 1
        data = buf.getvalue()
        self.set_header("Content-Type", "application/zip")
        self.set_header("Content-Disposition", 'attachment; filename="Zza.zip"')
        self.set_header("Content-Length", str(len(data)))
        for i in range(0, len(data), 256 * 1024):
            self.write(data[i:i + 256 * 1024])
            await self.flush()


class GameSocket(tornado.websocket.WebSocketHandler):
    """Одно соединение = один игрок. Вся валидация — в proto.parse_client."""

    def initialize(self, rooms):
        self.rooms = rooms
        self.player = None
        self.pid = 0

    def check_origin(self, origin):
        return True          # игра для локалки, ходят по IP

    def get_compression_options(self):
        # permessage-deflate жмёт снапшот ~в 4 раза, но контекст сжатия
        # у каждого соединения свой, то есть CPU умножается на число
        # игроков — ровно то, чего избегает 4.4. Включать только вместе
        # с замером тика. Сейчас выключено: return {} чтобы включить.
        return None

    def open(self):
        self.set_nodelay(True)
        self.pid = self.rooms.new_pid()
        self.player = room_mod.Player(self.pid, "", self)
        self.send_str(proto.welcome(self.pid))

    def send_str(self, text):
        try:
            self.write_message(text)
        except tornado.websocket.WebSocketClosedError:
            pass
        except Exception:
            pass

    def on_message(self, raw):
        m = proto.parse_client(raw)
        if m is None:
            return           # мусор молча роняем, сервер не падает
        t = m["t"]
        p = self.player
        if t == "hello":
            if m["name"]:
                p.name = m["name"]
            self.send_str(proto.welcome(p.pid))
        elif t == "rooms":
            self.send_str(proto.roomlist(self.rooms.listing()))
        elif t == "join":
            self.do_join(m)
        elif t == "ready":
            p.ready = m["v"]
        elif t == "input":
            p.pending = m
        elif t == "ping":
            self.send_str(proto.pong(m["id"], m["ct"], time.time()))

    def do_join(self, m):
        p = self.player
        if m["name"]:
            p.name = m["name"]
        if p.room is not None:
            return
        r = self.rooms.get_or_create(m["room"])
        used = r.add(p)
        if used is None:
            self.send_str(proto.error("full", "комната заполнена"))
            return
        self.player = used       # при переподключении это прежний игрок
        r.send_level(self.player)

    def on_close(self):
        p = self.player
        if p is not None and p.room is not None:
            p.room.disconnect(p)
        self.player = None


def make_app(rooms):
    return tornado.web.Application([
        (r"/", IndexHandler),
        (r"/index.html", IndexHandler),
        (r"/ws", GameSocket, {"rooms": rooms}),
        (r"/download", DownloadHandler),
        (r"/static/(.*)", NoCacheStatic, {"path": STATIC_DIR}),
        (r"/vendor/(.*)", NoCacheStatic, {"path": VENDOR_DIR}),
    ], websocket_max_message_size=MAX_WS_MESSAGE, websocket_ping_interval=20,
       websocket_ping_timeout=60)
