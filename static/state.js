// -*- coding: utf-8 -*-
// Локальное зеркало мира и интерполяция (DESIGN.md 4.3, 5.2, 6.1).
//
// Что здесь есть и чего здесь нет:
//
//  * ЕСТЬ: применение снапшотов (полных и дельт), история позиций каждой
//    сущности, часы рендера с задержкой в два тика и интерполяция между
//    двумя ближайшими выборками (6.1);
//  * НЕТ: предсказания своего персонажа. Это решение 6.2, а не недосмотр.
//    Свой персонаж рисуется ровно тем, что прислал сервер, — как и чужой.
//
// Сеть сюда не заглядывает: net.js зовёт applySnap()/applyLevel(), а всё
// остальное — чистая арифметика над числами.

export const TICK_HZ = 30;
export const DT = 1 / TICK_HZ;

// 6.1: рисуем с задержкой два тика относительно последнего снапшота.
// Замерено на живом клиенте: показанное состояние отстаёт от свежего
// снапшота на 1.0..2.0 тика, в среднем на 1.5 (33..67 мс, в среднем 50).
// Это не расхождение с контрактом, а его следствие: интерполяция идёт
// между снапшотами tick-2 и tick-1, и пока мы едем от первого ко второму,
// отставание убывает с 2 до 1. «Два тика» из 6.1 — верхний край пилы.
export const DELAY_TICKS = 2;

// kind (world.py)
export const K_PLAYER = 1;
export const K_ENEMY = 2;
export const K_PROP = 3;
export const K_SHOT = 4;

// flags (DESIGN.md 4.3). WINDUP и DASH — НЕ украшение: 5.2 запрещает держать
// состояние на событиях ev, поэтому замах (8 тиков) и рывок (5 тиков) клиенту
// больше нечем рисовать. Читаются каждый кадр, объявлены здесь один раз.
export const F_DEAD = 1;
export const F_OFFLINE = 2;
export const F_WINDUP = 4;
export const F_DASH = 8;

// Виды событий ev (5.2). На проводе — строки; внутри клиента числа, чтобы
// сравнение в горячем пути рендера было числовым, а не строковым.
export const FX_HIT = 1;
export const FX_DIE = 2;
export const FX_SHOT = 3;
export const FX_BOOM = 4;
const FX_KIND = { hit: FX_HIT, die: FX_DIE, shot: FX_SHOT, boom: FX_BOOM };

// Сколько вспышек живёт одновременно. 5.2: потолок событий 64 на тик, а
// вспышка живёт 0.3 с = 9 тиков; честный потолок 64*9 = 576 записей в кадре
// не нужен никому — глазом в одной точке различимы единицы. 96 — это полтора
// потолка ОДНОГО тика: пачка из одного тика влезает целиком, а дальше новые
// вспышки затирают самые старые. Кольцо выделено один раз: мусор на 30 Гц
// событий сборщик собирает не бесплатно.
const FX_MAX = 96;
const FX_LIFE_MS = 300;

// Тайлы карты (DESIGN.md 4.1, server/physics.py)
export const TILE_WALL = 0;
export const TILE_FLOOR = 1;
export const TILE_STAIRS = 2;   // лестница вниз; стеной для физики она не является

// Туман войны: значения клетки на проводе (DESIGN.md 5.2, server/vis.py).
// Туман ОДИН на команду (4.4) — клиент не считает его сам, а только применяет
// то, что прислал сервер.
export const VIS_DARK = 0;      // не открыта: клиент не знает о ней ничего
export const VIS_SEEN = 1;      // открыта, но сейчас не видна — память группы
export const VIS_LIT = 2;       // видна прямо сейчас хотя бы одному живому
export const VIS_KEEP = 255;    // только в дельте: клетка не менялась

// Глубина истории позиций на сущность. Нужно строго больше DELAY_TICKS,
// иначе кадр с задержкой в два тика не попадает между двумя выборками.
// Восемь тиков = 267 мс: переживает потерю подряд нескольких снапшотов,
// а памяти стоит 4 массива по 8 чисел на сущность (256 байт).
const HIST = 8;

// Часы рендера догоняют цель не рывком, а изменением темпа. Предел темпа —
// ±10 % от номинального; отсюда берётся порог плавности в tests/.
const RATE_CLAMP = 0.10;
const RATE_GAIN = 0.25;      // насколько быстро съедается рассинхрон
const HARD_RESYNC = 6;       // тиков: больше — не догоняем, а прыгаем

// Скорость бега игрока (DESIGN.md 4.2) — нужна только для опознания своей
// сущности по корреляции ввода, на отрисовку не влияет.
const SPEED_RUN = 5.0;

// Глубина журнала событий для проверок. 64 — потолок событий на тик (4.2),
// то есть журнал вмещает ровно одну самую тяжёлую пачку.
const EVLOG = 64;

function lerp(a, b, t) { return a + (b - a) * t; }

// Углы интерполируем по короткой дуге, иначе поворот через ±π даёт оборот
// на месте.
function lerpAngle(a, b, t) {
  let d = b - a;
  while (d > Math.PI) d -= Math.PI * 2;
  while (d < -Math.PI) d += Math.PI * 2;
  return a + d * t;
}

function makeEnt(id) {
  return {
    id: id,
    kind: 0,
    // последнее авторитетное (из снапшота)
    x: 0, y: 0, vx: 0, vy: 0,
    hp: 0, hpMax: 0, flags: 0, facing: 0,
    // история для интерполяции: кольцевые буферы
    st: new Float64Array(HIST),
    sx: new Float64Array(HIST),
    sy: new Float64Array(HIST),
    sf: new Float64Array(HIST),
    sn: 0, si: 0,
    // то, что видит рендер (заполняется в view())
    dx: 0, dy: 0, dfacing: 0,
    lastTick: -1,
  };
}

function pushSample(e, tick, x, y, facing) {
  const i = e.si;
  e.st[i] = tick; e.sx[i] = x; e.sy[i] = y; e.sf[i] = facing;
  e.si = (i + 1) % HIST;
  if (e.sn < HIST) e.sn++;
}

// Интерполяция позиции сущности на момент renderTick.
// Правила:
//   * есть выборки по обе стороны -> линейная интерполяция;
//   * все выборки раньше renderTick -> держим последнюю (НЕ экстраполируем:
//     6.2 запрещает предсказание, а экстраполяция — это оно же);
//   * все выборки позже -> держим первую (бывает только в первые кадры).
function sampleAt(e, renderTick) {
  const n = e.sn;
  if (n === 0) { e.dx = e.x; e.dy = e.y; e.dfacing = e.facing; return; }
  const base = (e.si - n + HIST * 2) % HIST;   // индекс самой старой выборки
  let lo = -1, hi = -1;
  for (let k = 0; k < n; k++) {
    const idx = (base + k) % HIST;
    if (e.st[idx] <= renderTick) lo = idx;
    else { hi = idx; break; }
  }
  if (lo < 0) { const idx = base; e.dx = e.sx[idx]; e.dy = e.sy[idx]; e.dfacing = e.sf[idx]; return; }
  if (hi < 0) { e.dx = e.sx[lo]; e.dy = e.sy[lo]; e.dfacing = e.sf[lo]; return; }
  const span = e.st[hi] - e.st[lo];
  const t = span > 0 ? (renderTick - e.st[lo]) / span : 0;
  e.dx = lerp(e.sx[lo], e.sx[hi], t);
  e.dy = lerp(e.sy[lo], e.sy[hi], t);
  e.dfacing = lerpAngle(e.sf[lo], e.sf[hi], t);
}

export class WorldState {
  constructor() {
    this.ents = new Map();
    this.order = [];            // тот же набор, но массивом — рендер ходит по нему
    this.orderDirty = true;

    this.level = null;          // {floor, seed, w, h, tiles: Uint8Array}
    this.latestTick = -1;
    this.ack = 0;
    this.snaps = 0;
    this.bytes = 0;

    this.renderTick = null;
    this.lastNow = 0;

    // Туман (4.4, 5.2). Позиционный массив w*h, живёт МЕЖДУ снапшотами:
    // сервер шлёт только изменившиеся клетки, остальное клиент помнит сам.
    // fogVersion растёт при каждом изменении — по нему рендер понимает, что
    // маску пора перерисовать, и в остальных кадрах её не трогает вовсе.
    this.fog = null;
    this.fogVersion = 0;
    this.visMsgs = 0;
    // Выключатель «клиент игнорирует vis» — ровно то, чем клиент был до
    // тумана. Существует только чтобы tests/client_fog.py мог покраснеть.
    this.fogOn = true;

    // 6.1 можно выключить — этим tests/client_interp.py доказывает, что
    // проверка плавности умеет краснеть.
    this.interp = true;

    // --- вспышки от событий ev (5.2) ------------------------------------
    // Кольцо предвыделенных записей. Событие МОЖЕТ ПОТЕРЯТЬСЯ, и на нём
    // нельзя держать состояние (5.2): отсюда и срок жизни в миллисекундах, и
    // то, что ни одна проверка игры сюда не смотрит. Полоска здоровья, замах
    // и рывок живут в снапшоте, а здесь — только то, что нельзя восстановить.
    this.fx = new Array(FX_MAX);
    for (let i = 0; i < FX_MAX; i++) {
      this.fx[i] = { k: 0, x: 0, y: 0, dmg: 0, a: 0, b: 0, t0: -1e9, tick: 0 };
    }
    this.fxI = 0;
    this.fxOn = true;        // выключатель для проверки «умеет краснеть»
    this.evs = 0;            // сколько событий пришло за забег

    // Журнал событий для проверок: кольцо ЧИСЕЛ, без единого объекта в
    // горячем пути. tests/client_combat.py читает его, чтобы сказать, на
    // каком тике пришло hit.
    this.evK = new Int32Array(EVLOG);
    this.evTick = new Int32Array(EVLOG);
    this.evA = new Int32Array(EVLOG);
    this.evB = new Int32Array(EVLOG);
    this.evDmg = new Int32Array(EVLOG);
    this.evNow = new Float64Array(EVLOG);
    this.evI = 0;
    this.evN = 0;

    // своя сущность (см. selfId ниже)
    this.pid = 0;
    this.room = '';
    this.players = [];          // [[pid, name], ...] из joined
    this.selfId = 0;
    this._rankGuess = 0;
    this._score = new Map();    // id -> счёт корреляции ввода
    this._locked = false;
    this._youSeen = false;      // сервер прислал `you` — подпорки молчат
    this._sentMv = new Map();   // seq -> [dx, dy]

    this.view = { tick: 0, alpha: 0, level: null, ents: this.order,
                  self: null, selfId: 0, interp: true,
                  fog: null, fogVersion: 0,
                  fx: null, fxLife: FX_LIFE_MS, now: 0 };
  }

  reset() {
    this.ents.clear();
    this.order.length = 0;
    this.orderDirty = true;
    this.level = null;
    this.latestTick = -1;
    this.renderTick = null;
    this.selfId = 0;
    this._locked = false;
    this._youSeen = false;
    this._score.clear();
    this._sentMv.clear();
    this.fog = null;
    this.fogVersion++;
  }

  // --- вход данных -------------------------------------------------------

  applyJoined(m) {
    this.room = m.room;
    this.pid = m.pid;
    this.players = m.players || [];
    this._rankGuess = 0;
    for (let i = 0; i < this.players.length; i++) {
      if (this.players[i][0] === this.pid) { this._rankGuess = i; break; }
    }
    this._locked = false;
  }

  applyLevel(m, tiles) {
    // Новый этаж — мир строится заново: старые id к нему отношения не имеют.
    this.ents.clear();
    this.order.length = 0;
    this.orderDirty = true;
    this.latestTick = -1;
    this.renderTick = null;
    this.level = { floor: m.floor, seed: m.seed, w: m.w, h: m.h, tiles: tiles };
    // 4.4: при смене этажа память группы сбрасывается — новый этаж начинается
    // в полной темноте. Полный туман придёт следующим снапшотом (full:true).
    this.fog = new Uint8Array(m.w * m.h);
    this.fogVersion++;
  }

  // Запоминаем свой ввод: сервер эхом вернёт seq в поле ack, и по нему
  // можно понять, какая сущность послушалась именно нас.
  noteInput(seq, mv) {
    this._sentMv.set(seq, mv);
    if (this._sentMv.size > 90) {        // 3 с истории хватает с запасом
      const first = this._sentMv.keys().next().value;
      this._sentMv.delete(first);
    }
  }

  /**
   * Событие ev (5.2). ТОЛЬКО вспышка и журнал: ни одно поле мира отсюда не
   * меняется. Здоровье, смерть, замах и рывок берутся из снапшота — событие
   * может потеряться, и клиент, который держал бы на нём состояние, показал
   * бы игроку ложь ровно в тот момент, когда сеть просела.
   */
  pushEvent(m, nowMs) {
    const k = FX_KIND[m.k] || 0;
    if (!k) return 0;
    this.evs++;
    const now = nowMs === undefined ? performance.now() : nowMs;

    const j = this.evI;
    this.evK[j] = k;
    this.evTick[j] = m.tick | 0;
    this.evA[j] = m.a | 0;
    this.evB[j] = m.b | 0;
    this.evDmg[j] = m.dmg | 0;
    this.evNow[j] = now;
    this.evI = (j + 1) % EVLOG;
    if (this.evN < EVLOG) this.evN++;

    if (!this.fxOn) return k;
    // Координаты у события есть не всегда (5.2 не требует их от каждого
    // вида): без точки вспышку рисовать негде — берём того, с кем это
    // случилось, из мира. Если и его нет — вспышки просто не будет.
    let x = m.x, y = m.y;
    if (typeof x !== 'number' || typeof y !== 'number') {
      const e = this.ents.get(m.b) || this.ents.get(m.a);
      if (e === undefined) return k;
      x = e.dx; y = e.dy;
    }
    const f = this.fx[this.fxI];
    this.fxI = (this.fxI + 1) % FX_MAX;
    f.k = k; f.x = x; f.y = y; f.dmg = m.dmg | 0;
    f.a = m.a | 0; f.b = m.b | 0; f.t0 = now; f.tick = m.tick | 0;
    return k;
  }

  /** Последние события журналом (для проверок, не для игры). */
  evLog() {
    const out = [];
    const n = this.evN;
    const base = (this.evI - n + EVLOG * 2) % EVLOG;
    for (let i = 0; i < n; i++) {
      const j = (base + i) % EVLOG;
      out.push({ k: this.evK[j], tick: this.evTick[j], a: this.evA[j],
                 b: this.evB[j], dmg: this.evDmg[j], now: this.evNow[j] });
    }
    return out;
  }

  applySnap(m) {
    const rows = m.e || [];
    const tick = m.tick;

    if (m.full) {
      // Полный снапшот — истина в последней инстанции: чего в нём нет,
      // того в мире нет. Дельта так поступать НЕ имеет права (5.2).
      const seen = new Set();
      for (let i = 0; i < rows.length; i++) seen.add(rows[i][0]);
      for (const id of Array.from(this.ents.keys())) {
        if (!seen.has(id)) { this.ents.delete(id); this.orderDirty = true; }
      }
    }

    for (let i = 0; i < rows.length; i++) {
      const a = rows[i];
      const id = a[0];
      let e = this.ents.get(id);
      if (e === undefined) {
        e = makeEnt(id);
        this.ents.set(id, e);
        this.orderDirty = true;
      }
      e.kind = a[1];
      e.x = a[2]; e.y = a[3];
      e.vx = a[4]; e.vy = a[5];
      e.hp = a[6]; e.hpMax = a[7];
      e.flags = a[8]; e.facing = a[9];
      e.lastTick = tick;
      pushSample(e, tick, e.x, e.y, e.facing);
    }

    // Сущность пропадает ТОЛЬКО по rm (5.2): отсутствие в дельте означает
    // «не менялась», а не «исчезла».
    const rm = m.rm;
    if (rm && rm.length) {
      for (let i = 0; i < rm.length; i++) {
        if (this.ents.delete(rm[i])) this.orderDirty = true;
      }
      if (this.selfId && rm.indexOf(this.selfId) >= 0) {
        // Своя сущность исчезла. Сбрасываем и знание из `you`: если сервер
        // заведёт новую, он пришлёт `you` снова, а до тех пор пусть работают
        // подпорки — иначе selfId останется нулём навсегда.
        this.selfId = 0; this._locked = false; this._youSeen = false;
      }
    }

    // --- туман (5.2, поле vis) --------------------------------------------
    // Полный снапшот несёт весь массив, дельта — только изменившиеся клетки
    // (255 = «не менялась»). И то и другое накатывается на ОДИН И ТОТ ЖЕ
    // массив: память группы живёт у клиента между снапшотами.
    if (typeof m.vis === 'string' && this.fog) {
      this.visMsgs++;
      try {
        if (visApply(m.vis, this.fog)) this.fogVersion++;
      } catch (e) {
        // Битый vis — беда, но не повод ронять кадр: лучше старый туман.
      }
    }

    this.latestTick = tick;
    this.ack = m.ack | 0;
    this.snaps++;

    // --- своя сущность: 5.2, поле `you` -----------------------------------
    // Пришло — значит истина, и гадать больше не о чем. Подпорки в
    // _resolveSelf остаются запасным путём и включаются сами, если `you`
    // в снапшоте нет (старый сервер, дельта без поля).
    if (typeof m.you === 'number' && isFinite(m.you) && m.you > 0) {
      this.selfId = m.you;
      this._youSeen = true;
      this._locked = true;
    }
    this._resolveSelf();
  }

  // --- опознание своей сущности -----------------------------------------
  //
  // ЗАПАСНОЙ ПУТЬ. Штатный — поле `you` в снапшоте (5.2), оно читается в
  // applySnap и, когда пришло, выключает всё, что ниже (_youSeen). Всё
  // остальное здесь — подпорки на случай сервера, который `you` ещё не шлёт.
  //
  // ДЫРА В ПРОТОКОЛЕ (5.2): ни welcome, ни joined, ни snap не сообщают,
  // какой id сущности принадлежит этому игроку. pid != id сущности.
  // Тест этапа 0a обходил это, залезая в потроха сервера (r.players[pid]
  // .ent_id), браузеру так нельзя. Пока контракт не дополнен, опознаём
  // двумя способами:
  //
  //   1) по рангу: сервер заводит сущности игроков в том же порядке, в
  //      каком перечисляет их joined.players, а id растут монотонно
  //      (room.start и room.add идут по self.players.values()). Значит
  //      k-й игрок в списке = k-я по возрастанию id сущность kind=PLAYER.
  //      Для обычного пути это точное соответствие, а не догадка;
  //   2) по корреляции ввода: сервер вернул ack=seq, значит в этот тик
  //      к нашей сущности применён mv из этого seq. Сущность, чья
  //      скорость совпала по направлению с нашим mv, получает плюс,
  //      не совпавшая — минус. Перебивает ранг только при уверенном
  //      отрыве, поэтому два игрока, бегущие в одну сторону, ничего не
  //      портят: они просто не дают отрыва.
  _resolveSelf() {
    if (this._youSeen) return;      // сервер сказал прямо — подпорки не нужны
    const mine = [];
    for (const e of this.ents.values()) {
      if (e.kind === K_PLAYER) mine.push(e.id);
    }
    if (mine.length === 0) { this.selfId = 0; return; }
    mine.sort((a, b) => a - b);

    if (!this._locked) {
      const guess = mine[Math.min(this._rankGuess, mine.length - 1)];
      if (this.selfId === 0 || this.ents.get(this.selfId) === undefined) {
        this.selfId = guess;
      }
    }

    // корреляция
    const mv = this._sentMv.get(this.ack);
    if (!mv) return;
    const mlen = Math.hypot(mv[0], mv[1]);
    if (mlen < 0.5) return;                 // стоим — судить не по чему
    let best = 0, bestScore = -1e9, second = -1e9;
    for (let i = 0; i < mine.length; i++) {
      const e = this.ents.get(mine[i]);
      const vlen = Math.hypot(e.vx, e.vy);
      let s = this._score.get(e.id) || 0;
      if (vlen > SPEED_RUN * 0.4) {
        const dot = (e.vx * mv[0] + e.vy * mv[1]) / (vlen * mlen);
        s += dot > 0.9 ? 1 : -1;
      } else {
        s -= 1;                             // мы бежим, а он стоит — не мы
      }
      if (s > 40) s = 40; if (s < -40) s = -40;
      this._score.set(e.id, s);
      if (s > bestScore) { second = bestScore; bestScore = s; best = e.id; }
      else if (s > second) { second = s; }
    }
    if (bestScore >= 8 && bestScore - second >= 6) {
      this.selfId = best;
      this._locked = true;
    }
  }

  self() { return this.selfId ? this.ents.get(this.selfId) || null : null; }

  // --- часы рендера ------------------------------------------------------
  //
  // Цель — latestTick - 2 (6.1). Догоняем не прыжком, а темпом: иначе
  // каждый скачок цели виден как рывок персонажа. Темп ограничен ±10 %,
  // и именно из этого предела выводится порог плавности в проверке.
  _advanceClock(nowMs) {
    if (this.latestTick < 0) return;
    const target = this.latestTick - DELAY_TICKS;
    if (this.renderTick === null) {
      this.renderTick = target;
      this.lastNow = nowMs;
      return;
    }
    let dt = (nowMs - this.lastNow) / 1000;
    this.lastNow = nowMs;
    if (!(dt > 0)) dt = 0;
    if (dt > 0.25) dt = 0.25;               // вкладка была свёрнута

    const err = target - this.renderTick;
    if (err > HARD_RESYNC || err < -HARD_RESYNC) {
      this.renderTick = target;             // отстали/убежали слишком — прыжок
      return;
    }
    let rate = err * RATE_GAIN;
    if (rate > RATE_CLAMP) rate = RATE_CLAMP;
    if (rate < -RATE_CLAMP) rate = -RATE_CLAMP;
    this.renderTick += dt * TICK_HZ * (1 + rate);
    // Вперёд последнего снапшота не забегаем: это была бы экстраполяция.
    if (this.renderTick > this.latestTick) this.renderTick = this.latestTick;
  }

  _rebuildOrder() {
    this.order.length = 0;
    for (const e of this.ents.values()) this.order.push(e);
    this.orderDirty = false;
  }

  // Состояние для рендера на момент nowMs (DESIGN.md 7.1 draw(view, alpha)).
  buildView(nowMs) {
    this._advanceClock(nowMs);
    if (this.orderDirty) this._rebuildOrder();

    const rt = this.renderTick === null ? 0 : this.renderTick;
    if (this.interp) {
      for (let i = 0; i < this.order.length; i++) sampleAt(this.order[i], rt);
    } else {
      // Режим «как есть»: последний снапшот без интерполяции. Существует
      // только чтобы проверка плавности могла покраснеть.
      for (let i = 0; i < this.order.length; i++) {
        const e = this.order[i];
        e.dx = e.x; e.dy = e.y; e.dfacing = e.facing;
      }
    }

    const v = this.view;
    v.tick = rt;
    v.alpha = rt - Math.floor(rt);
    v.level = this.level;
    v.ents = this.order;
    v.selfId = this.selfId;
    v.self = this.self();
    v.interp = this.interp;
    // fog === null означает «тумана нет» — рендер тогда рисует ровно то же,
    // что рисовал до тумана. Это и путь для старого сервера без vis, и
    // красный прогон tests/client_fog.py.
    v.fog = this.fogOn ? this.fog : null;
    v.fogVersion = this.fogVersion;
    // Вспышки: рендер сам решает, какие из них ещё живы (по now - t0).
    // Чистить кольцо здесь незачем — просроченная запись стоит одного
    // сравнения, а сборка живого списка каждый кадр стоила бы мусора.
    v.fx = this.fxOn ? this.fx : null;
    v.now = nowMs;
    return v;
  }
}

// --- RLE тайлов (proto.rle_encode) ---------------------------------------
// base64 от пар (значение, длина<=255).
export function rleDecode(b64, expect) {
  const bin = atob(b64);
  const out = new Uint8Array(expect || 0);
  let n = 0;
  const grow = [];
  for (let i = 0; i + 1 < bin.length; i += 2) {
    const v = bin.charCodeAt(i), c = bin.charCodeAt(i + 1);
    if (expect) {
      for (let k = 0; k < c && n < out.length; k++) out[n++] = v;
    } else {
      for (let k = 0; k < c; k++) grow.push(v);
    }
  }
  return expect ? out : Uint8Array.from(grow);
}

// --- RLE тумана (5.2, server/vis.py) --------------------------------------
// Тот же формат пар (значение, длина<=255), но накатывается НА МЕСТЕ, на уже
// имеющийся массив: в дельте значение 255 (VIS_KEEP) означает «эти клетки не
// менялись», и их надо перешагнуть, а не обнулить. Полный туман приходит тем
// же кодом — в нём 255 просто не встречается.
//
// Возвращает true, если хоть одна клетка изменилась: по этому рендер решает,
// перерисовывать маску или нет. Дельта, в которой всё совпало, кадра не стоит.
export function visApply(b64, out) {
  const bin = atob(b64);
  const len = out.length;
  let n = 0;
  let changed = false;
  for (let i = 0; i + 1 < bin.length && n < len; i += 2) {
    const v = bin.charCodeAt(i);
    const c = bin.charCodeAt(i + 1);
    if (v === VIS_KEEP) { n += c; continue; }
    const end = n + c < len ? n + c : len;
    for (; n < end; n++) {
      if (out[n] !== v) { out[n] = v; changed = true; }
    }
  }
  return changed;
}
