# -*- coding: utf-8 -*-
"""Бонусы, снаряды и боксы: раздел 8 контракта.

Модуль отвечает за всё, что не является физикой машины: таблицу выпадения,
пул снарядов (ракеты и мины), подбор боксов и их респаун. Наружу торчит ровно
то, что названо в разделе 8::

    ITEM_KINDS: dict
    def roll_item(place, total, enabled) -> int
    class Projectile
    class ItemSystem:
        def update(self, sim, dt)
        def use(self, sim, car)
        def check_pickups(self, sim)

Что ``ItemSystem`` ждёт от ``sim`` (утиная типизация, см. ``game/sim.py``)::

    sim.track          Track из game/track.py
    sim.cars           список машин гонки, порядок фиксирован на всю гонку
    sim.rank           индексы cars по местам: rank[0] — лидер
    sim.events         список событий тика, сюда кладутся pickup/use/hit
    sim.race_time      время от старта, с (для событий)

Машина (``sim.cars[i]``) обязана иметь поля ``slot``, ``state`` (CarState),
``item`` (код бонуса в руках, 0 — пусто), ``place``, ``rank_index``,
``finished``, ``ghost``, ``removed``.

Производительность
------------------
Тик бонусов обязан быть практически бесплатным, поэтому:

* ноль аллокаций: пул снарядов, списки боксов и маска выделены один раз
  и переиспользуются, временных списков/кортежей/словарей в ``update`` нет
  (кроме словарей самих событий — они редкие и по своей природе одноразовые);
* ни одного прохода «всё по всему». Боксы и снаряды разложены по корзинам
  вдоль дуги трассы (связные списки на преаллоцированных массивах int),
  машина смотрит только в три соседние корзины — это и есть «использовать то,
  что боксы уже отсортированы по дуге»;
* цель ракете ищется за O(1) по массиву мест ``sim.rank``, а не перебором
  машин: «ближайший впереди по progress» — это ровно сосед сверху в рейтинге;
* позиция снаряда по дуге не пересчитывается через ``Track.nearest_index``,
  а доводится локальным шагом по осевой линии: снаряд за тик пролетает
  меньше метра, точки стоят через два.
"""

from __future__ import annotations

import math
import random

from .protocol import MAX_PROJECTILES

__all__ = [
    'ITEM_BOOST', 'ITEM_ROCKET', 'ITEM_MINE', 'ITEM_SHIELD', 'ITEM_STORM',
    'ITEM_KINDS', 'ITEM_IDS', 'ITEM_CODE_BY_ID',
    'ITEM_WEIGHTS', 'WEIGHT_ROWS',
    'BOOST_TIME', 'ROCKET_SPIN', 'MINE_SPIN', 'SHIELD_TIME', 'STORM_SLOW',
    'BOX_RESPAWN', 'PICKUP_RADIUS',
    'roll_item', 'place_row', 'Projectile', 'ItemSystem',
]

# --- коды бонусов (раздел 8, колонка kind) ----------------------------------

ITEM_BOOST = 1
ITEM_ROCKET = 2
ITEM_MINE = 3
ITEM_SHIELD = 4
ITEM_STORM = 5

ITEM_COUNT = 5

# --- числовые параметры бонусов ---------------------------------------------
# Значения из раздела 8, где контракт их называет. Остальное подобрано
# замерами на ботах (см. tools/test_sim.py) и прокомментировано по месту.

BOOST_TIME = 2.5             # с ускорения от «Турбо» (раздел 8)
SHIELD_TIME = 8.0            # с щита (раздел 8)
STORM_SLOW = 1.2             # с замедления всем впереди (раздел 8)

ROCKET_SPIN = 1.5            # с раскрутки от попадания ракеты (раздел 8)
ROCKET_SPEED = 46.0          # м/с; потолок машин 38..42, догнать обязана
ROCKET_TURN_RATE = 2.4       # рад/с — конечная скорость поворота: ракета
                             # с бесконечной наводкой неуклонима и бесит
ROCKET_LIFE = 5.0            # с жизни, дальность выходит ~230 м
ROCKET_RADIUS = 2.8          # м, радиус поражения
ROCKET_ARM = 0.0             # ракете взведение не нужно: от собственного
                             # бампера её защищает проверка по слоту владельца,
                             # а любая задержка даёт промах в упор — за 0,08 с
                             # ракета пролетает почти четыре метра
ROCKET_SPAWN_AHEAD = 2.4     # м впереди носа, чтобы не родиться внутри машины

MINE_SPIN = 1.0              # с раскрутки от мины: ловушка мягче ракеты
MINE_ARM = 0.5               # с взведения (раздел 8)
MINE_LIFE = 25.0             # с жизни (раздел 8)
MINE_RADIUS = 2.4            # м, радиус срабатывания
MINE_DROP_BACK = 3.4         # м позади центра машины

BOX_RESPAWN = 6.0            # с до возвращения подобранного бокса (раздел 8)
PICKUP_RADIUS = 2.2          # м, радиус подбора бокса

BIN_SIZE = 12.0              # м, шаг корзины вдоль дуги трассы
_BIN_LOOKUP = 1              # сколько корзин смотрим в каждую сторону

# --- описание бонусов -------------------------------------------------------

ITEM_KINDS = {
    ITEM_BOOST: {
        'kind': ITEM_BOOST, 'id': 'boost', 'name': 'Турбо',
        'desc': 'Ускорение на %.1f с' % BOOST_TIME,
        'duration': BOOST_TIME, 'aim': 'self',
    },
    ITEM_ROCKET: {
        'kind': ITEM_ROCKET, 'id': 'rocket', 'name': 'Ракета',
        'desc': 'Самонаводящийся снаряд в ближайшего впереди',
        'duration': ROCKET_LIFE, 'aim': 'ahead',
    },
    ITEM_MINE: {
        'kind': ITEM_MINE, 'id': 'mine', 'name': 'Мина',
        'desc': 'Сбрасывается позади, взводится %.1f с, живёт %.0f с'
                % (MINE_ARM, MINE_LIFE),
        'duration': MINE_LIFE, 'aim': 'behind',
    },
    ITEM_SHIELD: {
        'kind': ITEM_SHIELD, 'id': 'shield', 'name': 'Щит',
        'desc': 'Гасит одно попадание, %.0f с' % SHIELD_TIME,
        'duration': SHIELD_TIME, 'aim': 'self',
    },
    ITEM_STORM: {
        'kind': ITEM_STORM, 'id': 'storm', 'name': 'Гроза',
        'desc': 'Все, кто впереди, замедляются на %.1f с' % STORM_SLOW,
        'duration': STORM_SLOW, 'aim': 'all_ahead',
    },
}

# Канонический порядок из раздела 8 и config.ITEM_IDS сервера.
ITEM_IDS = ('boost', 'rocket', 'mine', 'shield', 'storm')
ITEM_CODE_BY_ID = {'boost': ITEM_BOOST, 'rocket': ITEM_ROCKET,
                   'mine': ITEM_MINE, 'shield': ITEM_SHIELD,
                   'storm': ITEM_STORM}
# Обратное отображение для событий: в JSON едет строковый id (раздел 9).
ITEM_ID_BY_CODE = ('', 'boost', 'rocket', 'mine', 'shield', 'storm')

_ALL_MASK = (1 << ITEM_COUNT) - 1


# --- таблица выпадения ------------------------------------------------------
#
# Восемь строк — по одной на место, сверху лидер, снизу последний. Колонки в
# порядке кодов: boost, rocket, mine, shield, storm. Числа — веса, а не
# проценты: сумма строки нормируется на месте, поэтому отключённый в настройках
# комнаты бонус просто выпадает из розыгрыша, не перекашивая остальные.
#
# Замысел — резина, которая держит гонку интересной:
#
# * Лидеру (строка 0) достаются почти только мина и щит. Это оборона: мина
#   мешает догоняющему, щит гасит прилетевшую ракету. Лидер не беспомощная
#   мишень, но и оторваться ещё сильнее ему нечем — турбо у него слабое,
#   ракеты нет вовсе (бить некого: впереди никого).
# * Середина (строки 2..4) — самый ровный набор: и догонять, и защищаться.
#   Ракета тут на пике: именно из середины интереснее всего стрелять вперёд.
# * Хвост (строки 5..7) живёт на турбо и грозе. Гроза бьёт ВСЕХ впереди,
#   то есть чем дальше отстал, тем больше от неё толку — это главный
#   инструмент возврата в гонку. Мина и щит последнему почти не нужны:
#   позади него никого нет, а стрелять в него будут мало.
#
# Веса выверены сериями по восемь ботов на трёх трассах
# (``tools/test_sim.py --balance``, 24 гонки по три круга):
#
#   смен лидера за гонку      без бонусов 1,96   с бонусами 2,79
#   сдвиг мест от середины
#   дистанции к финишу        без бонусов 0,12   с бонусами 1,57
#   раскруток за гонку        всего 11,8; в лидера 0,67, ещё 0,50 гасит его щит
#                             (то есть лидера сбивают раз в четыре минуты, а не
#                              раз в десять секунд)
#
# То есть бонусы решают (порядок к финишу перетасовывается на полтора места
# против одной десятой без них), но лидера при этом не сносят каждые десять
# секунд: его выручают щит из строки 0 и то, что стрелять в него может
# только идущий вторым.
#
#                         boost rocket mine shield storm
ITEM_WEIGHTS = (
    (  12,     0,   33,    47,     8),   # 1-е место: оборона, 80 % на мину и щит
    (  17,    20,   23,    32,     8),   # 2-е: единственный, кто стреляет в лидера
    (  21,    23,   17,    27,    12),   # 3-е
    (  25,    24,   12,    23,    16),   # 4-е: ровная середина
    (  29,    24,    9,    20,    18),   # 5-е
    (  33,    23,    7,    17,    20),   # 6-е
    (  37,    22,    5,    14,    22),   # 7-е
    (  41,    21,    3,    11,    24),   # 8-е: 86 % на турбо, ракету и грозу
)

WEIGHT_ROWS = len(ITEM_WEIGHTS)

# Гонка в одиночку (тренировка): места «впереди» и «позади» не существует,
# поэтому берём ровную середину таблицы, а не оборонительную строку лидера.
SOLO_ROW = 3

_default_rng = random.Random()


def place_row(place, total):
    """Строка таблицы весов для места ``place`` из ``total`` участников.

    Таблица рассчитана на восьмерых, а ехать может и трое: место
    растягивается на все восемь строк, чтобы у первого всегда была строка
    лидера, а у последнего — строка хвоста.
    """
    if total <= 1:
        return SOLO_ROW
    if place < 1:
        place = 1
    elif place > total:
        place = total
    row = ((place - 1) * WEIGHT_ROWS) // total
    if row >= WEIGHT_ROWS:
        row = WEIGHT_ROWS - 1
    return row


def enabled_mask(enabled):
    """Набор разрешённых бонусов -> битовая маска кодов.

    Принимает и строковые id (``settings['items']`` сервера), и готовые коды,
    и ``None`` как «все разрешены». Считается один раз при создании гонки,
    в тик не попадает.
    """
    if enabled is None:
        return _ALL_MASK
    mask = 0
    for item in enabled:
        if isinstance(item, int):
            code = item
        else:
            code = ITEM_CODE_BY_ID.get(item, 0)
        if 1 <= code <= ITEM_COUNT:
            mask |= 1 << (code - 1)
    return mask


def _roll_masked(row, mask, rng):
    """Розыгрыш по готовой строке и готовой маске разрешённых бонусов."""
    if not mask:
        return 0
    weights = ITEM_WEIGHTS[row]
    total = 0
    for k in range(ITEM_COUNT):
        if mask & (1 << k):
            total += weights[k]
    if total <= 0:
        return 0
    ticket = rng.random() * total
    acc = 0.0
    last = 0
    for k in range(ITEM_COUNT):
        if mask & (1 << k):
            weight = weights[k]
            if weight <= 0:
                continue
            last = k + 1
            acc += weight
            if ticket < acc:
                return k + 1
    # Сюда попадаем только на краю округления: отдаём последний разрешённый.
    return last


def roll_item(place, total, enabled):
    """Что выпало игроку на месте ``place`` из ``total`` (раздел 8).

    ``enabled`` — разрешённые в комнате бонусы: строковые id, коды или
    ``None``. Возвращает код бонуса 1..5 или 0, если выпадать нечему.
    """
    return _roll_masked(place_row(place, total), enabled_mask(enabled),
                        _default_rng)


# --- снаряды ----------------------------------------------------------------

class Projectile(object):
    """Ракета или мина. Один класс на оба вида: ветка по ``kind`` в тике
    дешевле виртуального вызова, а полей у них почти поровну.

    Объекты живут в пуле ``ItemSystem`` и переиспользуются: ``spawn`` заполняет
    поля, ``active`` гасит запись. Ни одного создания объекта в тике.

    Поля::

        id           u16 для снапшота, растёт по кругу
        kind         ITEM_ROCKET или ITEM_MINE
        owner        слот выпустившего
        x, z, yaw    положение и курс
        speed        м/с, у мины ноль
        life         остаток времени жизни, с
        arm          остаток времени взведения, с (пока > 0 — не поражает)
        target       слот цели ракеты или -1
        sample_idx   индекс ближайшей точки осевой линии (для корзин)
        bin_index    корзина, в которой снаряд сейчас лежит, -1 — ни в одной
    """

    __slots__ = ('id', 'kind', 'owner', 'x', 'z', 'yaw', 'speed',
                 'life', 'arm', 'radius', 'target', 'sample_idx',
                 'bin_index', 'active')

    def __init__(self):
        self.id = 0
        self.kind = 0
        self.owner = -1
        self.x = 0.0
        self.z = 0.0
        self.yaw = 0.0
        self.speed = 0.0
        self.life = 0.0
        self.arm = 0.0
        self.radius = 0.0
        self.target = -1
        self.sample_idx = 0
        self.bin_index = -1
        self.active = False

    def spawn(self, proj_id, kind, owner, x, z, yaw, sample_idx):
        """Занять запись пула под новый снаряд."""
        self.id = proj_id
        self.kind = kind
        self.owner = owner
        self.x = x
        self.z = z
        self.yaw = yaw
        self.sample_idx = sample_idx
        self.bin_index = -1
        self.active = True
        self.target = -1
        if kind == ITEM_ROCKET:
            self.speed = ROCKET_SPEED
            self.life = ROCKET_LIFE
            self.arm = ROCKET_ARM
            self.radius = ROCKET_RADIUS
        else:
            self.speed = 0.0
            self.life = MINE_LIFE
            self.arm = MINE_ARM
            self.radius = MINE_RADIUS

    def __repr__(self):
        name = ITEM_ID_BY_CODE[self.kind] if 0 < self.kind <= ITEM_COUNT else '?'
        return '<Projectile %s #%d slot=%d life=%.2f>' % (
            name, self.id, self.owner, self.life)


# --- система бонусов --------------------------------------------------------

class ItemSystem(object):
    """Боксы, снаряды и применение бонусов одной гонки."""

    def __init__(self, track, settings=None, rng=None):
        self.track = track
        self.rng = rng if rng is not None else _default_rng

        settings = settings or {}
        enabled_flag = settings.get('items_enabled', True)
        # Маска разрешённых в комнате бонусов: считается один раз, в тик
        # разбор списка строк не попадает.
        self.item_mask = enabled_mask(settings.get('items', ITEM_IDS))
        if not enabled_flag:
            self.item_mask = 0
        # Бонусов нет вовсе — боксы не показываем и не подбираем.
        self.enabled = self.item_mask != 0

        # --- плоские копии осевой линии: обращение по индексу в списке float
        #     заметно дешевле, чем атрибут у TrackSample в горячем цикле
        samples = track.samples
        count = len(samples)
        self._n = count
        self._sx = [s.x for s in samples]
        self._sz = [s.z for s in samples]
        self._stx = [s.tangent_x for s in samples]
        self._stz = [s.tangent_z for s in samples]
        self._ss = [s.s for s in samples]
        self._step = track.length / count if count else 1.0

        # --- корзины вдоль дуги -------------------------------------------
        bins = int(track.length / BIN_SIZE) + 1
        if bins < 3:
            bins = 3
        self._bins = bins
        self._inv_bin = bins / track.length if track.length > 0.0 else 0.0

        # --- боксы ---------------------------------------------------------
        boxes = track.item_boxes
        box_count = len(boxes)
        self.box_count = box_count
        self.box_x = [float(b['x']) for b in boxes]
        self.box_z = [float(b['z']) for b in boxes]
        self.box_s = [float(b['s']) for b in boxes]
        self.box_active = [self.enabled] * box_count
        self.box_timer = [0.0] * box_count
        # Связные списки боксов по корзинам: строятся один раз, боксы не ездят.
        self._box_head = [-1] * bins
        self._box_next = [-1] * box_count
        for i in range(box_count):
            slot_bin = self._bin_of_s(self.box_s[i])
            self._box_next[i] = self._box_head[slot_bin]
            self._box_head[slot_bin] = i
        self._mask_len = (box_count + 7) >> 3
        self._box_mask = bytearray(self._mask_len)
        self._mask_dirty = True

        # --- пул снарядов ---------------------------------------------------
        # Три преаллоцированных массива int вместо динамики: свободные записи
        # лежат стопкой в _free, занятые — плотно в начале _live, а _live_pos
        # даёт обратный переход «запись пула -> её место в _live». Благодаря
        # ему ни создание, ни гашение снаряда не требуют поиска.
        self.projectiles = [Projectile() for _ in range(MAX_PROJECTILES)]
        self._live = [0] * MAX_PROJECTILES        # индексы занятых записей пула
        self._live_pos = [-1] * MAX_PROJECTILES   # запись пула -> место в _live
        self._free = list(range(MAX_PROJECTILES))
        self._free_count = MAX_PROJECTILES
        self.live_count = 0
        self._proj_head = [-1] * bins
        self._proj_next = [-1] * MAX_PROJECTILES
        self._next_id = 1

        self._pickup_r2 = PICKUP_RADIUS * PICKUP_RADIUS

    # --- корзины ------------------------------------------------------------

    def _bin_of_s(self, s):
        """Номер корзины по расстоянию вдоль дуги."""
        index = int(s * self._inv_bin)
        if index >= self._bins:
            index %= self._bins
        elif index < 0:
            index = index % self._bins
        return index

    def _bin_link(self, index, proj):
        """Вложить снаряд в корзину."""
        self._proj_next[index] = self._proj_head[proj.bin_index]
        self._proj_head[proj.bin_index] = index

    def _bin_unlink(self, index, bin_index):
        """Вынуть снаряд из корзины (список короткий, проход дешёвый)."""
        head = self._proj_head[bin_index]
        if head == index:
            self._proj_head[bin_index] = self._proj_next[index]
            return
        prev = head
        while prev >= 0:
            nxt = self._proj_next[prev]
            if nxt == index:
                self._proj_next[prev] = self._proj_next[index]
                return
            prev = nxt

    # --- снапшот -------------------------------------------------------------

    def box_mask(self):
        """Маска активных боксов для снапшота (раздел 12.3).

        Буфер один на всю гонку и пересобирается только когда состав активных
        боксов изменился.
        """
        if self._mask_dirty:
            mask = self._box_mask
            for i in range(self._mask_len):
                mask[i] = 0
            active = self.box_active
            for i in range(self.box_count):
                if active[i]:
                    mask[i >> 3] |= 1 << (i & 7)
            self._mask_dirty = False
        return self._box_mask

    def fill_snapshot(self, out):
        """Дописать живые снаряды в список ``out`` кортежами (id, kind, x, z, yaw).

        Зовётся только при сборке снапшота (20 Гц), не каждый тик.
        """
        live = self._live
        pool = self.projectiles
        for k in range(self.live_count):
            proj = pool[live[k]]
            out.append((proj.id, proj.kind, proj.x, proj.z, proj.yaw))

    # --- применение бонуса ---------------------------------------------------

    def use(self, sim, car):
        """Применить бонус, который держит ``car``. Возвращает код или 0.

        Слот бонуса очищается только если применение действительно состоялось:
        при исчерпанном пуле снарядов (потолок ``MAX_PROJECTILES`` из
        протокола) бонус остаётся в руках, а не пропадает молча.
        """
        kind = car.item
        if not kind or car.removed or car.ghost:
            return 0
        state = car.state

        if kind == ITEM_BOOST:
            state.boost_time = BOOST_TIME
        elif kind == ITEM_SHIELD:
            state.shield_time = SHIELD_TIME
        elif kind == ITEM_STORM:
            self._cast_storm(sim, car)
        elif kind == ITEM_ROCKET:
            if not self._launch_rocket(sim, car):
                return 0
        elif kind == ITEM_MINE:
            if not self._drop_mine(sim, car):
                return 0
        else:
            car.item = 0
            return 0

        car.item = 0
        sim.events.append({
            't': 'race_event', 'kind': 'use',
            'slot': car.slot, 'item': ITEM_ID_BY_CODE[kind],
        })
        return kind

    def _target_ahead(self, sim, car):
        """Ближайший впереди по progress — сосед сверху в таблице мест.

        O(1) по ``sim.rank``: перебирать машины ради этого не нужно, они уже
        отсортированы по дуге для выставления мест.
        """
        rank = sim.rank
        cars = sim.cars
        index = car.rank_index - 1
        while index >= 0:
            other = cars[rank[index]]
            if not other.removed and not other.ghost:
                return other
            index -= 1
        return None

    def _launch_rocket(self, sim, car):
        """Выпустить ракету вперёд по курсу машины."""
        index = self._take()
        if index < 0:
            return False
        proj = self.projectiles[index]
        state = car.state
        yaw = state.yaw
        x = state.x + math.sin(yaw) * ROCKET_SPAWN_AHEAD
        z = state.z + math.cos(yaw) * ROCKET_SPAWN_AHEAD
        proj.spawn(self._new_id(), ITEM_ROCKET, car.slot, x, z, yaw,
                   state.sample_idx)
        target = self._target_ahead(sim, car)
        proj.target = target.slot if target is not None else -1
        self._place(index)
        return True

    def _drop_mine(self, sim, car):
        """Сбросить мину позади машины."""
        index = self._take()
        if index < 0:
            return False
        proj = self.projectiles[index]
        state = car.state
        yaw = state.yaw
        x = state.x - math.sin(yaw) * MINE_DROP_BACK
        z = state.z - math.cos(yaw) * MINE_DROP_BACK
        idx = self.track.nearest_index(x, z, state.sample_idx)
        proj.spawn(self._new_id(), ITEM_MINE, car.slot, x, z, yaw, idx)
        self._place(index)
        return True

    def _cast_storm(self, sim, car):
        """Гроза: замедлить всех, кто впереди."""
        rank = sim.rank
        cars = sim.cars
        index = car.rank_index - 1
        while index >= 0:
            other = cars[rank[index]]
            index -= 1
            if other.removed or other.ghost:
                continue
            self._apply_hit(sim, other, car.slot, ITEM_STORM)

    # --- пул -----------------------------------------------------------------

    def _new_id(self):
        """Идентификатор снаряда для снапшота: u16, крутится по кругу."""
        proj_id = self._next_id
        self._next_id = proj_id + 1 if proj_id < 65535 else 1
        return proj_id

    def _take(self):
        """Индекс свободной записи пула или -1, если потолок исчерпан.

        ``MAX_PROJECTILES`` — контракт протокола (раздел 12.3): снарядов
        физически не может быть больше, лишнее не создаётся.
        """
        free = self._free_count
        if free <= 0:
            return -1
        free -= 1
        self._free_count = free
        index = self._free[free]
        position = self.live_count
        self._live[position] = index
        self._live_pos[index] = position
        self.live_count = position + 1
        return index

    def _place(self, index):
        """Вложить только что созданный снаряд в корзину."""
        proj = self.projectiles[index]
        proj.bin_index = self._bin_of_s(self._ss[proj.sample_idx])
        self._bin_link(index, proj)

    def _kill(self, index):
        """Погасить снаряд по индексу записи пула."""
        position = self._live_pos[index]
        if position < 0:
            return
        proj = self.projectiles[index]
        if proj.bin_index >= 0:
            self._bin_unlink(index, proj.bin_index)
            proj.bin_index = -1
        proj.active = False
        live = self._live
        last = self.live_count - 1
        moved = live[last]
        live[position] = moved
        self._live_pos[moved] = position
        self._live_pos[index] = -1
        self.live_count = last
        self._free[self._free_count] = index
        self._free_count += 1

    # --- тик -----------------------------------------------------------------

    def update(self, sim, dt):
        """Шаг бонусов: снаряды летят, мины взводятся, боксы возвращаются.

        Порядок: сначала респаун боксов, потом движение и жизнь снарядов,
        потом поражение машин. Так попадание всегда считается по положению,
        которое уйдёт в снапшот этого же тика.
        """
        if not self.enabled:
            return
        self._respawn_boxes(dt)
        self._move_projectiles(sim, dt)
        self._detect_hits(sim)

    def _respawn_boxes(self, dt):
        """Подобранные боксы возвращаются через BOX_RESPAWN."""
        timer = self.box_timer
        active = self.box_active
        for i in range(self.box_count):
            if active[i]:
                continue
            left = timer[i] - dt
            if left <= 0.0:
                timer[i] = 0.0
                active[i] = True
                self._mask_dirty = True
            else:
                timer[i] = left

    def _move_projectiles(self, sim, dt):
        """Ракеты наводятся и летят, мины взводятся, всё стареет."""
        pool = self.projectiles
        live = self._live
        # Идём с хвоста: _kill переставляет последний живой снаряд на
        # освободившееся место, поэтому уже просмотренные записи не сдвигаются.
        k = self.live_count - 1
        while k >= 0:
            index = live[k]
            proj = pool[index]
            life = proj.life - dt
            if life <= 0.0:
                self._kill(index)
                k -= 1
                continue
            proj.life = life
            if proj.arm > 0.0:
                arm = proj.arm - dt
                proj.arm = arm if arm > 0.0 else 0.0
            if proj.kind == ITEM_ROCKET:
                self._steer_rocket(sim, proj, dt)
                self._advance_along(proj)
                new_bin = self._bin_of_s(self._ss[proj.sample_idx])
                if new_bin != proj.bin_index:
                    self._bin_unlink(index, proj.bin_index)
                    proj.bin_index = new_bin
                    self._bin_link(index, proj)
            k -= 1

    def _steer_rocket(self, sim, proj, dt):
        """Наведение с конечной скоростью поворота и полёт на шаг."""
        target_slot = proj.target
        if target_slot >= 0:
            target = sim.car_by_slot.get(target_slot)
            if target is None or target.removed or target.ghost:
                # Цель выбыла: перенаводимся на того, кто теперь впереди
                # выпустившего. Не нашли — летим прямо и догораем.
                owner = sim.car_by_slot.get(proj.owner)
                target = self._target_ahead(sim, owner) if owner is not None else None
                proj.target = target.slot if target is not None else -1
            if target is not None:
                dx = target.state.x - proj.x
                dz = target.state.z - proj.z
                if dx * dx + dz * dz > 1e-6:
                    desired = math.atan2(dx, dz)
                    diff = desired - proj.yaw
                    # кратчайшая дуга
                    if diff > math.pi:
                        diff -= 2.0 * math.pi
                    elif diff < -math.pi:
                        diff += 2.0 * math.pi
                    limit = ROCKET_TURN_RATE * dt
                    if diff > limit:
                        diff = limit
                    elif diff < -limit:
                        diff = -limit
                    proj.yaw += diff
        travel = proj.speed * dt
        proj.x += math.sin(proj.yaw) * travel
        proj.z += math.cos(proj.yaw) * travel

    def _advance_along(self, proj):
        """Довести индекс осевой точки снаряда локальным шагом.

        За тик ракета пролетает меньше метра при шаге точек в два, поэтому
        цикл делает ноль или один шаг. Полный поиск по трассе не нужен.
        """
        index = proj.sample_idx
        step = self._step
        n = self._n
        sx = self._sx
        sz = self._sz
        stx = self._stx
        stz = self._stz
        for _ in range(6):
            along = (proj.x - sx[index]) * stx[index] + (proj.z - sz[index]) * stz[index]
            if along > step:
                index = index + 1 if index + 1 < n else 0
            elif along < -step:
                index = index - 1 if index > 0 else n - 1
            else:
                break
        else:
            # Ракета улетела далеко в сторону (стена, срез) — честный поиск,
            # он редкий и стоит того, чтобы корзина не разъехалась.
            index = self.track.nearest_index(proj.x, proj.z, proj.sample_idx)
        proj.sample_idx = index

    def _detect_hits(self, sim):
        """Поражение машин: машина смотрит только в соседние корзины."""
        if not self.live_count:
            return
        cars = sim.cars
        pool = self.projectiles
        head = self._proj_head
        nxt = self._proj_next
        bins = self._bins
        ss = self._ss
        for car in cars:
            if car.removed or car.ghost or car.finished:
                continue
            state = car.state
            if state.spin_time > 0.0:
                continue           # уже крутит — второй раз не бьём
            cx = state.x
            cz = state.z
            base = self._bin_of_s(ss[state.sample_idx])
            b = base - _BIN_LOOKUP
            # Одна машина — одно попадание за тик: иначе наехавший на мину
            # под ракетой сжигал бы за один кадр и щит, и половину арсенала.
            hit_index = -1
            hit_kind = 0
            hit_owner = -1
            while b <= base + _BIN_LOOKUP and hit_index < 0:
                index = head[b % bins]
                while index >= 0:
                    proj = pool[index]
                    if proj.arm <= 0.0 and not (
                            proj.kind == ITEM_ROCKET and proj.owner == car.slot):
                        dx = cx - proj.x
                        dz = cz - proj.z
                        radius = proj.radius
                        if dx * dx + dz * dz <= radius * radius:
                            hit_index = index
                            hit_kind = proj.kind
                            hit_owner = proj.owner
                            break
                    index = nxt[index]
                b += 1
            if hit_index >= 0:
                self._kill(hit_index)
                self._apply_hit(sim, car, hit_owner, hit_kind)

    def _apply_hit(self, sim, car, by_slot, kind):
        """Попадание: щит гасит ровно одно и при этом тратится (раздел 8)."""
        state = car.state
        blocked = state.shield_time > 0.0
        if blocked:
            state.shield_time = 0.0
        elif kind == ITEM_STORM:
            state.slow_time = STORM_SLOW
        elif kind == ITEM_ROCKET:
            state.spin_time = ROCKET_SPIN
            state.drift_active = False
            state.drift_charge = 0.0
        else:
            state.spin_time = MINE_SPIN
            state.drift_active = False
            state.drift_charge = 0.0
        sim.events.append({
            't': 'race_event', 'kind': 'hit',
            'slot': car.slot, 'by': by_slot,
            'item': ITEM_ID_BY_CODE[kind], 'blocked': blocked,
        })

    # --- подбор боксов --------------------------------------------------------

    def check_pickups(self, sim):
        """Подбор боксов: машина смотрит только в соседние корзины.

        Занятый слот бонуса — подбор игнорируется целиком, бокс остаётся
        стоять (раздел 8).
        """
        if not self.enabled:
            return
        head = self._box_head
        nxt = self._box_next
        bins = self._bins
        active = self.box_active
        timer = self.box_timer
        box_x = self.box_x
        box_z = self.box_z
        ss = self._ss
        radius2 = self._pickup_r2
        mask = self.item_mask
        rng = self.rng
        total = sim.racer_count
        for car in sim.cars:
            if car.item or car.removed or car.ghost or car.finished:
                continue
            state = car.state
            cx = state.x
            cz = state.z
            base = self._bin_of_s(ss[state.sample_idx])
            b = base - _BIN_LOOKUP
            found = -1
            best = radius2
            # Боксы в ряду стоят плотнее радиуса подбора, поэтому берём
            # ближайший, а не первый попавшийся: иначе машина «подбирала» бы
            # соседний бокс через один.
            while b <= base + _BIN_LOOKUP:
                index = head[b % bins]
                while index >= 0:
                    if active[index]:
                        dx = cx - box_x[index]
                        dz = cz - box_z[index]
                        dist2 = dx * dx + dz * dz
                        if dist2 <= best:
                            best = dist2
                            found = index
                    index = nxt[index]
                b += 1
            if found < 0:
                continue
            kind = _roll_masked(place_row(car.place, total), mask, rng)
            if not kind:
                continue
            active[found] = False
            timer[found] = BOX_RESPAWN
            self._mask_dirty = True
            car.item = kind
            sim.events.append({
                't': 'race_event', 'kind': 'pickup',
                'slot': car.slot, 'item': ITEM_ID_BY_CODE[kind],
            })

    def __repr__(self):
        return '<ItemSystem боксов %d, снарядов %d/%d>' % (
            self.box_count, self.live_count, MAX_PROJECTILES)
