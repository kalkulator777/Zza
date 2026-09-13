// Геометрия трассы на клиенте.
//
// Ломаную считает сервер и присылает готовой — спрямляющего кода здесь нет.
// Зеркалятся только две вещи, нужные для предсказания своей машины:
//   query()       ЗЕРКАЛО: Track.query       в server/track.py
//   resolveWall() ЗЕРКАЛО: Track.resolve_wall в server/track.py

const QUERY_WINDOW = 14;

/** Остаток как в Python: результат всегда неотрицательный.
 *  В JS (-3 % 10) === -3, в Python 7. Без этого оконный поиск возле нулевого
 *  сегмента уезжал бы у клиента в другую сторону, чем у сервера. */
function imod(a, n) {
  return ((a % n) + n) % n;
}

export class TrackGeom {
  constructor(payload) {
    this.id = payload.id;
    this.name = payload.name;
    this.author = payload.author;
    this.theme = payload.theme;
    this.closed = payload.closed;
    this.laps = payload.laps;
    this.length = payload.length;
    this.shoulder = payload.shoulder;
    this.offSurface = payload.offSurface;
    this.px = payload.px;
    this.py = payload.py;
    this.hw = payload.hw;
    this.surf = payload.surf;
    this.checkpoints = payload.checkpoints;
    this.decor = payload.decor || [];
    this.bounds = payload.bounds;

    this.n = this.px.length;
    this.nseg = this.closed ? this.n : this.n - 1;

    // Длины сегментов — для прогресса и для отбора видимого куска дороги.
    this.seglen = new Float64Array(this.nseg);
    this.cum = new Float64Array(this.nseg + 1);
    for (let i = 0; i < this.nseg; i++) {
      const a = i, b = (i + 1) % this.n;
      const dx = this.px[b] - this.px[a];
      const dy = this.py[b] - this.py[a];
      this.seglen[i] = Math.sqrt(dx * dx + dy * dy);
      this.cum[i + 1] = this.cum[i] + this.seglen[i];
    }

    this._buildEdges();
  }

  /** Кромки дороги и обочины считаем один раз на загрузке.
   *  В кадре остаётся только собрать путь из готовых точек — иначе на каждый
   *  кадр пришлось бы пересчитывать нормали для сотен точек. */
  _buildEdges() {
    const n = this.n, sh = this.shoulder;
    this.lx = new Float64Array(n); this.ly = new Float64Array(n);
    this.rx = new Float64Array(n); this.ry = new Float64Array(n);
    this.slx = new Float64Array(n); this.sly = new Float64Array(n);
    this.srx = new Float64Array(n); this.sry = new Float64Array(n);
    this.nx = new Float64Array(n); this.ny = new Float64Array(n);
    // Габарит сегмента вместе с обочиной — для отбрасывания невидимого
    this.segMinX = new Float64Array(this.nseg);
    this.segMinY = new Float64Array(this.nseg);
    this.segMaxX = new Float64Array(this.nseg);
    this.segMaxY = new Float64Array(this.nseg);

    for (let i = 0; i < n; i++) {
      // Нормаль по усреднённому направлению соседних сегментов — на стыках
      // кромка не ломается углом.
      const p = this.closed ? (i - 1 + n) % n : Math.max(0, i - 1);
      const q = this.closed ? (i + 1) % n : Math.min(n - 1, i + 1);
      let dx = this.px[q] - this.px[p];
      let dy = this.py[q] - this.py[p];
      const dl = Math.sqrt(dx * dx + dy * dy) || 1;
      dx /= dl; dy /= dl;
      const nx = -dy, ny = dx;
      this.nx[i] = nx; this.ny[i] = ny;
      const w = this.hw[i], ws = w + sh;
      this.lx[i] = this.px[i] + nx * w;  this.ly[i] = this.py[i] + ny * w;
      this.rx[i] = this.px[i] - nx * w;  this.ry[i] = this.py[i] - ny * w;
      this.slx[i] = this.px[i] + nx * ws; this.sly[i] = this.py[i] + ny * ws;
      this.srx[i] = this.px[i] - nx * ws; this.sry[i] = this.py[i] - ny * ws;
    }

    for (let i = 0; i < this.nseg; i++) {
      const a = i, b = (i + 1) % n;
      const r = Math.max(this.hw[a], this.hw[b]) + sh;
      this.segMinX[i] = Math.min(this.px[a], this.px[b]) - r;
      this.segMaxX[i] = Math.max(this.px[a], this.px[b]) + r;
      this.segMinY[i] = Math.min(this.py[a], this.py[b]) - r;
      this.segMaxY[i] = Math.max(this.py[a], this.py[b]) + r;
    }
  }

  /** Ближайший сегмент в окне вокруг подсказки.
   *  Возвращает [seg, t, lateralSigned, halfw, surface, qx, qy]. */
  query(x, y, hint) {
    const n = this.n, nseg = this.nseg;
    const px = this.px, py = this.py, hw = this.hw;

    let bestI = imod(hint, nseg), bestT = 0.0, bestD = Infinity;
    let bestQx = 0.0, bestQy = 0.0;

    for (let k = -QUERY_WINDOW; k <= QUERY_WINDOW; k++) {
      let i = hint + k;
      if (this.closed) {
        i = imod(i, nseg);
      } else if (i < 0 || i >= nseg) {
        continue;
      }
      const a = i, b = (i + 1) % n;
      const dx = px[b] - px[a];
      const dy = py[b] - py[a];
      const l2 = dx * dx + dy * dy;
      let t;
      if (l2 <= 0.0) {
        t = 0.0;
      } else {
        t = ((x - px[a]) * dx + (y - py[a]) * dy) / l2;
        if (t < 0.0) t = 0.0;
        else if (t > 1.0) t = 1.0;
      }
      const qx = px[a] + dx * t;
      const qy = py[a] + dy * t;
      const ex = x - qx;
      const ey = y - qy;
      const d = ex * ex + ey * ey;
      if (d < bestD) {
        bestD = d; bestI = i; bestT = t;
        bestQx = qx; bestQy = qy;
      }
    }

    const a = bestI, b = (bestI + 1) % n;
    const dx = px[b] - px[a];
    const dy = py[b] - py[a];
    let lat = Math.sqrt(bestD);
    if (dx * (y - py[a]) - dy * (x - px[a]) < 0.0) {
      lat = -lat;
    }
    const halfw = hw[a] + (hw[b] - hw[a]) * bestT;
    const sfc = Math.abs(lat) <= halfw ? this.surf[a] : this.offSurface;
    return [bestI, bestT, lat, halfw, sfc, bestQx, bestQy];
  }

  /** Выталкивает машину обратно за границу обочины и гасит скорость.
   *  Возвращает силу удара (0 — стены не было). */
  resolveWall(car, lat, halfw, qx, qy, CC) {
    const limit = halfw + this.shoulder;
    const alat = lat >= 0.0 ? lat : -lat;
    if (alat <= limit) return 0.0;

    const ex = car.x - qx;
    const ey = car.y - qy;
    const el = Math.sqrt(ex * ex + ey * ey);
    if (el < 1e-9) return 0.0;
    const nx = ex / el;
    const ny = ey / el;

    const push = alat - limit;
    car.x = car.x - nx * push;
    car.y = car.y - ny * push;

    const vn = car.vx * nx + car.vy * ny;
    if (vn <= 0.0) return 0.0;

    const rest = CC.wallRestitution;
    const k = (1.0 + rest) * vn;
    car.vx = car.vx - k * nx;
    car.vy = car.vy - k * ny;

    let loss = CC.wallSpeedLoss * (vn / 260.0);
    if (loss > CC.wallSpeedLoss) loss = CC.wallSpeedLoss;
    const f = 1.0 - loss;
    car.vx = car.vx * f;
    car.vy = car.vy * f;
    return vn;
  }

  progress(seg, t) {
    return this.cum[seg] + this.seglen[seg] * t;
  }
}
