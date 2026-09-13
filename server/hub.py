"""Хаб: сессии, разбор сообщений, общий тикер.

Один процесс, один цикл событий, всё состояние в памяти. Никаких воркеров:
состояние заезда нельзя размазать по процессам, а нагрузки тут на проценты
одного ядра — шесть машин и простая физика.
"""

import json
import logging
import secrets
import time

from tornado.ioloop import PeriodicCallback

from server.lobby import LobbyManager, clean_name, MAX_PLAYERS
from server.powerups import load_powerups
from server.race import Race

log = logging.getLogger("zza.hub")

MAX_MSG = 4096
MAX_CHAT = 160
SESSION_TTL = 180.0
# Сколько ждать вернувшегося игрока, прежде чем убрать его из лобби.
# F5 посреди лобби не должен выкидывать человека из комнаты.
LOBBY_GRACE = 25.0


class Session:
    __slots__ = ("token", "name", "ws", "lobby", "last_seen", "lead")

    def __init__(self, token, name):
        self.token = token
        self.name = name
        self.ws = None
        self.lobby = None
        self.last_seen = time.time()
        self.lead = 3


class Hub:
    def __init__(self, tracks, phys, powerups_path, records, hz=60, snapshot_hz=30):
        self.tracks = tracks
        self.phys = phys
        self.records = records
        self.powerups_path = powerups_path
        with open(powerups_path, encoding="utf-8") as f:
            self.powerup_info = [
                {"id": it["id"], "name": it.get("name", it["id"]),
                 "short": it.get("short", it["id"][:3].upper()),
                 "color": it.get("color", "#ffd23f"), "kind": it.get("kind", "self")}
                for it in json.load(f)["items"]
            ]
        self.hz = hz
        self.dt = 1.0 / hz
        self.snap_every = max(1, round(hz / snapshot_hz))
        self.sessions = {}
        self.lm = LobbyManager(tracks)
        self._acc = 0.0
        self._last = time.monotonic()
        self._pc = None
        self.stats = {"ticks": 0, "busy": 0.0, "peak_ms": 0.0}

    # ------------------------------------------------------------------ жизнь

    def start(self):
        self._last = time.monotonic()
        self._pc = PeriodicCallback(self.pump, 1000.0 / self.hz)
        self._pc.start()
        self._housekeep = PeriodicCallback(self.housekeep, 5000)
        self._housekeep.start()

    def stop(self):
        for pc in (self._pc, getattr(self, "_housekeep", None)):
            if pc:
                pc.stop()
        self.records.save()

    # --------------------------------------------------------------- отправка

    @staticmethod
    def send(ws, obj):
        if ws is None:
            return
        try:
            ws.write_message(obj if isinstance(obj, str) else json.dumps(obj))
        except Exception:  # noqa: BLE001 — сокет мог закрыться между тиками
            pass

    def to_lobby(self, lb, obj):
        raw = obj if isinstance(obj, str) else json.dumps(obj)
        for tok in list(lb.order):
            s = self.sessions.get(tok)
            if s and s.ws:
                self.send(s.ws, raw)

    def push_lobby(self, lb):
        self.to_lobby(lb, {"t": "lobby", "lobby": lb.full()})
        self.push_list()

    def push_list(self):
        msg = json.dumps({"t": "lobbies", "list": self.lm.listing()})
        for s in self.sessions.values():
            if s.ws and s.lobby is None:
                self.send(s.ws, msg)

    def err(self, ws, code, text):
        self.send(ws, {"t": "err", "code": code, "msg": text})

    # -------------------------------------------------------------- соединение

    def on_open(self, ws):
        pass

    def on_close(self, ws):
        s = getattr(ws, "session", None)
        if s is None:
            return
        s.ws = None
        s.last_seen = time.time()
        lb = s.lobby
        if lb is None:
            return
        # Место сохраняем и в заезде, и в лобби: человек мог просто нажать F5.
        # Если не вернётся — уберёт housekeep через LOBBY_GRACE секунд.
        if lb.race and s.token in lb.race.by_token:
            car = lb.race.by_token[s.token]
            car.connected = False
            car.gone_since = time.time()
            car.inp = 0
        p = lb.players.get(s.token)
        if p:
            p.online = False
        self.to_lobby(lb, {"t": "peer", "token": s.token, "online": False})
        self.push_lobby(lb)

    # ------------------------------------------------------------------ приём

    def on_message(self, ws, raw):
        if len(raw) > MAX_MSG:
            return self.err(ws, "big", "слишком длинное сообщение")
        try:
            msg = json.loads(raw)
            kind = msg.get("t")
        except (ValueError, AttributeError):
            return self.err(ws, "bad", "не разобрал сообщение")
        if not isinstance(msg, dict) or not isinstance(kind, str):
            return self.err(ws, "bad", "не разобрал сообщение")

        s = getattr(ws, "session", None)
        if kind == "hello":
            return self._hello(ws, msg)
        if s is None:
            return self.err(ws, "nohello", "сначала представьтесь")
        s.last_seen = time.time()

        fn = getattr(self, "_m_" + kind, None)
        if fn is None:
            return self.err(ws, "unknown", f"неизвестное сообщение: {kind}")
        try:
            fn(s, msg)
        except Exception:  # noqa: BLE001 — кривое сообщение не должно ронять сервер
            log.exception("ошибка в обработчике %s", kind)
            self.err(ws, "oops", "сервер не смог обработать сообщение")

    # ------------------------------------------------------------- приветствие

    def _hello(self, ws, msg):
        token = msg.get("token")
        name = clean_name(msg.get("name"))
        s = self.sessions.get(token) if isinstance(token, str) else None

        if s is None:
            token = secrets.token_urlsafe(12)
            s = Session(token, name)
            self.sessions[token] = s
        else:
            s.name = name
            if s.ws is not None and s.ws is not ws:
                self.send(s.ws, {"t": "err", "code": "taken",
                                 "msg": "подключение открыто в другой вкладке"})
                try:
                    s.ws.close()
                except Exception:  # noqa: BLE001
                    pass
        s.ws = ws
        s.last_seen = time.time()
        ws.session = s

        self.send(ws, {
            "t": "hello_ok",
            "token": s.token,
            "name": s.name,
            "hz": self.hz,
            "snapshotHz": self.hz / self.snap_every,
            "tracks": [{"id": t.id, "name": t.name, "author": t.author,
                        "laps": t.laps, "length": round(t.length),
                        "difficulty": t.difficulty, "theme": t.theme}
                       for t in self.tracks.values()],
            "physics": self.phys,
            "records": self.records.all_best(),
            "powerups": self.powerup_info,
            "maxPlayers": MAX_PLAYERS,
        })

        # Вернулся в свою комнату — отдадим её состояние
        lb = s.lobby
        if lb is not None and lb.id in self.lm.lobbies:
            p = lb.players.get(s.token)
            if p:
                p.online = True
            self.to_lobby(lb, {"t": "peer", "token": s.token, "online": True})
            self.send(ws, {"t": "lobby", "lobby": lb.full()})
            if lb.state == "racing" and lb.race:
                self._send_race_init(s, lb)
                car = lb.race.by_token.get(s.token)
                if car:
                    car.connected = True
                    car.gone_since = None
                self.to_lobby(lb, {"t": "peer", "token": s.token, "online": True})
            elif lb.state == "results" and lb.results:
                self.send(ws, {"t": "results", **lb.results})
        else:
            s.lobby = None
            self.send(ws, {"t": "lobbies", "list": self.lm.listing()})

    # ------------------------------------------------------------- сообщения

    def _m_ping(self, s, msg):
        self.send(s.ws, {"t": "pong", "c": msg.get("c"), "s": int(time.time() * 1000)})

    def _m_lobby_list(self, s, msg):
        self.send(s.ws, {"t": "lobbies", "list": self.lm.listing()})

    def _m_lobby_create(self, s, msg):
        if s.lobby is not None:
            self._leave(s)
        lb = self.lm.create(s.token, msg.get("name") or f"Заезд {s.name}",
                            msg.get("settings"), msg.get("password"))
        lb.add(s.token, s.name)
        s.lobby = lb
        self.push_lobby(lb)

    def _m_lobby_join(self, s, msg):
        lid = str(msg.get("id", "")).strip().upper()
        lb = self.lm.lobbies.get(lid)
        if lb is None:
            return self.err(s.ws, "nolobby", "лобби не найдено")
        if lb.state != "waiting":
            return self.err(s.ws, "inrace", "в этом лобби идёт заезд")
        if lb.is_full():
            return self.err(s.ws, "full", "в лобби нет мест")
        if lb.password and str(msg.get("password", "")) != lb.password:
            return self.err(s.ws, "pass", "неверный пароль")
        if s.lobby is not None and s.lobby is not lb:
            self._leave(s)
        lb.add(s.token, s.name)
        s.lobby = lb
        self.push_lobby(lb)

    def _m_lobby_leave(self, s, msg):
        self._leave(s)
        self.send(s.ws, {"t": "lobbies", "list": self.lm.listing()})

    def _m_lobby_settings(self, s, msg):
        lb = s.lobby
        if lb is None or lb.host != s.token:
            return self.err(s.ws, "nothost", "менять настройки может только хозяин лобби")
        if lb.state != "waiting":
            return self.err(s.ws, "inrace", "заезд уже идёт")
        keep_mode = lb.settings.get("mode", "race")
        lb.settings = self.lm.sanitize({**lb.settings, **(msg.get("settings") or {})})
        lb.settings["mode"] = keep_mode
        if len(lb.order) > lb.max_players:
            for tok in lb.order[lb.max_players:]:
                other = self.sessions.get(tok)
                if other:
                    self._leave(other)
        self.push_lobby(lb)

    def _m_ready(self, s, msg):
        lb = s.lobby
        if lb is None:
            return
        p = lb.players.get(s.token)
        if p:
            p.ready = bool(msg.get("v"))
        self.push_lobby(lb)

    def _m_chat(self, s, msg):
        lb = s.lobby
        if lb is None:
            return
        text = str(msg.get("text", "")).strip()[:MAX_CHAT]
        if text:
            lb.say(s.name, text)
            self.to_lobby(lb, {"t": "chat", "name": s.name, "text": text})

    def _m_kick(self, s, msg):
        lb = s.lobby
        if lb is None or lb.host != s.token or lb.state != "waiting":
            return
        tok = msg.get("token")
        other = self.sessions.get(tok)
        if other and other.lobby is lb and tok != s.token:
            self.send(other.ws, {"t": "err", "code": "kicked", "msg": "вас исключили из лобби"})
            self._leave(other)

    def _m_start(self, s, msg):
        lb = s.lobby
        if lb is None or lb.host != s.token:
            return self.err(s.ws, "nothost", "начать заезд может только хозяин лобби")
        if lb.state == "racing":
            return
        if len(lb.order) < 1:
            return self.err(s.ws, "empty", "в лобби никого нет")
        self._begin_race(lb)

    def _m_rematch(self, s, msg):
        lb = s.lobby
        if lb is None or lb.host != s.token or lb.state == "racing":
            return
        for p in lb.roster():
            p.ready = False
        lb.state = "waiting"
        lb.results = None
        lb.race = None
        self.push_lobby(lb)

    def _m_solo(self, s, msg):
        """Заезд на время: без лобби, сразу на трассу."""
        if s.lobby is not None:
            self._leave(s)
        settings = self.lm.sanitize(msg.get("settings"))
        settings["mode"] = "timetrial"
        settings["powerups"] = False
        settings["collisions"] = False
        settings["max"] = 1
        lb = self.lm.create(s.token, f"На время — {s.name}", settings)
        lb.hidden = True
        # sanitize поднимает max до минимума для сетевой игры — здесь он не нужен
        lb.settings["mode"] = "timetrial"
        lb.settings["max"] = 1
        lb.add(s.token, s.name)
        s.lobby = lb
        self._begin_race(lb)

    def _m_input(self, s, msg):
        lb = s.lobby
        if lb is None or lb.race is None or lb.state != "racing":
            return
        lb.race.set_input(s.token, int(msg.get("q", 0)), int(msg.get("k", 0)),
                          int(msg.get("m", 0)) & 63)

    def _m_respawn(self, s, msg):
        lb = s.lobby
        if lb and lb.race and lb.state == "racing":
            lb.race.request_respawn(s.token)

    def _m_leave_race(self, s, msg):
        lb = s.lobby
        if lb and lb.state == "racing":
            self._leave(s)
            self.send(s.ws, {"t": "lobbies", "list": self.lm.listing()})

    def _m_tune(self, s, msg):
        """Живая подстройка физики хозяином лобби — прямо во время заезда.

        Итераций на подбор ощущения машины обычно нужны десятки, а поездка с
        флешкой на другие компьютеры стоит дорого. Проще крутить ползунки на
        месте и увезти готовый physics.json.
        """
        lb = s.lobby
        if lb is None or lb.host != s.token:
            return self.err(s.ws, "nothost", "крутить физику может только хозяин лобби")
        section = msg.get("section")
        key = msg.get("key")
        if section not in ("car", "collision", "respawn", "camera") or section not in self.phys:
            return
        if key not in self.phys[section]:
            return
        try:
            val = float(msg.get("value"))
        except (TypeError, ValueError):
            return
        if val != val or val in (float("inf"), float("-inf")):
            return
        # Изменяем словарь на месте: Race держит на него ссылку, поэтому
        # правка долетает до идущего заезда без пересоздания.
        self.phys[section][key] = val
        self.to_lobby(lb, {"t": "tuned", "section": section, "key": key, "value": val})

    # --------------------------------------------------------------- заезды

    def _leave(self, s, silent=True):
        lb = s.lobby
        s.lobby = None
        if lb is None:
            return
        lb.remove(s.token)
        if lb.race and s.token in lb.race.by_token:
            car = lb.race.by_token[s.token]
            car.connected = False
            car.inp = 0
        if not lb.order:
            self.lm.drop(lb.id)
            self.push_list()
        else:
            self.push_lobby(lb)

    def _begin_race(self, lb):
        track = self.tracks[lb.settings["track"]]
        players = [{"token": p.token, "name": p.name, "color": p.color}
                   for p in lb.roster()]
        pw = load_powerups(self.powerups_path)
        lb.race = Race(track, lb.settings, players, pw, self.phys, hz=self.hz)
        lb.state = "racing"
        lb.results = None
        for tok in lb.order:
            s = self.sessions.get(tok)
            if s:
                self._send_race_init(s, lb)
        self.push_list()

    def _send_race_init(self, s, lb):
        race = lb.race
        self.send(s.ws, {
            "t": "race_init",
            "track": race.track.client_payload(),
            "settings": lb.settings,
            "laps": race.laps,
            "hz": self.hz,
            "startTick": race.start_tick,
            "tick": race.tick,
            "you": next((c.slot for c in race.cars if c.token == s.token), -1),
            "cars": [{"slot": c.slot, "name": c.name, "color": c.color,
                      "token": c.token} for c in race.cars],
            "boxes": [{"x": round(b["x"], 1), "y": round(b["y"], 1)} for b in race.boxes],
        })

    def _end_race(self, lb):
        race = lb.race
        rows = race.results()
        boards = {}
        if race.mode == "timetrial":
            for c in race.cars:
                if c.best_lap:
                    self.records.submit(race.track.id, c.name, c.best_lap, "timetrial")
            boards["timetrial"] = self.records.board(race.track.id, "timetrial")
        else:
            for c in race.cars:
                if c.best_lap and c.lap >= 1:
                    self.records.submit(race.track.id, c.name, c.best_lap, "race")
            boards["race"] = self.records.board(race.track.id, "race")
        self.records.save()

        lb.results = {"rows": rows, "track": race.track.id,
                      "trackName": race.track.name, "mode": race.mode,
                      "boards": boards}
        lb.state = "results"
        for p in lb.roster():
            p.ready = False
        self.to_lobby(lb, {"t": "results", **lb.results})
        self.push_list()

    # ------------------------------------------------------------------ тикер

    def pump(self):
        """Фиксированный шаг с накопителем: если цикл событий подвис, время
        не теряется, но и не отыгрывается бесконечным догоном."""
        now = time.monotonic()
        el = now - self._last
        self._last = now
        if el > 0.25:
            el = 0.25
        self._acc += el

        steps = 0
        t0 = time.perf_counter()
        while self._acc >= self.dt and steps < 6:
            self._acc -= self.dt
            self._sim_step()
            steps += 1
        if steps:
            busy = (time.perf_counter() - t0) * 1000.0
            self.stats["ticks"] += steps
            self.stats["busy"] += busy
            if busy > self.stats["peak_ms"]:
                self.stats["peak_ms"] = busy

    def _sim_step(self):
        for lb in list(self.lm.lobbies.values()):
            if lb.state != "racing" or lb.race is None:
                continue
            race = lb.race
            race.step()
            if race.tick % self.snap_every == 0 or race.events:
                # Снапшот сериализуется один раз на всё лобби, а не на клиента
                self.to_lobby(lb, json.dumps(race.snapshot()))
            if race.state == "done":
                self._end_race(lb)

    def housekeep(self):
        now = time.time()
        # Не вернувшихся убираем из комнаты, но саму сессию держим дольше:
        # человек может зайти снова и оказаться в меню, а не в никуда.
        for s in list(self.sessions.values()):
            if s.ws is None and s.lobby is not None and now - s.last_seen > LOBBY_GRACE:
                self._leave(s)
        dead = [tok for tok, s in self.sessions.items()
                if s.ws is None and now - s.last_seen > SESSION_TTL]
        for tok in dead:
            s = self.sessions.pop(tok)
            if s.lobby is not None:
                self._leave(s)
        if self.lm.sweep():
            self.push_list()
        self.records.save()
