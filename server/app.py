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
        # permessage-deflate. Без него снапшот на 206 сущностей даёт
        # 263 КБ/с против бюджета 2.3 в 200 КБ/с — то есть мимо. С уровнем 1
        # тот же снапшот весит 3238 байт = 95 КБ/с, а стоит 0.05 мс на
        # сообщение; контекст сжатия у каждого соединения свой, значит
        # 6 игроков = 0.3 мс к тику при бюджете 12 мс (2.2). Числа — из
        # tests/bench_tick.py. Уровень 6 дал бы 86 КБ/с за 1.3 мс: дороже
        # вчетверо ради 9 КБ/с, не берём.
        return {"compression_level": 1, "mem_level": 7}

    def open(self):
        self.set_nodelay(True)
        self.pid = self.rooms.new_pid()
        self.player = room_mod.Player(self.pid, "", self)
        # 5.1: token выдаётся в welcome и возвращается в join. Опознание по
        # ИМЕНИ было дырой: тёзки путались, чужое имя угадывалось.
        self.send_str(proto.welcome(self.pid, self.player.token))

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
            self.send_str(proto.welcome(p.pid, p.token))
        elif t == "rooms":
            self.send_str(proto.roomlist(self.rooms.listing()))
        elif t == "join":
            self.do_join(m)
        elif t == "leave":
            if p.room is not None:
                p.room.leave(p)
            self.send_str(proto.roomlist(self.rooms.listing()))
        elif t == "ready":
            if p.room is not None:
                p.room.set_ready(p, m["v"])
            else:
                p.ready = m["v"]
        elif t == "opts":
            # 8.7: параметры меняет только хозяин, и только по списку
            # допустимых значений. Отказ — это ответ, а не молчание.
            if p.room is None:
                return
            ok, bad = p.room.set_opts(p, m["opts"])
            if not ok:
                self.send_str(proto.error("nothost", "параметры меняет хозяин лобби"))
            elif bad:
                self.send_str(proto.error(
                    "opt", "сервер не принял: " + ", ".join(sorted(bad))))
        elif t == "start":
            if p.room is None:
                return
            if not p.room.set_armed(p, m["v"]):
                self.send_str(proto.error("nothost", "игру начинает хозяин лобби"))
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
        if m["room"] and self.rooms.get(m["room"]) is None:
            # Вход по коду в несуществующую комнату — это опечатка, а не
            # заявка на новую: get_or_create молча создавал бы комнату с
            # ДРУГИМ кодом, и человек искал бы там коллегу.
            self.send_str(proto.error("nosuch", "комнаты %s нет" % m["room"]))
            self.send_str(proto.roomlist(self.rooms.listing()))
            return
        r = self.rooms.get_or_create(m["room"])
        used = r.add(p, m.get("token", ""))
        if used is None or isinstance(used, str):
            msg = {"full": "комната заполнена",
                   "closed": "в эту партию уже не пускают"}.get(used, "не пускают")
            self.send_str(proto.error(used or "full", msg))
            self.send_str(proto.roomlist(self.rooms.listing()))
            return
        self.player = used       # при переподключении это прежний игрок
        # в лобби мира ещё нет, и send_level честно ничего не пришлёт;
        # вошедшему в идущую партию уровень уходит сразу
        r.send_level(self.player)

    def on_close(self):
        p = self.player
        # p.conn is self — обязательная проверка, а не перестраховка. При
        # переподключении по token (room.add) прежнее соединение того же
        # игрока закрывается НАМЕРЕННО, и его on_close приходит уже ПОСЛЕ
        # того, как игрок переехал на новый сокет. Без этой проверки он
        # отключил бы только что восстановленного игрока — то есть каждое
        # второе переподключение с продублированной вкладки.
        if p is not None and p.room is not None and p.conn is self:
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
    ], websocket_max_message_size=MAX_WS_MESSAGE, websocket_ping_interval=20)
