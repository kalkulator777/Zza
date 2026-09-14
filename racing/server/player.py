# -*- coding: utf-8 -*-
"""Игрок и его соединение.

Player — это одно WebSocket-соединение плюс всё, что о нём знает комната:
слот, имя, выбранная машина и цвет, готовность, признак наблюдателя,
замер ping и последний учтённый ``ack_seq``.

Отправка наружу идёт только через методы этого класса: они гасят ошибки
закрытого сокета, чтобы один отвалившийся клиент не ронял рассылку на всю
комнату, и не дают Tornado ругаться на «Future exception was never retrieved».
"""

import json
import time

from . import config


def _drop_write_error(future):
    """Проглотить ошибку асинхронной записи в закрытый сокет.

    ``write_message`` возвращает Future; если клиент отвалился между тиками,
    исключение придёт именно в него. Без этого колбэка asyncio напечатает
    «Future exception was never retrieved» на каждый такой кадр.
    """
    if not future.cancelled():
        future.exception()


def sanitize_text(value, limit):
    """Привести пришедшую от клиента строку к безопасному виду.

    Не строка -> пустая строка. Управляющие символы выкидываем (перевод строки
    в том числе: и имя, и чат — однострочные). Длина режется по ``limit``.
    """
    if not isinstance(value, str):
        return ''
    out = []
    for ch in value:
        if ch == '\t':
            out.append(' ')
            continue
        if ch < ' ' or ch == '\x7f':
            continue
        out.append(ch)
        if len(out) >= limit:
            break
    return ''.join(out).strip()


class Player(object):
    """Соединение + состояние игрока в комнате."""

    __slots__ = (
        'conn', 'player_id', 'slot_token', 'name', 'is_host',
        'room', 'slot', 'car', 'color', 'ready', 'spectator',
        'ack_seq', 'ping', 'connected', 'joined_at',
        '_ping_t0', '_ping_sent', '_chat_times',
    )

    def __init__(self, conn, player_id, slot_token):
        self.conn = conn                # WebSocket-хендлер (см. app.GameSocket)
        self.player_id = player_id      # сквозной номер соединения, для логов
        self.slot_token = slot_token    # личный токен из welcome (§9)
        self.name = ''
        self.is_host = False            # предъявил токен хоста при hello
        self.room = None                # Room или None, если игрок в меню
        self.slot = -1                  # место в комнате 0..7, вне комнаты -1
        self.car = None                 # id выбранной машины
        self.color = config.COLORS[0]
        self.ready = False
        self.spectator = False          # вошёл во время гонки — ждёт следующей
        self.ack_seq = 0                # последний учтённый сервером seq (§5.3)
        self.ping = 0                   # сглаженный RTT, мс
        self.connected = True
        self.joined_at = time.monotonic()
        self._ping_t0 = 0.0             # метка последнего отправленного ping
        self._ping_sent = 0.0
        self._chat_times = []           # отметки последних сообщений в чат

    # --- отправка ----------------------------------------------------------

    def send_text(self, payload):
        """Отправить готовую JSON-строку (общая для всей комнаты рассылка)."""
        if not self.connected:
            return
        try:
            future = self.conn.write_message(payload)
        except Exception:
            self.connected = False
            return
        if future is not None:
            future.add_done_callback(_drop_write_error)

    def send_event(self, event):
        """Отправить одно JSON-событие этому игроку."""
        self.send_text(json.dumps(event, ensure_ascii=False, separators=(',', ':')))

    def send_error(self, code, message):
        """Событие error (§9): что-то пошло не так, но соединение живо."""
        self.send_event({'t': 'error', 'code': code, 'message': message})

    def send_binary(self, data):
        """Отправить бинарный кадр (снапшот).

        ``data`` — bytes; bytearray Tornado не принимает, поэтому буфер
        снапшота копируется вызывающей стороной ровно один раз на клиента.
        """
        if not self.connected:
            return
        try:
            future = self.conn.write_message(data, binary=True)
        except Exception:
            self.connected = False
            return
        if future is not None:
            future.add_done_callback(_drop_write_error)

    def close(self, code=1000, reason=''):
        """Закрыть соединение со своей стороны."""
        self.connected = False
        try:
            self.conn.close(code, reason)
        except Exception:
            pass

    # --- ping --------------------------------------------------------------

    def start_ping(self, now):
        """Отправить событие ping с меткой времени (§9)."""
        self._ping_t0 = now
        self._ping_sent = now
        self.send_event({'t': 'ping', 't0': now})

    def note_pong(self, t0, now):
        """Учесть ответ pong. Возвращает True, если замер принят.

        Чужую или выдуманную метку не принимаем: сверяем с последней своей.
        """
        if not isinstance(t0, (int, float)) or isinstance(t0, bool):
            return False
        if self._ping_t0 <= 0.0 or abs(float(t0) - self._ping_t0) > 1e-6:
            return False
        rtt_ms = (now - self._ping_t0) * 1000.0
        if rtt_ms < 0.0:
            rtt_ms = 0.0
        elif rtt_ms > 10000.0:
            rtt_ms = 10000.0
        if self.ping <= 0:
            self.ping = int(rtt_ms + 0.5)
        else:
            smooth = self.ping + (rtt_ms - self.ping) * config.PING_SMOOTHING
            self.ping = int(smooth + 0.5)
        self._ping_t0 = 0.0
        return True

    # --- чат ---------------------------------------------------------------

    def chat_allowed(self, now):
        """Пропускать ли сообщение в чат: короткая очередь — да, флуд — нет."""
        times = self._chat_times
        limit = now - config.CHAT_MIN_INTERVAL * config.CHAT_BURST
        while times and times[0] < limit:
            times.pop(0)
        if len(times) >= config.CHAT_BURST:
            return False
        times.append(now)
        return True

    # --- состояние для событий ---------------------------------------------

    def reset_for_lobby(self):
        """Возврат в лобби после гонки: готовность снимается, зритель играет."""
        self.ready = False
        self.spectator = False
        self.ack_seq = 0

    def lobby_info(self):
        """Элемент players[] в событии room (§9)."""
        return {
            'slot': self.slot,
            'name': self.name,
            'car': self.car,
            'color': self.color,
            'ready': self.ready,
            'spectator': self.spectator,
            'ping': self.ping,
        }

    def race_info(self, grid):
        """Элемент players[] в событии race_init (§9)."""
        return {
            'slot': self.slot,
            'name': self.name,
            'car': self.car,
            'color': self.color,
            'grid': grid,
        }

    def sim_info(self):
        """Элемент players[] для Simulation.__init__ (§12.4)."""
        return {
            'slot': self.slot,
            'name': self.name,
            'car_id': self.car,
            'color': self.color,
        }

    def __repr__(self):
        return '<Player #%d %r slot=%d>' % (self.player_id, self.name, self.slot)
