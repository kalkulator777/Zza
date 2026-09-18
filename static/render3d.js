// -*- coding: utf-8 -*-
// Бэкенд рендера three.js (DESIGN.md 7.1 интерфейс, 7.3 приёмы).
//
// Интерфейс ровно из 7.1: init / resize / draw / dispose / stats. Плюс то,
// что от бэкенда требует уже написанный клиент: screenToWorld /
// worldToScreen (прицел в input.js и крючки проверок), hpBarRect(),
// выключатели combat / windup / torch и поля camX / camY / tilePx.
//
// Обязательные приёмы 7.3, все четыре:
//   1. InstancedMesh на каждый тип тайла и на каждый вид сущности; в кадре
//      их два десятка, а не по вызову на тело (цель 7.3 — не больше 40);
//   2. камера СТРОГО ортографическая под углом, не перспектива: сетка
//      остаётся читаемой, клетка остаётся клеткой в любом углу кадра;
//   3. карты теней на минимальных настройках выключены, свет ЗАПЕЧЁН — в
//      вершинные цвета геометрии (форма тела) и в цвет экземпляра (туман и
//      факел). Динамических источников света в сцене нет вообще ни одного;
//   4. модели генерируются кодом из примитивов, никаких .glb.
//
// --- ПОЧЕМУ СВЕТ ЗАПЕЧЁН, А НЕ ВЗЯТ НАСТОЯЩИМ ИСТОЧНИКОМ --------------
//
// Соблазн очевидный: в 3D есть PointLight, поставь его игроку в руку — и
// факел получится сам собой. Он и получится, вместе с утечкой из 5.2.
// Настоящий точечный свет НЕ ЗНАЕТ ПРО СТЕНЫ (карт теней на q=low нет по
// самому же 7.3) и светит СКВОЗЬ них: клетка за стеной в трёх шагах от
// игрока — это VIS_SEEN, «помню, но сейчас не вижу», и она обязана быть
// тусклой. Точечный свет поднял бы ей яркость ровно так же, как соседней
// освещённой, и по подсветке стало бы видно то, чего группа не видит.
// Поэтому свет считается ПО КЛЕТКАМ ТУМАНА, тем же расстоянием до
// ближайшего живого игрока, что и в render2d.js, и кладётся в цвет
// экземпляра тайла. Цена в кадре — ноль: цвета пересчитываются только
// когда сменился туман или игрок уехал на полклетки (7.2 про маску, здесь
// то же правило), а в самом кадре это те же instanced-вызовы.
//
// Побочно это даёт то, ради чего затевались два бэкенда: числа 4.1
// (освещённый пол против памяти по Rec.709) считаются ТОЙ ЖЕ арифметикой,
// что и в 2D, и их можно сравнивать между бэкендами, а не на глаз.

import * as THREE from '../vendor/three.module.min.js';

import { K_PLAYER, K_ENEMY, K_ENEMY_RANGED, K_PROP, K_SHOT,
         F_DEAD, F_OFFLINE, F_WINDUP, F_DASH,
         FX_HIT, FX_DIE, FX_SHOT, FX_BOOM,
         TILE_WALL, TILE_STAIRS, VIS_DARK, VIS_LIT }
  from './state.js';

// --- камера (7.3) ---------------------------------------------------------
//
// Ортографическая под углом. Азимут НОЛЬ, то есть камера повёрнута только
// по высоте: мировой X остаётся экранным X, сетка карты остаётся
// параллельной краям кадра. Стенд bench/bench3d.html крутит ещё и азимут на
// 40°, но стенд меряет кадры, а не играется: повёрнутая сетка в бою с
// толпой читается заметно хуже (клетка перестаёт быть клеткой, «вправо» на
// экране перестаёт быть «вправо» на клавиатуре). 7.3 требует ортографии
// ради читаемости сетки — азимут 0 это требование только усиливает.
//
// Высота 57° — НЕ НА ГЛАЗ, а из трёх условий сразу, и среднее из них
// решающее.
//   * поперечное сжатие sin(57°) = 0.84: клетка остаётся почти квадратной,
//     а не лентой (при 30° было бы 0.5, то есть вдвое);
//   * ТЕЛА СОСЕДНИХ РЯДОВ НЕ СЛИПАЮТСЯ В СТОЛБИК. Ряд от ряда на экране
//     стоит 48*sin(57°) = 40.3 px. Тело занимает по вертикали свою
//     проекцию плюс подъём: 0.70*40.3 + BODY_H*48*cos(57°) = 28.2 + 9.4 =
//     37.6 px. 37.6 < 40.3 — значит между рядами остаётся просвет.
//     ЭТО ЗАМЕРЕНО НА КАДРЕ, а не выведено заранее: при 52° и высоте тела
//     0.60 получалось 44 px при шаге 37.8, и толпа читалась вертикальными
//     трубами вместо отдельных врагов;
//   * стена высотой WALL_H закрывает игрока с расстояния
//     (WALL_H - 0.05)/tan(57°) = 0.78 клетки — то есть соседняя клетка, и
//     гашение стен (последний пункт 7.3) не превращается в мёртвый код.
//     При 75° закрывать было бы нечему.
const ELEV = 57 * Math.PI / 180;
const SIN_E = Math.sin(ELEV);
const COS_E = Math.cos(ELEV);
// Камера ортографическая — расстояние на размер картинки не влияет вообще,
// оно нужно только чтобы весь этаж (128 клеток, 4.1) влез между near и far.
const CAM_DIST = 200;

const MARGIN_TILES = 10;   // запас окна инстансов вокруг экрана, в клетках
// Стена 1.25 клетки. Выше — красивее, но каждая стена крадёт из кадра
// полосу пола позади себя высотой WALL_H*48*cos(57°) = 34 px, а это чистая
// потеря обзора, которой в 2D нет вовсе (там выступ 0.45 клетки).
const WALL_H = 1.25;
const BODY_H = 0.36;       // см. вывод угла камеры выше
const ENT_Y = BODY_H / 2;  // тело стоит на полу, центр — на половине высоты

// --- бой (DESIGN.md 4.2), те же числа, что в render2d.js ------------------
const MELEE_REACH = 1.2;
const MELEE_ARC = 110 * Math.PI / 180;
const SHOT_TAIL = 0.40;
const HPBAR_W = 0.8;
const HURT_AT = 0.5;

// --- туман и факел (DESIGN.md 4.1, 4.4) ----------------------------------
//
// Числа взяты из render2d.js БЕЗ ИЗМЕНЕНИЙ и смешиваются той же
// арифметикой в том же пространстве (sRGB, 0..255). Это не лень: 4.1
// называет отношение «освещённый пол против памяти» приёмочным числом, и
// если бэкенды считают его по-разному, сравнивать их нечем.
const TORCH_RGB = [255, 206, 150];
const TORCH_A_IN = 0.18;
const TORCH_A_OUT = 0.045;
const TORCH_R_IN = 3.0;
const TORCH_R_OUT = 11.0;
const TORCH_Q = 2;                        // шаг квантования центра, 1/2 клетки
const FOG_SEEN_RGB = [8, 13, 30];
const FOG_SEEN_A = 158 / 255;             // 0.62, как в render2d.js

// --- палитра (та же, что у 2D) -------------------------------------------
const PAL = {
  floor: [35, 41, 58],          // #23293a
  floorAlt: [38, 45, 64],       // #262d40
  wall: [102, 111, 136],        // #666f88 — верх стены; бока темнее вершинно
  stairsBg: [59, 47, 20],       // #3b2f14
  stairsA: [202, 161, 74],      // #caa14a
  stairsB: [143, 111, 44],      // #8f6f2c
  stairsEdge: [240, 201, 106],  // #f0c96a
};

// --- виды (4.2, 4.3) ------------------------------------------------------
//
// Красный канал у рубаки и стрелка одинаков (209) — как и в 2D, чтобы
// «самый красный пиксель это враг» осталось правдой и здесь. Различаются
// они ФОРМОЙ: рубака — круг (цилиндр), стрелок — наконечник (треугольная
// призма остриём по facing). Сверху под углом 52° это читается с одного
// взгляда, и читается в темноте.
const KIND = {};
KIND[K_PLAYER] = { r: 0.35, rgb: [127, 209, 138], bar: 1, body: 'player' };
KIND[K_ENEMY] = { r: 0.35, rgb: [209, 106, 106], bar: 1, body: 'melee' };
KIND[K_ENEMY_RANGED] = { r: 0.35, rgb: [209, 86, 140], bar: 1, body: 'ranged' };
KIND[K_PROP] = { r: 0.30, rgb: [139, 127, 90], body: 'prop' };
KIND[K_SHOT] = { r: 0.12, rgb: [255, 226, 122], body: 'shot' };
const KIND_DEF = { r: 0.30, rgb: [139, 147, 161], body: 'unknown' };

// Затемнение лестницы в памяти группы. Выведено из 2D: золото #caa14a под
// наложением FOG_SEEN (альфа 0.62) даёт (82,69,47), то есть в линейном
// пространстве 0.145 / 0.166 / 0.387 от исходного по каналам. Синий гаснет
// слабее прочих — это и есть «уход в холод», который в 2D даёт наложение.
const STAIRS_SEEN = [0.15, 0.17, 0.39];
const LIN_ONE = [1, 1, 1];

const SELF_RGB = [255, 226, 122];
const DEAD_RGB = [159, 200, 255];
const OFFLINE_RGB = [107, 114, 128];

// --- sRGB -> линейное ------------------------------------------------------
//
// three.js держит цвета в ЛИНЕЙНОМ пространстве и переводит их в sRGB на
// выходе. Всё смешивание тумана и факела здесь делается в sRGB 0..255 —
// ровно как в render2d.js поверх канвы, — а в буфер экземпляра кладётся уже
// перевод. Таблица на 256 значений вместо Math.pow в горячем пути: цветов
// на перестройку окна тысячи.
const SRGB_LUT = new Float32Array(256);
for (let i = 0; i < 256; i++) {
  const c = i / 255;
  SRGB_LUT[i] = c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

/** Смешать цвет sRGB с цветом sRGB по альфе — как канва в render2d.js. */
function mix255(out, base, over, a) {
  const k = 1 - a;
  out[0] = base[0] * k + over[0] * a;
  out[1] = base[1] * k + over[1] * a;
  out[2] = base[2] * k + over[2] * a;
  return out;
}

// --- геометрия: свет запечён в вершинные цвета (7.3) ----------------------
//
// Ни одного источника света в сцене нет. Объём даёт вершинный цвет,
// посчитанный ОДИН РАЗ при сборке геометрии по нормали: вверх ярче, к
// камере (на +Z) чуть ярче, от камеры темнее. Это и есть «свет запечён в
// вершинные цвета» из 7.3, только запекается он в браузере за микросекунды,
// а не в редакторе.
//
// Множители линейные (в шейдере vColor умножается до перевода в sRGB),
// поэтому числа тут не читаются как «доля яркости на глаз» — на глаз это
// примерно корень из них. Проверялось глазами на кадре, а не выводилось.
function shade(geo, amb, up, front) {
  const g = geo.index ? geo.toNonIndexed() : geo;
  if (g !== geo) geo.dispose();
  const n = g.attributes.normal.array;
  const cnt = g.attributes.position.count;
  const col = new Float32Array(cnt * 3);
  for (let i = 0; i < cnt; i++) {
    const ny = n[i * 3 + 1], nz = n[i * 3 + 2];
    let k = amb + up * Math.max(0, ny) + front * Math.max(0, nz);
    if (k > 1) k = 1;
    col[i * 3] = k; col[i * 3 + 1] = k; col[i * 3 + 2] = k;
  }
  g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  return g;
}

/**
 * Тайл пола с притемнённой каймой: вершины на краю квада получают edge,
 * центральная — единицу. Сетка видна, а вызовов отрисовки не прибавляется
 * ни одного.
 */
function gridTile(geo, edge) {
  const g = geo.index ? geo.toNonIndexed() : geo;
  if (g !== geo) geo.dispose();
  const p = g.attributes.position.array;
  const n = g.attributes.position.count;
  const col = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    const x = p[i * 3], z = p[i * 3 + 2];
    const k = (Math.abs(x) > 0.49 || Math.abs(z) > 0.49) ? edge : 1;
    col[i * 3] = k; col[i * 3 + 1] = k; col[i * 3 + 2] = k;
  }
  g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  return g;
}

/** Умножить готовый вершинный цвет ещё на число (нос, кайма и т.п.). */
function tint(geo, k) {
  const c = geo.attributes.color.array;
  for (let i = 0; i < c.length; i++) c[i] = Math.min(1.6, c[i] * k);
  return geo;
}

/** Склеить несколько геометрий в одну (BufferGeometryUtils в vendor нет). */
function mergeGeos(src) {
  const list = src.map((g) => {
    if (!g.index) return g;
    const n = g.toNonIndexed();
    g.dispose();
    return n;
  });
  let total = 0;
  for (const g of list) total += g.attributes.position.count;
  const pos = new Float32Array(total * 3);
  const nor = new Float32Array(total * 3);
  const col = new Float32Array(total * 3);
  let o = 0;
  for (const g of list) {
    const p = g.attributes.position.array;
    const n = g.attributes.normal.array;
    const c = g.attributes.color.array;
    pos.set(p, o); nor.set(n, o); col.set(c, o);
    o += p.length;
    g.dispose();
  }
  const out = new THREE.BufferGeometry();
  out.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  out.setAttribute('normal', new THREE.Float32BufferAttribute(nor, 3));
  out.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  return out;
}

// --- пачка экземпляров (7.3, приём 1) -------------------------------------
//
// Один InstancedMesh + счётчик заполнения. Ёмкость растёт вдвое и никогда
// не падает: пересоздание меша стоит выделения буфера, и делать это каждый
// кадр из-за качнувшегося числа врагов было бы ровно тем, от чего instanced
// и спасает.
class Batch {
  constructor(scene, geo, mat, cap, order) {
    this.scene = scene;
    this.geo = geo;
    this.mat = mat;
    this.order = order || 0;
    this.mesh = null;
    this.cap = 0;
    this.n = 0;
    this._alloc(cap);
  }

  _alloc(cap) {
    const old = this.mesh;
    const m = new THREE.InstancedMesh(this.geo, this.mat, cap);
    m.frustumCulled = false;            // окно считаем сами, см. _ensureTiles
    m.renderOrder = this.order;
    m.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    // instanceColor заводится один раз: без него шейдер собирается без
    // USE_INSTANCING_COLOR, и первый же цветной экземпляр потребовал бы
    // пересборки программы посреди кадра.
    m.instanceColor = new THREE.InstancedBufferAttribute(
      new Float32Array(cap * 3).fill(1), 3);
    m.instanceColor.setUsage(THREE.DynamicDrawUsage);
    // Рост ёмкости случается посреди набора кадра — уже уложенные
    // экземпляры обязаны переехать, иначе «врагов стало 201» означало бы
    // потерянный кадр вместо честного кадра.
    if (old) {
      m.instanceMatrix.array.set(old.instanceMatrix.array.subarray(0, this.n * 16));
      m.instanceColor.array.set(old.instanceColor.array.subarray(0, this.n * 3));
      this.scene.remove(old);
      old.dispose();
    }
    m.count = 0;
    m.visible = false;
    this.scene.add(m);
    this.mesh = m;
    this.cap = cap;
  }

  begin() { this.n = 0; }

  /** Матрица уже посчитана снаружи; rgb — sRGB 0..255 или null. */
  push(m4, rgb) {
    if (this.n >= this.cap) this._alloc(Math.max(8, this.cap * 2));
    const i = this.n++;
    m4.toArray(this.mesh.instanceMatrix.array, i * 16);
    const a = this.mesh.instanceColor.array;
    if (rgb) {
      a[i * 3] = SRGB_LUT[rgb[0] | 0];
      a[i * 3 + 1] = SRGB_LUT[rgb[1] | 0];
      a[i * 3 + 2] = SRGB_LUT[rgb[2] | 0];
    } else {
      a[i * 3] = a[i * 3 + 1] = a[i * 3 + 2] = 1;
    }
  }

  /** То же, но цвет уже в ЛИНЕЙНОМ пространстве (для готовых палитр). */
  pushLinear(m4, lin) {
    if (this.n >= this.cap) this._alloc(Math.max(8, this.cap * 2));
    const i = this.n++;
    m4.toArray(this.mesh.instanceMatrix.array, i * 16);
    const a = this.mesh.instanceColor.array;
    a[i * 3] = lin[0]; a[i * 3 + 1] = lin[1]; a[i * 3 + 2] = lin[2];
  }

  end() {
    this.mesh.count = this.n;
    // Экземпляров нет — меш прячем. InstancedMesh с count = 0 всё равно
    // уходит в очередь отрисовки и всё равно считается в drawCalls: пустая
    // пачка стоила бы вызова ни за что.
    this.mesh.visible = this.n > 0;
    this.mesh.instanceMatrix.needsUpdate = true;
    this.mesh.instanceColor.needsUpdate = true;
  }

  dispose() {
    if (!this.mesh) return;
    this.scene.remove(this.mesh);
    this.mesh.dispose();
    this.mesh = null;
  }
}

// --- кэш текста: строка рисуется в канву один раз (как 7.2 в 2D) ---------
const texCache = new Map();
function textTexture(text, font, color) {
  const key = text + '|' + font + '|' + color;
  let rec = texCache.get(key);
  if (rec) return rec;
  const c = document.createElement('canvas');
  const mc = c.getContext('2d');
  mc.font = font;
  const w = Math.ceil(mc.measureText(text).width) + 8;
  const h = Math.ceil(parseInt(font, 10) * 1.6) + 6;
  c.width = w; c.height = h;
  const g = c.getContext('2d');
  g.font = font;
  g.textBaseline = 'middle';
  g.fillStyle = 'rgba(0,0,0,0.55)';
  g.fillText(text, 5, h / 2 + 1);
  g.fillStyle = color;
  g.fillText(text, 4, h / 2);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  rec = { tex: tex, w: w, h: h };
  texCache.set(key, rec);
  return rec;
}

export function createRenderer() {
  const m4 = new THREE.Matrix4();
  const q4 = new THREE.Quaternion();
  const vPos = new THREE.Vector3();
  const vScale = new THREE.Vector3(1, 1, 1);
  const UP = new THREE.Vector3(0, 1, 0);
  // Камера не вращается (азимут 0, высота постоянна), поэтому «лицом к
  // камере» — это один и тот же поворот на весь забег, а не работа в кадре.
  const Q_BILLBOARD = new THREE.Quaternion()
    .setFromAxisAngle(new THREE.Vector3(1, 0, 0), -ELEV);
  const rgbTmp = [0, 0, 0];

  return {
    name: 'three.js',
    canvas: null, gl: null, renderer: null,
    scene: null, camera: null, hudScene: null, hudCamera: null,
    w: 0, h: 0, tilePx: 48, quality: 'high',

    // Окно инстансов тайлов — то же понятие, что статический слой 2D (7.2):
    // перестраивается не каждый кадр, а когда камера уехала за край запаса,
    // сменился туман, сдвинулся факел или сменился набор гасимых стен.
    stTx: 0, stTy: 0, stTw: 0, stTh: 0,
    fogKey: '', fogRepaints: 0, fogOn: false,

    torch: true,
    combat: true, windup: true,

    camX: 0, camY: 0, camCX: 0, camCY: 0,
    _drawCalls: 0, _ents: 0, _ms: 0, _hidden: 0, _fx: 0, _tris: 0,

    _batches: null,
    _hud: null,
    _lights: [],
    _fade: new Set(),
    _fadeKey: '',
    _view: null,
    _lost: false,

    // --- 7.1 ---------------------------------------------------------

    init(canvas, opts) {
      opts = opts || {};
      this.canvas = canvas;
      this.tilePx = opts.tilePx || 48;
      this.quality = opts.quality || 'high';

      const r = new THREE.WebGLRenderer({
        canvas: canvas,
        antialias: this.quality === 'high',
        alpha: false,
        powerPreference: 'high-performance',
        // Буфер НЕ сохраняем: preserveDrawingBuffer заставляет драйвер
        // копировать кадр целиком каждый раз. readPixels() ниже вместо
        // этого перерисовывает сцену — вне кадра это стоит один кадр, а в
        // игре не стоит ничего.
        preserveDrawingBuffer: false,
      });
      r.setPixelRatio(1);
      // 4.1 говорит про пиксели на клетку; если буфер окажется вдвое
      // крупнее заявленного (HiDPI), любое число про кадр будет не про то
      // разрешение, которое меряют.
      r.setClearColor(0x000000, 1);
      r.shadowMap.enabled = false;        // 7.3: карты теней выключены
      r.info.autoReset = false;           // кадр = два прохода, считаем сами
      this.renderer = r;
      this.gl = r.getContext();

      canvas.addEventListener('webglcontextlost', this._onLost = (e) => {
        e.preventDefault();
        this._lost = true;
      }, false);

      this.scene = new THREE.Scene();
      this.hudScene = new THREE.Scene();
      this.camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 1, CAM_DIST * 2);
      this.hudCamera = new THREE.OrthographicCamera(0, 1, 0, -1, -10, 10);

      this._buildAssets();
      this.resize(canvas.width || 960, canvas.height || 540);
    },

    resize(w, h) {
      w = Math.max(1, Math.round(w));
      h = Math.max(1, Math.round(h));
      this.w = w; this.h = h;
      if (this.renderer) this.renderer.setSize(w, h, false);
      const tw = w / this.tilePx;
      const th = h / (this.tilePx * SIN_E);
      const c = this.camera;
      c.left = -tw / 2; c.right = tw / 2;
      c.top = th * SIN_E / 2; c.bottom = -th * SIN_E / 2;
      c.updateProjectionMatrix();
      const hc = this.hudCamera;
      hc.left = 0; hc.right = w; hc.top = 0; hc.bottom = -h;
      hc.updateProjectionMatrix();
      this.fogKey = '';
    },

    dispose() {
      if (this.canvas && this._onLost) {
        this.canvas.removeEventListener('webglcontextlost', this._onLost);
      }
      if (this._batches) {
        for (const k in this._batches) this._batches[k].dispose();
      }
      if (this._hud) {
        for (const k in this._hud) {
          const o = this._hud[k];
          if (!o) continue;
          if (o.isMesh) {
            this.hudScene.remove(o);
            if (o.isInstancedMesh) o.dispose();
            o.material.dispose();
          } else if (o.isBufferGeometry) {
            o.dispose();
          }
        }
      }
      for (const rec of texCache.values()) rec.tex.dispose();
      texCache.clear();
      // КОНТЕКСТ НЕ УБИВАЕМ, И ЭТО НЕ НЕДОСМОТР. Соблазн позвать здесь
      // WEBGL_lose_context.loseContext() («контекстов у вкладки конечное
      // число») выглядит правильным ровно до второго включения 3D: у
      // канвы контекст ОДИН НА ВСЮ ЖИЗНЬ ЭЛЕМЕНТА, убитый не
      // восстанавливается, и следующий getContext вернёт null — three.js
      // падает на нём с «Cannot read properties of null». Поймано
      // прогоном: первое переключение работало, второе роняло клиент.
      // Утечки при этом нет: канва у трёхмерного бэкенда одна и та же
      // (#c3), значит и контекст всю игру один, сколько ни переключай.
      if (this.renderer) this.renderer.dispose();
      this.renderer = null; this.gl = null;
      this.scene = null; this.hudScene = null;
      this.canvas = null; this._batches = null; this._hud = null;
      this._view = null;
    },

    stats() {
      return { drawCalls: this._drawCalls, ents: this._ents, ms: this._ms,
               hidden: this._hidden, fogRepaints: this.fogRepaints,
               fx: this._fx, tris: this._tris };
    },

    // --- камера ------------------------------------------------------
    //
    // Проекция ортографическая и азимут нулевой, поэтому переход
    // «мир <-> экран» считается арифметикой, а не unproject: мировой X
    // это экранный X в масштабе tilePx, мировой Y (он же Z сцены) — экранный
    // Y в масштабе tilePx*sin(ELEV), высота поднимает точку на
    // tilePx*cos(ELEV) за клетку. Это ровно та же формула, по которой
    // строится камера ниже, а не её приближение.

    screenToWorld(sx, sy) {
      // Прицел кладётся на плоскость ЦЕНТРОВ ТЕЛ, а не на пол: курсор,
      // наведённый на врага, обязан означать этого врага. На полу он
      // означал бы точку за ним.
      return [this.camX + sx / this.tilePx,
              this.camY + (sy + ENT_Y * this.tilePx * COS_E) / (this.tilePx * SIN_E)];
    },

    worldToScreen(wx, wy, hgt) {
      const t = this.tilePx;
      return [(wx - this.camX) * t,
              (wy - this.camY) * t * SIN_E - (hgt || 0) * t * COS_E];
    },

    _updateCamera(view) {
      const tw = this.w / this.tilePx;
      const th = this.h / (this.tilePx * SIN_E);
      const self = view.self;
      let cx, cy;
      if (self) { cx = self.dx; cy = self.dy; }
      else if (view.level) { cx = view.level.w / 2; cy = view.level.h / 2; }
      else { cx = tw / 2; cy = th / 2; }
      if (view.level) {
        const L = view.level;
        cx = L.w <= tw ? L.w / 2 : Math.max(tw / 2, Math.min(L.w - tw / 2, cx));
        cy = L.h <= th ? L.h / 2 : Math.max(th / 2, Math.min(L.h - th / 2, cy));
      }
      this.camCX = cx; this.camCY = cy;
      this.camX = cx - tw / 2;
      this.camY = cy - th / 2;
      const c = this.camera;
      c.position.set(cx, SIN_E * CAM_DIST, cy + COS_E * CAM_DIST);
      c.up.set(0, 1, 0);
      c.lookAt(cx, 0, cy);
      c.updateMatrixWorld();
    },

    // --- модели из примитивов (7.3, приём 4) --------------------------

    _buildAssets() {
      const S = this.scene;
      const hi = this.quality === 'high';
      const seg = hi ? 16 : 10;

      // Пол — плоский квадрат. Не коробка: у пола видна ровно одна грань,
      // а коробка стоила бы 12 треугольников вместо 2 на каждую клетку из
      // тысячи с лишним в окне.
      // 7.3 просит ортографию РАДИ ЧИТАЕМОСТИ СЕТКИ — значит сетку надо
      // видеть. В 2D это линия rgba(0,0,0,0.16) по краю клетки; здесь
      // рисовать линии было бы по вызову на клетку, поэтому кайма
      // ЗАПЕЧЕНА В ВЕРШИНЫ: квад с одним делением (9 вершин, 8
      // треугольников), центр в полную яркость, кольцо по краю в 0.72.
      // Цена — 8 треугольников на клетку вместо 2, то есть около 9 тысяч
      // на кадр при полном окне; при опорных 150 тысяч (11.4) это 6%.
      const floorGeo = gridTile(new THREE.PlaneGeometry(1, 1, 2, 2)
                                  .rotateX(-Math.PI / 2), 0.72);
      // Стена — коробка с ЗАПЕЧЁННЫМИ гранями: верх полный, южная (к
      // камере) заметно темнее, северная и боковины совсем тёмные. Ровно
      // это в 2D подделывалось «вертикальным выступом» (7.2), только здесь
      // выступ настоящий.
      const wallGeo = shade(new THREE.BoxGeometry(1, WALL_H, 1), 0.10, 0.90, 0.30);

      const matTile = new THREE.MeshBasicMaterial({ vertexColors: true });
      const matWallFade = new THREE.MeshBasicMaterial({
        vertexColors: true, transparent: true, opacity: 0.26,
        depthWrite: false });

      const b = {};
      b.floor = new Batch(S, floorGeo, matTile, 2048, 0);
      b.wall = new Batch(S, wallGeo, matTile, 1024, 0);
      // Гасимые стены — ОТДЕЛЬНАЯ пачка с прозрачным материалом (7.3:
      // «гасятся по прозрачности, а не режутся»). Прозрачное рисуется после
      // непрозрачного, поэтому игрок виден СКВОЗЬ такую стену, а дыры в
      // стене при этом нет: она на месте, её видно, через неё видно.
      // renderOrder 8: гасимая стена ложится ПОВЕРХ всего, включая полоски
      // и кольца. Она физически ближе к камере, чем то, что закрывает, —
      // рисовать её раньше значило бы «стена за игроком», то есть враньё.
      b.wallFade = new Batch(S, wallGeo.clone(), matWallFade, 64, 8);
      b.stairs = new Batch(S, this._stairsGeo(), matTile, 8, 0);

      // Плоские накладки на пол: тень, кольца, сектор замаха.
      const disc = shade(new THREE.CircleGeometry(1, hi ? 20 : 12)
                           .rotateX(-Math.PI / 2), 1, 0, 0);
      const ring = shade(new THREE.RingGeometry(0.82, 1.0, hi ? 24 : 14)
                           .rotateX(-Math.PI / 2), 1, 0, 0);
      const sector = shade(new THREE.CircleGeometry(1, 18, -MELEE_ARC / 2, MELEE_ARC)
                             .rotateX(-Math.PI / 2), 1, 0, 0);
      const flatMat = (op) => new THREE.MeshBasicMaterial({
        vertexColors: true, transparent: true, opacity: op,
        depthWrite: false });
      b.shadow = new Batch(S, disc, flatMat(0.40), 256, 2);
      b.sector = new Batch(S, sector, flatMat(0.30), 64, 3);
      b.ring = new Batch(S, ring, flatMat(0.85), 256, 4);
      b.spark = new Batch(S, disc.clone(), flatMat(0.9), 128, 6);

      // Тела: по InstancedMesh на вид (7.3, приём 1).
      const bodyMat = new THREE.MeshBasicMaterial({ vertexColors: true });
      const bodies = this._bodyGeos(seg);
      b.player = new Batch(S, bodies.player, bodyMat, 16, 0);
      b.melee = new Batch(S, bodies.melee, bodyMat, 256, 0);
      b.ranged = new Batch(S, bodies.ranged, bodyMat, 256, 0);
      b.prop = new Batch(S, bodies.prop, bodyMat, 64, 0);
      b.shot = new Batch(S, bodies.shot, bodyMat, 128, 0);
      b.unknown = new Batch(S, bodies.unknown, bodyMat, 32, 0);
      // Дух и отвалившийся — одна пачка: оба полупрозрачные, отличаются
      // цветом экземпляра. Двумя пачками это стоило бы лишнего вызова
      // ровно ради разницы в альфе, которой глаз не видит.
      b.ghost = new Batch(S, bodies.ghost,
                          new THREE.MeshBasicMaterial({
                            vertexColors: true, transparent: true,
                            opacity: 0.34, depthWrite: false }), 32, 5);

      // Полоски здоровья: квад, повёрнутый ЛИЦОМ К КАМЕРЕ раз и навсегда.
      // Камера не вращается (азимут 0, высота постоянна), поэтому
      // билборд — это поворот геометрии при сборке, а не работа в кадре.
      // Квад лежит в XY и НЕ повёрнут в геометрии: масштаб в compose()
      // применяется ДО поворота, и наклон, запечённый в вершины, растянулся
      // бы вместе с высотой полоски — полоска перестала бы смотреть в
      // камеру ровно тогда, когда у неё меняется длина.
      const barGeo = shade(new THREE.PlaneGeometry(1, 1).translate(0.5, 0, 0),
                           1, 0, 0);
      b.bar = new Batch(S, barGeo, flatMat(0.95), 512, 5);

      this._batches = b;
      this._buildHud();
    },

    _bodyGeos(seg) {
      // Игрок: восьмигранная тумба с носом по направлению взгляда.
      const pBody = shade(new THREE.CylinderGeometry(0.30, 0.34, BODY_H, seg),
                          0.30, 0.62, 0.16);
      const pNose = tint(shade(new THREE.BoxGeometry(0.34, 0.12, 0.14)
                                 .translate(0.40, 0.04, 0), 0.30, 0.62, 0.16), 1.7);
      // Рубака: КРУГ (4.2). Цилиндр плюс тонкая чёрточка взгляда — силуэт
      // сверху остаётся кругом, а куда он смотрит, видно.
      const mBody = shade(new THREE.CylinderGeometry(0.33, 0.35, BODY_H, seg),
                          0.30, 0.62, 0.16);
      const mNose = tint(shade(new THREE.BoxGeometry(0.26, 0.09, 0.09)
                                 .translate(0.40, 0.06, 0), 0.30, 0.62, 0.16), 1.7);
      // Стрелок: НАКОНЕЧНИК (4.2). Треугольная призма остриём по facing;
      // площадь силуэта против круга рубаки отличается в 2.4 раза, то есть
      // различие даёт форма, а не только цвет.
      const rBody = shade(new THREE.CylinderGeometry(0.44, 0.44, BODY_H, 3)
                            .rotateY(-Math.PI / 2).scale(1.15, 1, 0.85),
                          0.30, 0.62, 0.16);
      return {
        player: mergeGeos([pBody, pNose]),
        melee: mergeGeos([mBody, mNose]),
        ranged: rBody,
        prop: shade(new THREE.BoxGeometry(0.46, 0.34, 0.46), 0.30, 0.62, 0.16),
        // Снаряд — вытянутая по X коробка: хвост 4.2 (12 кл/с) рисуется
        // масштабом экземпляра вдоль скорости, а не отдельной геометрией.
        shot: shade(new THREE.BoxGeometry(1, 0.16, 0.16), 0.55, 0.35, 0.15),
        unknown: shade(new THREE.CylinderGeometry(0.28, 0.30, BODY_H, 8),
                       0.30, 0.62, 0.16),
        ghost: shade(new THREE.CylinderGeometry(0.30, 0.34, BODY_H, seg),
                     0.50, 0.40, 0.16),
      };
    },

    _stairsGeo() {
      // Лестница вниз (4.1, тайл 2). Тёплое золото против холодного пола,
      // ступени поперёк, яркая рамка — те же три приметы, что в 2D, только
      // собранные из плоских квадов. Цвета абсолютные: цвет экземпляра у
      // этой пачки только гасит их туманом.
      const parts = [];
      const quad = (x0, z0, x1, z1, y, rgb, k) => {
        const g = new THREE.PlaneGeometry(x1 - x0, z1 - z0)
          .rotateX(-Math.PI / 2)
          .translate((x0 + x1) / 2 - 0.5, y, (z0 + z1) / 2 - 0.5);
        const n = g.attributes.position.count;
        const col = new Float32Array(n * 3);
        for (let i = 0; i < n; i++) {
          col[i * 3] = SRGB_LUT[rgb[0]] * k;
          col[i * 3 + 1] = SRGB_LUT[rgb[1]] * k;
          col[i * 3 + 2] = SRGB_LUT[rgb[2]] * k;

        }
        g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
        return g;
      };
      // (mergeGeos снимет индекс сам)
      parts.push(quad(0, 0, 1, 1, 0.004, PAL.stairsBg, 1));
      for (let i = 0; i < 4; i++) {
        const ins = 0.09 + 0.07 * i;
        parts.push(quad(ins, 0.13 + 0.20 * i, 1 - ins, 0.25 + 0.20 * i,
                        0.010 + i * 0.001,
                        (i & 1) ? PAL.stairsB : PAL.stairsA, 1));
      }
      const e = 0.05;
      parts.push(quad(0, 0, 1, e, 0.016, PAL.stairsEdge, 1));
      parts.push(quad(0, 1 - e, 1, 1, 0.016, PAL.stairsEdge, 1));
      parts.push(quad(0, e, e, 1 - e, 0.016, PAL.stairsEdge, 1));
      parts.push(quad(1 - e, e, 1, 1 - e, 0.016, PAL.stairsEdge, 1));
      // mergeGeos ждёт нормали — у PlaneGeometry они есть.
      return mergeGeos(parts);
    },

    _buildHud() {
      // Интерфейс живёт во ВТОРОЙ сцене с пиксельной ортокамерой и
      // рисуется вторым проходом поверх мира. Так «своё здоровье» не
      // зависит ни от угла камеры, ни от тумана — это интерфейс, а не мир.
      const quad = new THREE.PlaneGeometry(1, 1).translate(0.5, -0.5, 0);
      // transparent ВСЕГДА, даже при opacity 1. Непрозрачные и прозрачные
      // объекты у three.js это две разные очереди, и непрозрачная идёт
      // первой независимо от renderOrder: подложка полоски здоровья
      // нарисовалась бы ПОВЕРХ самой полоски.
      const flat = (col, op) => new THREE.MeshBasicMaterial({
        color: col, transparent: true, opacity: op,
        depthTest: false, depthWrite: false });
      const mk = (mat, order) => {
        const m = new THREE.Mesh(quad, mat);
        m.renderOrder = order;
        m.visible = false;
        this.hudScene.add(m);
        return m;
      };
      const h = {};
      h.quad = quad;
      h.back = mk(flat(0x000000, 0.62), 1);
      h.bad = mk(flat(0xd16a6a, 1), 2);
      h.good = mk(flat(0x7fd18a, 1), 3);
      h.edge = new THREE.InstancedMesh(quad, flat(0xbe281e, 0.55), 4);
      h.edge.renderOrder = 4;
      h.edge.frustumCulled = false;
      h.edge.count = 0;
      h.edge.visible = false;
      this.hudScene.add(h.edge);
      h.tint = mk(flat(0x2a5fa8, 0.16), 0);
      h.text = mk(new THREE.MeshBasicMaterial({
        transparent: true, depthTest: false, depthWrite: false }), 5);
      h.big = mk(new THREE.MeshBasicMaterial({
        transparent: true, depthTest: false, depthWrite: false }), 5);
      h.sub = mk(new THREE.MeshBasicMaterial({
        transparent: true, depthTest: false, depthWrite: false }), 5);
      this._hud = h;
    },

    // --- туман: три состояния, запечённые в цвет экземпляра ------------
    //
    // Неразведанная клетка НЕ РИСУЕТСЯ ВОВСЕ — ни пола, ни стены. Фон
    // сцены чёрный ровно нулём, поэтому чернота выходит абсолютной, а не
    // «почти чёрной»: порог черноты у чужих проверок — 10 по максимальному
    // каналу, и фон 0x05070c (как в 2D) дал бы 12, то есть не чёрное.
    // В 2D это же место решается маской, которая заливает клетку в ноль;
    // здесь заливать нечего — там просто ничего нет.

    _torchLights(view) {
      const out = this._lights;
      out.length = 0;
      if (!this.torch) return out;
      const ents = view.ents;
      if (!ents) return out;
      for (let i = 0; i < ents.length; i++) {
        const e = ents[i];
        // 4.4: дух не несёт огня. Свет считается по ЖИВЫМ игрокам.
        if (e.kind !== K_PLAYER || (e.flags & F_DEAD)) continue;
        out.push(Math.round(e.dx * TORCH_Q) / TORCH_Q,
                 Math.round(e.dy * TORCH_Q) / TORCH_Q);
      }
      return out;
    },

    /** Альфа факела в точке — та же форма, что в render2d.js (4.1). */
    _torchA(lights, wx, wy) {
      const n = lights.length;
      if (!n) return 0;
      let best = 1e9;
      for (let i = 0; i < n; i += 2) {
        const dx = lights[i] - wx, dy = lights[i + 1] - wy;
        const q = dx * dx + dy * dy;
        if (q < best) best = q;
      }
      const d = Math.sqrt(best);
      if (d <= TORCH_R_IN) return TORCH_A_IN;
      if (d >= TORCH_R_OUT) return TORCH_A_OUT;
      return TORCH_A_IN - (d - TORCH_R_IN) *
             (TORCH_A_IN - TORCH_A_OUT) / (TORCH_R_OUT - TORCH_R_IN);
    },

    /** Видна ли клетка прямо сейчас (5.2: только VIS_LIT). */
    _lit(view, wx, wy) {
      const L = view.level, f = view.fog;
      const tx = Math.floor(wx), ty = Math.floor(wy);
      if (tx < 0 || ty < 0 || tx >= L.w || ty >= L.h) return false;
      return f[ty * L.w + tx] === VIS_LIT;
    },

    // --- 7.3, последний пункт: стены гасятся, а не режутся -------------
    //
    // Камера ортографическая и смотрит с постоянного направления, поэтому
    // «что закрывает игрока» — это не поиск по сцене, а короткий шаг вдоль
    // ОДНОГО И ТОГО ЖЕ вектора: от ног игрока к камере, пока луч не
    // поднялся выше стены. Высота стены 1.5 и угол 52° дают горизонтальный
    // пробег 1.13 клетки — то есть проверять надо соседний ряд и не более.
    _findFade(view) {
      const out = this._fade;
      out.clear();
      const L = view.level, self = view.self;
      if (!L || !self) return '';
      const tiles = L.tiles;
      const tMax = (WALL_H - 0.05) / SIN_E;
      for (let s = -1; s <= 1; s++) {
        const ox = s * 0.32;
        for (let t = 0.12; t <= tMax; t += 0.18) {
          const px = self.dx + ox;
          const pz = self.dy + t * COS_E;
          const tx = Math.floor(px), ty = Math.floor(pz);
          if (tx < 0 || ty < 0 || tx >= L.w || ty >= L.h) break;
          if (tiles[ty * L.w + tx] === TILE_WALL) out.add(ty * L.w + tx);
        }
      }
      let key = '';
      for (const v of out) key += v + ',';
      return key;
    },

    // --- 7.3, приём 1: окно тайлов одним instanced-вызовом на тип ------
    //
    // Перестраивается по ключу, а не каждый кадр — ровно та же экономия,
    // что у статического слоя и маски тумана в 7.2. Ключ меняют: уход
    // камеры за запас окна, новая дельта тумана, сдвиг факела на полклетки
    // и смена набора гасимых стен. Между перестройками кадр не трогает эти
    // буферы вообще.
    _ensureTiles(view) {
      const L = view.level;
      const b = this._batches;
      if (!L) {
        b.floor.begin(); b.floor.end();
        b.wall.begin(); b.wall.end();
        b.wallFade.begin(); b.wallFade.end();
        b.stairs.begin(); b.stairs.end();
        this.fogOn = false;
        return;
      }
      const tw = Math.ceil(this.w / this.tilePx) + MARGIN_TILES * 2;
      const th = Math.ceil(this.h / (this.tilePx * SIN_E)) + MARGIN_TILES * 2;
      let tx = Math.floor(this.camX) - MARGIN_TILES;
      let ty = Math.floor(this.camY) - MARGIN_TILES;
      tx = Math.max(-MARGIN_TILES, Math.min(L.w + MARGIN_TILES - tw, tx));
      ty = Math.max(-MARGIN_TILES, Math.min(L.h + MARGIN_TILES - th, ty));

      const arr = view.fog;
      this.fogOn = !!arr;
      const lights = this._torchLights(view);
      const fadeKey = this._findFade(view);
      const key = L.floor + ':' + L.seed + '|' + tx + ',' + ty + ',' + tw + ',' + th +
                  '|' + (arr ? view.fogVersion : -1) + '|' + lights.join(',') +
                  '|' + fadeKey;
      if (key === this.fogKey) return;
      this.fogKey = key;
      this.stTx = tx; this.stTy = ty; this.stTw = tw; this.stTh = th;
      this.fogRepaints++;
      this._paintTiles(view, lights);
    },

    _paintTiles(view, lights) {
      const L = view.level, arr = view.fog, tiles = L.tiles;
      const b = this._batches;
      const fade = this._fade;
      b.floor.begin(); b.wall.begin(); b.wallFade.begin(); b.stairs.begin();
      const x0 = this.stTx, y0 = this.stTy;
      const x1 = x0 + this.stTw, y1 = y0 + this.stTh;
      const sr = FOG_SEEN_RGB, sa = FOG_SEEN_A;
      for (let y = Math.max(0, y0); y < Math.min(L.h, y1); y++) {
        for (let x = Math.max(0, x0); x < Math.min(L.w, x1); x++) {
          const idx = y * L.w + x;
          const vis = arr ? arr[idx] : VIS_LIT;
          // ТРИ СОСТОЯНИЯ. Неразведанное не рисуется вовсе — ни пола, ни
          // стены, ни выступа: под ним абсолютная чернота фона.
          if (vis === VIS_DARK) continue;
          const t = tiles[idx];
          const base = t === TILE_WALL
            ? PAL.wall
            : (((x + y) & 1) ? PAL.floorAlt : PAL.floor);
          let rgb;
          if (vis === VIS_LIT) {
            // Освещено — полный свет плюс тёплая добавка факела по
            // расстоянию до ближайшего живого игрока (4.1).
            const a = this._torchA(lights, x + 0.5, y + 0.5);
            rgb = a > 0 ? mix255(rgbTmp, base, TORCH_RGB, a) : base;
          } else {
            // Память группы — холодная и приглушённая, но НЕ чёрная:
            // стены надо помнить (4.4).
            rgb = mix255(rgbTmp, base, sr, sa);
          }
          if (t === TILE_WALL) {
            m4.makeTranslation(x + 0.5, WALL_H / 2, y + 0.5);
            (fade.has(idx) ? b.wallFade : b.wall).push(m4, rgb);
          } else if (t === TILE_STAIRS) {
            // У лестницы четыре своих цвета в вершинах (золото, тень,
            // кайма), и одним цветом экземпляра их не выразить. Поэтому
            // цвет экземпляра здесь — ЗАТЕМНЕНИЕ, а не цвет: в памяти
            // группы лестница гаснет, но остаётся золотой.
            m4.makeTranslation(x + 0.5, 0, y + 0.5);
            b.stairs.pushLinear(m4, vis === VIS_LIT ? LIN_ONE : STAIRS_SEEN);
            // Пол под лестницей не нужен: её геометрия закрывает клетку.
          } else {
            m4.makeTranslation(x + 0.5, 0, y + 0.5);
            b.floor.push(m4, rgb);
          }
        }
      }
      b.floor.end(); b.wall.end(); b.wallFade.end(); b.stairs.end();
    },

    // --- сущности ------------------------------------------------------

    _pushBody(e, k, isSelf) {
      const b = this._batches;
      const dead = (e.flags & F_DEAD) !== 0;
      const offline = (e.flags & F_OFFLINE) !== 0;

      if (e.kind === K_SHOT) {
        // Снаряд: хвост длиной SHOT_TAIL по направлению скорости — та же
        // причина, что в 2D, точка радиусом 0.12 на 12 кл/с иначе читается
        // как мерцание, а не как летящий предмет.
        let dx = e.vx, dy = e.vy;
        const d = Math.hypot(dx, dy);
        const ang = d > 1e-6 ? Math.atan2(dy, dx) : e.dfacing;
        q4.setFromAxisAngle(UP, -ang);
        vPos.set(e.dx, ENT_Y, e.dy);
        vScale.set(SHOT_TAIL + 0.24, 1, 1);
        m4.compose(vPos, q4, vScale);
        b.shot.push(m4, k.rgb);
        vScale.set(1, 1, 1);
        return;
      }

      // Тень — плоский круг под телом, чуть в сторону камеры, иначе тело
      // закрывает её целиком и объём не читается.
      if (!dead) {
        vPos.set(e.dx, 0.014, e.dy + k.r * 0.22);
        q4.identity();
        vScale.set(k.r * 1.20, 1, k.r * 1.20);
        m4.compose(vPos, q4, vScale);
        b.shadow.push(m4, [0, 0, 0]);
        vScale.set(1, 1, 1);
      }

      q4.setFromAxisAngle(UP, -e.dfacing);
      vPos.set(e.dx, ENT_Y, e.dy);
      m4.compose(vPos, q4, vScale);

      if (dead || offline) {
        // 4.3: бит 1 — дух, бит 2 — игрок отвалился. Оба полупрозрачные и
        // холодные; напарник обязан ГАСНУТЬ, а не стоять столбом.
        b.ghost.push(m4, dead ? DEAD_RGB : OFFLINE_RGB);
      } else {
        const bat = b[k.body] || b.unknown;
        bat.push(m4, isSelf ? SELF_RGB : k.rgb);
      }

      if (isSelf && !dead) {
        vPos.set(e.dx, 0.020, e.dy);
        q4.identity();
        vScale.set(k.r * 1.45, 1, k.r * 1.45);
        m4.compose(vPos, q4, vScale);
        b.ring.push(m4, [255, 246, 207]);
        vScale.set(1, 1, 1);
      }

      // --- бой: замах и рывок живут в flags (4.3) ---------------------
      if (this.combat && !dead && this.windup && (e.flags & F_WINDUP)) {
        // Сектор рисуется ТЕМИ ЖЕ числами, какими сервер считает удар
        // (дальность 1.2, дуга 110°): нарисованная дуга — обещание, куда
        // придёт удар, а не намёк.
        q4.setFromAxisAngle(UP, -e.dfacing);
        vPos.set(e.dx, 0.026, e.dy);
        vScale.set(MELEE_REACH, 1, MELEE_REACH);
        m4.compose(vPos, q4, vScale);
        b.sector.push(m4, [255, 186, 54]);
        vScale.set(1, 1, 1);
        q4.identity();
        vPos.set(e.dx, 0.030, e.dy);
        vScale.set(k.r * 1.20, 1, k.r * 1.20);
        m4.compose(vPos, q4, vScale);
        b.ring.push(m4, [255, 214, 107]);
        vScale.set(1, 1, 1);
      }
      if (this.combat && !dead && (e.flags & F_DASH)) {
        // Рывок — ХОЛОДНЫЙ: он про неуязвимость, а не про урон, и путать
        // его с замахом нельзя ни в каком кадре.
        q4.identity();
        vPos.set(e.dx, 0.034, e.dy);
        vScale.set(k.r * 1.55, 1, k.r * 1.55);
        m4.compose(vPos, q4, vScale);
        b.ring.push(m4, [143, 230, 255]);
        let dx = e.vx, dy = e.vy;
        const d = Math.hypot(dx, dy);
        if (d > 1e-6) {
          dx /= d; dy /= d;
          for (let i = 1; i <= 3; i++) {
            const kk = i * 0.34;
            vPos.set(e.dx - dx * kk, 0.018, e.dy - dy * kk);
            vScale.set(k.r * (1 - i * 0.18), 1, k.r * (1 - i * 0.18));
            m4.compose(vPos, q4, vScale);
            b.spark.push(m4, [143, 230, 255]);
          }
        }
        vScale.set(1, 1, 1);
      }

      // Полоска здоровья над головой — ради неё hp_max и живёт в снапшоте
      // (4.3). Берётся ТОЛЬКО из снапшота: событие hit может потеряться, и
      // полоска, собранная из событий, соврала бы ровно тогда, когда сеть
      // просела.
      if (this.combat && !dead && k.bar && e.hpMax > 0 && e.hp < e.hpMax) {
        const f = Math.max(0, Math.min(1, e.hp / e.hpMax));
        const by = ENT_Y + BODY_H * 0.5 + 0.17;
        q4.copy(Q_BILLBOARD);
        vPos.set(e.dx - HPBAR_W / 2 - 0.02, by, e.dy);
        vScale.set(HPBAR_W + 0.04, 0.16, 1);
        m4.compose(vPos, q4, vScale);
        b.bar.push(m4, [0, 0, 0]);
        vPos.set(e.dx - HPBAR_W / 2, by, e.dy);
        vScale.set(HPBAR_W, 0.12, 1);
        m4.compose(vPos, q4, vScale);
        b.bar.push(m4, [209, 106, 106]);
        if (f > 0) {
          vScale.set(HPBAR_W * f, 0.12, 1);
          m4.compose(vPos, q4, vScale);
          b.bar.push(m4, f > HURT_AT ? [127, 209, 138] : [224, 160, 74]);
        }
        vScale.set(1, 1, 1);
      }
    },

    // --- вспышки событий (5.2) ----------------------------------------
    _pushFx(view, now) {
      const fx = view.fx;
      this._fx = 0;
      if (!fx || !this.combat) return;
      const b = this._batches;
      const life = view.fxLife;
      const fogOn = !!(view.fog && view.level);
      const me = view.selfId;
      for (let i = 0; i < fx.length; i++) {
        const f = fx[i];
        if (f.k === 0) continue;
        const age = now - f.t0;
        if (age < 0 || age > life) continue;
        // 5.2: то, что случилось в темноте, показывать нельзя — вспышка
        // выдала бы позицию врага не хуже его тела.
        if (fogOn && f.a !== me && f.b !== me && !this._lit(view, f.x, f.y)) continue;
        const p = age / life;
        const fade = 1 - p;
        this._fx++;
        q4.identity();
        if (f.k === FX_HIT) {
          const grow = 0.20 + 0.50 * p + Math.min(1, f.dmg / 40) * 0.25;
          vPos.set(f.x, 0.040, f.y);
          vScale.set(grow, 1, grow);
          m4.compose(vPos, q4, vScale);
          b.ring.push(m4, [255, 122, 74]);
          vPos.set(f.x, 0.044, f.y);
          vScale.set(0.16 * fade, 1, 0.16 * fade);
          m4.compose(vPos, q4, vScale);
          b.spark.push(m4, [255, 217, 192]);
        } else if (f.k === FX_DIE) {
          const grow = 0.30 + 1.10 * p;
          vPos.set(f.x, 0.042, f.y);
          vScale.set(grow, 1, grow);
          m4.compose(vPos, q4, vScale);
          b.ring.push(m4, [220, 233, 255]);
        } else if (f.k === FX_SHOT) {
          vPos.set(f.x, 0.040, f.y);
          vScale.set(0.22 * fade, 1, 0.22 * fade);
          m4.compose(vPos, q4, vScale);
          b.spark.push(m4, [255, 226, 122]);
        } else {
          vPos.set(f.x, 0.040, f.y);
          const g = (0.10 + 0.16 * p) * fade;
          vScale.set(g, 1, g);
          m4.compose(vPos, q4, vScale);
          b.spark.push(m4, [255, 176, 102]);
        }
      }
      vScale.set(1, 1, 1);
    },

    // --- свой интерфейс: здоровье и «ты дух» ---------------------------

    hpBarRect() {
      const w = Math.min(420, this.w * 0.28);
      return { x: (this.w - w) / 2, y: this.h - 20 - 26, w: w, h: 20 };
    },

    _setQuad(mesh, x, y, w, h) {
      mesh.position.set(x, -y, 0);
      mesh.scale.set(w, h, 1);
      mesh.visible = true;
    },

    _setText(mesh, rec, x, y) {
      if (mesh.material.map !== rec.tex) {
        mesh.material.map = rec.tex;
        mesh.material.needsUpdate = true;
      }
      this._setQuad(mesh, x, y, rec.w, rec.h);
    },

    _buildHudFrame(view) {
      const h = this._hud;
      for (const k in h) { if (h[k] && h[k].isMesh) h[k].visible = false; }
      h.edge.visible = false;
      h.edge.count = 0;
      if (!this.combat) return;
      const me = view.self;
      if (!me) return;
      const dead = (me.flags & F_DEAD) !== 0;

      if (!dead && me.hpMax > 0) {
        const f = Math.max(0, Math.min(1, me.hp / me.hpMax));
        const b = this.hpBarRect();
        this._setQuad(h.back, b.x - 3, b.y - 3, b.w + 6, b.h + 6);
        this._setQuad(h.bad, b.x, b.y, b.w, b.h);
        if (f > 0) {
          h.good.material.color.setHex(f > HURT_AT ? 0x7fd18a : 0xe0a04a);
          this._setQuad(h.good, b.x, b.y, b.w * f, b.h);
        }
        const rec = textTexture(me.hp + ' / ' + me.hpMax,
                                '14px ui-monospace, Consolas, monospace', '#0a0d12');
        this._setText(h.text, rec, b.x + b.w / 2 - rec.w / 2,
                      b.y + b.h / 2 - rec.h / 2);
        if (f <= HURT_AT) {
          // Кайма по краю кадра, а не заливка всего экрана: та же
          // арифметика, что в 2D — четыре полосы по 40 px против двух
          // миллионов пикселей полного кадра.
          const t = 40, a = (HURT_AT - f) / HURT_AT;
          h.edge.material.opacity = 0.25 + 0.55 * a;
          const put = (i, x, y, w, hh) => {
            m4.makeScale(w, hh, 1);
            m4.setPosition(x, -y, 0);
            m4.toArray(h.edge.instanceMatrix.array, i * 16);
          };
          put(0, 0, 0, this.w, t);
          put(1, 0, this.h - t, this.w, t);
          put(2, 0, t, t, this.h - t * 2);
          put(3, this.w - t, t, t, this.h - t * 2);
          h.edge.count = 4;
          h.edge.visible = true;
          h.edge.instanceMatrix.needsUpdate = true;
        }
        return;
      }

      if (dead) {
        // 8.5: смерть это другой режим игры, и так и написано словами —
        // иначе игрок пять секунд не понимает, почему проходит сквозь стены.
        this._setQuad(h.tint, 0, 0, this.w, this.h);
        const a = textTexture('ТЫ ДУХ', '44px system-ui, sans-serif', '#cfe4ff');
        const s = textTexture('летишь сквозь стены · воскреснешь на следующем этаже',
                              '18px system-ui, sans-serif', '#9fc8ff');
        this._setText(h.big, a, (this.w - a.w) / 2, this.h * 0.5 - a.h);
        this._setText(h.sub, s, (this.w - s.w) / 2, this.h * 0.5 + 6);
      }
    },

    // --- кадр ----------------------------------------------------------

    draw(view, alpha) {
      const t0 = performance.now();
      if (!this.renderer || this._lost) return;
      this._view = view;
      const b = this._batches;

      this._updateCamera(view);
      this._ensureTiles(view);

      b.shadow.begin(); b.sector.begin(); b.ring.begin(); b.spark.begin();
      b.bar.begin(); b.ghost.begin();
      b.player.begin(); b.melee.begin(); b.ranged.begin();
      b.prop.begin(); b.shot.begin(); b.unknown.begin();

      // ЗДЕСЬ ТУМАН РЕЖЕТ СУЩНОСТЕЙ. Сервер шлёт всех без разбора (4.4:
      // снапшот один на комнату), прятать невидимое обязан клиент — иначе
      // враг в неосвещённой комнате виден сквозь темноту, а это прямая
      // выдача того, чего группа не видит (5.2). Два исключения ровно те
      // же, что в 2D: своя сущность и МЁРТВЫЙ ИГРОК (дух туман не светит,
      // и союзник, умерший в неразведанном коридоре, иначе пропал бы для
      // группы насовсем).
      const ents = view.ents;
      const fogOn = !!(view.fog && view.level);
      const tw = this.w / this.tilePx, th = this.h / (this.tilePx * SIN_E);
      let hidden = 0;
      for (let i = 0; i < ents.length; i++) {
        const e = ents[i];
        if (e.dx < this.camX - 2 || e.dx > this.camX + tw + 2 ||
            e.dy < this.camY - 3 || e.dy > this.camY + th + 3) continue;
        const ghost = e.kind === K_PLAYER && (e.flags & F_DEAD) !== 0;
        if (fogOn && e.id !== view.selfId && !ghost && !this._lit(view, e.dx, e.dy)) {
          hidden++;
          continue;
        }
        this._pushBody(e, KIND[e.kind] || KIND_DEF, e.id === view.selfId);
      }
      this._ents = ents.length;
      this._hidden = hidden;

      this._pushFx(view, t0);

      b.shadow.end(); b.sector.end(); b.ring.end(); b.spark.end();
      b.bar.end(); b.ghost.end();
      b.player.end(); b.melee.end(); b.ranged.end();
      b.prop.end(); b.shot.end(); b.unknown.end();

      this._buildHudFrame(view);

      const r = this.renderer;
      r.info.reset();
      r.autoClear = true;
      r.render(this.scene, this.camera);
      r.autoClear = false;
      r.render(this.hudScene, this.hudCamera);
      r.autoClear = true;
      this._drawCalls = r.info.render.calls;
      this._tris = r.info.render.triangles;

      this._ms = performance.now() - t0;
    },

    // --- крючки проверок ------------------------------------------------
    //
    // Канва у WebGL не отдаёт getImageData: контекст у канвы один, и он
    // здесь трёхмерный. Поэтому пиксели кадра читает сам бэкенд, а ui.js
    // спрашивает их у того бэкенда, который сейчас работает.

    /**
     * Пиксели кадра в координатах КАНВЫ (начало сверху слева), RGBA.
     * Сцена перерисовывается перед чтением: буфер рисования между задачами
     * браузера не сохраняется (preserveDrawingBuffer выключен намеренно —
     * он стоит копии кадра каждый кадр, а нужен раз в проверку).
     */
    readPixels(x, y, w, h) {
      const out = new Uint8ClampedArray(w * h * 4);
      if (!this.gl || !this._view) return out;
      this.draw(this._view, this._view.alpha);
      const gl = this.gl;
      const buf = new Uint8Array(w * h * 4);
      const gy = this.h - (y + h);
      gl.readPixels(x, gy, w, h, gl.RGBA, gl.UNSIGNED_BYTE, buf);
      // GL отдаёт строки снизу вверх — переворачиваем, чтобы вызывающий
      // работал с теми же координатами, что и getImageData.
      const row = w * 4;
      for (let r = 0; r < h; r++) {
        out.set(buf.subarray((h - 1 - r) * row, (h - r) * row), r * row);
      }
      return out;
    },

    /**
     * Заставить драйвер дорисовать накопленное. Точный аналог чтения
     * одного пикселя канвы в benchDrawFlush (7.2): без него замер меряет
     * подачу команд, а не закраску.
     */
    flush() {
      if (!this.gl) return 0;
      const b = new Uint8Array(4);
      this.gl.readPixels(0, 0, 1, 1, this.gl.RGBA, this.gl.UNSIGNED_BYTE, b);
      return b[0];
    },
  };
}

export default createRenderer;
