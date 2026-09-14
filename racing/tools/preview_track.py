#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Просмотр трассы: сводка в консоль и вид сверху в SVG.

    python3 tools/preview_track.py office
    python3 tools/preview_track.py                # все трассы сразу
    python3 tools/preview_track.py serpentine --mirror --out /tmp

SVG пишется руками в текст, никаких библиотек. Владелец: [track].
"""

import argparse
import math
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from game.track import Track, CHECKPOINT_COUNT, WALL_MARGIN  # noqa: E402

TRACKS_DIR = os.path.join(ROOT, 'content', 'tracks')

CURVATURE_STENCIL = 2      # точек в каждую сторону для оценки радиуса (±4 м)
CLEARANCE_SKIP = 30        # соседей ближе этого по индексу не сравниваем


# --- измерения --------------------------------------------------------------

def curvature_radius(samples, i, n):
    """Радиус описанной окружности по трём точкам осевой линии."""
    a = samples[(i - CURVATURE_STENCIL) % n]
    b = samples[i]
    c = samples[(i + CURVATURE_STENCIL) % n]
    ab = math.hypot(b.x - a.x, b.z - a.z)
    bc = math.hypot(c.x - b.x, c.z - b.z)
    ca = math.hypot(a.x - c.x, a.z - c.z)
    area2 = abs((b.x - a.x) * (c.z - a.z) - (c.x - a.x) * (b.z - a.z))
    if area2 < 1e-9:
        return float('inf')
    return ab * bc * ca / (2.0 * area2)


def segments_cross(p1, p2, p3, p4):
    """Пересекаются ли отрезки p1p2 и p3p4."""
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1 = side(p3, p4, p1)
    d2 = side(p3, p4, p2)
    d3 = side(p1, p2, p3)
    d4 = side(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def self_intersections(samples):
    """Список пар индексов, где осевая линия пересекает саму себя."""
    n = len(samples)
    pts = [(s.x, s.z) for s in samples]
    bad = []
    for i in range(n):
        a1 = pts[i]
        a2 = pts[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            b1 = pts[j]
            b2 = pts[(j + 1) % n]
            if segments_cross(a1, a2, b1, b2):
                bad.append((i, j))
    return bad


def min_clearance(samples):
    """Минимальный зазор между несоседними участками полотна, м.

    Отрицательный зазор означает, что два куска трассы слиплись бортами.
    """
    n = len(samples)
    best = float('inf')
    best_pair = (0, 0)
    for i in range(n):
        si = samples[i]
        j = i + CLEARANCE_SKIP
        limit = i + n - CLEARANCE_SKIP
        while j < limit:
            sj = samples[j % n]
            gap = (math.hypot(si.x - sj.x, si.z - sj.z)
                   - si.half_width - sj.half_width)
            if gap < best:
                best = gap
                best_pair = (i, j % n)
            j += 1
    return best, best_pair


def summarize(track):
    samples = track.samples
    n = len(samples)
    radii = [curvature_radius(samples, i, n) for i in range(n)]
    min_r = min(radii)
    min_r_i = radii.index(min_r)
    hw = [s.half_width for s in samples]
    ys = [s.y for s in samples]
    crosses = self_intersections(samples)
    clearance, pair = min_clearance(samples)

    cp = track.checkpoints
    gaps = []
    for k in range(CHECKPOINT_COUNT):
        a = cp[k]
        b = cp[(k + 1) % CHECKPOINT_COUNT]
        d = (b - a) % n
        gaps.append(d * track.to_client()['sample_step'])

    print('=' * 62)
    print('%s  (%s)%s' % (track.name, track.id, '  [зеркало]' if track.mirror else ''))
    print('  %s' % track.desc)
    print('  тема: %-11s сложность: %d   зерно декора: %d'
          % (track.theme, track.difficulty, track.decor_seed))
    print('  длина круга:       %8.1f м' % track.length)
    print('  точек осевой:      %8d  (шаг %.3f м)' % (n, track.to_client()['sample_step']))
    print('  ширина полотна:    %8.1f .. %.1f м (min/max)' % (min(hw) * 2, max(hw) * 2))
    print('  минимальный радиус:%8.1f м  на s = %.0f м (%.0f%% круга)'
          % (min_r, samples[min_r_i].s, 100.0 * samples[min_r_i].s / track.length))
    print('  перепад высот:     %8.1f м  (%.2f .. %.2f)' % (max(ys) - min(ys), min(ys), max(ys)))
    print('  максимальный уклон:%8.1f %%' % (100.0 * max(
        abs(samples[(i + 1) % n].y - samples[i - 1].y) / (2.0 * track.to_client()['sample_step'])
        for i in range(n))))
    print('  боксы с бонусами:  %8d  в %d рядах' % (len(track.item_boxes),
                                                    len(track.source.get('item_rows', []))))
    print('  самопересечения:   %8s' % ('НЕТ' if not crosses else
                                        'ДА! %d пар, напр. %s' % (len(crosses), crosses[0])))
    print('  зазор между витками:%7.1f м  (точки %d и %d)' % (clearance, pair[0], pair[1]))
    print('  отсечки, м между соседними: %s'
          % ' '.join('%.0f' % g for g in gaps))
    grid = track.start_grid
    print('  решётка: 8 мест, от %.1f до %.1f м позади линии'
          % (track.length - _grid_s(track, grid[0]), track.length - _grid_s(track, grid[7])))
    if crosses:
        print('  !!! трасса пересекает сама себя — переделать контрольные точки')
    if clearance < 2.0:
        print('  !!! витки почти слиплись, между ними меньше 2 м')
    if min_r < 12.0:
        print('  !!! слишком острый излом: радиус %.1f м' % min_r)
    return {'min_r': min_r, 'min_r_i': min_r_i, 'clearance': clearance,
            'crosses': crosses}


def _grid_s(track, place):
    """Дуговая координата места на решётке (для печати отступа от линии)."""
    idx = track.nearest_index(place['x'], place['z'], -1)
    return track.samples[idx].s


# --- SVG --------------------------------------------------------------------

PALETTE = [(0.0, '#2f6fb2'), (0.5, '#59b37a'), (1.0, '#d8a03c')]


def height_color(t):
    """Цвет по нормированной высоте: синий низ -> зелёный -> охра верх."""
    for k in range(len(PALETTE) - 1):
        t0, c0 = PALETTE[k]
        t1, c1 = PALETTE[k + 1]
        if t <= t1 or k == len(PALETTE) - 2:
            f = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
            f = max(0.0, min(1.0, f))
            a = [int(c0[1:][i:i + 2], 16) for i in (0, 2, 4)]
            b = [int(c1[1:][i:i + 2], 16) for i in (0, 2, 4)]
            return '#%02x%02x%02x' % tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))
    return PALETTE[-1][1]


def write_svg(track, path, info):
    samples = track.samples
    n = len(samples)
    left = [(s.x - s.normal_x * s.half_width, s.z - s.normal_z * s.half_width) for s in samples]
    right = [(s.x + s.normal_x * s.half_width, s.z + s.normal_z * s.half_width) for s in samples]
    wall_l = [(s.x - s.normal_x * (s.half_width + WALL_MARGIN),
               s.z - s.normal_z * (s.half_width + WALL_MARGIN)) for s in samples]
    wall_r = [(s.x + s.normal_x * (s.half_width + WALL_MARGIN),
               s.z + s.normal_z * (s.half_width + WALL_MARGIN)) for s in samples]

    xs = [p[0] for p in wall_l] + [p[0] for p in wall_r]
    zs = [p[1] for p in wall_l] + [p[1] for p in wall_r]
    pad = 24.0
    min_x, max_x = min(xs) - pad, max(xs) + pad
    min_z, max_z = min(zs) - pad, max(zs) + pad
    size = 1100.0
    scale = size / max(max_x - min_x, max_z - min_z)
    w = (max_x - min_x) * scale
    h = (max_z - min_z) * scale

    def sx(x):
        return (x - min_x) * scale

    def sy(z):
        return (max_z - z) * scale        # z вверх на карте -> y вниз в SVG

    def poly(points):
        return ' '.join('%.1f,%.1f' % (sx(p[0]), sy(p[1])) for p in points)

    def ring(points):
        """Замкнутый контур в виде пути SVG."""
        head = 'M %.1f %.1f' % (sx(points[0][0]), sy(points[0][1]))
        rest = ''.join(' L %.1f %.1f' % (sx(p[0]), sy(p[1])) for p in points[1:])
        return head + rest + ' Z'

    ys = [s.y for s in samples]
    y_lo, y_hi = min(ys), max(ys)
    y_span = (y_hi - y_lo) or 1.0

    out = []
    add = out.append
    add('<?xml version="1.0" encoding="UTF-8"?>')
    add('<svg xmlns="http://www.w3.org/2000/svg" width="%.0f" height="%.0f" '
        'viewBox="0 0 %.0f %.0f">' % (w, h + 96, w, h + 96))
    add('<rect width="100%%" height="100%%" fill="#15181d"/>')
    add('<g stroke-linejoin="round">')
    # зона вылета: кольцо между стенами
    add('<path d="%s %s" fill="#3f4632" fill-rule="evenodd"/>'
        % (ring(wall_l), ring(wall_r)))
    # полотно: кольцо между левой и правой кромкой
    add('<path d="%s %s" fill="#4b5058" fill-rule="evenodd"/>'
        % (ring(left), ring(right)))
    add('<polyline points="%s" fill="none" stroke="#9aa3ad" stroke-width="1.6"/>'
        % (poly(left) + ' ' + '%.1f,%.1f' % (sx(left[0][0]), sy(left[0][1]))))
    add('<polyline points="%s" fill="none" stroke="#9aa3ad" stroke-width="1.6"/>'
        % (poly(right) + ' ' + '%.1f,%.1f' % (sx(right[0][0]), sy(right[0][1]))))
    # осевая линия, цвет по высоте
    for i in range(n):
        j = (i + 1) % n
        c = height_color((samples[i].y - y_lo) / y_span)
        add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="3.2"/>'
            % (sx(samples[i].x), sy(samples[i].z), sx(samples[j].x), sy(samples[j].z), c))
    # отсечки
    for k, idx in enumerate(track.checkpoints):
        s = samples[idx]
        a = (s.x - s.normal_x * s.half_width, s.z - s.normal_z * s.half_width)
        b = (s.x + s.normal_x * s.half_width, s.z + s.normal_z * s.half_width)
        colour = '#f2f4f7' if k == 0 else '#7d8794'
        add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="%s"/>'
            % (sx(a[0]), sy(a[1]), sx(b[0]), sy(b[1]), colour, '5' if k == 0 else '2'))
        add('<text x="%.1f" y="%.1f" fill="#7d8794" font-family="monospace" '
            'font-size="13">%d</text>' % (sx(b[0]) + 5, sy(b[1]), k))
    # самый острый поворот
    s = samples[info['min_r_i']]
    add('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="none" stroke="#e5484d" '
        'stroke-width="2" stroke-dasharray="6 4"/>'
        % (sx(s.x), sy(s.z), max(6.0, s.half_width * scale * 1.4)))
    # боксы с бонусами
    for box in track.item_boxes:
        add('<rect x="%.1f" y="%.1f" width="7" height="7" fill="#f0c040" '
            'transform="rotate(45 %.1f %.1f)"/>'
            % (sx(box['x']) - 3.5, sy(box['z']) - 3.5, sx(box['x']), sy(box['z'])))
    # стартовая решётка
    for k, place in enumerate(track.start_grid):
        px, pz = place['x'], place['z']
        hx, hz = math.sin(place['yaw']), math.cos(place['yaw'])
        add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#57b6ff" stroke-width="2.5"/>'
            % (sx(px), sy(pz), sx(px + hx * 3.2), sy(pz + hz * 3.2)))
        add('<circle cx="%.1f" cy="%.1f" r="3.5" fill="#57b6ff"/>' % (sx(px), sy(pz)))
        add('<text x="%.1f" y="%.1f" fill="#57b6ff" font-family="monospace" '
            'font-size="12">%d</text>' % (sx(px) + 6, sy(pz) + 4, k + 1))
    add('</g>')
    caption = ('%s [%s]  —  %.0f м, ширина %.1f..%.1f м, мин. радиус %.0f м, '
               'перепад %.1f м, %d точек'
               % (track.name, track.id, track.length,
                  min(s.half_width for s in samples) * 2,
                  max(s.half_width for s in samples) * 2,
                  info['min_r'], y_hi - y_lo, n))
    add('<text x="16" y="%.0f" fill="#e8ebef" font-family="monospace" font-size="22">%s</text>'
        % (h + 34, caption))
    add('<text x="16" y="%.0f" fill="#7d8794" font-family="monospace" font-size="16">'
        'белая черта — старт/финиш, ромбы — боксы, синие метки — решётка, '
        'красный круг — самый острый поворот, цвет осевой — высота</text>' % (h + 62))
    add('</svg>')
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write('\n'.join(x for x in out if x))
    return path


# --- запуск -----------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description='Сводка и вид сверху для трассы')
    parser.add_argument('track', nargs='*', help='id трассы; без аргументов — все')
    parser.add_argument('--mirror', action='store_true', help='зеркальный вариант')
    parser.add_argument('--out', default=tempfile.gettempdir(), help='куда положить SVG')
    args = parser.parse_args(argv)

    ids = args.track
    if not ids:
        ids = sorted(f[:-5] for f in os.listdir(TRACKS_DIR) if f.endswith('.json'))

    bad = 0
    for track_id in ids:
        path = os.path.join(TRACKS_DIR, track_id + '.json')
        if not os.path.exists(path):
            print('нет такой трассы: %s' % path)
            bad += 1
            continue
        track = Track.load(path, mirror=args.mirror)
        info = summarize(track)
        name = '%s%s.svg' % (track_id, '_mirror' if args.mirror else '')
        svg = write_svg(track, os.path.join(args.out, name), info)
        print('  SVG: %s' % svg)
        if info['crosses'] or info['clearance'] < 2.0 or info['min_r'] < 12.0:
            bad += 1
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
