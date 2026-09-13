"""Заезд: авторитетная симуляция.

Симуляция идёт фиксированным шагом 60 Гц, снапшоты уходят реже (30 Гц) —
это разные вещи, и путать их нельзя: одинаковый шаг у сервера и клиента это
условие сходимости предсказания, а частота снапшотов лишь торгует трафик
на плавность чужих машин.

Клиент предсказывает ТОЛЬКО свою машину и только против статики. Столкновения
машин, бонусы и попадания считает исключительно сервер.
"""

import math
import time

from shared.carstep import drive_tick, IN_USE, SURFACE_ORDER
from server.powerups import aggregate

COUNTDOWN_SECONDS = 3.6
GRACE_AFTER_LEADER = 45.0
DEFAULT_TIME_LIMIT = 900.0
RESPAWN_FREEZE = 0.9


class Car:
    __slots__ = (
        "slot", "token", "name", "color",
        "x", "y", "vx", "vy", "a", "seg",
        "inp", "last_seq", "pending", "applied_tick", "newest_tick", "slack",
        "connected", "gone_since",
        "lap", "next_cp", "last_prog", "total_prog", "rank",
        "effects", "shield_until", "item",
        "lap_start_tick", "prev_lap_start", "lap_times", "best_lap",
        "finish_tick", "finished",
        "stuck_ticks", "freeze_until", "respawns", "hit_flash",
    )

    def __init__(self, slot, token, name, color):
        self.slot = slot
        self.token = token
        self.name = name
        self.color = color
        self.x = self.y = self.vx = self.vy = self.a = 0.0
        self.seg = 0
        self.inp = 0
        self.last_seq = 0
        self.pending = []
        self.applied_tick = -1
        self.newest_tick = -1
        self.slack = 0
        self.connected = True
        self.gone_since = None
        self.lap = 0
        self.next_cp = 1
        self.last_prog = 0.0
        self.total_prog = 0.0
        self.rank = 0
        self.effects = []
        self.shield_until = 0
        self.item = None
        self.lap_start_tick = 0
        self.prev_lap_start = 0
        self.lap_times = []
        self.best_lap = None
        self.finish_tick = None
        self.finished = False
        self.stuck_ticks = 0
        self.freeze_until = 0
        self.respawns = 0
        self.hit_flash = 0


class Race:
    def __init__(self, track, settings, players, powerups, phys, hz=60):
        self.track = track
        self.settings = settings
        self.powerups = powerups
        self.phys = phys
        self.hz = hz
        self.dt = 1.0 / hz

        self.C = phys["car"]
        self.CC = phys["collision"]
        self.car_radius = float(self.C["radius"])
        self.axle = float(self.C["axleOffset"])
        self.surfaces = [
            [phys["surfaces"][n]["grip"], phys["surfaces"][n]["drag"],
             phys["surfaces"][n]["accel"], phys["surfaces"][n]["maxSpeed"]]
            for n in SURFACE_ORDER
        ]

        self.laps = int(settings.get("laps") or track.laps)
        self.use_powerups = bool(settings.get("powerups", True))
        self.use_collisions = bool(settings.get("collisions", True))
        self.mode = settings.get("mode", "race")
        self.time_limit = float(settings.get("timeLimit", DEFAULT_TIME_LIMIT))

        # Физически невозможный круг — признак того, что машину качнуло через
        # линию, а не проехала. Втрое выше предельной скорости не выжать даже
        # с ускорением на разгонном покрытии.
        self.min_lap_ms = int(1000.0 * track.length / (self.C["maxSpeed"] * 3.0))

        self.tick = 0
        self.state = "countdown"
        self.start_tick = int(COUNTDOWN_SECONDS * hz)
        self.started_wall = time.time()
        self.leader_finish_tick = None
        self.events = []
        self.entities = []

        slots = track.start_slots(max(1, len(players)))
        self.cars = []
        for i, p in enumerate(players):
            c = Car(i, p["token"], p["name"], p["color"])
            s = slots[i]
            c.x, c.y, c.a, c.seg = s["x"], s["y"], s["a"], s["seg"]
            q = track.query(c.x, c.y, c.seg)
            c.seg = q[0]
            c.last_prog = track.progress(q[0], q[1])
            c.lap_start_tick = self.start_tick
            self.cars.append(c)
        self.by_token = {c.token: c for c in self.cars}

        self.boxes = ([{"x": b["x"], "y": b["y"], "until": 0} for b in track.boxes]
                      if self.use_powerups else [])

        self._rank()

    # ---------------------------------------------------------------- helpers

    def live_cars(self):
        return [c for c in self.cars if not c.finished]

    def event(self, kind, **kw):
        kw["t"] = kind
        self.events.append(kw)

    def rank_of(self, car):
        return car.rank

    def elapsed(self):
        return max(0, self.tick - self.start_tick) / self.hz

    def set_input(self, token, seq, tick, mask):
        """Ввод помечен номером тика и применяется именно на нём.

        Иначе клиент, воспроизводя свои вводы после коррекции, применил бы их
        на другом тике, чем сервер, и предсказание дёргалось бы на каждом
        нажатии. Клиент идёт с небольшим опережением, чтобы ввод успевал
        прийти заранее; поле slack в снапшоте говорит ему, насколько он попал.
        """
        c = self.by_token.get(token)
        if c is None:
            return
        if seq <= c.last_seq:
            return
        c.last_seq = seq
        tick = int(tick)
        # Свежесть ввода отмечаем даже у доехавших. Это замер опережения, а не
        # управление: без него у финишировавшего запас замерзал, клиент считал,
        # что безнадёжно отстал, и пересобирал предсказание каждый снапшот.
        if tick > c.newest_tick:
            c.newest_tick = tick
        if c.finished:
            return
        # Ввод, помеченный слишком далёким будущим, — это либо убежавший вперёд
        # клиент, либо подделка. Применяем как можно скорее, но не копим:
        # переполнение очереди молча теряло бы нажатия.
        if tick > self.tick + 120:
            tick = self.tick + 120
        c.pending.append((tick, int(mask)))
        if len(c.pending) > 150:
            del c.pending[:-150]

    def _consume_inputs(self):
        """Берём все вводы, чей тик уже наступил, в правильном порядке."""
        for c in self.cars:
            # Запас считаем по самому свежему ПОЛУЧЕННОМУ вводу, а не по
            # оставшемуся в очереди: иначе в паузах между нажатиями клиенту
            # казалось бы, что он безнадёжно опаздывает, и он разгонялся,
            # убегая от сервера на сотни тиков.
            c.slack = c.newest_tick - self.tick
            if not c.pending:
                continue
            c.pending.sort(key=lambda it: it[0])
            keep = []
            for t, m in c.pending:
                if t > self.tick:
                    keep.append((t, m))
                    continue
                # Фронт кнопки «применить» ловим на каждом вводе, а не только
                # на последнем: иначе быстрое нажатие потерялось бы при
                # склейке нескольких тиков.
                if (m & IN_USE) and not (c.inp & IN_USE):
                    if self.use_powerups and self.state == "racing" and c.item and not c.finished:
                        self.powerups.use(self, c)
                c.inp = m
                c.applied_tick = t
            c.pending = keep

    def request_respawn(self, token):
        c = self.by_token.get(token)
        if c is not None and self.state == "racing" and not c.finished:
            self._respawn(c)

    # ------------------------------------------------------------------- tick

    NO_CONTROL = [1.0, 1.0, 1.0, 1.0, 0.0, 0.0]

    def effects_for(self, c):
        """Эффекты машины на текущий тик.

        Один метод и для симуляции, и для снапшота — намеренно. Когда эти две
        стороны решали независимо, во время обратного отсчёта сервер держал
        машины на месте, а клиенту сообщал, что управление доступно: тот жал
        газ, уезжал, и каждый снапшот возвращал его обратно.
        """
        if c.finished or self.state == "countdown" or self.tick < c.freeze_until:
            return self.NO_CONTROL
        return aggregate(c, self.tick)

    def step(self):
        self.tick += 1
        self._consume_inputs()

        counting = self.state == "countdown"
        if counting and self.tick >= self.start_tick:
            self.state = "racing"
            counting = False
            for c in self.cars:
                c.lap_start_tick = self.tick
            self.event("go")

        for c in self.cars:
            eff = self.effects_for(c)
            inp = 0 if eff is self.NO_CONTROL else c.inp
            drive_tick(c, inp, self.dt, self.track, self.C, self.CC, eff, self.surfaces)

        if counting:
            # На отсчёте машины стоят: считать столкновения, бонусы и круги рано
            return

        if self.use_collisions:
            self._collide()

        if self.use_powerups:
            self.powerups.tick(self, self.dt)

        for c in self.cars:
            self._progress(c)
            self._stuck(c)

        self._rank()
        self._finish_check()

    # -------------------------------------------------------------- collisions

    def _collide(self):
        """Две окружности на машину (нос и корма) — этого хватает, чтобы
        «встал поперёк» отличалось от «догнал сзади». Разрешается только самое
        глубокое касание в паре: иначе импульс применится дважды и машины
        разлетятся."""
        cars = self.cars
        r = self.car_radius
        rr = (2.0 * r) ** 2
        rest = self.CC["restitution"]
        sep = self.CC["separation"]
        kick = self.CC["spinKick"]
        loss = self.CC["speedLoss"]
        n = len(cars)

        for i in range(n):
            A = cars[i]
            ca, sa = math.cos(A.a), math.sin(A.a)
            apts = ((A.x + ca * self.axle, A.y + sa * self.axle),
                    (A.x - ca * self.axle, A.y - sa * self.axle))
            for j in range(i + 1, n):
                B = cars[j]
                if (A.x - B.x) ** 2 + (A.y - B.y) ** 2 > (2.0 * (r + self.axle)) ** 2:
                    continue
                cb, sb = math.cos(B.a), math.sin(B.a)
                bpts = ((B.x + cb * self.axle, B.y + sb * self.axle),
                        (B.x - cb * self.axle, B.y - sb * self.axle))

                best = None
                for pa in apts:
                    for pb in bpts:
                        dx = pb[0] - pa[0]
                        dy = pb[1] - pa[1]
                        d2 = dx * dx + dy * dy
                        if d2 < rr and (best is None or d2 < best[0]):
                            best = (d2, dx, dy, pa, pb)
                if best is None:
                    continue

                d2, dx, dy, pa, pb = best
                d = math.sqrt(d2)
                if d < 1e-6:
                    dx, dy, d = 1.0, 0.0, 1.0
                nx, ny = dx / d, dy / d
                overlap = 2.0 * r - d

                push = overlap * 0.5 * sep
                A.x -= nx * push
                A.y -= ny * push
                B.x += nx * push
                B.y += ny * push

                rvx = B.vx - A.vx
                rvy = B.vy - A.vy
                vn = rvx * nx + rvy * ny
                if vn >= 0.0:
                    continue

                imp = -(1.0 + rest) * vn * 0.5
                A.vx -= imp * nx
                A.vy -= imp * ny
                B.vx += imp * nx
                B.vy += imp * ny

                f = 1.0 - loss
                A.vx *= f
                A.vy *= f
                B.vx *= f
                B.vy *= f

                # Разворачивающий момент: удар в нос крутит не так, как в борт
                A.a -= (( pa[0] - A.x) * ny - (pa[1] - A.y) * nx) * imp * kick * 1e-4
                B.a += (( pb[0] - B.x) * ny - (pb[1] - B.y) * nx) * imp * kick * 1e-4

                if -vn > 120.0:
                    A.hit_flash = self.tick
                    B.hit_flash = self.tick
                    self.event("bump", a=A.slot, b=B.slot,
                               x=round((pa[0] + pb[0]) * 0.5, 1),
                               y=round((pa[1] + pb[1]) * 0.5, 1),
                               f=round(-vn, 1))

    # ---------------------------------------------------------------- progress

    def _progress(self, c):
        tr = self.track
        q = tr.query(c.x, c.y, c.seg)
        c.seg = q[0]
        prog = tr.progress(q[0], q[1])
        L = tr.length

        # Чекпоинты: круг засчитывается, только если пройдены все по порядку —
        # иначе можно было бы срезать через траву и получить круг даром.
        cps = tr.checkpoints
        if c.next_cp < len(cps):
            target = tr.cum[cps[c.next_cp]]
            if c.last_prog <= target <= prog:
                c.next_cp += 1

        d = prog - c.last_prog
        if d < -L * 0.5:
            # пересекли линию старта вперёд
            if c.next_cp >= len(cps):
                lap_ms = int((self.tick - c.lap_start_tick) * 1000 / self.hz)
                if lap_ms >= self.min_lap_ms:
                    c.lap += 1
                    c.next_cp = 1
                    c.prev_lap_start = c.lap_start_tick
                    c.lap_start_tick = self.tick
                    c.lap_times.append(lap_ms)
                    if c.best_lap is None or lap_ms < c.best_lap:
                        c.best_lap = lap_ms
                    self.event("lap", slot=c.slot, lap=c.lap, ms=lap_ms)
        elif d > L * 0.5:
            # Поехали назад через линию: круг снимаем И возвращаем отсчёт
            # времени. Иначе машина, которую качнуло на линии, при следующем
            # пересечении получала бы круг, засчитанный за считаные секунды.
            if c.lap > 0:
                c.lap -= 1
                c.next_cp = len(cps)
                c.lap_start_tick = c.prev_lap_start
                if c.lap_times:
                    c.lap_times.pop()
                    c.best_lap = min(c.lap_times) if c.lap_times else None

        c.last_prog = prog
        c.total_prog = c.lap * L + prog

    def _stuck(self, c):
        """Вылетел и стоит — вернём на трассу. Едет — пусть выбирается сам."""
        if c.finished or self.state != "racing" or self.tick < c.freeze_until:
            c.stuck_ticks = 0
            return
        rc = self.phys["respawn"]
        sp2 = c.vx * c.vx + c.vy * c.vy
        q = self.track.query(c.x, c.y, c.seg)
        off_road = abs(q[2]) > q[3]
        if off_road and sp2 < rc["stuckSpeed"] ** 2:
            c.stuck_ticks += 1
            if c.stuck_ticks >= int(rc["stuckSeconds"] * self.hz):
                self._respawn(c)
        else:
            c.stuck_ticks = 0

    def _respawn(self, c):
        tr = self.track
        # На последний пройденный чекпоинт, по центру дороги, носом вперёд
        idx = tr.checkpoints[max(0, c.next_cp - 1) % len(tr.checkpoints)]
        a, b = idx, (idx + 1) % tr.n
        dx = tr.px[b] - tr.px[a]
        dy = tr.py[b] - tr.py[a]
        c.x, c.y = tr.px[a], tr.py[a]
        c.a = math.atan2(dy, dx)
        c.vx = c.vy = 0.0
        c.seg = idx
        c.stuck_ticks = 0
        c.respawns += 1
        c.effects = []
        c.freeze_until = self.tick + int(RESPAWN_FREEZE * self.hz)
        q = tr.query(c.x, c.y, c.seg)
        c.last_prog = tr.progress(q[0], q[1])
        c.total_prog = c.lap * tr.length + c.last_prog
        self.event("respawn", slot=c.slot)

    # ------------------------------------------------------------------- rank

    def _rank(self):
        order = sorted(self.cars, key=lambda c: (
            0 if c.finished else 1,
            c.finish_tick if c.finish_tick is not None else 0,
            -c.total_prog,
        ))
        for i, c in enumerate(order):
            c.rank = i
        self.order = order

    def _finish_check(self):
        for c in self.cars:
            if not c.finished and c.lap >= self.laps:
                c.finished = True
                c.finish_tick = self.tick
                if self.leader_finish_tick is None:
                    self.leader_finish_tick = self.tick
                self.event("finish", slot=c.slot, place=c.rank + 1,
                           ms=int((self.tick - self.start_tick) * 1000 / self.hz))

        if self.state != "racing":
            return
        if all(c.finished for c in self.cars):
            self.state = "done"
        elif (self.leader_finish_tick is not None
              and self.tick - self.leader_finish_tick > GRACE_AFTER_LEADER * self.hz):
            self.state = "done"
        elif self.elapsed() > self.time_limit:
            self.state = "done"
        if self.state == "done":
            self.event("over")

    # --------------------------------------------------------------- snapshot

    def snapshot(self):
        cars = []
        for c in self.cars:
            eff = self.effects_for(c)
            row = [c.slot, round(c.x, 3), round(c.y, 3), round(c.a, 5),
                   round(c.vx, 3), round(c.vy, 3), c.seg, c.lap, c.applied_tick,
                   c.item or 0, c.slack]
            if eff != [1.0, 1.0, 1.0, 1.0, 0.0, 1.0] or c.shield_until > self.tick:
                row.append(eff)
                row.append(1 if c.shield_until > self.tick else 0)
            cars.append(row)

        ents = [[e.eid, e.kind, round(e.x, 1), round(e.y, 1), round(e.a, 3), e.item]
                for e in self.entities]
        boxes = [i for i, b in enumerate(self.boxes) if b["until"] > self.tick]

        snap = {
            "t": "s",
            "k": self.tick,
            "st": self.state,
            "el": round(self.elapsed(), 2),
            "c": cars,
            "e": ents,
            "bx": boxes,
            "r": [c.slot for c in self.order],
        }
        if self.events:
            snap["ev"] = self.events
            self.events = []
        return snap

    def results(self):
        rows = []
        for c in self.order:
            rows.append({
                "slot": c.slot,
                "name": c.name,
                "color": c.color,
                "finished": c.finished,
                "laps": c.lap,
                "ms": (int((c.finish_tick - self.start_tick) * 1000 / self.hz)
                       if c.finish_tick is not None else None),
                "best": c.best_lap,
                "respawns": c.respawns,
            })
        return rows
