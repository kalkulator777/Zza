"""Заезд: круги, срезание, столкновения, возврат на трассу, снапшоты."""

import math

from server.race import Race
from tests.driver import autopilot

PLAYERS = [{"token": "a", "name": "А", "color": "#f00"},
           {"token": "b", "name": "Б", "color": "#0f0"}]


def mk(track, phys, powerups, players=None, **settings):
    s = {"laps": 1, "powerups": False, "collisions": False}
    s.update(settings)
    return Race(track, s, players or PLAYERS[:1], powerups, phys)


def place_at(race, car, s):
    """Ставит машину на осевую линию в точку s по длине круга.

    last_prog намеренно не трогаем: именно по разнице с ним _progress
    определяет пересечение линии старта.
    """
    t = race.track
    i = t.seg_at_length(s % t.length)
    a, b = i, (i + 1) % t.n
    car.x, car.y = t.px[a], t.py[a]
    car.a = math.atan2(t.py[b] - t.py[a], t.px[b] - t.px[a])
    car.vx = car.vy = 0.0
    car.seg = i


def run(race, ticks, style=5):
    for _ in range(ticks):
        for c in race.cars:
            if not c.finished:
                race.set_input(c.token, race.tick * 10 + c.slot + 1, race.tick + 2,
                               autopilot(c, race.track, look=style + c.slot))
        race.step()


def test_countdown_holds_cars_still(track, phys, powerups):
    race = mk(track, phys, powerups)
    c = race.cars[0]
    x0, y0 = c.x, c.y
    for _ in range(race.start_tick - 1):
        race.set_input("a", race.tick + 1, race.tick + 2, 1)
        race.step()
    assert race.state == "countdown"
    assert (c.x, c.y) == (x0, y0), "машина поехала до старта"


def test_effects_during_countdown_say_no_control(track, phys, powerups):
    """Снапшот на отсчёте обязан сообщать, что управления нет.

    Если он этого не скажет, клиент нажмёт газ, уедет и будет дёргаться
    назад от каждого снапшота — так и было до появления effects_for."""
    race = mk(track, phys, powerups)
    race.step()
    row = race.snapshot()["c"][0]
    assert len(row) > 11, "на отсчёте эффекты обязаны быть в снапшоте"
    assert row[11][5] == 0.0, "control должен быть 0 во время отсчёта"


def test_full_lap_counts(track, phys, powerups):
    race = mk(track, phys, powerups, laps=1)
    run(race, 60 * 90)
    assert race.cars[0].lap >= 1
    assert race.cars[0].finished


def test_cutting_the_track_does_not_count(track, phys, powerups):
    """Проезд линии старта без чекпоинтов не должен давать круг."""
    race = mk(track, phys, powerups, laps=3)
    for _ in range(race.start_tick + 2):
        race.step()
    c = race.cars[0]

    place_at(race, c, track.length - 200)
    c.next_cp = 1                      # чекпоинты не пройдены
    race._progress(c)
    place_at(race, c, 200)
    race._progress(c)
    assert c.lap == 0, "срезавший получил круг"

    # А теперь честно: отмечаем все чекпоинты и снова пересекаем линию.
    # Время круга задаём правдоподобное, иначе сработает защита от кругов,
    # проехать которые физически невозможно.
    c.next_cp = len(track.checkpoints)
    c.lap_start_tick = race.tick - 60 * 40
    place_at(race, c, track.length - 200)
    race._progress(c)
    place_at(race, c, 200)
    race._progress(c)
    assert c.lap == 1, "честно пройденный круг не засчитали"


def test_going_backwards_over_line_removes_lap(track, phys, powerups):
    race = mk(track, phys, powerups, laps=3)
    for _ in range(race.start_tick + 2):
        race.step()
    c = race.cars[0]
    c.lap = 2
    place_at(race, c, 200)
    race._progress(c)
    place_at(race, c, track.length - 200)
    race._progress(c)
    assert c.lap == 1, "проезд назад через линию должен снимать круг"


def test_wobbling_on_the_finish_line_gives_no_free_lap(track, phys, powerups):
    """Машину качнуло через линию туда-сюда — круг за это не полагается."""
    race = mk(track, phys, powerups, laps=5)
    for _ in range(race.start_tick + 2):
        race.step()
    c = race.cars[0]
    c.next_cp = len(track.checkpoints)
    c.lap_start_tick = race.tick - 60 * 40

    place_at(race, c, track.length - 150)
    race._progress(c)
    place_at(race, c, 150)
    race._progress(c)
    assert c.lap == 1
    first = list(c.lap_times)

    # Проходит немного времени, и машину качает обратно через линию и снова вперёд
    for _ in range(60):
        race.step()
    place_at(race, c, track.length - 150)
    race._progress(c)
    place_at(race, c, 150)
    race._progress(c)

    assert c.lap == 1, "качание на линии выдало лишний круг"
    assert len(c.lap_times) == 1, f"записан лишний круг: {c.lap_times}"
    # Время круга правильно пересчитывается от прежнего старта — машина же
    # действительно каталась дольше. Важно, что оно не стало коротким.
    assert c.lap_times[0] >= first[0], f"круг подозрительно укоротился: {c.lap_times}"
    assert c.best_lap >= first[0]


def test_absurd_lap_time_is_rejected(track, phys, powerups):
    race = mk(track, phys, powerups, laps=5)
    assert race.min_lap_ms > 3000
    for _ in range(race.start_tick + 2):
        race.step()
    c = race.cars[0]
    c.next_cp = len(track.checkpoints)
    c.lap_start_tick = race.tick        # круг «за ноль секунд»
    place_at(race, c, track.length - 150)
    race._progress(c)
    place_at(race, c, 150)
    race._progress(c)
    assert c.lap == 0, "засчитали невозможно быстрый круг"


def test_stuck_car_is_respawned(track, phys, powerups):
    race = mk(track, phys, powerups, laps=3)
    for _ in range(race.start_tick + 2):
        race.step()
    c = race.cars[0]
    i = 60
    a, b = i, (i + 1) % track.n
    dx, dy = track.px[b] - track.px[a], track.py[b] - track.py[a]
    d = math.hypot(dx, dy)
    nx, ny = -dy / d, dx / d
    off = track.hw[i] + track.shoulder - 5
    c.x = track.px[i] + nx * off
    c.y = track.py[i] + ny * off
    c.vx = c.vy = 0.0
    c.seg = i
    before = (c.x, c.y)
    for _ in range(int(phys["respawn"]["stuckSeconds"] * 60) + 10):
        race.step()
    assert c.respawns == 1, "застрявшую машину не вернули на трассу"
    assert (c.x, c.y) != before
    q = track.query(c.x, c.y, c.seg)
    assert abs(q[2]) < q[3], "вернули не на дорогу"


def test_moving_offroad_car_is_not_respawned(track, phys, powerups):
    """Едет по траве — пусть выбирается сам, это его выбор."""
    race = mk(track, phys, powerups, laps=3)
    for _ in range(race.start_tick + 2):
        race.step()
    c = race.cars[0]
    i = 60
    a, b = i, (i + 1) % track.n
    dx, dy = track.px[b] - track.px[a], track.py[b] - track.py[a]
    d = math.hypot(dx, dy)
    c.x = track.px[i] - dy / d * (track.hw[i] + 20)
    c.y = track.py[i] + dx / d * (track.hw[i] + 20)
    c.a = math.atan2(dy, dx)
    c.seg = i
    for _ in range(60 * 4):
        race.set_input("a", race.tick + 1, race.tick + 2, 1)
        race.step()
    assert c.respawns == 0, "едущую машину вернули насильно"


def test_collisions_separate_cars(track, phys, powerups):
    race = mk(track, phys, powerups, collisions=True, players=PLAYERS)
    a, b = race.cars
    for _ in range(race.start_tick + 2):
        race.step()
    b.x, b.y = a.x, a.y + 2.0
    b.a = a.a
    for _ in range(40):
        race.step()
    d = math.hypot(a.x - b.x, a.y - b.y)
    assert d > phys["car"]["radius"], f"машины остались слипшимися, расстояние {d:.1f}"


def test_collisions_off_means_no_push(track, phys, powerups):
    race = mk(track, phys, powerups, collisions=False, players=PLAYERS)
    a, b = race.cars
    for _ in range(race.start_tick + 2):
        race.step()
    b.x, b.y = a.x, a.y
    b.vx = b.vy = a.vx = a.vy = 0.0
    race.step()
    assert math.hypot(a.x - b.x, a.y - b.y) < 1.0


def test_snapshot_shape(track, phys, powerups):
    race = mk(track, phys, powerups, players=PLAYERS)
    run(race, 200)
    s = race.snapshot()
    assert s["t"] == "s" and s["k"] == race.tick
    assert len(s["c"]) == 2
    for row in s["c"]:
        assert len(row) >= 11
        slot, x, y, ang, vx, vy, seg, lap, applied, item, slack = row[:11]
        assert isinstance(slot, int) and 0 <= seg < track.n
    assert len(s["r"]) == 2


def test_input_is_applied_on_its_own_tick(track, phys, powerups):
    """Ввод применяется на помеченном тике, не раньше и не позже —
    на этом держится совпадение с воспроизведением на клиенте."""
    race = mk(track, phys, powerups)
    for _ in range(race.start_tick + 2):
        race.step()
    c = race.cars[0]
    target = race.tick + 5
    race.set_input("a", 999, target, 1)
    while race.tick < target - 1:
        race.step()
        assert c.inp == 0, f"ввод применили раньше времени на тике {race.tick}"
    race.step()
    assert c.inp == 1, "ввод не применился на своём тике"


def test_far_future_input_is_clamped(track, phys, powerups):
    race = mk(track, phys, powerups)
    race.set_input("a", 1, race.tick + 100000, 1)
    c = race.cars[0]
    assert c.pending[0][0] <= race.tick + 120


def test_finished_car_still_reports_input_freshness(track, phys, powerups):
    """Иначе у доехавшего замерзает запас, и клиент бесконечно
    пересобирает предсказание."""
    race = mk(track, phys, powerups, laps=1)
    c = race.cars[0]
    c.finished = True
    race.set_input("a", 5, race.tick + 7, 1)
    assert c.newest_tick == race.tick + 7
    assert not c.pending, "доехавшая машина не должна принимать управление"
