# -*- coding: utf-8 -*-
"""Происшествия на дороге: взрыв, масло, обломки, перекрытие полотна.

Порождает их ТОЛЬКО сервер, клиент получает готовый список в снапшоте
и лишь рисует его да учитывает в предсказании своей машины. Никакой
случайности на стороне клиента нет вовсе — это единственный способ
гарантировать, что все видят одно и то же.

Четыре вида (раздел «Виды» ниже): взрыв с разлётом обломков, разлив
масла, перевёрнутая машина и кратковременное перекрытие части полотна.

Три требования заказчика и как каждое закрыто
---------------------------------------------
**«Событие видно заранее, иначе это не реакция, а лотерея».**
Закрыто двумя независимыми механизмами. Первый: у каждого происшествия
есть фаза предупреждения ``PHASE_WARN`` длиной ``WARN_TIME`` секунд, в
которой на дороге уже стоят мигающие маяки, но физики ещё нет. Второй:
место выбирается так, что в момент появления НИ ОДИН едущий не ближе,
чем ``sight_need(v)`` метров, а это время предупреждения плюс полторы
секунды на реакцию, посчитанные по его собственной скорости. Плюс
проверка обзора: участок перед происшествием обязан быть прямым —
суммарный поворот осевой линии на ``SIGHT_BACK`` метрах до него меньше
``SIGHT_LIMIT``. В слепой шпильке происшествие не появится.

**«Происходит не чаще разумного».**
Настройка комнаты на три значения: выключено, редко, часто. Интервал
между попытками — случайная величина из таблицы ``EVENT_INTERVAL``;
кроме того, два происшествия не встают ближе ``EVENT_SPACING`` метров
друг к другу и больше ``MAX_ROAD_EVENTS`` одновременно не живёт.

**«Не делает трассу непроезжаемой».**
Поперечный габарит каждого происшествия подбирается по фактической
полуширине полотна в точке так, чтобы рядом осталось не меньше
``MIN_FREE_WIDTH`` метров асфальта. Если столько не остаётся —
кандидат отвергается, а не ужимается. Плюс поток болванок
(``game/traffic.py``) объезжает перекрытия и не встаёт в них пробкой.

Где это считается
-----------------
Всё живёт в системе координат трассы: дуга ``s`` и смещение от оси
``lateral``. Так происшествие занимает в снапшоте восемь байт вместо
двух десятков, а проверка попадания сводится к двум сравнениям
в прямоугольнике — без корней и без тригонометрии.

Предфильтр. Держать проверку по всем машинам на каждом тике не нужно:
при создании и при любой смене фаз строится ``_zone`` — байт на каждую
точку осевой линии с битами «здесь масло / обломки / твёрдое / взрыв».
Машина смотрит ``zone[state.sample_idx]``, и пока он нулевой (а он
нулевой почти всегда), не считается вообще ничего.
"""

from __future__ import annotations

import math
import random

from . import physics
from . import protocol

__all__ = [
    'EVENT_LEVELS', 'EVENT_KINDS', 'RoadEvents', 'normalize_level',
    'KIND_EXPLOSION', 'KIND_OIL', 'KIND_WRECK', 'KIND_BLOCKADE',
    'PHASE_WARN', 'PHASE_ACTIVE', 'PHASE_CLEARING', 'PHASE_DEBRIS',
]

MAX_ROAD_EVENTS = protocol.MAX_ROAD_EVENTS

# --- виды --------------------------------------------------------------------

KIND_EXPLOSION = 1   # взрыв на дороге, после него — поле обломков
KIND_OIL = 2         # разлив масла: резко сниженное сцепление
KIND_WRECK = 3       # перевёрнутая машина или упавший груз: объезжать
KIND_BLOCKADE = 4    # кратковременное перекрытие части полотна конусами

EVENT_KINDS = (KIND_EXPLOSION, KIND_OIL, KIND_WRECK, KIND_BLOCKADE)

# Веса выпадения. Взрыв — самое заметное и самое дорогое происшествие,
# поэтому он реже прочих; масло и перекрытие требуют смены траектории,
# а не расплаты, и потому основа набора.
KIND_WEIGHTS = (
    (KIND_EXPLOSION, 2),
    (KIND_OIL, 4),
    (KIND_WRECK, 3),
    (KIND_BLOCKADE, 3),
)

# --- фазы --------------------------------------------------------------------

PHASE_WARN = 0       # маяки стоят, физики нет
PHASE_ACTIVE = 1     # действует
PHASE_CLEARING = 2   # убирается: видно, физики уже нет
PHASE_DEBRIS = 3     # только у взрыва: вспышка прошла, лежат обломки

# --- настройка комнаты -------------------------------------------------------

EVENT_LEVELS = ('off', 'rare', 'often')
EVENT_INTERVAL = {
    'off': (0.0, 0.0),
    'rare': (26.0, 40.0),     # с между попытками
    'often': (11.0, 17.0),
}
EVENT_DEFAULT = 'off'

FIRST_DELAY = 8.0            # с после старта: первые метры гонки всегда чистые

# --- времена фаз -------------------------------------------------------------

WARN_TIME = 3.0              # с предупреждения — одинаково у всех видов
CLEAR_TIME = 1.2             # с уборки

ACTIVE_TIME = {
    KIND_EXPLOSION: 0.7,     # сама вспышка; дальше PHASE_DEBRIS
    KIND_OIL: 17.0,
    KIND_WRECK: 21.0,
    KIND_BLOCKADE: 13.0,
}
DEBRIS_TIME = 11.0           # с, сколько лежат обломки после взрыва

# --- размещение --------------------------------------------------------------

# Сколько асфальта обязано остаться рядом. 4,6 м — это две с половиной
# ширины машины (габарит 1,90 м): проехать можно, но прицелиться надо.
# Меньше делать нельзя: на 4 м проезд перестаёт быть проездом.
MIN_FREE_WIDTH = 4.6         # м свободного асфальта рядом с происшествием
# Габарит происшествия масштабируется по ширине полотна: перекрытие на
# семиметровой serpentine не может быть таким же, как на четырнадцатиметровой
# office — иначе на узкой трассе не проходит НИ ОДИН кандидат и происшествий
# не появляется вовсе (замер до правки: одно за две с половиной минуты
# при настройке «часто»).
REF_HALF_WIDTH = 6.0         # м полуширины, на которой габарит берётся целиком
MIN_WIDTH_SCALE = 0.55       # ниже этой доли габарит не ужимается
EVENT_SPACING = 95.0         # м между двумя происшествиями
SIGHT_BACK = 55.0            # м перед происшествием, которые обязаны быть видны
SIGHT_LIMIT = 0.52           # рад суммарного поворота на этом участке
SIGHT_MIN_AHEAD = 75.0       # м — нижняя граница «видно заранее» на любой скорости
SIGHT_REACTION = 1.5         # с на реакцию сверх времени предупреждения
BACK_CLEAR = 25.0            # м позади гонщика, где тоже не появляемся
TRAFFIC_CLEAR = 34.0         # м перед болванкой: ей хватает, она объезжает
TRAFFIC_CLEAR_BACK = 18.0    # м позади болванки
PLACE_TRIES = 28             # попыток найти место за один заход

# --- физика происшествий -----------------------------------------------------

OIL_GRIP = 0.45              # во столько раз масло режет сцепление
DEBRIS_GRIP = 0.72           # обломки: сцепление хуже, но не каток
DEBRIS_DAMP = 0.9960         # на шаг: тряска по обломкам съедает ход
                             # (для сравнения, газон в physics.py — 0.992)
BLAST_RADIUS = 5.2           # м, радиус вспышки
BLAST_SPIN = 1.2             # с раскрутки попавшему во вспышку
SOLID_BOUNCE = 0.30          # доля нормальной скорости после удара о препятствие

CAR_LONG = physics.CAR_AXIS_HALF + physics.CAR_RADIUS   # 2.0 м, полудлина машины
CAR_SIDE = physics.CAR_RADIUS                           # 0.95 м, полуширина

# --- габариты по видам -------------------------------------------------------
# (полудлина вдоль трассы, полуширина поперёк, доля полуширины полотна,
#  на которую уводится центр)

GEOMETRY = {
    KIND_EXPLOSION: (5.0, 3.4, 0.34),
    KIND_OIL: (7.0, 2.6, 0.40),
    KIND_WRECK: (2.4, 1.35, 0.46),
    KIND_BLOCKADE: (1.7, 2.9, 0.58),
}

# --- биты предфильтра --------------------------------------------------------

ZONE_OIL = 1
ZONE_DEBRIS = 2
ZONE_SOLID = 4
ZONE_BLAST = 8

_TWO_PI = math.pi * 2.0


def normalize_level(value):
    """Привести значение настройки к одному из EVENT_LEVELS."""
    if isinstance(value, str) and value in EVENT_LEVELS:
        return value
    return EVENT_DEFAULT


class RoadEvent(object):
    """Одно происшествие. Живёт от появления маяков до конца уборки."""

    __slots__ = ('id', 'kind', 'phase', 'arc', 'lateral',
                 'half_len', 'half_width', 'track_half_width',
                 'timer', 'hit_mask', 'alive')

    def __init__(self, event_id):
        self.id = event_id
        self.kind = 0
        self.phase = PHASE_WARN
        self.arc = 0.0
        self.lateral = 0.0
        self.half_len = 0.0
        self.half_width = 0.0
        self.track_half_width = 0.0
        self.timer = 0.0
        self.hit_mask = 0        # кого уже задела вспышка: биты по токенам
        self.alive = False

    def __repr__(self):
        return '<RoadEvent %d вид %d фаза %d дуга %.0f>' % (
            self.id, self.kind, self.phase, self.arc)


class RoadEvents(object):
    """Все происшествия одной гонки.

    Интерфейс для ``game/sim.py``::

        events = RoadEvents(track, settings, rng)
        events.update(dt, sim)                  # каждый тик, до шага машин
        scale = events.grip_scale(state)        # ДО physics.step
        events.apply_after_step(state, dt)      # ПОСЛЕ physics.step
        spin = events.blast_spin(state, token)  # только сервер
        events.fill_snapshot(out)               # 20 Гц
    """

    def __init__(self, track, settings=None, rng=None):
        self.track = track
        settings = settings or {}
        self.level = normalize_level(settings.get('events'))
        self.rng = rng if rng is not None else random.Random(
            (getattr(track, 'decor_seed', 0) or 0) ^ 0x5D0AD)

        samples = track.samples
        count = len(samples)
        self._n = count
        self._sx = [s.x for s in samples]
        self._sz = [s.z for s in samples]
        self._stx = [s.tangent_x for s in samples]
        self._stz = [s.tangent_z for s in samples]
        self._snx = [s.normal_x for s in samples]
        self._snz = [s.normal_z for s in samples]
        self._shw = [s.half_width for s in samples]
        self._ss = [s.s for s in samples]
        self._syaw = [math.atan2(s.tangent_x, s.tangent_z) for s in samples]
        self.length = track.length
        self._step = track.length / count if count else 1.0
        self._inv_step = 1.0 / self._step if self._step else 0.0
        self._half_length = track.length * 0.5

        self.pool = [RoadEvent(i) for i in range(MAX_ROAD_EVENTS)]
        self.live = []                    # ссылки на живые, переиспользуется
        self._zone = bytearray(count)
        self._zone_dirty = False
        self.enabled = self.level != 'off' and count > 0

        low, high = EVENT_INTERVAL.get(self.level, (0.0, 0.0))
        self._interval = (low, high)
        self._cooldown = FIRST_DELAY + (self.rng.uniform(low, high) * 0.4
                                        if high > 0.0 else 0.0)
        self.spawned = 0                  # счётчик для тестов и отчёта
        self.last_margin = 0.0            # запас «видно заранее» последнего, м

        self._car_scratch = []
        self._weight_total = sum(w for _kind, w in KIND_WEIGHTS)
        # Выход solid_resolve: x, z, vx, vz, множитель затухания. Список
        # переиспользуется — за тик его заполняют до восьми раз, и мусорить
        # кортежем на горячем пути незачем.
        self._solid = [0.0, 0.0, 0.0, 0.0, 1.0]

    # --- жизненный цикл ------------------------------------------------------

    def update(self, dt, sim):
        """Фазы живых происшествий и попытка породить новое."""
        if not self.enabled:
            return
        live = self.live
        changed = False
        write = 0
        for event in live:
            event.timer -= dt
            if event.timer > 0.0:
                live[write] = event
                write += 1
                continue
            changed = True
            phase = event.phase
            if phase == PHASE_WARN:
                event.phase = PHASE_ACTIVE
                event.timer = ACTIVE_TIME.get(event.kind, 10.0)
            elif phase == PHASE_ACTIVE and event.kind == KIND_EXPLOSION:
                event.phase = PHASE_DEBRIS
                event.timer = DEBRIS_TIME
            elif phase == PHASE_ACTIVE or phase == PHASE_DEBRIS:
                event.phase = PHASE_CLEARING
                event.timer = CLEAR_TIME
            else:
                event.alive = False
                continue
            live[write] = event
            write += 1
        if write != len(live):
            del live[write:]
        if changed:
            self._zone_dirty = True

        self._cooldown -= dt
        if self._cooldown > 0.0:
            if self._zone_dirty:
                self._rebuild_zone()
            return
        low, high = self._interval
        self._cooldown = self.rng.uniform(low, high) if high > 0.0 else 1e9
        if len(live) < MAX_ROAD_EVENTS:
            self._try_spawn(sim)
        if self._zone_dirty:
            self._rebuild_zone()

    def _try_spawn(self, sim):
        """Найти честное место и зажечь маяки. Не нашли — молча ждём дальше."""
        rng = self.rng
        kind = self._roll_kind()
        half_len, half_width, lane_k = GEOMETRY[kind]
        cars = self._collect_cars(sim)
        length = self.length
        for _ in range(PLACE_TRIES):
            arc = rng.random() * length
            index = int(arc * self._inv_step) % self._n
            track_hw = self._shw[index]
            scale = track_hw / REF_HALF_WIDTH
            if scale > 1.0:
                scale = 1.0
            elif scale < MIN_WIDTH_SCALE:
                scale = MIN_WIDTH_SCALE
            width = half_width * scale
            side = 1.0 if rng.random() < 0.5 else -1.0
            lateral = self._fit_lateral(track_hw, width, lane_k, side)
            if lateral is None:
                continue
            if not self._sight_ok(index):
                continue
            if not self._spacing_ok(arc, half_len):
                continue
            margin = self._cars_ok(arc, half_len, cars)
            if margin is None:
                continue
            slot = self._take()
            if slot is None:
                return
            slot.kind = kind
            slot.phase = PHASE_WARN
            slot.timer = WARN_TIME
            slot.arc = arc
            slot.lateral = lateral
            slot.half_len = half_len
            slot.half_width = width
            slot.track_half_width = track_hw
            slot.hit_mask = 0
            slot.alive = True
            self.live.append(slot)
            self.spawned += 1
            self.last_margin = margin
            self._zone_dirty = True
            return

    def _roll_kind(self):
        """Вид происшествия по таблице весов."""
        roll = self.rng.random() * self._weight_total
        for kind, weight in KIND_WEIGHTS:
            roll -= weight
            if roll <= 0.0:
                return kind
        return KIND_WEIGHTS[-1][0]

    def _take(self):
        """Свободная запись пула или None."""
        for event in self.pool:
            if not event.alive:
                return event
        return None

    # --- правила честности ---------------------------------------------------

    def _fit_lateral(self, track_hw, half_width, lane_k, side):
        """Смещение центра так, чтобы рядом остался проезд. None — не влезает."""
        want = track_hw * lane_k * side
        limit = (track_hw + 0.5 - half_width)
        if want > limit:
            want = limit
        elif want < -limit:
            want = -limit
        # Свободная полоса лежит с противоположной стороны от центра.
        if want >= 0.0:
            free = track_hw + want - half_width
        else:
            free = track_hw - want - half_width
        if free < MIN_FREE_WIDTH:
            return None
        return want

    def _sight_ok(self, index):
        """Участок перед происшествием обязан просматриваться."""
        n = self._n
        back = int(SIGHT_BACK * self._inv_step)
        if back < 1:
            back = 1
        start = (index - back) % n
        turn = abs(_wrap_angle(self._syaw[index] - self._syaw[start]))
        # Плюс проверка середины: S-образный участок даёт нулевую разницу
        # на концах, а видно там ничего не будет.
        mid = (index - (back >> 1)) % n
        turn_a = abs(_wrap_angle(self._syaw[mid] - self._syaw[start]))
        turn_b = abs(_wrap_angle(self._syaw[index] - self._syaw[mid]))
        return turn <= SIGHT_LIMIT and (turn_a + turn_b) <= SIGHT_LIMIT * 1.35

    def _spacing_ok(self, arc, half_len):
        """Два происшествия не встают вплотную друг к другу."""
        length = self.length
        for event in self.live:
            gap = abs(_wrap_delta(arc - event.arc, length))
            if gap < EVENT_SPACING + half_len + event.half_len:
                return False
        return True

    def _cars_ok(self, arc, half_len, cars):
        """Никому не под нос. Возвращает запас в метрах или None.

        ``cars`` — кортежи ``(дуга, скорость, гонщик_ли)``.

        У ГОНЩИКА требование полное: происшествие обязано быть впереди него
        не ближе, чем он проедет за время предупреждения плюс полторы
        секунды на реакцию — и не ближе ``SIGHT_MIN_AHEAD`` в любом случае.

        У БОЛВАНКИ требование мягче (``TRAFFIC_CLEAR``), и это не поблажка,
        а необходимость: при плотном траффике на коротком круге девять
        болванок с полным требованием запирают всю трассу, и происшествий
        не появляется вовсе (замер на serpentine: одно за две с половиной
        минуты вместо десятка). Болванке хватает и мягкого правила — она
        не ловит реакцию игрока, а объезжает перекрытие по данным системы
        происшествий, ещё пока горят маяки.
        """
        length = self.length
        worst = 1e9
        for car_arc, speed, is_racer in cars:
            if is_racer:
                need = speed * (WARN_TIME + SIGHT_REACTION)
                if need < SIGHT_MIN_AHEAD:
                    need = SIGHT_MIN_AHEAD
                back = BACK_CLEAR
            else:
                need = TRAFFIC_CLEAR
                back = TRAFFIC_CLEAR_BACK
            need += half_len
            gap = arc - car_arc
            if gap < 0.0:
                gap += length
            if gap < need:
                return None
            if gap > length - back - half_len:
                return None
            if is_racer:
                spare = gap - need
                if spare < worst:
                    worst = spare
        return 0.0 if worst >= 1e9 else worst

    def _collect_cars(self, sim):
        """(дуга, скорость, гонщик_ли) всех участников движения."""
        out = self._car_scratch
        if out:
            del out[:]
        length = self.length
        for race_car in getattr(sim, 'cars', ()):
            if race_car.removed:
                continue
            state = race_car.state
            arc = state.progress % length
            speed = math.sqrt(state.vx * state.vx + state.vz * state.vz)
            out.append((arc, speed, True))
        traffic = getattr(sim, 'traffic', None)
        if traffic is not None:
            traffic.positions_into(out)
        return out

    # --- предфильтр ----------------------------------------------------------

    def _rebuild_zone(self):
        """Пересобрать байт-на-выборку с битами действующих происшествий."""
        zone = self._zone
        n = self._n
        for i in range(n):
            zone[i] = 0
        inv = self._inv_step
        for event in self.live:
            bit = _zone_bit(event)
            if not bit:
                continue
            reach = event.half_len + CAR_LONG + self._step
            first = int((event.arc - reach) * inv)
            last = int((event.arc + reach) * inv) + 1
            for j in range(first, last + 1):
                zone[j % n] |= bit
        self._zone_dirty = False

    # --- влияние на физику ---------------------------------------------------

    def grip_scale(self, state):
        """Множитель сцепления в точке машины. 1.0 — обычный асфальт.

        Зовётся ДО ``physics.step``: масло и обломки меняют не шаг, а
        характеристики, которые в шаг подаются. Так ни ``game/physics.py``,
        ни ``static/js/physics.js`` трогать не нужно, а порядок операций
        раздела 6.2 остаётся дословно прежним.
        """
        if not self.live:
            return 1.0
        flags = self._zone[state.sample_idx]
        if not (flags & (ZONE_OIL | ZONE_DEBRIS)):
            return 1.0
        arc, lateral = self._pose(state)
        scale = 1.0
        for event in self.live:
            phase = event.phase
            kind = event.kind
            if kind == KIND_OIL:
                if phase != PHASE_ACTIVE:
                    continue
                factor = OIL_GRIP
            elif kind == KIND_EXPLOSION and phase == PHASE_DEBRIS:
                factor = DEBRIS_GRIP
            else:
                continue
            if self._inside(event, arc, lateral, 0.0, 0.0):
                if factor < scale:
                    scale = factor
        return scale

    def apply_after_step(self, state, dt):
        """Обломки и твёрдые препятствия. Зовётся ПОСЛЕ ``physics.step``.

        Тонкая обёртка над ``solid_resolve``: всю механику считает она,
        здесь только присваивание. Так у классики и у Rapier одно
        вычисление на двоих, а не две копии, которые разъедутся.
        """
        out = self._solid
        if not self.solid_resolve(state, out):
            return
        state.x = out[0]
        state.z = out[1]
        damp = out[4]
        if damp < 1.0:
            state.vx = out[2] * damp
            state.vz = out[3] * damp
        else:
            state.vx = out[2]
            state.vz = out[3]

    def solid_resolve(self, state, out):
        """Механика обломков и препятствий БЕЗ правки состояния машины.

        Пишет в ``out`` пять чисел — итоговые ``x``, ``z``, ``vx``, ``vz`` и
        множитель затухания — и возвращает True, если считать было что.

        Твёрдое препятствие ведёт себя как стена трассы (шаг 14): машина
        выталкивается по той оси, по которой перекрытие меньше, а
        нормальная составляющая скорости гасится с ``SOLID_BOUNCE``.
        Считается в координатах трассы, поэтому и на сервере, и на клиенте
        выходит одно и то же число.

        Почему итоговые значения, а не приращения. Классике нужно первое
        (она просто присваивает), Rapier — второе: состояние машины лежит
        в модуле, и хозяин выписывает ДОЗУ ``shift_*``/``push_*`` (§12.27).
        Разность считает тот, кому она нужна; общий код остаётся один, и
        у классики он побитово тот же, что был до разделения.
        """
        if not self.live:
            return False
        flags = self._zone[state.sample_idx]
        if not (flags & (ZONE_DEBRIS | ZONE_SOLID)):
            return False
        arc, lateral = self._pose(state)
        damp = 1.0
        x = state.x
        z = state.z
        vx = state.vx
        vz = state.vz
        for event in self.live:
            kind = event.kind
            phase = event.phase
            if kind == KIND_EXPLOSION and phase == PHASE_DEBRIS:
                if self._inside(event, arc, lateral, 0.0, 0.0):
                    damp *= DEBRIS_DAMP
                continue
            if phase != PHASE_ACTIVE:
                continue
            if kind != KIND_WRECK and kind != KIND_BLOCKADE:
                continue
            pen_s = (event.half_len + CAR_LONG) - abs(
                _wrap_delta(arc - event.arc, self.length))
            if pen_s <= 0.0:
                continue
            d_lat = lateral - event.lateral
            pen_l = (event.half_width + CAR_SIDE) - abs(d_lat)
            if pen_l <= 0.0:
                continue
            i = state.sample_idx
            if pen_l <= pen_s:
                # выталкивание поперёк трассы, по нормали осевой линии
                sign = 1.0 if d_lat >= 0.0 else -1.0
                nx = self._snx[i] * sign
                nz = self._snz[i] * sign
                x += nx * pen_l
                z += nz * pen_l
                lateral += sign * pen_l
            else:
                # выталкивание вдоль трассы, по касательной
                sign = 1.0 if _wrap_delta(arc - event.arc, self.length) >= 0.0 else -1.0
                nx = self._stx[i] * sign
                nz = self._stz[i] * sign
                x += nx * pen_s
                z += nz * pen_s
                arc += sign * pen_s
            vn = vx * nx + vz * nz
            if vn < 0.0:
                k = vn * (1.0 + SOLID_BOUNCE)
                vx -= nx * k
                vz -= nz * k
        out[0] = x
        out[1] = z
        out[2] = vx
        out[3] = vz
        out[4] = damp
        return True

    def blast_spin(self, state, token):
        """Вспышка взрыва: секунды раскрутки или 0. Считает только сервер.

        ``token`` — 0..7 для гонщика по слоту, 8.. для болванки: каждому
        взрыву каждая машина достаётся ровно один раз, иначе раскрутка
        обновлялась бы каждый тик и машина крутилась бы вечно.
        """
        if not self.live:
            return 0.0
        if not (self._zone[state.sample_idx] & ZONE_BLAST):
            return 0.0
        bit = 1 << token if token < 30 else 0
        arc, lateral = self._pose(state)
        for event in self.live:
            if event.kind != KIND_EXPLOSION or event.phase != PHASE_ACTIVE:
                continue
            if bit and (event.hit_mask & bit):
                continue
            ds = _wrap_delta(arc - event.arc, self.length)
            dl = lateral - event.lateral
            if ds * ds + dl * dl > BLAST_RADIUS * BLAST_RADIUS:
                continue
            event.hit_mask |= bit
            return BLAST_SPIN
        return 0.0

    def blocked_span(self, arc, scan):
        """Ближайшее перекрытие впереди: (low, high, полуширина полотна).

        Нужно потоку болванок (``game/traffic.py``), чтобы объехать, а не
        упереться. Возвращает None, если на ``scan`` метрах чисто.
        """
        if not self.live:
            return None
        best = None
        best_gap = scan
        length = self.length
        for event in self.live:
            if event.phase != PHASE_ACTIVE and event.phase != PHASE_WARN:
                continue
            if event.kind != KIND_WRECK and event.kind != KIND_BLOCKADE:
                continue
            gap = event.arc - arc
            if gap < 0.0:
                gap += length
            gap -= event.half_len
            if gap < -event.half_len * 2.0 or gap > best_gap:
                continue
            best_gap = gap
            best = event
        if best is None:
            return None
        return (best.lateral - best.half_width,
                best.lateral + best.half_width,
                best.track_half_width)

    # --- положение на трассе -------------------------------------------------

    def _pose(self, state):
        """(дуга, смещение от оси) машины по её же ``sample_idx``."""
        i = state.sample_idx
        dx = state.x - self._sx[i]
        dz = state.z - self._sz[i]
        arc = self._ss[i] + dx * self._stx[i] + dz * self._stz[i]
        length = self.length
        if arc >= length:
            arc -= length
        elif arc < 0.0:
            arc += length
        return arc, dx * self._snx[i] + dz * self._snz[i]

    def _inside(self, event, arc, lateral, pad_s, pad_l):
        """Точка внутри прямоугольника происшествия (в координатах трассы)."""
        if abs(_wrap_delta(arc - event.arc, self.length)) > event.half_len + pad_s:
            return False
        return abs(lateral - event.lateral) <= event.half_width + pad_l

    # --- снапшот -------------------------------------------------------------

    def fill_snapshot(self, out):
        """Дописать происшествия кортежами (id, kind, phase, s_q, lat_q, hl, hw)."""
        if not self.live:
            return
        length = self.length
        arc_q = protocol.quantize_arc
        lat_q = protocol.quantize_lateral
        hl_q = protocol.quantize_half_length
        hw_q = protocol.quantize_half_width
        for event in self.live:
            out.append((event.id, event.kind, event.phase,
                        arc_q(event.arc, length),
                        lat_q(event.lateral),
                        hl_q(event.half_len),
                        hw_q(event.half_width)))

    # --- для тестов и инструментов -------------------------------------------

    def free_width(self, event):
        """Сколько метров асфальта осталось рядом с происшествием."""
        if event.lateral >= 0.0:
            return event.track_half_width + event.lateral - event.half_width
        return event.track_half_width - event.lateral - event.half_width

    def __repr__(self):
        return '<RoadEvents %s, живых %d, всего %d>' % (
            self.level, len(self.live), self.spawned)


# --- вспомогательное ---------------------------------------------------------

def _zone_bit(event):
    """Какой бит предфильтра ставит происшествие в своей фазе."""
    phase = event.phase
    kind = event.kind
    if phase == PHASE_WARN or phase == PHASE_CLEARING:
        return 0
    if kind == KIND_OIL:
        return ZONE_OIL
    if kind == KIND_EXPLOSION:
        return ZONE_BLAST if phase == PHASE_ACTIVE else ZONE_DEBRIS
    return ZONE_SOLID


def _wrap_angle(a):
    """Угол в (-pi, pi]."""
    while a > math.pi:
        a -= _TWO_PI
    while a <= -math.pi:
        a += _TWO_PI
    return a


def _wrap_delta(d, length):
    """Кратчайшая разница двух дуг замкнутого круга."""
    half = length * 0.5
    while d > half:
        d -= length
    while d < -half:
        d += length
    return d
