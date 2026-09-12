#!/usr/bin/env python3
"""Прогон игровых сценариев без браузера.

Поднимает сервер на свободном порту, играет партии двумя клиентами и
проверяет механику каждого режима.

    python3 tests/game_test.py
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tornado.httpclient import AsyncHTTPClient          # noqa: E402
from tornado.websocket import websocket_connect         # noqa: E402

BASE = None
SCENARIOS = []


def scenario(title):
    def wrap(fn):
        SCENARIOS.append((title, fn))
        return fn
    return wrap


class Client:
    """Игрок, говорящий с сервером по тому же протоколу, что и браузер."""

    def __init__(self, nick):
        self.nick = nick
        self.inbox = []
        self.token = None
        self.game = None
        self.room = None
        self.results = None

    async def join(self, code):
        self.ws = await websocket_connect(BASE.replace("http", "ws") + "/ws")
        await self.send({"t": "join", "code": code, "nick": self.nick, "token": None})
        asyncio.ensure_future(self.pump())

    async def send(self, message):
        await self.ws.write_message(json.dumps(message))

    async def pump(self):
        while True:
            raw = await self.ws.read_message()
            if raw is None:
                return
            message = json.loads(raw)
            self.inbox.append(message)
            kind = message["t"]
            if kind == "joined":
                self.token = message["token"]
            elif kind == "game":
                self.game = message
            elif kind == "room":
                self.room = message
            elif kind == "results":
                self.results = message

    async def wait(self, check, what, timeout=10):
        for _ in range(int(timeout * 50)):
            try:
                if check():
                    return
            except (TypeError, AttributeError, KeyError):
                pass
            await asyncio.sleep(0.02)
        raise AssertionError("%s не дождался: %s (состояние: %s)" % (self.nick, what, self.game))

    async def in_task(self, step):
        await self.wait(lambda: self.game and self.game.get("phase") == "task"
                        and self.game["round"] == step + 1, "ход %d" % (step + 1))

    async def draw(self, tag, ops=None):
        await self.send({"t": "draw", "ops": ops or [
            {"t": "p", "c": "#000000", "w": 5, "s": tag, "pts": [10 * tag, 10, 300, 400 + tag]}]})
        await asyncio.sleep(0.05)


async def start_room(patch):
    http = AsyncHTTPClient()
    response = await http.fetch(BASE + "/api/create", method="POST", body=b"")
    code = json.loads(response.body)["code"]
    a, b = Client("Аня"), Client("Боря")
    await a.join(code)
    await a.wait(lambda: a.room, "лобби")
    await b.join(code)
    await b.wait(lambda: b.room, "лобби")
    await a.send({"t": "settings", "patch": patch})
    await asyncio.sleep(0.15)
    for key, value in patch.items():
        assert a.room["settings"][key] == value, \
            "настройка %s не применилась: %s" % (key, a.room["settings"])
    await a.send({"t": "start"})
    return a, b


async def play_album(a, b, steps, phrase="фраза"):
    """Проходит альбом до конца, возвращая раскладку ходов."""
    plan = []
    for step in range(steps):
        await a.in_task(step)
        await b.in_task(step)
        plan.append((a.game["task"], a.game["sourceMode"], a.game["total"]))
        for client in (a, b):
            if a.game["task"] == "draw":
                await client.draw(step + 1)
                await client.send({"t": "submit"})
            else:
                await client.send({"t": "submit", "text": "%s %d" % (phrase, step)})
        await asyncio.sleep(0.3)
    return plan


@scenario("Угадайка: слово, штрихи, отмена, догадка, очки")
async def guess():
    a, b = await start_room({"mode": "guess", "rounds": 1, "draw_time": 40, "hints": 1})
    await a.wait(lambda: a.game, "выбор слова")
    await b.wait(lambda: b.game, "выбор слова")
    artist, guesser = (a, b) if a.game["youAreArtist"] else (b, a)
    assert "choices" not in guesser.game, "угадывающему варианты слов не показывают"

    await artist.send({"t": "pick", "index": 0})
    await asyncio.sleep(0.2)
    word = artist.game["word"]
    assert guesser.game["word"] == "", "угадывающий не видит слово"
    assert guesser.game["mask"].count("_") == len(word.replace(" ", ""))

    await artist.draw(1, [{"t": "p", "c": "#000", "w": 6, "s": 1, "pts": [10, 10, 200, 300]},
                          {"t": "p", "c": "#000", "w": 6, "s": 1, "pts": [200, 300, 400, 120]}])
    await guesser.wait(lambda: any(m["t"] == "draw" for m in guesser.inbox), "штрихи")
    await artist.send({"t": "undo"})
    await guesser.wait(lambda: any(m["t"] == "canvas" and m["ops"] == [] for m in guesser.inbox),
                       "отмена штриха целиком")

    await guesser.send({"t": "chat", "text": "  " + word.upper() + "!  "})
    await guesser.wait(lambda: any(m["t"] == "chat" and m.get("kind") == "correct" for m in guesser.inbox),
                       "засчитанная догадка")
    await asyncio.sleep(0.3)
    scores = {p["nick"]: p["score"] for p in guesser.room["players"]}
    assert scores[guesser.nick] > 0 and scores[artist.nick] > 0, scores
    assert guesser.game["phase"] == "reveal" and guesser.game["word"] == word


@scenario("Обычно: чередование текста и рисунка, показ, галерея")
async def normal():
    a, b = await start_room({"mode": "normal", "steps": 4, "write_time": 20, "draw_time": 25})
    plan = await play_album(a, b, 4)
    assert [kind for kind, _, _ in plan] == ["text", "draw", "text", "draw"], plan

    await a.wait(lambda: a.game.get("phase") == "present", "показ")
    for _ in range(30):
        if a.results:
            break
        await a.send({"t": "next"})
        await asyncio.sleep(0.1)
    await a.wait(lambda: a.results, "итоги")
    albums = a.results["albums"]
    assert len(albums) == 2
    for album in albums:
        assert [s["type"] for s in album["steps"]] == ["text", "draw", "text", "draw"]
        assert album["steps"][0]["authorNick"] != album["steps"][1]["authorNick"], \
            "альбом должен переезжать к соседу"


@scenario("Сэндвич: подпись в начале и в конце")
async def sandwich():
    a, b = await start_room({"mode": "sandwich", "steps": 5, "write_time": 20, "draw_time": 25})
    plan = await play_album(a, b, 5)
    assert [kind for kind, _, _ in plan] == ["text", "draw", "draw", "draw", "text"], plan


@scenario("Плагиат: время на копию убывает")
async def plagiat():
    a, b = await start_room({"mode": "plagiat", "steps": 5, "write_time": 20, "draw_time": 40})
    plan = await play_album(a, b, 5)
    assert [kind for kind, _, _ in plan] == ["text", "draw", "draw", "draw", "draw"], plan
    times = [seconds for kind, _, seconds in plan if kind == "draw"]
    assert times == sorted(times, reverse=True) and times[0] > times[-1], times


@scenario("Анимация: калька, цикл, общий фон")
async def animation():
    a, b = await start_room({"mode": "animation", "steps": 3, "draw_time": 25, "fps": 5})
    for step in range(3):
        await a.in_task(step)
        assert a.game["task"] == "draw"
        assert a.game["sourceMode"] == ("none" if step == 0 else "onion")
        assert bool(a.game["under"]) == (step > 0), "на первом кадре кальки нет, дальше есть"
        for client in (a, b):
            await client.draw(step + 1)
            await client.send({"t": "submit"})
        await asyncio.sleep(0.3)
    await a.wait(lambda: a.game.get("phase") == "present", "показ")
    anim = a.game["animation"]
    assert len(anim["frames"]) == 3 and anim["fps"] == 5, anim

    a, b = await start_room({"mode": "animation", "steps": 3, "draw_time": 25, "background": True})
    for step in range(3):
        await a.in_task(step)
        if step == 0:
            assert "фон" in a.game["hint"], a.game["hint"]
        for client in (a, b):
            await client.draw(step + 1)
            await client.send({"t": "submit"})
        await asyncio.sleep(0.3)
    await a.wait(lambda: a.game.get("phase") == "present", "показ")
    anim = a.game["animation"]
    assert anim["background"] and len(anim["frames"]) == 2, anim


@scenario("Сотрудничество: рисунок копится и чужое не стирается")
async def coop():
    a, b = await start_room({"mode": "coop", "steps": 4, "write_time": 20, "draw_time": 25})
    plan = await play_album(a, b, 4)
    assert [kind for kind, _, _ in plan] == ["text", "draw", "draw", "draw"], plan
    assert [mode for _, mode, _ in plan] == ["none", "text", "carry", "carry"], plan


@scenario("Дополнение: каракули, дорисовка, подпись")
async def complete():
    a, b = await start_room({"mode": "complete", "steps": 4, "write_time": 20, "draw_time": 25})
    plan = await play_album(a, b, 4)
    assert [kind for kind, _, _ in plan] == ["draw", "draw", "text", "draw"], plan
    assert [mode for _, mode, _ in plan] == ["none", "carry", "copy", "text"], plan
    assert plan[0][2] == 15, "на каракули даётся 15 секунд"


@scenario("Изысканный труп: полосы холста и сборка на показе")
async def corpse():
    a, b = await start_room({"mode": "corpse", "steps": 4, "draw_time": 25})
    for step in range(4):
        await a.in_task(step)
        band = a.game["band"]
        assert band["index"] == step and band["count"] == 4, band
        for client in (a, b):
            await client.draw(step + 1, [{
                "t": "p", "c": "#000000", "w": 5, "s": step + 1,
                "b": [band["y0"], band["y1"]],
                "pts": [100, band["y0"] + 5, 400, band["y1"] - 5]}])
            await client.send({"t": "submit"})
        await asyncio.sleep(0.3)
    await a.wait(lambda: a.game.get("phase") == "present", "показ")
    assert len(a.game["step"]["ops"]) == 1, "показ копит полосы сверху вниз"
    for _ in range(30):
        if a.results:
            break
        await a.send({"t": "next"})
        await asyncio.sleep(0.1)
    await a.wait(lambda: a.results, "итоги")
    album = a.results["albums"][0]
    assert album["kind"] == "corpse" and len(album["full"]) == 4
    assert all(op.get("b") for op in album["full"]), "полоса вшита в операцию"


@scenario("Недостающая часть: кусок пропадает, остальное переносится")
async def missing():
    a, b = await start_room({"mode": "missing", "steps": 3, "draw_time": 25})
    carried = []
    for step in range(3):
        await a.in_task(step)
        carried.append(len(a.game.get("canvas", [])))
        for client in (a, b):
            await client.draw(step + 1, [
                {"t": "p", "c": "#000000", "w": 5, "s": step * 10 + i,
                 "pts": [80 + i * 150, 80 + (i % 3) * 250, 120 + i * 150, 120 + (i % 3) * 250]}
                for i in range(6)])
            await client.send({"t": "submit"})
        await asyncio.sleep(0.3)
    assert carried[0] == 0, carried
    assert 0 < carried[1] < 6, "часть первого рисунка должна пропасть: %s" % (carried,)
    assert carried[2] < carried[1] + 6, "каждый ход что-то теряется"


def free_port():
    probe = socket.socket()
    probe.bind(("", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


async def run_all():
    failed = 0
    for title, fn in SCENARIOS:
        try:
            await fn()
            print("  OK   %s" % title)
        except Exception as exc:                        # noqa: BLE001
            failed += 1
            print("  ПАДАЕТ %s\n         %s: %s" % (title, type(exc).__name__, exc))
    return failed


def main():
    global BASE
    port = free_port()
    BASE = "http://127.0.0.1:%d" % port
    server = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "main.py"), "--no-browser", "--port", str(port), "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT,
    )
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit("сервер не поднялся")
        print("Сервер на %s\n" % BASE)
        failed = asyncio.run(run_all())
    finally:
        server.terminate()
        server.wait(timeout=5)
    print("\n%s" % ("Всё прошло" if not failed else "Провалено сценариев: %d" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
