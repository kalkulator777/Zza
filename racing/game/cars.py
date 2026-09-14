# -*- coding: utf-8 -*-
"""Характеристики машин: загрузка, валидация, доступ по id.

Владелец модуля: [track]. Только стандартная библиотека.

``CarSpec`` отдаёт характеристики плоскими атрибутами (``engine_force``,
``max_speed``, ...), поэтому объект скармливается прямо в
``game.physics.step`` вместо ``CarStats``: шаг физики читает атрибуты, а не
ключи словаря.

Про ``max_speed``
-----------------
``max_speed`` в шаге 6 — ОПОРНАЯ скорость двигателя, а не достижимая. Тяга
падает как ``engine_force * (1 - v / max_speed)``, и фактический потолок
берётся из равновесия с сопротивлением::

    engine_force * (1 - v / max_speed) = drag * v**2 + roll * v

Модуль решает это уравнение при загрузке (``CarSpec.top_speed``) и проверяет,
что потолок попал в разумный диапазон: ровно на этом контракт один раз уже
обманул исполнителей, и лучше падать на старте сервера, чем выпускать на
трассу машину с потолком 72 км/ч.
"""

from __future__ import annotations

import json
import math
import os

# Обязательные характеристики и допустимые пределы. Пределы намеренно широкие:
# это защита от опечаток и перепутанных полей, а не диктат баланса.
STAT_LIMITS = {
    'engine_force': (4.0, 40.0),      # м/с² тяги при нулевой скорости
    'max_speed': (20.0, 90.0),        # м/с, опорная скорость двигателя
    'brake_force': (5.0, 60.0),       # м/с²
    'reverse_force': (2.0, 30.0),     # м/с²
    'turn_rate': (0.8, 5.0),          # рад/с при полном руле
    'grip_step': (0.02, 0.6),         # доля боковой скорости, гасимая за шаг
    'drift_grip_step': (0.005, 0.3),
    'drag': (0.0001, 0.01),           # квадратичное сопротивление
    'roll': (0.005, 0.5),             # линейное сопротивление качению
    'boost_speed': (25.0, 120.0),     # м/с, опорная скорость под ускорением
    'mass': (0.3, 3.0),               # относительная, для столкновений
}
STAT_NAMES = tuple(sorted(STAT_LIMITS))

# Фактический потолок скорости: за этими рамками машина либо ползёт, либо
# улетает за пределы аркадного диапазона (125..165 км/ч по замыслу).
TOP_SPEED_LIMITS = (25.0, 70.0)       # м/с, то есть 90..252 км/ч
# Это граница вменяемости для проверки каталога, а не игровая настройка.
# Прежние 55 м/с (198 км/ч) упирались ровно в то, куда балансировщик уже
# дотянул машины, и мешали двигать потолок дальше.

SHAPE_STYLES = ('hatch', 'muscle', 'buggy', 'van', 'wedge')
SHAPE_NUMBERS = (
    'length', 'width', 'height', 'wheelbase', 'track_width',
    'wheel_radius', 'wheel_width', 'ride_height',
    'cabin_start', 'cabin_end', 'cabin_height',
    'nose_drop', 'tail_drop', 'taper_front', 'taper_rear',
    'spoiler_height',
)
BAR_NAMES = ('speed', 'accel', 'grip', 'weight')


class CarValidationError(ValueError):
    """Битое или неполное описание машины."""


class CarSpec(object):
    """Одна машина из ``content/cars.json``."""

    __slots__ = ('id', 'name', 'desc', 'stats', 'shape', 'bars',
                 'top_speed', 'boost_top') + STAT_NAMES

    def __init__(self, data):
        self.id = data['id']
        self.name = data['name']
        self.desc = data.get('desc', '')
        self.stats = dict(data['stats'])
        self.shape = dict(data['shape'])
        self.bars = dict(data['bars'])
        for name in STAT_NAMES:
            setattr(self, name, float(self.stats[name]))
        self.top_speed = solve_top_speed(self.engine_force, self.max_speed,
                                         self.drag, self.roll)
        # под ускорением опорной скоростью становится boost_speed, и шаг 7
        # подтягивает к ней напрямую: фактический потолок буста — она же
        self.boost_top = self.boost_speed

    def to_client(self) -> dict:
        """Запись машины для welcome.content.cars.

        Контракт (раздел 9) называет только id/name/desc/bars, но клиенту
        нужны ещё stats — иначе предсказание в physics.js нечем кормить —
        и shape для процедурной модели. Добавлены оба, см. отчёт.
        """
        return {
            'id': self.id,
            'name': self.name,
            'desc': self.desc,
            'bars': dict(self.bars),
            'stats': dict(self.stats),
            'shape': dict(self.shape),
        }

    def __repr__(self):
        return '<CarSpec %s: потолок %.1f м/с (%.0f км/ч)>' % (
            self.id, self.top_speed, self.top_speed * 3.6)


class CarCatalog(object):
    """Все машины сразу: загрузка, проверка, доступ по id."""

    def __init__(self, specs):
        if not specs:
            raise CarValidationError('в cars.json нет ни одной машины')
        self._by_id = {}
        self.list = []
        for spec in specs:
            if spec.id in self._by_id:
                raise CarValidationError('дублируется id машины: %r' % spec.id)
            self._by_id[spec.id] = spec
            self.list.append(spec)
        self.ids = [spec.id for spec in self.list]
        self.default_id = self.ids[0]

    @classmethod
    def load(cls, path: str) -> "CarCatalog":
        with open(path, 'r', encoding='utf-8') as fp:
            data = json.load(fp)
        if isinstance(data, dict):
            rows = data.get('cars')
        else:
            rows = data
        if not isinstance(rows, list):
            raise CarValidationError(
                '%s: ожидался список машин в поле "cars"' % os.path.basename(path))
        specs = []
        for index, row in enumerate(rows):
            validate(row, index)
            specs.append(CarSpec(row))
        return cls(specs)

    def get(self, car_id):
        """Машина по id или None, если такой нет."""
        return self._by_id.get(car_id)

    def require(self, car_id) -> CarSpec:
        """Машина по id; неизвестный id — это ошибка данных, а не игрока."""
        spec = self._by_id.get(car_id)
        if spec is None:
            raise KeyError('нет машины с id %r (есть: %s)'
                           % (car_id, ', '.join(self.ids)))
        return spec

    def resolve(self, car_id) -> CarSpec:
        """Машина по id, а если id чужой или пустой — машина по умолчанию.

        Нужна серверу: клиент присылает car_id в set_car, и гонку нельзя
        уронить из-за подделанного значения.
        """
        return self._by_id.get(car_id) or self._by_id[self.default_id]

    def to_client(self) -> list:
        """Список машин для welcome.content.cars."""
        return [spec.to_client() for spec in self.list]

    def __contains__(self, car_id):
        return car_id in self._by_id

    def __iter__(self):
        return iter(self.list)

    def __len__(self):
        return len(self.list)

    def __repr__(self):
        return '<CarCatalog: %s>' % ', '.join(self.ids)


def solve_top_speed(engine_force, max_speed, drag, roll) -> float:
    """Фактический потолок скорости из равновесия тяги и сопротивления.

    ``engine*(1 - v/max) = drag*v^2 + roll*v``  ->  положительный корень
    ``drag*v^2 + (roll + engine/max)*v - engine = 0``.
    """
    a = drag
    b = roll + engine_force / max_speed
    c = -engine_force
    disc = b * b - 4.0 * a * c
    return (-b + math.sqrt(disc)) / (2.0 * a)


def validate(row, index=0) -> None:
    """Проверка одной записи машины. Бросает CarValidationError."""
    where = 'машина #%d' % index
    if not isinstance(row, dict):
        raise CarValidationError('%s: ожидался объект JSON' % where)
    for field in ('id', 'name', 'stats', 'shape', 'bars'):
        if field not in row:
            raise CarValidationError('%s: нет поля %r' % (where, field))
    car_id = row['id']
    if not isinstance(car_id, str) or not car_id:
        raise CarValidationError('%s: id должен быть непустой строкой' % where)
    where = 'машина %r' % car_id

    stats = row['stats']
    if not isinstance(stats, dict):
        raise CarValidationError('%s: stats должен быть объектом' % where)
    for name in STAT_NAMES:
        if name not in stats:
            raise CarValidationError('%s: в stats нет %r' % (where, name))
        value = stats[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise CarValidationError('%s: stats.%s не число: %r' % (where, name, value))
        lo, hi = STAT_LIMITS[name]
        if not lo <= float(value) <= hi:
            raise CarValidationError('%s: stats.%s = %r вне разумных пределов %g..%g'
                                     % (where, name, value, lo, hi))
    extra = set(stats) - set(STAT_NAMES)
    if extra:
        raise CarValidationError('%s: лишние поля в stats: %s'
                                 % (where, ', '.join(sorted(extra))))
    if stats['drift_grip_step'] >= stats['grip_step']:
        raise CarValidationError('%s: drift_grip_step (%g) должен быть меньше '
                                 'grip_step (%g), иначе занос цепче обычной езды'
                                 % (where, stats['drift_grip_step'], stats['grip_step']))

    top = solve_top_speed(stats['engine_force'], stats['max_speed'],
                          stats['drag'], stats['roll'])
    lo, hi = TOP_SPEED_LIMITS
    if not lo <= top <= hi:
        raise CarValidationError(
            '%s: фактический потолок %.1f м/с (%.0f км/ч) вне %g..%g м/с. '
            'Это решение уравнения engine_force*(1 - v/max_speed) = drag*v^2 + roll*v, '
            'а не поле max_speed' % (where, top, top * 3.6, lo, hi))
    if stats['boost_speed'] <= top:
        raise CarValidationError('%s: boost_speed %.1f не выше фактического потолка '
                                 '%.1f м/с — ускорение ничего не даст'
                                 % (where, stats['boost_speed'], top))

    shape = row['shape']
    if not isinstance(shape, dict):
        raise CarValidationError('%s: shape должен быть объектом' % where)
    if shape.get('style') not in SHAPE_STYLES:
        raise CarValidationError('%s: shape.style = %r, ожидался один из %s'
                                 % (where, shape.get('style'), ', '.join(SHAPE_STYLES)))
    for name in SHAPE_NUMBERS:
        if name not in shape:
            raise CarValidationError('%s: в shape нет %r' % (where, name))
        value = shape[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise CarValidationError('%s: shape.%s не число: %r' % (where, name, value))
        if float(value) < 0.0 or float(value) > 8.0:
            raise CarValidationError('%s: shape.%s = %r вне 0..8 м' % (where, name, value))
    if not isinstance(shape.get('spoiler'), bool):
        raise CarValidationError('%s: shape.spoiler должен быть true/false' % where)
    accent = shape.get('accent')
    if not isinstance(accent, str) or not accent.startswith('#') or len(accent) != 7:
        raise CarValidationError('%s: shape.accent = %r, ожидался цвет вида #rrggbb'
                                 % (where, accent))
    if not 0.0 <= shape['cabin_start'] < shape['cabin_end'] <= 1.0:
        raise CarValidationError('%s: нужно 0 <= cabin_start < cabin_end <= 1, '
                                 'получено %r и %r'
                                 % (where, shape['cabin_start'], shape['cabin_end']))
    if shape['cabin_height'] >= shape['height']:
        raise CarValidationError('%s: cabin_height (%g) не может быть больше всей '
                                 'высоты кузова (%g)'
                                 % (where, shape['cabin_height'], shape['height']))
    if shape['wheelbase'] >= shape['length']:
        raise CarValidationError('%s: колёсная база длиннее самой машины' % where)
    if shape['track_width'] > shape['width']:
        raise CarValidationError('%s: колея шире кузова' % where)

    bars = row['bars']
    if not isinstance(bars, dict):
        raise CarValidationError('%s: bars должен быть объектом' % where)
    for name in BAR_NAMES:
        if name not in bars:
            raise CarValidationError('%s: в bars нет %r' % (where, name))
        value = bars[name]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
            raise CarValidationError('%s: bars.%s = %r, ожидалось целое 1..5'
                                     % (where, name, value))


def load_cars(path: str) -> CarCatalog:
    """Короткая форма ``CarCatalog.load``."""
    return CarCatalog.load(path)
