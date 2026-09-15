# -*- coding: utf-8 -*-
"""ВЫПУЩЕНО tools/gen_abi.py из tools/abi_layout.json. Руками не править: правка переживёт ровно до первого `python3 tools/gen_abi.py --check`.

Единственное описание раскладок общей памяти модуля физики.
Из него tools/gen_abi.py выпускает native/src/abi_gen.rs, native/abi/abi_layout.py и native/abi/abi_layout.js.
Руками правится ТОЛЬКО этот файл; всё остальное — вывод генератора, и `python3 tools/gen_abi.py --check` ловит расхождение.
"""

ABI_VERSION = 1

# Максимум машин в мире. Восемь по контракту, держим запас.
MAX_CARS = 16

# Максимум подвижных предметов (конусы и прочее), чьи позы едут наружу.
MAX_PROPS = 64

# Сколько массивов по N f32 занимает залитая осевая линия: x, y, z, tx, tz, nx, nz, hw, s (12.1).
TRACK_COLUMNS = 9

# Столбцов вершин поперёк полотна: верх и низ левой стены, кромка, ось, кромка, низ и верх правой стены.
TRACK_MESH_COLS = 7


class CarInput(object):
    """Ввод одной машины.

    4 f32 = 16 байт.
    """

    SIZE = 16
    FLOATS = 4
    # газ, 0..1
    THROTTLE = 0
    # тормоз, 0..1
    BRAKE = 1
    # руль, -1..1; плюс — налево, как в разделе 4 контракта
    STEER = 2
    # ручник, 0..1
    HANDBRAKE = 3


class WheelOut(object):
    """Телеметрия одного колеса.

    8 f32 = 32 байт.
    """

    SIZE = 32
    FLOATS = 8
    # текущая длина подвески, м
    SUSPENSION_LENGTH = 0
    # ход подвески от полного отбоя, м (0 — вывешено, растёт при сжатии)
    SUSPENSION_TRAVEL = 1
    # сила подвески, Н
    SUSPENSION_FORCE = 2
    # угол поворота колеса вокруг вертикали, рад
    STEERING = 3
    # угол проворота колеса вокруг оси, рад
    ROTATION = 4
    # продольный импульс на колесе, Н·с
    FORWARD_IMPULSE = 5
    # боковой импульс на колесе, Н·с
    SIDE_IMPULSE = 6
    # 1.0 — колесо на земле, 0.0 — в воздухе
    CONTACT = 7


class CarOut(object):
    """Состояние одной машины наружу.

    52 f32 = 208 байт.
    """

    SIZE = 208
    FLOATS = 52
    # позиция центра масс, м
    PX = 0
    PY = 1
    PZ = 2
    # курс вокруг Y, рад (для совместимости с разделом 4)
    YAW = 3
    # кватернион ориентации кузова
    QX = 4
    QY = 5
    QZ = 6
    QW = 7
    # линейная скорость, м/с
    VX = 8
    VY = 9
    VZ = 10
    # модуль горизонтальной скорости, м/с
    SPEED = 11
    # угловая скорость, рад/с
    WX = 12
    WY = 13
    WZ = 14
    # угол скольжения кузова, рад
    SLIP_ANGLE = 15
    # условные обороты двигателя, об/мин
    ENGINE_RPM = 16
    # сколько колёс на земле
    WHEELS_ON_GROUND = 17
    # накопленный заряд заноса, с (аналог drift_charge из 6.3)
    DRIFT_CHARGE = 18
    # сторона заноса: +1 налево, -1 направо, 0 нет
    DRIFT_DIR = 19
    # четыре колеса: FL, FR, RL, RR
    WHEELS = 20
    WHEELS_COUNT = 4
    WHEELS_STRIDE = 8


class CarDesc(object):
    """Неизменные размеры машины: рендеру — поставить колёса, хозяину — не дублировать константы из Rust.

    8 f32 = 32 байт.
    """

    SIZE = 32
    FLOATS = 8
    HALF_WIDTH = 0
    HALF_HEIGHT = 1
    HALF_LENGTH = 2
    WHEEL_RADIUS = 3
    # вынос колеса вбок от оси кузова, м
    AXLE_X = 4
    # вынос колеса вперёд от центра, м
    AXLE_Z = 5
    # точка крепления подвески по Y в локальных координатах, м
    CONNECTION_Y = 6
    # длина подвески в покое, м
    SUSPENSION_REST = 7


class PropOut(object):
    """Поза одного подвижного предмета.

    8 f32 = 32 байт.
    """

    SIZE = 32
    FLOATS = 8
    PX = 0
    PY = 1
    PZ = 2
    # полугабариты, чтобы рендер не хранил их отдельно
    HX = 3
    QX = 4
    QY = 5
    QZ = 6
    QW = 7


class CarSave(object):
    """Полное состояние машины для отката.
    Контроллер колёс НЕ хранит физического состояния: подвеска целиком пересчитывается лучом из позы кузова на каждом шаге. Поэтому откат машины — это «поза + скорости + четыре моих скаляра», а не сериализация всего мира.

    24 f32 = 96 байт.
    """

    SIZE = 96
    FLOATS = 24
    PX = 0
    PY = 1
    PZ = 2
    QX = 3
    QY = 4
    QZ = 5
    QW = 6
    VX = 7
    VY = 8
    VZ = 9
    WX = 10
    WY = 11
    WZ = 12
    STEER = 13
    STEER_ANGLE = 14
    DRIFT_CHARGE = 15
    DRIFT_DIR = 16
    # углы проворота колёс — только для картинки
    WHEEL_ROT = 17
    WHEEL_ROT_COUNT = 4
    WHEEL_ROT_STRIDE = 1
    PAD = 21
    PAD_COUNT = 3
    PAD_STRIDE = 1


class CarTuning(object):
    """Шаблон настроек машины. Хозяин правит поля напрямую в общей памяти, следующая rp_car_spawn берёт их отсюда.

    32 f32 = 128 байт.
    """

    SIZE = 128
    FLOATS = 32
    # --- кузов ---
    # полугабариты кузова: длина/2 по Z, высота/2 по Y, ширина/2 по X
    HALF_LENGTH = 0
    HALF_HEIGHT = 1
    HALF_WIDTH = 2
    # масса кузова, кг
    MASS = 3
    # смещение центра масс вниз, м (устойчивость от переворота)
    COM_DROP = 4
    # --- подвеска ---
    SUSPENSION_REST = 5
    SUSPENSION_STIFFNESS = 6
    SUSPENSION_COMPRESSION = 7
    SUSPENSION_DAMPING = 8
    MAX_SUSPENSION_TRAVEL = 9
    MAX_SUSPENSION_FORCE = 10
    WHEEL_RADIUS = 11
    # вынос колеса от центра: по Z (вперёд) и по X (влево)
    AXLE_Z = 12
    AXLE_X = 13
    # --- сцепление ---
    FRICTION_SLIP = 14
    SIDE_FRICTION_STIFFNESS = 15
    # во сколько раз ручник роняет боковое сцепление задней оси
    HANDBRAKE_SIDE_DROP = 16
    # насколько ручник поднимает просимый доворот (аналог HANDBRAKE_TURN_GAIN)
    HANDBRAKE_TURN_GAIN = 17
    # --- двигатель и тормоза ---
    # максимальная тяга на ось, Н
    ENGINE_FORCE = 18
    # опорная скорость двигателя, м/с (тяга падает как 1 - v/max_speed)
    MAX_SPEED = 19
    BRAKE_FORCE = 20
    HANDBRAKE_FORCE = 21
    REVERSE_FORCE = 22
    # --- руль ---
    # максимальный угол поворота колёс, рад
    STEER_MAX = 23
    # скорость подхода руля к цели, 1/с
    STEER_RATE = 24
    STEER_RETURN = 25
    # во сколько раз ужимается руль на максимальной скорости
    STEER_SPEED_FALLOFF = 26
    # --- аркадная надстройка ---
    # момент доворота по рулю, Н·м на рад невязки
    YAW_ASSIST = 27
    # гашение паразитного рыскания, Н·м·с/рад
    YAW_DAMP = 28
    # прижим, Н на (м/с)^2
    DOWNFORCE = 29
    # доля боковой скорости, снимаемая за шаг (аркадное «держит»)
    LATERAL_BITE = 30
    # момент выравнивания кузова в воздухе, Н·м на рад
    AIR_RIGHTING = 31


# Общие буферы: имя, структура, сколько записей, экспорт с адресом.
BUFFERS = (
    ('INPUTS', CarInput, MAX_CARS, 'rp_inputs_ptr'),
    ('OUTPUTS', CarOut, MAX_CARS, 'rp_outputs_ptr'),
    ('DESCS', CarDesc, MAX_CARS, 'rp_descs_ptr'),
    ('PROPS', PropOut, MAX_PROPS, 'rp_props_ptr'),
    ('SAVES', CarSave, MAX_CARS, 'rp_saves_ptr'),
    ('TUNING', CarTuning, 1, 'rp_tuning_ptr'),
)
