"""Бонусы: движок реализует несколько примитивов, содержимое задаёт JSON.

Примитивы: self / projectile / trap / blast (+ флаг shield). Новый бонус —
строчка в shared/powerups.json, а не новый код. Всё считается ТОЛЬКО на
сервере: клиент бонусы не предсказывает, потому что предсказывать чужие
решения нельзя, а в локальной сети задержка попадания незаметна.
"""

import json
import math
import random

BOX_RESPAWN = 7.0
BOX_RADIUS = 30.0


class Effect:
    __slots__ = ("until", "accel", "maxs", "steer", "grip", "spin", "control", "item")

    def __init__(self, until, spec, item):
        self.until = until
        self.accel = float(spec.get("accelMul", 1.0))
        self.maxs = float(spec.get("maxSpeedMul", 1.0))
        self.steer = float(spec.get("steerMul", 1.0))
        self.grip = float(spec.get("gripMul", 1.0))
        self.spin = float(spec.get("spin", 0.0))
        self.control = spec.get("control", None)
        self.item = item


class Entity:
    __slots__ = ("eid", "kind", "item", "x", "y", "vx", "vy", "a", "owner",
                 "born", "until", "radius", "seg")


def aggregate(car, tick):
    """Сводит активные эффекты в массив, который понимает shared/carstep."""
    accel = maxs = steer = grip = 1.0
    spin = 0.0
    control = 1.0
    alive = []
    for e in car.effects:
        if e.until <= tick:
            continue
        alive.append(e)
        accel *= e.accel
        maxs *= e.maxs
        steer *= e.steer
        grip *= e.grip
        spin += e.spin
        if e.control is not None:
            if e.control == 0.0:
                control = 0.0
            elif e.control < 0.0 and control > 0.0:
                control = -1.0
    if len(alive) != len(car.effects):
        car.effects = alive
    return [accel, maxs, steer, grip, spin, control]


class Powerups:
    def __init__(self, defs, seed=None):
        self.items = {it["id"]: it for it in defs["items"]}
        self.order = [it["id"] for it in defs["items"]]
        self.rng = random.Random(seed)
        self._next_eid = 1

    # ------------------------------------------------------------------ выдача

    def roll(self, pos_norm):
        """pos_norm: 0 — лидер, 1 — последний. Отстающим чаще падает сильное."""
        total = 0.0
        weights = []
        for iid in self.order:
            w = self.items[iid].get("weight", [1.0, 1.0, 1.0])
            if pos_norm <= 0.5:
                k = pos_norm * 2.0
                v = w[0] + (w[1] - w[0]) * k
            else:
                k = (pos_norm - 0.5) * 2.0
                v = w[1] + (w[2] - w[1]) * k
            if v < 0.0:
                v = 0.0
            weights.append(v)
            total += v
        if total <= 0.0:
            return self.order[0]
        r = self.rng.random() * total
        for iid, v in zip(self.order, weights):
            r -= v
            if r <= 0.0:
                return iid
        return self.order[-1]

    # ------------------------------------------------------------- применение

    def _apply_hit(self, race, car, spec, item, source):
        """Наложить эффект на машину. Щит съедает одно попадание целиком."""
        if car.shield_until > race.tick:
            car.shield_until = 0
            race.event("shield", slot=car.slot, by=source)
            return False
        dur = float(spec.get("duration", 1.0))
        car.effects.append(Effect(race.tick + int(dur * race.hz), spec, item))
        race.event("hit", slot=car.slot, item=item, by=source)
        return True

    def use(self, race, car):
        """Игрок нажал «применить»."""
        item = car.item
        if not item:
            return
        spec = self.items.get(item)
        car.item = None
        if not spec:
            return
        kind = spec.get("kind")

        if kind == "self":
            if spec.get("shield"):
                dur = float(spec.get("effect", {}).get("duration", 8.0))
                car.shield_until = race.tick + int(dur * race.hz)
            eff = spec.get("effect")
            if eff and not spec.get("shield"):
                car.effects.append(Effect(
                    race.tick + int(float(eff.get("duration", 1.0)) * race.hz), eff, item))
            race.event("use", slot=car.slot, item=item)

        elif kind == "projectile":
            sp = float(spec.get("speed", 600.0))
            e = Entity()
            e.eid = self._next_eid
            self._next_eid += 1
            e.kind = "proj"
            e.item = item
            e.a = car.a
            e.x = car.x + math.cos(car.a) * 30.0
            e.y = car.y + math.sin(car.a) * 30.0
            e.vx = car.vx + math.cos(car.a) * sp
            e.vy = car.vy + math.sin(car.a) * sp
            e.owner = car.slot
            e.born = race.tick
            e.until = race.tick + int(float(spec.get("life", 3.0)) * race.hz)
            e.radius = float(spec.get("radius", 22.0))
            e.seg = car.seg
            race.entities.append(e)
            race.event("use", slot=car.slot, item=item)

        elif kind == "trap":
            e = Entity()
            e.eid = self._next_eid
            self._next_eid += 1
            e.kind = "trap"
            e.item = item
            e.a = car.a
            e.x = car.x - math.cos(car.a) * 34.0
            e.y = car.y - math.sin(car.a) * 34.0
            e.vx = e.vy = 0.0
            e.owner = car.slot
            e.born = race.tick
            e.until = race.tick + int(float(spec.get("life", 25.0)) * race.hz)
            e.radius = float(spec.get("radius", 30.0))
            e.seg = car.seg
            race.entities.append(e)
            race.event("use", slot=car.slot, item=item)

        elif kind == "blast":
            target = spec.get("target", "others")
            hit = spec.get("hit", {})
            victims = []
            if target == "ahead":
                victims = [c for c in race.live_cars() if c.total_prog > car.total_prog]
            elif target == "leader":
                others = [c for c in race.live_cars() if c.slot != car.slot]
                if others:
                    victims = [max(others, key=lambda c: c.total_prog)]
            elif target == "nearest":
                others = [c for c in race.live_cars() if c.slot != car.slot]
                if others:
                    victims = [min(others, key=lambda c: (c.x - car.x) ** 2 + (c.y - car.y) ** 2)]
            else:
                victims = [c for c in race.live_cars() if c.slot != car.slot]
            for v in victims:
                self._apply_hit(race, v, hit, item, car.slot)
            race.event("use", slot=car.slot, item=item)

    # ------------------------------------------------------------------- тик

    def tick(self, race, dt):
        # Боксы: подбор и возрождение
        for b in race.boxes:
            if b["until"] > race.tick:
                continue
            for car in race.live_cars():
                if car.item is not None:
                    continue
                dx = car.x - b["x"]
                dy = car.y - b["y"]
                if dx * dx + dy * dy <= BOX_RADIUS * BOX_RADIUS:
                    n = max(1, len(race.cars) - 1)
                    pos_norm = race.rank_of(car) / n if n else 0.0
                    car.item = self.roll(pos_norm)
                    b["until"] = race.tick + int(BOX_RESPAWN * race.hz)
                    race.event("pickup", slot=car.slot, item=car.item)
                    break

        # Снаряды и ловушки
        alive = []
        for e in race.entities:
            if e.until <= race.tick:
                race.event("expire", eid=e.eid)
                continue
            spec = self.items.get(e.item, {})

            if e.kind == "proj":
                hom = float(spec.get("homing", 0.0))
                if hom > 0.0:
                    tgt = self._nearest_ahead(race, e)
                    if tgt is not None:
                        want = math.atan2(tgt.y - e.y, tgt.x - e.x)
                        d = (want - e.a + math.pi) % (2 * math.pi) - math.pi
                        turn = max(-hom * dt, min(hom * dt, d))
                        e.a += turn
                        sp = math.hypot(e.vx, e.vy)
                        e.vx = math.cos(e.a) * sp
                        e.vy = math.sin(e.a) * sp
                e.x += e.vx * dt
                e.y += e.vy * dt

                # Снаряд гасится о стену, чтобы не улетал в поле навсегда.
                # Подсказка ведётся по самому снаряду: оконный поиск от нуля
                # нашёл бы не тот сегмент.
                q = race.track.query(e.x, e.y, e.seg)
                e.seg = q[0]
                if abs(q[2]) > q[3] + race.track.shoulder + 40.0:
                    race.event("boom", eid=e.eid, x=round(e.x, 1), y=round(e.y, 1))
                    continue

            hit_car = None
            r2 = (e.radius + race.car_radius) ** 2
            # Своя же ловушка не срабатывает, пока владелец отъезжает, а свой
            # снаряд не бьёт стрелка в момент выстрела.
            grace = 8 if e.kind == "proj" else 40
            for car in race.live_cars():
                if car.slot == e.owner and race.tick - e.born < grace:
                    continue
                dx = car.x - e.x
                dy = car.y - e.y
                if dx * dx + dy * dy <= r2:
                    hit_car = car
                    break

            if hit_car is not None:
                self._apply_hit(race, hit_car, spec.get("hit", {}), e.item, e.owner)
                race.event("boom", eid=e.eid, x=round(e.x, 1), y=round(e.y, 1))
                continue

            alive.append(e)
        race.entities = alive

    @staticmethod
    def _nearest_ahead(race, e):
        best, bd = None, 1.0e18
        for car in race.live_cars():
            if car.slot == e.owner:
                continue
            dx = car.x - e.x
            dy = car.y - e.y
            # только то, что впереди по курсу снаряда
            if dx * math.cos(e.a) + dy * math.sin(e.a) <= 0.0:
                continue
            d = dx * dx + dy * dy
            if d < bd:
                bd, best = d, car
        return best


def load_powerups(path, seed=None):
    with open(path, encoding="utf-8") as f:
        return Powerups(json.load(f), seed)
