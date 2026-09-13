/* Canvas-рендер боя.
 *
 * Три вещи, на которых держится производительность:
 *   1. фон (градиент + сетка + виньетка) рисуется один раз в offscreen-канвас;
 *   2. геометрия арены — тоже отдельный слой, он лишь сдвигается при тряске;
 *   3. уровень качества (разрешение, частицы, эффекты) подстраивается под машину.
 * Каждый кадр остаётся только динамика: бойцы, снаряды, зоны, частицы и HUD.
 */
const Render = {
  S: 0.78, CW: 1600, CH: 900, WCX: 800, WCY: 480,
  DELAY: 90,
  cv: null, ctx: null, arena: null, players: {}, heroes: {}, myPid: null,
  buf: [], parts: [], beams: [], shake: 0, t: 0, aimLine: null,

  // качество: 2 — высокое, 1 — среднее, 0 — низкое
  qmode: 'auto', level: 2, rscale: 1,
  PART_CAP: [140, 260, 600],
  _bg: null, _arenaLayer: null,
  _view: null, _viewT: -1,
  fps: 60, _autoT: 0, _autoStep: 0,

  init(cv, heroes) {
    this.cv = cv;
    this.ctx = cv.getContext('2d', { alpha: false });
    heroes.forEach(h => this.heroes[h.id] = h);
    this.setQuality(localStorage.getItem('zza.q') || 'auto');
  },

  /* ---------------- качество ---------------- */
  setQuality(mode) {
    this.qmode = mode;
    localStorage.setItem('zza.q', mode);
    this._autoStep = 0;
    this._autoT = 0;
    this.applyLevel(mode === 'low' ? 0 : mode === 'med' ? 1 : 2);
  },

  applyLevel(lv) {
    this.level = lv;
    this.rscale = [0.55, 0.75, 1][lv];
    const w = Math.round(this.CW * this.rscale);
    const h = Math.round(this.CH * this.rscale);
    if (this.cv && (this.cv.width !== w || this.cv.height !== h)) {
      this.cv.width = w;
      this.cv.height = h;
    }
    this._bg = null;
    this._arenaLayer = null;
  },

  /* Авто-режим: если машина не тянет, тихо снижаем качество. Обратно не
     поднимаем — мигающая картинка раздражает сильнее, чем лишний запас. */
  autoQuality(dt) {
    if (this.qmode !== 'auto' || this.level === 0) return;
    this._autoT += dt;
    if (this._autoT < 3) return;
    this._autoT = 0;
    if (this.fps < 48) this.applyLevel(this.level - 1);
  },

  setMatch(msg, myPid) {
    this.arena = msg.arena;
    this.myPid = myPid;
    this.players = {};
    msg.players.forEach(p => this.players[p.pid] = p);
    this.buf = []; this.parts = []; this.beams = []; this.shake = 0;
    this._arenaLayer = null;
    this._bg = null;
  },

  w2s(x, y) { return { x: (x - this.WCX) * this.S + this.CW / 2, y: (y - this.WCY) * this.S + this.CH / 2 }; },
  screenToWorld(sx, sy) {
    return { x: (sx - this.CW / 2) / this.S + this.WCX, y: (sy - this.CH / 2) / this.S + this.WCY };
  },

  push(snap) {
    snap._ct = performance.now();
    this.buf.push(snap);
    if (this.buf.length > 24) this.buf.shift();
    this.shake = Math.max(this.shake, snap.sk || 0);
    (snap.e || []).forEach(e => this.onEvent(e, snap));
  },

  latest() { return this.buf.length ? this.buf[this.buf.length - 1] : null; },

  /* ---------- интерполяция (результат кэшируется на кадр) ---------- */
  view() {
    if (this._viewT === this.t) return this._view;
    this._viewT = this.t;
    this._view = this._interp();
    return this._view;
  },

  _interp() {
    const n = this.buf.length;
    if (!n) return null;
    const rt = performance.now() - this.DELAY;
    let a = this.buf[n - 1], b = null;
    for (let i = n - 1; i > 0; i--) {
      if (this.buf[i - 1]._ct <= rt && this.buf[i]._ct >= rt) { a = this.buf[i - 1]; b = this.buf[i]; break; }
    }
    if (!b) return { s: a, p: a.p, o: a.o };
    const k = Math.max(0, Math.min(1, (rt - a._ct) / Math.max(1, b._ct - a._ct)));
    const pa = {};
    a.p.forEach(x => pa[x.i] = x);
    const p = b.p.map(y => {
      const x = pa[y.i];
      if (!x) return y;
      return Object.assign({}, y, { x: x.x + (y.x - x.x) * k, y: x.y + (y.y - x.y) * k });
    });
    const oa = {};
    (a.o || []).forEach(x => oa[x.i] = x);
    const o = (b.o || []).map(y => {
      const x = oa[y.i];
      return x ? Object.assign({}, y, { x: x.x + (y.x - x.x) * k, y: x.y + (y.y - x.y) * k }) : y;
    });
    return { s: b, p, o };
  },

  /* ---------- частицы ---------- */
  add(p) { if (this.parts.length < this.PART_CAP[this.level]) this.parts.push(p); },
  spark(x, y, col, n, sp, life) {
    n = Math.max(2, Math.round(n * [0.35, 0.6, 1][this.level]));
    for (let i = 0; i < n; i++) {
      const a = Math.random() * 6.283, v = (0.4 + Math.random()) * (sp || 260);
      this.add({ k: 'p', x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 40, c: col,
                 l: life || 0.4, m: life || 0.4, r: 1.5 + Math.random() * 2 });
    }
  },
  ring(x, y, r0, r1, col, life, w) { this.add({ k: 'r', x, y, r0, r1, c: col, l: life, m: life, w: w || 3 }); },
  arc(x, y, d, s, col) { this.add({ k: 'a', x, y, d, s, c: col, l: 0.22, m: 0.22 }); },
  num(x, y, txt, col, big) {
    this.add({ k: 'n', x, y, vy: -70, t: txt, c: col, l: 0.85, m: 0.85, s: big ? 26 : 17 });
  },
  beam(x1, y1, x2, y2, col, life, w) { this.beams.push({ x1, y1, x2, y2, c: col, l: life, m: life, w }); },

  heroCol(pid) {
    const p = this.players[pid], h = p && this.heroes[p.hero];
    return h ? h.color : '#fff';
  },

  onEvent(e) {
    switch (e.k) {
      case 'hit':
        this.spark(e.x, e.y, '#ffd9a0', Math.min(14, 4 + (e.m || 6)), 300, 0.35);
        if (e.m >= 3) this.num(e.x, e.y - 30, '-' + Math.round(e.m), '#ffd9a0', e.m >= 25);
        break;
      case 'frosthit':
        this.spark(e.x, e.y, '#bff0ff', 8, 220, 0.5);
        this.num(e.x, e.y - 30, '-' + Math.round(e.m), '#bff0ff');
        break;
      case 'burn':
        if (Math.random() < 0.3) this.spark(e.x, e.y, '#ff9a5b', 2, 90, 0.4);
        break;
      case 'heal':
        this.num(e.x, e.y - 34, '+' + Math.round(e.m), '#6ee7a0');
        this.ring(e.x, e.y, 10, 40, '#6ee7a0', 0.4, 2);
        break;
      case 'slash': this.arc(e.x, e.y, e.d || 1, e.s || 1, '#fff'); break;
      case 'dashslash': this.ring(e.x, e.y, 10, 90, '#ff8ea0', 0.3, 4); break;
      case 'whirl': this.ring(e.x, e.y, 20, 115 * (e.s || 1), '#ff4d6d', 0.35, 5); break;
      case 'bash': case 'sparks':
        this.spark(e.x, e.y, '#ffd166', 10, 320, 0.35);
        this.arc(e.x, e.y, e.d || 1, e.s || 1, '#ffe6a8');
        break;
      case 'ram': this.ring(e.x, e.y, 20, 80, '#f4a259', 0.3, 6); break;
      case 'guard': this.ring(e.x, e.y, 30, 60, '#f4a259', 0.3, 3); break;
      case 'cone': this.arc(e.x + 70 * (e.d || 1), e.y, e.d || 1, 1.8, '#bff0ff'); break;
      case 'shock': this.ring(e.x, e.y, 10, e.r || 170, '#b58cff', 0.45, 6); break;
      case 'boom':
        this.ring(e.x, e.y, 10, e.r || 100, '#ffb45b', 0.5, 8);
        this.spark(e.x, e.y, '#ffd166', 26, 480, 0.6);
        break;
      case 'death': {
        const c = this.heroCol(e.p) || '#fff';
        this.ring(e.x, e.y, 10, 150, c, 0.6, 6);
        this.spark(e.x, e.y, c, 34, 560, 0.8);
        break;
      }
      case 'spawn': this.ring(e.x, e.y, 90, 10, this.heroCol(e.p), 0.45, 3); break;
      case 'jump': this.spark(e.x, e.y, '#6b7590', 4, 90, 0.25); break;
      case 'dash': this.ring(e.x, e.y, 8, 46, '#9fd8ff', 0.22, 3); break;
      case 'shieldhit': case 'shieldon': this.ring(e.x, e.y, 12, 52, '#8ad7ff', 0.32, 3); break;
      case 'pdie': this.spark(e.x, e.y, '#9aa6c0', 4, 120, 0.25); break;
      case 'crit': this.ring(e.x, e.y, 6, 40, '#fff', 0.25, 3); break;
      case 'bang': this.ring(e.x, e.y, 6, 30 + 60 * (e.m || 0), '#5bc8ff', 0.25, 4); break;
      case 'charge': this.ring(e.x, e.y, 60 - 40 * (e.m || 0), 20, '#5bc8ff', 0.2, 2); break;
      case 'beamaim': this.aimLine = { x: e.x, y: e.y, dx: e.dx, dy: e.dy, t: performance.now() }; break;
      case 'beam': this.beam(e.x, e.y, e.x + e.dx * 2600, e.y + e.dy * 2600, '#5bc8ff', 0.35, 16); break;
      case 'ultflash': this.ring(e.x, e.y, 10, 220, e.c || '#fff', 0.5, 7); break;
      case 'snow':
        if (this.level > 0) {
          for (let i = 0; i < 5; i++) {
            const a = Math.random() * 6.283, r = Math.random() * (e.r || 100);
            this.add({ k: 'p', x: e.x + Math.cos(a) * r, y: e.y + Math.sin(a) * r - 60,
                       vx: 0, vy: 90, c: '#dff6ff', l: 1.1, m: 1.1, r: 2 });
          }
        }
        break;
      case 'pulse': this.ring(e.x, e.y, 20, e.r || 150, '#6ee7a0', 0.5, 2); break;
      case 'pull': this.ring(e.x, e.y, 40, 8, '#ffd166', 0.3, 3); break;
      case 'build': this.ring(e.x, e.y, 40, 12, '#ffd166', 0.3, 2); break;
      case 'revive': this.ring(e.x, e.y, 8, 90, '#b58cff', 0.5, 5); break;
      case 'windup': case 'guardtick': case 'shoot': case 'ko': case 'end':
      case 'go': case 'ult': break;
      default:
        // незнакомый эффект (например, от нового героя) — хоть что-то, но покажем
        this.ring(e.x || 0, e.y || 0, 8, 50, '#ffffff', 0.3, 3);
    }
  },

  step(dt) {
    for (let i = this.parts.length - 1; i >= 0; i--) {
      const p = this.parts[i];
      p.l -= dt;
      if (p.l <= 0) { this.parts.splice(i, 1); continue; }
      if (p.k === 'p') { p.x += p.vx * dt; p.y += p.vy * dt; p.vy += 700 * dt; p.vx *= 0.97; }
      else if (p.k === 'n') { p.y += p.vy * dt; p.vy *= 0.93; }
    }
    for (let i = this.beams.length - 1; i >= 0; i--) {
      this.beams[i].l -= dt;
      if (this.beams[i].l <= 0) this.beams.splice(i, 1);
    }
    this.shake = Math.max(0, this.shake - 40 * dt);
  },

  /* ---------- статические слои ---------- */
  buildBg() {
    const q = this.rscale;
    const cv = document.createElement('canvas');
    cv.width = Math.round(this.CW * q);
    cv.height = Math.round(this.CH * q);
    const c = cv.getContext('2d');
    c.scale(q, q);
    const a = this.arena;
    const g = c.createLinearGradient(0, 0, 0, this.CH);
    g.addColorStop(0, (a && a.bg[0]) || '#161a2b');
    g.addColorStop(1, (a && a.bg[1]) || '#0a0c14');
    c.fillStyle = g;
    c.fillRect(0, 0, this.CW, this.CH);

    if (this.level > 0) {
      c.globalAlpha = 0.22;
      c.strokeStyle = (a && a.accent) || '#3d5a80';
      c.lineWidth = 1;
      c.beginPath();
      const st = this.level === 2 ? 80 : 140;
      for (let x = 0; x < this.CW; x += st) { c.moveTo(x, 0); c.lineTo(x, this.CH); }
      for (let y = 0; y < this.CH; y += st) { c.moveTo(0, y); c.lineTo(this.CW, y); }
      c.stroke();
      c.globalAlpha = 1;
    }

    const vg = c.createRadialGradient(this.CW / 2, this.CH / 2, 260, this.CW / 2, this.CH / 2, 900);
    vg.addColorStop(0, 'rgba(0,0,0,0)');
    vg.addColorStop(1, 'rgba(0,0,0,.65)');
    c.fillStyle = vg;
    c.fillRect(0, 0, this.CW, this.CH);
    this._bg = cv;
  },

  buildArenaLayer() {
    const q = this.rscale;
    const cv = document.createElement('canvas');
    cv.width = Math.round(this.CW * q);
    cv.height = Math.round(this.CH * q);
    const c = cv.getContext('2d');
    c.scale(q, q);
    c.translate(this.CW / 2, this.CH / 2);
    c.scale(this.S, this.S);
    c.translate(-this.WCX, -this.WCY);
    this.drawBlast(c);
    this.drawPlatforms(c);
    this._arenaLayer = cv;
  },

  /* ---------------------------- кадр ---------------------------- */
  frame(dt) {
    const c = this.ctx;
    if (!c) return;
    this.t += dt;
    this.autoQuality(dt);
    this.step(dt);
    const v = this.view();
    const q = this.rscale;

    if (!this._bg) this.buildBg();
    if (!this._arenaLayer && this.arena) this.buildArenaLayer();

    c.setTransform(1, 0, 0, 1, 0, 0);
    c.drawImage(this._bg, 0, 0);

    const sh = this.shake;
    const sx = sh ? (Math.random() - .5) * sh * 2 : 0;
    const sy = sh ? (Math.random() - .5) * sh * 2 : 0;
    if (this._arenaLayer) c.drawImage(this._arenaLayer, sx * q, sy * q);

    c.setTransform(q, 0, 0, q, 0, 0);
    c.translate(this.CW / 2 + sx, this.CH / 2 + sy);
    c.scale(this.S, this.S);
    c.translate(-this.WCX, -this.WCY);

    if (v) {
      this.drawZones(c, v.s.z || []);
      this.drawStructs(c, v.s.b || []);
      this.drawProjectiles(c, v.o || []);
      for (const p of v.p) if (p.al) this.drawFighter(c, p);
      this.drawParts(c);
    }

    c.setTransform(q, 0, 0, q, 0, 0);
    if (v) this.drawHUD(c, v);
  },

  drawBlast(c) {
    const b = this.arena && this.arena.blast;
    if (!b) return;
    c.save();
    c.strokeStyle = 'rgba(255,80,90,.35)';
    c.lineWidth = 3;
    c.setLineDash([16, 14]);
    c.strokeRect(b.x0, b.y0, b.x1 - b.x0, b.y1 - b.y0);
    c.restore();
  },

  drawPlatforms(c) {
    const a = this.arena;
    if (!a) return;
    a.platforms.forEach(p => {
      if (p.oneway) {
        c.fillStyle = hexA(a.accent, .85);
        this.rr(c, p.x, p.y, p.w, p.h, 6); c.fill();
        c.fillStyle = 'rgba(255,255,255,.18)';
        c.fillRect(p.x + 4, p.y, p.w - 8, 3);
      } else {
        const g = c.createLinearGradient(0, p.y, 0, p.y + p.h);
        g.addColorStop(0, hexA(a.accent, .95));
        g.addColorStop(.12, hexA(a.accent, .5));
        g.addColorStop(1, 'rgba(6,8,14,.95)');
        c.fillStyle = g;
        this.rr(c, p.x, p.y, p.w, p.h, 8); c.fill();
        c.fillStyle = 'rgba(255,255,255,.22)';
        c.fillRect(p.x + 6, p.y, p.w - 12, 4);
      }
    });
  },

  rr(c, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    c.beginPath();
    c.moveTo(x + r, y); c.lineTo(x + w - r, y); c.quadraticCurveTo(x + w, y, x + w, y + r);
    c.lineTo(x + w, y + h - r); c.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    c.lineTo(x + r, y + h); c.quadraticCurveTo(x, y + h, x, y + h - r);
    c.lineTo(x, y + r); c.quadraticCurveTo(x, y, x + r, y);
    c.closePath();
  },

  drawZones(c, zs) {
    const COL = { dome: '#f4a259', blizzard: '#bff0ff', field: '#6ee7a0', mark: '#ff5b6e' };
    zs.forEach(z => {
      const col = COL[z.k] || '#c9d4ff';
      if (z.k === 'mark') {
        c.strokeStyle = col;
        c.lineWidth = 4;
        c.globalAlpha = .5 + .5 * Math.sin(this.t * 22);
        c.beginPath(); c.arc(z.x, z.y, z.r, 0, 6.283); c.stroke();
        c.globalAlpha = .3;
        c.fillStyle = col;
        c.beginPath(); c.arc(z.x, z.y, z.r * z.p, 0, 6.283); c.fill();
      } else {
        c.globalAlpha = .13;
        c.fillStyle = col;
        c.beginPath(); c.arc(z.x, z.y, z.r, 0, 6.283); c.fill();
        c.globalAlpha = .5;
        c.strokeStyle = col;
        c.lineWidth = 2;
        c.beginPath(); c.arc(z.x, z.y, z.r * (.97 + .03 * Math.sin(this.t * 4)), 0, 6.283); c.stroke();
      }
      c.globalAlpha = 1;
    });
  },

  drawStructs(c, bs) {
    bs.forEach(s => {
      const tc = TEAM_COL[s.t];
      c.save();
      if (s.k === 'turret') {
        c.translate(s.x + s.w / 2, s.y + s.h / 2);
        c.fillStyle = '#2a3145';
        this.rr(c, -s.w / 2, 0, s.w, s.h / 2, 4); c.fill();
        c.strokeStyle = tc; c.lineWidth = 3;
        c.save(); c.rotate(-(s.d || 0) * Math.PI / 180);
        c.beginPath(); c.moveTo(0, -4); c.lineTo(26, -4); c.stroke();
        c.restore();
        c.fillStyle = '#ffd166';
        c.beginPath(); c.arc(0, -4, 9, 0, 6.283); c.fill();
        c.strokeStyle = tc; c.lineWidth = 2; c.stroke();
        c.fillStyle = 'rgba(0,0,0,.6)'; c.fillRect(-s.w / 2, s.h / 2 + 3, s.w, 4);
        c.fillStyle = tc; c.fillRect(-s.w / 2, s.h / 2 + 3, s.w * s.hp, 4);
      } else if (s.k === 'mine') {
        c.translate(s.x + s.w / 2, s.y + s.h / 2);
        c.fillStyle = '#39405a';
        c.beginPath(); c.arc(0, 0, 10, 0, 6.283); c.fill();
        c.fillStyle = (Math.sin(this.t * 14) > 0) ? '#ff4d6d' : '#601b28';
        c.beginPath(); c.arc(0, -3, 4, 0, 6.283); c.fill();
      } else if (s.k === 'wall') {
        c.fillStyle = 'rgba(170,230,255,.30)';
        this.rr(c, s.x, s.y, s.w, s.h, 5); c.fill();
        c.strokeStyle = 'rgba(200,245,255,.85)'; c.lineWidth = 2; c.stroke();
        if (this.level > 0) {
          c.strokeStyle = 'rgba(255,255,255,.35)'; c.lineWidth = 1;
          c.beginPath();
          for (let i = 1; i < 4; i++) {
            c.moveTo(s.x, s.y + s.h * i / 4);
            c.lineTo(s.x + s.w, s.y + s.h * i / 4 - 6);
          }
          c.stroke();
        }
      } else {
        // незнакомая постройка — обобщённый ящик в цвете команды
        c.fillStyle = 'rgba(30,36,54,.9)';
        this.rr(c, s.x, s.y, s.w, s.h, 5); c.fill();
        c.strokeStyle = tc; c.lineWidth = 2; c.stroke();
        c.fillStyle = 'rgba(0,0,0,.6)'; c.fillRect(s.x, s.y + s.h + 3, s.w, 4);
        c.fillStyle = tc; c.fillRect(s.x, s.y + s.h + 3, s.w * s.hp, 4);
      }
      c.restore();
    });
  },

  drawProjectiles(c, os) {
    const COL = { bolt: '#9fe4ff', slug: '#5bc8ff', ice: '#bff0ff', orb: '#c9a8ff', bolt2: '#ffd166' };
    os.forEach(o => {
      const a = o.a * Math.PI / 180;
      c.save();
      c.translate(o.x, o.y);
      c.rotate(-a);
      if (o.k === 'hook') {
        c.strokeStyle = '#ffd166'; c.lineWidth = 3;
        c.beginPath(); c.moveTo(0, 0); c.lineTo(-120, 0); c.stroke();
        c.fillStyle = '#ffd166';
        c.beginPath(); c.arc(0, 0, 8, 0, 6.283); c.fill();
      } else if (o.k === 'beam') {
        c.fillStyle = 'rgba(120,220,255,.9)';
        c.fillRect(-90, -o.r, 180, o.r * 2);
      } else {
        const col = COL[o.k] || '#e8ecff';
        c.fillStyle = col;
        if (o.k === 'ice') {
          c.beginPath();
          c.moveTo(o.r * 1.6, 0); c.lineTo(0, -o.r); c.lineTo(-o.r * 1.6, 0); c.lineTo(0, o.r);
          c.closePath(); c.fill();
        } else {
          this.rr(c, -o.r * 1.8, -o.r, o.r * 3.6, o.r * 2, o.r); c.fill();
        }
        c.globalAlpha = .25;
        c.fillRect(-o.r * 8, -o.r * .4, o.r * 6, o.r * .8);
        c.globalAlpha = 1;
      }
      c.restore();
    });
  },

  drawFighter(c, p) {
    const info = this.players[p.i] || {};
    const hero = this.heroes[info.hero] || {};
    const col = hero.color || '#fff';
    const tc = TEAM_COL[info.team || 0];
    const W = 44, H = 72;
    const x = p.x, y = p.y;
    const me = p.i === this.myPid;
    const alpha = p.iv ? .45 + .35 * Math.sin(this.t * 26) : 1;

    c.globalAlpha = .3 * alpha;
    c.fillStyle = '#000';
    c.beginPath(); c.ellipse(x, y + H / 2 + 4, W * .5, 6, 0, 0, 6.283); c.fill();

    c.globalAlpha = alpha;
    c.fillStyle = col;
    this.rr(c, x - W / 2, y - H / 2, W, H, 11); c.fill();
    c.strokeStyle = tc; c.lineWidth = 3; c.stroke();
    // затемнение низа вместо градиента — заметно дешевле, выглядит так же
    c.fillStyle = 'rgba(0,0,0,.26)';
    this.rr(c, x - W / 2 + 3, y + 6, W - 6, H / 2 - 9, 7); c.fill();

    c.fillStyle = 'rgba(10,12,20,.85)';
    this.rr(c, x - W / 2 + (p.f > 0 ? 14 : 4), y - H / 2 + 10, 26, 12, 5); c.fill();
    c.fillStyle = tc;
    c.fillRect(x + (p.f > 0 ? 14 : -16), y - H / 2 + 13, 5, 6);

    const gl = GLYPH[info.hero] || glyphFallback(info.hero);
    gl(c, x, y + 10, 30, 'rgba(12,14,22,.9)');
    c.globalAlpha = 1;

    if (p.sh > 0) {
      c.strokeStyle = 'rgba(140,215,255,.9)'; c.lineWidth = 3;
      c.beginPath(); c.arc(x, y, 44 + Math.sin(this.t * 6) * 2, 0, 6.283); c.stroke();
    }
    const ef = p.ef || [];
    if (ef.length) {
      if (ef.includes('freeze')) {
        c.fillStyle = 'rgba(160,225,255,.35)';
        this.rr(c, x - W / 2 - 4, y - H / 2 - 4, W + 8, H + 8, 8); c.fill();
        c.strokeStyle = '#dff6ff'; c.lineWidth = 2; c.stroke();
      }
      if (ef.includes('stun')) {
        c.fillStyle = '#ffd166';
        for (let i = 0; i < 3; i++) {
          const a = this.t * 5 + i * 2.09;
          c.beginPath();
          c.arc(x + Math.cos(a) * 20, y - H / 2 - 12 + Math.sin(a) * 5, 3, 0, 6.283); c.fill();
        }
      }
      if (ef.includes('burn')) {
        c.fillStyle = 'rgba(255,140,70,.35)';
        this.rr(c, x - W / 2, y - H / 2, W, H, 11); c.fill();
      }
      if (ef.includes('dr')) {
        c.strokeStyle = 'rgba(244,162,89,.8)'; c.lineWidth = 2;
        c.beginPath(); c.arc(x, y, 40, 0, 6.283); c.stroke();
      }
      if (ef.includes('slow')) {
        c.fillStyle = 'rgba(140,215,255,.18)';
        this.rr(c, x - W / 2, y - H / 2, W, H, 11); c.fill();
      }
    }

    c.textAlign = 'center';
    c.font = '600 16px "Segoe UI",sans-serif';
    c.fillStyle = me ? '#fff' : 'rgba(220,228,245,.85)';
    c.fillText(info.name || '', x, y - H / 2 - 18);
    const bw = 66, hpk = Math.max(0, p.hp / (hero.hp || 100));
    c.fillStyle = 'rgba(0,0,0,.6)';
    c.fillRect(x - bw / 2, y - H / 2 - 13, bw, 6);
    c.fillStyle = hpk > .35 ? tc : '#ff4d6d';
    c.fillRect(x - bw / 2, y - H / 2 - 13, bw * hpk, 6);
    if (p.sh > 0) {
      c.fillStyle = 'rgba(200,240,255,.9)';
      c.fillRect(x - bw / 2, y - H / 2 - 16, bw * Math.min(1, p.sh / 40), 3);
    }
    if (me) {
      c.fillStyle = 'rgba(255,255,255,.8)';
      c.beginPath();
      c.moveTo(x, y - H / 2 - 26); c.lineTo(x - 6, y - H / 2 - 35); c.lineTo(x + 6, y - H / 2 - 35);
      c.closePath(); c.fill();
      c.globalAlpha = .35; c.strokeStyle = col; c.lineWidth = 2;
      c.beginPath(); c.moveTo(x, y); c.lineTo(x + p.ax * 90, y + p.ay * 90); c.stroke();
      c.globalAlpha = 1;
    }
  },

  drawParts(c) {
    for (const b of this.beams) {
      const k = b.l / b.m;
      c.globalAlpha = k;
      c.strokeStyle = b.c;
      c.lineWidth = b.w * k;
      c.beginPath(); c.moveTo(b.x1, b.y1); c.lineTo(b.x2, b.y2); c.stroke();
    }
    if (this.aimLine && performance.now() - this.aimLine.t < 120) {
      const a = this.aimLine;
      c.globalAlpha = .45;
      c.strokeStyle = '#ff6b6b';
      c.lineWidth = 2;
      c.setLineDash([10, 10]);
      c.beginPath(); c.moveTo(a.x, a.y); c.lineTo(a.x + a.dx * 2600, a.y + a.dy * 2600); c.stroke();
      c.setLineDash([]);
    }
    const square = this.level === 0;
    for (const p of this.parts) {
      const k = p.l / p.m;
      if (p.k === 'p') {
        c.globalAlpha = k;
        c.fillStyle = p.c;
        const r = p.r * k + .5;
        if (square) c.fillRect(p.x - r, p.y - r, r * 2, r * 2);
        else { c.beginPath(); c.arc(p.x, p.y, r, 0, 6.283); c.fill(); }
      } else if (p.k === 'r') {
        const r = p.r0 + (p.r1 - p.r0) * (1 - k);
        c.globalAlpha = k * .9;
        c.strokeStyle = p.c;
        c.lineWidth = p.w * k + .5;
        c.beginPath(); c.arc(p.x, p.y, Math.max(1, r), 0, 6.283); c.stroke();
      } else if (p.k === 'a') {
        c.globalAlpha = k;
        c.strokeStyle = p.c;
        c.lineWidth = 7 * k + 1;
        c.save();
        c.translate(p.x, p.y); c.scale(p.d, 1);
        c.beginPath(); c.arc(0, 0, 48 * p.s, -1.0, 1.0); c.stroke();
        c.restore();
      } else if (p.k === 'n') {
        c.globalAlpha = Math.min(1, k * 2);
        c.textAlign = 'center';
        c.font = `800 ${p.s}px "Segoe UI",sans-serif`;
        if (this.level > 0) {
          c.strokeStyle = 'rgba(0,0,0,.8)';
          c.lineWidth = 4;
          c.strokeText(p.t, p.x, p.y);
        }
        c.fillStyle = p.c;
        c.fillText(p.t, p.x, p.y);
      }
    }
    c.globalAlpha = 1;
  },

  /* ---------------------------- HUD ---------------------------- */
  drawHUD(c, v) {
    const s = v.s;
    const byTeam = [[], []];
    for (const p of v.p) {
      const i = this.players[p.i];
      if (i) byTeam[i.team].push(p);
    }
    byTeam[0].forEach((p, i) => this.card(c, 22, 22 + i * 66, p, 1));
    byTeam[1].forEach((p, i) => this.card(c, this.CW - 22 - 320, 22 + i * 66, p, -1));

    c.textAlign = 'center';
    c.font = '700 22px "Segoe UI",sans-serif';
    c.fillStyle = 'rgba(230,235,245,.8)';
    const tl = Math.max(0, s.tl || 0);
    c.fillText(`${String(Math.floor(tl / 60)).padStart(2, '0')}:${String(Math.floor(tl % 60)).padStart(2, '0')}`,
               this.CW / 2, 40);

    const mine = v.p.find(p => p.i === this.myPid);
    if (mine) {
      this.selfBar(c, mine);
      if (s.st === 'countdown') this.briefing(c, mine);
    }
  },

  card(c, x, y, p, dir) {
    const info = this.players[p.i] || {};
    const hero = this.heroes[info.hero] || {};
    const tc = TEAM_COL[info.team || 0];
    c.fillStyle = 'rgba(10,13,22,.62)';
    this.rr(c, x, y, 320, 56, 10); c.fill();
    c.strokeStyle = hexA(tc, .5); c.lineWidth = 1.5; c.stroke();
    const gl = GLYPH[info.hero] || glyphFallback(info.hero);
    gl(c, dir > 0 ? x + 30 : x + 290, y + 28, 30, hero.color || '#fff');
    const bx = dir > 0 ? x + 56 : x + 22;
    c.textAlign = 'left';
    c.font = '600 14px "Segoe UI",sans-serif';
    c.fillStyle = '#dfe6f5';
    c.fillText((info.name || '') + (info.bot ? ' ·бот' : ''), bx, y + 17);
    const w = 210, hpk = Math.max(0, p.hp / (hero.hp || 100));
    c.fillStyle = 'rgba(0,0,0,.55)'; this.rr(c, bx, y + 24, w, 11, 5); c.fill();
    c.fillStyle = hpk > .35 ? tc : '#ff4d6d'; this.rr(c, bx, y + 24, w * hpk, 11, 5); c.fill();
    if (p.sh > 0) {
      c.fillStyle = 'rgba(200,240,255,.95)';
      this.rr(c, bx, y + 24, w * Math.min(1, p.sh / 40), 11, 5); c.fill();
    }
    c.font = '700 11px "Segoe UI",sans-serif';
    c.fillStyle = 'rgba(255,255,255,.85)';
    c.fillText(Math.ceil(p.hp) + (p.sh > 0 ? ' +' + Math.ceil(p.sh) : ''), bx + 4, y + 33);
    c.fillStyle = 'rgba(0,0,0,.55)'; c.fillRect(bx, y + 38, w, 5);
    c.fillStyle = p.u >= 100 ? '#ffd166' : '#8ab4ff';
    c.fillRect(bx, y + 38, w * Math.min(1, p.u / 100), 5);
    c.fillStyle = tc;
    for (let i = 0; i < p.st; i++) {
      c.beginPath(); c.arc(bx + w + 14 + i * 13, y + 29, 4.5, 0, 6.283); c.fill();
    }
    if (!p.al) {
      c.fillStyle = 'rgba(0,0,0,.55)';
      this.rr(c, x, y, 320, 56, 10); c.fill();
      c.fillStyle = '#ff9aab';
      c.font = '700 16px "Segoe UI",sans-serif';
      c.textAlign = 'center';
      c.fillText(p.st > 0 ? `возрождение ${p.rt.toFixed(1)}` : 'выбыл', x + 160, y + 34);
    }
  },

  selfBar(c, p) {
    const info = this.players[p.i] || {};
    const hero = this.heroes[info.hero] || {};
    const keys = ['basic', 'q', 'e', 'r'];
    const cds = { basic: 0, q: p.cd[0], e: p.cd[1], r: 0 };
    const abs = hero.abilities || [];
    const bw = 74, gap = 10, n = keys.length + 1;
    const total = n * bw + (n - 1) * gap;
    let x = this.CW / 2 - total / 2;
    const y = this.CH - 104;
    keys.forEach(k => {
      const ab = abs.find(a => a.key === k) || {};
      const cd = cds[k], maxcd = ab.cd || 1;
      const ready = k === 'r' ? p.u >= 100 : cd <= 0;
      c.fillStyle = 'rgba(10,13,22,.72)';
      this.rr(c, x, y, bw, bw, 12); c.fill();
      c.strokeStyle = ready ? (hero.color || '#fff') : 'rgba(120,130,155,.5)';
      c.lineWidth = ready ? 2.5 : 1.5;
      c.stroke();
      c.globalAlpha = ready ? 1 : .35;
      ABICON[k](c, x + bw / 2, y + bw / 2 - 4, 32, hero.color || '#fff');
      c.globalAlpha = 1;
      if (!ready) {
        if (k === 'r') {
          c.fillStyle = 'rgba(0,0,0,.55)';
          this.rr(c, x, y + bw * (p.u / 100), bw, bw * (1 - p.u / 100), 12); c.fill();
          c.fillStyle = '#ffd166'; c.textAlign = 'center';
          c.font = '700 15px "Segoe UI",sans-serif';
          c.fillText(Math.floor(p.u) + '%', x + bw / 2, y + bw - 12);
        } else {
          c.fillStyle = 'rgba(0,0,0,.6)';
          this.rr(c, x, y, bw, bw * Math.min(1, cd / maxcd), 12); c.fill();
          c.fillStyle = '#fff'; c.textAlign = 'center';
          c.font = '800 22px "Segoe UI",sans-serif';
          c.fillText(cd.toFixed(1), x + bw / 2, y + bw / 2 + 8);
        }
      }
      c.fillStyle = 'rgba(230,236,250,.9)';
      c.textAlign = 'center';
      c.font = '700 11px "Segoe UI",sans-serif';
      c.fillText(KEYLABEL[k], x + bw / 2, y + bw - 6);
      x += bw + gap;
    });
    const dcd = p.cd[2];
    c.fillStyle = 'rgba(10,13,22,.72)';
    this.rr(c, x, y, bw, bw, 12); c.fill();
    c.strokeStyle = dcd <= 0 ? '#9fd8ff' : 'rgba(120,130,155,.5)';
    c.lineWidth = dcd <= 0 ? 2.5 : 1.5;
    c.stroke();
    c.fillStyle = dcd <= 0 ? '#9fd8ff' : 'rgba(150,160,185,.5)';
    c.textAlign = 'center';
    c.font = '800 26px "Segoe UI",sans-serif';
    c.fillText('»', x + bw / 2, y + bw / 2 + 8);
    c.font = '700 11px "Segoe UI",sans-serif';
    c.fillStyle = 'rgba(230,236,250,.9)';
    c.fillText('SHIFT', x + bw / 2, y + bw - 6);
    if (dcd > 0) {
      c.fillStyle = 'rgba(0,0,0,.6)';
      this.rr(c, x, y, bw, bw * Math.min(1, dcd / 2.2), 12); c.fill();
    }
  },

  /* Пока идёт отсчёт — напоминаем, что вообще умеет выбранный герой. */
  briefing(c, p) {
    const info = this.players[p.i] || {};
    const hero = this.heroes[info.hero] || {};
    const parts = (hero.abilities || []).map(a => [KEYLABEL[a.key], a.name]).concat([['SHIFT', 'Рывок']]);
    c.font = '600 17px "Segoe UI",sans-serif';
    const gap = 26;
    let total = 0;
    const widths = parts.map(([k, n]) => {
      const w = c.measureText(k + '  ' + n).width;
      total += w + gap;
      return w;
    });
    total -= gap;
    let x = this.CW / 2 - total / 2;
    const y = this.CH - 136;
    c.fillStyle = 'rgba(8,10,18,.72)';
    this.rr(c, x - 18, y - 24, total + 36, 36, 10); c.fill();
    parts.forEach(([k, n], i) => {
      c.textAlign = 'left';
      c.fillStyle = hero.color || '#fff';
      c.font = '800 17px "Segoe UI",sans-serif';
      c.fillText(k, x, y);
      const kw = c.measureText(k).width;
      c.fillStyle = 'rgba(226,233,248,.9)';
      c.font = '400 17px "Segoe UI",sans-serif';
      c.fillText('  ' + n, x + kw, y);
      x += widths[i] + gap;
    });
    c.textAlign = 'center';
    c.font = '600 15px "Segoe UI",sans-serif';
    c.fillStyle = 'rgba(150,160,185,.9)';
    c.fillText(`${hero.name} · ${hero.passive ? hero.passive.name + ': ' + hero.passive.desc : ''}`,
               this.CW / 2, y - 40);
  }
};
