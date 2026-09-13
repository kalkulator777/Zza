"""Геометрия трассы.

Трасса в JSON — это опорные точки с шириной и покрытием. Здесь они
разворачиваются в равномерную ломаную: по ней считаются и коллизии, и прогресс
по кругу, и по ней же клиент рисует дорогу. Одна геометрия на всё — картинка и
физика не могут разъехаться в принципе.

Разворачивает только сервер, клиенту уходит готовая ломаная. Спрямляющего кода
на JS нет вообще — значит нет и класса багов «сплайн посчитался чуть иначе».

Оконный поиск ближайшего сегмента и отталкивание от стены ЗЕРКАЛЯТСЯ в
static/js/game/trackgeom.js: клиент предсказывает свою машину против статики.
"""

import json
import math
import os

from shared.carstep import SURFACE_ORDER

# Округление ломаной. Сервер сам пользуется округлёнными значениями и их же
# отдаёт клиенту — то есть у обоих ровно одни и те же числа.
ROUND = 3

SPLINE_SUBSTEPS = 24
GRID_CELL = 256.0
QUERY_WINDOW = 14


def _lerp(a, b, t):
    return a + (b - a) * t


def _catmull_rom(p0, p1, p2, p3, t0, t1, t2, t3, t):
    """Центростремительный Catmull-Rom (alpha=0.5) — не даёт петель на резких
    поворотах, в отличие от равномерного."""
    a1x = (t1 - t) / (t1 - t0) * p0[0] + (t - t0) / (t1 - t0) * p1[0]
    a1y = (t1 - t) / (t1 - t0) * p0[1] + (t - t0) / (t1 - t0) * p1[1]
    a2x = (t2 - t) / (t2 - t1) * p1[0] + (t - t1) / (t2 - t1) * p2[0]
    a2y = (t2 - t) / (t2 - t1) * p1[1] + (t - t1) / (t2 - t1) * p2[1]
    a3x = (t3 - t) / (t3 - t2) * p2[0] + (t - t2) / (t3 - t2) * p3[0]
    a3y = (t3 - t) / (t3 - t2) * p2[1] + (t - t2) / (t3 - t2) * p3[1]

    b1x = (t2 - t) / (t2 - t0) * a1x + (t - t0) / (t2 - t0) * a2x
    b1y = (t2 - t) / (t2 - t0) * a1y + (t - t0) / (t2 - t0) * a2y
    b2x = (t3 - t) / (t3 - t1) * a2x + (t - t1) / (t3 - t1) * a3x
    b2y = (t3 - t) / (t3 - t1) * a2y + (t - t1) / (t3 - t1) * a3y

    cx = (t2 - t) / (t2 - t1) * b1x + (t - t1) / (t2 - t1) * b2x
    cy = (t2 - t) / (t2 - t1) * b1y + (t - t1) / (t2 - t1) * b2y
    return cx, cy


class Track:
    def __init__(self, data):
        self.id = data["id"]
        self.name = data.get("name", data["id"])
        self.author = data.get("author", "")
        self.theme = data.get("theme", "day")
        self.laps = int(data.get("laps", 3))
        self.closed = bool(data.get("closed", True))
        self.spacing = float(data.get("spacing", 28.0))
        self.off_surface = SURFACE_ORDER.index(data.get("offSurface", "grass"))
        self.shoulder = float(data.get("shoulder", 78.0))
        self.decor = data.get("decor", [])
        self.raw_boxes = data.get("boxes", [])
        self.difficulty = data.get("difficulty", "")

        nodes = self._dedupe(data["nodes"])
        if len(nodes) < 4:
            raise ValueError(f"трасса {self.id}: нужно минимум 4 опорные точки")

        self._build(nodes)
        self._build_grid()
        self._build_checkpoints(data.get("checkpoints", 8))
        self._build_boxes()
        self._payload = None

    # ------------------------------------------------------------------ build

    @staticmethod
    def _dedupe(nodes):
        out = []
        for nd in nodes:
            if out:
                dx = nd["x"] - out[-1]["x"]
                dy = nd["y"] - out[-1]["y"]
                if dx * dx + dy * dy < 1.0:
                    continue
            out.append(nd)
        return out

    def _build(self, nodes):
        n = len(nodes)
        dense = []

        def node(i):
            if self.closed:
                return nodes[i % n]
            return nodes[max(0, min(n - 1, i))]

        spans = n if self.closed else n - 1
        for i in range(spans):
            p0, p1 = node(i - 1), node(i)
            p2, p3 = node(i + 1), node(i + 2)
            pts = [(p["x"], p["y"]) for p in (p0, p1, p2, p3)]

            # Центростремительная параметризация
            ts = [0.0]
            for k in range(3):
                dx = pts[k + 1][0] - pts[k][0]
                dy = pts[k + 1][1] - pts[k][1]
                d = math.sqrt(math.sqrt(dx * dx + dy * dy))
                ts.append(ts[-1] + max(d, 1e-6))

            for s in range(SPLINE_SUBSTEPS):
                u = s / SPLINE_SUBSTEPS
                t = _lerp(ts[1], ts[2], u)
                x, y = _catmull_rom(pts[0], pts[1], pts[2], pts[3], ts[0], ts[1], ts[2], ts[3], t)
                w = _lerp(p1.get("w", 140.0), p2.get("w", 140.0), u)
                sf = p1.get("s", "road") if u < 0.5 else p2.get("s", "road")
                dense.append((x, y, w, SURFACE_ORDER.index(sf)))

        if not self.closed:
            last = nodes[-1]
            dense.append((last["x"], last["y"], last.get("w", 140.0),
                          SURFACE_ORDER.index(last.get("s", "road"))))

        self._resample(dense)

    def _resample(self, dense):
        """Равномерная ломаная — чтобы шаг поиска и прогресс были предсказуемы."""
        m = len(dense)
        seglen = []
        total = 0.0
        count = m if self.closed else m - 1
        for i in range(count):
            a, b = dense[i], dense[(i + 1) % m]
            d = math.hypot(b[0] - a[0], b[1] - a[1])
            seglen.append(d)
            total += d

        target = max(8, int(round(total / self.spacing)))
        stepd = total / target

        px, py, hw, sf = [], [], [], []
        di, acc = 0, 0.0
        for k in range(target):
            want = k * stepd
            while di < count - 1 and acc + seglen[di] < want:
                acc += seglen[di]
                di += 1
            t = 0.0 if seglen[di] <= 0.0 else (want - acc) / seglen[di]
            if t > 1.0:
                t = 1.0
            a, b = dense[di], dense[(di + 1) % m]
            px.append(round(_lerp(a[0], b[0], t), ROUND))
            py.append(round(_lerp(a[1], b[1], t), ROUND))
            hw.append(round(_lerp(a[2], b[2], t) * 0.5, ROUND))
            sf.append(a[3])

        if not self.closed:
            last = dense[-1]
            px.append(round(last[0], ROUND))
            py.append(round(last[1], ROUND))
            hw.append(round(last[2] * 0.5, ROUND))
            sf.append(last[3])

        self.px, self.py, self.hw, self.surf = px, py, hw, sf
        self.n = len(px)
        self.nseg = self.n if self.closed else self.n - 1

        # Длины сегментов и накопленная длина считаются ПОСЛЕ округления, иначе
        # прогресс по трассе разошёлся бы с тем, что видит клиент.
        self.seglen = []
        self.cum = [0.0]
        for i in range(self.nseg):
            a = i
            b = (i + 1) % self.n
            d = math.hypot(px[b] - px[a], py[b] - py[a])
            self.seglen.append(d)
            self.cum.append(self.cum[-1] + d)
        self.length = self.cum[-1]

    def _build_grid(self):
        xs, ys = self.px, self.py
        self.minx, self.maxx = min(xs), max(xs)
        self.miny, self.maxy = min(ys), max(ys)
        pad = self.shoulder + 400.0
        self.minx -= pad
        self.miny -= pad
        self.maxx += pad
        self.maxy += pad
        self.gw = max(1, int((self.maxx - self.minx) / GRID_CELL) + 1)
        self.gh = max(1, int((self.maxy - self.miny) / GRID_CELL) + 1)
        self.grid = [[] for _ in range(self.gw * self.gh)]
        for i in range(self.nseg):
            a, b = i, (i + 1) % self.n
            x0, x1 = sorted((self.px[a], self.px[b]))
            y0, y1 = sorted((self.py[a], self.py[b]))
            r = self.hw[a] + self.shoulder
            cx0 = int((x0 - r - self.minx) / GRID_CELL)
            cx1 = int((x1 + r - self.minx) / GRID_CELL)
            cy0 = int((y0 - r - self.miny) / GRID_CELL)
            cy1 = int((y1 + r - self.miny) / GRID_CELL)
            for cy in range(max(0, cy0), min(self.gh - 1, cy1) + 1):
                for cx in range(max(0, cx0), min(self.gw - 1, cx1) + 1):
                    self.grid[cy * self.gw + cx].append(i)

    def _build_checkpoints(self, spec):
        """Чекпоинты равномерно по длине круга. Нужны, чтобы нельзя было
        срезать: круг засчитывается только если пройдены все по порядку."""
        if isinstance(spec, list):
            raw = [int(v) % self.nseg for v in spec]
        else:
            count = max(3, min(int(spec), self.nseg // 4))
            raw = [int(round(k * self.nseg / count)) % self.nseg for k in range(count)]
        # Совпавшие чекпоинты сломали бы подсчёт кругов: один и тот же индекс
        # нельзя пройти дважды, и круг перестал бы засчитываться.
        self.checkpoints = sorted(set(raw))
        if self.checkpoints[0] != 0:
            self.checkpoints.insert(0, 0)

    def _build_boxes(self):
        """Боксы с бонусами: либо заданы явно, либо расставляются рядами поперёк
        дороги через равные промежутки."""
        boxes = []
        if self.raw_boxes:
            for b in self.raw_boxes:
                boxes.append({"x": float(b["x"]), "y": float(b["y"])})
        else:
            every = 1400.0
            count = max(1, int(self.length / every))
            for k in range(count):
                s = (k + 0.5) * self.length / count
                i = self.seg_at_length(s)
                a, b = i, (i + 1) % self.n
                dx = self.px[b] - self.px[a]
                dy = self.py[b] - self.py[a]
                dl = math.hypot(dx, dy) or 1.0
                nx, ny = -dy / dl, dx / dl
                # off — доля ПОЛУширины, не ширины: с удвоением боксы уезжали
                # на обочину и их нельзя было подобрать, не вылетев с трассы.
                w = self.hw[a]
                for off in (-0.5, 0.0, 0.5):
                    boxes.append({"x": self.px[a] + nx * w * off,
                                  "y": self.py[a] + ny * w * off})
        self.boxes = boxes

    # ------------------------------------------------------------------ query

    def seg_at_length(self, s):
        lo, hi = 0, self.nseg - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.cum[mid] <= s:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def query_global(self, x, y):
        """Полный поиск ближайшего сегмента через сетку. Только для респавна и
        расстановки на старте — в тике используется оконный поиск."""
        cx = int((x - self.minx) / GRID_CELL)
        cy = int((y - self.miny) / GRID_CELL)
        cand = []
        for r in range(1, 6):
            for j in range(max(0, cy - r), min(self.gh - 1, cy + r) + 1):
                for i in range(max(0, cx - r), min(self.gw - 1, cx + r) + 1):
                    cand.extend(self.grid[j * self.gw + i])
            if cand:
                break
        if not cand:
            cand = range(self.nseg)

        best, bd = 0, float("inf")
        for i in set(cand):
            a, b = i, (i + 1) % self.n
            dx = self.px[b] - self.px[a]
            dy = self.py[b] - self.py[a]
            l2 = dx * dx + dy * dy
            t = 0.0 if l2 <= 0.0 else ((x - self.px[a]) * dx + (y - self.py[a]) * dy) / l2
            t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
            qx = self.px[a] + dx * t
            qy = self.py[a] + dy * t
            d = (x - qx) ** 2 + (y - qy) ** 2
            if d < bd:
                bd, best = d, i
        return best

    def query(self, x, y, hint):
        """Ближайший сегмент в окне вокруг подсказки.

        ЗЕРКАЛО: trackQuery в static/js/game/trackgeom.js.
        Возвращает (seg, t, lateral_signed, halfw, surface, cx, cy).
        """
        n, nseg = self.n, self.nseg
        px, py, hw = self.px, self.py, self.hw

        best_i, best_t, best_d = hint % nseg, 0.0, float("inf")
        best_qx, best_qy = 0.0, 0.0

        for k in range(-QUERY_WINDOW, QUERY_WINDOW + 1):
            i = hint + k
            if self.closed:
                i %= nseg
            elif i < 0 or i >= nseg:
                continue
            a, b = i, (i + 1) % n
            dx = px[b] - px[a]
            dy = py[b] - py[a]
            l2 = dx * dx + dy * dy
            if l2 <= 0.0:
                t = 0.0
            else:
                t = ((x - px[a]) * dx + (y - py[a]) * dy) / l2
                if t < 0.0:
                    t = 0.0
                elif t > 1.0:
                    t = 1.0
            qx = px[a] + dx * t
            qy = py[a] + dy * t
            ex = x - qx
            ey = y - qy
            d = ex * ex + ey * ey
            if d < best_d:
                best_d, best_i, best_t = d, i, t
                best_qx, best_qy = qx, qy

        a = best_i
        b = (best_i + 1) % n
        dx = px[b] - px[a]
        dy = py[b] - py[a]
        lat = math.sqrt(best_d)
        # Знак: слева от направления движения или справа
        if dx * (y - py[a]) - dy * (x - px[a]) < 0.0:
            lat = -lat
        halfw = hw[a] + (hw[b] - hw[a]) * best_t
        sfc = self.surf[a] if abs(lat) <= halfw else self.off_surface
        return best_i, best_t, lat, halfw, sfc, best_qx, best_qy

    def progress(self, seg, t):
        return self.cum[seg] + self.seglen[seg] * t

    # ------------------------------------------------------------------ walls

    def resolve_wall(self, car, lat, halfw, qx, qy, CC):
        """Выталкивает машину обратно за границу обочины и гасит скорость.

        ЗЕРКАЛО: resolveWall в static/js/game/trackgeom.js.
        Возвращает силу удара (0 — стены не было), её использует клиент для
        тряски экрана, а сервер — для звука.
        """
        limit = halfw + self.shoulder
        alat = lat if lat >= 0.0 else -lat
        if alat <= limit:
            return 0.0

        # Наружная нормаль — от центра дороги к машине
        ex = car.x - qx
        ey = car.y - qy
        el = math.sqrt(ex * ex + ey * ey)
        if el < 1e-9:
            return 0.0
        nx = ex / el
        ny = ey / el

        push = alat - limit
        car.x = car.x - nx * push
        car.y = car.y - ny * push

        vn = car.vx * nx + car.vy * ny
        if vn <= 0.0:
            return 0.0

        rest = CC["wallRestitution"]
        k = (1.0 + rest) * vn
        car.vx = car.vx - k * nx
        car.vy = car.vy - k * ny

        # Продольная составляющая теряется тем сильнее, чем прямее удар.
        loss = CC["wallSpeedLoss"] * (vn / 260.0)
        if loss > CC["wallSpeedLoss"]:
            loss = CC["wallSpeedLoss"]
        f = 1.0 - loss
        car.vx = car.vx * f
        car.vy = car.vy * f
        return vn

    # ---------------------------------------------------------------- payload

    def start_slots(self, count):
        """Стартовая решётка: попарно, со смещением назад от линии старта."""
        slots = []
        row_gap = 62.0
        side = 0.30
        back = 120.0
        for k in range(count):
            row = k // 2
            col = -1 if (k % 2 == 0) else 1
            s = (-back - row * row_gap) % self.length
            i = self.seg_at_length(s)
            a, b = i, (i + 1) % self.n
            dx = self.px[b] - self.px[a]
            dy = self.py[b] - self.py[a]
            dl = math.hypot(dx, dy) or 1.0
            ux, uy = dx / dl, dy / dl
            nx, ny = -uy, ux
            off = self.hw[a] * side * col
            slots.append({
                "x": self.px[a] + nx * off,
                "y": self.py[a] + ny * off,
                "a": math.atan2(uy, ux),
                "seg": i,
            })
        return slots

    def client_payload(self):
        """То, что уходит клиенту. Считается один раз и кэшируется строкой:
        сериализовать ~1000 точек на каждое подключение незачем."""
        if self._payload is None:
            self._payload = {
                "id": self.id,
                "name": self.name,
                "author": self.author,
                "theme": self.theme,
                "closed": self.closed,
                "laps": self.laps,
                "length": round(self.length, 2),
                "shoulder": self.shoulder,
                "offSurface": self.off_surface,
                "px": self.px,
                "py": self.py,
                "hw": self.hw,
                "surf": self.surf,
                "checkpoints": self.checkpoints,
                "decor": self.decor,
                "bounds": [round(self.minx, 1), round(self.miny, 1),
                           round(self.maxx, 1), round(self.maxy, 1)],
            }
        return self._payload


def load_tracks(directory):
    tracks = {}
    errors = []
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(directory, fn)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            t = Track(data)
            tracks[t.id] = t
        except Exception as exc:  # noqa: BLE001 — битая трасса не должна ронять сервер
            errors.append(f"{fn}: {exc}")
    return tracks, errors
