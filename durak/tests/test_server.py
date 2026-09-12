# -*- coding: utf-8 -*-
"""Интеграционные тесты: реальные WebSocket-соединения к tornado-серверу.

Запуск: python3 -m unittest discover -s tests
Требуют установленного/положенного рядом tornado.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tornado import gen
    from tornado.testing import AsyncHTTPTestCase, gen_test
    from tornado.websocket import websocket_connect
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("tornado не найден")

import server  # noqa: E402


class Client:
    """Тонкая обёртка: копит входящие сообщения в список."""

    def __init__(self):
        self.conn = None
        self.inbox = []

    async def connect(self, url):
        self.conn = await websocket_connect(
            url, on_message_callback=self._on_message)
        return self

    def _on_message(self, raw):
        if raw is None:
            return
        self.inbox.append(json.loads(raw))

    async def send(self, **kw):
        self.conn.write_message(json.dumps(kw))
        await gen.sleep(0.05)

    async def wait_err(self, substring, tries=100):
        for _ in range(tries):
            for msg in self.inbox:
                if msg.get("t") == "err" and substring in msg["msg"]:
                    return msg["msg"]
            await gen.sleep(0.01)
        raise AssertionError("не дождался ошибки про %r, получено: %s"
                             % (substring, self.errors()))

    async def wait(self, kind, tries=100):
        for _ in range(tries):
            for msg in reversed(self.inbox):
                if msg.get("t") == kind:
                    return msg
            await gen.sleep(0.01)
        raise AssertionError("не дождался сообщения %r, получено: %s"
                             % (kind, [m.get("t") for m in self.inbox]))

    def last(self, kind):
        for msg in reversed(self.inbox):
            if msg.get("t") == kind:
                return msg
        return None

    def errors(self):
        return [m["msg"] for m in self.inbox if m.get("t") == "err"]

    def clear(self):
        self.inbox = []

    def close(self):
        if self.conn:
            self.conn.close()


class ServerTest(AsyncHTTPTestCase):
    def setUp(self):
        server.HUB.players.clear()
        server.HUB.rooms.clear()
        self._clients = []
        super().setUp()

    def tearDown(self):
        for c in getattr(self, "_clients", []):
            c.close()
        super().tearDown()

    def get_app(self):
        return server.make_app()

    def ws_url(self):
        return self.get_url("/ws").replace("http://", "ws://")

    async def login(self, name, token=None):
        c = Client()
        await c.connect(self.ws_url())
        await c.send(t="hello", token=token, name=name)
        hello = await c.wait("hello")
        c.token = hello["token"]
        c.name = hello["name"]
        self._clients.append(c)
        return c

    def test_index_page(self):
        page = self.fetch("/")
        self.assertEqual(page.code, 200)
        self.assertIn("Дурак", page.body.decode("utf-8"))
        self.assertEqual(self.fetch("/static/app.js").code, 200)
        self.assertEqual(self.fetch("/static/style.css").code, 200)

    @gen_test
    async def test_create_and_join(self):
        host = await self.login("Хозяин")
        rooms = await host.wait("rooms")
        self.assertEqual(rooms["rooms"], [])

        await host.send(t="create", title="Тестовая",
                        settings={"deck_size": 36, "max_players": 3,
                                  "transfer": True, "throw_in": "neighbors",
                                  "max_attack_cards": 6})
        room = (await host.wait("room"))["room"]
        self.assertEqual(room["title"], "Тестовая")
        self.assertTrue(room["is_host"])
        self.assertFalse(room["can_start"])
        self.assertTrue(room["settings"]["transfer"])
        code = room["id"]

        guest = await self.login("Гость")
        lobby = await guest.wait("rooms")
        self.assertEqual(len(lobby["rooms"]), 1)
        self.assertEqual(lobby["rooms"][0]["id"], code)

        await guest.send(t="join", room=code)
        groom = (await guest.wait("room"))["room"]
        self.assertEqual(len(groom["players"]), 2)
        self.assertFalse(groom["is_host"])
        hroom = host.last("room")["room"]
        self.assertTrue(hroom["can_start"])
        self.assertEqual(host.errors(), [])

    @gen_test
    async def test_join_errors(self):
        c = await self.login("Один")
        await c.send(t="join", room="ZZZZ")
        await c.wait_err("Комната ZZZZ не найдена")

        await c.send(t="create", settings={"deck_size": 24, "max_players": 2})
        code = (await c.wait("room"))["room"]["id"]
        a = await self.login("Второй")
        await a.send(t="join", room=code)
        b = await self.login("Третий")
        await b.send(t="join", room=code)
        await b.wait_err("нет свободных мест")

    @gen_test
    async def test_bad_settings_rejected(self):
        c = await self.login("Хозяин")
        await c.send(t="create", settings={"deck_size": 24, "max_players": 6})
        await c.wait_err("не раздать")
        await c.send(t="create", settings={"deck_size": 40, "max_players": 2})
        await c.wait_err("Размер колоды")

    @gen_test
    async def test_start_requires_two_players(self):
        c = await self.login("Хозяин")
        await c.send(t="create", settings={})
        await c.wait("room")
        await c.send(t="start")
        await c.wait_err("Нужно минимум два игрока")

    @gen_test
    async def test_only_host_controls_room(self):
        host = await self.login("Хозяин")
        await host.send(t="create", settings={})
        code = (await host.wait("room"))["room"]["id"]
        guest = await self.login("Гость")
        await guest.send(t="join", room=code)
        await guest.wait("room")
        await guest.send(t="start")
        await guest.wait_err("только ведущий")
        guest.clear()
        await guest.send(t="settings", settings={"deck_size": 52, "max_players": 2})
        await guest.wait_err("только ведущий")

    async def _start_game(self, players=2, **settings):
        cfg = {"deck_size": 36, "max_players": max(players, 2)}
        cfg.update(settings)
        host = await self.login("Игрок1")
        await host.send(t="create", settings=cfg)
        code = (await host.wait("room"))["room"]["id"]
        clients = [host]
        for i in range(2, players + 1):
            c = await self.login("Игрок%d" % i)
            await c.send(t="join", room=code)
            await c.wait("room")
            clients.append(c)
        await host.send(t="start")
        for c in clients:
            await c.wait("room")
        return clients

    @gen_test
    async def test_deal_is_consistent(self):
        clients = await self._start_game(3)
        views = [c.last("room")["room"]["game"] for c in clients]
        seats = sorted(v["seat"] for v in views)
        self.assertEqual(seats, [0, 1, 2])
        for v in views:
            self.assertEqual(len(v["hand"]), 6)
            self.assertEqual(v["deck"], 36 - 18)
            self.assertEqual(len(v["players"]), 3)
            # чужие руки не утекают
            for p in v["players"]:
                self.assertNotIn("hand", p)
        # карты не дублируются между руками
        all_cards = [c for v in views for c in v["hand"]]
        self.assertEqual(len(all_cards), len(set(all_cards)))

    @gen_test
    async def test_illegal_move_rejected(self):
        clients = await self._start_game(2)
        views = [c.last("room")["room"]["game"] for c in clients]
        defender = next(c for c, v in zip(clients, views)
                        if v["seat"] == v["defender"])
        dview = defender.last("room")["room"]["game"]
        await defender.send(t="move", action="attack", card=dview["hand"][0])
        await defender.wait_err("отбиваетесь")

    @gen_test(timeout=120)
    async def test_full_game_to_the_end(self):
        """Играем партию до конца, выбирая первый допустимый ход."""
        clients = await self._start_game(3, transfer=True)
        finished = None
        for _ in range(600):
            acted = False
            for c in clients:
                view = c.last("room")["room"]["game"]
                if view["finished"]:
                    finished = view
                    break
                acts = view["actions"]
                if acts["defend"]:
                    card, targets = sorted(acts["defend"].items())[0]
                    await c.send(t="move", action="defend", card=card,
                                 target=targets[0])
                elif acts["attack"]:
                    await c.send(t="move", action="attack", card=acts["attack"][0])
                elif acts["can_pass"]:
                    await c.send(t="move", action="pass")
                elif acts["can_take"]:
                    await c.send(t="move", action="take")
                else:
                    continue
                acted = True
                break
            if finished:
                break
            self.assertTrue(acted, "партия зависла: никто не может ходить")
        self.assertIsNotNone(finished, "партия не завершилась за 600 ходов")
        self.assertTrue(finished["draw"] or finished["durak"])
        for c in clients:
            self.assertEqual(c.errors(), [])
        # счёт проигрышей обновился
        if finished["durak"]:
            room = clients[0].last("room")["room"]
            loser = next(p for p in room["players"] if p["id"] == finished["durak"])
            self.assertEqual(loser["losses"], 1)

    @gen_test
    async def test_offline_player_is_not_dealt_in(self):
        host = await self.login("Хозяин")
        await host.send(t="create", settings={"max_players": 4})
        code = (await host.wait("room"))["room"]["id"]
        stayed = await self.login("Остался")
        await stayed.send(t="join", room=code)
        ghost = await self.login("Призрак")
        await ghost.send(t="join", room=code)
        await ghost.wait("room")
        ghost.close()
        await gen.sleep(0.15)

        await host.send(t="start")
        await gen.sleep(0.1)
        room = host.last("room")["room"]
        names = sorted(p["name"] for p in room["players"])
        self.assertEqual(names, ["Остался", "Хозяин"])
        self.assertEqual(len(room["game"]["players"]), 2)

    @gen_test
    async def test_start_needs_two_online(self):
        host = await self.login("Хозяин")
        await host.send(t="create", settings={})
        code = (await host.wait("room"))["room"]["id"]
        ghost = await self.login("Призрак")
        await ghost.send(t="join", room=code)
        await ghost.wait("room")
        ghost.close()
        await gen.sleep(0.15)
        await host.send(t="start")
        await host.wait_err("два игрока на связи")

    @gen_test
    async def test_reconnect_restores_seat_and_hand(self):
        clients = await self._start_game(2)
        victim = clients[1]
        before = victim.last("room")["room"]["game"]
        victim.close()
        await gen.sleep(0.1)
        # партнёр видит, что игрок не в сети
        await clients[0].send(t="chat", text="ты тут?")
        room = clients[0].last("room")["room"]
        other = next(p for p in room["game"]["players"] if p["seat"] == before["seat"])
        self.assertFalse(other["online"])

        again = await self.login("Игрок2", token=victim.token)
        restored = (await again.wait("room"))["room"]
        self.assertEqual(restored["game"]["seat"], before["seat"])
        self.assertEqual(restored["game"]["hand"], before["hand"])
        self.assertEqual(restored["id"], room["id"])

    @gen_test
    async def test_leave_during_game_keeps_seat(self):
        clients = await self._start_game(2)
        leaver = clients[1]
        seat = leaver.last("room")["room"]["game"]["seat"]
        await leaver.send(t="leave")
        await leaver.wait("rooms")
        room = clients[0].last("room")["room"]
        self.assertEqual(len(room["players"]), 2)
        self.assertTrue(room["players"][seat]["away"])
        # вернуться в свою партию можно по коду комнаты
        await leaver.send(t="join", room=room["id"])
        back = (await leaver.wait("room"))["room"]
        self.assertEqual(back["game"]["seat"], seat)

    @gen_test
    async def test_host_passes_when_leaving_lobby(self):
        host = await self.login("Хозяин")
        await host.send(t="create", settings={})
        code = (await host.wait("room"))["room"]["id"]
        guest = await self.login("Гость")
        await guest.send(t="join", room=code)
        await guest.wait("room")
        await host.send(t="leave")
        await gen.sleep(0.05)
        room = guest.last("room")["room"]
        self.assertTrue(room["is_host"])
        self.assertEqual(len(room["players"]), 1)

    @gen_test
    async def test_abort_returns_to_lobby(self):
        clients = await self._start_game(2)
        await clients[0].send(t="abort")
        await gen.sleep(0.05)
        for c in clients:
            self.assertEqual(c.last("room")["room"]["phase"], "lobby")

    @gen_test
    async def test_chat_reaches_everyone(self):
        clients = await self._start_game(2)
        await clients[1].send(t="chat", text="привет всем")
        await gen.sleep(0.05)
        for c in clients:
            texts = [m["text"] for m in c.last("room")["room"]["chat"]]
            self.assertIn("привет всем", texts)

    @gen_test
    async def test_unknown_command(self):
        c = await self.login("Кто-то")
        await c.send(t="on_close")
        await c.wait_err("Неизвестная команда")


if __name__ == "__main__":
    unittest.main()
