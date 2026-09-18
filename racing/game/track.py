# -*- coding: utf-8 -*-
"""Геометрия трассы: центростремительный Catmull-Rom, ресэмплинг по дуге,
профиль ширины и высоты, прогресс по кругу, границы полотна, стартовая решётка.

Владелец модуля: [track]. Только стандартная библиотека.

Система координат — из раздела 4 контракта: правая, Y вверх, земля — плоскость XZ,
forward = (sin yaw, cos yaw), right = (cos yaw, -sin yaw). Отсюда для касательной
(tx, tz) нормаль «вправо» равна (tz, -tx).

Все передаваемые массивы геометрии хранятся в двух видах:
  * ``_j*`` — значения, округлённые до 3 знаков, ровно они уходят в JSON клиенту;
  * ``_c*`` — те же значения, приведённые к float32, по ним идёт счёт.
Клиент кладёт присланные числа в Float32Array, то есть получает ровно ``_c*``.
Счёт на обеих сторонах идёт в float64 над одинаковыми входными числами, поэтому
nearest_index / surface / clamp_to_track / advance_progress сходятся бит в бит.

Трамплины (правка контракта к разделам 7.1 и 12.1). Поле ``ramps`` в описании
трассы — это въезд под углом, с которого машина уходит в свободный полёт
(шаг 14б в ``game/physics.py``). Формат и пределы — у констант ``RAMP_*``
ниже. Производная геометрия считается на загрузке (в том числе тангенс угла:
в шаге физики тригонометрии быть не может) и уезжает клиенту готовыми
числами в ``to_client()``. Во время гонки трамплин стоит ровно одно
обращение: ``clamp_to_track`` пишет высоту полотна трамплина под машиной
в ``state.ramp_h``, и шаг 14б читает её в том же шаге.
"""

from __future__ import annotations

import json
import math
import os
import struct

# --- константы геометрии ----------------------------------------------------

SAMPLE_STEP = 2.0          # целевой шаг ресэмплинга по дуге, м (7.2)
CHECKPOINT_COUNT = 12      # число отсечек на круг (7.4)

# Поиск ближайшей точки осевой линии.
NARROW_WINDOW = 4          # быстрое окно вокруг подсказки, точек
LOCAL_WINDOW = 16          # широкое окно вокруг подсказки, точек (±32 м)
COARSE_STRIDE = 8          # шаг грубого прохода, когда подсказка потеряна
LOST_DISTANCE = 24.0       # дальше этого подсказка считается недействительной, м
LOST_D2 = LOST_DISTANCE * LOST_DISTANCE

# Границы полотна. Контракт задаёт только WALL_BOUNCE (6.4); ширину зоны вылета
# за кромкой асфальта до жёсткой стены задаёт этот модуль.
WALL_MARGIN = 2.5          # м асфальт -> стена: тут машина уже «вне трассы»
WALL_BOUNCE = 0.35         # отражение нормальной составляющей скорости (6.4)

# Полотно как ВХОД ФИЗИКИ (§12.21). Сетку строит сам модуль Rapier, но игровых
# констант он не знает: и сервер, и браузер обязаны передать ему одни и те же
# три числа, иначе получатся две РАЗНЫЕ геометрии, обязанные совпасть. На 4b
# два из них лежали по копии в каждом хозяине; теперь они объявлены здесь
# и уезжают клиенту в race_init.track (§12.23) — копии нет, расходиться нечему.
WALL_HEIGHT = 2.0          # м, высота стенки за обочиной
TRACK_FRICTION = 1.1       # трение полотна
GRID_HALF_WIDTH = 1.1      # м, половина поперечного габарита машины —
# запас по ширине при расстановке на стартовой решётке. Это НЕ CAR_RADIUS
# из раздела 6.4: тот описывает форму столкновения (капсула 4,00 x 1,90 м)
# и живёт в physics. Раньше здесь лежала его копия, и при смене формы
# столкновений числа молча разъехались.

# Стартовая решётка: две шахматные колонны позади линии старта.
GRID_FIRST_BACK = 7.0      # отступ первой машины от линии, м
GRID_PAIR_GAP = 9.0        # шаг между парами, м
GRID_STAGGER = 4.5         # смещение второй колонны назад, м
GRID_LANE_FRACTION = 0.42  # доля половины ширины под смещение колонны от оси
GRID_LANE_MAX = 3.4        # м, дальше от оси не отходим даже на широкой трассе

ITEM_ROW_SPREAD = 0.72     # доля половины ширины, по которой раскидан ряд боксов

# --- трамплины --------------------------------------------------------------
# Описание в JSON трассы (поле `ramps`), по одному объекту на трамплин:
#
#   {"at": 0.412, "angle": 11.5, "rise": 1.25, "half_width": 4.2,
#    "offset": 0.0, "note": "..."}
#
#   at          доля круга, на которой стоит КРОМКА ВЫЛЕТА (0..1). Въезд
#               лежит ПЕРЕД ней, то есть трамплин занимает [at*L - length, at*L]
#   angle       угол въезда, градусы (3..20)
#   rise        высота кромки над полотном, м (0.2..3.0)
#   half_width  половина ширины трамплина, м
#   offset      смещение центра трамплина от оси полотна, м, плюс — влево
#               (раздел 4); необязательное, по умолчанию 0
#
# Длина въезда выводится: length = rise / tan(angle). Тангенс считается ЗДЕСЬ,
# на загрузке, и уезжает к клиенту готовым числом — в шаге физики никаких
# тригонометрий нет и быть не может (раздел 6).
RAMP_EDGE = 1.0            # м, боковой скос трамплина: высота сходит на нет
RAMP_ANGLE_LIMITS = (3.0, 20.0)     # градусы
RAMP_RISE_LIMITS = (0.2, 3.0)       # м
RAMP_HALF_WIDTH_MIN = 1.5           # м

# --- коробки трамплина под физику Rapier (§12.30) ----------------------------
#
# У классики трамплин — это поле высоты (``ramp_height``), никакой геометрии
# в мире нет. У Rapier полотно настоящее, и трамплин обязан быть настоящей
# геометрией, иначе машина проедет сквозь него. Геометрию ставит
# ``rp_add_box``: неподвижные коробки с наклоном (yaw + pitch).
#
# ПОЧЕМУ КОРОБКИ СЧИТАЮТСЯ ЗДЕСЬ, А НЕ У КАЖДОГО ХОЗЯИНА. Ровно по той же
# причине, по которой сетку полотна считает сам модуль (native/src/trackmesh.rs):
# две постройки, обязанные совпасть до бита, — это болезнь, а не решение.
# Угол коробки берётся через ``atan2``, а он на CPython (libm) и на V8
# (свой fdlibm) имеет право разойтись в последнем разряде. Поэтому числа
# коробок выводятся ОДИН раз, на сервере, округляются и уезжают клиенту в
# ``to_client()`` тем же куском, что и остальная выведенная геометрия
# трамплина. Оба хозяина скармливают модулю одни и те же байты.
#
# ПРОФИЛЬ. Коробка плоская, а профиль ``ramp_height`` — нет: вдоль трассы он
# линеен (это плоскость), а поперёк держит полную высоту в сердцевине и
# сходит на нет на скосе ``RAMP_EDGE``. Поверхность скоса — линейчатая
# (z = a*ds*u), её образующие идут ВДОЛЬ трассы, поэтому продольная полоса
# постоянной ширины — это точная плоскость. Отсюда раскладка: одна коробка
# на сердцевину плюс ``N`` продольных полос на каждый скос. Вдоль трассы
# приближения нет вовсе; поперёк скос становится лесенкой со ступенькой
# ``rise / N``.
#
# ОТКУДА N. Ступенька обязана быть такой, чтобы колесо на неё ВЪЕХАЛО, а не
# ударилось. Два физических предела, оба из настроек машины модуля:
#
#   * ход подвески ``max_suspension_travel`` — в аркаде 0,13 м (в симуляторе
#     0,20). Ступенька ниже хода гасится подвеской, не доходя до отбойника;
#   * просвет кузова ``wheel_radius + suspension_rest - 0,8*half_height`` —
#     у самой низкой машины каталога (Хэтч, аркада) 0,284 м. Ступенька ниже
#     половины просвета не достаёт до днища ни при каком крене.
#
# Берём меньший: 0,13 м. Запас до второго предела двукратный (0,284 / 0,13 =
# 2,18). Оба числа проверяются по живым настройкам в
# ``tools/test_rapier_guard.py``: если пресет поменяется, проверка покраснеет.
RAMP_STEP_MAX = 0.13       # м, потолок боковой ступеньки лесенки скоса
# Насколько коробка утоплена под полотно. Нужна не «толщина плиты», а
# гарантия: НИЗ коробки обязан быть под полотном и у кромки вылета тоже,
# иначе под нависшим краем можно проехать. Поэтому полутолщина считается от
# высоты своей полосы: hy = (h + RAMP_BURY) / 2.
RAMP_BURY = 0.5            # м, на столько низ коробки уходит под полотно
RAMP_BOX_FLOATS = 8        # hx, hy, hz, x, y, z, yaw, pitch на коробку

# Время суток, предложенное описанием трассы (§12.16). Это ТОЛЬКО умолчание:
# настоящее время суток выбирается в лобби и живёт в настройках комнаты.
TIMES_OF_DAY = ('day', 'dusk', 'night')
DEFAULT_TIME_OF_DAY = TIMES_OF_DAY[0]

# Круг засчитывается по floor(progress / length) в момент пересечения линии.
# progress в этот момент только что перешагнул кратное длине круга, и запас
# в микрометр снимает вопрос о единице в последнем разряде.
LAP_EPS = 1e-9

SPLINE_MIN_SUBDIV = 8      # минимум подотрезков на сегмент при оцифровке сплайна
SPLINE_MAX_SUBDIV = 256
SPLINE_RESOLUTION = 0.5    # целевая длина подотрезка при оцифровке, м


def _f32(value: float) -> float:
    """Приведение к float32 — ровно то, что произойдёт с числом в Float32Array."""
    return struct.unpack('<f', struct.pack('<f', value))[0]


def _quantize(values):
    """(список для JSON, список для счёта) из списка float64."""
    js = []
    calc = []
    for v in values:
        r = round(v, 3)
        if r == 0.0:
            r = 0.0            # убираем -0.0, чтобы JSON был чистым
        js.append(r)
        calc.append(_f32(r))
    return js, calc


def _smoothstep(u: float) -> float:
    """Плавный переход 0..1 с нулевой производной на концах."""
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return 1.0
    return u * u * (3.0 - 2.0 * u)


class TrackSample(object):
    """Одна точка осевой линии, структура из 7.2."""

    __slots__ = ('x', 'y', 'z', 'tangent_x', 'tangent_z',
                 'normal_x', 'normal_z', 'half_width', 's')

    def __init__(self, x, y, z, tx, tz, nx, nz, half_width, s):
        self.x = x
        self.y = y
        self.z = z
        self.tangent_x = tx
        self.tangent_z = tz
        self.normal_x = nx
        self.normal_z = nz
        self.half_width = half_width
        self.s = s

    def __repr__(self):
        return '<TrackSample s=%.1f x=%.2f z=%.2f hw=%.2f>' % (
            self.s, self.x, self.z, self.half_width)


class Track(object):
    """Производная геометрия трассы и вся работа с ней во время гонки."""

    # ------------------------------------------------------------------ load

    @classmethod
    def load(cls, path: str, mirror: bool = False) -> "Track":
        """Загрузка описания трассы из JSON.

        ``mirror`` — зеркальная трасса из настроек комнаты (раздел 9).
        Отражение делается по оси X ещё на контрольных точках, поэтому длина,
        профиль высоты, отсечки и доли ``at`` остаются прежними, а все повороты
        меняют сторону.
        """
        with open(path, 'r', encoding='utf-8') as fp:
            data = json.load(fp)
        return cls(data, mirror=mirror)

    def __init__(self, data: dict, mirror: bool = False):
        _validate(data)
        self.id = data['id']
        self.name = data['name']
        self.desc = data.get('desc', '')
        self.difficulty = int(data.get('difficulty', 1))
        self.theme = data.get('theme', 'city')
        # Умолчание времени суток из описания карты: лобби предлагает его при
        # выборе трассы, а выбранное значение приезжает в настройках комнаты.
        self.time_of_day = data.get('time_of_day', DEFAULT_TIME_OF_DAY)
        self.decor_seed = int(data.get('decor_seed', 0))
        self.width = float(data['width'])
        self.mirror = bool(mirror)
        self.source = data

        control = [(float(p[0]), float(p[1])) for p in data['control']]
        if mirror:
            control = [(-p[0], p[1]) for p in control]

        dense_x, dense_z, dense_s = _dense_polyline(control)
        total = dense_s[-1]

        count = int(round(total / SAMPLE_STEP))
        if count < 4 * CHECKPOINT_COUNT:
            count = 4 * CHECKPOINT_COUNT
        step = total / count

        px, pz = _resample(dense_x, dense_z, dense_s, count, step)

        # Сдвиг линии старта вдоль круга: просто прокручиваем массив точек.
        offset = float(data.get('start_offset', 0.0))
        shift = int(round(offset * count)) % count
        if shift:
            px = px[shift:] + px[:shift]
            pz = pz[shift:] + pz[:shift]

        self.length = round(total, 3)
        self._step = round(step, 3)
        self._inv_step = 1.0 / self._step
        self._n = count
        self._half_length = self.length * 0.5
        self._quarter_length = self.length * 0.25

        # Касательная — центральная разность по ресэмплированным точкам:
        # так она непрерывна по кругу и не зависит от разбиения сплайна.
        tx = [0.0] * count
        tz = [0.0] * count
        nx = [0.0] * count
        nz = [0.0] * count
        for i in range(count):
            a = i - 1 if i > 0 else count - 1
            b = i + 1 if i + 1 < count else 0
            dx = px[b] - px[a]
            dz = pz[b] - pz[a]
            inv = 1.0 / math.sqrt(dx * dx + dz * dz)
            tx[i] = dx * inv
            tz[i] = dz * inv
            nx[i] = tz[i]          # «вправо» относительно направления движения
            nz[i] = -tx[i]

        s_vals = [i * step for i in range(count)]
        y_vals = _elevation(data.get('elevation', ()), s_vals, total)
        hw_vals = _width_profile(self.width, data.get('width_overrides', ()),
                                 s_vals, total)

        self._jx, self._cx = _quantize(px)
        self._jy, self._cy = _quantize(y_vals)
        self._jz, self._cz = _quantize(pz)
        self._jtx, self._ctx = _quantize(tx)
        self._jtz, self._ctz = _quantize(tz)
        self._jnx, self._cnx = _quantize(nx)
        self._jnz, self._cnz = _quantize(nz)
        self._jhw, self._chw = _quantize(hw_vals)
        self._js, self._cs = _quantize(s_vals)

        # Продольный уклон в точке: считается по уже квантованным высотам,
        # чтобы клиент получил ровно те же числа. На физику не влияет (раздел 4),
        # нужен только для наклона кузова и камеры.
        # Уклон НЕ квантуется: он не передаётся клиенту, тот считает его сам
        # по тем же (уже квантованным) высотам и той же формулой. Так обе
        # стороны получают одно и то же с точностью до реализации atan.
        pitch = [0.0] * count
        inv2 = 1.0 / (2.0 * self._step)
        for i in range(count):
            a = i - 1 if i > 0 else count - 1
            b = i + 1 if i + 1 < count else 0
            pitch[i] = math.atan((self._cy[b] - self._cy[a]) * inv2)
        self._cpitch = pitch

        self.samples = [TrackSample(self._cx[i], self._cy[i], self._cz[i],
                                    self._ctx[i], self._ctz[i],
                                    self._cnx[i], self._cnz[i],
                                    self._chw[i], self._cs[i])
                        for i in range(count)]

        self.checkpoints = [int(round(k * count / CHECKPOINT_COUNT)) % count
                            for k in range(CHECKPOINT_COUNT)]
        self._cp_s = [self._cs[i] for i in self.checkpoints]

        self.item_boxes = self._build_item_boxes(data.get('item_rows', ()))
        self.start_grid = self._build_start_grid()
        self._build_ramps(data.get('ramps', ()))
        self._client = None

    # ------------------------------------------------------- служебные куски

    def _build_item_boxes(self, rows):
        """Ряды боксов с бонусами поперёк полотна."""
        boxes = []
        n = self._n
        for row in rows:
            at = float(row['at']) % 1.0
            cnt = int(row['count'])
            if cnt < 1:
                continue
            idx = int(round(at * self.length * self._inv_step)) % n
            spread = self._chw[idx] * ITEM_ROW_SPREAD
            for j in range(cnt):
                off = (2.0 * (j + 0.5) / cnt - 1.0) * spread
                boxes.append({
                    'id': len(boxes),
                    'x': round(self._cx[idx] + self._cnx[idx] * off, 3),
                    'z': round(self._cz[idx] + self._cnz[idx] * off, 3),
                    's': self._js[idx],
                })
        return boxes

    def _build_ramps(self, rows):
        """Трамплины: из описания в JSON — в плоские массивы для шага физики.

        Хранится всё, что нужно посчитать высоту полотна трамплина под
        машиной, и ничего лишнего. Массивы плоские и параллельные, а не
        список словарей: ``clamp_to_track`` зовётся 480 раз в секунду и
        обязана обходиться без разыменований по ключам и без аллокаций.

        Зеркальная трасса (``mirror``) трамплинов не трогает: доля круга и
        длина при отражении не меняются, а ``offset`` отсчитывается от оси
        полотна, и его знак вместе с нормалью отражается сам.
        """
        length = self.length
        s0 = []
        span = []
        slope = []
        off = []
        hw = []
        rise = []
        public = []
        for row in rows:
            at = float(row['at']) % 1.0
            angle = float(row['angle'])
            r = float(row['rise'])
            k = math.tan(math.radians(angle))
            span_v = r / k
            s_lip = at * length
            s_start = s_lip - span_v
            while s_start < 0.0:
                s_start += length
            o = float(row.get('offset', 0.0))
            w = float(row['half_width'])
            # округление — ровно то же число уедет клиенту в to_client(),
            # и обе стороны будут считать по одинаковым битам
            s_start = round(s_start, 6)
            span_v = round(span_v, 6)
            k = round(k, 9)
            o = round(o, 6)
            w = round(w, 6)
            r = round(r, 6)
            s0.append(s_start)
            span.append(span_v)
            slope.append(k)
            off.append(o)
            hw.append(w)
            rise.append(r)
            public.append({
                'at': round(at, 6),
                's0': s_start,
                'length': span_v,
                'slope': k,
                'offset': o,
                'half_width': w,
                'rise': r,
                # Коробки под Rapier: выведенная геометрия, как и всё
                # остальное в этой записи. Классика поле не читает вовсе.
                'boxes': self._ramp_boxes(s_start, span_v, o, w, r),
            })
        self._ramp_s0 = s0
        self._ramp_span = span
        self._ramp_slope = slope
        self._ramp_off = off
        self._ramp_hw = hw
        self._ramp_rise = rise
        self._ramp_count = len(s0)
        self._ramp_inv_edge = 1.0 / RAMP_EDGE
        self.ramps = public

    def _sample_at(self, s: float) -> tuple:
        """(x, y, z, nx, nz) на доле дуги ``s`` — ровно как их видит полотно.

        Линейная доводка между соседними точками оцифровки, то есть та же
        поверхность, которую строит из осевой линии сетка модуля
        (``native/src/trackmesh.rs``: одно кольцо на точку, высота поперёк
        полотна не меняется) и та же, которую считает ``surface()`` у
        классики. Считать иначе значило бы посадить трамплин мимо асфальта.
        """
        n = self._n
        # Шаг берётся как length / n, а НЕ как self._step: _step округлён до
        # миллиметра (2,001 против 2,000776 на ridge), а массив self._cs, по
        # которому считает ramp_height, построен на неокруглённом. На 900-й
        # точке эти два счёта расходятся на 0,20 м — и коробка уезжала бы
        # вдоль трассы на те же 0,20 м, то есть на 0,04 м по высоте.
        # Замерено: с _inv_step профиль расходился на 0,0389 м при нулевой
        # ступеньке лесенки, с length/n — на 0,0019 м.
        f = (s % self.length) * n / self.length
        i = int(f)
        if i >= n:
            i -= n
        t = f - i
        j = i + 1 if i + 1 < n else 0
        cx, cy, cz = self._cx, self._cy, self._cz
        nx, nz = self._cnx, self._cnz
        return (cx[i] + (cx[j] - cx[i]) * t,
                cy[i] + (cy[j] - cy[i]) * t,
                cz[i] + (cz[j] - cz[i]) * t,
                nx[i] + (nx[j] - nx[i]) * t,
                nz[i] + (nz[j] - nz[i]) * t)

    def _ramp_boxes(self, s0: float, span: float, off: float,
                    hw: float, rise: float) -> list:
        """Трамплин неподвижными коробками ``rp_add_box`` (§12.30).

        Возвращает плоский список по ``RAMP_BOX_FLOATS`` чисел на коробку:
        ``hx, hy, hz, x, y, z, yaw, pitch``. Числа округлены — это ровно те
        байты, которые уедут клиенту и которые оба хозяина отдадут модулю.

        Раскладка: сердцевина полной высоты шириной ``2*(hw - RAMP_EDGE)``
        плюс по ``N`` продольных полос на каждый скос, ``N`` выведено из
        ``RAMP_STEP_MAX``. Каждая полоса — ТОЧНАЯ плоскость вдоль трассы:
        её верх идёт от нуля на въезде до ``frac*rise`` на кромке вылета,
        где ``frac`` — та же доля скоса, что считает ``ramp_height`` по
        середине полосы.
        """
        ax, ay, az, _anx, _anz = self._sample_at(s0)
        bx, by, bz, _bnx, _bnz = self._sample_at(s0 + span)
        _mx, _my, _mz, mnx, mnz = self._sample_at(s0 + span * 0.5)
        # Середина ХОРДЫ, а не дуги: верх коробки — плоскость через въезд и
        # кромку, и её центр лежит на отрезке между ними.
        mx = (ax + bx) * 0.5
        mz = (az + bz) * 0.5
        dx = bx - ax
        dz = bz - az
        flat = math.hypot(dx, dz)
        if flat <= 0.0:
            return []
        yaw = math.atan2(dx, dz)
        # Полосы: (нижняя граница, верхняя граница, доля высоты).
        core = hw - RAMP_EDGE
        bands = [(-core, core, 1.0)]
        steps = int(math.ceil(rise / RAMP_STEP_MAX))
        if steps < 1:
            steps = 1
        width = RAMP_EDGE / steps
        for k in range(steps):
            # Доля берётся по СЕРЕДИНЕ полосы — то же число, что вернула бы
            # ramp_height в её центре. Так ступенька делится пополам между
            # соседями, а не набегает в одну сторону.
            frac = 1.0 - (k + 0.5) / steps
            lo = core + k * width
            bands.append((lo, lo + width, frac))
            bands.append((-lo - width, -lo, frac))
        out = []
        for lo, hi, frac in bands:
            top = frac * rise
            dy = (by + top) - ay
            length = math.hypot(flat, dy)
            pitch = -math.atan2(dy, flat)
            half_thick = (top + RAMP_BURY) * 0.5
            u = off + (lo + hi) * 0.5
            # Верх коробки обязан пройти через отрезок «въезд — кромка»,
            # поэтому центр опускается на полутолщину по НОРМАЛИ коробки,
            # а не по мировому Y: иначе наклонная плита всплывёт на въезде.
            up_y = math.cos(pitch)
            up_h = math.sin(pitch)
            cx = mx + mnx * u - half_thick * up_h * math.sin(yaw)
            cz = mz + mnz * u - half_thick * up_h * math.cos(yaw)
            cy = (ay + by + top) * 0.5 - half_thick * up_y
            out.extend((round((hi - lo) * 0.5, 6), round(half_thick, 6),
                        round(length * 0.5, 6),
                        round(cx, 6), round(cy, 6), round(cz, 6),
                        round(yaw, 9), round(pitch, 9)))
        return out

    def ramp_height(self, index: int, lateral: float, along: float) -> float:
        """Высота полотна трамплина под машиной, м. Ноль — трамплина нет.

        ``index`` / ``lateral`` / ``along`` — то, что уже посчитала
        ``clamp_to_track``: индекс ближайшей точки, смещение от оси и
        смещение вдоль касательной. Своего поиска здесь нет.

        Профиль: вдоль трассы высота растёт линейно от нуля на въезде до
        ``rise`` на кромке вылета, поперёк — держится по всей ширине и
        сходит на нет на боковом скосе ``RAMP_EDGE``. Скос нужен не для
        красоты: без него въезд сбоку давал бы мгновенную ступеньку.
        """
        length = self.length
        s = self._cs[index] + along
        best = 0.0
        k = 0
        count = self._ramp_count
        while k < count:
            ds = s - self._ramp_s0[k]
            if ds < 0.0:
                ds += length
            elif ds >= length:
                ds -= length
            if ds <= self._ramp_span[k]:
                du = lateral - self._ramp_off[k]
                if du < 0.0:
                    du = -du
                edge = self._ramp_hw[k] - du
                if edge > 0.0:
                    h = self._ramp_slope[k] * ds
                    if edge < RAMP_EDGE:
                        h *= edge * self._ramp_inv_edge
                    if h > best:
                        best = h
            k += 1
        return best

    def _build_start_grid(self):
        """Восемь мест: две шахматные колонны позади линии старта."""
        grid = []
        n = self._n
        for slot in range(8):
            back = (GRID_FIRST_BACK + (slot // 2) * GRID_PAIR_GAP
                    + (slot % 2) * GRID_STAGGER)
            s_pos = self.length - back
            while s_pos < 0.0:
                s_pos += self.length
            idx = int(round(s_pos * self._inv_step)) % n
            lane = self._chw[idx] * GRID_LANE_FRACTION
            if lane > GRID_LANE_MAX:
                lane = GRID_LANE_MAX
            # запас по ширине: машина не должна свисать за кромку
            limit = self._chw[idx] - GRID_HALF_WIDTH - 0.4
            if lane > limit:
                lane = limit
            if lane < 0.0:
                lane = 0.0
            side = 1.0 if slot % 2 == 0 else -1.0
            grid.append({
                'x': round(self._cx[idx] + self._cnx[idx] * side * lane, 3),
                'z': round(self._cz[idx] + self._cnz[idx] * side * lane, 3),
                'yaw': round(math.atan2(self._ctx[idx], self._ctz[idx]), 3),
            })
        return grid

    # ------------------------------------------------------------- геометрия

    def nearest_index(self, x: float, z: float, hint: int) -> int:
        """Локальный поиск ближайшей точки осевой линии вокруг hint.

        Три ступени: узкое окно (обычный случай, машина сместилась на доли
        точки), широкое окно и грубый проход по всей трассе. Последняя ступень
        включается только при потерянной подсказке: респаун, телепорт, старт,
        hint вне диапазона.
        """
        n = self._n
        if hint < 0 or hint >= n:
            return self._global_index(x, z)
        cx = self._cx
        cz = self._cz

        best = hint
        best_d2 = 1e30
        best_off = 0
        for off in range(-NARROW_WINDOW, NARROW_WINDOW + 1):
            j = hint + off
            if j < 0:
                j += n
            elif j >= n:
                j -= n
            dx = x - cx[j]
            dz = z - cz[j]
            d2 = dx * dx + dz * dz
            if d2 < best_d2:
                best_d2 = d2
                best = j
                best_off = off
        # минимум строго внутри окна и рядом — дальше искать нечего
        if -NARROW_WINDOW < best_off < NARROW_WINDOW and best_d2 <= LOST_D2:
            return best

        best_d2 = 1e30
        for off in range(-LOCAL_WINDOW, LOCAL_WINDOW + 1):
            j = hint + off
            if j < 0:
                j += n
            elif j >= n:
                j -= n
            dx = x - cx[j]
            dz = z - cz[j]
            d2 = dx * dx + dz * dz
            if d2 < best_d2:
                best_d2 = d2
                best = j
                best_off = off
        if best_off <= -LOCAL_WINDOW or best_off >= LOCAL_WINDOW or best_d2 > LOST_D2:
            return self._global_index(x, z)
        return best

    def _global_index(self, x: float, z: float) -> int:
        """Грубый проход по всей трассе плюс уточнение. Подсказка потеряна."""
        n = self._n
        cx = self._cx
        cz = self._cz
        best = 0
        best_d2 = 1e30
        for j in range(0, n, COARSE_STRIDE):
            dx = x - cx[j]
            dz = z - cz[j]
            d2 = dx * dx + dz * dz
            if d2 < best_d2:
                best_d2 = d2
                best = j
        coarse = best
        for off in range(-COARSE_STRIDE, COARSE_STRIDE + 1):
            j = coarse + off
            if j < 0:
                j += n
            elif j >= n:
                j -= n
            dx = x - cx[j]
            dz = z - cz[j]
            d2 = dx * dx + dz * dz
            if d2 < best_d2:
                best_d2 = d2
                best = j
        return best

    def surface(self, x: float, z: float, hint: int) -> tuple:
        """(index, lateral, half_width, y, pitch).

        lateral — знаковое смещение от оси, положительное влево (раздел 4).
        Высота, полуширина и уклон линейно доводятся до соседней точки, чтобы
        на шаге 2 м не было ступенек.
        """
        i = self.nearest_index(x, z, hint)
        dx = x - self._cx[i]
        dz = z - self._cz[i]
        lateral = dx * self._cnx[i] + dz * self._cnz[i]
        along = dx * self._ctx[i] + dz * self._ctz[i]
        n = self._n
        if along >= 0.0:
            j = i + 1 if i + 1 < n else 0
            f = along * self._inv_step
        else:
            j = i - 1 if i > 0 else n - 1
            f = -along * self._inv_step
        if f > 1.0:
            f = 1.0
        y = self._cy[i] + (self._cy[j] - self._cy[i]) * f
        half_width = self._chw[i] + (self._chw[j] - self._chw[i]) * f
        pitch = self._cpitch[i] + (self._cpitch[j] - self._cpitch[i]) * f
        return (i, lateral, half_width, y, pitch)

    def clamp_to_track(self, state, hint: int) -> None:
        """Шаг 14 физики: выталкивание из стены, гашение скорости.

        Полотно шириной ``half_width`` — асфальт. Дальше идёт зона вылета
        шириной WALL_MARGIN: там уже выставлен флаг ``offtrack`` (его читает
        шаг 11 на следующем шаге), но машина ещё едет. За ней — жёсткая стена.
        Обновляет: x, z, vx, vz, sample_idx, offtrack, ramp_h.

        В полёте (``state.airborne``) границы не действуют: машина летит НАД
        ними, выталкивать её неоткуда. Флаг ``offtrack`` при этом снят —
        травы под колёсами нет, пока колёс на земле нет. Приземление за
        кромкой наказывается обычным путём: на следующем шаге машина уже
        на земле, и всё это заработает как всегда.
        """
        i, lateral, half_width, _y, _pitch = self.surface(state.x, state.z, hint)
        state.sample_idx = i
        # Высота полотна трамплина под машиной — для шага 14б. Считается до
        # выталкивания: трамплин лежит на асфальте, а выталкивание случается
        # только за зоной вылета, то есть заведомо мимо любого трамплина.
        if self._ramp_count:
            dx = state.x - self._cx[i]
            dz = state.z - self._cz[i]
            along = dx * self._ctx[i] + dz * self._ctz[i]
            state.ramp_h = self.ramp_height(i, lateral, along)
        else:
            state.ramp_h = 0.0
        if state.airborne:
            state.offtrack = False
            return
        state.offtrack = lateral > half_width or lateral < -half_width
        limit = half_width + WALL_MARGIN
        over = 0.0
        if lateral > limit:
            over = lateral - limit
        elif lateral < -limit:
            over = lateral + limit
        if over == 0.0:
            return
        nx = self._cnx[i]
        nz = self._cnz[i]
        state.x -= nx * over
        state.z -= nz * over
        vn = state.vx * nx + state.vz * nz
        # гасим только составляющую, направленную наружу
        if (over > 0.0 and vn > 0.0) or (over < 0.0 and vn < 0.0):
            k = vn * (1.0 + WALL_BOUNCE)
            state.vx -= nx * k
            state.vz -= nz * k

    def advance_progress(self, state, hint: int) -> None:
        """Шаг 16 физики: накопление пути, отсечки, круги (раздел 7.4).

        Обновляет: sample_idx, progress, checkpoint, lap. Больше полей у
        ``CarState`` нет — ``__slots__``, поэтому предыдущее положение по дуге
        не хранится отдельно, а берётся из самого ``progress``: по построению
        ``progress`` всегда равен ``s + круги * length``, значит остаток от
        деления по модулю длины и есть прошлое ``s``.

        Скачок больше четверти круга не засчитывается (телепорт или шум), но
        точка отсчёта пересинхронизируется — назад, никогда вперёд. Так машина
        после респауна не остаётся навсегда с гигантским ds, а прыжок через
        газон не приносит ни метра прогресса, только потерю круга.
        """
        n = self._n
        i = self.nearest_index(state.x, state.z, hint)
        state.sample_idx = i
        dx = state.x - self._cx[i]
        dz = state.z - self._cz[i]
        along = dx * self._ctx[i] + dz * self._ctz[i]
        step = self._step
        if along > step:
            along = step
        elif along < -step:
            along = -step
        length = self.length
        s_new = self._cs[i] + along
        if s_new >= length:
            s_new -= length
        elif s_new < 0.0:
            s_new += length

        progress = state.progress
        laps_done = math.floor(progress / length)
        last = progress - laps_done * length      # прошлое s, без оператора %

        ds = s_new - last
        if ds > self._half_length:
            ds -= length
        elif ds < -self._half_length:
            ds += length

        if ds > self._quarter_length or ds < -self._quarter_length:
            # Телепорт: путь не засчитываем. Точку отсчёта подтягиваем к новому
            # положению, иначе машина навсегда осталась бы с гигантским ds,
            # но НИКОГДА вперёд: прыжок через газон обязан быть невыгодным.
            target = s_new + laps_done * length
            if target > progress:
                target -= length
            state.progress = target
            return

        progress += ds
        state.progress = progress
        cp_s = self._cp_s
        cp = state.checkpoint

        if ds > 0.0:
            remaining = ds
            for _ in range(CHECKPOINT_COUNT):
                target = cp_s[cp]
                gap = target - last
                if gap < 0.0:
                    gap += length
                if gap > remaining:
                    break
                last = target
                remaining -= gap
                if cp == 0:
                    # линия старта пересечена после всех одиннадцати отсечек:
                    # круг можно засчитывать. Счётчик берём из самого пути,
                    # он уже перешагнул через кратное длине круга.
                    # LAP_EPS гасит единицу в последнем разряде на самой границе.
                    state.lap = int(math.floor(progress / length + LAP_EPS))
                cp = cp + 1 if cp + 1 < CHECKPOINT_COUNT else 0
        elif ds < 0.0:
            remaining = -ds
            for _ in range(CHECKPOINT_COUNT):
                prev_cp = cp - 1 if cp > 0 else CHECKPOINT_COUNT - 1
                target = cp_s[prev_cp]
                gap = last - target
                if gap < 0.0:
                    gap += length
                if gap > remaining:
                    break
                last = target
                remaining -= gap
                cp = prev_cp
                if prev_cp == 0 and state.lap > 0:
                    # откат через линию старта задним ходом
                    state.lap -= 1

        state.checkpoint = cp

    def init_state(self, state) -> None:
        """Первичная привязка машины к трассе: стартовая решётка, начало гонки.

        Поля progress / checkpoint / lap / sample_idx / offtrack принадлежат
        этому модулю. Решётка стоит позади линии, поэтому progress отрицателен:
        пересечение линии даёт ровно ноль, а floor(progress / length) совпадает
        со счётчиком кругов по отсечкам.

        Для респауна по ходу гонки это звать НЕ надо: круги обнулятся.
        Достаточно поправить x/z — advance_progress сам увидит скачок, не
        засчитает его и пересинхронизируется.
        """
        i = self._global_index(state.x, state.z)
        dx = state.x - self._cx[i]
        dz = state.z - self._cz[i]
        along = dx * self._ctx[i] + dz * self._ctz[i]
        step = self._step
        if along > step:
            along = step
        elif along < -step:
            along = -step
        s_pos = self._cs[i] + along
        if s_pos >= self.length:
            s_pos -= self.length
        elif s_pos < 0.0:
            s_pos += self.length
        state.sample_idx = i
        state.progress = s_pos - self.length if s_pos > self._half_length else s_pos
        state.checkpoint = 0
        state.lap = 0
        state.offtrack = False

    # ------------------------------------------------------------ сериализация

    def to_client(self) -> dict:
        """Формат 12.1: плоские массивы, числа округлены до 3 знаков."""
        if self._client is None:
            self._client = {
                'id': self.id,
                'name': self.name,
                'theme': self.theme,
                # Умолчание окружения карты: рендер берёт его, только если
                # комната по какой-то причине ничего не выбрала (§12.16).
                'time_of_day': self.time_of_day,
                'length': self.length,
                'sample_step': self._step,
                'count': self._n,
                'x': self._jx, 'y': self._jy, 'z': self._jz,
                'tx': self._jtx, 'tz': self._jtz,
                'nx': self._jnx, 'nz': self._jnz,
                'hw': self._jhw,
                's': self._js,
                'checkpoints': self.checkpoints,
                'item_boxes': self.item_boxes,
                'start_grid': self.start_grid,
                # Трамплины: уже выведенная геометрия, а не исходное описание.
                # Клиенту нужны ровно те же числа, по которым считает сервер,
                # а тангенс угла на двух реализациях мог бы разойтись
                # в последнем разряде — поэтому он считается один раз здесь.
                'ramps': self.ramps,
                'decor_seed': self.decor_seed,
                # Вход сетки полотна для модуля физики (§12.23). Клиент
                # строит её теми же числами, что и сервер, потому что берёт
                # их отсюда, а не из своей копии.
                'wall_margin': WALL_MARGIN,
                'wall_height': WALL_HEIGHT,
                'friction': TRACK_FRICTION,
            }
        return self._client

    def preview_path(self, points: int = 64) -> list:
        """Контур трассы для меню: нормированная в [0, 1] ломаная [[x, y], ...].

        Ось Y в превью — экранная: z растёт вниз, север сверху.
        """
        n = self._n
        if points > n:
            points = n
        xs = []
        zs = []
        for k in range(points):
            i = int(round(k * n / points)) % n
            xs.append(self._cx[i])
            zs.append(self._cz[i])
        min_x = min(xs)
        min_z = min(zs)
        span = max(max(xs) - min_x, max(zs) - min_z)
        if span <= 0.0:
            span = 1.0
        inv = 1.0 / span
        return [[round((xs[k] - min_x) * inv, 4),
                 round((zs[k] - min_z) * inv, 4)] for k in range(points)]

    def __repr__(self):
        return '<Track %s %.1f m, %d samples%s>' % (
            self.id, self.length, self._n, ', mirror' if self.mirror else '')


# --- построение сплайна -----------------------------------------------------

def _dense_polyline(control):
    """Оцифровка замкнутого центростремительного Catmull-Rom (alpha = 0.5).

    Возвращает (xs, zs, cumulative_s); последняя точка совпадает с первой,
    cumulative_s[-1] — длина круга.
    """
    m = len(control)
    xs = []
    zs = []
    for i in range(m):
        p0 = control[(i - 1) % m]
        p1 = control[i]
        p2 = control[(i + 1) % m]
        p3 = control[(i + 2) % m]
        t0 = 0.0
        t1 = t0 + _knot(p0, p1)
        t2 = t1 + _knot(p1, p2)
        t3 = t2 + _knot(p2, p3)
        chord = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        sub = int(chord / SPLINE_RESOLUTION)
        if sub < SPLINE_MIN_SUBDIV:
            sub = SPLINE_MIN_SUBDIV
        elif sub > SPLINE_MAX_SUBDIV:
            sub = SPLINE_MAX_SUBDIV
        span = t2 - t1
        for k in range(sub):
            t = t1 + span * (k / sub)
            x, z = _catmull_rom(p0, p1, p2, p3, t0, t1, t2, t3, t)
            xs.append(x)
            zs.append(z)
    xs.append(xs[0])
    zs.append(zs[0])
    acc = [0.0] * len(xs)
    total = 0.0
    for i in range(1, len(xs)):
        total += math.hypot(xs[i] - xs[i - 1], zs[i] - zs[i - 1])
        acc[i] = total
    return xs, zs, acc


def _knot(a, b) -> float:
    """Центростремительный узел: |P1 - P0| ** alpha при alpha = 0.5."""
    d = math.hypot(b[0] - a[0], b[1] - a[1])
    if d < 1e-9:
        raise ValueError('контрольные точки трассы совпадают: %r и %r' % (a, b))
    return math.sqrt(d)


def _catmull_rom(p0, p1, p2, p3, t0, t1, t2, t3, t):
    """Пирамида Барри — Голдмена: то же, что классическая форма, но без
    вырождения при неравномерных узлах."""
    d10 = t1 - t0
    d21 = t2 - t1
    d32 = t3 - t2
    d20 = t2 - t0
    d31 = t3 - t1
    a1x = ((t1 - t) * p0[0] + (t - t0) * p1[0]) / d10
    a1z = ((t1 - t) * p0[1] + (t - t0) * p1[1]) / d10
    a2x = ((t2 - t) * p1[0] + (t - t1) * p2[0]) / d21
    a2z = ((t2 - t) * p1[1] + (t - t1) * p2[1]) / d21
    a3x = ((t3 - t) * p2[0] + (t - t2) * p3[0]) / d32
    a3z = ((t3 - t) * p2[1] + (t - t2) * p3[1]) / d32
    b1x = ((t2 - t) * a1x + (t - t0) * a2x) / d20
    b1z = ((t2 - t) * a1z + (t - t0) * a2z) / d20
    b2x = ((t3 - t) * a2x + (t - t1) * a3x) / d31
    b2z = ((t3 - t) * a2z + (t - t1) * a3z) / d31
    cx = ((t2 - t) * b1x + (t - t1) * b2x) / d21
    cz = ((t2 - t) * b1z + (t - t1) * b2z) / d21
    return cx, cz


def _resample(xs, zs, acc, count, step):
    """Равномерная по дуге выборка из оцифрованной ломаной."""
    px = [0.0] * count
    pz = [0.0] * count
    seg = 0
    last = len(acc) - 1
    for i in range(count):
        target = i * step
        while seg < last - 1 and acc[seg + 1] < target:
            seg += 1
        span = acc[seg + 1] - acc[seg]
        f = 0.0 if span <= 0.0 else (target - acc[seg]) / span
        px[i] = xs[seg] + (xs[seg + 1] - xs[seg]) * f
        pz[i] = zs[seg] + (zs[seg + 1] - zs[seg]) * f
    return px, pz


def _elevation(harmonics, s_vals, length):
    """y(s) = sum amp_i * sin(2*pi*harmonic_i*s/L + phase_i) — только гармоники,
    поэтому круг сходится по высоте без шва (7.1)."""
    out = [0.0] * len(s_vals)
    if not harmonics:
        return out
    base = 2.0 * math.pi / length
    for h in harmonics:
        amp = float(h['amp'])
        w = base * float(h['harmonic'])
        phase = float(h.get('phase', 0.0))
        for i, s in enumerate(s_vals):
            out[i] += amp * math.sin(w * s + phase)
    return out


def _width_profile(base_width, overrides, s_vals, length):
    """Половина ширины в каждой точке с плавными переходами.

    Переопределение действует в точке ``at`` (доля круга) и растворяется в
    базовой ширине за ``blend`` долей круга в обе стороны. Несколько
    переопределений применяются по очереди, поэтому соседние участки с одной и
    той же шириной образуют плато.
    """
    half = base_width * 0.5
    out = [half] * len(s_vals)
    if not overrides:
        return out
    prepared = []
    for ov in overrides:
        at = float(ov['at']) % 1.0
        target = float(ov['width']) * 0.5
        blend = float(ov.get('blend', 0.05))
        if blend <= 0.0:
            blend = 1e-6
        prepared.append((at, target, blend))
    for i, s in enumerate(s_vals):
        frac = s / length
        w = half
        for at, target, blend in prepared:
            d = abs(frac - at)
            if d > 0.5:
                d = 1.0 - d
            if d >= blend:
                continue
            w += (target - w) * _smoothstep(1.0 - d / blend)
        out[i] = w
    return out


# --- валидация и каталог ----------------------------------------------------

_REQUIRED_FIELDS = ('id', 'name', 'width', 'control')


def _validate(data):
    """Проверка описания трассы: понятная ошибка вместо падения в математике."""
    if not isinstance(data, dict):
        raise ValueError('описание трассы должно быть объектом JSON')
    for field in _REQUIRED_FIELDS:
        if field not in data:
            raise ValueError('в описании трассы нет поля %r' % field)
    control = data['control']
    if not isinstance(control, list) or len(control) < 4:
        raise ValueError('control: нужен список минимум из 4 точек')
    for p in control:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError('control: точка должна быть парой [x, z], а не %r' % (p,))
    tod = data.get('time_of_day', DEFAULT_TIME_OF_DAY)
    if tod not in TIMES_OF_DAY:
        raise ValueError('time_of_day: ожидалось одно из %s, получено %r'
                         % ('/'.join(TIMES_OF_DAY), tod))
    width = float(data['width'])
    if width < 5.0 or width > 40.0:
        raise ValueError('width: ожидалась полная ширина 5..40 м, получено %r' % width)
    for ov in data.get('width_overrides', ()):
        w = float(ov['width'])
        if w < 4.0 or w > 40.0:
            raise ValueError('width_overrides: ширина %r вне 4..40 м' % w)
        if not 0.0 <= float(ov['at']) <= 1.0:
            raise ValueError('width_overrides: at вне 0..1')
    for h in data.get('elevation', ()):
        if int(h['harmonic']) < 1:
            raise ValueError('elevation: harmonic должен быть >= 1')
    for row in data.get('item_rows', ()):
        if not 0.0 <= float(row['at']) <= 1.0:
            raise ValueError('item_rows: at вне 0..1')
        if not 1 <= int(row['count']) <= 9:
            raise ValueError('item_rows: count вне 1..9')
    half_full = width * 0.5
    for row in data.get('ramps', ()):
        for field in ('at', 'angle', 'rise', 'half_width'):
            if field not in row:
                raise ValueError('ramps: нет поля %r' % field)
        if not 0.0 <= float(row['at']) <= 1.0:
            raise ValueError('ramps: at вне 0..1')
        angle = float(row['angle'])
        if not RAMP_ANGLE_LIMITS[0] <= angle <= RAMP_ANGLE_LIMITS[1]:
            raise ValueError('ramps: angle %r вне %g..%g градусов'
                             % (angle, RAMP_ANGLE_LIMITS[0], RAMP_ANGLE_LIMITS[1]))
        rise = float(row['rise'])
        if not RAMP_RISE_LIMITS[0] <= rise <= RAMP_RISE_LIMITS[1]:
            raise ValueError('ramps: rise %r вне %g..%g м'
                             % (rise, RAMP_RISE_LIMITS[0], RAMP_RISE_LIMITS[1]))
        hw = float(row['half_width'])
        if hw < RAMP_HALF_WIDTH_MIN:
            raise ValueError('ramps: half_width %r меньше %g м'
                             % (hw, RAMP_HALF_WIDTH_MIN))
        # Трамплин обязан помещаться в полотно: улететь с него мимо трассы
        # игрок должен по своей вине, а не потому, что он так построен.
        # Проверка идёт по НОМИНАЛЬНОЙ ширине; сужения width_overrides
        # проверяются уже на геометрии, в тесте трассы.
        if abs(float(row.get('offset', 0.0))) + hw > half_full:
            raise ValueError('ramps: трамплин шире полотна (offset %r + '
                             'half_width %r > %g м)'
                             % (row.get('offset', 0.0), hw, half_full))


_CATALOG_CACHE = {}


def catalog(tracks_dir: str) -> list:
    """Список трасс для welcome.content.tracks (раздел 9).

    Поле ``preview`` — нормированный контур трассы, чтобы меню могло нарисовать
    её форму, не загружая всю геометрию. Результат кэшируется: описания трасс
    во время работы сервера не меняются.
    """
    key = os.path.abspath(tracks_dir)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached
    out = []
    for name in sorted(os.listdir(key)):
        if not name.endswith('.json'):
            continue
        track = Track.load(os.path.join(key, name))
        out.append({
            'id': track.id,
            'name': track.name,
            'desc': track.desc,
            'difficulty': track.difficulty,
            'theme': track.theme,
            'time_of_day': track.time_of_day,
            'preview': track.preview_path(),
        })
    out.sort(key=lambda item: (item['difficulty'], item['id']))
    _CATALOG_CACHE[key] = out
    return out
