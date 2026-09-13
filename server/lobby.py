"""Лобби: комнаты внутри одного сервера.

Кто запустил main.py — тот сервер. Остальные заходят по его IP. «Поиск лобби»
это список комнат на этом сервере, никакого сетевого обнаружения: в локальной
сети проще один раз сказать адрес, чем отлаживать широковещательные пакеты.
"""

import random
import time

# Без похожих символов: код лобби диктуют голосом через комнату
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

PALETTE = ["#ff5252", "#4fc3f7", "#ffd23f", "#66bb6a", "#ba68c8", "#ff8a65",
           "#4db6ac", "#f06292"]

MAX_NAME = 18
MAX_LOBBY_NAME = 28
MIN_PLAYERS = 2
MAX_PLAYERS = 6


def clean_name(s, fallback="Гонщик", limit=MAX_NAME):
    s = (s or "").strip().replace("\n", " ")
    s = "".join(ch for ch in s if ch.isprintable())[:limit].strip()
    return s or fallback


class LobbyPlayer:
    __slots__ = ("token", "name", "color", "ready", "joined", "online")

    def __init__(self, token, name, color):
        self.token = token
        self.name = name
        self.color = color
        self.ready = False
        self.joined = time.time()
        self.online = True


class Lobby:
    def __init__(self, lid, name, host_token, settings, password=""):
        self.id = lid
        self.name = name
        self.host = host_token
        self.password = password or ""
        self.settings = settings
        self.players = {}
        self.order = []
        self.state = "waiting"          # waiting | racing | results
        self.race = None
        self.results = None
        self.created = time.time()
        self.chat = []
        self.hidden = False        # заезды на время в общем списке не нужны

    # ------------------------------------------------------------------ игроки

    @property
    def max_players(self):
        return int(self.settings.get("max", MAX_PLAYERS))

    def is_full(self):
        return len(self.order) >= self.max_players

    def add(self, token, name):
        if token in self.players:
            return self.players[token]
        used = {p.color for p in self.players.values()}
        color = next((c for c in PALETTE if c not in used), PALETTE[0])
        p = LobbyPlayer(token, name, color)
        self.players[token] = p
        self.order.append(token)
        if self.host not in self.players:
            self.host = token
        return p

    def remove(self, token):
        self.players.pop(token, None)
        if token in self.order:
            self.order.remove(token)
        if self.host == token and self.order:
            self.host = self.order[0]

    def roster(self):
        return [self.players[t] for t in self.order if t in self.players]

    def everyone_ready(self):
        rs = self.roster()
        if not rs:
            return False
        return all(p.ready or p.token == self.host for p in rs)

    def say(self, name, text):
        self.chat.append({"name": name, "text": text, "at": time.time()})
        del self.chat[:-40]

    # -------------------------------------------------------------- состояние

    def brief(self):
        """Строка для списка лобби."""
        return {
            "id": self.id,
            "name": self.name,
            "players": len(self.order),
            "max": self.max_players,
            "track": self.settings.get("track"),
            "laps": self.settings.get("laps"),
            "powerups": self.settings.get("powerups", True),
            "collisions": self.settings.get("collisions", True),
            "locked": bool(self.password),
            "state": self.state,
        }

    def full(self):
        return {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "state": self.state,
            "settings": self.settings,
            "locked": bool(self.password),
            "players": [{"token": p.token, "name": p.name, "color": p.color,
                         "ready": p.ready, "host": p.token == self.host,
                         "online": p.online}
                        for p in self.roster()],
            "chat": self.chat[-20:],
        }


class LobbyManager:
    def __init__(self, tracks):
        self.lobbies = {}
        self.tracks = tracks

    def new_code(self):
        while True:
            code = "".join(random.choice(CODE_ALPHABET) for _ in range(4))
            if code not in self.lobbies:
                return code

    def sanitize(self, raw):
        """Настройки лобби приходят из браузера — доверять им нельзя."""
        s = raw if isinstance(raw, dict) else {}
        track = s.get("track")
        if track not in self.tracks:
            track = next(iter(self.tracks))
        laps = s.get("laps")
        try:
            laps = int(laps)
        except (TypeError, ValueError):
            laps = self.tracks[track].laps
        laps = max(1, min(20, laps))
        try:
            mx = int(s.get("max", MAX_PLAYERS))
        except (TypeError, ValueError):
            mx = MAX_PLAYERS
        mx = max(MIN_PLAYERS, min(MAX_PLAYERS, mx))
        return {
            "track": track,
            "laps": laps,
            "max": mx,
            "powerups": bool(s.get("powerups", True)),
            "collisions": bool(s.get("collisions", True)),
            "mode": "race",
        }

    def create(self, host_token, name, settings, password=""):
        lid = self.new_code()
        lb = Lobby(lid, clean_name(name, "Заезд", MAX_LOBBY_NAME), host_token,
                   self.sanitize(settings), (password or "").strip()[:24])
        self.lobbies[lid] = lb
        return lb

    def drop(self, lid):
        self.lobbies.pop(lid, None)

    def listing(self):
        return [lb.brief() for lb in sorted(self.lobbies.values(),
                                            key=lambda l: l.created)
                if not lb.hidden]

    def sweep(self):
        """Убирает пустые комнаты, чтобы список не зарастал."""
        dead = [lid for lid, lb in self.lobbies.items()
                if not lb.order and time.time() - lb.created > 20]
        for lid in dead:
            del self.lobbies[lid]
        return dead
