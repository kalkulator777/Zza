"""Общий каркас игрового режима."""

import logging

from tornado.ioloop import IOLoop

log = logging.getLogger("mode")


class Mode:
    key = ""
    title = ""

    def __init__(self, room):
        self.room = room
        self._timers = []

    # --- таймеры ------------------------------------------------------

    def schedule(self, delay, fn):
        handle = IOLoop.current().call_later(delay, self._guard(fn))
        self._timers.append(handle)
        return handle

    def _guard(self, fn):
        def runner():
            if self.room.mode is not self:
                return
            try:
                fn()
            except Exception:
                log.exception("Ошибка в таймере режима %s", self.key)
        return runner

    def clear_timers(self):
        loop = IOLoop.current()
        for handle in self._timers:
            try:
                loop.remove_timeout(handle)
            except Exception:
                pass
        self._timers = []

    # --- интерфейс ----------------------------------------------------

    def validate(self):
        """Вернуть текст ошибки, если запускать нельзя."""
        return None

    def allows_late_join(self):
        return True

    def start(self):
        raise NotImplementedError

    def stop(self):
        self.clear_timers()

    def handle(self, player, message):
        pass

    def handle_chat(self, player, text):
        """True — сообщение обработано режимом и в общий чат не идёт."""
        return False

    def on_disconnect(self, player):
        pass

    def state_for(self, player, with_canvas=False):
        return {"t": "game", "mode": self.key}
