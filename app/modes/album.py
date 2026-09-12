"""Альбомные режимы: альбом ходит по кругу, каждый добавляет свой шаг.

Все три режима — это один движок с разной раскладкой шагов:
  Обычно   текст, рисунок, текст, рисунок, …
  Сэндвич  текст, рисунок, рисунок, …, текст
  Плагиат  текст, рисунок, а дальше копии предыдущего с убывающим временем
"""

import random
import time

from tornado.ioloop import PeriodicCallback

from app.modes.base import Mode

MAX_OPS = 6000
CANVAS_W = 1000        # логический размер холста, должен совпадать с draw.js
CANVAS_H = 750
PRESENT_TEXT = 4       # секунд на показ подписи
PRESENT_DRAW = 7       # секунд на показ рисунка
EMPTY_TEXT = "(промолчал)"


class Album:
    def __init__(self, owner_token, owner_nick):
        self.owner = owner_token
        self.owner_nick = owner_nick
        self.steps = []

    def public(self):
        return {"owner": self.owner, "ownerNick": self.owner_nick, "steps": self.steps}


class AlbumMode(Mode):
    """Общий движок. Наследники задают тип и длительность каждого шага."""

    def __init__(self, room):
        super().__init__(room)
        self.phase = "idle"
        self.albums = []
        self.roster = []
        self.round_index = -1
        self.steps_total = 0
        self.buffers = {}
        self.submitted = set()
        self.present_album = 0
        self.present_step = 0
        self.deadline = 0.0
        self.total = 0
        self.ticker = PeriodicCallback(self._tick, 3000)

    # --- что именно за шаг (переопределяют наследники) -------------------

    def step_type(self, index):
        return "text" if index % 2 == 0 else "draw"

    def step_seconds(self, index):
        if self.step_type(index) == "text":
            return int(self.room.settings.get("write_time", 45))
        return int(self.room.settings.get("draw_time", 80))

    def source_mode(self, index):
        """Как игрок видит предыдущий шаг:
        none — никак, text — фразой, copy — рисунком рядом,
        onion — бледной калькой под своим холстом, carry — дорисовываешь поверх.
        """
        source = None if index == 0 else True
        if source is None:
            return "none"
        return "text" if self.step_type(index - 1) == "text" else "copy"

    def band_for(self, index):
        """Полоса холста, доступная на этом шаге. None — весь холст."""
        return None

    def carry_ops(self, ops):
        """Что из чужого рисунка достаётся игроку при дорисовке."""
        return ops

    def step_hint(self, index, source):
        """Подпись задачи для игрока."""
        if source is None:
            return "Придумай фразу — её будет рисовать сосед"
        if self.step_type(index) == "draw":
            if source["type"] == "text":
                return "Нарисуй это"
            return "Повтори рисунок как можешь"
        return "Опиши словами, что нарисовано"

    # --- жизненный цикл ---------------------------------------------------

    def validate(self):
        if len(self.room.online_players()) < 2:
            return "Нужно хотя бы два игрока"
        return None

    def allows_late_join(self):
        return True  # зашедший позже смотрит и попадёт в следующую игру

    def start(self):
        players = self.room.online_players()
        self.roster = [p.token for p in players]
        self.albums = [Album(p.token, p.nick) for p in players]
        configured = int(self.room.settings.get("steps", 0))
        self.steps_total = configured if configured >= 2 else max(4, len(self.roster))
        self.steps_total = min(self.steps_total, 12)
        self.round_index = -1
        self.ticker.start()
        self.begin_round()

    def stop(self):
        super().stop()
        if self.ticker.is_running():
            self.ticker.stop()

    def begin_round(self):
        self.clear_timers()
        self.round_index += 1
        if self.round_index >= self.steps_total:
            self.begin_present()
            return
        self.buffers = {}
        self.submitted = set()
        self.phase = "task"
        if self.source_mode(self.round_index) == "carry":
            # дорисовываем поверх чужого: чужие штрихи уже лежат в холсте и не стираются
            for token in self.roster:
                source = self.source_for(token)
                ops = self.carry_ops(list(source.get("ops", [])) if source else [])
                self.buffers[token] = {"ops": ops, "text": "", "locked": len(ops)}
        seconds = self.step_seconds(self.round_index)
        self.set_deadline(seconds)
        self.schedule(seconds, self.end_round)
        self.room.push_game_state(with_canvas=True)

    def album_for(self, token):
        """В каждом раунде альбом переезжает к следующему игроку."""
        if token not in self.roster or not self.albums:
            return None
        index = self.roster.index(token)
        return self.albums[(index - self.round_index) % len(self.albums)]

    def source_for(self, token):
        album = self.album_for(token)
        if album is None or not album.steps:
            return None
        return album.steps[-1]

    def end_round(self):
        if self.phase != "task":
            return
        kind = self.step_type(self.round_index)
        for token in self.roster:
            album = self.album_for(token)
            if album is None:
                continue
            buffer = self.buffers.get(token, {})
            player = self.room.players.get(token)
            step = {
                "type": kind,
                "author": token,
                "authorNick": player.nick if player else "",
            }
            if kind == "text":
                step["text"] = (buffer.get("text") or "").strip() or EMPTY_TEXT
            else:
                step["ops"] = buffer.get("ops") or []
            album.steps.append(step)
        self.begin_round()

    # --- показ ------------------------------------------------------------

    def begin_present(self):
        self.clear_timers()
        self.phase = "present"
        self.present_album = 0
        self.present_step = 0
        self.room.system_chat("Смотрим, что получилось")
        self.show_current()

    def show_current(self):
        album = self.albums[self.present_album]
        step = album.steps[self.present_step] if self.present_step < len(album.steps) else None
        seconds = PRESENT_TEXT if step and step.get("type") == "text" else PRESENT_DRAW
        self.set_deadline(seconds)
        self.clear_timers()
        self.schedule(seconds, self.advance)
        self.room.push_game_state(with_canvas=True)

    def advance(self):
        if self.phase != "present":
            return
        album = self.albums[self.present_album]
        self.present_step += 1
        if self.present_step >= len(album.steps):
            self.present_album += 1
            self.present_step = 0
            if self.present_album >= len(self.albums):
                self.room.finish_game()
                return
        self.show_current()

    def under_ops(self, token):
        """Что подложить бледной калькой под холст игрока."""
        if self.source_mode(self.round_index) != "onion":
            return []
        source = self.source_for(token)
        return list(source.get("ops", [])) if source else []

    def results_payload(self):
        return {"albums": [album.public() for album in self.albums]}

    # --- ввод от игроков --------------------------------------------------

    def buffer_for(self, player):
        return self.buffers.setdefault(player.token, {"ops": [], "text": "", "locked": 0})

    def handle(self, player, message):
        kind = message.get("t")

        if kind == "next" and self.phase == "present" and player.token == self.room.host_token:
            self.advance()
            return
        if self.phase != "task" or player.token not in self.roster:
            return
        if player.token in self.submitted:
            return

        if kind == "draw" and self.step_type(self.round_index) == "draw":
            from app.modes.guess import GuessMode
            ops = GuessMode.sanitize(message.get("ops"))
            buffer = self.buffer_for(player)
            buffer["ops"].extend(ops)
            del buffer["ops"][MAX_OPS:]
        elif kind == "undo":
            buffer = self.buffer_for(player)
            ops, locked = buffer["ops"], buffer.get("locked", 0)
            if len(ops) > locked:
                stroke = ops[-1].get("s")
                if stroke is None:
                    ops.pop()
                else:
                    while len(ops) > locked and ops[-1].get("s") == stroke:
                        ops.pop()
        elif kind == "clear":
            buffer = self.buffer_for(player)
            del buffer["ops"][buffer.get("locked", 0):]
        elif kind == "submit":
            buffer = self.buffer_for(player)
            if self.step_type(self.round_index) == "text":
                buffer["text"] = str(message.get("text", ""))[:140]
            self.submitted.add(player.token)
            self.room.push_game_state()
            waiting = [t for t in self.roster
                       if t not in self.submitted
                       and self.room.players.get(t) is not None
                       and self.room.players[t].online]
            if not waiting:
                self.end_round()

    def handle_chat(self, player, text):
        # пока идёт ход, чат молчит — иначе весь смысл испорченного телефона теряется
        if self.phase == "task":
            player.send({"t": "toast", "text": "Чат откроется на показе"})
            return True
        return False

    # --- состояние --------------------------------------------------------

    def set_deadline(self, seconds):
        self.total = seconds
        self.deadline = time.monotonic() + seconds

    def remaining_ms(self):
        return max(0, int((self.deadline - time.monotonic()) * 1000))

    def _tick(self):
        if self.room.mode is not self:
            return
        self.room.broadcast({"t": "tick", "remaining": self.remaining_ms()})

    def state_for(self, player, with_canvas=False):
        state = {
            "t": "game",
            "mode": self.key,
            "family": "album",
            "phase": self.phase,
            "round": self.round_index + 1,
            "rounds": self.steps_total,
            "remaining": self.remaining_ms(),
            "total": self.total,
            "isHost": player.token == self.room.host_token,
        }

        if self.phase == "task":
            playing = player.token in self.roster
            kind = self.step_type(self.round_index)
            source = self.source_for(player.token) if playing else None
            state.update({
                "playing": playing,
                "task": kind,
                "hint": self.step_hint(self.round_index, source) if playing else "Ты подключился посреди игры — смотри",
                "source": source,
                "sourceMode": self.source_mode(self.round_index) if playing else "none",
                "band": self.band_for(self.round_index) if playing else None,
                "under": self.under_ops(player.token) if playing else [],
                "locked": self.buffers.get(player.token, {}).get("locked", 0),
                "done": player.token in self.submitted,
                "submitted": list(self.submitted),
                "waiting": [self.room.players[t].nick for t in self.roster
                            if t not in self.submitted and t in self.room.players],
            })
            if with_canvas:
                state["canvas"] = self.buffers.get(player.token, {}).get("ops", [])
        elif self.phase == "present":
            album = self.albums[self.present_album]
            state.update({
                "albumIndex": self.present_album,
                "albumsTotal": len(self.albums),
                "ownerNick": album.owner_nick,
                "stepIndex": self.present_step,
                "stepsTotal": len(album.steps),
                "step": album.steps[self.present_step] if self.present_step < len(album.steps) else None,
            })
        return state


class NormalMode(AlbumMode):
    key = "normal"
    title = "Обычно"


class SandwichMode(AlbumMode):
    key = "sandwich"
    title = "Сэндвич"

    def step_type(self, index):
        return "text" if index == 0 or index == self.steps_total - 1 else "draw"

    def source_mode(self, index):
        """Как игрок видит предыдущий шаг:
        none — никак, text — фразой, copy — рисунком рядом,
        onion — бледной калькой под своим холстом, carry — дорисовываешь поверх.
        """
        source = None if index == 0 else True
        if source is None:
            return "none"
        return "text" if self.step_type(index - 1) == "text" else "copy"

    def step_hint(self, index, source):
        if index == self.steps_total - 1:
            return "Последний ход: опиши словами, что видишь"
        return super().step_hint(index, source)


class PlagiatMode(AlbumMode):
    key = "plagiat"
    title = "Плагиат"

    DECAY = 0.72
    MIN_SECONDS = 8

    def step_type(self, index):
        return "text" if index == 0 else "draw"

    def step_seconds(self, index):
        if index == 0:
            return int(self.room.settings.get("write_time", 45))
        base = int(self.room.settings.get("draw_time", 80))
        return max(self.MIN_SECONDS, int(base * (self.DECAY ** (index - 1))))


class AnimationMode(AlbumMode):
    """Каждый рисует свой кадр, видя предыдущий калькой. В конце всё крутится циклом."""

    key = "animation"
    title = "Анимация"

    def background_enabled(self):
        return bool(self.room.settings.get("background"))

    def step_type(self, index):
        return "draw"

    def step_seconds(self, index):
        return int(self.room.settings.get("draw_time", 80))

    def source_mode(self, index):
        if index == 0:
            return "none"
        return "onion"

    def step_hint(self, index, source):
        if self.background_enabled() and index == 0:
            return "Нарисуй фон — он будет стоять за всеми кадрами"
        if index == 0:
            return "Нарисуй первый кадр"
        return "Следующий кадр: предыдущий виден бледным"

    def under_ops(self, token):
        under = []
        if self.background_enabled() and self.albums:
            album = self.album_for(token)
            if album is not None and album.steps and self.round_index > 0:
                under.extend(album.steps[0].get("ops", []))
        if self.round_index > 0:
            source = self.source_for(token)
            if source is not None and not (self.background_enabled() and self.round_index == 1):
                under.extend(source.get("ops", []))
        return under

    def album_payload(self, album):
        steps = album.steps
        background = steps[0].get("ops", []) if (self.background_enabled() and steps) else []
        frames = steps[1:] if (self.background_enabled() and steps) else steps
        return {
            "kind": "animation",
            "background": background,
            "frames": [step.get("ops", []) for step in frames],
            "fps": int(self.room.settings.get("fps", 4)),
        }

    def show_current(self):
        """Альбом показывается целиком как анимация, а не по шагам."""
        album = self.albums[self.present_album]
        self.present_step = max(0, len(album.steps) - 1)
        self.set_deadline(9)
        self.clear_timers()
        self.schedule(9, self.advance)
        self.room.push_game_state(with_canvas=True)

    def advance(self):
        if self.phase != "present":
            return
        self.present_album += 1
        if self.present_album >= len(self.albums):
            self.room.finish_game()
            return
        self.show_current()

    def state_for(self, player, with_canvas=False):
        state = super().state_for(player, with_canvas=with_canvas)
        if self.phase == "present":
            state["animation"] = self.album_payload(self.albums[self.present_album])
        return state

    def results_payload(self):
        return {"albums": [dict(album.public(), **self.album_payload(album)) for album in self.albums]}


class CoopMode(AlbumMode):
    """Один рисунок на всех: каждый добавляет своё поверх предыдущего."""

    key = "coop"
    title = "Сотрудничество"

    def step_type(self, index):
        return "text" if index == 0 else "draw"

    def source_mode(self, index):
        if index == 0:
            return "none"
        if index == 1:
            return "text"
        return "carry"

    def step_hint(self, index, source):
        if index == 0:
            return "Придумай фразу — рисовать её будете все вместе"
        if index == 1:
            return "Начни рисунок по этой фразе"
        return "Добавь своё к тому, что уже нарисовано"


class CompleteMode(AlbumMode):
    """Каракули превращаются в рисунок, рисунок — в подпись, и понеслось."""

    key = "complete"
    title = "Дополнение"

    SCRIBBLE_SECONDS = 15

    def step_type(self, index):
        if index <= 1:
            return "draw"
        return "text" if index % 2 == 0 else "draw"

    def step_seconds(self, index):
        if index == 0:
            return self.SCRIBBLE_SECONDS
        return super().step_seconds(index)

    def source_mode(self, index):
        if index == 0:
            return "none"
        if index == 1:
            return "carry"
        return super().source_mode(index)

    def step_hint(self, index, source):
        if index == 0:
            return "Начерти несколько случайных линий — и всё"
        if index == 1:
            return "Преврати эти линии во что-нибудь осмысленное"
        return super().step_hint(index, source)


def op_center(op):
    """Где примерно находится операция — нужно, чтобы понять, в какой она части."""
    if op.get("t") == "f":
        return op.get("x", 0), op.get("y", 0)
    points = op.get("pts") or []
    if len(points) < 2:
        return 0, 0
    xs = points[0::2]
    ys = points[1::2]
    return sum(xs) / len(xs), sum(ys) / len(ys)


class CorpseMode(AlbumMode):
    """Изысканный труп: каждому своя полоса холста и краешек соседней."""

    key = "corpse"
    title = "Изысканный труп"
    SEAM = 46  # сколько пикселей предыдущей полосы видно на стыке

    def step_type(self, index):
        return "draw"

    def source_mode(self, index):
        return "none" if index == 0 else "onion"

    def band_for(self, index):
        count = max(2, self.steps_total)
        height = CANVAS_H / count
        return {
            "index": index,
            "count": count,
            "y0": round(index * height, 1),
            "y1": round((index + 1) * height, 1),
            "seam": self.SEAM,
        }

    def step_hint(self, index, source):
        if index == 0:
            return "Рисуй только в своей полосе — верх существа"
        if index == self.steps_total - 1:
            return "Последняя полоса: доведи существо до низа"
        return "Продолжи линии соседа — видно только край его полосы"

    def accumulated(self, upto):
        album = self.albums[self.present_album]
        ops = []
        for step in album.steps[:upto + 1]:
            ops.extend(step.get("ops", []))
        return ops

    def state_for(self, player, with_canvas=False):
        state = super().state_for(player, with_canvas=with_canvas)
        # на показе полосы собираются в одно существо сверху вниз
        if self.phase == "present" and state.get("step"):
            state["step"] = dict(state["step"], ops=self.accumulated(self.present_step))
        return state

    def results_payload(self):
        albums = []
        for album in self.albums:
            data = album.public()
            full = []
            for step in album.steps:
                full.extend(step.get("ops", []))
            data.update(kind="corpse", full=full)
            albums.append(data)
        return {"albums": albums}


class MissingMode(AlbumMode):
    """Недостающая часть: кусок предыдущего рисунка пропадает, дорисовывай своё."""

    key = "missing"
    title = "Недостающая часть"
    COLUMNS = 3
    ROWS = 3

    def step_type(self, index):
        return "draw"

    def source_mode(self, index):
        return "none" if index == 0 else "carry"

    def step_hint(self, index, source):
        if index == 0:
            return "Нарисуй что угодно — дальше его начнут портить"
        return "Часть рисунка пропала: дорисуй на её месте своё"

    def cell_of(self, op):
        x, y = op_center(op)
        column = min(self.COLUMNS - 1, max(0, int(x * self.COLUMNS / CANVAS_W)))
        row = min(self.ROWS - 1, max(0, int(y * self.ROWS / CANVAS_H)))
        return row * self.COLUMNS + column

    def carry_ops(self, ops):
        if len(ops) < 2:
            return ops
        cells = {}
        for op in ops:
            cells.setdefault(self.cell_of(op), []).append(op)
        limit = max(1, int(len(ops) * 0.6))
        candidates = [cell for cell, items in cells.items() if len(items) <= limit]
        if not candidates:
            candidates = [min(cells, key=lambda cell: len(cells[cell]))]
        # выкидываем не самый пустой угол, а что-нибудь заметное
        candidates.sort(key=lambda cell: -len(cells[cell]))
        target = random.choice(candidates[:2])
        return [op for op in ops if self.cell_of(op) != target]
