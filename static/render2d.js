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

import { K_PLAYER, K_ENEMY, K_ENEMY_RANGED, K_BOSS, K_PROP, K_SHOT,
         F_DEAD, F_OFFLINE,
         F_WINDUP, F_DASH, FX_HIT, FX_DIE, FX_SHOT, FX_BOOM,
         TILE_WALL, TILE_STAIRS, VIS_DARK, VIS_LIT }
  from './state.js';

const MARGIN_TILES = 10;     // запас кэша карты вокруг экрана, в клетках
const WALL_RISE = 0.45;      // высота фальшивого выступа стены, в клетках

// --- бой (DESIGN.md 4.2) --------------------------------------------------
// Дуга удара рисуется ТЕМИ ЖЕ числами, какими сервер её считает: дальность
// 1.2 клетки от центра, полная ширина 110 градусов. Расходиться им нельзя —
// нарисованная дуга это обещание игроку, куда придёт удар.
const MELEE_REACH = 1.2;
const MELEE_ARC = 110 * Math.PI / 180;
// Хвост снаряда: 12 кл/с (4.2) за 1.5 кадра при 30 Гц — 0.4 клетки. Это не
// украшение: точка радиусом 0.12 клетки на скорости 12 кл/с иначе читается
// как мерцание, а не как летящий предмет.
const SHOT_TAIL = 0.40;
// Полоска здоровья над головой. 0.8 клетки в ширину — уже головы (0.7) на
// столько, чтобы край полоски не сливался с телом.
const HPBAR_W = 0.8;

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

// --- СВЕТ ФАКЕЛА (DESIGN.md 4.1) -----------------------------------------
//
// ПОЧЕМУ СВЕТ ЖИВЁТ В МАСКЕ ТУМАНА, А НЕ В ВИНЬЕТКЕ. Виньетка блитится
// ПОСЛЕ маски и про туман не знает ничего: тёплое пятно в ней осветлило бы
// заодно и неразведанную черноту, а она обязана оставаться ровным нулём
// (5.2; tests/client_fog.py меряет это порогом 10 по максимальному каналу и
// требует в коробке 17x17 ровно 100% чёрных пикселей). Маска, наоборот,
// знает про каждую клетку, светится она сейчас или только помнится, —
// поэтому свет ставится ровно туда: VIS_LIT получает тёплую подсветку,
// VIS_SEEN и VIS_DARK не получают ничего. Цена в кадре — НОЛЬ: это тот же
// один блит маски (7.2), в нём просто другие пиксели.
//
// Форма: плато радиусом TORCH_R_IN у ног и спад до TORCH_A_OUT к
// TORCH_R_OUT. Числа привязаны к обзору (4.1: радиус 10 клеток):
//   * плато 3 клетки — комната под ногами освещена ровно, без пятна-нашлёпки;
//   * спад кончается на 11 клетках, то есть на клетку дальше обзора: у
//     кромки света альфа ещё не ноль, и «видно сейчас» не сливается с
//     «помню» (иначе туман перестаёт читаться ровно там, где он и нужен);
//   * TORCH_A_OUT остаётся и ЗА пределом: туман общий на группу (4.4), и
//     клетки, которые светит напарник на том конце этажа, обязаны быть
//     светлее памяти, а не темнее.
//
// ПОТОЛОК ЯРКОСТИ ЗАДАН НЕ ВКУСОМ. tests/client_combat.py считает «ярким»
// пиксель с максимальным каналом выше 90 и ловит замах по ПРИБАВКЕ таких
// пикселей в коробке вокруг бойца. Освещённый пол обязан остаться НИЖЕ 90,
// иначе прибавке неоткуда взяться и проверка замаха краснеет на исправном
// клиенте. Самый светлый пол — #262d40, максимальный канал 64; при
// TORCH_A_IN = 0.18 и TORCH_RGB он становится
//   r = 38*0.82 + 255*0.18 = 77.1,  b = 64*0.82 + 150*0.18 = 79.5,
// то есть 79.5 при потолке 90 — запас 1.13 раза. Это и есть предел, до
// которого свет в этом клиенте можно поднимать вообще; дальше — только
// вместе с правкой палитры пола, и это вынесено в отчёт.
const TORCH_RGB = [255, 206, 150];   // тёплый белый, не оранжевый фильтр
const TORCH_A_IN = 0.18;
const TORCH_A_OUT = 0.045;
const TORCH_R_IN = 3.0;
const TORCH_R_OUT = 11.0;
// Шаг квантования центра света, в клетках. Маска перерисовывается только по
// дельте (7.2), а свет обязан ехать за игроком — значит перерисовка теперь
// случается ещё и когда игрок уехал на полклетки. ВЫВОД ЧИСЛА: при беге
// 5 кл/с (4.2) это 10 перерисовок в секунду на игрока сверх пяти на дельте
// vis; при замеренных 0.2-0.5 мс на перерисовку — доли процента секунды.
// Полклетки не видно: на спаде света альфа меняется на
// (0.18-0.045)/8 = 0.017 на клетку, то есть 0.008 на шаг — меньше одного
// уровня из 255. И главное: у СТОЯЩЕГО клиента кадр от этого остаётся
// детерминированным до пикселя, а на этом стоят две чужие проверки.
const TORCH_Q = 2;

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

  // --- бой ---------------------------------------------------------------
  // Замах — тёплый и ОЧЕНЬ светлый: он обязан читаться в темноте подземелья
  // за долю секунды. Заливка сектора даёт площадь (её видно краем глаза),
  // кромка даёт точную границу (по ней видно, попадёшь ли).
  windupFill: 'rgba(255,186,54,0.30)',
  windupEdge: '#ffd86b',
  windupRing: 'rgba(255,214,107,0.55)',
  // Рывок — холодный: он про неуязвимость, а не про урон, и путать его с
  // замахом нельзя ни в каком кадре.
  dashRing: '#8fe6ff',
  dashTrail: 'rgba(143,230,255,0.20)',
  // Вспышки событий (5.2). Держатся 0.3 с и состояния не несут.
  fxHit: '#ff7a4a',
  fxHitCore: '#ffd9c0',
  fxDie: '#dce9ff',
  fxShot: '#ffe27a',
  fxBoom: '#ffb066',
  hpGood: '#7fd18a',
  hpLow: '#e0a04a',
  hpBad: '#d16a6a',
  hpBack: 'rgba(0,0,0,0.62)',
  hurtEdge: 'rgba(190,40,30,0.55)',
};

// --- пинги на карте (8.8) -------------------------------------------------
//
// ПОЧЕМУ КРАСНЫЙ КАНАЛ ЗДЕСЬ ВЕЗДЕ НЕ ВЫШЕ 209. Чужие проверки ищут врага
// по самому красному пикселю коробки: у рубаки и стрелка красный ровно 209
// (#d16a6a), и на этом стоит «самый красный пиксель — это враг» (4.2).
// Пинг рисуется поверх всего и сквозь туман, то есть может оказаться в
// любой коробке; возьми он красный 255 — и приёмка боя нашла бы «врага»
// там, где стоит метка. Поэтому «опасность» сделана НЕ самым красным, а
// самым тёплым и другой ФОРМОЙ: треугольник-знак против кружка «сюда».
// Различать пинги по форме всё равно обязательно — кадр тёмный, а
// дальтоник читает форму, а не оттенок (та же логика, что у силуэтов 4.2).
const PING_COL = [
  // 0 — «внимание, сюда»: холодный синий. Ни один игровой объект таким не
  // бывает (игрок зелёный, враг красный, замах жёлтый, рывок голубой —
  // но рывок это кольцо на теле, а не метка на полу).
  { fill: '#2f86c9', edge: '#a6e4ff', text: '#a6e4ff' },
  // 1 — «опасность»: тёплый, с жёлтой каймой знака.
  { fill: '#c8401f', edge: '#d1b03c', text: '#d1b03c' },
];
// Пульс: метка дышит, чтобы её ловил край глаза. Период 1.1 с — медленнее
// сердцебиения, быстрее, чем «ничего не происходит».
const PING_PULSE_MS = 1100;
// Отступ стрелки от края кадра. 46 px — половина метки (0.7 клетки = 45 px
// при 64 px/клетку), то есть стрелка целиком внутри кадра при любом краю.
const PING_EDGE = 46;
const PING_FONT = '15px system-ui, sans-serif';
const PING_FONT_SM = '13px ui-monospace, Consolas, monospace';

// Порог «мало здоровья»: ниже него полоска краснеет и по краю кадра идёт
// красная кайма. 0.5 — это ровно 2 попадания врага ближней атакой (20 урона,
// 4.2) до порога и ещё 2 после: предупреждение приходит на середине пути,
// а не когда всё уже решено.
const HURT_AT = 0.5;

// --- таблица видов (4.2, 4.3) --------------------------------------------
//
// Поведение вида задаётся ПОЛЯМИ ТАБЛИЦЫ, а не сравнениями `kind === ...`
// по телу рисовалки. Причина прямая: видов стало больше, чем два, и каждое
// такое сравнение пришлось бы искать и править при появлении следующего.
//   arrow — тело рисуется наконечником стрелы по facing, а не кружком;
//   aim   — рисуется отдельная чёрточка направления взгляда (наконечнику
//           она не нужна: он сам и есть направление);
//   bar   — над головой рисуется полоска здоровья.
//
// РУБАКА И СТРЕЛОК ОБЯЗАНЫ РАЗЛИЧАТЬСЯ С ОДНОГО ВЗГЛЯДА (4.2): один прёт в
// упор, другой держит 5-9 клеток и стреляет, а до этой работы оба были
// одинаковым красным кружком. Различие сделано ФОРМОЙ, а не только цветом:
// круг против наконечника читается и в темноте, и у того, кто не различает
// красный с зелёным. Красный канал у обоих одинаковый (209) намеренно —
// «красное = враг» остаётся правдой, и проверки, которые ищут врага по
// самому красному пикселю кадра, не замечают разницы.
const KIND = {};
KIND[K_PLAYER] = { r: 0.35, css: '#7fd18a', ring: '#dff3e2', aim: 1, bar: 1 };
KIND[K_ENEMY] = { r: 0.35, css: '#d16a6a', ring: '#f5cccc', aim: 1, bar: 1 };
KIND[K_ENEMY_RANGED] = { r: 0.35, css: '#d1568c', ring: '#f6c3d6',
                         arrow: 1, bar: 1 };
KIND[K_BOSS] = { r: 0.50, css: '#c25a3a', ring: '#f0c0a8', aim: 1, bar: 1 };
KIND[K_PROP] = { r: 0.30, css: '#8b7f5a', ring: '#cfc49c' };
KIND[K_SHOT] = { r: 0.12, css: '#ffe27a', ring: '#fff6cf' };
// Незнакомый kind не имеет права ронять клиент: сервер может начать слать
// новое значение раньше, чем клиент про него узнает (и наоборот). Такая
// сущность рисуется серым кружком — видно, что что-то есть, и видно, что
// клиент этого вида не знает.
const KIND_DEF = { r: 0.30, css: '#8b93a1', ring: '#d8dde5' };

// Наконечник стрелка: остриё вперёд на ARROW_TIP радиусов, два задних угла
// под ARROW_BACK радиан от направления взгляда на ARROW_REAR радиусах.
// Площадь такого треугольника = 1.21 r^2 против 3.14 r^2 у круга рубаки,
// то есть силуэты различаются по заполнению в 2.6 раза — это и есть
// «видно с одного взгляда», выраженное числом (tests/client_light.py).
const ARROW_TIP = 1.25;
const ARROW_REAR = 0.95;
const ARROW_BACK = 140 * Math.PI / 180;

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
    w: 0, h: 0, tilePx: 64, quality: 'high',

    // статический слой карты
    st: null, stCtx: null,
    stTx: 0, stTy: 0, stTw: 0, stTh: 0, stKey: '',
    repaints: 0,

    // предрасчитанный оверлей света/затемнения
    vig: null,

    // маска тумана: FOG_PX пикселей на клетку, окно то же, что у карты
    fog: null, fogCtx: null, fogImg: null, fogKey: '', fogOn: false,
    fogRepaints: 0,
    // Выключатель света — затем же, зачем setFog и setCombat: чтобы
    // tests/client_light.py умела ПОКРАСНЕТЬ и чтобы «до и после» мерилось
    // на ОДНОЙ сцене спина к спине, а не на двух разных прогонах.
    torch: true,
    _lights: [],

    camX: 0, camY: 0,
    _drawCalls: 0, _ents: 0, _ms: 0, _hidden: 0, _fx: 0,
    _pingsDrawn: 0, _pingsOff: 0,

    // Выключатели существуют ровно затем же, зачем setFog и setInterp:
    // чтобы проверка боя умела ПОКРАСНЕТЬ, а замер «стоимость кадра до и
    // после» делался на одной сцене спина к спине.
    //   combat=false — рендер ровно такой, каким он был до этой работы:
    //     ни замаха, ни рывка, ни вспышек, ни своего здоровья;
    //   windup=false — всё остальное на месте, не рисуется только замах.
    combat: true, windup: true,
    // 8.8: выключатель рисования пингов. Существует ровно затем же, зачем
    // combat и torch: чтобы приёмка умела ПОКРАСНЕТЬ и чтобы стоимость
    // кадра «с пингами / без» мерилась спина к спине на одной сцене.
    pings: true,

    _pool: [], _vis: [],

    // --- 7.1 ---------------------------------------------------------

    init(canvas, opts) {
      opts = opts || {};
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d', { alpha: false });
      this.tilePx = opts.tilePx || 64;
    // 4.1: базовый масштаб 64 px/клетку (было 48).
      // ЗАЧЕМ: тело игрока 0.35 клетки — это 34 px при 48 и 45 px при 64.
      // На экране 1920 первое читается как точка, и движение точки глаз
      // меряет В ДОЛЯХ ЭКРАНА: кадр при 48 — 40x22.5 клетки, при 64 —
      // 30x16.9. Тот же самый бег пересекает кадр на треть быстрее, и
      // стоит это ноль: зум — ручка клиента, на симуляцию он не влияет
      // (4.1 прямо называет его ручкой «если нужно больше поля» вместо
      // радиуса обзора, который на сервере стоит r^2).
      // Побочно чинится и тёмная рамка: диск обзора 10 клеток (20 в
      // поперечнике) занимал половину ширины кадра, теперь две трети.
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
               hidden: this._hidden, fogRepaints: this.fogRepaints,
               fx: this._fx, pings: this._pingsDrawn,
               pingsOff: this._pingsOff };
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
      const lights = this._torchLights(view);
      const key = this.stKey + '|' + this.stTx + ',' + this.stTy +
                  '|' + view.fogVersion + '|' + lights.join(',');
      if (key === this.fogKey) return true;
      this.fogKey = key;
      this._paintFog(level, arr, lights);
      this.fogRepaints++;
      return true;
    },

    /**
     * Где сейчас стоят факелы. Источник — ЖИВЫЕ игроки, и это не вольность:
     * 4.4 уже говорит, что мёртвый игрок туман не светит (обзор считается по
     * живым). Дух не несёт и огня — иначе труп в коридоре освещал бы комнату,
     * которую группа не видит.
     *
     * Координаты квантуются шагом 1/TORCH_Q клетки: маска перерисовывается
     * только когда ключ изменился, а без квантования ключ менялся бы каждый
     * кадр — то есть маска считалась бы каждый кадр, ровно то, что запрещает
     * 7.2.
     */
    _torchLights(view) {
      const out = this._lights;
      out.length = 0;
      if (!this.torch) return out;
      const ents = view.ents;
      if (!ents) return out;
      for (let i = 0; i < ents.length; i++) {
        const e = ents[i];
        if (e.kind !== K_PLAYER || (e.flags & F_DEAD)) continue;
        out.push(Math.round(e.dx * TORCH_Q) / TORCH_Q,
                 Math.round(e.dy * TORCH_Q) / TORCH_Q);
      }
      return out;
    },

    _paintFog(level, arr, lights) {
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
      const nL = lights ? lights.length : 0;
      const tr = TORCH_RGB[0], tg = TORCH_RGB[1], tb = TORCH_RGB[2];
      // Альфа плато и спад на клетку — считаются один раз на перерисовку.
      const aIn = TORCH_A_IN * 255, aOut = TORCH_A_OUT * 255;
      const slope = (aIn - aOut) / (TORCH_R_OUT - TORCH_R_IN);
      // За этим расстоянием свет ровный (TORCH_A_OUT) — там не нужен ни
      // корень, ни поиск ближайшего факела: клетка заливается одним числом.
      const farQ = (TORCH_R_OUT + 1.5) * (TORCH_R_OUT + 1.5);
      const aFar = nL ? (aOut | 0) : 0;

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
          if (v === VIS_LIT) {
            // Освещено сейчас — сюда и ложится свет факела. Без факелов
            // (this.torch === false, или в кадре нет живых игроков) клетка
            // остаётся полностью прозрачной, ровно как было до этой работы.
            if (!nL) continue;
            const x0 = x * FOG_PX, y0l = y * FOG_PX;
            // Сначала грубо: далеко ли клетка от всех факелов.
            let cq = 1e9;
            const ccx = wx + 0.5, ccy = wy + 0.5;
            for (let li = 0; li < nL; li += 2) {
              const ddx = ccx - lights[li], ddy = ccy - lights[li + 1];
              const q = ddx * ddx + ddy * ddy;
              if (q < cq) cq = q;
            }
            if (cq > farQ) {
              if (aFar === 0) continue;
              for (let py = y0l; py < y0l + FOG_PX; py++) {
                let o = (py * cw + x0) * 4;
                for (let px = 0; px < FOG_PX; px++) {
                  d[o] = tr; d[o + 1] = tg; d[o + 2] = tb; d[o + 3] = aFar;
                  o += 4;
                }
              }
              continue;
            }
            for (let py = y0l; py < y0l + FOG_PX; py++) {
              const pwy = ty0 + (py + 0.5) / FOG_PX;
              let o = (py * cw + x0) * 4;
              for (let px = 0; px < FOG_PX; px++) {
                const pwx = tx0 + (x0 + px + 0.5) / FOG_PX;
                let best = 1e9;
                for (let li = 0; li < nL; li += 2) {
                  const ddx = pwx - lights[li], ddy = pwy - lights[li + 1];
                  const q = ddx * ddx + ddy * ddy;
                  if (q < best) best = q;
                }
                const dist = Math.sqrt(best);
                let a2 = dist <= TORCH_R_IN ? aIn
                       : (dist >= TORCH_R_OUT ? aOut
                          : aIn - (dist - TORCH_R_IN) * slope);
                d[o] = tr; d[o + 1] = tg; d[o + 2] = tb; d[o + 3] = a2 | 0;
                o += 4;
              }
            }
            continue;
          }
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

    // --- бой: замах и рывок живут в flags (4.3), а не в событиях ------
    //
    // Почему это вообще рисуется отдельной функцией, а не двумя строчками в
    // теле сущности: замах обязан быть виден ВСЕ 8 тиков (0.267 с) и обязан
    // показывать, КУДА придёт удар. Сервер замораживает facing на время
    // замаха (combat.start_melee), поэтому нарисованный сектор — это не
    // намёк, а обещание: удар придёт ровно в него.

    _drawWindup(e, sx, sy, r) {
      const ctx = this.ctx;
      const reach = MELEE_REACH * this.tilePx;
      const a0 = e.dfacing - MELEE_ARC / 2;
      const a1 = e.dfacing + MELEE_ARC / 2;
      // Сектор: заливка даёт площадь, её видно краем глаза.
      ctx.fillStyle = COL.windupFill;
      ctx.beginPath();
      ctx.moveTo(sx, sy);
      ctx.arc(sx, sy, reach, a0, a1);
      ctx.closePath();
      ctx.fill();
      // Кромка: по ней видно точную границу досягаемости.
      ctx.strokeStyle = COL.windupEdge;
      ctx.lineWidth = Math.max(2, r * 0.18);
      ctx.stroke();
      // Кольцо вокруг самого бойца: если сектор ушёл за край кадра или
      // накрыт телом соседа, замах всё равно видно по владельцу.
      ctx.strokeStyle = COL.windupRing;
      ctx.lineWidth = Math.max(2, r * 0.26);
      ctx.beginPath();
      ctx.arc(sx, sy, r * 1.15, 0, Math.PI * 2);
      ctx.stroke();
      this._drawCalls += 3;
    },

    _drawDash(e, sx, sy, r) {
      const ctx = this.ctx, tpx = this.tilePx;
      // Хвост строится по СКОРОСТИ из снапшота: направление рывка отдельным
      // полем не передаётся, а vx/vy на рывке — это ровно оно (14 кл/с).
      let dx = e.vx, dy = e.vy;
      const d = Math.hypot(dx, dy);
      if (d > 1e-6) {
        dx /= d; dy /= d;
        ctx.fillStyle = COL.dashTrail;
        for (let i = 1; i <= 3; i++) {
          const k = i * 0.32 * tpx;
          ctx.beginPath();
          ctx.arc(sx - dx * k, sy - dy * k, r * (1 - i * 0.18), 0, Math.PI * 2);
          ctx.fill();
        }
        this._drawCalls += 3;
      }
      // Кольцо неуязвимости: холодное и яркое, чтобы «меня сейчас не
      // достать» читалось с одного взгляда и отличалось от замаха.
      ctx.strokeStyle = COL.dashRing;
      ctx.lineWidth = Math.max(2, r * 0.22);
      ctx.beginPath();
      ctx.arc(sx, sy, r * 1.45, 0, Math.PI * 2);
      ctx.stroke();
      this._drawCalls++;
    },

    _drawShot(e, sx, sy, r) {
      const ctx = this.ctx, tpx = this.tilePx;
      let dx = e.vx, dy = e.vy;
      const d = Math.hypot(dx, dy);
      if (d > 1e-6) {
        dx /= d; dy /= d;
        const tail = SHOT_TAIL * tpx;
        ctx.strokeStyle = COL.fxShot;
        ctx.lineWidth = Math.max(2, r * 1.1);
        ctx.beginPath();
        ctx.moveTo(sx - dx * tail, sy - dy * tail);
        ctx.lineTo(sx, sy);
        ctx.stroke();
        this._drawCalls++;
      }
      ctx.fillStyle = '#fff6cf';
      ctx.beginPath();
      ctx.arc(sx, sy, r, 0, Math.PI * 2);
      ctx.fill();
      this._drawCalls++;
    },

    _drawEntity(e, sx, sy, isSelf) {
      const ctx = this.ctx, tpx = this.tilePx;
      const k = KIND[e.kind] || KIND_DEF;
      const r = k.r * tpx;
      const dead = (e.flags & F_DEAD) !== 0;
      const offline = (e.flags & F_OFFLINE) !== 0;

      if (e.kind === K_SHOT) {          // снаряд: ни тени, ни полоски, ни глаз
        this._drawShot(e, sx, sy, r);
        return;
      }

      // Тень — плоский эллипс, без shadowBlur (7.2).
      if (!dead) {
        ctx.fillStyle = COL.shadow;
        ctx.beginPath();
        ctx.ellipse(sx, sy + r * 0.75, r * 0.95, r * 0.42, 0, 0, Math.PI * 2);
        ctx.fill();
        this._drawCalls++;
      }

      // Замах — ПОД телом: тело сверху, сектор вокруг него.
      if (this.combat && this.windup && !dead && (e.flags & F_WINDUP)) {
        this._drawWindup(e, sx, sy, r);
      }
      if (this.combat && !dead && (e.flags & F_DASH)) {
        this._drawDash(e, sx, sy, r);
      }

      // 4.3: бит 1 = мёртв -> дух (полупрозрачный, холодный).
      // бит 2 = игрок отвалился -> гасим, а не оставляем столбом.
      let alpha = 1;
      if (dead) alpha = 0.32;
      if (offline) alpha = Math.min(alpha, 0.28);
      ctx.globalAlpha = alpha;

      ctx.fillStyle = dead ? '#9fc8ff' : (offline ? '#6b7280' : (isSelf ? '#ffe27a' : k.css));
      if (k.arrow) {
        // Стрелок: наконечник по facing. Ни градиента, ни composite (7.2) —
        // три линии и заливка.
        const f = e.dfacing;
        ctx.beginPath();
        ctx.moveTo(sx + Math.cos(f) * r * ARROW_TIP,
                   sy + Math.sin(f) * r * ARROW_TIP);
        ctx.lineTo(sx + Math.cos(f + ARROW_BACK) * r * ARROW_REAR,
                   sy + Math.sin(f + ARROW_BACK) * r * ARROW_REAR);
        ctx.lineTo(sx + Math.cos(f - ARROW_BACK) * r * ARROW_REAR,
                   sy + Math.sin(f - ARROW_BACK) * r * ARROW_REAR);
        ctx.closePath();
        ctx.fill();
        ctx.strokeStyle = dead ? 'rgba(200,225,255,0.7)' : k.ring;
        ctx.lineWidth = Math.max(1.5, r * 0.16);
        ctx.stroke();
        this._drawCalls += 2;
      } else {
        ctx.beginPath();
        ctx.arc(sx, sy, r, 0, Math.PI * 2);
        ctx.fill();
        this._drawCalls++;
      }

      if (isSelf) {
        ctx.strokeStyle = '#fff6cf';
        ctx.lineWidth = Math.max(1.5, r * 0.16);
        ctx.beginPath();
        ctx.arc(sx, sy, r * 1.35, 0, Math.PI * 2);
        ctx.stroke();
        this._drawCalls++;
      }

      // Направление взгляда (facing в радианах, 4.3). У наконечника его
      // рисовать нечем: он сам и есть направление.
      if (k.aim) {
        ctx.strokeStyle = dead ? 'rgba(200,225,255,0.7)' : k.ring;
        ctx.lineWidth = Math.max(1.5, r * 0.20);
        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(sx + Math.cos(e.dfacing) * r * 1.5, sy + Math.sin(e.dfacing) * r * 1.5);
        ctx.stroke();
        this._drawCalls++;
      }
      ctx.globalAlpha = 1;

      // Полоска здоровья — ради неё hp_max и живёт в снапшоте (4.3).
      // Берётся ТОЛЬКО из снапшота: событие hit может потеряться (5.2), и
      // полоска, собранная из событий, показала бы ложь ровно в тот момент,
      // когда сеть просела.
      if (!dead && e.hpMax > 0 && e.hp < e.hpMax && k.bar) {
        const f = Math.max(0, Math.min(1, e.hp / e.hpMax));
        const bw = tpx * HPBAR_W, bh = Math.max(3, tpx * 0.09);
        const bx = sx - bw / 2, by = sy - r - bh * 2.2;
        ctx.fillStyle = COL.hpBack;
        ctx.fillRect(bx - 1, by - 1, bw + 2, bh + 2);
        ctx.fillStyle = COL.hpBad;
        ctx.fillRect(bx, by, bw, bh);
        ctx.fillStyle = f > HURT_AT ? COL.hpGood : COL.hpLow;
        ctx.fillRect(bx, by, bw * f, bh);
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

    // --- вспышки событий (5.2) ---------------------------------------
    //
    // Событие может потеряться, состояния на нём нет: пропавшая вспышка
    // стоит ровно одной невидимой искры, а не вранья на экране. Всё, что
    // тут рисуется, — плоские заливки и обводки: ни градиента, ни
    // shadowBlur, ни composite (7.2). Градиент в кадре — самая частая
    // причина, по которой «красивые вспышки» съедают бюджет.

    _drawFx(view, now) {
      const fx = view.fx;
      this._fx = 0;
      if (!fx || !this.combat) return;
      const ctx = this.ctx, tpx = this.tilePx, life = view.fxLife;
      const fogOn = !!(view.fog && view.level);
      const me = view.selfId;
      for (let i = 0; i < fx.length; i++) {
        const f = fx[i];
        if (f.k === 0) continue;
        const age = now - f.t0;
        if (age < 0 || age > life) continue;
        const sx = (f.x - this.camX) * tpx;
        const sy = (f.y - this.camY) * tpx;
        if (sx < -tpx * 2 || sy < -tpx * 2 ||
            sx > this.w + tpx * 2 || sy > this.h + tpx * 2) continue;
        // 5.2: то, что происходит в темноте, клиент показывать не имеет
        // права — вспышка выдала бы позицию врага не хуже его тела.
        // Исключение то же, что и для сущностей: своё видно всегда.
        if (fogOn && f.a !== me && f.b !== me && !this._lit(view, f.x, f.y)) continue;
        const p = age / life;                 // 0..1 — прожитая доля
        const fade = 1 - p;
        this._fx++;
        ctx.globalAlpha = fade;
        if (f.k === FX_HIT) {
          // Кольцо расходится, ядро гаснет на месте. Размер кольца — от
          // урона: 20 (ближняя) заметно крупнее 12 (снаряд).
          const grow = tpx * (0.20 + 0.50 * p) * (1 + Math.min(1, f.dmg / 40));
          ctx.strokeStyle = COL.fxHit;
          ctx.lineWidth = Math.max(2, tpx * 0.09 * fade);
          ctx.beginPath();
          ctx.arc(sx, sy, grow, 0, Math.PI * 2);
          ctx.stroke();
          ctx.fillStyle = COL.fxHitCore;
          ctx.beginPath();
          ctx.arc(sx, sy, tpx * 0.16 * fade, 0, Math.PI * 2);
          ctx.fill();
          this._drawCalls += 2;
        } else if (f.k === FX_DIE) {
          ctx.strokeStyle = COL.fxDie;
          ctx.lineWidth = Math.max(2, tpx * 0.10 * fade);
          ctx.beginPath();
          ctx.arc(sx, sy, tpx * (0.30 + 1.10 * p), 0, Math.PI * 2);
          ctx.stroke();
          this._drawCalls++;
        } else if (f.k === FX_SHOT) {
          ctx.fillStyle = COL.fxShot;
          ctx.beginPath();
          ctx.arc(sx, sy, tpx * 0.22 * fade, 0, Math.PI * 2);
          ctx.fill();
          this._drawCalls++;
        } else {                              // FX_BOOM: искра о стену
          ctx.fillStyle = COL.fxBoom;
          ctx.beginPath();
          ctx.arc(sx, sy, tpx * (0.10 + 0.16 * p) * fade, 0, Math.PI * 2);
          ctx.fill();
          this._drawCalls++;
        }
      }
      ctx.globalAlpha = 1;
    },

    // --- пинги на карте (8.8) ----------------------------------------
    //
    // ПИНГ ВИДЕН СКВОЗЬ СТЕНЫ И В ЧЕРНОТЕ, И ЭТО НЕ НАРУШЕНИЕ 5.2. Запрет
    // 5.2 — про СУЩНОСТИ: сервер шлёт всех подряд, и клиент, рисующий их в
    // темноте, выдаёт игроку позиции врагов, которых группа не видит. Пинг
    // ставит ЧЕЛОВЕК руками из того, что видит сам, — он не выдаёт ничего,
    // он и есть способ рассказать. Спрятать его в темноте значило бы
    // выключить 8.8 ровно там, где он нужен: «не ходи в тот чёрный
    // коридор» — сообщение ИМЕННО про черноту.
    //
    // Поэтому пинги рисуются ПОСЛЕ виньетки, вместе со своим здоровьем:
    // это интерфейс, а не мир. Под виньеткой метка у края кадра гасла бы в
    // 0.2 раза (замерено в _drawSelfHud) — то есть ровно там, куда смотрит
    // стрелка «пинг вне кадра».
    //
    // ЗАПРЕЩЁННОГО 7.2 здесь нет: ни shadowBlur, ни filter, ни
    // globalCompositeOperation. Текст — кэшированные спрайты (textSprite).

    _pingBadge(cx, cy, r, kind, a) {
      const ctx = this.ctx, c = PING_COL[kind] || PING_COL[0];
      ctx.globalAlpha = a;
      ctx.fillStyle = c.fill;
      ctx.strokeStyle = c.edge;
      ctx.lineWidth = Math.max(2, r * 0.22);
      ctx.beginPath();
      if (kind === 1) {
        // «Опасность» — треугольник вершиной вверх: знак, а не метка.
        ctx.moveTo(cx, cy - r);
        ctx.lineTo(cx + r * 0.92, cy + r * 0.72);
        ctx.lineTo(cx - r * 0.92, cy + r * 0.72);
      } else {
        // «Сюда» — ромб: круглым нельзя (кругом нарисованы тела, 4.2).
        ctx.moveTo(cx, cy - r);
        ctx.lineTo(cx + r * 0.82, cy);
        ctx.lineTo(cx, cy + r);
        ctx.lineTo(cx - r * 0.82, cy);
      }
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
      this._drawCalls += 2;
      if (kind === 1) {
        // Восклицательный знак внутри: два прямоугольника, ноль текста.
        ctx.fillStyle = '#1a0e08';
        ctx.fillRect(cx - r * 0.11, cy - r * 0.38, r * 0.22, r * 0.66);
        ctx.fillRect(cx - r * 0.11, cy + r * 0.44, r * 0.22, r * 0.20);
        this._drawCalls += 2;
      }
    },

    _drawPings(view, now) {
      this._pingsDrawn = 0;
      this._pingsOff = 0;
      const ps = view.pings;
      if (!this.pings || !ps || ps.length === 0) return;
      const ctx = this.ctx, tpx = this.tilePx;
      const fade = view.pingFade || 400;
      const pulse = 0.5 + 0.5 * Math.sin(now * (2 * Math.PI / PING_PULSE_MS));
      const W = this.w, H = this.h, M = PING_EDGE;
      for (let i = 0; i < ps.length; i++) {
        const p = ps[i];
        const left = p.dies - now;
        if (left <= 0) continue;
        // Гаснет плавно: пинг, пропадающий мгновенно, читается как «что-то
        // моргнуло», а не как «время вышло».
        const a = left < fade ? Math.max(0, left / fade) : 1;
        const sx = (p.x - this.camX) * tpx;
        const sy = (p.y - this.camY) * tpx;
        const col = PING_COL[p.u] || PING_COL[0];
        const inside = sx >= M && sx <= W - M && sy >= M && sy <= H - M;
        if (inside) {
          this._pingsDrawn++;
          // Кольцо на полу: оно и есть «вот эта точка». Расходится по
          // пульсу — движение ловит край глаза даже в темноте.
          ctx.globalAlpha = a * (0.85 - 0.45 * pulse);
          ctx.strokeStyle = col.edge;
          ctx.lineWidth = Math.max(2, tpx * 0.07);
          ctx.beginPath();
          ctx.arc(sx, sy, tpx * (0.30 + 0.34 * pulse), 0, Math.PI * 2);
          ctx.stroke();
          this._drawCalls++;
          // Ножка от точки к значку: без неё значок «висит» и непонятно,
          // на какую клетку он показывает.
          const top = sy - tpx * 0.92;
          ctx.globalAlpha = a;
          ctx.strokeStyle = col.fill;
          ctx.lineWidth = Math.max(2, tpx * 0.05);
          ctx.beginPath();
          ctx.moveTo(sx, sy);
          ctx.lineTo(sx, top + tpx * 0.22);
          ctx.stroke();
          this._drawCalls++;
          this._pingBadge(sx, top, tpx * 0.28, p.u, a);
          this._pingName(p, sx, top - tpx * 0.42, a, col);
        } else {
          // ВНЕ КАДРА — стрелка у края, остриём точно на пинг. Без неё
          // половина смысла 8.8 теряется: «опасность» за спиной не видна
          // вовсе, а спросить некого.
          this._pingsOff++;
          const self = view.self;
          const ox = self ? (self.dx - this.camX) * tpx : W / 2;
          const oy = self ? (self.dy - this.camY) * tpx : H / 2;
          let dx = sx - ox, dy = sy - oy;
          const d = Math.hypot(dx, dy) || 1;
          dx /= d; dy /= d;
          // Точка на рамке кадра по лучу из игрока: параметр t по каждой
          // оси, берём меньший — он и есть первая пересечённая сторона.
          const tx = dx > 0 ? (W - M - ox) / dx : (dx < 0 ? (M - ox) / dx : 1e9);
          const ty = dy > 0 ? (H - M - oy) / dy : (dy < 0 ? (M - oy) / dy : 1e9);
          const t = Math.max(0, Math.min(tx, ty));
          const ax = ox + dx * t, ay = oy + dy * t;
          const r = tpx * 0.26;
          ctx.globalAlpha = a;
          ctx.fillStyle = col.fill;
          ctx.strokeStyle = col.edge;
          ctx.lineWidth = Math.max(2, r * 0.25);
          ctx.beginPath();
          ctx.moveTo(ax + dx * r * 1.35, ay + dy * r * 1.35);
          ctx.lineTo(ax - dx * r * 0.7 - dy * r, ay - dy * r * 0.7 + dx * r);
          ctx.lineTo(ax - dx * r * 0.7 + dy * r, ay - dy * r * 0.7 - dx * r);
          ctx.closePath();
          ctx.fill();
          ctx.stroke();
          this._drawCalls += 2;
          // Сколько клеток до метки: без числа стрелка говорит «туда», а
          // человеку нужно «туда и далеко».
          const cells = Math.round(d / tpx);
          const spr = textSprite(cells + ' кл', PING_FONT_SM, col.text);
          ctx.drawImage(spr, ax - spr.width / 2 - dx * r * 2.2,
                        ay - spr.height / 2 - dy * r * 2.2);
          this._drawCalls++;
          this._pingName(p, ax - dx * r * 2.2, ay - dy * r * 2.2 - 15, a, col);
        }
      }
      ctx.globalAlpha = 1;
    },

    // КТО ПОСТАВИЛ — обязательная часть, а не подпись для красоты. Четверо
    // в темноте, метка без автора не говорит ничего: «иду сюда» от того,
    // кто стоит рядом, и от того, кто ушёл на два экрана, — разные вещи.
    _pingName(p, cx, cy, a, col) {
      if (!p.nm) return;
      const spr = textSprite(p.nm, PING_FONT, col.text);
      this.ctx.globalAlpha = a;
      this.ctx.drawImage(spr, cx - spr.width / 2, cy - spr.height / 2);
      this._drawCalls++;
    },

    // --- своё здоровье и смерть: поверх виньетки ----------------------
    //
    // Рисуется ПОСЛЕ затемнения намеренно. Виньетка у нижнего края кадра
    // множит на 0.2 (замерено), и полоска здоровья, положенная под неё,
    // оказалась бы самым тусклым местом экрана — ровно то, на что смотрят
    // в последнюю секунду жизни.

    /**
     * Где лежит полоска СВОЕГО здоровья. Отдельным методом, потому что это
     * же число нужно проверке: она смотрит пиксели полоски, и списывать
     * геометрию в питон означало бы завести вторую истину о раскладке.
     */
    hpBarRect() {
      const w = Math.min(420, this.w * 0.28);
      return { x: (this.w - w) / 2, y: this.h - 20 - 26, w: w, h: 20 };
    },

    _drawSelfHud(view) {
      if (!this.combat) return;
      const me = view.self;
      if (!me) return;
      const ctx = this.ctx;
      const dead = (me.flags & F_DEAD) !== 0;

      if (!dead && me.hpMax > 0) {
        const f = Math.max(0, Math.min(1, me.hp / me.hpMax));
        const b = this.hpBarRect();
        const bw = b.w, bh = b.h, bx = b.x, by = b.y;
        ctx.fillStyle = COL.hpBack;
        ctx.fillRect(bx - 3, by - 3, bw + 6, bh + 6);
        ctx.fillStyle = COL.hpBad;
        ctx.fillRect(bx, by, bw, bh);
        ctx.fillStyle = f > HURT_AT ? COL.hpGood : COL.hpLow;
        ctx.fillRect(bx, by, bw * f, bh);
        const spr = textSprite(me.hp + ' / ' + me.hpMax,
                               '14px ui-monospace, Consolas, monospace', '#0a0d12');
        ctx.drawImage(spr, bx + bw / 2 - spr.width / 2, by + bh / 2 - spr.height / 2);
        this._drawCalls += 4;

        // Мало здоровья — красная кайма по краю кадра. Кайма, а не заливка
        // всего экрана: четыре полосы по 40 px это 0.24 млн пикселей против
        // 2.07 млн у полного кадра, в 8.6 раза дешевле, а видно её так же.
        if (f <= HURT_AT) {
          const t = 40, a = (HURT_AT - f) / HURT_AT;
          ctx.globalAlpha = 0.25 + 0.55 * a;
          ctx.fillStyle = COL.hurtEdge;
          ctx.fillRect(0, 0, this.w, t);
          ctx.fillRect(0, this.h - t, this.w, t);
          ctx.fillRect(0, t, t, this.h - t * 2);
          ctx.fillRect(this.w - t, t, t, this.h - t * 2);
          ctx.globalAlpha = 1;
          this._drawCalls += 4;
        }
        return;
      }

      if (dead) {
        // 8.5: смерть это не серый экран, а другой режим игры. Так и
        // написано словами — иначе игрок пять секунд не понимает, почему
        // он вдруг проходит сквозь стены.
        ctx.globalAlpha = 0.16;
        ctx.fillStyle = '#2a5fa8';
        ctx.fillRect(0, 0, this.w, this.h);
        ctx.globalAlpha = 1;
        const a = textSprite('ТЫ ДУХ', '44px system-ui, sans-serif', '#cfe4ff');
        const b = textSprite('летишь сквозь стены · воскреснешь на следующем этаже',
                             '18px system-ui, sans-serif', '#9fc8ff');
        ctx.drawImage(a, (this.w - a.width) / 2, this.h * 0.5 - a.height);
        ctx.drawImage(b, (this.w - b.width) / 2, this.h * 0.5 + 6);
        this._drawCalls += 3;
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
        // Своя сущность рисуется всегда (5.2). Второе исключение —
        // МЁРТВЫЙ ИГРОК: дух не светит туман (4.4), поэтому союзник,
        // умерший в неразведанном коридоре, не виден никому вообще, и
        // группа теряет его насовсем. Ничего секретного это не выдаёт:
        // враги живыми игроками не бывают (team 1 — только игроки), а
        // трупы врагов сервер убирает из мира в тот же тик (combat.resolve).
        // Правка 5.2 — в отчёте.
        const ghost = e.kind === K_PLAYER && (e.flags & F_DEAD) !== 0;
        if (fogOn && e.id !== view.selfId && !ghost &&
            !this._lit(view, e.dx, e.dy)) {
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

      // 4) вспышки событий — ПОВЕРХ сущностей, но ПОД затемнением: искра
      // в дальнем углу должна гаснуть в темноте вместе со всем остальным.
      this._drawFx(view, t0);

      // 5) свет и затемнение — один блит предрасчитанного оверлея (7.2).
      // q=low обходится без него: это украшение, а не механика.
      if (this.quality !== 'low' && this.vig) {
        ctx.drawImage(this.vig, 0, 0);
        this._drawCalls++;
      }

      // 6) пинги (8.8) — ПОВЕРХ виньетки: это речь игроков, а не мир, и
      // гаснуть в темноте она не должна (см. _drawPings).
      this._drawPings(view, t0);

      // 7) своё здоровье и «ты дух» — поверх всего: это интерфейс, а не мир.
      this._drawSelfHud(view);

      this._ms = performance.now() - t0;
    },
  };
}

export default createRenderer;
