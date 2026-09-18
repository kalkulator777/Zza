# -*- coding: utf-8 -*-
"""Туман войны — ОДИН на команду (DESIGN.md 4.4, 11.2).

Клетка видна, если её видит хоть кто-то из живых игроков. Персонального
обзора нет и быть не должно: снапшот сериализуется один раз на комнату, и
персональный туман умножил бы стоимость сериализации на число игроков
(11.2). Здесь та же экономия: обзор считается один на комнату, дельта
тумана — одна строка на комнату.

Три состояния клетки:

    0 VIS_DARK  — не открыта, клиент не знает о ней ничего;
    1 VIS_SEEN  — открыта, но сейчас никто не смотрит (память группы);
    2 VIS_LIT   — видна прямо сейчас хотя бы одному живому игроку.

В дельте добавляется 255 VIS_KEEP — «клетка не менялась».

Обзор — рекурсивный shadowcasting по восьми октантам, а не круг по
радиусу: за стеной не видно, и именно это делает игру игрой про темноту
(8.2, 11.4). Круг по радиусу тут неуместен принципиально — с ним видно
сквозь стену, и вся разведка обесценивается.

Две экономии, без которых тик 2.2 не удержать:

* **сдвинулся меньше чем на клетку — обзор не пересчитывается.** Обзор
  зависит только от целой клетки, в которой стоит игрок. При беге 5 кл/с
  и тике 30 Гц игрок меняет клетку раз в 6 тиков, то есть 5 из 6 тиков
  обзор не считается вообще;
* **пересчитался — обновляется только разница.** Новый обзор сравнивается
  со старым множеством клеток (`set` — операция на C), и счётчики
  видимости правятся только на симметрической разности. При шаге на одну
  клетку это десятки клеток вместо трёхсот.

Дельта тумана кодируется RLE ровно так же, как это делает
`proto.rle_encode` для полного массива с 255 на месте неизменившихся
клеток — но собирается сразу из списка изменений, за O(изменений), а не
за O(ширина*высота). Побайтовое совпадение с `proto.rle_encode`
проверяется в `tests/vis_check.py`.
"""

import base64

from . import proto

VIS_DARK = 0
VIS_SEEN = 1
VIS_LIT = 2
VIS_KEEP = 255      # только в дельте: клетка не изменилась

# Радиус обзора в клетках. Откуда 10: при 48 px/клетка и 1920x1080 в кадре
# 40x22.5 клеток (4.1), то есть круг диаметром 20 клеток заведомо помещается
# по вертикали экрана и не помещается по горизонтали — темнота остаётся
# видна как темнота, а комната (4..11 клеток) освещается целиком.
# Стоимость растёт как радиус в квадрате, так что это и есть ручка бюджета.
VIEW_RADIUS = 10

# восемь октантов: множители перехода (dx,dy) -> (X,Y)
_MULT = (
    (1, 0, 0, -1, -1, 0, 0, 1),
    (0, 1, -1, 0, 0, -1, 1, 0),
    (0, 1, 1, 0, 0, -1, -1, 0),
    (1, 0, 0, 1, -1, 0, 0, -1),
)

_EMPTY = frozenset()


def _octant(tiles, w, h, cx, cy, radius, r2, xx, xy, yx, yy, out,
            row, start, end):
    """Рекурсивный shadowcasting по одному октанту."""
    if start < end:
        return
    new_start = 0.0
    for j in range(row, radius + 1):
        dx = -j - 1
        dy = -j
        blocked = False
        while dx <= 0:
            dx += 1
            X = cx + dx * xx + dy * xy
            Y = cy + dx * yx + dy * yy
            l_slope = (dx - 0.5) / (dy + 0.5)
            r_slope = (dx + 0.5) / (dy - 0.5)
            if start < r_slope:
                continue
            if end > l_slope:
                break
            inside = 0 <= X < w and 0 <= Y < h
            i = Y * w + X
            if inside and dx * dx + dy * dy <= r2:
                out.add(i)
            solid = (not inside) or tiles[i] == 0
            if blocked:
                if solid:
                    new_start = r_slope
                    continue
                blocked = False
                start = new_start
            elif solid and j < radius:
                blocked = True
                _octant(tiles, w, h, cx, cy, radius, r2, xx, xy, yx, yy,
                        out, j + 1, start, l_slope)
                new_start = r_slope
        if blocked:
            break


def field_of_view(grid, tx, ty, radius=VIEW_RADIUS):
    """Множество индексов клеток, видимых из (tx,ty). Стены перекрывают."""
    w = grid.w
    h = grid.h
    tiles = grid.tiles
    out = set()
    if not (0 <= tx < w and 0 <= ty < h):
        return out
    out.add(ty * w + tx)
    r2 = radius * radius
    m0, m1, m2, m3 = _MULT
    for oct_i in range(8):
        _octant(tiles, w, h, tx, ty, radius, r2,
                m0[oct_i], m1[oct_i], m2[oct_i], m3[oct_i],
                out, 1, 1.0, 0.0)
    return out


def _emit(out, v, length):
    """Пара (значение, длина) с той же нарезкой по 255, что в proto.rle_encode."""
    while length > 255:
        out.append(v)
        out.append(255)
        length -= 255
    out.append(v)
    out.append(length)


class Fog(object):
    """Туман одной комнаты. Владелец — World, потребитель — снапшот."""

    __slots__ = ("grid", "radius", "n", "state", "_sent", "_count",
                 "_fov", "_at", "_dirty", "casts", "updates")

    def __init__(self, grid, radius=VIEW_RADIUS):
        self.grid = grid
        self.radius = radius
        self.n = grid.w * grid.h
        self.state = bytearray(self.n)        # VIS_DARK везде
        self._sent = bytearray(self.n)        # что уже ушло клиентам
        self._count = bytearray(self.n)       # сколько игроков видят клетку
        self._fov = {}                        # ключ игрока -> set индексов
        self._at = {}                         # ключ игрока -> (tx,ty)
        self._dirty = set()                   # индексы, тронутые с прошлой дельты
        self.casts = 0                        # счётчик пересчётов обзора
        self.updates = 0                      # счётчик вызовов update

    # --- обновление --------------------------------------------------------

    def _apply(self, key, new):
        """Заменить вклад игрока в общий обзор, тронув только разницу."""
        old = self._fov.get(key, _EMPTY)
        if old == new:
            return
        self._fov[key] = new
        cnt = self._count
        st = self.state
        dirty = self._dirty
        for i in old - new:
            c = cnt[i] - 1
            cnt[i] = c
            if c == 0:
                st[i] = VIS_SEEN            # запомнили, но больше не видим
                dirty.add(i)
        for i in new - old:
            c = cnt[i] + 1
            cnt[i] = c
            if c == 1 and st[i] != VIS_LIT:
                st[i] = VIS_LIT
                dirty.add(i)

    def update(self, viewers):
        """viewers — последовательность (ключ, x, y) живых игроков.

        Вызывается каждый тик, но работу делает только когда кто-то
        перешёл в другую клетку.
        """
        self.updates += 1
        at = self._at
        n = 0
        for key, x, y in viewers:
            n += 1
            tx = int(x)
            ty = int(y)
            prev = at.get(key)
            if prev is not None and prev[0] == tx and prev[1] == ty:
                continue                     # меньше клетки — обзор тот же
            at[key] = (tx, ty)
            self.casts += 1
            self._apply(key, field_of_view(self.grid, tx, ty, self.radius))
        if len(at) != n:
            # кто-то умер, вышел или сменил этаж — снять его вклад
            live = set()
            for key, x, y in viewers:
                live.add(key)
            for key in list(at.keys()):
                if key not in live:
                    del at[key]
                    self._apply(key, _EMPTY)
                    self._fov.pop(key, None)

    def forget_all(self):
        """Смена этажа: обзор с нуля, память группы сбрасывается."""
        self.state = bytearray(self.n)
        self._sent = bytearray(self.n)
        self._count = bytearray(self.n)
        self._fov = {}
        self._at = {}
        self._dirty = set()

    # --- чтение ------------------------------------------------------------

    def at(self, tx, ty):
        if 0 <= tx < self.grid.w and 0 <= ty < self.grid.h:
            return self.state[ty * self.grid.w + tx]
        return VIS_DARK

    def is_lit(self, tx, ty):
        return self.at(tx, ty) == VIS_LIT

    def lit_count(self):
        return self.state.count(VIS_LIT)

    def seen_count(self):
        return self.n - self.state.count(VIS_DARK)

    # --- на провод (5.2, поле vis) -----------------------------------------

    def full_encoded(self):
        """Полное состояние тумана. Для входящего и для смены этажа.

        Чистое чтение: учёт «что клиенты уже знают» ведёт только дельта.
        Иначе полный снапшот одному вошедшему съедал бы изменения у всех
        остальных — они получили бы дельту без этих клеток.
        """
        return proto.rle_encode(bytes(self.state))

    def delta_encoded(self):
        """Изменения с прошлого раза или None, если ничего не менялось."""
        dirty = self._dirty
        if not dirty:
            return None
        st = self.state
        sent = self._sent
        changes = sorted(i for i in dirty if st[i] != sent[i])
        dirty.clear()
        if not changes:
            return None
        out = bytearray()
        pos = 0
        k = 0
        m = len(changes)
        while k < m:
            i = changes[k]
            if i > pos:
                _emit(out, VIS_KEEP, i - pos)
            v = st[i]
            j = k + 1
            while j < m and changes[j] == changes[j - 1] + 1 and st[changes[j]] == v:
                j += 1
            last = changes[j - 1]
            _emit(out, v, last - i + 1)
            for q in range(k, j):
                sent[changes[q]] = v
            pos = last + 1
            k = j
        if pos < self.n:
            _emit(out, VIS_KEEP, self.n - pos)
        return base64.b64encode(bytes(out)).decode("ascii")
