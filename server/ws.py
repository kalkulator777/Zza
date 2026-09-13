# -*- coding: utf-8 -*-
"""WebSocket-протокол клиента."""

import json

from tornado.websocket import WebSocketHandler


class GameSocket(WebSocketHandler):
    def initialize(self, mgr, disco):
        self.mgr = mgr
        self.disco = disco
        self.pid = None
        self.name = "Игрок"
        self.room = None

    def check_origin(self, origin):
        return True  # локальная сеть, авторизации нет

    def get_compression_options(self):
        return {}

    def open(self):
        self.set_nodelay(True)
        self.pid = self.mgr.next_pid()
        self.send({"t": "welcome", "pid": self.pid, **self.mgr.catalog()})

    def send(self, msg):
        self.send_raw(json.dumps(msg, ensure_ascii=False))

    def send_raw(self, raw):
        """Уже сериализованный JSON: широковещательные сообщения готовятся один раз."""
        try:
            self.write_message(raw)
        except Exception:
            pass

    def on_close(self):
        if self.room:
            self.room.remove(self.pid)
            self.room = None

    # ------------------------------------------------------------------
    def on_message(self, raw):
        try:
            m = json.loads(raw)
        except Exception:
            return
        t = m.get("t")
        if t == "i":
            if self.room:
                self.room.input(self.pid, m.get("k", 0), m.get("p", 0),
                                m.get("ax", 1), m.get("ay", 0))
            return
        if t == "ping":
            self.send({"t": "pong", "ts": m.get("ts")})
            return
        if t == "hello":
            self.name = (str(m.get("name") or "Игрок").strip() or "Игрок")[:16]
            if self.room:
                p = self.room.players.get(self.pid)
                if p:
                    p.name = self.name
                    self.room.push_lobby()
            return

        if t == "create":
            self._leave()
            r = self.mgr.create(m.get("rname") or f"Игра {self.name}",
                                m.get("mode", "1v1"), m.get("arena", "plato"),
                                int(m.get("stocks", 3) or 3))
            self.room = r
            r.add(self.pid, self.name, self)
            r.host = self.pid
            r.push_lobby()
            return

        if t == "join":
            code = (m.get("code") or "").upper().strip()
            r = self.mgr.get(code)
            if r is None:
                self.send({"t": "error", "m": "Игра %s не найдена" % code})
                return
            self._leave()
            self.room = r
            r.add(self.pid, self.name, self, spectator=bool(m.get("spec")))
            return

        if t == "leave":
            self._leave()
            self.send({"t": "left"})
            return

        r = self.room
        if r is None:
            return

        if t == "hero":
            r.set_hero(self.pid, m.get("hero"))
        elif t == "team":
            r.set_team(self.pid, m.get("team", 0))
        elif t == "spectate":
            r.set_spectator(self.pid, m.get("v", True))
        elif t == "ready":
            r.set_ready(self.pid, m.get("v", True))
        elif t == "config":
            r.configure(self.pid, m.get("mode"), m.get("arena"),
                        m.get("stocks"), m.get("rname"))
        elif t == "start":
            r.start(self.pid)
        elif t == "abort":
            r.abort(self.pid)
        elif t == "addbot":
            if self.pid == r.host:
                r.add_bot(int(m.get("level", 2)))
        elif t == "kick":
            if self.pid == r.host:
                r.kick(int(m.get("pid", 0)))
        elif t == "chat":
            txt = (m.get("m") or "").strip()
            if txt:
                r.say(self.name, txt)

    def _leave(self):
        if self.room:
            self.room.remove(self.pid)
            self.room = None
