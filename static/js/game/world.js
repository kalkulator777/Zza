// Мир на клиенте: своя машина предсказывается, чужие интерполируются.
//
// Своя машина считается здесь же и сразу — руль обязан отзываться мгновенно,
// ждать ответа сервера нельзя даже в локальной сети. Когда приходит снапшот,
// состояние откатывается к авторитетному и переигрывается вперёд на
// сохранённых вводах. Физика побитово совпадает с серверной (это проверяет
// tools/crosscheck.py), поэтому в обычной езде откат не меняет НИЧЕГО и
// поправка равна нулю. Она появляется ровно там, где клиент и не мог знать
// правды: столкновения, попадания, респавн.
//
// Чужие машины не предсказываются вовсе: угадывать чужие нажатия бессмысленно.
// Они рисуются с небольшим запаздыванием между двумя снапшотами — так они
// движутся плавно, а не скачками по 33 мс.

import { driveTick, noEffects } from './carstep.js';
import { normAngle } from './fixmath.js';

const HIST = 256;

function shortAngle(d) {
  return normAngle(d);
}

export class World {
  constructor(track, phys, surfaces, hz, youSlot) {
    this.track = track;
    this.C = phys.car;
    this.CC = phys.collision;
    this.sim = phys.sim;
    this.surfaces = surfaces;
    this.hz = hz;
    this.dt = 1.0 / hz;
    this.you = youSlot;

    this.tick = 0;
    this.ready = false;
    this.cars = new Map();

    // Кольцевые буферы своей машины
    this.hist = new Array(HIST);
    this.inputs = new Int32Array(HIST);
    this.histTick = new Int32Array(HIST).fill(-1);

    this.own = null;
    this.ownEff = noEffects();
    this.offX = 0; this.offY = 0; this.offA = 0;

    // Снапшоты для интерполяции чужих машин
    this.snaps = [];
    this.serverTick = 0;
    this.lead = 4;
    this.slack = 0;

    // Статистика для оверлея F3
    this.stat = { corr: 0, corrPx: 0, maxCorrPx: 0, snaps: 0, resets: 0,
                  lastCorrPx: 0, sizes: [] };
  }

  addCar(meta) {
    this.cars.set(meta.slot, {
      slot: meta.slot, name: meta.name, color: meta.color, token: meta.token,
      x: 0, y: 0, a: 0, vx: 0, vy: 0, seg: 0, lap: 0, item: null,
      eff: noEffects(), shield: 0, online: true, finished: false,
      // для интерполяции
      rx: 0, ry: 0, ra: 0,
    });
  }

  get ownCar() { return this.cars.get(this.you); }

  // ------------------------------------------------------------ своя машина

  simTick(mask) {
    this.tick++;
    const i = ((this.tick % HIST) + HIST) % HIST;
    this.inputs[i] = mask;
    if (this.own) {
      driveTick(this.own, mask, this.dt, this.track, this.C, this.CC,
                this.ownEff, this.surfaces);
      this.hist[i] = { x: this.own.x, y: this.own.y, vx: this.own.vx,
                       vy: this.own.vy, a: this.own.a, seg: this.own.seg };
      this.histTick[i] = this.tick;
    }
  }

  _reconcile(k, auth, eff) {
    this.ownEff = eff;
    if (!this.own) {
      this.own = { x: auth.x, y: auth.y, vx: auth.vx, vy: auth.vy,
                   a: auth.a, seg: auth.seg };
      this.tick = k + this.lead;
      this.ready = true;
      this.stat.resets++;
      return;
    }
    if (k > this.tick || k <= this.tick - HIST + 4 || this.needsResync()) {
      // Ушли слишком далеко (долгий фриз вкладки) — проще начать заново
      this.own = { x: auth.x, y: auth.y, vx: auth.vx, vy: auth.vy,
                   a: auth.a, seg: auth.seg };
      this.tick = k + this.lead;
      this.offX = this.offY = this.offA = 0;
      this.stat.resets++;
      return;
    }

    const i = ((k % HIST) + HIST) % HIST;
    const h = this.histTick[i] === k ? this.hist[i] : null;
    if (h) {
      const dx = h.x - auth.x, dy = h.y - auth.y;
      const err = Math.sqrt(dx * dx + dy * dy);
      this.stat.lastCorrPx = err;
      if (err < 0.02 && Math.abs(shortAngle(h.a - auth.a)) < 1e-4) {
        return;   // предсказание совпало — обычный случай
      }
      this.stat.corr++;
      this.stat.corrPx = err;
      if (err > this.stat.maxCorrPx) this.stat.maxCorrPx = err;
      // Максимум задаёт респавн — законный телепорт. Чтобы видеть обычную
      // работу предсказания, храним распределение и смотрим на медиану.
      this.stat.sizes.push(err);
      if (this.stat.sizes.length > 400) this.stat.sizes.shift();
    }

    const beforeX = this.own.x, beforeY = this.own.y, beforeA = this.own.a;

    this.own.x = auth.x; this.own.y = auth.y;
    this.own.vx = auth.vx; this.own.vy = auth.vy;
    this.own.a = auth.a; this.own.seg = auth.seg;

    for (let t = k + 1; t <= this.tick; t++) {
      const j = ((t % HIST) + HIST) % HIST;
      driveTick(this.own, this.inputs[j], this.dt, this.track, this.C, this.CC,
                eff, this.surfaces);
      this.hist[j] = { x: this.own.x, y: this.own.y, vx: this.own.vx,
                       vy: this.own.vy, a: this.own.a, seg: this.own.seg };
      this.histTick[j] = t;
    }

    // Разницу не показываем рывком, а гасим за пару кадров
    this.offX += beforeX - this.own.x;
    this.offY += beforeY - this.own.y;
    this.offA += shortAngle(beforeA - this.own.a);
    const d = Math.sqrt(this.offX * this.offX + this.offY * this.offY);
    if (d > this.sim.blendHardPx) {
      // Респавн или сильный удар — плавно тут только хуже, лучше честно прыгнуть
      this.offX = 0; this.offY = 0; this.offA = 0;
    }
  }

  decayOffset(dtFrame) {
    const k = Math.exp(-this.sim.blendRate * dtFrame);
    this.offX *= k; this.offY *= k; this.offA *= k;
    if (Math.abs(this.offX) < 0.01) this.offX = 0;
    if (Math.abs(this.offY) < 0.01) this.offY = 0;
    if (Math.abs(this.offA) < 0.0001) this.offA = 0;
  }

  // ------------------------------------------------------------- снапшоты

  onSnapshot(snap) {
    this.serverTick = snap.k;
    this.stat.snaps++;
    this.state = snap.st;
    this.elapsed = snap.el;
    this.rank = snap.r;

    const rows = new Map();
    for (const row of snap.c) {
      const [slot, x, y, a, vx, vy, seg, lap, appliedTick, item, slack, eff, shield] = row;
      rows.set(slot, { x, y, a, vx, vy, seg, lap, item: item || null,
                       eff: eff || noEffects(), shield: shield || 0, slack });
      const car = this.cars.get(slot);
      if (!car) continue;
      car.lap = lap;
      car.item = item || null;
      car.eff = eff || noEffects();
      car.shield = shield || 0;
      if (slot !== this.you) {
        car.x = x; car.y = y; car.a = a; car.vx = vx; car.vy = vy; car.seg = seg;
      }
    }

    const mine = rows.get(this.you);
    if (mine) {
      this.slack = mine.slack;
      this._reconcile(snap.k, mine, mine.eff);
      const own = this.cars.get(this.you);
      if (own && this.own) {
        own.x = this.own.x; own.y = this.own.y; own.a = this.own.a;
        own.vx = this.own.vx; own.vy = this.own.vy; own.seg = this.own.seg;
      }
    } else if (!this.ready) {
      // Зритель: своей машины нет, но время всё равно ведём от сервера
      this.tick = snap.k + this.lead;
      this.ready = true;
    }

    this.snaps.push({ k: snap.k, rows, at: performance.now() });
    while (this.snaps.length > 24) this.snaps.shift();

    this.entities = snap.e || [];
    this.boxesHidden = new Set(snap.bx || []);
    return snap.ev || [];
  }

  /** Куда рисовать чужие машины: между двумя снапшотами, с запаздыванием.
   *  Без этого они дёргались бы 30 раз в секунду вместо плавного движения. */
  interpolate() {
    const delay = this.sim ? (this.hz / (this.sim.snapshotHz || 30)) * 1.6 : 3;
    const want = this.tick - this.lead - delay;
    let a = null, b = null;
    for (let i = this.snaps.length - 1; i >= 0; i--) {
      if (this.snaps[i].k <= want) { a = this.snaps[i]; b = this.snaps[i + 1] || null; break; }
    }
    if (!a) { a = this.snaps[0]; b = this.snaps[1] || null; }
    if (!a) return;

    const t = (b && b.k > a.k) ? Math.max(0, Math.min(1, (want - a.k) / (b.k - a.k))) : 1;

    for (const car of this.cars.values()) {
      if (car.slot === this.you) {
        car.rx = this.own ? this.own.x + this.offX : car.x;
        car.ry = this.own ? this.own.y + this.offY : car.y;
        car.ra = this.own ? this.own.a + this.offA : car.a;
        continue;
      }
      const ra = a.rows.get(car.slot);
      if (!ra) { car.rx = car.x; car.ry = car.y; car.ra = car.a; continue; }
      const rb = b && b.rows.get(car.slot);
      if (!rb) { car.rx = ra.x; car.ry = ra.y; car.ra = ra.a; continue; }
      car.rx = ra.x + (rb.x - ra.x) * t;
      car.ry = ra.y + (rb.y - ra.y) * t;
      car.ra = ra.a + shortAngle(rb.a - ra.a) * t;
    }
  }

  /** Небольшое опережение сервера нужно, чтобы ввод успевал прийти к нужному
   *  тику. Подтягиваем его растяжением времени, а не скачком номера тика —
   *  скачок порвал бы историю и вызвал ложную коррекцию. */
  corrPercentile(p) {
    const a = this.stat.sizes;
    if (!a.length) return 0;
    const s = [...a].sort((x, y) => x - y);
    return s[Math.min(s.length - 1, Math.floor(s.length * p))];
  }

  paceFactor() {
    const d = this.slack - this.lead;
    // Мёртвая зона шире шага коррекции — иначе опережение качалось бы
    // туда-сюда вокруг цели и постоянно портило предсказание.
    if (d > 2) return 0.97;
    if (d < -2) return 1.03;
    return 1.0;
  }

  /** Если опережение всё-таки уехало далеко (вкладка была свёрнута, машина
   *  тормозила), тянуть его процентами бессмысленно — проще пересобрать. */
  needsResync() {
    if (!this.ready || this.state !== 'racing') return false;
    return Math.abs(this.slack - this.lead) > 30;
  }
}
