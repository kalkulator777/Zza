"""Лобби, бонусы и таблица рекордов."""

import os
import tempfile

from server.lobby import LobbyManager, clean_name, MAX_PLAYERS
from server.powerups import Powerups, aggregate, Effect
from server.race import Race
from server.records import Records


# ------------------------------------------------------------------- лобби

def test_settings_from_browser_are_sanitized(tracks):
    lm = LobbyManager(tracks)
    s = lm.sanitize({"track": "нет такой", "laps": 9999, "max": 99,
                     "powerups": "да", "collisions": 0})
    assert s["track"] in tracks
    assert 1 <= s["laps"] <= 20
    assert 2 <= s["max"] <= MAX_PLAYERS
    assert s["powerups"] is True and s["collisions"] is False

    s2 = lm.sanitize({"laps": "мусор", "max": None})
    assert isinstance(s2["laps"], int) and isinstance(s2["max"], int)

    assert lm.sanitize(None)["track"] in tracks
    assert lm.sanitize("не словарь")["track"] in tracks


def test_names_are_cleaned():
    assert clean_name("  Вася  ") == "Вася"
    assert clean_name("") == "Гонщик"
    assert clean_name(None) == "Гонщик"
    assert clean_name("a" * 100) == "a" * 18
    assert "\n" not in clean_name("плохое\nимя")


def test_host_passes_on_when_host_leaves(tracks):
    lm = LobbyManager(tracks)
    lb = lm.create("t1", "Заезд", {})
    lb.add("t1", "А")
    lb.add("t2", "Б")
    assert lb.host == "t1"
    lb.remove("t1")
    assert lb.host == "t2", "лобби осталось без хозяина"


def test_colors_do_not_repeat(tracks):
    lm = LobbyManager(tracks)
    lb = lm.create("t0", "Заезд", {})
    colors = {lb.add(f"t{i}", f"И{i}").color for i in range(MAX_PLAYERS)}
    assert len(colors) == MAX_PLAYERS


def test_hidden_lobby_not_listed(tracks):
    lm = LobbyManager(tracks)
    lm.create("t1", "Обычное", {}).add("t1", "А")
    solo = lm.create("t2", "На время", {})
    solo.hidden = True
    solo.add("t2", "Б")
    ids = [x["id"] for x in lm.listing()]
    assert solo.id not in ids
    assert len(ids) == 1


def test_empty_lobbies_are_swept(tracks):
    lm = LobbyManager(tracks)
    lb = lm.create("t1", "Пустое", {})
    lb.created -= 100
    assert lm.sweep() == [lb.id]
    assert not lm.lobbies


# ------------------------------------------------------------------ бонусы

def test_roll_favours_the_trailing_player():
    defs = {"items": [
        {"id": "weak", "kind": "self", "weight": [5.0, 1.0, 0.0]},
        {"id": "strong", "kind": "self", "weight": [0.0, 1.0, 5.0]},
    ]}
    pw = Powerups(defs, seed=1)
    lead = [pw.roll(0.0) for _ in range(400)].count("strong")
    last = [pw.roll(1.0) for _ in range(400)].count("strong")
    assert lead == 0, "лидеру не должно падать сильное"
    assert last == 400, "последнему должно падать сильное"


def test_aggregate_multiplies_and_expires():
    class C:
        effects = []
    c = C()
    c.effects = [Effect(100, {"accelMul": 2.0}, "a"),
                 Effect(100, {"accelMul": 3.0}, "b"),
                 Effect(5, {"accelMul": 9.0}, "старый")]
    eff = aggregate(c, 50)
    assert eff[0] == 6.0, "эффекты должны перемножаться"
    assert len(c.effects) == 2, "истёкший эффект не убран"


def test_control_effects_take_the_worst():
    class C:
        effects = []
    c = C()
    c.effects = [Effect(100, {"control": -1}, "a"), Effect(100, {"control": 0}, "b")]
    assert aggregate(c, 10)[5] == 0.0


def test_shield_absorbs_one_hit(track, phys, powerups):
    race = Race(track, {"laps": 1, "powerups": True}, [
        {"token": "a", "name": "А", "color": "#f00"}], powerups, phys)
    c = race.cars[0]
    c.shield_until = race.tick + 600
    powerups._apply_hit(race, c, {"spin": 9.0, "duration": 2.0}, "rocket", 1)
    assert not c.effects, "щит не поглотил попадание"
    assert c.shield_until == 0, "щит должен расходоваться"
    powerups._apply_hit(race, c, {"spin": 9.0, "duration": 2.0}, "rocket", 1)
    assert c.effects, "второе попадание должно пройти"


def test_projectile_hits_and_disappears(track, phys, powerups):
    race = Race(track, {"laps": 1, "powerups": True, "collisions": False},
                [{"token": "a", "name": "А", "color": "#f00"},
                 {"token": "b", "name": "Б", "color": "#0f0"}], powerups, phys)
    shooter, victim = race.cars
    shooter.item = "rocket"
    race.state = "racing"
    victim.x = shooter.x + 90
    victim.y = shooter.y
    shooter.a = 0.0
    powerups.use(race, shooter)
    assert len(race.entities) == 1
    for _ in range(30):
        race.tick += 1
        powerups.tick(race, 1 / 60.0)
        if not race.entities:
            break
    assert not race.entities, "снаряд не исчез после попадания"
    assert victim.effects, "попадание не наложило эффект"


def test_own_trap_does_not_hit_owner_immediately(track, phys, powerups):
    race = Race(track, {"laps": 1, "powerups": True},
                [{"token": "a", "name": "А", "color": "#f00"}], powerups, phys)
    c = race.cars[0]
    c.item = "mine"
    race.state = "racing"
    powerups.use(race, c)
    for _ in range(20):
        race.tick += 1
        powerups.tick(race, 1 / 60.0)
    assert not c.effects, "подорвался на собственной мине сразу после установки"


# --------------------------------------------------------------- рекорды

def test_records_keep_best_per_person():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "records.json")
        r = Records(path)
        r.submit("t", "Вася", 40000)
        r.submit("t", "Вася", 35000)
        r.submit("t", "Петя", 38000)
        board = r.board("t")
        assert [x["name"] for x in board] == ["Вася", "Петя"]
        assert board[0]["ms"] == 35000
        r.save()

        again = Records(path)
        assert again.board("t")[0]["ms"] == 35000, "рекорды не пережили перезапуск"


def test_records_survive_broken_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "records.json")
        with open(path, "w") as f:
            f.write("{ это не json")
        r = Records(path)
        assert r.data == {}
        r.submit("t", "Вася", 1000)
        r.save()
        assert Records(path).board("t")


def test_records_ignore_garbage_times():
    with tempfile.TemporaryDirectory() as d:
        r = Records(os.path.join(d, "r.json"))
        assert r.submit("t", "Вася", 0) is None
        assert r.submit("t", "Вася", None) is None
        assert not r.board("t")
