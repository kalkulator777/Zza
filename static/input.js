// -*- coding: utf-8 -*-
// Клавиатура, мышь и сборка сообщения input (DESIGN.md 5.1).
//
// Важное свойство: здесь НЕТ отправки. Модуль только держит текущее
// состояние органов управления, а net.js опрашивает его ровно 30 раз в
// секунду. Поэтому зажатая клавиша (браузер сыплет автоповтором keydown
// с частотой ~30-60 Гц, а на некоторых раскладках и чаще) не превращается
// в поток сообщений: повторы просто перезаписывают один и тот же бит.

// btn: битовая маска (5.1). Дальний бой получил СВОЙ бит 8, предмет уехал
// на 16 — до этапа 2a бита под стрельбу в контракте не было вовсе, и снаряд
// висел на бите предмета. Значения берутся из 5.1, а не из того, что было в
// коде раньше.
export const BTN_ATTACK = 1;    // ближняя атака
export const BTN_DASH = 2;      // рывок
export const BTN_USE = 4;       // действие
export const BTN_SHOOT = 8;     // дальняя атака
export const BTN_ITEM = 16;     // предмет
// 8.8: пинги на карте. Точка пинга — это ПРИЦЕЛ, который и так едет в том же
// сообщении (5.1): отдельного сообщения нет, есть ещё две кнопки.
export const BTN_PING = 32;         // «внимание, сюда»
export const BTN_PING_DANGER = 64;  // «опасность»
export const BTN_PING_ANY = BTN_PING | BTN_PING_DANGER;

const MOVE_KEYS = {
  KeyW: [0, -1], ArrowUp: [0, -1],
  KeyS: [0, 1], ArrowDown: [0, 1],
  KeyA: [-1, 0], ArrowLeft: [-1, 0],
  KeyD: [1, 0], ArrowRight: [1, 0],
};

// Русская раскладка: код клавиши (event.code) от раскладки не зависит,
// поэтому ЦФЫВ работают сами собой. Но если браузер вдруг не дал code,
// падаем на key.
const MOVE_KEYS_FALLBACK = {
  w: [0, -1], s: [0, 1], a: [-1, 0], d: [1, 0],
  ц: [0, -1], ы: [0, 1], ф: [-1, 0], в: [1, 0],
};

// Клавиатурные дубли боевых кнопок есть намеренно. Мышь — основной путь
// (ЛКМ ближняя, ПКМ дальняя), но кнопка, которую нельзя нажать с клавиатуры,
// не проверяется автоматически: playwright жмёт клавиши по коду, а мышь — по
// пикселям, и пиксели зависят от того, куда уехала камера.
const BTN_KEYS = {
  Space: BTN_DASH, ShiftLeft: BTN_DASH, ShiftRight: BTN_DASH,
  KeyE: BTN_USE, KeyQ: BTN_ITEM,
  KeyF: BTN_ATTACK, KeyR: BTN_SHOOT,
  // 8.8: пинг. ЧЕМ ЗАНЯТЫ РУКИ В БОЮ — левая держит WASD, правая держит
  // мышь (прицел, ЛКМ удар, ПКМ выстрел). Значит клавиша пинга обязана
  // жаться ЛЕВОЙ рукой, не отпуская WASD: V — один ход указательного вниз,
  // C — средним. Правая рука получает СРЕДНЮЮ КНОПКУ мыши: она в той же
  // руке, что и прицел, то есть пинг ставится без единого движения пальцев
  // с боевых органов. Ни V, ни C, ни колесо не заняты ничем (WASD/стрелки,
  // Shift/пробел, E, Q, F, R, ЛКМ, ПКМ).
  // Раскладка не мешает: event.code от неё не зависит, ЦФЫВ работают тем же
  // кодом, и «М»/«С» на ЙЦУКЕН — это KeyV/KeyC.
  KeyV: BTN_PING, KeyC: BTN_PING_DANGER,
};

// Кнопки-ОДИНОЧКИ: бит живёт ровно один опрос sample(), сколько бы кадров
// клавишу ни держали. Пинг — это событие, а не состояние: зажатая V иначе
// слала бы пинг каждый тик (30 в секунду), и сервер резал бы 28 из 30
// впустую. Удар и рывок так делать НЕЛЬЗЯ — там зажатая кнопка означает
// «бей, пока держу», и это разные вещи.
const ONESHOT = BTN_PING | BTN_PING_DANGER;

// СКОЛЬКО ОПРОСОВ ЖИВЁТ ОДИНОЧКА. Не один, и вот почему.
//
// btn на проводе — это СОСТОЯНИЕ, а не событие: 5.1 прямо разрешает
// серверу применить только ПОСЛЕДНИЙ ввод за тик. Клиент шлёт 30 Гц и
// сервер тикает 30 Гц, но часы у них разные, и время от времени два опроса
// попадают в один тик — тогда первый не применяется вовсе. Для зажатой
// кнопки это ничего не значит (следующий опрос принесёт то же самое), а
// одиночный пинг от этого ПРОПАДАЕТ. Замерено на живых прогонах приёмки:
// из десятка пингов терялся один-два, и приёмка краснела на исправной
// игре — ровно тот случай, ради которого 8.8 и просил думать о потере.
//
// Лечение: бит держится PULSE_SAMPLES опросов подряд, а сервер склеивает
// их обратно в ОДИН пинг своим ограничителем (room.PING_GAP = 15 тиков,
// то есть в тридцать раз длиннее удержания). Двух опросов хватает по
// построению: «два опроса в одном тике» означает, что в СЛЕДУЮЩЕМ тике
// опросов ноль, — то есть подряд потеряться они не могут. Берём три,
// запас в полтора раза. Цена — два лишних отвергнутых пинга на сервере
// (он их считает) и 100 мс, за которые прицел пинга не меняется.
const PULSE_SAMPLES = 3;

// Клавиши, у которых поведение браузера по умолчанию мешает игре
// (стрелки и пробел прокручивают страницу).
const SWALLOW = new Set(['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Space']);

export class Input {
  /**
   * @param {HTMLCanvasElement} canvas — по нему считается прицел
   * @param {function} screenToWorld — (sx, sy) -> [wx, wy]
   */
  constructor(canvas, screenToWorld) {
    this.canvas = canvas;
    this.screenToWorld = screenToWorld;
    this.enabled = false;

    this.down = new Set();        // коды нажатых клавиш
    this.btn = 0;                 // маска кнопок из клавиш
    this.mouseBtn = 0;            // маска кнопок из мыши
    this.pulse = 0;               // кнопки-одиночки: держатся PULSE_SAMPLES
    this.pulseLeft = 0;
    this.aimOnce = null;          // прицел пинга, замороженный на удержание
    this.pings = 0;               // сколько пингов нажато за забег (отладка)
    this.aim = [0, 0];            // мировые координаты прицела
    this.haveAim = false;
    this.mouseX = 0;
    this.mouseY = 0;
    this.sent = 0;                // сколько раз опросили (для отладки)

    this._onKeyDown = (e) => this._key(e, true);
    this._onKeyUp = (e) => this._key(e, false);
    this._onMouseMove = (e) => this._move(e);
    this._onMouseDown = (e) => this._mouse(e, true);
    this._onMouseUp = (e) => this._mouse(e, false);
    this._onBlur = () => this.clear();
    this._onContext = (e) => e.preventDefault();
  }

  attach() {
    window.addEventListener('keydown', this._onKeyDown);
    window.addEventListener('keyup', this._onKeyUp);
    window.addEventListener('blur', this._onBlur);
    this.canvas.addEventListener('mousemove', this._onMouseMove);
    this.canvas.addEventListener('mousedown', this._onMouseDown);
    window.addEventListener('mouseup', this._onMouseUp);
    this.canvas.addEventListener('contextmenu', this._onContext);
    return this;
  }

  detach() {
    window.removeEventListener('keydown', this._onKeyDown);
    window.removeEventListener('keyup', this._onKeyUp);
    window.removeEventListener('blur', this._onBlur);
    this.canvas.removeEventListener('mousemove', this._onMouseMove);
    this.canvas.removeEventListener('mousedown', this._onMouseDown);
    window.removeEventListener('mouseup', this._onMouseUp);
    this.canvas.removeEventListener('contextmenu', this._onContext);
  }

  clear() {
    this.down.clear();
    this.btn = 0;
    this.mouseBtn = 0;
    // pulse НЕ чистится: нажатие уже случилось, и потеря фокуса не отменяет
    // сказанного. Он уйдёт ближайшим опросом и сам себя погасит.
  }

  _target(e) {
    // Пока человек печатает имя или код комнаты, WASD — это буквы,
    // а не движение.
    const el = e.target;
    return el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA');
  }

  _key(e, isDown) {
    if (!this.enabled || this._target(e)) return;
    const code = e.code;
    const known = (code && (MOVE_KEYS[code] || BTN_KEYS[code])) ||
                  MOVE_KEYS_FALLBACK[(e.key || '').toLowerCase()];
    if (!known) return;
    if (SWALLOW.has(code)) e.preventDefault();
    // Автоповтор зажатой клавиши: состояние уже такое, ничего не делаем.
    if (isDown && e.repeat) return;
    if (isDown) this.down.add(code || e.key.toLowerCase());
    else this.down.delete(code || e.key.toLowerCase());
    // Кнопка-одиночка взводится на НАЖАТИИ и живёт PULSE_SAMPLES опросов.
    const bit = code ? BTN_KEYS[code] : 0;
    if (isDown && (bit & ONESHOT)) this._oneshot(bit, null);
    this._recalcBtn();
  }

  _recalcBtn() {
    let b = 0;
    for (const code of this.down) {
      const bit = BTN_KEYS[code];
      if (bit) b |= bit;
    }
    // Зажатая клавиша пинга не должна слать пинг каждый тик: одиночки едут
    // ТОЛЬКО через pulse, из состояния «что зажато» они вычеркнуты.
    this.btn = b & ~ONESHOT;
  }

  _move(e) {
    if (!this.enabled) return;
    const r = this.canvas.getBoundingClientRect();
    this.mouseX = e.clientX - r.left;
    this.mouseY = e.clientY - r.top;
    this.haveAim = true;
  }

  _mouse(e, isDown) {
    if (!this.enabled) return;
    if (isDown && e.target !== this.canvas) return;
    // ПКМ — дальняя атака (бит 8), а не «действие»: действие висит на E,
    // и отдавать правую кнопку мыши под него в игре про бой — расточительство.
    // Средняя кнопка (колесо) — пинг «сюда» (8.8): она в той же руке, что и
    // прицел, то есть в бою пинг не стоит ни одного движения пальцев с
    // боевых органов. preventDefault обязателен: по умолчанию средняя
    // кнопка включает автопрокрутку страницы.
    const bit = e.button === 0 ? BTN_ATTACK
              : (e.button === 2 ? BTN_SHOOT
              : (e.button === 1 ? BTN_PING : 0));
    if (!bit) return;
    if (bit & ONESHOT) {
      if (isDown) { this._oneshot(bit, null); e.preventDefault(); }
      return;
    }
    if (isDown) { this.mouseBtn |= bit; e.preventDefault(); }
    else this.mouseBtn &= ~bit;
  }

  /**
   * Пинг в точку МИРА, а не под курсором (8.8).
   *
   * Зачем это есть отдельно. Мышью можно ткнуть только в то, что на
   * экране, а пинг за кадром поставить надо (стрелка к нему — половина
   * смысла 8.8, и проверка обязана уметь его поставить). Сюда же придёт
   * пинг с мини-карты, когда она появится. ГЛАВНОЕ: пинг уезжает
   * ОБЫЧНЫМ опросом 30 Гц, а не своим сообщением. Отдельное сообщение
   * поверх опроса теряется: сервер применяет ПОСЛЕДНИЙ ввод за тик (5.1),
   * и очередной опрос затирал бы его в том же тике — замерено, пропадал
   * примерно каждый второй пинг.
   */
  pingAtWorld(wx, wy, danger) {
    this._oneshot(danger ? BTN_PING_DANGER : BTN_PING, [wx, wy]);
    return true;
  }

  /**
   * Взвести кнопку-одиночку. Прицел ЗАМОРАЖИВАЕТСЯ на всё удержание:
   * бит едет три опроса подряд, и если прицел за эти 100 мс уедет вместе с
   * мышью, сервер поставит пинг не туда, куда человек смотрел, когда жал.
   */
  _oneshot(bit, world) {
    this.pulse |= bit;
    this.pulseLeft = PULSE_SAMPLES;
    this.pings++;
    if (world) {
      this.aimOnce = world;
    } else if (this.haveAim && this.screenToWorld) {
      this.aimOnce = this.screenToWorld(this.mouseX, this.mouseY) || null;
    } else {
      this.aimOnce = this.aim ? this.aim.slice() : null;
    }
  }

  /** Текущее состояние в форме поля input (5.1). Вызывается 30 раз/с. */
  sample() {
    let dx = 0, dy = 0;
    for (const code of this.down) {
      const v = MOVE_KEYS[code] || MOVE_KEYS_FALLBACK[code];
      if (v) { dx += v[0]; dy += v[1]; }
    }
    // 5.1: длина <= 1. Сервер всё равно нормирует (клиенту не верят), но
    // слать по диагонали 1.41 — значит просить сервер резать нам скорость.
    const len = Math.hypot(dx, dy);
    if (len > 1) { dx /= len; dy /= len; }

    let aim = this.aim;
    if (this.haveAim && this.screenToWorld) {
      const w = this.screenToWorld(this.mouseX, this.mouseY);
      if (w) { aim = w; this.aim = w; }
    }
    // Пока одиночка держится, прицел берётся замороженный, а не с мыши.
    if (this.pulseLeft > 0 && this.aimOnce) aim = this.aimOnce;
    this.sent++;
    let pulse = 0;
    if (this.pulseLeft > 0) {
      pulse = this.pulse;
      this.pulseLeft--;
      if (this.pulseLeft === 0) { this.pulse = 0; this.aimOnce = null; }
    }
    return { mv: [dx, dy], aim: aim, btn: this.btn | this.mouseBtn | pulse };
  }

  moving() {
    for (const code of this.down) {
      if (MOVE_KEYS[code] || MOVE_KEYS_FALLBACK[code]) return true;
    }
    return false;
  }
}
