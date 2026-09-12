"""Комнаты, игроки и общее состояние."""

import logging
import random
import secrets
import string
import time

from tornado.ioloop import IOLoop, PeriodicCallback

from app import discovery

log = logging.getLogger("rooms")

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 4
MAX_PLAYERS = 12
CHAT_HISTORY = 60
EMPTY_ROOM_TTL = 180  # секунд без единого живого игрока — комната удаляется

PLAYER_COLORS = [
    "#ff5c8a", "#ffb03b", "#ffe14d", "#6ee07a", "#4dd2c0",
    "#4db8ff", "#8f7bff", "#d07bff", "#ff8a5c", "#9ad35c",
    "#5cd6ff", "#ff6bcb",
]

MODE_INFO = {
    "guess": ("Угадайка", "Один рисует, остальные угадывают слово"),
    "normal": ("Обычно", "Фраза, рисунок, подпись, рисунок — испорченный телефон"),
    "sandwich": ("Сэндвич", "Фраза, потом только рисунки, в конце подпись"),
    "plagiat": ("Плагиат", "Копируй предыдущий рисунок, времени всё меньше"),
}

DEFAULT_SETTINGS = {
    "mode": "guess",
    "rounds": 3,
    "draw_time": 80,
    "write_time": 45,
    "steps": 0,            # 0 — по числу игроков
    "hints": 2,
    "difficulty": "mixed",
    "custom_words": "",
}

SETTINGS_LIMITS = {
    "mode": set(MODE_INFO),
    "rounds": (1, 10),
    "draw_time": (20, 180),
    "write_time": (15, 120),
    "steps": (0, 12),
    "hints": (0, 3),
    "difficulty": {"easy", "mixed", "hard"},
}


class Player:
    def __init__(self, token, nick, color):
        self.token = token
        self.nick = nick
        self.color = color
        self.score = 0
        self.conn = None
        self.left_at = None

    @property
    def online(self):
        return self.conn is not None

    def send(self, message):
        if self.conn is not None:
            self.conn.send(message)

    def public(self):
        return {
            "token": self.token,
            "nick": self.nick,
            "color": self.color,
            "score": self.score,
            "online": self.online,
        }


class Room:
    def __init__(self, code, manager):
        self.code = code
        self.manager = manager
        self.players = {}
        self.order = []
        self.host_token = None
        self.state = "lobby"
        self.settings = dict(DEFAULT_SETTINGS)
        self.mode = None
        self.chat = []
        self.created_at = time.time()
        self.empty_since = time.time()

    # --- игроки -------------------------------------------------------

    def unique_nick(self, nick, skip_token=None):
        nick = (nick or "").strip()[:16] or "Игрок"
        taken = {p.nick for t, p in self.players.items() if t != skip_token}
        if nick not in taken:
            return nick
        for n in range(2, 100):
            candidate = "%s %d" % (nick, n)
            if candidate not in taken:
                return candidate
        return nick

    def free_color(self):
        """По порядку палитры — так цвета игроков заведомо различимы."""
        used = {p.color for p in self.players.values()}
        for color in PLAYER_COLORS:
            if color not in used:
                return color
        return random.choice(PLAYER_COLORS)

    def join(self, nick, token, conn):
        """Новый игрок или возврат старого по токену."""
        player = self.players.get(token) if token else None
        if player is None:
            if len(self.players) >= MAX_PLAYERS:
                return None, "В комнате уже максимум игроков"
            if self.state != "lobby" and self.mode is not None and not self.mode.allows_late_join():
                return None, "Игра уже идёт, дождитесь конца раунда"
            token = secrets.token_hex(8)
            player = Player(token, self.unique_nick(nick), self.free_color())
            self.players[token] = player
            self.order.append(token)
            self.system_chat("%s заходит в игру" % player.nick)
        else:
            if player.conn is not None and player.conn is not conn:
                player.conn.close_with_reason("Вы открыли игру в другой вкладке")
            player.left_at = None
            if nick:
                player.nick = self.unique_nick(nick, skip_token=token)
        player.conn = conn
        self.empty_since = None
        if self.host_token is None or self.host_token not in self.players:
            self.host_token = player.token
        return player, None

    def disconnect(self, player):
        if player.conn is not None:
            player.conn = None
        player.left_at = time.time()
        if self.state == "lobby":
            # из лобби выходят насовсем, чтобы список не зарастал призраками
            self.drop(player)
        elif self.mode is not None:
            self.mode.on_disconnect(player)
        if not any(p.online for p in self.players.values()):
            self.empty_since = time.time()
        self.broadcast_room()

    def drop(self, player):
        self.players.pop(player.token, None)
        if player.token in self.order:
            self.order.remove(player.token)
        self.system_chat("%s выходит" % player.nick)
        if self.host_token == player.token:
            self.host_token = self.order[0] if self.order else None

    def online_players(self):
        return [self.players[t] for t in self.order if t in self.players and self.players[t].online]

    # --- рассылка -----------------------------------------------------

    def broadcast(self, message, only=None, exclude=None):
        for token in list(self.order):
            player = self.players.get(token)
            if player is None or not player.online:
                continue
            if only is not None and token not in only:
                continue
            if exclude is not None and token in exclude:
                continue
            player.send(message)

    def system_chat(self, text, kind="system"):
        self.push_chat({"t": "chat", "kind": kind, "text": text})

    def push_chat(self, message, only=None):
        if only is None:
            self.chat.append(message)
            del self.chat[:-CHAT_HISTORY]
        self.broadcast(message, only=only)

    def room_snapshot(self):
        return {
            "t": "room",
            "code": self.code,
            "state": self.state,
            "host": self.host_token,
            "settings": self.settings,
            "players": [self.players[t].public() for t in self.order if t in self.players],
            "invite": self.manager.invite_url(self.code),
            "modes": {key: {"title": title, "about": about}
                      for key, (title, about) in MODE_INFO.items()},
        }

    def broadcast_room(self):
        self.broadcast(self.room_snapshot())

    def push_game_state(self, only=None, with_canvas=False):
        if self.mode is None:
            return
        for player in self.online_players():
            if only is not None and player.token not in only:
                continue
            player.send(self.mode.state_for(player, with_canvas=with_canvas))

    # --- игра ---------------------------------------------------------

    def update_settings(self, patch):
        for key, value in (patch or {}).items():
            if key not in DEFAULT_SETTINGS:
                continue
            limit = SETTINGS_LIMITS.get(key)
            if isinstance(limit, tuple):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
                value = max(limit[0], min(limit[1], value))
            elif isinstance(limit, set):
                if value not in limit:
                    continue
            elif key == "custom_words":
                value = str(value)[:4000]
            self.settings[key] = value
        self.broadcast_room()

    def start_game(self):
        from app.modes import build_mode

        if self.state == "playing":
            return
        mode = build_mode(self.settings.get("mode", "guess"), self)
        error = mode.validate()
        if error:
            self.broadcast({"t": "toast", "text": error})
            return
        for player in self.players.values():
            player.score = 0
        self.chat.clear()
        self.mode = mode
        self.state = "playing"
        self.broadcast_room()
        mode.start()

    def finish_game(self):
        if self.mode is not None:
            self.mode.stop()
        self.state = "results"
        table = sorted(
            (self.players[t].public() for t in self.order if t in self.players),
            key=lambda p: -p["score"],
        )
        payload = {"t": "results", "table": table}
        if self.mode is not None:
            payload.update(self.mode.results_payload())
        self.broadcast_room()
        self.broadcast(payload)

    def back_to_lobby(self):
        if self.mode is not None:
            self.mode.stop()
            self.mode = None
        self.state = "lobby"
        self.chat.clear()
        for token in list(self.order):
            player = self.players.get(token)
            if player is not None and not player.online:
                self.drop(player)
        self.broadcast_room()

    def handle(self, player, message):
        kind = message.get("t")
        if kind == "chat":
            text = str(message.get("text", "")).strip()[:200]
            if not text:
                return
            if self.mode is not None and self.mode.handle_chat(player, text):
                return
            self.push_chat({
                "t": "chat", "kind": "chat", "text": text,
                "from": player.nick, "color": player.color,
            })
        elif kind == "settings":
            if player.token == self.host_token and self.state == "lobby":
                self.update_settings(message.get("patch"))
        elif kind == "start":
            if player.token == self.host_token and self.state in ("lobby", "results"):
                self.start_game()
        elif kind == "again":
            if player.token == self.host_token and self.state == "results":
                self.back_to_lobby()
        elif kind == "kick":
            target = self.players.get(message.get("token"))
            if player.token == self.host_token and target is not None and target is not player:
                if target.conn is not None:
                    target.conn.close_with_reason("Вас выгнали из комнаты")
                self.drop(target)
                self.broadcast_room()
        elif kind == "nick":
            nick = self.unique_nick(message.get("nick"), skip_token=player.token)
            player.nick = nick
            self.broadcast_room()
        elif self.mode is not None:
            self.mode.handle(player, message)


class RoomManager:
    def __init__(self):
        self.rooms = {}
        self._local_ip = None
        self._http_port = None
        self._cleanup = PeriodicCallback(self.cleanup, 30_000)
        try:
            self._cleanup.start()
        except Exception:  # ioloop ещё не запущен в тестах
            pass

    def local_ip(self):
        if self._local_ip is None:
            self._local_ip = discovery.local_ip()
        return self._local_ip

    def set_port(self, port):
        self._http_port = port

    def invite_url(self, code):
        if not self._http_port:
            return ""
        return "http://%s:%d/room/%s" % (self.local_ip(), self._http_port, code)

    def new_code(self):
        while True:
            code = "".join(random.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self.rooms:
                return code

    def create(self):
        code = self.new_code()
        room = Room(code, self)
        self.rooms[code] = room
        log.info("Создана комната %s", code)
        return room

    def get(self, code):
        return self.rooms.get((code or "").upper())

    def public_rooms(self):
        """Что показывать соседям по сети: только лобби, куда можно зайти."""
        result = []
        for room in self.rooms.values():
            if room.state != "lobby":
                continue
            players = [room.players[t] for t in room.order if t in room.players]
            if not players:
                continue
            host = room.players.get(room.host_token)
            result.append({
                "code": room.code,
                "owner": host.nick if host else "",
                "players": len(players),
                "max": MAX_PLAYERS,
                "mode": MODE_INFO.get(room.settings.get("mode"), ("", ""))[0],
            })
        return result

    def cleanup(self):
        now = time.time()
        for code, room in list(self.rooms.items()):
            if room.empty_since and now - room.empty_since > EMPTY_ROOM_TTL:
                if room.mode is not None:
                    room.mode.stop()
                del self.rooms[code]
                log.info("Комната %s удалена (все ушли)", code)
