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
};

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
    this._recalcBtn();
  }

  _recalcBtn() {
    let b = 0;
    for (const code of this.down) {
      const bit = BTN_KEYS[code];
      if (bit) b |= bit;
    }
    this.btn = b;
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
    const bit = e.button === 0 ? BTN_ATTACK : (e.button === 2 ? BTN_SHOOT : 0);
    if (!bit) return;
    if (isDown) { this.mouseBtn |= bit; e.preventDefault(); }
    else this.mouseBtn &= ~bit;
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
    this.sent++;
    return { mv: [dx, dy], aim: aim, btn: this.btn | this.mouseBtn };
  }

  moving() {
    for (const code of this.down) {
      if (MOVE_KEYS[code] || MOVE_KEYS_FALLBACK[code]) return true;
    }
    return false;
  }
}
