// Клавиатура. Стрелки и WASD работают одновременно — в офисе кому-то удобнее
// так, кому-то эдак, и спрашивать об этом не нужно.

import { IN_UP, IN_DOWN, IN_LEFT, IN_RIGHT, IN_HANDBRAKE, IN_USE } from './game/carstep.js';

const MAP = {
  ArrowUp: IN_UP, KeyW: IN_UP,
  ArrowDown: IN_DOWN, KeyS: IN_DOWN,
  ArrowLeft: IN_LEFT, KeyA: IN_LEFT,
  ArrowRight: IN_RIGHT, KeyD: IN_RIGHT,
  ShiftLeft: IN_HANDBRAKE, ShiftRight: IN_HANDBRAKE,
  Space: IN_USE, ControlLeft: IN_USE,
};

export class Input {
  constructor() {
    this.mask = 0;
    this.enabled = false;
    this.onKey = null;
    this._down = new Set();

    addEventListener('keydown', (e) => this._key(e, true));
    addEventListener('keyup', (e) => this._key(e, false));
    // Потеря фокуса при зажатом газе оставила бы машину ехать в стену
    addEventListener('blur', () => { this.mask = 0; this._down.clear(); });
  }

  _key(e, down) {
    if (e.repeat) return;
    const bit = MAP[e.code];
    if (bit !== undefined && this.enabled) {
      // Стрелки и пробел иначе прокручивают страницу
      e.preventDefault();
      if (down) { this.mask |= bit; this._down.add(e.code); }
      else { this.mask &= ~bit; this._down.delete(e.code); }
      // Одна и та же роль на двух клавишах: бит снимаем, только когда
      // отпущены обе (иначе W+стрелка гасили бы друг друга).
      if (!down) {
        for (const code of this._down) {
          if (MAP[code] === bit) { this.mask |= bit; break; }
        }
      }
      return;
    }
    if (down && this.onKey) this.onKey(e);
  }

  read() { return this.mask; }
  clear() { this.mask = 0; this._down.clear(); }
}
