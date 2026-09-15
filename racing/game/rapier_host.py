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
  тика и сверять хэш мира на живой трассе, ничем не рискуя в гонке.

Смена физики на настоящую — этап 4c, здесь её нет намеренно.
"""

from __future__ import annotations

import ctypes
import importlib.util
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
BACKENDS = (CLASSIC, SHADOW)


def backend() -> str:
    """Какая физика выбрана. Читается в холодном пути, раз за гонку."""
    name = (os.environ.get(ENV_VAR) or CLASSIC).strip().lower()
    return name if name in BACKENDS else CLASSIC


def enabled() -> bool:
    return backend() != CLASSIC


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
# констант не знает, поэтому оба хозяина ОБЯЗАНЫ передать одно и то же:
# WALL_MARGIN берётся из game/track.py, а static/js/rapier_host.js берёт его
# из static/js/track.js — это одно число контракта, а не два.
from .track import WALL_MARGIN                        # noqa: E402  (после _load_abi)

WALL_HEIGHT = 2.0      # высота стенки за обочиной, м (§12.21)
TRACK_FRICTION = 1.1   # трение полотна, как в стенде разведки


class HostError(RuntimeError):
    """Модуль не тот, ABI не тот, рантайма нет — всё сюда."""


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

    def build_track(self, track, wall_margin: float = WALL_MARGIN,
                    wall_height: float = WALL_HEIGHT,
                    friction: float = TRACK_FRICTION) -> None:
        """Залить осевую линию и попросить модуль построить полотно.

        ``track`` — либо ``game.track.Track``, либо уже готовый словарь
        ``Track.to_client()``. Числа в обоих случаях одни и те же: сервер
        хранит квантованные ``_f32(round(v, 3))``, а клиент получает те же
        ``round(v, 3)`` и кладёт их в ``Float32Array``. Это и есть общий
        вход физики из §12.21 — не картинка, а данные.
        """
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


class ShadowWorld(object):
    """Мир Rapier, идущий рядом с гонкой на тех же вводах.

    Состояние машин из него НЕ берётся: гонку по-прежнему считает
    ``game/physics.py``. Смысл теневого режима — мерить цену тика и сверять
    хэш мира на живой трассе, ничем в гонке не рискуя.
    """

    def __init__(self, sim, wasm_path: str = WASM_PATH):
        self.sim = sim
        self.host = RapierHost(wasm_path)
        self.host.build_track(sim.track)
        self.host.free_track_mesh()
        self.slots = []
        for car in sim.cars:
            state = car.state
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


def attach(sim):
    """Подвесить теневой мир к симуляции. Возвращает ShadowWorld или None.

    Горячий путь выбирается здесь и только здесь: метод ``_step_cars``
    подменяется на экземпляре. При выключенном флаге ни эта функция, ни
    wasmtime не трогаются вовсе — ``game/sim.py`` зовёт прежний метод,
    и ни одной лишней проверки за тик не появляется.
    """
    world = ShadowWorld(sim)
    world._classic = sim._step_cars
    sim._step_cars = world.step_cars
    return world
