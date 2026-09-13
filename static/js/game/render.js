// Рендер.
//
// Трасса рисуется процедурно каждый кадр из той же ломаной, по которой
// считается физика. Пре-рендера в большой битмап нет намеренно: круг на
// 3-5 минут это десятки тысяч пикселей длины, такая картинка заняла бы сотни
// мегабайт. Вместо неё — несколько заливок полигонов по видимым сегментам.
// Побочная выгода важнее экономии: картинка и коллизии строятся из одних и
// тех же точек и разъехаться не могут.
//
// Бюджет кадра на слабой машине небольшой, поэтому: кромки дороги посчитаны
// заранее, подписи и спрайты нарисованы заранее, текста в цикле нет, и есть
// автоматическое снижение качества, если кадр перестаёт укладываться.

import { makeCarSprite, makeDecorSprite, makeLabel, makeGrassTile } from './sprites.js';

const THEMES = {
  day:    { road: '#3a3f4b', road2: '#454b59', edge: '#e8ecf4', shoulder: '#4a4230',
            sand: '#b09256', ice: '#a8d8e8', boost: '#ffb03a', sky: '#1f3b23' },
  night:  { road: '#2b3140', road2: '#343b4d', edge: '#9fb0c8', shoulder: '#332e26',
            sand: '#7e6a45', ice: '#7fa8bd', boost: '#ff9a2a', sky: '#131d2a' },
  desert: { road: '#4a4438', road2: '#565043', edge: '#f0e6cf', shoulder: '#7a663d',
            sand: '#c9a86a', ice: '#a8d8e8', boost: '#ffb03a', sky: '#6b5a36' },
  snow:   { road: '#4b515e', road2: '#565d6c', edge: '#ffffff', shoulder: '#8fa3ad',
            sand: '#b8c4cc', ice: '#dff1f8', boost: '#ffb03a', sky: '#dfe8ee' },
};

const SURF_COLOR = ['road', 'sky', 'sand', 'ice', 'road', 'road'];
const SURF_BOOST = 4;
const MAX_PARTICLES = [0, 60, 150];

export class Renderer {
  constructor(canvas, phys) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d', { alpha: false });
    this.cam = phys.camera;
    this.C = phys.car;
    this.quality = 2;
    this.autoQuality = true;
    this.track = null;
    this.theme = THEMES.day;

    this.camX = 0; this.camY = 0; this.scale = 1; this.ready = false;
    this.parts = [];
    this.carSprites = new Map();
    this.decorSprites = new Map();
    this.labels = new Map();

    this.frameMs = 0; this.frameP95 = 0; this._times = [];
    this._slow = 0;

    this.resize();
    addEventListener('resize', () => this.resize());
    // Следим за самим холстом, а не только за окном. Пока экран заезда скрыт,
    // его размер нулевой, и resize() по событию окна не помог бы: холст остался
    // бы в размере по умолчанию 300x150, а CSS растянул бы его на весь экран.
    // Картинка была бы мутной, а замеры времени кадра — заниженными в разы.
    if (typeof ResizeObserver !== 'undefined') {
      this._ro = new ResizeObserver(() => this.resize());
      this._ro.observe(this.cv);
    }
  }

  resize() {
    // Рисуем в нативном разрешении: ускорение Canvas2D включено, и
    // масштабирование заметно мылит. Если качество придётся ронять,
    // уменьшаем именно здесь.
    const dpr = this.quality >= 2 ? Math.min(devicePixelRatio || 1, 2) : 1;
    const w = Math.round(this.cv.clientWidth * dpr);
    const h = Math.round(this.cv.clientHeight * dpr);
    if (w && h && (this.cv.width !== w || this.cv.height !== h)) {
      this.cv.width = w;
      this.cv.height = h;
    }
  }

  /** Холст ещё не получил настоящий размер — рисовать рано. */
  get sized() {
    return this.cv.width > 320 || this.cv.clientWidth === 0;
  }

  setTrack(track) {
    this.track = track;
    this.theme = THEMES[track.theme] || THEMES.day;
    this.decorSprites.clear();
    this.labels.clear();
    this.parts.length = 0;
    this.grass = makeGrassTile(track.theme || 'day');
    this.grassPat = this.ctx.createPattern(this.grass, 'repeat');
    this._buildDecorGrid();
    this._buildMinimap();
    this.camX = track.px[0];
    this.camY = track.py[0];
    this.ready = true;
  }

  carSprite(color) {
    let s = this.carSprites.get(color);
    if (!s) {
      s = makeCarSprite(color, this.C.length, this.C.width);
      this.carSprites.set(color, s);
    }
    return s;
  }

  label(text, color) {
    const key = text + color;
    let l = this.labels.get(key);
    if (!l) { l = makeLabel(text, color); this.labels.set(key, l); }
    return l;
  }

  _buildDecorGrid() {
    // Декора бывает под сотню на трассу; перебирать весь список каждый кадр
    // незачем, раскладываем по клеткам.
    const cell = 700;
    this.decorCell = cell;
    this.decorGrid = new Map();
    for (const d of this.track.decor) {
      const k = `${Math.floor(d.x / cell)},${Math.floor(d.y / cell)}`;
      let a = this.decorGrid.get(k);
      if (!a) { a = []; this.decorGrid.set(k, a); }
      a.push(d);
    }
  }

  decorSprite(type) {
    let s = this.decorSprites.get(type);
    if (!s) {
      s = makeDecorSprite(type, this.track.theme);
      this.decorSprites.set(type, s);
    }
    return s;
  }

  _buildMinimap() {
    const t = this.track, size = 150, pad = 8;
    const [x0, y0, x1, y1] = t.bounds;
    const sc = Math.min((size - pad * 2) / (x1 - x0), (size - pad * 2) / (y1 - y0));
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const g = c.getContext('2d');
    g.translate(pad, pad);
    g.scale(sc, sc);
    g.translate(-x0, -y0);
    g.lineJoin = g.lineCap = 'round';
    g.strokeStyle = 'rgba(255,255,255,.75)';
    g.lineWidth = 2 / sc * 6;
    g.beginPath();
    g.moveTo(t.px[0], t.py[0]);
    for (let i = 1; i < t.n; i++) g.lineTo(t.px[i], t.py[i]);
    if (t.closed) g.closePath();
    g.stroke();
    this.minimap = { canvas: c, size, sc, x0, y0, pad };
  }

  // ------------------------------------------------------------------ камера

  updateCamera(car, dtFrame) {
    if (!car) return;
    const la = this.cam.lookAhead;
    const tx = car.rx + car.vx * la;
    const ty = car.ry + car.vy * la;
    const k = 1 - Math.exp(-this.cam.smooth * dtFrame);
    this.camX += (tx - this.camX) * k;
    this.camY += (ty - this.camY) * k;
  }

  computeScale() {
    const W = this.cv.width, H = this.cv.height;
    let sc = H / this.cam.worldHeight;
    const worldW = W / sc;
    // Одинаковый обзор для всех: на широком мониторе иначе было бы видно
    // поворот раньше, чем на обычном.
    if (worldW > this.cam.maxWorldWidth) sc = W / this.cam.maxWorldWidth;
    else if (worldW < this.cam.minWorldWidth) sc = W / this.cam.minWorldWidth;
    this.scale = sc;
    return sc;
  }

  // ------------------------------------------------------------------- кадр

  frame(world, dtFrame) {
    const t0 = performance.now();
    const ctx = this.ctx;
    const W = this.cv.width, H = this.cv.height;
    if (!this.ready || !W || !H) return;

    const own = world.cars.get(world.you) || world.cars.values().next().value;
    this.updateCamera(own, dtFrame);
    const sc = this.computeScale();

    const viewW = W / sc, viewH = H / sc;
    const vx0 = this.camX - viewW / 2, vy0 = this.camY - viewH / 2;
    const vx1 = vx0 + viewW, vy1 = vy0 + viewH;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = this.theme.sky;
    ctx.fillRect(0, 0, W, H);

    ctx.setTransform(sc, 0, 0, sc, -vx0 * sc, -vy0 * sc);

    if (this.quality >= 2 && this.grassPat) {
      ctx.fillStyle = this.grassPat;
      ctx.fillRect(vx0, vy0, viewW, viewH);
    }

    this._drawTrack(ctx, vx0, vy0, vx1, vy1);
    if (this.quality >= 1) this._drawDecor(ctx, vx0, vy0, vx1, vy1);
    this._drawBoxes(ctx, world);
    this._drawEntities(ctx, world);
    if (this.quality >= 1) this._drawParticles(ctx, dtFrame);
    this._drawCars(ctx, world);

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    this._drawMinimap(ctx, world, W, H);

    const ms = performance.now() - t0;
    this.frameMs = this.frameMs * 0.9 + ms * 0.1;
    this._times.push(ms);
    if (this._times.length > 120) this._times.shift();
    this._autoQuality(ms);
  }

  _autoQuality(ms) {
    if (!this.autoQuality) return;
    // Замерить игру на боевом железе заранее не вышло, поэтому она
    // подстраивается сама: если кадры перестают укладываться в бюджет,
    // отключаем сначала траву и частицы, потом декор.
    if (ms > 13) {
      if (++this._slow > 45 && this.quality > 0) {
        this.quality--;
        this._slow = 0;
        this.resize();
      }
    } else if (ms < 6) {
      if (--this._slow < -600 && this.quality < 2) {
        this.quality++;
        this._slow = 0;
        this.resize();
      }
    }
  }

  get p95() {
    if (!this._times.length) return 0;
    const s = [...this._times].sort((a, b) => a - b);
    return s[Math.floor(s.length * 0.95)];
  }

  // ------------------------------------------------------------------ трасса

  _drawTrack(ctx, vx0, vy0, vx1, vy1) {
    const t = this.track;
    const runs = [];
    let cur = null;
    for (let i = 0; i < t.nseg; i++) {
      const vis = !(t.segMaxX[i] < vx0 || t.segMinX[i] > vx1 ||
                    t.segMaxY[i] < vy0 || t.segMinY[i] > vy1);
      if (vis) {
        if (cur && cur.surf === t.surf[i] && cur.to === i - 1) cur.to = i;
        else { cur = { surf: t.surf[i], from: i, to: i }; runs.push(cur); }
      } else {
        cur = null;
      }
    }
    if (!runs.length) return;

    // Обочина: широкая подложка. Игрок должен видеть, где кончается
    // «медленно» и начинается «стена».
    ctx.fillStyle = this.theme.shoulder;
    for (const r of runs) this._band(ctx, r.from, r.to, t.slx, t.sly, t.srx, t.sry);

    for (const r of runs) {
      const key = SURF_COLOR[r.surf] || 'road';
      ctx.fillStyle = key === 'sky' ? this.theme.shoulder : this.theme[key];
      this._band(ctx, r.from, r.to, t.lx, t.ly, t.rx, t.ry);
    }

    // Разгонный участок — это асфальт с разметкой, а не оранжевое полотно:
    // сплошная заливка во всю ширину читается как ошибка отрисовки.
    for (const r of runs) {
      if (r.surf !== SURF_BOOST) continue;
      ctx.fillStyle = this.theme.boost;
      ctx.globalAlpha = 0.22;
      this._band(ctx, r.from, r.to, t.lx, t.ly, t.rx, t.ry);
      ctx.globalAlpha = 1;
      this._chevrons(ctx, r.from, r.to);
    }

    ctx.lineWidth = 3;
    ctx.strokeStyle = this.theme.edge;
    ctx.globalAlpha = 0.65;
    for (const r of runs) {
      this._line(ctx, r.from, r.to, t.lx, t.ly);
      this._line(ctx, r.from, r.to, t.rx, t.ry);
    }
    ctx.globalAlpha = 1;

    // Осевая прерывистая — помогает читать направление поворота
    ctx.strokeStyle = 'rgba(255,255,255,.28)';
    ctx.lineWidth = 2.5;
    ctx.setLineDash([26, 26]);
    for (const r of runs) this._line(ctx, r.from, r.to, t.px, t.py);
    ctx.setLineDash([]);

    this._drawStartLine(ctx, vx0, vy0, vx1, vy1);
  }

  _band(ctx, from, to, ax, ay, bx, by) {
    const n = this.track.n;
    ctx.beginPath();
    ctx.moveTo(ax[from], ay[from]);
    for (let i = from + 1; i <= to + 1; i++) ctx.lineTo(ax[i % n], ay[i % n]);
    for (let i = to + 1; i >= from; i--) ctx.lineTo(bx[i % n], by[i % n]);
    ctx.closePath();
    ctx.fill();
  }

  /** Стрелки «сюда, быстрее» вдоль разгонного участка. */
  _chevrons(ctx, from, to) {
    const t = this.track, n = t.n;
    ctx.fillStyle = this.theme.boost;
    for (let i = from; i <= to; i += 5) {
      const a = i % n, b = (i + 1) % n;
      const ux = t.px[b] - t.px[a], uy = t.py[b] - t.py[a];
      const ul = Math.sqrt(ux * ux + uy * uy) || 1;
      const dx = ux / ul, dy = uy / ul;
      const nx = -dy, ny = dx;
      const w = t.hw[a] * 0.5, d = 26;
      ctx.beginPath();
      ctx.moveTo(t.px[a] + dx * d, t.py[a] + dy * d);
      ctx.lineTo(t.px[a] + nx * w, t.py[a] + ny * w);
      ctx.lineTo(t.px[a] + nx * w - dx * 11, t.py[a] + ny * w - dy * 11);
      ctx.lineTo(t.px[a] + dx * (d - 11), t.py[a] + dy * (d - 11));
      ctx.lineTo(t.px[a] - nx * w - dx * 11, t.py[a] - ny * w - dy * 11);
      ctx.lineTo(t.px[a] - nx * w, t.py[a] - ny * w);
      ctx.closePath();
      ctx.fill();
    }
  }

  _line(ctx, from, to, ax, ay) {
    const n = this.track.n;
    ctx.beginPath();
    ctx.moveTo(ax[from], ay[from]);
    for (let i = from + 1; i <= to + 1; i++) ctx.lineTo(ax[i % n], ay[i % n]);
    ctx.stroke();
  }

  _drawStartLine(ctx, vx0, vy0, vx1, vy1) {
    const t = this.track;
    const i = 0;
    if (t.px[i] < vx0 - 300 || t.px[i] > vx1 + 300 ||
        t.py[i] < vy0 - 300 || t.py[i] > vy1 + 300) return;
    const nx = t.nx[i], ny = t.ny[i], w = t.hw[i];
    const ux = -ny, uy = nx;
    const cells = 10, cw = (w * 2) / cells, depth = 16;
    for (let k = 0; k < cells; k++) {
      const o = -w + k * cw;
      ctx.fillStyle = k % 2 ? '#f2f2f2' : '#1b1e25';
      ctx.beginPath();
      ctx.moveTo(t.px[i] + nx * o, t.py[i] + ny * o);
      ctx.lineTo(t.px[i] + nx * (o + cw), t.py[i] + ny * (o + cw));
      ctx.lineTo(t.px[i] + nx * (o + cw) + ux * depth, t.py[i] + ny * (o + cw) + uy * depth);
      ctx.lineTo(t.px[i] + nx * o + ux * depth, t.py[i] + ny * o + uy * depth);
      ctx.closePath();
      ctx.fill();
    }
  }

  _drawDecor(ctx, vx0, vy0, vx1, vy1) {
    const cell = this.decorCell;
    const i0 = Math.floor(vx0 / cell), i1 = Math.floor(vx1 / cell);
    const j0 = Math.floor(vy0 / cell), j1 = Math.floor(vy1 / cell);
    for (let j = j0; j <= j1; j++) {
      for (let i = i0; i <= i1; i++) {
        const arr = this.decorGrid.get(`${i},${j}`);
        if (!arr) continue;
        for (const d of arr) {
          const s = this.decorSprite(d.t || 'tree');
          const h = s.size / 2;
          ctx.drawImage(s.canvas, d.x - h, d.y - h, s.size, s.size);
        }
      }
    }
  }

  _drawBoxes(ctx, world) {
    if (!world.boxes) return;
    const hidden = world.boxesHidden || new Set();
    const pulse = 0.5 + 0.5 * Math.sin(performance.now() / 260);
    for (let i = 0; i < world.boxes.length; i++) {
      if (hidden.has(i)) continue;
      const b = world.boxes[i];
      const r = 15 + pulse * 2.5;
      ctx.fillStyle = '#ffd23f';
      ctx.globalAlpha = 0.9;
      ctx.beginPath();
      ctx.roundRect(b.x - r, b.y - r, r * 2, r * 2, 5);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.fillStyle = '#241b00';
      ctx.beginPath();
      ctx.arc(b.x, b.y, r * 0.32, 0, 7);
      ctx.fill();
    }
  }

  _drawEntities(ctx, world) {
    for (const [, kind, x, y, a, item] of world.entities || []) {
      const def = world.items && world.items.get(item);
      const col = (def && def.color) || '#ff5252';
      if (kind === 'proj') {
        ctx.save();
        ctx.translate(x, y);
        ctx.rotate(a);
        ctx.fillStyle = col;
        ctx.beginPath();
        ctx.moveTo(12, 0); ctx.lineTo(-8, 5); ctx.lineTo(-8, -5);
        ctx.closePath(); ctx.fill();
        ctx.fillStyle = 'rgba(255,180,80,.75)';
        ctx.beginPath(); ctx.arc(-11, 0, 5, 0, 7); ctx.fill();
        ctx.restore();
      } else {
        ctx.fillStyle = 'rgba(24,24,28,.82)';
        ctx.beginPath(); ctx.ellipse(x, y, 22, 15, a, 0, 7); ctx.fill();
        ctx.fillStyle = col;
        ctx.globalAlpha = 0.55;
        ctx.beginPath(); ctx.ellipse(x, y, 12, 8, a, 0, 7); ctx.fill();
        ctx.globalAlpha = 1;
      }
    }
  }

  // ------------------------------------------------------------------ машины

  _drawCars(ctx, world) {
    const showNames = world.cars.size > 1;
    for (const car of world.cars.values()) {
      const sp = this.carSprite(car.color);
      ctx.save();
      ctx.translate(car.rx, car.ry);
      ctx.rotate(car.ra);

      if (car.shield) {
        ctx.strokeStyle = 'rgba(79,195,247,.85)';
        ctx.lineWidth = 2.5;
        ctx.beginPath(); ctx.arc(0, 0, 26, 0, 7); ctx.stroke();
      }
      const eff = car.eff || null;
      if (eff && eff[1] > 1.1) {
        ctx.fillStyle = 'rgba(255,160,40,.7)';
        ctx.beginPath();
        ctx.moveTo(-18, -5); ctx.lineTo(-32 - Math.random() * 8, 0); ctx.lineTo(-18, 5);
        ctx.closePath(); ctx.fill();
      }
      if (eff && eff[5] === 0) {
        ctx.strokeStyle = 'rgba(255,82,82,.8)';
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(0, 0, 24, 0, 7); ctx.stroke();
      }

      ctx.drawImage(sp.canvas, -sp.cx, -sp.cy, sp.w, sp.h);
      ctx.restore();

      if (showNames) {
        const l = this.label(car.name, car.online === false ? '#ff5252' : '#e8ecf4');
        ctx.drawImage(l.canvas, car.rx - l.w / 2, car.ry - 40, l.w, l.h);
      }
    }
  }

  // --------------------------------------------------------------- частицы

  emit(x, y, vx, vy, color, life, r) {
    const cap = MAX_PARTICLES[this.quality];
    if (this.parts.length >= cap) return;
    this.parts.push({ x, y, vx, vy, life, max: life, color, r });
  }

  skid(car) {
    if (this.quality < 1) return;
    const vlen = Math.hypot(car.vx, car.vy);
    if (vlen < 60) return;
    const fx = Math.cos(car.ra), fy = Math.sin(car.ra);
    const slip = Math.abs(-car.vx * fy + car.vy * fx);
    if (slip < 90) return;
    this.emit(car.rx - fx * 12, car.ry - fy * 12,
              (Math.random() - .5) * 22, (Math.random() - .5) * 22,
              'rgba(210,210,215,', 0.5 + Math.random() * 0.3, 4 + Math.random() * 3);
  }

  burst(x, y, color, n) {
    for (let i = 0; i < n; i++) {
      const a = Math.random() * 7, s = 60 + Math.random() * 180;
      this.emit(x, y, Math.cos(a) * s, Math.sin(a) * s, color,
                0.4 + Math.random() * 0.4, 3 + Math.random() * 4);
    }
  }

  _drawParticles(ctx, dt) {
    const arr = this.parts;
    for (let i = arr.length - 1; i >= 0; i--) {
      const p = arr[i];
      p.life -= dt;
      if (p.life <= 0) { arr[i] = arr[arr.length - 1]; arr.pop(); continue; }
      p.x += p.vx * dt; p.y += p.vy * dt;
      p.vx *= 0.94; p.vy *= 0.94;
      const k = p.life / p.max;
      ctx.fillStyle = p.color + (k * 0.55).toFixed(2) + ')';
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r * (0.5 + k), 0, 7);
      ctx.fill();
    }
  }

  // -------------------------------------------------------------- миникарта

  _drawMinimap(ctx, world, W, H) {
    const m = this.minimap;
    if (!m) return;
    const x = W - m.size - 16, y = H - m.size - 16;
    ctx.globalAlpha = 0.62;
    ctx.fillStyle = '#0c0e14';
    ctx.beginPath();
    ctx.roundRect(x, y, m.size, m.size, 10);
    ctx.fill();
    ctx.globalAlpha = 0.85;
    ctx.drawImage(m.canvas, x, y);
    ctx.globalAlpha = 1;

    for (const car of world.cars.values()) {
      const px = x + m.pad + (car.rx - m.x0) * m.sc;
      const py = y + m.pad + (car.ry - m.y0) * m.sc;
      ctx.fillStyle = car.color;
      ctx.beginPath();
      ctx.arc(px, py, car.slot === world.you ? 4.5 : 3, 0, 7);
      ctx.fill();
      if (car.slot === world.you) {
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
      }
    }
  }
}
