# -*- coding: utf-8 -*-
"""Симуляция одной гонки: машины, снаряды, боксы, круги, места.

Реализует интерфейс раздела 12.4 контракта — ровно его, ни полем больше::

    sim = Simulation(track, settings, players)
    sim.set_input(slot, seq, buttons)
    events = sim.tick()                       # 60 Гц
    cars, projectiles, box_mask = sim.snapshot_args()   # 20 Гц
    sim.ack_seq(slot)
    sim.is_over()
    sim.results()
    sim.drop_player(slot)

Порядок работы тика зафиксирован контрактом (разделы 6.2 и 12.4):

1. шаг физики каждой машине — ``physics.step``, он же двигает progress;
2. столкновения машина-машина одним проходом, если включены в комнате;
3. бонусы: снаряды и боксы (``ItemSystem.update``), затем применение бонусов
   по кнопке, затем подбор (``ItemSystem.check_pickups``);
4. круги, финиш и места.

Производительность
------------------
Бюджет комнаты — 2 мс на тик (раздел 1), физика и сервер уже съедают около
0,09 мс. В горячем пути нет ни одной аллокации: состояния, характеристики,
буферы столкновений и таблица мест выделены при создании гонки и правятся
на месте. Список событий один на всю гонку — он очищается в начале тика и
возвращается как есть; сервер рассылает его синхронно, до следующего тика
(``Room._tick_once``), поэтому переиспользование безопасно. Словари событий
одноразовы по своей природе, но событий на тике обычно ноль.

Места считаются вставкой в почти отсортированный массив: за тик машины
меняются местами редко, поэтому средняя стоимость линейна, а не n log n,
и сортировка не аллоцирует ключей.
"""

from __future__ import annotations

import os

from . import physics
from . import protocol
from .items import ItemSystem, ITEM_ID_BY_CODE

__all__ = ['Simulation', 'RaceCar', 'GHOST_TIME', 'set_car_catalog',
           'car_catalog']

DT = physics.DT                  # фиксированный шаг 1/60 (раздел 5.1)
GHOST_TIME = 3.0                 # с, машина-призрак после финиша или обрыва (§9)

# Ключи сортировки мест. Финишировавшие всегда выше едущих (раздел 7.4),
# сошедшие — всегда ниже. Числа заведомо шире любого мыслимого progress
# (девять кругов самой длинной трассы — меньше 15 км).
_FINISHED_BASE = 1.0e12
_DNF_BASE = -1.0e9

_FLAG_OFFTRACK = protocol.FLAG_OFFTRACK
_FLAG_DRIFTING = protocol.FLAG_DRIFTING
_FLAG_BOOST = protocol.FLAG_BOOST
_FLAG_SPIN = protocol.FLAG_SPIN
_FLAG_SHIELD = protocol.FLAG_SHIELD
_FLAG_FINISHED = protocol.FLAG_FINISHED
_FLAG_GHOST = protocol.FLAG_GHOST
_FLAG_BRAKING = protocol.FLAG_BRAKING
_BTN_BRAKE = protocol.BTN_BRAKE
_BTN_ITEM = protocol.BTN_ITEM
_quantize_steer = protocol.quantize_steer
_quantize_drift_charge = protocol.quantize_drift_charge

# --- каталог машин -----------------------------------------------------------
# Сервер отдаёт симуляции только ``car_id`` (раздел 12.4), а характеристики
# лежат в content/cars.json. Каталог читается один раз на процесс и кэшируется:
# в гонке к диску не обращается никто.
#
# Дыра в контракте: ``Simulation(track, settings, players)`` не получает ни
# каталога машин, ни пути к контенту, поэтому файл ищется рядом с пакетом.
# Если сервер запущен с ``--content`` на другой каталог, характеристики машин
# возьмутся не оттуда. Шов для этого случая — ``set_car_catalog(catalog)``:
# достаточно позвать её один раз при старте процесса.

_CATALOG = None
_CATALOG_TRIED = False
_DEFAULT_STATS = physics.CarStats()

CONTENT_CARS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'content', 'cars.json')


def set_car_catalog(catalog):
    """Подставить готовый ``CarCatalog`` (сервер, тесты) вместо чтения файла."""
    global _CATALOG, _CATALOG_TRIED
    _CATALOG = catalog
    _CATALOG_TRIED = True


def car_catalog():
    """Каталог машин или None, если content/cars.json недоступен."""
    global _CATALOG, _CATALOG_TRIED
    if _CATALOG_TRIED:
        return _CATALOG
    _CATALOG_TRIED = True
    try:
        from .cars import CarCatalog
        _CATALOG = CarCatalog.load(CONTENT_CARS)
    except Exception:
        # Гонку из-за битого каталога ронять нельзя: поедут одинаковые
        # машины со стартовыми характеристиками из physics.CarStats.
        _CATALOG = None
    return _CATALOG


class RaceCar(object):
    """Один участник гонки: состояние физики плюс всё гоночное вокруг него."""

    __slots__ = (
        'slot', 'name', 'car_id', 'color',
        'state', 'stats',
        'buttons', 'last_seq', 'seen_input', 'item_request',
        'item',                  # код бонуса в руках, 0 — пусто
        'place', 'rank_index', 'rank_key',
        'laps', 'best_lap', 'lap_start',
        'finished', 'finish_order', 'finish_time',
        'dnf', 'ghost', 'ghost_time', 'removed',
    )

    def __init__(self, slot, name, car_id, color, stats, grid):
        self.slot = slot
        self.name = name
        self.car_id = car_id
        self.color = color
        self.stats = stats
        self.state = physics.CarState(grid['x'], grid['z'], grid['yaw'])
        self.buttons = 0
        self.last_seq = 0
        self.seen_input = False
        self.item_request = False
        self.item = 0
        self.place = slot + 1
        self.rank_index = slot
        self.rank_key = 0.0
        self.laps = 0
        self.best_lap = 0.0
        self.lap_start = 0.0
        self.finished = False
        self.finish_order = 0
        self.finish_time = 0.0
        self.dnf = False
        self.ghost = False
        self.ghost_time = 0.0
        self.removed = False

    def __repr__(self):
        return '<RaceCar slot=%d %r %s место %d круг %d>' % (
            self.slot, self.name, self.car_id, self.place, self.laps)


class Simulation(object):
    """Одна гонка в одной комнате. Живёт от старта до итогов."""

    def __init__(self, track, settings, players):
        self.track = track
        self.settings = dict(settings) if settings else {}
        self.tick_no = 0
        self.race_time = 0.0

        laps = int(self.settings.get('laps', 3))
        self.laps_total = laps if laps > 0 else 1
        self.collisions = bool(self.settings.get('collisions', True))

        # Стартовая расстановка: список игроков сортируется по слоту, индекс
        # в списке равен индексу места на решётке (раздел 12.6).
        rows = sorted(players, key=lambda item: int(item['slot']))
        catalog = car_catalog()
        grid = track.start_grid
        default_grid = {'x': 0.0, 'z': 0.0, 'yaw': 0.0}

        self.cars = []
        self.car_by_slot = {}
        for index, info in enumerate(rows):
            slot = int(info['slot'])
            car_id = info.get('car_id')
            stats = _DEFAULT_STATS
            if catalog is not None:
                spec = catalog.get(car_id)
                if spec is None:
                    spec = catalog.get(catalog.default_id)
                    car_id = spec.id if spec is not None else car_id
                if spec is not None:
                    stats = spec
            place = grid[index] if index < len(grid) else default_grid
            car = RaceCar(slot, info.get('name', ''), car_id,
                          info.get('color'), stats, place)
            track.init_state(car.state)
            self.cars.append(car)
            self.car_by_slot[slot] = car

        count = len(self.cars)
        self.racer_count = count
        self._count = count
        self._finish_count = 0

        # --- преаллоцированные буферы горячего пути ------------------------
        self.rank = list(range(count))          # индексы cars по местам
        self.events = []                        # события тика, переиспользуется
        self._coll_states = [None] * count      # аргументы resolve_collisions
        self._coll_stats = [None] * count
        self._snap_cars = []                    # кортежи машин для снапшота
        self._snap_proj = []                    # кортежи снарядов
        self._empty_mask = b''

        self.items = ItemSystem(track, self.settings)
        self._update_places()

    # --- приём ввода ---------------------------------------------------------

    def set_input(self, slot, seq, buttons):
        """Принять ввод от игрока (раздел 12.4).

        Пакеты приходят не по порядку и дублируются: устаревший ``seq``
        отбрасывается молча. Нажатие бонуса запоминается защёлкой, иначе
        короткое нажатие между двумя тиками потерялось бы: тик видит только
        последний принятый пакет.
        """
        car = self.car_by_slot.get(slot)
        if car is None:
            return
        if car.seen_input and seq <= car.last_seq:
            return
        car.seen_input = True
        car.last_seq = seq
        previous = car.buttons
        car.buttons = buttons
        if (buttons & _BTN_ITEM) and not (previous & _BTN_ITEM):
            car.item_request = True

    def ack_seq(self, slot):
        """Последний учтённый seq этого игрока — для ``protocol.stamp_ack``."""
        car = self.car_by_slot.get(slot)
        return car.last_seq if car is not None else 0

    # --- тик -----------------------------------------------------------------

    def tick(self):
        """Один шаг 1/60 с. Возвращает список готовых событий race_event."""
        events = self.events
        if events:
            del events[:]
        self.tick_no += 1
        dt = DT
        self.race_time += dt

        self._step_cars(dt, events)
        if self.collisions:
            self._resolve_collisions()
        items = self.items
        items.update(self, dt)
        self._use_items(items)
        items.check_pickups(self)
        self._progress_and_laps(events)
        self._update_places()
        return events

    def _step_cars(self, dt, events):
        """Шаг физики каждой машине; призраки доживают и катятся без ввода."""
        track = self.track
        step = physics.step
        for car in self.cars:
            if car.removed:
                continue
            if car.ghost:
                left = car.ghost_time - dt
                if left <= 0.0:
                    car.ghost_time = 0.0
                    car.removed = True
                    continue
                car.ghost_time = left
                buttons = 0
            else:
                buttons = car.buttons
            state = car.state
            level = step(state, car.stats, buttons, dt, track, state.sample_idx)
            if level:
                events.append({'t': 'race_event', 'kind': 'drift_boost',
                               'slot': car.slot, 'level': level})

    def _resolve_collisions(self):
        """Столкновения машина-машина после того, как шаг сделали все (§6.2)."""
        states = self._coll_states
        stats = self._coll_stats
        count = 0
        for car in self.cars:
            if car.removed:
                continue
            states[count] = car.state
            stats[count] = car.stats
            count += 1
        if count > 1:
            physics.resolve_collisions(states, stats, count)

    def _use_items(self, items):
        """Применение бонуса по кнопке: одно нажатие — одно применение."""
        for car in self.cars:
            if not car.item_request:
                continue
            car.item_request = False
            if car.item and not car.ghost and not car.removed:
                items.use(self, car)

    def _progress_and_laps(self, events):
        """Круги, лучший круг и финиш. Прогресс уже посчитан шагом 16 физики."""
        race_time = self.race_time
        laps_total = self.laps_total
        for car in self.cars:
            if car.removed or car.finished or car.dnf:
                # Сошедший доживает призраком и может по инерции пересечь
                # линию: круга и тем более финиша ему за это не полагается.
                continue
            lap = car.state.lap
            if lap <= car.laps:
                if lap < car.laps:
                    # откат через линию задним ходом: круг снимается молча
                    car.laps = lap
                    car.lap_start = race_time
                continue
            car.laps = lap
            lap_time = race_time - car.lap_start
            car.lap_start = race_time
            if car.best_lap <= 0.0 or lap_time < car.best_lap:
                car.best_lap = lap_time
            events.append({'t': 'race_event', 'kind': 'lap',
                           'slot': car.slot, 'lap': lap,
                           'time': round(lap_time, 3),
                           'best': round(car.best_lap, 3)})
            if lap >= laps_total:
                self._finish_count += 1
                car.finished = True
                car.finish_order = self._finish_count
                car.finish_time = race_time
                car.buttons = 0
                car.item = 0
                car.item_request = False
                car.ghost = True
                car.ghost_time = GHOST_TIME
                events.append({'t': 'race_event', 'kind': 'finish',
                               'slot': car.slot, 'place': car.finish_order,
                               'time': round(race_time, 3)})

    def _update_places(self):
        """Места: по progress убыванием, финишировавшие всегда выше (§7.4).

        Массив ``rank`` живёт всю гонку и досортировывается вставкой: за тик
        соседи меняются местами редко, поэтому проход почти всегда линейный
        и ничего не выделяет.
        """
        cars = self.cars
        for car in cars:
            if car.finished:
                car.rank_key = _FINISHED_BASE - car.finish_order
            elif car.dnf:
                car.rank_key = _DNF_BASE + car.state.progress
            else:
                car.rank_key = car.state.progress
        rank = self.rank
        count = self._count
        i = 1
        while i < count:
            moving = rank[i]
            key = cars[moving].rank_key
            j = i - 1
            while j >= 0 and cars[rank[j]].rank_key < key:
                rank[j + 1] = rank[j]
                j -= 1
            rank[j + 1] = moving
            i += 1
        for i in range(count):
            car = cars[rank[i]]
            car.place = i + 1
            car.rank_index = i

    # --- снапшот -------------------------------------------------------------

    def snapshot_args(self):
        """(cars, projectiles, box_mask) в формате раздела 12.3.

        Кортежи, а не словари: именно их ждёт ``protocol.build_snapshot_base``.
        Списки-контейнеры переиспользуются, зовётся это 20 раз в секунду.
        """
        out = self._snap_cars
        if out:
            del out[:]
        for car in self.cars:
            if car.removed:
                continue
            state = car.state
            flags = 0
            if state.offtrack:
                flags |= _FLAG_OFFTRACK
            if state.drift_active:
                flags |= _FLAG_DRIFTING
            if state.boost_time > 0.0:
                flags |= _FLAG_BOOST
            if state.spin_time > 0.0:
                flags |= _FLAG_SPIN
            if state.shield_time > 0.0:
                flags |= _FLAG_SHIELD
            if car.finished:
                flags |= _FLAG_FINISHED
            if car.dnf:
                flags |= _FLAG_GHOST
            if car.buttons & _BTN_BRAKE:
                flags |= _FLAG_BRAKING
            lap = state.lap
            if lap < 0:
                lap = 0
            elif lap > 255:
                lap = 255
            place = car.place
            if place < 1:
                place = 1
            elif place > 255:
                place = 255
            out.append((car.slot, flags, state.x, state.z, state.yaw,
                        state.vx, state.vz, lap, place,
                        _quantize_steer(state.steer),
                        _quantize_drift_charge(state.drift_charge)))
        projectiles = self._snap_proj
        if projectiles:
            del projectiles[:]
        self.items.fill_snapshot(projectiles)
        if self.items.box_count:
            mask = self.items.box_mask()
        else:
            mask = self._empty_mask
        return out, projectiles, mask

    # --- окончание гонки ------------------------------------------------------

    def is_over(self):
        """Гонка закончилась: каждый либо финишировал, либо сошёл."""
        for car in self.cars:
            if not car.finished and not car.dnf:
                return False
        return True

    def results(self):
        """rows для события results (раздел 9), уже в порядке мест."""
        rows = []
        race_time = self.race_time
        cars = self.cars
        rank = self.rank
        for i in range(self._count):
            car = cars[rank[i]]
            rows.append({
                'slot': car.slot,
                'name': car.name,
                'car': car.car_id,
                'color': car.color,
                'place': i + 1,
                'time': round(car.finish_time if car.finished else race_time, 3),
                'best_lap': round(car.best_lap, 3) if car.best_lap > 0.0 else None,
                'dnf': not car.finished,
            })
        return rows

    def drop_player(self, slot):
        """Игрок отключился: машина становится призраком и через 3 с исчезает."""
        car = self.car_by_slot.get(slot)
        if car is None or car.removed:
            return
        if car.finished:
            # Уже финишировал: результат остаётся, призрак и так доживает.
            return
        if car.dnf:
            return
        car.dnf = True
        car.ghost = True
        car.ghost_time = GHOST_TIME
        car.buttons = 0
        car.item = 0
        car.item_request = False

    # --- вспомогательное для инструментов -------------------------------------

    def car(self, slot):
        """Машина по слоту или None — нужна тестам и инструментам."""
        return self.car_by_slot.get(slot)

    def item_name(self, code):
        """Строковый id бонуса по коду (раздел 8)."""
        return ITEM_ID_BY_CODE[code] if 0 < code < len(ITEM_ID_BY_CODE) else ''

    def __repr__(self):
        return '<Simulation %s, машин %d, кругов %d, тик %d>' % (
            getattr(self.track, 'id', '?'), self._count,
            self.laps_total, self.tick_no)
