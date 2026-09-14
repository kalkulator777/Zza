# -*- coding: utf-8 -*-
"""ВРЕМЕННАЯ ЗАГЛУШКА симуляции. Удалить вместе с этим файлом, как только
появится ``game/sim.py``.

Зачем она есть. Сервер и симуляция пишутся параллельно; сервер обязан
запускаться и проверяться до того, как появится настоящая ``Simulation``.
Заглушка реализует ровно интерфейс из §12.4 контракта: честно крутит тик
с шагом 1/60 с, принимает ввод и помнит ``ack_seq`` каждого игрока — этого
достаточно, чтобы проверить темп цикла, рассылку снапшотов и штамповку ack.
Машин, снарядов и боксов у неё нет: снапшоты пустые.

Подставляется только если ``game.sim`` не импортируется (см.
``server.room.resolve_simulation``). Как только настоящий модуль появится,
этот файл не будет использоваться ни при каких условиях.
"""

from . import config


class StubSimulation(object):
    """Пустая гонка: тик идёт, машины не едут, гонка сама не заканчивается."""

    def __init__(self, track, settings, players):
        self.track = track
        self.settings = settings
        self.tick_no = 0
        self._slots = [int(item['slot']) for item in players]
        self._names = {int(item['slot']): item.get('name', '') for item in players}
        self._cars = {int(item['slot']): item.get('car_id') for item in players}
        self._colors = {int(item['slot']): item.get('color') for item in players}
        self._acks = {slot: 0 for slot in self._slots}
        self._dropped = set()
        self._empty_mask = b''

    # --- §12.4 ---------------------------------------------------------------

    def set_input(self, slot, seq, buttons):
        """Принять ввод: устаревший seq игнорируется молча."""
        previous = self._acks.get(slot)
        if previous is None:
            return
        if seq > previous:
            self._acks[slot] = seq

    def tick(self):
        """Один шаг 1/60 с. Событий у пустой гонки не бывает."""
        self.tick_no += 1
        return ()

    def snapshot_args(self):
        """(cars, projectiles, box_mask) — у заглушки всё пусто."""
        return (), (), self._empty_mask

    def ack_seq(self, slot):
        return self._acks.get(slot, 0)

    def is_over(self):
        """Никто не едет — значит и не финиширует. Гонку закроет таймаут сервера."""
        return False

    def results(self):
        """Итоги для события results: все без времени и без круга."""
        rows = []
        place = 0
        for slot in self._slots:
            place += 1
            rows.append({
                'slot': slot,
                'name': self._names.get(slot, ''),
                'car': self._cars.get(slot),
                'color': self._colors.get(slot),
                'place': place,
                'time': 0.0,
                'best_lap': None,
                'dnf': True,
            })
        return rows

    def drop_player(self, slot):
        self._dropped.add(slot)
        self._acks.pop(slot, None)


# Шаг заглушки обязан совпадать с шагом сервера: если константа разъедется,
# разъедется и измеренный темп цикла.
TICK_DT = config.TICK_DT
