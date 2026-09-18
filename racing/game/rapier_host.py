# -*- coding: utf-8 -*-
"""Хозяин модуля физики на сервере: wasmtime + плоский ABI.

Что это. ``native/testbed/racing_physics.wasm`` — Rapier, собранный под
wasm32 (§12.21). Импортов у модуля ноль, наружу торчат 45 функций и одна
линейная память; весь обмен идёт через общие буферы, адреса которых модуль
отдаёт сам. Раскладку этих буферов ЗДЕСЬ НЕ ОПИСЫВАЮТ: она импортируется
из ``native/abi/abi_layout.py``, выпущенного ``tools/gen_abi.py``. Иначе
вместо двух физик получились бы три описания одной структуры.

Почему ctypes-виды, а не ``Memory.read/write``. Замер разведки (§4):
вызов экспортированной функции 9,5 мкс, штатная запись 128 байт 2,5 мкс,
а ``ctypes`` по сырому адресу — 0,38 мкс. Поэтому за тик делается РОВНО
один вызов ``rp_step``, а ввод и вывод лежат в ctypes-массивах поверх
памяти модуля.

Осторожно: линейная память wasm умеет расти, и при росте базовый адрес
переезжает. Все виды пересоздаются в ``_sync()``, который вызывается перед
каждым шагом и после каждой аллокации внутри модуля.

Флаг. Переменная окружения ``RACING_PHYSICS`` (или ``run.py --physics``):

* ``classic`` — умолчание, старая арифметика раздела 6. Этот модуль при
  ней не импортирует wasmtime и не создаёт ни одного объекта;
* ``shadow`` — Rapier крутится ТЕНЬЮ рядом с гонкой: те же вводы, тот же
  темп, но состояние машин из него не берётся. Нужен, чтобы мерить цену
  тика и сверять хэш мира на живой трассе, ничем не рискуя в гонке;
* ``rapier`` — гонку считает Rapier (§12.24). ``game/physics.py`` при этом
  не зовётся вовсе, а прогресс, круги и отсечки по-прежнему считает
  ``game/track.py`` поверх позы из модуля: они игровые правила, а не физика,
  и в f32 их затаскивать нельзя (§8.5 разведки).

Умолчание — ``classic``, и оно обязано вести себя ровно как до 4b.
"""

from __future__ import annotations

import ctypes
import importlib.util
import math
import os
import struct
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(BASE_DIR, 'vendor')
ABI_PATH = os.path.join(BASE_DIR, 'native', 'abi', 'abi_layout.py')
WASM_PATH = os.path.join(BASE_DIR, 'native', 'testbed', 'racing_physics.wasm')

# --- флаг -------------------------------------------------------------------

ENV_VAR = 'RACING_PHYSICS'
CLASSIC = 'classic'
SHADOW = 'shadow'
RAPIER = 'rapier'
BACKENDS = (CLASSIC, SHADOW, RAPIER)


def backend() -> str:
    """Какая физика выбрана. Читается в холодном пути, раз за гонку."""
    name = (os.environ.get(ENV_VAR) or CLASSIC).strip().lower()
    return name if name in BACKENDS else CLASSIC


def enabled() -> bool:
    """Нужен ли процессу модуль физики вообще (тень ИЛИ настоящая гонка)."""
    return backend() != CLASSIC


def race_enabled() -> bool:
    """Считает ли гонку Rapier. Только при этом значении меняется заезд."""
    return backend() == RAPIER


# --- что Rapier пока не считает (§12.24, §12.25) -----------------------------
#
# Поток машин и происшествия живут в game/traffic.py и game/events.py и
# написаны под состояние старой физики: они правят x/z/vx/vz и подменяют
# характеристики между шагами. У Rapier состояние машины лежит внутри
# модуля, менять его снаружи между шагами нечем, а на половину перенесённой
# механике играть хуже, чем без неё. Поэтому при physics=rapier эти
# настройки честно опускаются, и комната об этом СООБЩАЕТ.
#
# БОНУСЫ ИЗ ЭТОГО СПИСКА УШЛИ (§12.25): дверь «внешнее воздействие на
# машину» в ABI теперь есть — буфер EFFECTS и структура CarEffect. Через
# неё же поедут покрытие и выталкивание происшествий: grip_drop уже
# множит сцепление колёс, push_* и shift_* уже двигают тело. Здесь остались
# только те две настройки, которым нужна не дверь, а тела в мире —
# болванки потока и корпуса твёрдых препятствий, — и та, что не про
# воздействие вовсе.
#
# Столкновения в этом списке с другой стороны: их Rapier считает ВНУТРИ
# шага, и выключить их нечем — групп столкновений в ABI нет. Галочка
# «Столкновения» при этом режиме не врёт только принудительно включённой.
#
# (имя поля настроек, во что оно ставится, что сказать игроку)
RESTRICTED = (
    ('traffic', 'off', 'поток машин выключен'),
    ('events', 'off', 'происшествия выключены'),
    ('collisions', True, 'столкновения включены всегда'),
    # Гандикап подменяет ХАРАКТЕРИСТИКИ машины уже после того, как мир
    # создан и тела расставлены, а перенастроить живую машину модуль не
    # умеет: настройки читаются один раз, в spawn_car. Оставить галочку
    # включённой значило бы обещать замедление победителя и не делать его.
    ('handicap', False, 'гандикап выключен'),
)


def restrict_settings(settings) -> list:
    """Привести настройки к тому, что Rapier действительно считает.

    Правит переданный словарь на месте, возвращает список фраз для игрока.
    Пустой список — править было нечего, то есть игрок и не просил ничего
    из этого.
    """
    off = []
    for name, value, label in RESTRICTED:
        if name not in settings:
            continue
        if settings[name] == value:
            continue
        settings[name] = value
        off.append(label)
    return off


# --- модель управляемости ----------------------------------------------------
#
# Настройка комнаты ``physics`` (§12.23) выбирает пресет CarTuning: «аркада»
# или «симулятор». Номер — это аргумент ``rp_world_reset`` и
# ``rp_tuning_preset``, порядок задан перечислением ``Preset`` в
# native/src/world.rs: менять только парой.
#
# Список имён повторён в ``server.config.PHYSICS_MODES``: game/ про server/ не
# знает намеренно — ровно так же живёт WEATHER_GRIP в game/physics.py. Чтобы
# копии не разъехались молча, ``tools/test_sim.py`` сверяет их одной проверкой.
PRESET_NAMES = ('arcade', 'sim')


def preset_index(name) -> int:
    """Номер пресета по имени режима. Мусор — это аркада, как и умолчание."""
    if not isinstance(name, str):
        return 0
    try:
        return PRESET_NAMES.index(name.strip().lower())
    except ValueError:
        return 0


# --- раскладка и рантайм ------------------------------------------------------

def _load_abi():
    """Выпущенная раскладка. Импорт по пути: класть native/ в sys.path незачем."""
    spec = importlib.util.spec_from_file_location('racing_abi_layout', ABI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    abi = _load_abi()
    _ABI_ERROR = None
except OSError as _exc:          # native/abi/ не на месте — repo битый
    # Не валим импорт: при выключенном флаге сервер обязан подняться так же,
    # как поднимался до 4b. Жалуемся там, где хозяина действительно просят.
    abi, _ABI_ERROR = None, _exc

# Геометрия полотна, которую хозяин передаёт модулю (§12.21). Модуль игровых
# констант не знает, поэтому оба хозяина ОБЯЗАНЫ передать одно и то же.
# Объявлены они ОДИН раз, в game/track.py, и уезжают клиенту в race_init.track
# (§12.23): здесь их не переопределяют, а импортируют, и браузерный хозяин
# берёт те же числа из присланной записи трассы. На 4b два из них лежали
# по копии в каждом хозяине — эта копия убрана.
from .track import (WALL_MARGIN, WALL_HEIGHT,         # noqa: E402  (после _load_abi)
                    TRACK_FRICTION)


class HostError(RuntimeError):
    """Модуль не тот, ABI не тот, рантайма нет — всё сюда."""


# Имя поля CarTuning -> его индекс во f32. ВЫВОДИТСЯ из выпущенной раскладки,
# а не переписывается руками: список полей живёт в tools/abi_layout.json, и
# четвёртое его описание тут было бы той же болезнью, что и третье (§12.21).
def _tuning_index() -> dict:
    if abi is None:
        return {}
    skip = ('SIZE', 'FLOATS')
    return {name.lower(): getattr(abi.CarTuning, name)
            for name in dir(abi.CarTuning)
            if name.isupper() and name not in skip}


TUNING_INDEX = _tuning_index()
TUNING_FIELDS = tuple(sorted(TUNING_INDEX))


def mesh_params(track) -> tuple:
    """Три числа сетки полотна: (wall_margin, wall_height, friction).

    Берутся ОТТУДА ЖЕ, откуда их получит клиент, — из записи трассы формата
    12.1, если она словарь ``Track.to_client()``. Для объекта ``Track``
    источник тот же: модуль ``game/track.py``, который эти поля и заполняет.
    """
    if isinstance(track, dict):
        try:
            return (float(track['wall_margin']), float(track['wall_height']),
                    float(track['friction']))
        except KeyError as exc:
            raise HostError('в записи трассы нет поля %s: сетку полотна не из '
                            'чего строить (§12.23)' % exc)
    return (WALL_MARGIN, WALL_HEIGHT, TRACK_FRICTION)


def load_wasmtime():
    """wasmtime из ``vendor/``. Системный пакет не нужен и не ищется первым.

    На машинах заказчика ставить нечего (§1), поэтому рантайм лежит в
    репозитории папкой — ``tools/vendor_wasmtime.py`` её раскладывает.
    Свой каталог идёт в начало sys.path: версия рантайма физики должна быть
    той, на которой сданы замеры, а не той, что нашлась на машине.
    """
    if VENDOR_DIR not in sys.path:
        sys.path.insert(0, VENDOR_DIR)
    try:
        import wasmtime
    except ImportError as exc:      # pragma: no cover — только при битом vendor/
        raise HostError(
            'wasmtime не найден ни в vendor/, ни в системе: %s\n'
            'разложите его: python3 tools/vendor_wasmtime.py --wheel <колесо>' % exc)
    return wasmtime


# --- хозяин ------------------------------------------------------------------

class RapierHost(object):
    """Один экземпляр модуля = один мир. Таблицы миров в ABI нет намеренно."""

    def __init__(self, wasm_path: str = WASM_PATH, preset: int = 0):
        if abi is None:
            raise HostError('нет выпущенной раскладки %s: %s\n'
                            'выпустите её: python3 tools/gen_abi.py' % (ABI_PATH, _ABI_ERROR))
        wasmtime = load_wasmtime()
        if not os.path.isfile(wasm_path):
            raise HostError('нет модуля физики: %s' % wasm_path)
        engine = wasmtime.Engine()
        module = wasmtime.Module.from_file(engine, wasm_path)
        if len(module.imports) != 0:
            raise HostError('у модуля физики появились импорты (%d) — '
                            'хозяин рассчитан на модуль без них'
                            % len(module.imports))
        self.wasm_path = wasm_path
        self._store = wasmtime.Store(engine)
        self._module = module
        self._inst = wasmtime.Instance(self._store, module, [])
        ex = self._inst.exports(self._store)
        self._ex = ex
        self._mem = ex['memory']

        # Горячий путь достаёт функции из атрибутов, а не из словаря экспортов.
        store = self._store
        self._f_step = ex['rp_step']
        self._f_inputs = ex['rp_inputs_ptr']
        self._f_effects = ex['rp_effects_ptr']
        self._f_outputs = ex['rp_outputs_ptr']
        self._f_descs = ex['rp_descs_ptr']
        self._f_tuning = ex['rp_tuning_ptr']
        self._f_props = ex['rp_props_ptr']
        self._f_saves = ex['rp_saves_ptr']

        version = ex['rp_abi_version'](store)
        if version != abi.ABI_VERSION:
            raise HostError('ABI модуля %d, раскладки %d — пересоберите модуль'
                            % (version, abi.ABI_VERSION))
        stride = ex['rp_output_stride'](store)
        if stride != abi.CarOut.SIZE:
            raise HostError('шаг записи вывода %d, в раскладке %d'
                            % (stride, abi.CarOut.SIZE))
        eff_floats = ex['rp_effect_floats'](store)
        if eff_floats != abi.CarEffect.FLOATS:
            raise HostError('чисел в записи воздействия %d, в раскладке %d'
                            % (eff_floats, abi.CarEffect.FLOATS))
        max_cars = ex['rp_max_cars'](store)
        if max_cars != abi.MAX_CARS:
            raise HostError('машин в модуле %d, в раскладке %d'
                            % (max_cars, abi.MAX_CARS))
        columns = ex['rp_track_columns'](store)
        if columns != abi.TRACK_COLUMNS:
            raise HostError('столбцов осевой в модуле %d, в раскладке %d'
                            % (columns, abi.TRACK_COLUMNS))

        self.dt = ex['rp_dt'](store)
        self._pages = -1
        self._sync(force=True)
        self.reset(preset)

    # --- память ---------------------------------------------------------

    def _sync(self, force: bool = False) -> None:
        """Пересоздать виды, если линейная память переехала.

        Дешевле некуда: одна проверка размера памяти. Rapier аллоцирует
        внутри шага, поэтому проверка обязана быть перед КАЖДЫМ шагом —
        иначе рано или поздно хозяин прочитает освобождённый адрес.
        """
        store = self._store
        pages = self._mem.size(store)
        if not force and pages == self._pages:
            return
        self._pages = pages
        base = ctypes.addressof(self._mem.data_ptr(store).contents)
        self._base = base
        self.inputs = (ctypes.c_float * (abi.MAX_CARS * abi.CarInput.FLOATS)) \
            .from_address(base + self._f_inputs(store))
        # Внешние воздействия (§12.25): хозяин пишет дозу на один шаг,
        # модуль применяет её внутри шага и запись обнуляет.
        self.effects = (ctypes.c_float * (abi.MAX_CARS * abi.CarEffect.FLOATS)) \
            .from_address(base + self._f_effects(store))
        self.outputs = (ctypes.c_float * (abi.MAX_CARS * abi.CarOut.FLOATS)) \
            .from_address(base + self._f_outputs(store))
        self.descs = (ctypes.c_float * (abi.MAX_CARS * abi.CarDesc.FLOATS)) \
            .from_address(base + self._f_descs(store))
        self.tuning = (ctypes.c_float * abi.CarTuning.FLOATS) \
            .from_address(base + self._f_tuning(store))
        self.props = (ctypes.c_float * (abi.MAX_PROPS * abi.PropOut.FLOATS)) \
            .from_address(base + self._f_props(store))
        self.saves = (ctypes.c_float * (abi.MAX_CARS * abi.CarSave.FLOATS)) \
            .from_address(base + self._f_saves(store))

    def _write(self, offset: int, data: bytes) -> None:
        """Залить кусок в память модуля одним memmove."""
        self._sync()
        ctypes.memmove(self._base + offset, data, len(data))

    # --- мир ------------------------------------------------------------

    def reset(self, preset: int = 0) -> None:
        """Пересоздать мир. preset: 0 — аркада, 1 — симулятор (§12.21)."""
        self._ex['rp_world_reset'](self._store, int(preset))
        self._sync(force=True)

    def build_track(self, track, wall_margin: float = None,
                    wall_height: float = None,
                    friction: float = None) -> None:
        """Залить осевую линию и попросить модуль построить полотно.

        Три числа сетки по умолчанию берутся из самой записи трассы
        (``mesh_params``), а не из констант хозяина: у браузера их взять
        больше неоткуда, и брать их разными способами значило бы вернуть
        ту самую копию.

        ``track`` — либо ``game.track.Track``, либо уже готовый словарь
        ``Track.to_client()``. Числа в обоих случаях одни и те же: сервер
        хранит квантованные ``_f32(round(v, 3))``, а клиент получает те же
        ``round(v, 3)`` и кладёт их в ``Float32Array``. Это и есть общий
        вход физики из §12.21 — не картинка, а данные.
        """
        if wall_margin is None or wall_height is None or friction is None:
            margin, height, rub = mesh_params(track)
            if wall_margin is None:
                wall_margin = margin
            if wall_height is None:
                wall_height = height
            if friction is None:
                friction = rub
        data = centerline_bytes(track)
        count = len(data) // (4 * abi.TRACK_COLUMNS)
        store = self._store
        # Аллокация внутри модуля может подвинуть память: виды после неё
        # недействительны, поэтому _write() начинается с _sync().
        offset = self._ex['rp_track_alloc_centerline'](store, count)
        if offset == 0:
            raise HostError('модуль не дал буфер под осевую линию (%d точек)' % count)
        self._write(offset, data)
        code = self._ex['rp_track_build'](store, float(wall_margin),
                                          float(wall_height), float(friction))
        if code != 0:
            raise HostError('rp_track_build вернул %d '
                            '(1 — осевая не залита, 2 — Rapier не принял сетку)' % code)
        self._sync(force=True)

    def add_ground(self, half_size: float = 300.0, friction: float = TRACK_FRICTION) -> None:
        self._ex['rp_add_ground'](self._store, float(half_size), float(friction))
        self._sync(force=True)

    def free_track_mesh(self) -> None:
        """Вернуть аллокатору копию сетки: сама сетка уже внутри Rapier."""
        self._ex['rp_track_free_mesh'](self._store)
        self._sync(force=True)

    def spawn_car(self, x: float, y: float, z: float, yaw: float) -> int:
        idx = self._ex['rp_car_spawn'](self._store, float(x), float(y),
                                       float(z), float(yaw))
        self._sync(force=True)
        return idx

    def car_count(self) -> int:
        return self._ex['rp_car_count'](self._store)

    def tuning_preset(self, preset: int) -> None:
        """Загрузить пресет в шаблон настроек следующей машины."""
        self._ex['rp_tuning_preset'](self._store, int(preset))
        self._sync()

    def set_tuning(self, values) -> None:
        """Наложить настройки машины на шаблон: ``{имя поля: число}``.

        Правки ложатся ПОВЕРХ загруженного пресета, поэтому в
        ``content/cars.json`` лежит только то, чем машина отличается от
        режима, а не все 32 числа пятью копиями. Следующая ``spawn_car``
        возьмёт шаблон как есть.
        """
        if not values:
            return
        self._sync()
        buf = self.tuning
        for name, value in values.items():
            index = TUNING_INDEX.get(name)
            if index is None:
                raise HostError('в CarTuning нет поля %r (есть: %s)'
                                % (name, ', '.join(TUNING_FIELDS)))
            buf[index] = float(value)

    def tuning_values(self) -> dict:
        """Шаблон целиком, именами полей. Для замеров и отладки."""
        self._sync()
        return {name: self.tuning[index] for name, index in TUNING_INDEX.items()}

    # --- шаг ------------------------------------------------------------

    def set_input(self, idx: int, throttle: float = 0.0, brake: float = 0.0,
                  steer: float = 0.0, handbrake: float = 0.0) -> None:
        base = idx * abi.CarInput.FLOATS
        inputs = self.inputs
        inputs[base + abi.CarInput.THROTTLE] = throttle
        inputs[base + abi.CarInput.BRAKE] = brake
        inputs[base + abi.CarInput.STEER] = steer
        inputs[base + abi.CarInput.HANDBRAKE] = handbrake

    def step(self, ticks: int = 1) -> int:
        """Шаг(и) по 1/60 с. Один вызов на тик — это весь стык с модулем."""
        self._sync()
        return self._f_step(self._store, ticks)

    # --- чтение ---------------------------------------------------------

    def car_out(self, idx: int) -> list:
        """52 числа состояния машины (раскладка CarOut)."""
        base = idx * abi.CarOut.FLOATS
        return list(self.outputs[base:base + abi.CarOut.FLOATS])

    def car_pose(self, idx: int) -> tuple:
        """(x, y, z, yaw, speed) — самое частое, без копирования всей записи."""
        out = self.outputs
        base = idx * abi.CarOut.FLOATS
        return (out[base + abi.CarOut.PX], out[base + abi.CarOut.PY],
                out[base + abi.CarOut.PZ], out[base + abi.CarOut.YAW],
                out[base + abi.CarOut.SPEED])

    def car_save(self, idx: int) -> int:
        return self._ex['rp_car_save'](self._store, int(idx))

    def car_restore(self, idx: int) -> int:
        return self._ex['rp_car_restore'](self._store, int(idx))

    # --- хэши -----------------------------------------------------------

    def _hash(self, name: str) -> str:
        store = self._store
        hi = self._ex[name + '_hi'](store) & 0xFFFFFFFF
        lo = self._ex[name + '_lo'](store) & 0xFFFFFFFF
        return '%08x%08x' % (hi, lo)

    def world_hash(self) -> str:
        """FNV-1a по состоянию ВСЕХ тел мира. Ловит расхождение раньше всех."""
        return self._hash('rp_world_hash')

    def state_hash(self) -> str:
        return self._hash('rp_state_hash')

    def track_hashes(self) -> tuple:
        return (self._hash('rp_track_verts_hash'), self._hash('rp_track_tris_hash'))

    def mesh_size(self) -> tuple:
        store = self._store
        return (self._ex['rp_track_vert_count'](store),
                self._ex['rp_track_tri_count'](store))


def centerline_bytes(track) -> bytes:
    """Девять массивов по N f32 подряд, порядок 12.1: x, y, z, tx, tz, nx, nz, hw, s."""
    if isinstance(track, dict):
        columns = [track['x'], track['y'], track['z'], track['tx'], track['tz'],
                   track['nx'], track['nz'], track['hw'], track['s']]
    else:
        samples = track.samples
        columns = [[getattr(s, name) for s in samples] for name in
                   ('x', 'y', 'z', 'tangent_x', 'tangent_z',
                    'normal_x', 'normal_z', 'half_width', 's')]
    count = len(columns[0])
    if count < 3:
        raise HostError('осевая линия короче трёх точек')
    packer = struct.Struct('<%df' % count)
    return b''.join(packer.pack(*column) for column in columns)


# --- теневой режим -----------------------------------------------------------

# Разбор ввода: биты те же, что у старой физики (12.4). Импортируем, а не
# повторяем числами.
from .protocol import (BTN_THROTTLE, BTN_BRAKE, BTN_LEFT,   # noqa: E402
                       BTN_RIGHT, BTN_DRIFT)

# Числа награды за занос (6.3). ИМПОРТИРУЮТСЯ, а не повторяются: модуль
# игровых констант не знает (12.21), пресет их не выдумывает, и второго
# места, где они записаны, в проекте быть не должно. Браузерный хозяин
# берёт те же шесть чисел из static/js/physics.js — это и есть их
# единственная пара объявлений, ровно как у WEATHER_GRIP.
from .physics import (DRIFT_CHARGE_L1, DRIFT_CHARGE_L2,   # noqa: E402
                      DRIFT_CHARGE_L3, DRIFT_BOOST_L1,
                      DRIFT_BOOST_L2, DRIFT_BOOST_L3, BOOST_ACCEL,
                      SPIN_RATE, SLOW_FACTOR)

# Дозы воздействий на один шаг (§12.25). Считаются из тех же констант
# раздела 6, по которым живёт классика: второго объявления не заводим,
# а браузер берёт ту же пару из static/js/physics.js.
SPEED_DROP_SLOW = 1.0 - SLOW_FACTOR   # доля продольной скорости за шаг «Грозы»


def car_tuning(stats, mode: str) -> dict:
    """Правки CarTuning для машины каталога в выбранном режиме (§12.23).

    ``stats`` — ``game.cars.CarSpec`` из ``content/cars.json`` либо
    ``physics.CarStats`` по умолчанию. У второй настроек нет, и это не
    ошибка: тогда машина едет чистым пресетом режима.

    Блок ``body`` общий для обоих режимов: кузов у машины один, различаются
    режимы управляемостью, а не габаритами.
    """
    # Награда за занос идёт ПЕРВОЙ и одинакова у всех машин: это правила
    # раздела 6.3, а не свойство кузова. Каталог может лечь поверх, но в
    # content/cars.json этих полей нет и заводить их там незачем.
    values = {
        'drift_charge_l1': DRIFT_CHARGE_L1,
        'drift_charge_l2': DRIFT_CHARGE_L2,
        'drift_charge_l3': DRIFT_CHARGE_L3,
        'drift_boost_l1': DRIFT_BOOST_L1,
        'drift_boost_l2': DRIFT_BOOST_L2,
        'drift_boost_l3': DRIFT_BOOST_L3,
        'boost_accel': BOOST_ACCEL,
        # Потолок буста у каждой машины свой — stats.boost_speed из
        # cars.json, уже с гандикапом, если он на неё наложен.
        'boost_speed': float(getattr(stats, 'boost_speed', 0.0) or 0.0),
    }
    tuning = getattr(stats, 'tuning', None)
    if not tuning:
        return values
    values.update(tuning.get('body') or {})
    values.update(tuning.get(mode) or {})
    return values


class ShadowWorld(object):
    """Мир Rapier, идущий рядом с гонкой на тех же вводах.

    Состояние машин из него НЕ берётся: гонку по-прежнему считает
    ``game/physics.py``. Смысл теневого режима — мерить цену тика и сверять
    хэш мира на живой трассе, ничем в гонке не рискуя.
    """

    def __init__(self, sim, wasm_path: str = WASM_PATH):
        self.sim = sim
        # Модель управляемости — настройка комнаты (§12.23). Мир создаётся
        # сразу нужным пресетом: менять его на ходу нечем и незачем.
        self.preset = preset_index(sim.settings.get('physics'))
        self.mode = PRESET_NAMES[self.preset]
        self.host = RapierHost(wasm_path, self.preset)
        self.host.build_track(sim.track)
        self.host.free_track_mesh()
        self.slots = []
        for car in sim.cars:
            state = car.state
            # Настройки машины под выбранный режим (§12.23): сперва пресет
            # в шаблон, потом правки этой машины поверх — и только потом
            # spawn_car, которая шаблон и читает.
            self.host.tuning_preset(self.preset)
            self.host.set_tuning(car_tuning(car.stats, self.mode))
            # Высоты у старого состояния нет: раздел 4 держит гонку в
            # плоскости (x, z), а вертикаль появилась только под прыжки.
            # Поэтому точку постановки берём у полотна и приподнимаем на
            # метр — машина падает на трассу и стоит, как ей положено.
            ground = sim.track.surface(state.x, state.z, state.sample_idx)[3]
            idx = self.host.spawn_car(state.x, ground + 1.0, state.z, state.yaw)
            self.slots.append((car, idx * abi.CarInput.FLOATS))
        self.ticks = 0
        self.micros = []          # цена теневого шага, мкс на тик
        self._classic = None      # подменяемый шаг старой физики

    # Горячий путь. Выбирается ОДИН раз, в attach(): при выключенном флаге
    # sim.tick() зовёт прежний метод и про этот класс ничего не знает.
    def step_cars(self, dt, events):
        self._classic(dt, events)
        host = self.host
        host._sync()
        inputs = host.inputs
        for car, base in self.slots:
            buttons = 0 if car.ghost or car.removed else car.buttons
            inputs[base + abi.CarInput.THROTTLE] = 1.0 if buttons & BTN_THROTTLE else 0.0
            inputs[base + abi.CarInput.BRAKE] = 1.0 if buttons & BTN_BRAKE else 0.0
            steer = 0.0
            if buttons & BTN_LEFT:
                steer += 1.0
            if buttons & BTN_RIGHT:
                steer -= 1.0
            inputs[base + abi.CarInput.STEER] = steer
            inputs[base + abi.CarInput.HANDBRAKE] = 1.0 if buttons & BTN_DRIFT else 0.0
        started = time.perf_counter()
        host._f_step(host._store, 1)
        self.micros.append((time.perf_counter() - started) * 1e6)
        self.ticks += 1

    def stats(self) -> dict:
        rows = sorted(self.micros)
        if not rows:
            return {'ticks': 0}
        return {
            'ticks': self.ticks,
            'cars': len(self.slots),
            'mean_us': sum(rows) / len(rows),
            'p50_us': rows[len(rows) // 2],
            'p99_us': rows[min(len(rows) - 1, int(len(rows) * 0.99))],
            'max_us': rows[-1],
            'world_hash': self.host.world_hash(),
        }



# --- гонку считает Rapier (§12.24) -------------------------------------------
#
# Холостые шаги после расстановки на решётке. §8.4 разведки: широкая фаза
# обновляется ТОЛЬКО внутри pipeline.step, поэтому на первом шаге после
# постройки мира лучи подвески не находят полотна и машина падает свободно.
# Один шаг чинит луч, остальные дают подвеске осесть: без них гонка начинается
# с просадки на сантиметр, и первый же снапшот уезжает от предсказания.
SETTLE_TICKS = 24

# Ниже этого под полотном машина считается выпавшей из мира и ставится
# обратно на ось. Сеткой полотно закрыто (12.21), стены в ней настоящие, но
# рельеф теперь тоже настоящий: на гребне машину может выкинуть за стену.
FELL_THROUGH = 8.0

# Занос: ниже этой скорости и этого угла скольжения флаг не поднимается.
# Флаг едет в снапшот битом 1 и кормит звук, дым и HUD; у старой физики его
# поднимал ручник вместе с шагом 15, здесь его приходится собрать из вывода
# модуля, потому что состояния «идёт занос» у Rapier нет.
DRIFT_MIN_SPEED = 4.0      # м/с
DRIFT_MIN_SLIP = 0.12      # рад, около 7 градусов


class RapierRace(object):
    """Мир Rapier, из которого гонка БЕРЁТ состояние машин.

    Что переносится сюда из раздела 6 и что остаётся на месте:

    * шаги 1–13 (управление, силы, интегрирование) — внутри модуля;
    * шаг 14 (границы полотна) — стены стоят в сетке, солвер держит их сам;
      хозяину остаётся флаг ``offtrack`` (его читает HUD и снапшот) и сеть
      безопасности на случай вылета за мир;
    * шаг 14б (трамплины) — трамплинов в сетке нет, зато есть настоящий
      рельеф: подъёмы тормозят, спуски разгоняют. ``height`` считается от
      полотна, как и раньше, но её теперь диктует не геометрия трамплина,
      а сама машина;
    * шаг 15 (заряд заноса и награда) — целиком в модуле, числа 6.3
      приезжают снаружи в ``CarTuning`` (§12.24);
    * шаг 16 (прогресс, круги, отсечки) — остаётся в ``game/track.py``
      слово в слово. Это игровое правило, а не физика, и считать его надо
      в f64 (§8.5 разведки);
    * ``resolve_collisions`` — внутри шага модуля, отдельным проходом не
      зовётся (§12.23 ожидал этого, §8.6 разведки это записал);
    * шаги 1, 7 и 8 в части ВНЕШНИХ ВОЗДЕЙСТВИЙ (раскрутка, ускорение от
      «Турбо», замедление «Грозой») — механика в модуле, секунды снаружи:
      хозяин держит таймеры и каждый тик выписывает дозу в ``CarEffect``
      (см. ``_write_effect`` и §12.25).
    """

    def __init__(self, sim, wasm_path: str = WASM_PATH):
        self.sim = sim
        track = sim.track
        self.preset = preset_index(sim.settings.get('physics'))
        self.mode = PRESET_NAMES[self.preset]
        self.host = RapierHost(wasm_path, self.preset)
        # Погода правится ТРЕНИЕМ СЕТКИ полотна: она одна на всю трассу и
        # на обе стороны, ей персональная доза не нужна. Покрытие ПОД
        # ОТДЕЛЬНОЙ МАШИНОЙ — масло и обломки — едет другой дорогой:
        # CarEffect.grip_drop (§12.25). Пока происшествия выключены
        # (RESTRICTED), потому что им нужны ещё и тела в мире.
        margin, height, friction = mesh_params(track)
        self.host.build_track(track, margin, height, friction * sim.grip_mul)
        self.host.free_track_mesh()

        self.slots = []          # (car, idx, in_base, out_base, rest_y)
        for car in sim.cars:
            state = car.state
            self.host.tuning_preset(self.preset)
            self.host.set_tuning(car_tuning(car.stats, self.mode))
            tuning = self.host.tuning_values()
            # Высота центра кузова, когда машина стоит колёсами на полотне:
            # колесо плюс ход подвески плюс вынос точки крепления вниз
            # (world.rs ставит её на -half_height * 0.2).
            rest_y = (tuning['wheel_radius'] + tuning['suspension_rest']
                      + tuning['half_height'] * 0.2)
            steer_max = tuning['steer_max'] or 1.0
            ground = track.surface(state.x, state.z, state.sample_idx)[3]
            idx = self.host.spawn_car(state.x, ground + rest_y, state.z, state.yaw)
            self.slots.append([car, idx, idx * abi.CarInput.FLOATS,
                               idx * abi.CarOut.FLOATS, rest_y, 1.0 / steer_max,
                               idx * abi.CarEffect.FLOATS])

        self.ticks = 0
        self.micros = []
        self._settle()

    # --- расстановка ----------------------------------------------------

    def _settle(self):
        """Дать подвеске осесть до первого тика гонки (§8.4 разведки)."""
        host = self.host
        host._sync()
        inputs = host.inputs
        for i in range(len(inputs)):
            inputs[i] = 0.0
        host._f_step(host._store, SETTLE_TICKS)
        self._read_all()

    def _teleport(self, idx: int, x: float, y: float, z: float, yaw: float) -> None:
        """Поставить тело в точку: пишем слот сохранения и просим restore.

        Отдельного «телепорта» в ABI нет и не надо: слот сохранения — это
        и есть полное состояние тела, а ``rp_car_restore`` его применяет.
        """
        host = self.host
        host._sync()
        saves = host.saves
        base = idx * abi.CarSave.FLOATS
        for k in range(abi.CarSave.FLOATS):
            saves[base + k] = 0.0
        saves[base + abi.CarSave.PX] = x
        saves[base + abi.CarSave.PY] = y
        saves[base + abi.CarSave.PZ] = z
        saves[base + abi.CarSave.QY] = math.sin(yaw * 0.5)
        saves[base + abi.CarSave.QW] = math.cos(yaw * 0.5)
        host.car_restore(idx)

    def _retire(self, entry):
        """Машина исчезла из гонки — убрать её тело с дороги.

        Снять тело из мира модуль не умеет, а оставить его на полотне нельзя:
        призрак, который уже никому не виден, продолжал бы расталкивать
        живых. Уводим под мир и гасим скорости.
        """
        self._teleport(entry[1], 0.0, -500.0, 0.0, 0.0)

    def _respawn(self, entry, state):
        """Сеть безопасности: машина ушла из мира — вернуть её на ось."""
        track = self.sim.track
        i = state.sample_idx
        x = track._cx[i]
        z = track._cz[i]
        y = track._cy[i] + entry[4]
        yaw = math.atan2(track._ctx[i], track._ctz[i])
        self._teleport(entry[1], x, y, z, yaw)
        state.x = x
        state.z = z
        state.yaw = yaw
        state.vx = 0.0
        state.vz = 0.0
        state.height = 0.0
        state.v_vert = 0.0
        state.airborne = False

    # --- горячий путь ---------------------------------------------------

    def step_cars(self, dt, events):
        """Подменяет ``Simulation._step_cars``: старая физика не зовётся."""
        host = self.host
        host._sync()
        inputs = host.inputs
        effects = host.effects
        out = host.outputs
        for entry in self.slots:
            car = entry[0]
            if car.removed:
                continue
            if car.ghost:
                left = car.ghost_time - dt
                if left <= 0.0:
                    car.ghost_time = 0.0
                    car.removed = True
                    self._retire(entry)
                    continue
                car.ghost_time = left
                buttons = 0
            else:
                buttons = car.buttons
            base = entry[2]
            inputs[base + abi.CarInput.THROTTLE] = 1.0 if buttons & BTN_THROTTLE else 0.0
            inputs[base + abi.CarInput.BRAKE] = 1.0 if buttons & BTN_BRAKE else 0.0
            steer = 0.0
            if buttons & BTN_LEFT:
                steer += 1.0
            if buttons & BTN_RIGHT:
                steer -= 1.0
            inputs[base + abi.CarInput.STEER] = steer
            inputs[base + abi.CarInput.HANDBRAKE] = 1.0 if buttons & BTN_DRIFT else 0.0
            # Третий барьер 12.15 модулю снаружи: полотна он не знает.
            # Флаг взят с прошлого шага — у клиента он ровно такой же,
            # потому что считается той же surface() по той же позе.
            inputs[base + abi.CarInput.OFFTRACK] = 1.0 if car.state.offtrack else 0.0
            self._write_effect(entry, effects, out, dt)
        started = time.perf_counter()
        host._f_step(host._store, 1)
        self.micros.append((time.perf_counter() - started) * 1e6)
        self.ticks += 1
        self._read_all(events)

    # --- внешние воздействия (§12.25) -----------------------------------

    def _write_effect(self, entry, effects, out, dt):
        """Перевести таймеры бонусов в дозу воздействия на ЭТОТ шаг.

        Единственная дверь, через которую в мир Rapier попадает всё, что
        действует на машину извне. Правило разделения то же, что у награды
        за занос (§12.24): **числа снаружи, механика в модуле**. Здесь —
        только перевод «сколько секунд осталось» в «что сделать на этом
        шаге»; сами секунды назначает ``game/items.py``.

        Почему таймеры тикают ЗДЕСЬ, а не в модуле. Их надо уметь
        восстанавливать при откате клиента, а кольцо отката у клиента уже
        есть — ``net.js`` хранит ``spinTime`` и ``slowTime`` с самой старой
        физики. Положи таймер в модуль — пришлось бы расширять ``CarSave``
        и заводить второе место, откуда его восстанавливать. Доза же живёт
        один шаг и переигрывается из таймера бесплатно.

        Порядок «действует и тикает на одном и том же шаге» дословно
        повторяет шаги 1 и 8 раздела 6.2: у классики ``spin_time -= dt``
        стоит там же, где раскрутка применяется.
        """
        car = entry[0]
        state = car.state
        e = entry[6]
        E = abi.CarEffect

        spin = state.spin_time
        if spin > 0.0:
            # Раскрутка: крутит, глушит ввод и сжигает копилку заноса.
            # Последнее — не украшение: без него заглушённый ручник
            # выглядел бы для шага 15 как «отпустил» и попадание ракеты
            # ОПЛАЧИВАЛО бы занос. У классики от этого спасает
            # items._apply_hit, который обнуляет заряд руками.
            effects[e + E.SPIN_RATE] = SPIN_RATE
            effects[e + E.STUN] = 1.0
            effects[e + E.DRIFT_RESET] = 1.0
            spin -= dt
            state.spin_time = spin if spin > 0.0 else 0.0

        slow = state.slow_time
        if slow > 0.0:
            effects[e + E.SPEED_DROP] = SPEED_DROP_SLOW
            slow -= dt
            state.slow_time = slow if slow > 0.0 else 0.0

        # Щит на движение не влияет вовсе — тикает здесь только потому,
        # что у классики его тикает физика, а тут физики хозяина нет.
        shield = state.shield_time
        if shield > 0.0:
            shield -= dt
            state.shield_time = shield if shield > 0.0 else 0.0

        # Буст: его остаток держит МОДУЛЬ (он же платит за занос), а
        # хозяин умеет только добавить. Разница между тем, что лежит в
        # состоянии, и тем, что в модуле, и есть заявка: ``_read_all``
        # уравнивает их каждый тик, поэтому всё, что больше, написано
        # хозяином — то есть ``ItemSystem.use``.
        want = state.boost_time
        if want > out[entry[3] + abi.CarOut.BOOST_TIME]:
            effects[e + E.BOOST_ADD] = want

    def _read_all(self, events=None):
        """Поза из модуля -> CarState, затем шаг 16 из game/track.py."""
        host = self.host
        out = host.outputs
        track = self.sim.track
        surface = track.surface
        advance = track.advance_progress
        o = abi.CarOut
        wheel_steer = o.WHEELS + abi.WheelOut.STEERING
        for entry in self.slots:
            car = entry[0]
            if car.removed:
                continue
            state = car.state
            base = entry[3]
            x = out[base + o.PX]
            y = out[base + o.PY]
            z = out[base + o.PZ]
            state.x = x
            state.z = z
            state.yaw = out[base + o.YAW]
            state.vx = out[base + o.VX]
            state.vz = out[base + o.VZ]
            state.v_vert = out[base + o.VY]
            state.steer = out[base + wheel_steer] * entry[5]
            state.drift_charge = out[base + o.DRIFT_CHARGE]
            state.drift_dir = int(out[base + o.DRIFT_DIR])
            state.boost_time = out[base + o.BOOST_TIME]
            # Выплата за занос (шаг 15): модуль держит уровень один шаг,
            # хозяину остаётся отправить то же событие, что и у классики.
            level = int(out[base + o.DRIFT_LEVEL])
            if level and events is not None:
                events.append({'t': 'race_event', 'kind': 'drift_boost',
                               'slot': car.slot, 'level': level})
            grounded = out[base + o.WHEELS_ON_GROUND]
            state.airborne = grounded == 0.0
            slip = out[base + o.SLIP_ANGLE]
            if slip < 0.0:
                slip = -slip
            speed = out[base + o.SPEED]
            state.drift_active = (slip > DRIFT_MIN_SLIP
                                  and speed > DRIFT_MIN_SPEED
                                  and grounded > 0.0)
            # Шаг 14 остался модулю: стены стоят в сетке. Хозяину нужен
            # только флаг «вне полотна» — его читают снапшот и HUD.
            i, lateral, half_width, ground, _pitch = surface(x, z, state.sample_idx)
            state.sample_idx = i
            state.offtrack = lateral > half_width or lateral < -half_width
            height = y - ground - entry[4]
            state.height = height if height > 0.0 else 0.0
            if height < -FELL_THROUGH:
                self._respawn(entry, state)
            # Шаг 16 — слово в слово прежний, в f64 и в game/track.py.
            advance(state, i)

    def noop_collisions(self):
        """Подменяет ``Simulation._resolve_collisions``: считать нечего.

        Столкновения машина-машина Rapier делает внутри шага. Болванок
        потока в его мире нет — поток при этом режиме выключен (RESTRICTED),
        поэтому и второго участника у прохода не осталось.
        """
        return

    # --- замеры ---------------------------------------------------------

    def stats(self) -> dict:
        rows = sorted(self.micros)
        if not rows:
            return {'ticks': 0}
        return {
            'ticks': self.ticks,
            'cars': len(self.slots),
            'mean_us': sum(rows) / len(rows),
            'p50_us': rows[len(rows) // 2],
            'p99_us': rows[min(len(rows) - 1, int(len(rows) * 0.99))],
            'max_us': rows[-1],
            'world_hash': self.host.world_hash(),
        }


def attach(sim):
    """Подвесить мир Rapier к симуляции. Возвращает ShadowWorld/RapierRace.

    Горячий путь выбирается здесь и только здесь: метод ``_step_cars``
    подменяется на экземпляре. При выключенном флаге ни эта функция, ни
    wasmtime не трогаются вовсе — ``game/sim.py`` зовёт прежний метод,
    и ни одной лишней проверки за тик не появляется.
    """
    if race_enabled():
        world = RapierRace(sim)
        sim._step_cars = world.step_cars
        # Столкновения машина-машина Rapier делает ВНУТРИ шага (§8.6
        # разведки). Прежний отдельный проход обязан замолчать: он работает
        # по состоянию, которое модуль на следующем шаге всё равно перепишет,
        # то есть только тратил бы тик и врал бы клиенту.
        sim._resolve_collisions = world.noop_collisions
        return world
    world = ShadowWorld(sim)
    world._classic = sim._step_cars
    sim._step_cars = world.step_cars
    return world
