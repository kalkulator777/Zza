// -*- coding: utf-8 -*-
// Бэкенд рендера Canvas2D (DESIGN.md 7.1 интерфейс, 7.2 приёмы).
//
// Интерфейс ровно из 7.1: init / resize / draw / dispose / stats.
// draw() не меняет мир и не читает сеть — только рисует то, что дали.
//
// Обязательные приёмы 7.2, все три:
//   1. статический слой тайлов рисуется в offscreen-канву и блитится ОДНИМ
//      drawImage; перерисовывается только при смене этажа или когда камера
//      уехала за край окна кэша;
//   2. в горячем пути нет ни shadowBlur, ни filter, ни
//      globalCompositeOperation: свет и затемнение — один блит заранее
//      посчитанного оверлея, тень — плоский эллипс;
//   3. текст рисуется в маленькие канвы один раз и дальше блитится.
//
// Объём подделывается: тень эллипсом под объектом, у стен — вертикальный
// выступ вверх (рисуется в статическом слое, то есть бесплатно).

import { K_PLAYER, K_ENEMY, K_PROP, K_SHOT, F_DEAD, F_OFFLINE,
         TILE_WALL, TILE_STAIRS, VIS_DARK, VIS_LIT }
  from './state.js';

const MARGIN_TILES = 10;     // запас кэша карты вокруг экрана, в клетках
const WALL_RISE = 0.45;      // высота фальшивого выступа стены, в клетках

// --- туман (DESIGN.md 4.4, значения 5.2) ---------------------------------
//
// Маска тумана — ОТДЕЛЬНАЯ маленькая канва, FOG_PX пикселей на клетку, и
// она перерисовывается ТОЛЬКО когда пришла дельта vis (или уехало окно
// кэша карты), а не каждый кадр. В кадре тумана стоит ровно один
// drawImage с растяжением: маска 4 px/клетка растягивается в 48 px/клетка
// самим браузером. Считать маску экранного размера каждый кадр — ровно то,
// чего 7.2 запрещать не успевает, но что убивает бюджет вернее любого
// shadowBlur: 1920x1080 = 2 млн пикселей на кадр.
//
// FOG_PX = 4 выбран так: растяжение в 12 раз даёт билинейную кайму шириной
// в один пиксель маски, то есть 1/4 клетки (12 экранных пикселей) — граница
// света мягкая, но свет не течёт в неразведанную клетку целиком. Маска на
// окно 47x35 клеток = 188x140 = 26 тыс. пикселей: перерисовать её — это
// заполнить 105 КБ, десятые доли миллисекунды, и происходит это 5 раз в
// секунду (обзор пересчитывается при переходе игрока в другую клетку,
// server/vis.py), а не 100.
const FOG_PX = 4;
// Выступ стены (WALL_RISE) торчит в клетку СВЕРХУ. У неразведанной стены
// его надо закрыть, иначе из черноты выглядывает полоска стены, которой
// группа не видела. 0.45 клетки * 4 = 1.8 -> 2 пикселя маски.
const FOG_RISE = Math.max(1, Math.round(WALL_RISE * FOG_PX));
// «Открыта, но не видна» — холодная, приглушённая. Не чёрная: стены надо
// помнить. Альфа 0.62 гасит пол с (35,41,58) до (18,24,41), то есть в 0.62
// раза по яркости, и уводит его в синеву (r/b падает с 0.60 до 0.45).
// Замерено на живом кадре: при 0.52 та же клетка светила 0.70..0.75 от
// освещённой — «приглушённая», но не «сразу видно, что это память».
// Ниже опускать нельзя: самый тёмный разведанный пиксель (боковина стены
// #1b2028 в дальнем углу экрана, под виньеткой) при 0.62 даёт (7,10,16) —
// до порога черноты 10 по максимальному каналу остаётся полтора раза, а
// стену в памяти надо видеть, иначе это уже не память, а темнота.
const FOG_SEEN_RGBA = [8, 13, 30, 158];    // 158/255 = 0.62

const COL = {
  bg: '#05070c',
  floor: '#23293a',
  floorAlt: '#262d40',
  grid: 'rgba(0,0,0,0.16)',
  wall: '#454d63',
  wallTop: '#666f88',
  wallSide: '#1b2028',        // боковина: тот же камень, но в тени
  shadow: 'rgba(0,0,0,0.40)',
  // Лестница вниз (4.1, тайл 2). Тёплое золото против холодного пола:
  // единственная клетка на этаж, найти её надо с одного взгляда.
  stairsBg: '#3b2f14',
  stairsStepA: '#caa14a',
  stairsStepB: '#8f6f2c',
  stairsEdge: '#f0c96a',
};

const KIND = {};
KIND[K_PLAYER] = { r: 0.35, css: '#7fd18a', ring: '#dff3e2' };
KIND[K_ENEMY] = { r: 0.35, css: '#d16a6a', ring: '#f5cccc' };
KIND[K_PROP] = { r: 0.30, css: '#8b7f5a', ring: '#cfc49c' };
KIND[K_SHOT] = { r: 0.12, css: '#ffe27a', ring: '#fff6cf' };
const KIND_DEF = { r: 0.30, css: '#8b93a1', ring: '#d8dde5' };

// --- кэш текста (7.2): строка рисуется в свою канву один раз -------------
const textCache = new Map();
function textSprite(text, font, color) {
  const key = text + '|' + font + '|' + color;
  let c = textCache.get(key);
  if (c) return c;
  const m = document.createElement('canvas');
  const mc = m.getContext('2d');
  mc.font = font;
  const w = Math.ceil(mc.measureText(text).width) + 6;
  const h = Math.ceil(parseInt(font, 10) * 1.6) + 4;
  m.width = w; m.height = h;
  const g = m.getContext('2d');
  g.font = font;
  g.textBaseline = 'middle';
  g.fillStyle = 'rgba(0,0,0,0.55)';
  g.fillText(text, 4, h / 2 + 1);
  g.fillStyle = color;
  g.fillText(text, 3, h / 2);
  textCache.set(key, m);
  return m;
}

// Лестница вниз (4.1). Рисуется в СТАТИЧЕСКОМ слое, то есть в кадре стоит
// ноль. Заметность даётся тремя вещами сразу — тёплым цветом против
// холодного пола, ступенями поперёк и яркой рамкой: так клетка читается и
// краем глаза, и в «памяти группы», где всё приглушено.
function paintStairs(g, px, py, s) {
  g.fillStyle = COL.stairsBg;
  g.fillRect(px, py, s, s);
  const steps = 4;
  for (let i = 0; i < steps; i++) {
    // Ступени сужаются книзу — читается как уходящий вниз пролёт.
    const inset = s * (0.09 + 0.07 * i);
    g.fillStyle = (i & 1) ? COL.stairsStepB : COL.stairsStepA;
    g.fillRect(px + inset, py + s * (0.13 + 0.20 * i), s - inset * 2, s * 0.12);
  }
  const lw = Math.max(2, s * 0.06);
  g.strokeStyle = COL.stairsEdge;
  g.lineWidth = lw;
  g.strokeRect(px + lw / 2, py + lw / 2, s - lw, s - lw);
}

export function createRenderer() {
  return {
    name: 'Canvas2D',
    canvas: null, ctx: null,
    w: 0, h: 0, tilePx: 48, quality: 'high',

    // статический слой карты
    st: null, stCtx: null,
    stTx: 0, stTy: 0, stTw: 0, stTh: 0, stKey: '',
    repaints: 0,

    // предрасчитанный оверлей света/затемнения
    vig: null,

    // маска тумана: FOG_PX пикселей на клетку, окно то же, что у карты
    fog: null, fogCtx: null, fogImg: null, fogKey: '', fogOn: false,
    fogRepaints: 0,

    camX: 0, camY: 0,
    _drawCalls: 0, _ents: 0, _ms: 0, _hidden: 0,
    _pool: [], _vis: [],

    // --- 7.1 ---------------------------------------------------------

    init(canvas, opts) {
      opts = opts || {};
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d', { alpha: false });
      this.tilePx = opts.tilePx || 48;       // 4.1: базовый масштаб
      this.quality = opts.quality || 'high';
      this.resize(canvas.width || 960, canvas.height || 540);
    },

    resize(w, h) {
      w = Math.max(1, Math.round(w));
      h = Math.max(1, Math.round(h));
      this.w = w; this.h = h;
      if (this.canvas && (this.canvas.width !== w || this.canvas.height !== h)) {
        this.canvas.width = w;
        this.canvas.height = h;
      }
      this._buildVignette();
      this.stKey = '';                        // окно кэша карты больше не годится
      this.fogKey = '';                       // маска тумана вместе с ним
    },

    dispose() {
      this.st = null; this.stCtx = null; this.vig = null;
      this.fog = null; this.fogCtx = null; this.fogImg = null;
      this.ctx = null; this.canvas = null;
      textCache.clear();
    },

    stats() {
      // hidden — сущности, скрытые туманом (их сервер прислал, а группа их
      // не видит). Число для tests/client_fog.py и для оверлея отладки.
      return { drawCalls: this._drawCalls, ents: this._ents, ms: this._ms,
               hidden: this._hidden, fogRepaints: this.fogRepaints };
    },

    // --- камера ------------------------------------------------------

    /** Экранные пиксели -> мировые клетки. Нужно input.js для прицела. */
    screenToWorld(sx, sy) {
      return [this.camX + sx / this.tilePx, this.camY + sy / this.tilePx];
    },

    /** Мировые клетки -> экранные пиксели. Обратное к screenToWorld. */
    worldToScreen(wx, wy) {
      return [(wx - this.camX) * this.tilePx, (wy - this.camY) * this.tilePx];
    },

    _updateCamera(view) {
      const tw = this.w / this.tilePx, th = this.h / this.tilePx;
      const self = view.self;
      let cx, cy;
      if (self) { cx = self.dx - tw / 2; cy = self.dy - th / 2; }
      else if (view.level) { cx = view.level.w / 2 - tw / 2; cy = view.level.h / 2 - th / 2; }
      else { cx = 0; cy = 0; }
      if (view.level) {
        // Карта меньше экрана — центрируем её, а не упираем в угол.
        cx = view.level.w <= tw ? (view.level.w - tw) / 2
                                : Math.max(0, Math.min(view.level.w - tw, cx));
        cy = view.level.h <= th ? (view.level.h - th) / 2
                                : Math.max(0, Math.min(view.level.h - th, cy));
      }
      this.camX = cx; this.camY = cy;
    },

    // --- 7.2 приём 1: статический слой одним блитом -------------------

    _ensureStatic(level) {
      if (!level) return false;
      const tpx = this.tilePx;
      const needW = Math.ceil(this.w / tpx) + MARGIN_TILES * 2;
      const needH = Math.ceil(this.h / tpx) + MARGIN_TILES * 2;
      const tw = Math.min(level.w, needW);
      const th = Math.min(level.h, needH);

      // Окно кэша держим так, чтобы экран был внутри с запасом.
      let tx = Math.floor(this.camX) - MARGIN_TILES;
      let ty = Math.floor(this.camY) - MARGIN_TILES;
      tx = Math.max(0, Math.min(level.w - tw, tx));
      ty = Math.max(0, Math.min(level.h - th, ty));

      const inside = this.st &&
        this.stKey === (level.seed + ':' + level.floor + ':' + tw + 'x' + th) &&
        this.camX >= this.stTx && this.camY >= this.stTy &&
        this.camX + this.w / tpx <= this.stTx + this.stTw &&
        this.camY + this.h / tpx <= this.stTy + this.stTh;
      if (inside) return true;

      if (!this.st || this.stTw !== tw || this.stTh !== th) {
        this.st = document.createElement('canvas');
        this.st.width = tw * tpx;
        this.st.height = th * tpx;
        this.stCtx = this.st.getContext('2d', { alpha: false });
        this.stTw = tw; this.stTh = th;
      }
      this.stTx = tx; this.stTy = ty;
      this.stKey = level.seed + ':' + level.floor + ':' + tw + 'x' + th;
      this._paintStatic(level);
      this.repaints++;
      return true;
    },

    _paintStatic(level) {
      const g = this.stCtx, tpx = this.tilePx;
      const tx0 = this.stTx, ty0 = this.stTy, tw = this.stTw, th = this.stTh;
      g.fillStyle = COL.bg;
      g.fillRect(0, 0, this.st.width, this.st.height);
      const tiles = level.tiles;
      const solid = (x, y) => (x < 0 || y < 0 || x >= level.w || y >= level.h)
        ? true : tiles[y * level.w + x] === TILE_WALL;

      // Пол и сетка
      const stairs = [];
      for (let y = 0; y < th; y++) {
        for (let x = 0; x < tw; x++) {
          const wx = tx0 + x, wy = ty0 + y;
          if (solid(wx, wy)) continue;
          g.fillStyle = ((wx ^ wy) & 1) ? COL.floor : COL.floorAlt;
          g.fillRect(x * tpx, y * tpx, tpx, tpx);
          if (wx >= 0 && wy >= 0 && wx < level.w && wy < level.h &&
              tiles[wy * level.w + wx] === TILE_STAIRS) {
            stairs.push(x, y);
          }
        }
      }
      g.strokeStyle = COL.grid;
      g.lineWidth = 1;
      g.beginPath();
      for (let x = 0; x <= tw; x++) { g.moveTo(x * tpx + 0.5, 0); g.lineTo(x * tpx + 0.5, th * tpx); }
      for (let y = 0; y <= th; y++) { g.moveTo(0, y * tpx + 0.5); g.lineTo(tw * tpx, y * tpx + 0.5); }
      g.stroke();

      // Лестница — ПОСЛЕ сетки, иначе её штрих режет ступени пополам.
      for (let i = 0; i < stairs.length; i += 2) {
        paintStairs(g, stairs[i] * tpx, stairs[i + 1] * tpx, tpx);
      }

      // Стены: сначала вертикальный выступ (объём подделкой, 7.2), потом
      // «крышка». Рисуется один раз на перепокраску, в кадре стоит ноль.
      const rise = WALL_RISE * tpx;
      for (let y = 0; y < th; y++) {
        for (let x = 0; x < tw; x++) {
          const wx = tx0 + x, wy = ty0 + y;
          if (!solid(wx, wy)) continue;
          const px = x * tpx, py = y * tpx;
          // Верхняя грань — тот же тайл, поднятый на rise. Нижняя полоса
          // тайла остаётся «боковиной» стены: вместе они закрывают клетку
          // целиком, без щелей, а стена кажется выше пола.
          if (!solid(wx, wy + 1)) {          // боковина видна, только если снизу пол
            g.fillStyle = COL.wallSide;
            g.fillRect(px, py + tpx - rise, tpx, rise);
          }
          g.fillStyle = COL.wall;
          g.fillRect(px, py - rise, tpx, tpx);
          // Светлая кромка — только там, где стена ВЫГЛЯДЫВАЕТ из-за
          // соседа сверху. Иначе сплошной массив стен превращается в
          // полосатый забор: кромка рисуется на каждом тайле подряд.
          if (!solid(wx, wy - 1)) {
            g.fillStyle = COL.wallTop;
            g.fillRect(px, py - rise, tpx, Math.max(2, tpx * 0.09));
          }
        }
      }
    },

    // --- туман: маска строится РЕДКО, в кадре только блит --------------

    /**
     * Готова ли маска тумана к кадру. Перерисовывается только тогда, когда
     * что-то из этого изменилось: сам туман (fogVersion растёт в state.js
     * при дельте vis), окно кэша карты или этаж. Пока игрок стоит на месте,
     * маска не трогается вовсе — обзор от кадра не зависит.
     */
    _ensureFog(level, view) {
      const arr = view.fog;
      if (!level || !arr || !this.st) { this.fogOn = false; return false; }
      this.fogOn = true;
      const cw = this.stTw * FOG_PX, ch = this.stTh * FOG_PX;
      if (!this.fog || this.fog.width !== cw || this.fog.height !== ch) {
        this.fog = document.createElement('canvas');
        this.fog.width = cw; this.fog.height = ch;
        this.fogCtx = this.fog.getContext('2d');
        this.fogImg = null;
        this.fogKey = '';
      }
      const key = this.stKey + '|' + this.stTx + ',' + this.stTy +
                  '|' + view.fogVersion;
      if (key === this.fogKey) return true;
      this.fogKey = key;
      this._paintFog(level, arr);
      this.fogRepaints++;
      return true;
    },

    _paintFog(level, arr) {
      const cw = this.fog.width, ch = this.fog.height;
      let img = this.fogImg;
      if (!img || img.width !== cw || img.height !== ch) {
        img = this.fogImg = this.fogCtx.createImageData(cw, ch);
      }
      const d = img.data;
      d.fill(0);                       // VIS_LIT = прозрачно, полный свет
      const tx0 = this.stTx, ty0 = this.stTy, tw = this.stTw, th = this.stTh;
      const W = level.w, H = level.h, tiles = level.tiles;
      const sr = FOG_SEEN_RGBA[0], sg = FOG_SEEN_RGBA[1];
      const sb = FOG_SEEN_RGBA[2], sa = FOG_SEEN_RGBA[3];

      for (let y = 0; y < th; y++) {
        const wy = ty0 + y;
        const inY = wy >= 0 && wy < H;
        for (let x = 0; x < tw; x++) {
          const wx = tx0 + x;
          // За краем карты — та же чернота: там нет ничего и знать нечего.
          let v = VIS_DARK, wall = true;
          if (inY && wx >= 0 && wx < W) {
            const i = wy * W + wx;
            v = arr[i];
            wall = tiles[i] === TILE_WALL;
          }
          if (v === VIS_LIT) continue;
          let r, g, b, a;
          let y0 = y * FOG_PX;
          const y1 = y0 + FOG_PX;
          if (v === VIS_DARK) {
            r = 0; g = 0; b = 0; a = 255;
            // Закрыть выступ неразведанной стены, торчащий в клетку сверху.
            // Строки идут сверху вниз, поэтому дописываем поверх уже
            // положенного — так и надо: стены там нет, пока её не увидели.
            if (wall && y0 >= FOG_RISE) y0 -= FOG_RISE;
          } else {
            r = sr; g = sg; b = sb; a = sa;
          }
          for (let py = y0; py < y1; py++) {
            let o = (py * cw + x * FOG_PX) * 4;
            for (let px = 0; px < FOG_PX; px++) {
              d[o] = r; d[o + 1] = g; d[o + 2] = b; d[o + 3] = a;
              o += 4;
            }
          }
        }
      }
      this.fogCtx.putImageData(img, 0, 0);
    },

    /** Видна ли клетка, в которой стоит сущность (5.2: только VIS_LIT). */
    _lit(view, wx, wy) {
      const L = view.level, f = view.fog;
      const tx = Math.floor(wx), ty = Math.floor(wy);
      if (tx < 0 || ty < 0 || tx >= L.w || ty >= L.h) return false;
      return f[ty * L.w + tx] === VIS_LIT;
    },

    // --- 7.2 приём 2: свет предрасчитан, в кадре только блит ----------

    _buildVignette() {
      const c = document.createElement('canvas');
      c.width = this.w; c.height = this.h;
      const g = c.getContext('2d');
      const cx = this.w / 2, cy = this.h / 2;
      const torchR = Math.min(this.w, this.h) * 0.34;
      const edgeR = Math.max(this.w, this.h) * 0.78;
      const grad = g.createRadialGradient(cx, cy, torchR * 0.12, cx, cy, edgeR);
      grad.addColorStop(0, 'rgba(48,32,4,0)');
      grad.addColorStop(0.38, 'rgba(10,8,4,0.16)');
      grad.addColorStop(1, 'rgba(0,0,0,0.80)');
      g.fillStyle = grad;
      g.fillRect(0, 0, this.w, this.h);
      this.vig = c;
    },

    // --- сущности ----------------------------------------------------

    _drawEntity(e, sx, sy, isSelf) {
      const ctx = this.ctx, tpx = this.tilePx;
      const k = KIND[e.kind] || KIND_DEF;
      const r = k.r * tpx;
      const dead = (e.flags & F_DEAD) !== 0;
      const offline = (e.flags & F_OFFLINE) !== 0;

      // Тень — плоский эллипс, без shadowBlur (7.2).
      if (!dead) {
        ctx.fillStyle = COL.shadow;
        ctx.beginPath();
        ctx.ellipse(sx, sy + r * 0.75, r * 0.95, r * 0.42, 0, 0, Math.PI * 2);
        ctx.fill();
        this._drawCalls++;
      }

      // 4.3: бит 1 = мёртв -> дух (полупрозрачный, холодный).
      // бит 2 = игрок отвалился -> гасим, а не оставляем столбом.
      let alpha = 1;
      if (dead) alpha = 0.32;
      if (offline) alpha = Math.min(alpha, 0.28);
      ctx.globalAlpha = alpha;

      ctx.fillStyle = dead ? '#9fc8ff' : (offline ? '#6b7280' : (isSelf ? '#ffe27a' : k.css));
      ctx.beginPath();
      ctx.arc(sx, sy, r, 0, Math.PI * 2);
      ctx.fill();
      this._drawCalls++;

      if (isSelf) {
        ctx.strokeStyle = '#fff6cf';
        ctx.lineWidth = Math.max(1.5, r * 0.16);
        ctx.beginPath();
        ctx.arc(sx, sy, r * 1.35, 0, Math.PI * 2);
        ctx.stroke();
        this._drawCalls++;
      }

      // Направление взгляда (facing в радианах, 4.3)
      if (e.kind === K_PLAYER || e.kind === K_ENEMY) {
        ctx.strokeStyle = dead ? 'rgba(200,225,255,0.7)' : k.ring;
        ctx.lineWidth = Math.max(1.5, r * 0.20);
        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(sx + Math.cos(e.dfacing) * r * 1.5, sy + Math.sin(e.dfacing) * r * 1.5);
        ctx.stroke();
        this._drawCalls++;
      }
      ctx.globalAlpha = 1;

      // Полоска здоровья — ради неё hp_max и живёт в снапшоте (4.3)
      if (!dead && e.hpMax > 0 && e.hp < e.hpMax &&
          (e.kind === K_PLAYER || e.kind === K_ENEMY)) {
        const bw = tpx * 0.8, bh = Math.max(3, tpx * 0.09);
        const bx = sx - bw / 2, by = sy - r - bh * 2.2;
        ctx.fillStyle = 'rgba(0,0,0,0.6)';
        ctx.fillRect(bx - 1, by - 1, bw + 2, bh + 2);
        ctx.fillStyle = '#d16a6a';
        ctx.fillRect(bx, by, bw, bh);
        ctx.fillStyle = '#7fd18a';
        ctx.fillRect(bx, by, bw * Math.max(0, e.hp / e.hpMax), bh);
        this._drawCalls += 3;
      }

      // Текст — из кэша, не fillText каждый кадр (7.2)
      if (offline) {
        const spr = textSprite('нет связи', '12px system-ui, sans-serif', '#c0c6d2');
        ctx.drawImage(spr, sx - spr.width / 2, sy - r - spr.height - 4);
        this._drawCalls++;
      } else if (dead) {
        const spr = textSprite('дух', '12px system-ui, sans-serif', '#9fc8ff');
        ctx.drawImage(spr, sx - spr.width / 2, sy - r - spr.height - 4);
        this._drawCalls++;
      }
    },

    // --- кадр --------------------------------------------------------

    draw(view, alpha) {
      const t0 = performance.now();
      const ctx = this.ctx;
      if (!ctx) return;
      this._drawCalls = 0;

      this._updateCamera(view);
      const tpx = this.tilePx;

      // 1) карта — ОДИН drawImage из offscreen (7.2)
      if (this._ensureStatic(view.level)) {
        const sx = (this.camX - this.stTx) * tpx;
        const sy = (this.camY - this.stTy) * tpx;
        ctx.fillStyle = COL.bg;
        ctx.fillRect(0, 0, this.w, this.h);
        ctx.drawImage(this.st, sx, sy, this.w, this.h, 0, 0, this.w, this.h);
        this._drawCalls += 2;
      } else {
        ctx.fillStyle = COL.bg;
        ctx.fillRect(0, 0, this.w, this.h);
        this._drawCalls++;
      }

      // 2) туман — ОДИН drawImage растянутой маски. Сама маска посчитана
      // выше и только тогда, когда менялся туман (4.4): в кадре тумана
      // стоит ровно один блит, как и карты.
      if (this._ensureFog(view.level, view) && this.fog) {
        const fx = (this.camX - this.stTx) * FOG_PX;
        const fy = (this.camY - this.stTy) * FOG_PX;
        ctx.drawImage(this.fog,
                      fx, fy, (this.w / tpx) * FOG_PX, (this.h / tpx) * FOG_PX,
                      0, 0, this.w, this.h);
        this._drawCalls++;
      }

      // 3) сущности, сверху вниз по y — иначе тень ложится поверх соседа.
      // Список видимых переиспользуется: мусор на 200 сущностей 100 раз в
      // секунду сборщик собирает не бесплатно, а поля мира draw() трогать
      // не имеет права (7.1).
      //
      // ЗДЕСЬ ЖЕ ТУМАН РЕЖЕТ СУЩНОСТЕЙ. Сервер шлёт всех без разбора (4.4:
      // снапшот один на комнату), и прятать невидимое обязан клиент. Иначе
      // враг в неосвещённой комнате виден сквозь темноту — это прямая
      // выдача того, чего группа не видит. Своя сущность рисуется всегда:
      // мёртвый игрок туман не светит (4.4) и иначе потерял бы сам себя.
      const ents = view.ents;
      const pool = this._pool, vis = this._vis;
      const fogOn = !!(view.fog && view.level);
      let hidden = 0;
      vis.length = 0;
      for (let i = 0; i < ents.length; i++) {
        const e = ents[i];
        const sx = (e.dx - this.camX) * tpx;
        const sy = (e.dy - this.camY) * tpx;
        if (sx < -tpx * 2 || sy < -tpx * 2 || sx > this.w + tpx * 2 || sy > this.h + tpx * 2) continue;
        if (fogOn && e.id !== view.selfId && !this._lit(view, e.dx, e.dy)) {
          hidden++;
          continue;
        }
        let rec = pool[vis.length];
        if (rec === undefined) { rec = { e: null, sx: 0, sy: 0 }; pool.push(rec); }
        rec.e = e; rec.sx = sx; rec.sy = sy;
        vis.push(rec);
      }
      vis.sort((a, b) => a.e.dy - b.e.dy);
      for (let i = 0; i < vis.length; i++) {
        const rec = vis[i];
        this._drawEntity(rec.e, rec.sx, rec.sy, rec.e.id === view.selfId);
      }
      this._ents = ents.length;
      this._hidden = hidden;

      // 4) свет и затемнение — один блит предрасчитанного оверлея (7.2).
      // q=low обходится без него: это украшение, а не механика.
      if (this.quality !== 'low' && this.vig) {
        ctx.drawImage(this.vig, 0, 0);
        this._drawCalls++;
      }

      this._ms = performance.now() - t0;
    },
  };
}

export default createRenderer;
