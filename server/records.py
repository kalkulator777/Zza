"""Таблица рекордов. Живёт в data/records.json и переживает перезапуск.

Для заезда на время это вся суть соревнования, а в обычных гонках приятно
видеть, чей круг лучший на трассе.
"""

import json
import os
import tempfile
import time

KEEP = 12


class Records:
    def __init__(self, path):
        self.path = path
        self.data = {}
        self._dirty = False
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        if not self._dirty:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # Пишем через временный файл: обрыв на середине не должен оставить
        # покорёженный JSON, который потом не загрузится.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            if os.path.exists(tmp):
                os.remove(tmp)

    def submit(self, track_id, name, lap_ms, mode="race"):
        """Возвращает место в таблице (1..KEEP) или None."""
        if not lap_ms or lap_ms <= 0:
            return None
        board = self.data.setdefault(track_id, {}).setdefault(mode, [])
        board.append({"name": name, "ms": int(lap_ms), "at": int(time.time())})
        board.sort(key=lambda r: r["ms"])

        # По одной записи на человека — иначе один быстрый игрок забьёт всю доску
        seen = set()
        uniq = []
        for r in board:
            if r["name"] in seen:
                continue
            seen.add(r["name"])
            uniq.append(r)
        del board[:]
        board.extend(uniq[:KEEP])
        self.data[track_id][mode] = board
        self._dirty = True

        for i, r in enumerate(board):
            if r["name"] == name and r["ms"] == int(lap_ms):
                return i + 1
        return None

    def board(self, track_id, mode="race"):
        return self.data.get(track_id, {}).get(mode, [])

    def all_best(self):
        out = {}
        for tid, modes in self.data.items():
            rows = modes.get("timetrial") or modes.get("race") or []
            if rows:
                out[tid] = rows[0]
        return out
