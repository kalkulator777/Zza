#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сервер игры «Дурак» для локальной сети.

Запуск:   python3 server.py            (порт 8888)
          python3 server.py --port 9000

Папку tornado достаточно положить рядом с этим файлом — внешний pip не нужен.
Игроки открывают в браузере http://<ip-этой-машины>:8888/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import socket
import string
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # чтобы находились engine.py и локальная папка tornado

try:
    import tornado.ioloop
    import tornado.web
    import tornado.websocket
except ImportError:  # pragma: no cover
    sys.exit("Не найден tornado. Положите папку tornado рядом с server.py "
             "или установите: pip install --no-index tornado-*.whl")

from engine import Game, GameError, Settings

log = logging.getLogger("durak")

ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_NAME = 20
MAX_CHAT = 200
ROOM_TTL = 6 * 3600      # комната без живых игроков живёт 6 часов
CHAT_HISTORY = 40


def clean_text(value, limit) -> str:
    text = "" if value is None else str(value)
    text = "".join(ch for ch in text if ch.isprintable()).strip()
    return text[:limit]


# --- модель ----------------------------------------------------------------

class Player:
    def __init__(self, pid: str, name: str):
        self.pid = pid
        self.name = name
        self.conn = None          # текущее WebSocket-соединение или None
        self.room_id = None       # комната, экран которой он сейчас видит
        self.last_seen = time.time()

    @property
    def online(self) -> bool:
        return self.conn is not None

    def send(self, payload: dict) -> None:
        if self.conn is not None:
            self.conn.send(payload)


class Room:
    def __init__(self, rid: str, title: str, host: str, settings: Settings):
        self.id = rid
        self.title = title
        self.host = host
        self.settings = settings
        self.members = [host]     # порядок мест за столом
        self.away = set()         # участники, ушедшие из комнаты во время партии
        self.game = None
        self.chat = []
        self.created = time.time()
        self.touched = time.time()
        self.scores = {}          # pid -> сколько раз остался дураком

    @property
    def in_game(self) -> bool:
        return self.game is not None and not self.game.finished

    def is_member(self, pid: str) -> bool:
        return pid in self.members


class Hub:
    def __init__(self):
        self.players = {}   # pid -> Player
        self.rooms = {}     # rid -> Room
        self.peers_provider = None  # ставит durak.py: поиск игр в локальной сети

    def peers(self) -> list:
        """Другие компьютеры сети, где запущен «Дурак»."""
        if self.peers_provider is None:
            return []
        try:
            return self.peers_provider()
        except Exception:  # pragma: no cover
            log.exception("Не смог получить список соседей")
            return []

    # -- игроки ---------------------------------------------------------

    def player(self, pid):
        return self.players.get(pid)

    def ensure_player(self, token, name) -> Player:
        name = clean_text(name, MAX_NAME) or "Игрок"
        if token and token in self.players:
            p = self.players[token]
            if name:
                p.name = name
            p.last_seen = time.time()
            return p
        pid = token if token and len(str(token)) <= 64 else uuid.uuid4().hex
        p = Player(pid, name)
        self.players[pid] = p
        return p

    # -- комнаты --------------------------------------------------------

    def new_room_code(self) -> str:
        while True:
            code = "".join(random.choice(ROOM_CODE_ALPHABET) for _ in range(4))
            if code not in self.rooms:
                return code

    def create_room(self, host: Player, title: str, settings: Settings) -> Room:
        rid = self.new_room_code()
        room = Room(rid, clean_text(title, 30) or ("Комната %s" % rid),
                    host.pid, settings)
        self.rooms[rid] = room
        host.room_id = rid
        return room

    def drop_member(self, room: Room, pid: str) -> None:
        """Полностью убрать игрока из комнаты (только вне партии)."""
        if pid in room.members:
            room.members.remove(pid)
        room.away.discard(pid)
        room.scores.pop(pid, None)
        p = self.players.get(pid)
        if p and p.room_id == room.id:
            p.room_id = None
        if not room.members:
            self.rooms.pop(room.id, None)
        elif room.host == pid:
            room.host = room.members[0]
            self.system_msg(room, "Комнату ведёт %s" % self.name_of(room.host))

    def name_of(self, pid: str) -> str:
        p = self.players.get(pid)
        return p.name if p else "?"

    def system_msg(self, room: Room, text: str) -> None:
        room.chat.append({"from": None, "text": text, "ts": time.time()})
        del room.chat[:-CHAT_HISTORY]

    def fix_host(self, room: Room) -> None:
        """Если ведущий пропал — передаём комнату первому живому участнику."""
        host = self.players.get(room.host)
        host_alive = host is not None and host.online and room.host not in room.away
        if host_alive:
            return
        for pid in room.members:
            p = self.players.get(pid)
            if p and p.online and pid not in room.away:
                if room.host != pid:
                    room.host = pid
                    self.system_msg(room, "Комнату ведёт %s" % p.name)
                return

    # -- рассылка -------------------------------------------------------

    def rooms_payload(self) -> dict:
        items = []
        for room in sorted(self.rooms.values(), key=lambda r: r.created):
            items.append({
                "id": room.id,
                "title": room.title,
                "host": self.name_of(room.host),
                "players": len(room.members),
                "online": sum(1 for pid in room.members
                              if self.players.get(pid) and self.players[pid].online
                              and pid not in room.away),
                "max": room.settings.max_players,
                "settings": room.settings.to_dict(),
                "in_game": room.in_game,
            })
        return {"t": "rooms", "rooms": items, "peers": self.peers()}

    def room_payload(self, room: Room, pid: str) -> dict:
        online = {}
        players = []
        for seat, mid in enumerate(room.members):
            p = self.players.get(mid)
            is_online = bool(p and p.online and mid not in room.away)
            online[mid] = is_online
            players.append({
                "id": mid,
                "seat": seat,
                "name": self.name_of(mid),
                "online": is_online,
                "away": mid in room.away,
                "host": mid == room.host,
                "losses": room.scores.get(mid, 0),
            })
        payload = {
            "t": "room",
            "room": {
                "id": room.id,
                "title": room.title,
                "host": room.host,
                "you": pid,
                "is_host": pid == room.host,
                "settings": room.settings.to_dict(),
                "players": players,
                "phase": "game" if room.game is not None else "lobby",
                "can_start": (pid == room.host and not room.in_game
                              and len(room.members) >= 2),
                "chat": [{"name": self.name_of(m["from"]) if m["from"] else None,
                          "text": m["text"]} for m in room.chat[-CHAT_HISTORY:]],
            },
        }
        if room.game is not None:
            payload["room"]["game"] = room.game.view_for(pid, online=online)
        return payload

    def push_room(self, room: Room) -> None:
        room.touched = time.time()
        for pid in list(room.members):
            p = self.players.get(pid)
            if p and p.online and p.room_id == room.id:
                p.send(self.room_payload(room, pid))
        self.push_lobby()

    def push_lobby(self) -> None:
        payload = self.rooms_payload()
        for p in self.players.values():
            if p.online and p.room_id is None:
                p.send(payload)

    # -- уборка ---------------------------------------------------------

    def cleanup(self) -> None:
        now = time.time()
        for rid, room in list(self.rooms.items()):
            alive = any(self.players.get(pid) and self.players[pid].online
                        for pid in room.members)
            if alive:
                room.touched = now
                continue
            if now - room.touched > ROOM_TTL:
                log.info("Удаляю заброшенную комнату %s", rid)
                for pid in room.members:
                    p = self.players.get(pid)
                    if p and p.room_id == rid:
                        p.room_id = None
                self.rooms.pop(rid, None)
        for pid, p in list(self.players.items()):
            if not p.online and p.room_id is None and now - p.last_seen > ROOM_TTL:
                self.players.pop(pid, None)


HUB = Hub()


# --- websocket -------------------------------------------------------------

class GameSocket(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):  # в локалке ходят по разным адресам
        return True

    def open(self):
        self.player = None

    def send(self, payload: dict) -> None:
        try:
            self.write_message(json.dumps(payload, ensure_ascii=False))
        except tornado.websocket.WebSocketClosedError:
            pass
        except Exception:  # pragma: no cover
            log.exception("Не смог отправить сообщение")

    def fail(self, message: str) -> None:
        self.send({"t": "err", "msg": message})

    # -- приём ---------------------------------------------------------

    def on_message(self, raw):
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                raise ValueError
        except (ValueError, TypeError):
            return self.fail("Некорректный запрос")

        kind = msg.get("t")
        if kind == "hello":
            return self.on_hello(msg)
        if self.player is None:
            return self.fail("Сначала представьтесь")
        handler = self.COMMANDS.get(kind)
        if handler is None:
            return self.fail("Неизвестная команда: %s" % kind)
        try:
            handler(self, msg)
        except GameError as exc:
            self.fail(str(exc))
        except Exception:  # pragma: no cover
            log.exception("Ошибка обработки %s", kind)
            self.fail("Внутренняя ошибка сервера")

    def on_hello(self, msg):
        token = msg.get("token")
        player = HUB.ensure_player(token, msg.get("name"))
        if player.conn is not None and player.conn is not self:
            # второе окно с тем же токеном — отключаем прежнее
            try:
                player.conn.send({"t": "err", "msg": "Сессия открыта в другом окне"})
                player.conn.close()
            except Exception:
                pass
        player.conn = self
        player.last_seen = time.time()
        self.player = player
        self.send({"t": "hello", "token": player.pid, "name": player.name})

        room = HUB.rooms.get(player.room_id) if player.room_id else None
        if room is None or not room.is_member(player.pid):
            player.room_id = None
            self.send(HUB.rooms_payload())
        else:
            room.away.discard(player.pid)
            HUB.fix_host(room)
            HUB.push_room(room)
        HUB.push_lobby()

    # -- лобби ---------------------------------------------------------

    def on_name(self, msg):
        name = clean_text(msg.get("name"), MAX_NAME)
        if not name:
            return self.fail("Имя не может быть пустым")
        self.player.name = name
        self.send({"t": "hello", "token": self.player.pid, "name": name})
        room = HUB.rooms.get(self.player.room_id)
        if room:
            HUB.push_room(room)
        else:
            HUB.push_lobby()

    def on_rooms(self, msg):
        self.send(HUB.rooms_payload())

    def on_create(self, msg):
        if self.player.room_id:
            return self.fail("Вы уже в комнате")
        settings = Settings.from_dict(msg.get("settings"))
        room = HUB.create_room(self.player, msg.get("title"), settings)
        HUB.system_msg(room, "Комната создана")
        log.info("Комната %s создана игроком %s", room.id, self.player.name)
        HUB.push_room(room)

    def on_join(self, msg):
        rid = clean_text(msg.get("room"), 8).upper()
        room = HUB.rooms.get(rid)
        if room is None:
            return self.fail("Комната %s не найдена" % rid)
        pid = self.player.pid
        if self.player.room_id and self.player.room_id != rid:
            return self.fail("Сначала выйдите из текущей комнаты")
        if not room.is_member(pid):
            if room.in_game:
                return self.fail("В этой комнате уже идёт партия")
            if len(room.members) >= room.settings.max_players:
                return self.fail("В комнате нет свободных мест")
            room.members.append(pid)
            room.scores.setdefault(pid, 0)
            HUB.system_msg(room, "%s присоединился" % self.player.name)
        else:
            HUB.system_msg(room, "%s вернулся" % self.player.name)
        room.away.discard(pid)
        self.player.room_id = rid
        HUB.fix_host(room)
        HUB.push_room(room)

    def on_leave(self, msg):
        room = HUB.rooms.get(self.player.room_id)
        self.player.room_id = None
        if room is None:
            return self.send(HUB.rooms_payload())
        pid = self.player.pid
        if room.in_game and room.is_member(pid):
            # место за столом сохраняем — можно вернуться в свою партию
            room.away.add(pid)
            HUB.system_msg(room, "%s ушёл из комнаты" % self.player.name)
            HUB.fix_host(room)
            HUB.push_room(room)
        else:
            HUB.system_msg(room, "%s вышел" % self.player.name)
            HUB.drop_member(room, pid)
            if room.id in HUB.rooms:
                HUB.push_room(room)
        self.send(HUB.rooms_payload())
        HUB.push_lobby()

    # -- хозяин комнаты -------------------------------------------------

    def _my_room(self, need_host=False) -> Room:
        room = HUB.rooms.get(self.player.room_id)
        if room is None:
            raise GameError("Вы не в комнате")
        if need_host and room.host != self.player.pid:
            raise GameError("Это может сделать только ведущий комнаты")
        return room

    def on_settings(self, msg):
        room = self._my_room(need_host=True)
        if room.in_game:
            raise GameError("Нельзя менять настройки во время партии")
        settings = Settings.from_dict(msg.get("settings"))
        if settings.max_players < len(room.members):
            raise GameError("В комнате уже %d игроков" % len(room.members))
        room.settings = settings
        title = clean_text(msg.get("title"), 30)
        if title:
            room.title = title
        HUB.push_room(room)

    def on_kick(self, msg):
        room = self._my_room(need_host=True)
        if room.in_game:
            raise GameError("Во время партии выгнать нельзя — прервите партию")
        pid = str(msg.get("pid") or "")
        if pid == room.host:
            raise GameError("Себя выгнать нельзя")
        if not room.is_member(pid):
            raise GameError("Такого игрока в комнате нет")
        HUB.system_msg(room, "%s выгнан из комнаты" % HUB.name_of(pid))
        victim = HUB.players.get(pid)
        HUB.drop_member(room, pid)
        if victim:
            victim.send({"t": "err", "msg": "Вас выгнали из комнаты"})
            victim.send(HUB.rooms_payload())
        if room.id in HUB.rooms:
            HUB.push_room(room)
        HUB.push_lobby()

    def on_start(self, msg):
        room = self._my_room(need_host=True)
        if room.game is not None and not room.game.finished:
            raise GameError("Партия уже идёт")
        seats = []
        for pid in room.members:
            p = HUB.players.get(pid)
            if pid in room.away or p is None or not p.online:
                continue  # ушедших и отвалившихся за стол не сажаем
            seats.append(pid)
        if len(seats) < 2:
            raise GameError("Нужно минимум два игрока на связи")
        for pid in room.members:
            if pid not in seats:
                left = HUB.players.get(pid)
                if left is not None and left.room_id == room.id:
                    left.room_id = None
                HUB.system_msg(room, "%s не на связи — пропускает партию"
                               % HUB.name_of(pid))
        room.members = seats
        room.away.intersection_update(seats)
        players = [(pid, HUB.name_of(pid)) for pid in seats]
        room.game = Game(players, room.settings)
        for pid in seats:
            room.scores.setdefault(pid, 0)
        HUB.system_msg(room, "Партия началась")
        log.info("Комната %s: партия на %d игроков", room.id, len(seats))
        HUB.push_room(room)

    def on_abort(self, msg):
        room = self._my_room(need_host=True)
        if room.game is None:
            raise GameError("Партия не идёт")
        room.game = None
        HUB.system_msg(room, "Партия прервана ведущим")
        HUB.push_room(room)

    def on_again(self, msg):
        room = self._my_room(need_host=True)
        if room.game is not None and not room.game.finished:
            raise GameError("Партия ещё не закончена")
        room.game = None
        self.on_start(msg)

    # -- игра -----------------------------------------------------------

    def on_move(self, msg):
        room = self._my_room()
        if room.game is None:
            raise GameError("Партия не идёт")
        room.game.apply(self.player.pid, str(msg.get("action") or ""),
                        msg.get("card"), msg.get("target"))
        game = room.game
        if game.finished and game.durak:
            room.scores[game.durak] = room.scores.get(game.durak, 0) + 1
        HUB.push_room(room)

    def on_chat(self, msg):
        room = self._my_room()
        text = clean_text(msg.get("text"), MAX_CHAT)
        if not text:
            return
        room.chat.append({"from": self.player.pid, "text": text, "ts": time.time()})
        del room.chat[:-CHAT_HISTORY]
        HUB.push_room(room)

    # -- разрыв связи ---------------------------------------------------

    def on_close(self):
        player = getattr(self, "player", None)
        if player is None:
            return
        if player.conn is self:
            player.conn = None
            player.last_seen = time.time()
        room = HUB.rooms.get(player.room_id)
        if room is None:
            return HUB.push_lobby()
        if room.game is None and not room.is_member(player.pid):
            return HUB.push_lobby()
        HUB.fix_host(room)
        HUB.push_room(room)


GameSocket.COMMANDS = {
    "name": GameSocket.on_name,
    "rooms": GameSocket.on_rooms,
    "create": GameSocket.on_create,
    "join": GameSocket.on_join,
    "leave": GameSocket.on_leave,
    "settings": GameSocket.on_settings,
    "kick": GameSocket.on_kick,
    "start": GameSocket.on_start,
    "abort": GameSocket.on_abort,
    "again": GameSocket.on_again,
    "move": GameSocket.on_move,
    "chat": GameSocket.on_chat,
}


class IndexHandler(tornado.web.RequestHandler):
    def get(self):
        self.set_header("Cache-Control", "no-store")
        self.set_header("Content-Type", "text/html; charset=utf-8")
        with open(os.path.join(HERE, "static", "index.html"), "rb") as fh:
            self.write(fh.read())


def local_addresses():
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except socket.gaierror:
        pass
    try:  # адрес, через который машина ходит в сеть
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    addrs.discard("127.0.0.1")
    return sorted(addrs)


def make_app(debug=False):
    return tornado.web.Application(
        [(r"/", IndexHandler), (r"/ws", GameSocket)],
        static_path=os.path.join(HERE, "static"),
        websocket_ping_interval=20,
        debug=debug,
    )


def main():
    parser = argparse.ArgumentParser(description="Сервер игры «Дурак» для LAN")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--host", default="0.0.0.0",
                        help="интерфейс для прослушивания (по умолчанию все)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = make_app(args.debug)
    app.listen(args.port, address=args.host)
    tornado.ioloop.PeriodicCallback(HUB.cleanup, 60_000).start()

    print("Сервер «Дурак» запущен. Открывайте в браузере:")
    print("  http://localhost:%d/" % args.port)
    for ip in local_addresses():
        print("  http://%s:%d/   <- этот адрес давайте остальным" % (ip, args.port))
    print("Остановить: Ctrl+C")
    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    main()
