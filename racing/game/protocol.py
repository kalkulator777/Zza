"""Бинарный сетевой протокол гонки: коды сообщений, разбор ввода, сборка снапшота.

Зеркало этого файла — static/js/protocol.js. Шпаргалка по layout ниже
продублирована в обоих файлах слово в слово: правишь здесь — правь и там.

Как пользоваться на сервере
---------------------------
Ввод от клиента (20..60 раз в секунду на клиента)::

    parsed = protocol.decode_input(data)   # data: bytes из on_message
    if parsed is None:
        return                             # битый пакет — молча игнорируем
    seq, buttons = parsed
    if protocol.has_throttle(buttons):
        ...

Снапшот (20 Гц, до восьми клиентов). Общая часть пакета одинакова для всех,
различается только поле ``ack_seq``, поэтому пакет собирается ОДИН раз за тик,
а на клиента подменяются четыре байта::

    buf = protocol.build_snapshot_base(tick, cars, projectiles, box_mask)
    for player in room.players:
        protocol.stamp_ack(buf, player.ack_seq)     # правка 4 байт по месту
        player.ws.write_message(buf, binary=True)   # Tornado копирует payload
                                                    # при сборке кадра

``build_snapshot_base`` возвращает ``bytearray``; ``stamp_ack`` меняет его
на месте и ничего не возвращает. Буфер обязан быть отправлен до следующего
``stamp_ack`` — то есть цикл рассылки должен быть синхронным, как выше
(``write_message`` формирует WebSocket-кадр и копирует данные в буфер записи
немедленно, поэтому последующая правка буфера отправленный кадр не портит).
Между тиками буфер переиспользовать нельзя: его длина зависит от числа машин,
снарядов и длины маски боксов.

``encode_snapshot(tick, ack_seq, ...)`` — удобная обёртка «собрать и сразу
проштамповать», возвращает ``bytes``. Годится для одиночных отправок и тестов,
в горячем цикле рассылки используйте пару base/stamp.

Формат записей ``cars`` и ``projectiles``
-----------------------------------------
Это последовательности кортежей строго в порядке полей пакета — никаких
атрибутов и словарей, чтобы не платить за поиск имён в горячем пути::

    car  = (slot, flags, x, z, yaw, vx, vz, lap, place, steer_q, drift_charge,
            height_q)
    proj = (proj_id, kind, x, z, yaw)
    traf = (ident, s_q, lat_q, dyaw_q, spd_q)
    evt  = (event_id, kind, phase, s_q, lat_q, hl_q, hw_q)

Целочисленные поля должны быть уже приведены к диапазону: ``steer_q`` — через
``quantize_steer``, ``drift_charge`` — через ``quantize_drift_charge``,
``flags`` — побитовое ИЛИ констант ``FLAG_*``; поля траффика и происшествий —
через ``quantize_arc``, ``quantize_lateral``, ``quantize_angle``,
``quantize_speed``, ``quantize_half_length``, ``quantize_half_width``.
``ident`` болванки — ``traffic_id | (look << 4)``.
"""

import struct

# ---------------------------------------------------------------------------
# ШПАРГАЛКА ПО ФОРМАТУ ПАКЕТОВ
# (идентична в game/protocol.py и static/js/protocol.js — правишь одно, правь второе)
#
# Всё little-endian, без выравнивания.
#
# Ввод, клиент -> сервер, 7 байт:
#   off 0   u8    type = 0x01 (MSG_INPUT)
#   off 1   u32   seq           номер шага клиента, монотонно растёт
#   off 5   u8    buttons       битовая маска кнопок
#   off 6   u8    reserved = 0
#
# Снапшот, сервер -> клиент, заголовок 10 байт:
#   off 0   u8    type = 0x10 (MSG_SNAPSHOT)
#   off 1   u32   tick          номер тика сервера
#   off 5   u32   ack_seq       ACK_OFFSET: единственное поле, зависящее от клиента
#   off 9   u8    car_count     младшие 7 бит — число машин (0..8),
#                               бит 7 (SNAPSHOT_FLAG_EXTRA) — есть ли за
#                               маской боксов секции траффика и происшествий
#   далее car_count записей по 27 байт:
#     u8 slot, u8 flags, f32 x, f32 z, f32 yaw, f32 vx, f32 vz,
#     u8 lap, u8 place, i8 steer_q (-127..127 <-> -1..1), u8 drift_charge (charge*100),
#     u8 height_q (высота над полотном в сантиметрах, 0..2.55 м; 0 — колёса на земле)
#   u8 proj_count
#   далее proj_count записей по 15 байт:
#     u16 id, u8 kind, f32 x, f32 z, f32 yaw
#   u8 box_mask_len
#   box_mask_len байт маски активных боксов:
#     бокс i -> байт i >> 3, бит i & 7 (младший бит — первый бокс)
#   --- дальше только если в car_count поднят бит 7 (FLAG_EXTRA) ---
#   u8 traffic_count
#   далее traffic_count записей по 6 байт (машины-болванки):
#     u8 ident   младшие 4 бита — id в пуле, старшие 4 — вид (силуэт и цвет)
#     u16 s_q    положение вдоль дуги трассы, s * 65535 / length
#     i8 lat_q   смещение от оси, 0.1 м на единицу (+-12.7 м)
#     i8 dyaw_q  курс МИНУС курс касательной трассы, pi/127 на единицу
#     u8 spd_q   модуль скорости, 0.25 м/с на единицу (0..63.75)
#   u8 event_count
#   далее event_count записей по 8 байт (происшествия на дороге):
#     u8 id, u8 kind, u8 phase,
#     u16 s_q    центр вдоль дуги, как у траффика
#     i8 lat_q   центр поперёк, 0.1 м
#     u8 hl_q    полудлина вдоль трассы, 0.25 м на единицу
#     u8 hw_q    полуширина поперёк трассы, 0.1 м на единицу
#
# buttons:   0 газ, 1 тормоз/задний ход, 2 влево, 3 вправо,
#            4 дрифт (ручник), 5 применить бонус, 6 взгляд назад, 7 резерв
# car flags: 0 вне трассы, 1 дрифтует, 2 ускорение, 3 крутит (урон),
#            4 щит, 5 финишировал, 6 призрак (отключился), 7 тормозит
#
# Размер снапшота = 10 + 27*car_count + 1 + 15*proj_count + 1 + box_mask_len,
# и ЕСЛИ есть траффик или происшествия, ещё + 1 + 6*traffic_count
#                                            + 1 + 8*event_count.
# Для 8 машин, 4 снарядов и маски в 2 байта без траффика и происшествий это
# 290 байт: прежние 282 плюс восемь байт высоты, по одному на машину
# (высота нужна прыжкам через трамплины — без неё чужая машина проезжала бы
# сквозь трамплин по земле). Выключенные траффик и происшествия не стоят НИ
# ОДНОГО лишнего байта, за это отвечает флаг в car_count. Плотный траффик
# (12 машин) добавляет 1 + 72 = 73 байта, шесть происшествий — ещё 1 + 48 = 49.
# Потолок наполнения: 404 байта, 8,1 КБ/с против 5,6 КБ/с у пустого.
# В DESIGN.md §5.3 в итоговой сумме стоит 265 — та сумма не сходится
# с собственным списком полей: в ней снаряд посчитан как 11 байт (потерян f32 yaw)
# и не учтён байт box_mask_len. Здесь реализован список полей, он первичен.
#
# ПОЧЕМУ ТРАФФИК НЕ ЕДЕТ ОБЫЧНОЙ ЗАПИСЬЮ МАШИНЫ (26 байт). Запись гонщика
# несёт то, чего у болванки нет и не будет: круг, место, заряд заноса, угол
# руля, вектор скорости в мировых координатах. Болванка же по построению
# держится трассы, поэтому её положение описывается дугой и смещением от оси
# точнее и вчетверо дешевле: 6 байт против 26. Двенадцать болванок стоят
# 72 байта — столько же, сколько ТРИ записи гонщиков. Потолок MAX_CARS = 8
# при этом не тронут: гонщики и траффик — разные массивы.
# ---------------------------------------------------------------------------

__all__ = [
    "MSG_INPUT", "MSG_SNAPSHOT",
    "BTN_THROTTLE", "BTN_BRAKE", "BTN_LEFT", "BTN_RIGHT",
    "BTN_DRIFT", "BTN_ITEM", "BTN_LOOK_BACK", "BTN_RESERVED",
    "FLAG_OFFTRACK", "FLAG_DRIFTING", "FLAG_BOOST", "FLAG_SPIN",
    "FLAG_SHIELD", "FLAG_FINISHED", "FLAG_GHOST", "FLAG_BRAKING",
    "INPUT_SIZE", "SNAPSHOT_HEADER_SIZE", "SNAPSHOT_CAR_SIZE",
    "SNAPSHOT_PROJ_SIZE", "ACK_OFFSET",
    "SNAPSHOT_TRAFFIC_SIZE", "SNAPSHOT_EVENT_SIZE",
    "SNAPSHOT_FLAG_EXTRA", "SNAPSHOT_CAR_COUNT_MASK",
    "MAX_CARS", "MAX_PROJECTILES", "MAX_BOX_MASK_LEN",
    "MAX_TRAFFIC", "MAX_ROAD_EVENTS", "TRAFFIC_LOOKS",
    "decode_input",
    "has_throttle", "has_brake", "has_left", "has_right",
    "has_drift", "has_item", "has_look_back",
    "is_off_track", "is_drifting", "is_boosting", "is_spinning",
    "has_shield", "has_finished", "is_ghost", "is_braking",
    "quantize_steer", "quantize_drift_charge", "quantize_height",
    "quantize_arc", "quantize_lateral", "quantize_angle",
    "quantize_speed", "quantize_half_length", "quantize_half_width",
    "pack_box_mask", "box_active",
    "build_snapshot_base", "stamp_ack", "encode_snapshot", "snapshot_size",
]

# --- коды сообщений --------------------------------------------------------

MSG_INPUT = 0x01
MSG_SNAPSHOT = 0x10

# --- маска кнопок (раздел 5.2) ---------------------------------------------

BTN_THROTTLE = 1 << 0    # газ
BTN_BRAKE = 1 << 1       # тормоз / задний ход
BTN_LEFT = 1 << 2        # влево
BTN_RIGHT = 1 << 3       # вправо
BTN_DRIFT = 1 << 4       # дрифт (ручник)
BTN_ITEM = 1 << 5        # применить бонус
BTN_LOOK_BACK = 1 << 6   # взгляд назад
BTN_RESERVED = 1 << 7    # резерв

# --- флаги машины (раздел 5.3) ---------------------------------------------

FLAG_OFFTRACK = 1 << 0   # вне трассы
FLAG_DRIFTING = 1 << 1   # дрифтует
FLAG_BOOST = 1 << 2      # ускорение активно
FLAG_SPIN = 1 << 3       # крутит (получил урон)
FLAG_SHIELD = 1 << 4     # под щитом
FLAG_FINISHED = 1 << 5   # финишировал
FLAG_GHOST = 1 << 6      # отключился, машина-призрак
FLAG_BRAKING = 1 << 7    # тормозит (стоп-сигналы)

# --- потолки, согласованные с клиентскими буферами -------------------------

MAX_CARS = 8             # мест в гонке
MAX_PROJECTILES = 32     # столько снарядов держит буфер клиента
MAX_BOX_MASK_LEN = 32    # 32 байта маски = до 256 боксов с бонусами

# Траффик и происшествия на дороге. Оба потолка выбраны так, чтобы
# идентификатор влезал в половину байта (см. поле ident в шпаргалке):
# больше шестнадцати болванок на круг — это уже пробка, а не поток.
# Бит 7 поля car_count: за маской боксов идут секции траффика и происшествий.
# Нужен ради обратной совместимости по байтам — комната с выключенными
# траффиком и происшествиями обязана слать ровно тот же пакет, что слала
# до их появления. Номер машин 0..8 умещается в четыре бита, старшие три
# свободны, поэтому отдельного байта под флаг не понадобилось.
SNAPSHOT_FLAG_EXTRA = 0x80
SNAPSHOT_CAR_COUNT_MASK = 0x7F

MAX_TRAFFIC = 12         # машин-болванок в снапшоте
TRAFFIC_LOOKS = 12       # видов болванки (силуэт + цвет), 0..15
MAX_ROAD_EVENTS = 6      # одновременных происшествий на дороге

# --- скомпилированные форматы ----------------------------------------------
# Компилируем один раз на импорт: struct.pack со строкой формата в цикле
# каждый тик обходится заметно дороже.

_INPUT = struct.Struct("<BIBB")          # type, seq, buttons, reserved
_HEADER = struct.Struct("<BIIB")         # type, tick, ack_seq, car_count
_CAR = struct.Struct("<BBfffffBBbBB")    # slot, flags, x, z, yaw, vx, vz,
                                         # lap, place, steer_q, drift_charge,
                                         # height_q
_PROJ = struct.Struct("<HBfff")          # id, kind, x, z, yaw
_TRAFFIC = struct.Struct("<BHbbB")       # ident, s_q, lat_q, dyaw_q, spd_q
_EVENT = struct.Struct("<BBBHbBB")       # id, kind, phase, s_q, lat_q, hl_q, hw_q
_ACK = struct.Struct("<I")               # только поле ack_seq

INPUT_SIZE = _INPUT.size                 # 7
SNAPSHOT_HEADER_SIZE = _HEADER.size      # 10
SNAPSHOT_CAR_SIZE = _CAR.size            # 26
SNAPSHOT_PROJ_SIZE = _PROJ.size          # 15
SNAPSHOT_TRAFFIC_SIZE = _TRAFFIC.size    # 6
SNAPSHOT_EVENT_SIZE = _EVENT.size        # 8
ACK_OFFSET = 5                           # смещение поля ack_seq в снапшоте

_STEER_SCALE = 127.0                     # -1..1 <-> -127..127


# --- ввод: клиент -> сервер ------------------------------------------------

def decode_input(data):
    """Разобрать бинарный пакет ввода.

    Возвращает кортеж ``(seq, buttons)`` или ``None``, если пакет некорректен
    (не та длина, не тот тип, вообще не bytes). Наружу не выпускает ни одного
    исключения: пакет приходит из сети и может быть каким угодно, ронять
    сервер он не должен.
    """
    try:
        if len(data) != INPUT_SIZE:
            return None
        msg_type, seq, buttons, _reserved = _INPUT.unpack_from(data, 0)
    except Exception:
        return None
    if msg_type != MSG_INPUT:
        return None
    return seq, buttons


# Хелперы кнопок. Инлайновые проверки без аллокаций — в горячем пути физики
# вызываются восемь раз за тик на каждую кнопку.

def has_throttle(buttons):
    """Нажат газ."""
    return (buttons & BTN_THROTTLE) != 0


def has_brake(buttons):
    """Нажат тормоз / задний ход."""
    return (buttons & BTN_BRAKE) != 0


def has_left(buttons):
    """Нажат руль влево."""
    return (buttons & BTN_LEFT) != 0


def has_right(buttons):
    """Нажат руль вправо."""
    return (buttons & BTN_RIGHT) != 0


def has_drift(buttons):
    """Нажат ручник."""
    return (buttons & BTN_DRIFT) != 0


def has_item(buttons):
    """Нажата кнопка применения бонуса."""
    return (buttons & BTN_ITEM) != 0


def has_look_back(buttons):
    """Удерживается взгляд назад (только визуал, на физику не влияет)."""
    return (buttons & BTN_LOOK_BACK) != 0


# --- флаги машины ----------------------------------------------------------

def is_off_track(flags):
    """Машина вне полотна трассы."""
    return (flags & FLAG_OFFTRACK) != 0


def is_drifting(flags):
    """Идёт занос."""
    return (flags & FLAG_DRIFTING) != 0


def is_boosting(flags):
    """Активно ускорение."""
    return (flags & FLAG_BOOST) != 0


def is_spinning(flags):
    """Машину крутит после урона."""
    return (flags & FLAG_SPIN) != 0


def has_shield(flags):
    """Машина под щитом."""
    return (flags & FLAG_SHIELD) != 0


def has_finished(flags):
    """Машина финишировала."""
    return (flags & FLAG_FINISHED) != 0


def is_ghost(flags):
    """Игрок отключился, машина доживает как призрак."""
    return (flags & FLAG_GHOST) != 0


def is_braking(flags):
    """Горят стоп-сигналы."""
    return (flags & FLAG_BRAKING) != 0


# --- квантование полей снапшота --------------------------------------------

def quantize_steer(steer):
    """Угол руля -1..1 -> знаковый байт -127..127 (поле ``steer_q``)."""
    q = int(steer * _STEER_SCALE + (0.5 if steer >= 0.0 else -0.5))
    if q > 127:
        return 127
    if q < -127:
        return -127
    return q


def quantize_height(height):
    """Высота над полотном в метрах -> байт сантиметров (потолок 2.55 м).

    Прыжок через трамплин выше двух с половиной метров — это уже не прыжок,
    а полёт, и в аркадной геометрии здешних трасс такого нет. Отдельный бит
    «в полёте» во флагах не заводится: все восемь заняты (раздел 5.3),
    а для рендера достаточно самой высоты — ноль означает колёса на земле.
    """
    if height <= 0.0:
        return 0
    q = int(height * 100.0 + 0.5)
    return 255 if q > 255 else q


def quantize_drift_charge(charge):
    """Заряд дрифта в секундах -> байт ``min(255, round(charge * 100))``."""
    if charge <= 0.0:
        return 0
    q = int(charge * 100.0 + 0.5)
    return 255 if q > 255 else q


# --- квантование траффика и происшествий ------------------------------------
#
# Болванка и происшествие описываются НЕ мировыми координатами, а положением
# на трассе: дуга + смещение от оси. Так они вчетверо дешевле записи гонщика
# (6 и 8 байт против 26) и при этом точнее: клиент восстанавливает точку по
# той же осевой линии, по которой её считал сервер, поэтому ошибка округления
# не уводит машину с полотна, а остаётся вдоль него.

_ARC_SCALE = 65535.0
_LATERAL_SCALE = 10.0            # 0.1 м на единицу, потолок +-12.7 м
_ANGLE_SCALE = 127.0 / 3.141592653589793
_SPEED_SCALE = 4.0               # 0.25 м/с на единицу, потолок 63.75 м/с
_HALF_LENGTH_SCALE = 4.0         # 0.25 м на единицу, потолок 63.75 м
_HALF_WIDTH_SCALE = 10.0         # 0.1 м на единицу, потолок 25.5 м


def quantize_arc(s, length):
    """Положение вдоль дуги трассы -> u16. ``length`` — длина круга, м.

    Разрешение: длина круга / 65535, то есть 3..5 см на здешних трассах.
    Значение заворачивается по модулю длины: дуга замкнута.
    """
    if length <= 0.0:
        return 0
    u = s / length
    u -= int(u)                  # дробная часть, знак сохраняется
    if u < 0.0:
        u += 1.0
    q = int(u * _ARC_SCALE + 0.5)
    if q > 65535:
        q = 65535
    elif q < 0:
        q = 0
    return q


def quantize_lateral(lateral):
    """Смещение от оси в метрах -> i8 с шагом 0.1 м (потолок +-12.7 м)."""
    q = int(lateral * _LATERAL_SCALE + (0.5 if lateral >= 0.0 else -0.5))
    if q > 127:
        return 127
    if q < -127:
        return -127
    return q


def quantize_angle(angle):
    """Угол в радианах (-pi..pi) -> i8. Шаг около 1.4 градуса."""
    q = int(angle * _ANGLE_SCALE + (0.5 if angle >= 0.0 else -0.5))
    if q > 127:
        return 127
    if q < -127:
        return -127
    return q


def quantize_speed(speed):
    """Модуль скорости, м/с -> u8 с шагом 0.25 м/с (потолок 63.75)."""
    if speed <= 0.0:
        return 0
    q = int(speed * _SPEED_SCALE + 0.5)
    return 255 if q > 255 else q


def quantize_half_length(metres):
    """Полудлина происшествия вдоль трассы -> u8 с шагом 0.25 м."""
    if metres <= 0.0:
        return 0
    q = int(metres * _HALF_LENGTH_SCALE + 0.5)
    return 255 if q > 255 else q


def quantize_half_width(metres):
    """Полуширина происшествия поперёк трассы -> u8 с шагом 0.1 м."""
    if metres <= 0.0:
        return 0
    q = int(metres * _HALF_WIDTH_SCALE + 0.5)
    return 255 if q > 255 else q


# --- маска активных боксов -------------------------------------------------

def pack_box_mask(active, buf=None):
    """Упаковать последовательность признаков активности боксов в маску.

    ``active`` — последовательность значений, приводимых к bool, по одному
    на бокс в порядке ``Track.item_boxes``. ``buf`` — необязательный
    переиспользуемый ``bytearray`` нужной длины; если передан, заполняется
    на месте и возвращается он же (ноль аллокаций в тике).
    """
    count = len(active)
    mask_len = (count + 7) >> 3
    if buf is None or len(buf) != mask_len:
        buf = bytearray(mask_len)
    else:
        for i in range(mask_len):
            buf[i] = 0
    for i in range(count):
        if active[i]:
            buf[i >> 3] |= 1 << (i & 7)
    return buf


def box_active(mask, box_id):
    """Активен ли бокс ``box_id`` по упакованной маске."""
    byte_index = box_id >> 3
    if byte_index < 0 or byte_index >= len(mask):
        return False
    return (mask[byte_index] >> (box_id & 7)) & 1 == 1


# --- снапшот: сервер -> клиент ---------------------------------------------

def snapshot_size(car_count, proj_count, box_mask_len,
                  traffic_count=0, event_count=0):
    """Размер снапшота в байтах при заданном наполнении.

    Секции траффика и происшествий появляются в пакете только когда в них
    что-то есть: комната с выключенными настройками шлёт ровно тот пакет,
    что и до их появления, без единого лишнего байта. Признак — бит
    ``SNAPSHOT_FLAG_EXTRA`` в поле ``car_count``.
    """
    size = (SNAPSHOT_HEADER_SIZE
            + car_count * SNAPSHOT_CAR_SIZE
            + 1 + proj_count * SNAPSHOT_PROJ_SIZE
            + 1 + box_mask_len)
    if traffic_count or event_count:
        size += (1 + traffic_count * SNAPSHOT_TRAFFIC_SIZE
                 + 1 + event_count * SNAPSHOT_EVENT_SIZE)
    return size


def build_snapshot_base(tick, cars, projectiles, box_mask,
                        traffic=(), events=()):
    """Собрать общую для всех клиентов часть снапшота.

    ``cars``, ``projectiles``, ``traffic`` и ``events`` — последовательности
    кортежей в порядке полей (см. докстринг модуля), ``box_mask`` —
    bytes-подобная маска активных боксов. Поле ``ack_seq`` заполняется нулём:
    его проставляет ``stamp_ack`` для каждого клиента отдельно.

    Возвращает ``bytearray`` — изменяемый буфер, который дальше штампуется
    и отправляется. Переполнение потолков (больше ``MAX_CARS`` машин,
    ``MAX_PROJECTILES`` снарядов, ``MAX_BOX_MASK_LEN`` байт маски,
    ``MAX_TRAFFIC`` болванок, ``MAX_ROAD_EVENTS`` происшествий) не является
    ошибкой: лишнее отбрасывается, потому что принять такой пакет клиент
    всё равно не сможет, а ронять тик из-за этого нельзя.
    """
    car_count = len(cars)
    if car_count > MAX_CARS:
        cars = cars[:MAX_CARS]
        car_count = MAX_CARS
    proj_count = len(projectiles)
    if proj_count > MAX_PROJECTILES:
        projectiles = projectiles[:MAX_PROJECTILES]
        proj_count = MAX_PROJECTILES
    mask_len = len(box_mask)
    if mask_len > MAX_BOX_MASK_LEN:
        box_mask = box_mask[:MAX_BOX_MASK_LEN]
        mask_len = MAX_BOX_MASK_LEN
    traffic_count = len(traffic)
    if traffic_count > MAX_TRAFFIC:
        traffic = traffic[:MAX_TRAFFIC]
        traffic_count = MAX_TRAFFIC
    event_count = len(events)
    if event_count > MAX_ROAD_EVENTS:
        events = events[:MAX_ROAD_EVENTS]
        event_count = MAX_ROAD_EVENTS

    extra = bool(traffic_count or event_count)
    buf = bytearray(snapshot_size(car_count, proj_count, mask_len,
                                  traffic_count, event_count))
    _HEADER.pack_into(buf, 0, MSG_SNAPSHOT, tick & 0xFFFFFFFF, 0,
                      car_count | SNAPSHOT_FLAG_EXTRA if extra else car_count)

    offset = SNAPSHOT_HEADER_SIZE
    pack_car = _CAR.pack_into
    for car in cars:
        pack_car(buf, offset, *car)
        offset += SNAPSHOT_CAR_SIZE

    buf[offset] = proj_count
    offset += 1
    pack_proj = _PROJ.pack_into
    for projectile in projectiles:
        pack_proj(buf, offset, *projectile)
        offset += SNAPSHOT_PROJ_SIZE

    buf[offset] = mask_len
    offset += 1
    if mask_len:
        buf[offset:offset + mask_len] = box_mask
        offset += mask_len
    if not extra:
        return buf

    buf[offset] = traffic_count
    offset += 1
    pack_traffic = _TRAFFIC.pack_into
    for car in traffic:
        pack_traffic(buf, offset, *car)
        offset += SNAPSHOT_TRAFFIC_SIZE

    buf[offset] = event_count
    offset += 1
    pack_event = _EVENT.pack_into
    for event in events:
        pack_event(buf, offset, *event)
        offset += SNAPSHOT_EVENT_SIZE
    return buf


def stamp_ack(buf, ack_seq):
    """Проставить в готовый буфер снапшота ``ack_seq`` конкретного клиента.

    Правит ровно четыре байта по смещению ``ACK_OFFSET`` на месте, ничего
    не аллоцирует и ничего не возвращает. Вызывать непосредственно перед
    отправкой этому клиенту.
    """
    _ACK.pack_into(buf, ACK_OFFSET, ack_seq & 0xFFFFFFFF)


def encode_snapshot(tick, ack_seq, cars, projectiles, box_mask,
                    traffic=(), events=()):
    """Собрать законченный снапшот для одного клиента, вернуть ``bytes``.

    Обёртка над ``build_snapshot_base`` + ``stamp_ack``. Для рассылки восьми
    клиентам используйте эту пару напрямую, чтобы не собирать пакет восемь раз.
    """
    buf = build_snapshot_base(tick, cars, projectiles, box_mask, traffic, events)
    _ACK.pack_into(buf, ACK_OFFSET, ack_seq & 0xFFFFFFFF)
    return bytes(buf)
