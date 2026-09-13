"""Геометрия трассы: то, от чего зависят и коллизии, и подсчёт кругов."""

import math

from server.track import Track


def make_ring(w=150.0, r=1200.0, n=20):
    nodes = [{"x": math.cos(2 * math.pi * k / n) * r,
              "y": math.sin(2 * math.pi * k / n) * r,
              "w": w, "s": "road"} for k in range(n)]
    return Track({"id": "t", "nodes": nodes, "closed": True, "checkpoints": 8})


def test_polyline_is_uniform(track):
    """Шаг ломаной должен быть ровным: от него зависит и окно поиска
    сегмента, и точность прогресса по кругу."""
    assert max(track.seglen) - min(track.seglen) < 1.0
    assert abs(sum(track.seglen) - track.length) < 1e-6


def test_query_on_centerline_is_zero(track):
    for i in range(0, track.nseg, 17):
        seg, t, lat, hw, sfc, qx, qy = track.query(track.px[i], track.py[i], i)
        assert abs(lat) < 1e-6
        assert sfc == track.surf[seg]


def test_query_sign_distinguishes_sides(track):
    i = 30
    nx, ny = -(track.py[i + 1] - track.py[i]), track.px[i + 1] - track.px[i]
    d = math.hypot(nx, ny)
    nx, ny = nx / d, ny / d
    left = track.query(track.px[i] + nx * 40, track.py[i] + ny * 40, i)
    right = track.query(track.px[i] - nx * 40, track.py[i] - ny * 40, i)
    assert left[2] * right[2] < 0, "стороны дороги должны различаться знаком"


def test_query_with_stale_hint_still_finds_road(track):
    """Подсказка отстаёт на несколько сегментов — поиск обязан догнать."""
    i = 100
    for back in range(0, 13):
        seg, t, lat, hw, sfc, qx, qy = track.query(track.px[i], track.py[i], i - back)
        assert abs(lat) < 1.0, f"подсказка отстала на {back}, потеряли дорогу"


def test_off_road_reports_other_surface(track):
    i = 50
    nx, ny = track.nx_at(i) if hasattr(track, "nx_at") else (None, None)
    a, b = i, (i + 1) % track.n
    dx, dy = track.px[b] - track.px[a], track.py[b] - track.py[a]
    d = math.hypot(dx, dy)
    nx, ny = -dy / d, dx / d
    off = track.hw[i] + 30
    q = track.query(track.px[i] + nx * off, track.py[i] + ny * off, i)
    assert q[4] == track.off_surface


def test_wall_contains_car(track, phys):
    """Машина, едущая перпендикулярно дороге, не должна улететь в поле."""
    class C:
        pass
    car = C()
    i = 40
    a, b = i, (i + 1) % track.n
    dx, dy = track.px[b] - track.px[a], track.py[b] - track.py[a]
    d = math.hypot(dx, dy)
    nx, ny = -dy / d, dx / d
    car.x, car.y = track.px[i], track.py[i]
    car.vx, car.vy = nx * 900, ny * 900
    car.a, car.seg = 0.0, i
    limit = track.hw[i] + track.shoulder
    for _ in range(240):
        car.x += car.vx / 60.0
        car.y += car.vy / 60.0
        q = track.query(car.x, car.y, car.seg)
        car.seg = q[0]
        track.resolve_wall(car, q[2], q[3], q[5], q[6], phys["collision"])
        q2 = track.query(car.x, car.y, car.seg)
        assert abs(q2[2]) <= limit + 2.0, "машина пробила стену"


def test_start_slots_are_on_road(track):
    for slot in track.start_slots(6):
        seg = track.query_global(slot["x"], slot["y"])
        q = track.query(slot["x"], slot["y"], seg)
        assert abs(q[2]) < q[3], "стартовое место оказалось вне дороги"


def test_checkpoints_unique_and_start_first(tracks):
    for t in tracks.values():
        assert len(set(t.checkpoints)) == len(t.checkpoints), f"{t.id}: дубли чекпоинтов"
        assert t.checkpoints[0] == 0, f"{t.id}: линия старта должна быть первой"
        assert len(t.checkpoints) >= 3


def test_wide_road_is_allowed():
    """Широкая дорога когда-то ложно считалась самопересечением."""
    t = make_ring(w=340.0, r=2600.0, n=24)
    assert max(t.hw) * 2 > 330


def test_payload_round_trips_exactly(track):
    """Клиент должен получить ТЕ ЖЕ числа, которыми считает сервер, —
    иначе его предсказание поедет по чуть другой геометрии."""
    import json
    p = json.loads(json.dumps(track.client_payload()))
    assert p["px"] == track.px
    assert p["py"] == track.py
    assert p["hw"] == track.hw
    assert p["surf"] == track.surf
