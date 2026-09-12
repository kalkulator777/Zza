"""Угадайка: один рисует загаданное слово, остальные пишут догадки в чат."""

import random
import time

from tornado.ioloop import PeriodicCallback

from app import words
from app.modes.base import Mode

CHOOSE_TIME = 15      # секунд на выбор слова
REVEAL_TIME = 6       # секунд показа ответа между ходами
WORD_CHOICES = 3
MAX_OPS = 4000        # предохранитель от заваливания сервера штрихами
MAX_POINTS_PER_OP = 4000

BASE_POINTS = 50
SPEED_POINTS = 250
PLACE_BONUS = [50, 30, 15]


class GuessMode(Mode):
    key = "guess"
    title = "Угадайка"

    def __init__(self, room):
        super().__init__(room)
        self.phase = "idle"
        self.round_no = 0
        self.queue = []
        self.artist = None
        self.word = ""
        self.choices = []
        self.used = []
        self.ops = []
        self.guessed = {}
        self.revealed = set()
        self.deltas = {}
        self.deadline = 0.0
        self.total = 0
        self.ticker = PeriodicCallback(self._tick, 3000)

    # --- жизненный цикл -----------------------------------------------

    def validate(self):
        if len(self.room.online_players()) < 2:
            return "Для Угадайки нужно хотя бы два игрока"
        return None

    def start(self):
        self.round_no = 0
        self.queue = []
        self.used = []
        self.ticker.start()
        self.next_turn()

    def stop(self):
        super().stop()
        if self.ticker.is_running():
            self.ticker.stop()

    def next_turn(self):
        self.clear_timers()
        online = self.room.online_players()
        if len(online) < 2:
            self.room.system_chat("Игроков не осталось, игра окончена")
            self.room.finish_game()
            return

        artist = None
        while artist is None:
            while self.queue:
                token = self.queue.pop(0)
                candidate = self.room.players.get(token)
                if candidate is not None and candidate.online:
                    artist = candidate
                    break
            if artist is None:
                self.round_no += 1
                settings_rounds = int(self.room.settings.get("rounds", 3))
                if self.round_no > settings_rounds:
                    self.room.finish_game()
                    return
                self.queue = [p.token for p in self.room.online_players()]
                self.room.system_chat("Раунд %d из %d" % (self.round_no, settings_rounds))
        self.begin_choose(artist)

    def begin_choose(self, artist):
        self.artist = artist
        self.phase = "choosing"
        self.word = ""
        self.ops = []
        self.guessed = {}
        self.revealed = set()
        self.deltas = {}
        self.choices = words.pick(
            WORD_CHOICES,
            self.room.settings.get("difficulty", "mixed"),
            self.room.settings.get("custom_words", ""),
            used=self.used,
        )
        self.set_deadline(CHOOSE_TIME)
        self.schedule(CHOOSE_TIME, self.auto_pick)
        self.room.push_game_state(with_canvas=True)

    def auto_pick(self):
        if self.phase == "choosing":
            self.begin_draw(random.choice(self.choices) if self.choices else "дом")

    def begin_draw(self, word):
        self.clear_timers()
        self.word = word
        self.used.append(word)
        self.phase = "drawing"
        self.ops = []
        self.guessed = {}
        self.revealed = set()
        total = int(self.room.settings.get("draw_time", 80))
        self.set_deadline(total)

        hints = int(self.room.settings.get("hints", 2))
        for step in range(1, hints + 1):
            at = total * step / (hints + 1.0)
            self.schedule(at, self.reveal_letter)
        self.schedule(total, lambda: self.end_turn("время вышло"))
        self.room.push_game_state(with_canvas=True)

    def reveal_letter(self):
        if self.phase != "drawing":
            return
        spots = [i for i, ch in enumerate(self.word) if ch.strip() and i not in self.revealed]
        if len(spots) <= 1:
            return
        self.revealed.add(random.choice(spots))
        self.room.push_game_state()

    def end_turn(self, reason):
        if self.phase != "drawing":
            return
        self.clear_timers()
        self.phase = "reveal"

        artist_points = 0
        if self.guessed:
            artist_points = int(sum(self.guessed.values()) / len(self.guessed) * 0.7)
            if self.artist is not None:
                self.artist.score += artist_points
        self.deltas = dict(self.guessed)
        if self.artist is not None:
            self.deltas[self.artist.token] = artist_points

        self.room.system_chat("Слово было: %s (%s)" % (self.word, reason))
        self.set_deadline(REVEAL_TIME)
        self.schedule(REVEAL_TIME, self.next_turn)
        self.room.broadcast_room()
        self.room.push_game_state()

    # --- ввод от игроков ----------------------------------------------

    def handle(self, player, message):
        kind = message.get("t")
        if kind == "pick" and player is self.artist and self.phase == "choosing":
            index = message.get("index")
            if isinstance(index, int) and 0 <= index < len(self.choices):
                self.begin_draw(self.choices[index])
        elif kind == "draw" and player is self.artist and self.phase == "drawing":
            ops = self.sanitize(message.get("ops"))
            if ops:
                self.ops.extend(ops)
                del self.ops[MAX_OPS:]
                self.room.broadcast({"t": "draw", "ops": ops}, exclude={player.token})
        elif kind == "undo" and player is self.artist and self.phase == "drawing":
            if self.ops:
                # штрих приходит кусками с общим id — отменяем его целиком
                stroke = self.ops[-1].get("s")
                if stroke is None:
                    self.ops.pop()
                else:
                    while self.ops and self.ops[-1].get("s") == stroke:
                        self.ops.pop()
            self.room.broadcast({"t": "canvas", "ops": self.ops}, exclude={player.token})
        elif kind == "clear" and player is self.artist and self.phase == "drawing":
            self.ops = []
            self.room.broadcast({"t": "canvas", "ops": []}, exclude={player.token})

    @staticmethod
    def band_of(op):
        """Полоса, в которую заперта операция (режим «Изысканный труп»)."""
        band = op.get("b")
        if isinstance(band, list) and len(band) == 2:
            try:
                return [round(float(band[0]), 1), round(float(band[1]), 1)]
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def sanitize(ops):
        """Пропускаем только то, что умеем рисовать, и режем великанов."""
        result = []
        if not isinstance(ops, list):
            return result
        for op in ops[:200]:
            if not isinstance(op, dict):
                continue
            kind = op.get("t")
            color = str(op.get("c", "#000000"))[:9]
            if kind == "p":
                points = op.get("pts")
                if not isinstance(points, list) or len(points) < 2:
                    continue
                try:
                    points = [round(float(v), 1) for v in points[:MAX_POINTS_PER_OP]]
                except (TypeError, ValueError):
                    continue
                width = op.get("w", 4)
                width = width if isinstance(width, (int, float)) and 1 <= width <= 80 else 4
                clean = {"t": "p", "c": color, "w": width, "pts": points}
                if isinstance(op.get("s"), int):
                    clean["s"] = op["s"]
                band = GuessMode.band_of(op)
                if band:
                    clean["b"] = band
                result.append(clean)
            elif kind == "f":
                try:
                    clean = {"t": "f", "c": color,
                             "x": round(float(op["x"]), 1), "y": round(float(op["y"]), 1)}
                except (KeyError, TypeError, ValueError):
                    continue
                band = GuessMode.band_of(op)
                if band:
                    clean["b"] = band
                result.append(clean)
        return result

    def handle_chat(self, player, text):
        if self.phase != "drawing":
            return False

        guess = words.normalize(text)
        answer = words.normalize(self.word)

        if player is self.artist:
            if answer and answer in guess:
                player.send({"t": "toast", "text": "Словами подсказывать нельзя!"})
                return True
            return False

        if player.token in self.guessed:
            # уже угадал — переписывается только с такими же и с художником
            audience = set(self.guessed) | {self.artist.token if self.artist else None}
            self.room.push_chat({
                "t": "chat", "kind": "secret", "text": text,
                "from": player.nick, "color": player.color,
            }, only=audience)
            return True

        if guess == answer:
            self.on_correct(player)
            return True

        self.room.push_chat({
            "t": "chat", "kind": "chat", "text": text,
            "from": player.nick, "color": player.color,
        })
        if words.is_close(guess, answer):
            player.send({"t": "chat", "kind": "close", "text": "«%s» — почти!" % text})
        return True

    def on_correct(self, player):
        remaining = max(0.0, self.deadline - time.monotonic())
        fraction = remaining / self.total if self.total else 0
        points = BASE_POINTS + int(SPEED_POINTS * fraction)
        place = len(self.guessed)
        if place < len(PLACE_BONUS):
            points += PLACE_BONUS[place]
        self.guessed[player.token] = points
        player.score += points

        self.room.push_chat({
            "t": "chat", "kind": "correct",
            "text": "%s угадал! +%d" % (player.nick, points),
        })
        self.room.broadcast_room()
        self.room.push_game_state()

        others = [p for p in self.room.online_players() if p is not self.artist]
        if others and all(p.token in self.guessed for p in others):
            self.end_turn("угадали все")

    def on_disconnect(self, player):
        if player is self.artist and self.phase in ("choosing", "drawing"):
            self.room.system_chat("%s ушёл, ход пропускаем" % player.nick)
            self.clear_timers()
            self.phase = "reveal"
            self.set_deadline(2)
            self.schedule(2, self.next_turn)
            self.room.push_game_state()

    # --- состояние ----------------------------------------------------

    def set_deadline(self, seconds):
        self.total = seconds
        self.deadline = time.monotonic() + seconds

    def remaining_ms(self):
        return max(0, int((self.deadline - time.monotonic()) * 1000))

    def _tick(self):
        if self.room.mode is not self:
            return
        self.room.broadcast({"t": "tick", "remaining": self.remaining_ms()})

    def mask(self):
        out = []
        for index, char in enumerate(self.word):
            if not char.strip():
                out.append(" ")
            elif index in self.revealed:
                out.append(char)
            else:
                out.append("_")
        return "".join(out)

    def state_for(self, player, with_canvas=False):
        is_artist = player is self.artist
        knows_word = is_artist or player.token in self.guessed or self.phase == "reveal"
        state = {
            "t": "game",
            "mode": self.key,
            "phase": self.phase,
            "round": self.round_no,
            "rounds": int(self.room.settings.get("rounds", 3)),
            "artist": self.artist.token if self.artist else None,
            "artistNick": self.artist.nick if self.artist else "",
            "youAreArtist": is_artist,
            "remaining": self.remaining_ms(),
            "total": self.total,
            "mask": self.mask(),
            "word": self.word if knows_word else "",
            "guessed": list(self.guessed),
            "deltas": self.deltas if self.phase == "reveal" else {},
        }
        if is_artist and self.phase == "choosing":
            state["choices"] = self.choices
        if with_canvas or self.phase != "drawing":
            state["canvas"] = self.ops
        return state
