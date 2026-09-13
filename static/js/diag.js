// Оверлей диагностики (F3).
//
// Замерить игру на боевых машинах заранее не получится — у них нет интернета,
// и каждая проверка это поездка с флешкой. Поэтому вся нужная телеметрия
// выводится прямо на экран: одна фотография этого оверлея на телефон
// заменяет замерочный прогон.

import { $ } from './ui.js';

export class Diag {
  constructor() {
    this.on = false;
    this.node = $('diag');
    this.frames = 0;
    this.fps = 0;
    this._acc = 0;
    this._t = performance.now();
    this.longFrames = 0;
    this.worstFrame = 0;
  }

  toggle() {
    this.on = !this.on;
    this.node.classList.toggle('hidden', !this.on);
  }

  tickFrame(dtMs) {
    this.frames++;
    this._acc += dtMs;
    if (dtMs > 20) this.longFrames++;
    if (dtMs > this.worstFrame) this.worstFrame = dtMs;
    const now = performance.now();
    if (now - this._t >= 500) {
      this.fps = this.frames * 1000 / (now - this._t);
      this.frames = 0;
      this._t = now;
    }
  }

  reset() {
    this.longFrames = 0;
    this.worstFrame = 0;
  }

  render(app) {
    if (!this.on) return;
    const w = app.world, r = app.renderer, n = app.net;
    const L = [];
    const warn = (v, lim) => v > lim ? `<span class="warn">${v}</span>` : v;

    L.push(`<b>кадр</b>`);
    L.push(`  fps           ${this.fps.toFixed(0)}   <- главный показатель`);
    L.push(`  длинных >20мс ${warn(this.longFrames, 0)}   худший ${this.worstFrame.toFixed(1)} мс`);
    // Команды рисования выполняются асинхронно, поэтому эта цифра показывает
    // время НА ВЫДАЧУ команд, а не на саму отрисовку. Если она мала, а fps
    // низкий — упирается не наш код, а видеоподсистема браузера.
    L.push(`  выдача команд ${r ? r.frameMs.toFixed(2) : '-'} мс  p95 ${r ? r.p95.toFixed(2) : '-'}`);
    L.push(`  качество      ${r ? r.quality : '-'} ${r && r.quality < 2 ? '(снижено автоматически)' : ''}`);
    L.push(`  холст         ${r ? r.cv.width + 'x' + r.cv.height : '-'}  масштаб ${r ? r.scale.toFixed(2) : '-'}`);
    if (r) L.push(`  частиц        ${r.parts.length}`);

    L.push(`<b>сеть</b>`);
    L.push(`  задержка      ${n.rtt.toFixed(1)} мс`);
    L.push(`  соединение    ${n.open ? 'есть' : 'НЕТ'}${n.tries ? '  попыток ' + n.tries : ''}`);

    if (w) {
      L.push(`<b>синхронизация</b>`);
      L.push(`  тик клиента   ${w.tick}`);
      L.push(`  тик сервера   ${w.serverTick}   опережение ${w.tick - w.serverTick}`);
      L.push(`  запас ввода   ${w.slack} ${w.slack < 0 ? '<span class="bad">(опаздывает)</span>' : ''}`);
      L.push(`  снапшотов     ${w.stat.snaps}`);
      L.push(`<b>предсказание</b>`);
      L.push(`  поправок      ${w.stat.corr} из ${w.stat.snaps}`);
      L.push(`  обычная       ${w.corrPercentile(0.5).toFixed(2)} px  (половина поправок меньше)`);
      L.push(`  наибольшая    ${w.stat.maxCorrPx.toFixed(1)} px  (обычно это возврат на трассу)`);
      L.push(`  пересборок    ${warn(w.stat.resets, 1)}`);
      const off = Math.hypot(w.offX, w.offY);
      L.push(`  сглаживание   ${off.toFixed(2)} px`);
    }
    L.push(``);
    L.push(`F3 скрыть · F4 физика · сфотографируйте этот`);
    L.push(`экран, если что-то идёт не так`);

    this.node.innerHTML = L.join('\n');
  }
}
