// Живая подстройка физики (F4), доступна хозяину лобби.
//
// Ощущение машины подбирается десятками итераций. Возить ради каждой правку
// на флешку на другой компьютер — неделя работы. Здесь ползунки меняют физику
// у ВСЕХ в заезде сразу, а подобранное скачивается готовым physics.json,
// который кладётся в репозиторий как новые значения по умолчанию.

import { $, el } from './ui.js';

const FIELDS = {
  car: [
    ['maxSpeed', 150, 900, 5, 'предельная скорость'],
    ['accel', 100, 1400, 10, 'разгон'],
    ['brake', 200, 2000, 10, 'тормоз'],
    ['grip', 0.5, 14, 0.1, 'сцепление (меньше — сильнее занос)'],
    ['handbrakeGrip', 0.1, 6, 0.05, 'сцепление на ручнике'],
    ['steerRate', 0.5, 7, 0.05, 'скорость поворота руля'],
    ['steerFullSpeed', 20, 300, 5, 'скорость, с которой руль работает в полную'],
    ['drag', 0.05, 3, 0.05, 'сопротивление'],
    ['reverseAccel', 50, 600, 10, 'задний ход'],
    ['handbrakeSteerBonus', 1, 3, 0.05, 'доворот на ручнике'],
  ],
  collision: [
    ['restitution', 0, 1.2, 0.02, 'упругость удара машин'],
    ['separation', 0.1, 1.5, 0.05, 'жёсткость расталкивания'],
    ['spinKick', 0, 10, 0.1, 'разворот от удара'],
    ['speedLoss', 0, 0.6, 0.01, 'потеря скорости при ударе'],
    ['wallRestitution', 0, 1, 0.02, 'отскок от стены'],
    ['wallSpeedLoss', 0, 1, 0.02, 'потеря скорости о стену'],
  ],
  respawn: [
    ['stuckSpeed', 5, 90, 1, 'порог «стоит на месте»'],
    ['stuckSeconds', 0.5, 8, 0.1, 'через сколько секунд вернуть на трассу'],
  ],
  camera: [
    ['worldHeight', 380, 1100, 10, 'высота обзора: меньше — ближе к машине'],
    ['lookAhead', 0, 0.8, 0.02, 'насколько камера смотрит вперёд по движению'],
    ['smooth', 2, 20, 0.5, 'плавность камеры'],
  ],
};

const TITLES = { car: 'машина', collision: 'столкновения',
                 respawn: 'возврат на трассу', camera: 'камера' };

export class Tuner {
  constructor(app) {
    this.app = app;
    this.on = false;
    this.node = $('tuner');
    this.body = $('tuner-body');
    this.base = null;
    $('tuner-close').onclick = () => this.hide();
    $('tuner-save').onclick = () => this.download();
    $('tuner-reset').onclick = () => this.reset();
  }

  get allowed() {
    return this.app.lobby && this.app.lobby.host === this.app.me.token;
  }

  toggle() { this.on ? this.hide() : this.show(); }
  hide() { this.on = false; this.node.classList.add('hidden'); }

  show() {
    if (!this.app.phys) return;
    if (!this.allowed) {
      this.app.toast('Физику крутит только хозяин лобби');
      return;
    }
    if (!this.base) this.base = JSON.parse(JSON.stringify(this.app.phys));
    this.on = true;
    this.node.classList.remove('hidden');
    this.build();
  }

  build() {
    this.body.textContent = '';
    this.rows = [];
    for (const [section, fields] of Object.entries(FIELDS)) {
      this.body.appendChild(el('div', 'tune-group', TITLES[section] || section));
      for (const [key, min, max, step, title] of fields) {
        const wrap = el('div', 'tune-row');
        const lab = el('label', null, key);
        lab.title = title;
        const range = document.createElement('input');
        range.type = 'range';
        range.min = min; range.max = max; range.step = step;
        range.value = this.app.phys[section][key];
        const val = el('span', 'val', (+range.value).toFixed(2));
        range.oninput = () => {
          val.textContent = (+range.value).toFixed(2);
          this.app.phys[section][key] = +range.value;
          this.app.net.send({ t: 'tune', section, key, value: +range.value });
        };
        wrap.appendChild(lab);
        wrap.appendChild(val);
        this.body.appendChild(wrap);
        this.body.appendChild(range);
        this.rows.push({ section, key, range, val });
      }
    }
  }

  /** Правка от сервера (её мог сделать и другой хозяин) */
  applyRemote(section, key, value) {
    if (!this.app.phys[section]) return;
    this.app.phys[section][key] = value;
    if (!this.rows) return;
    for (const r of this.rows) {
      if (r.section === section && r.key === key) {
        r.range.value = value;
        r.val.textContent = (+value).toFixed(2);
      }
    }
  }

  reset() {
    if (!this.base) return;
    for (const [section, fields] of Object.entries(FIELDS)) {
      for (const [key] of fields) {
        const v = this.base[section][key];
        this.app.phys[section][key] = v;
        this.app.net.send({ t: 'tune', section, key, value: v });
      }
    }
    this.build();
  }

  download() {
    const blob = new Blob([JSON.stringify(this.app.phys, null, 2)],
                          { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'physics.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    this.app.toast('Файл сохранён — положите его в shared/physics.json');
  }
}
