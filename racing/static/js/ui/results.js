/*
 * Экран итогов гонки.
 *
 * Работает по схеме раздела 9 контракта:
 *   results {rows:[{slot,name,car,color,place,time,best_lap,dnf}]}
 *
 * Комната стоит в состоянии RESULTS пятнадцать секунд и сама возвращается
 * в лобби (раздел 9), поэтому экран показывает обратный отсчёт до возврата.
 * Отсчёт чисто визуальный: команду о переходе даёт сервер новым событием
 * `room`, ждать таймера клиента никто не обязан.
 *
 * Использование из main.js:
 *
 *   import { ResultsScreen } from './ui/results.js';
 *
 *   const results = new ResultsScreen(document.getElementById('screen-results'), {
 *       onReturn: () => { ... },                 // нажали «В лобби»
 *   });
 *   results.applyWelcome(welcomeMsg);            // чтобы знать названия машин
 *   results.applyResults(msg, mySlot);           // на событие results
 *   results.show();
 *   results.hide();                              // на возврат в лобби
 */

import { showToast } from './menu.js';

/** Секунд до автоматического возврата в лобби (раздел 9). */
export const RESULTS_SECONDS = 15;

function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
}

function pad2(n) { return n < 10 ? '0' + n : String(n); }

/** «1:42.77» или «42.77», если меньше минуты. */
function formatTime(seconds) {
    if (!(seconds > 0)) return '—';
    const hundredths = Math.round(seconds * 100);
    const cs = hundredths % 100;
    const totalSec = (hundredths - cs) / 100;
    const s = totalSec % 60;
    const m = (totalSec - s) / 60;
    if (m > 0) return m + ':' + pad2(s) + '.' + pad2(cs);
    return s + '.' + pad2(cs);
}

const MAX_ROWS = 8;

export class ResultsScreen {
    /**
     * @param {HTMLElement} root корень экрана (#screen-results)
     * @param {object} handlers {onReturn} — необязательный
     */
    constructor(root, handlers) {
        this.root = root;
        this.handlers = handlers || {};
        this.cars = [];
        this.localSlot = -1;
        this.timer = 0;
        this.secondsLeft = RESULTS_SECONDS;

        this._build();
        this.hide();
    }

    show() { this.root.hidden = false; this.visible = true; }

    hide() {
        this.root.hidden = true;
        this.visible = false;
        this.stopCountdown();
    }

    setError(message) { showToast(message || 'Ошибка', 'error'); }

    /** Событие `welcome`: нужны названия машин по их id. */
    applyWelcome(msg) {
        this.cars = (msg.content && msg.content.cars) || [];
    }

    /**
     * Событие `results`. Строки уже отсортированы сервером по местам,
     * но на всякий случай сортируем сами: DNF всегда в конце.
     */
    applyResults(msg, localSlot) {
        if (localSlot !== undefined && localSlot !== null) this.localSlot = localSlot | 0;

        const rows = (msg.rows || []).slice();
        rows.sort((a, b) => {
            const adnf = a.dnf ? 1 : 0;
            const bdnf = b.dnf ? 1 : 0;
            if (adnf !== bdnf) return adnf - bdnf;
            return (a.place | 0) - (b.place | 0);
        });

        const winner = rows.length && !rows[0].dnf ? rows[0] : null;
        this.subtitle.textContent = winner
            ? 'Победитель — ' + (winner.name || 'Гонщик')
            : 'Гонка окончена';

        for (let i = 0; i < MAX_ROWS; i++) {
            const ui = this.rows[i];
            const row = rows[i];
            if (!row) { ui.node.hidden = true; continue; }
            ui.node.hidden = false;

            const place = row.place | 0;
            ui.node.className = 'res-row'
                + (row.slot === this.localSlot ? ' is-me' : '')
                + (row.dnf ? ' dnf' : '');
            ui.place.textContent = row.dnf ? '—' : String(place);
            ui.place.className = 'res-place'
                + (!row.dnf && place >= 1 && place <= 3 ? ' m' + place : '');
            ui.color.style.background = row.color || '#8899bb';
            ui.name.textContent = row.name || 'Гонщик';
            ui.car.textContent = this._carName(row.car);
            ui.time.textContent = row.dnf ? 'сошёл' : formatTime(row.time);
            ui.best.textContent = formatTime(row.best_lap);
        }

        this.startCountdown(RESULTS_SECONDS);
    }

    /** Обратный отсчёт до возврата в лобби. */
    startCountdown(seconds) {
        this.stopCountdown();
        this.secondsLeft = seconds | 0;
        this.countdownValue.textContent = String(this.secondsLeft);
        this.timer = setInterval(() => {
            this.secondsLeft--;
            if (this.secondsLeft <= 0) {
                this.secondsLeft = 0;
                this.stopCountdown();
            }
            this.countdownValue.textContent = String(this.secondsLeft);
        }, 1000);
    }

    stopCountdown() {
        if (this.timer) { clearInterval(this.timer); this.timer = 0; }
    }

    _carName(carId) {
        for (let i = 0; i < this.cars.length; i++) {
            if (this.cars[i].id === carId) return this.cars[i].name || carId;
        }
        return carId || '—';
    }

    _build() {
        const root = this.root;
        root.innerHTML = '';
        const wrap = el('div', 'results-root');
        root.appendChild(wrap);

        const panel = el('div', 'panel results-panel');
        wrap.appendChild(panel);

        const title = el('div', 'results-title');
        const flag = el('span');
        flag.innerHTML = '<svg viewBox="0 0 32 32" width="34" height="34" fill="none" '
            + 'stroke="#080a12" stroke-width="3" stroke-linejoin="round" stroke-linecap="round">'
            + '<path d="M7 29V4" />'
            + '<path d="M7 5h20l-5 6 5 6H7z" fill="#eef2ff"/>'
            + '<path d="M7 5h5v6h5v6h-5v-6H7zM22 5h5v6h-5zM17 11h5v6h-5z" fill="#080a12" stroke="none"/>'
            + '</svg>';
        title.appendChild(flag.firstChild);
        const titleText = el('div');
        titleText.appendChild(el('h2', null, 'Итоги гонки'));
        this.subtitle = el('div', 'dim', '');
        this.subtitle.style.fontWeight = '700';
        this.subtitle.style.fontSize = '14px';
        titleText.appendChild(this.subtitle);
        title.appendChild(titleText);
        panel.appendChild(title);

        const body = el('div', 'results-rows scroll');

        // Шапка таблицы
        const head = el('div', 'res-row');
        head.style.background = 'transparent';
        head.style.border = '0';
        head.style.boxShadow = 'none';
        head.style.padding = '0 14px';
        head.appendChild(el('div', 'res-col-head', 'Место'));
        head.appendChild(el('div'));
        head.appendChild(el('div', 'res-col-head', 'Игрок'));
        head.appendChild(el('div', 'res-col-head', 'Машина'));
        const thTime = el('div', 'res-col-head', 'Время');
        thTime.style.textAlign = 'right';
        head.appendChild(thTime);
        const thBest = el('div', 'res-col-head', 'Лучший круг');
        thBest.style.textAlign = 'right';
        head.appendChild(thBest);
        body.appendChild(head);

        this.rows = [];
        for (let i = 0; i < MAX_ROWS; i++) {
            const row = el('div', 'res-row');
            const place = el('div', 'res-place', '—');
            const color = el('div', 'res-color');
            const name = el('div', 'res-name', '');
            const car = el('div', 'res-car', '');
            const time = el('div', 'res-time num', '—');
            const best = el('div', 'res-best num', '—');
            row.appendChild(place);
            row.appendChild(color);
            row.appendChild(name);
            row.appendChild(car);
            row.appendChild(time);
            row.appendChild(best);
            row.hidden = true;
            body.appendChild(row);
            this.rows.push({
                node: row, place: place, color: color,
                name: name, car: car, time: time, best: best,
            });
        }
        panel.appendChild(body);

        const foot = el('div', 'results-foot');
        const countdown = el('div', 'res-countdown');
        this.countdownValue = el('div', 'n', String(RESULTS_SECONDS));
        countdown.appendChild(this.countdownValue);
        countdown.appendChild(el('span', null, 'секунд до возврата в лобби'));
        foot.appendChild(countdown);

        this.returnButton = el('button', 'btn', 'В лобби');
        this.returnButton.type = 'button';
        this.returnButton.addEventListener('click', () => {
            if (this.handlers.onReturn) this.handlers.onReturn();
        });
        foot.appendChild(this.returnButton);
        panel.appendChild(foot);
    }
}
