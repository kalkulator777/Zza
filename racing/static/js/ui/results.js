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
 * ЧЕМПИОНАТ. Если комната ведёт серию, вместе с итогами приезжает поле
 * `results.championship` (то же, что в событии `room`): календарь этапов
 * и общий зачёт. Таблица чемпионата ложится поверх итогов гонки — очки
 * за этап, сумма и изменение позиции, — а после последнего этапа экран
 * превращается в финальный: чемпион отдельной плашкой.
 *
 * ПОВТОР ФИНИША. Пока идёт повтор (его крутит main.js прямо в рендере),
 * панель итогов прячется целиком, чтобы не закрывать сцену, а сверху
 * висит узкая плашка с кнопкой «Пропустить».
 *
 * Использование из main.js:
 *
 *   import { ResultsScreen } from './ui/results.js';
 *
 *   const results = new ResultsScreen(document.getElementById('screen-results'), {
 *       onReturn: () => { ... },                 // нажали «В лобби»
 *       onSkipReplay: () => { ... },             // нажали «Пропустить»
 *   });
 *   results.applyWelcome(welcomeMsg);            // чтобы знать названия машин
 *   results.applyResults(msg, mySlot);           // на событие results
 *   results.beginReplay(3);                      // пошёл повтор финиша
 *   results.endReplay();                         // повтор кончился
 *   results.show();
 *   results.hide();                              // на возврат в лобби
 */

import { showToast } from './menu.js';

/** Секунд до автоматического возврата в лобби (раздел 9). */
export const RESULTS_SECONDS = 15;

/** Финальная таблица чемпионата держится дольше (server/config.py). */
export const FINAL_SECONDS = 25;

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

// Зачёт чемпионата шире гонки: за серию через комнату могут пройти
// больше восьми имён (кто-то ушёл, кто-то пришёл), и очки остаются у всех.
const CHAMP_ROWS = 14;

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
        this.countdownSeconds = RESULTS_SECONDS;
        this.replayActive = false;
        this.resultsAt = 0;

        this._build();
        this.hide();
    }

    show() { this.root.hidden = false; this.visible = true; }

    hide() {
        this.root.hidden = true;
        this.visible = false;
        this.stopCountdown();
        this.endReplay();
    }

    // --- повтор финиша ------------------------------------------------------

    /**
     * Пошёл повтор: панель итогов уходит, сцену видно целиком.
     * Отсчёт до возврата в лобби на это время останавливается — иначе он
     * съест три секунды из пятнадцати, отведённых на чтение таблицы.
     */
    beginReplay(seconds) {
        this.replayActive = true;
        this.stopCountdown();
        this.panel.hidden = true;
        this.wrap.classList.add('is-replay');
        this.replayBar.hidden = false;
        this.replayLabel.textContent = 'Повтор финиша'
            + (seconds > 0 ? ' · последние ' + seconds + ' с' : '');
    }

    /** Повтор кончился (или его пропустили): показать таблицу. */
    endReplay() {
        this.replayBar.hidden = true;
        this.wrap.classList.remove('is-replay');
        this.panel.hidden = false;
        if (this.replayActive) {
            this.replayActive = false;
            // Сервер отсчитывает свои пятнадцать секунд от события results,
            // а не от конца повтора: показываем остаток, иначе цифра соврёт.
            const spent = (Date.now() - this.resultsAt) / 1000;
            const left = Math.max(1, Math.round(this.countdownSeconds - spent));
            if (this.visible) this.startCountdown(left);
        }
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
        this.resultsAt = Date.now();

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
        this._applyChampionship(msg.championship);

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

        if (!this.replayActive) this.startCountdown(this.countdownSeconds);
    }

    // --- чемпионат ----------------------------------------------------------

    /**
     * Поле `championship` события results. Его нет — блок прячется целиком,
     * и экран выглядит ровно так же, как до доработки.
     */
    _applyChampionship(champ) {
        const on = !!(champ && champ.active && champ.table);
        this.champBlock.hidden = !on;
        this.championBlock.hidden = true;
        if (!on) {
            this.countdownSeconds = RESULTS_SECONDS;
            this.titleText.textContent = 'Итоги гонки';
            return;
        }

        const final = champ.phase === 'final';
        this.countdownSeconds = final ? FINAL_SECONDS : RESULTS_SECONDS;
        // На экране итогов речь о ТОЛЬКО ЧТО отъезженном этапе, а champ.stage —
        // это уже следующий: сюда идёт stage_done.
        this.titleText.textContent = final
            ? 'Чемпионат завершён'
            : 'Итоги этапа ' + champ.stage_done + ' из ' + champ.stages;

        // Календарь: пройденные этапы гаснут, текущий подсвечен.
        this.champCalendar.innerHTML = '';
        const calendar = champ.calendar || [];
        for (let i = 0; i < calendar.length; i++) {
            const stage = calendar[i];
            const chip = el('div', 'champ-chip'
                + (stage.done ? ' done' : '') + (stage.current ? ' current' : ''));
            chip.appendChild(el('span', 'n', String(i + 1)));
            chip.appendChild(el('span', 't', stage.name || stage.track));
            this.champCalendar.appendChild(chip);
        }
        this.champTitle.textContent = final
            ? 'Общий зачёт · итог'
            : 'Общий зачёт после ' + champ.stage_done + ' из ' + champ.stages;

        const table = champ.table || [];
        for (let i = 0; i < CHAMP_ROWS; i++) {
            const ui = this.champRows[i];
            const row = table[i];
            if (!row) { ui.node.hidden = true; continue; }
            ui.node.hidden = false;
            ui.node.className = 'champ-row'
                + (row.slot === this.localSlot ? ' is-me' : '')
                + (row.online === false ? ' offline' : '')
                + (final && i === 0 ? ' is-champion' : '');
            ui.pos.textContent = String(row.pos);
            ui.pos.className = 'champ-pos' + (row.pos <= 3 ? ' m' + row.pos : '');
            ui.name.textContent = row.name || 'Гонщик';
            ui.note.textContent = row.online === false
                ? 'не в сети — очки сохранены'
                : (row.joined_stage > 1 ? 'с этапа ' + row.joined_stage : '');
            ui.stage.textContent = row.stage_points > 0 ? '+' + row.stage_points : '—';
            ui.points.textContent = String(row.points);
            const delta = row.delta | 0;
            ui.delta.textContent = row.prev_pos
                ? (delta > 0 ? '▲ ' + delta : delta < 0 ? '▼ ' + (-delta) : '·')
                : 'новый';
            ui.delta.className = 'champ-delta'
                + (row.prev_pos ? (delta > 0 ? ' up' : delta < 0 ? ' down' : '') : ' fresh');
        }

        if (final && champ.champion) {
            this.championBlock.hidden = false;
            this.championName.textContent = champ.champion.name || 'Гонщик';
            this.championPoints.textContent = champ.champion.points + ' очк.'
                + (champ.champion.wins ? ' · побед: ' + champ.champion.wins : '');
        }
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
        this.wrap = wrap;
        root.appendChild(wrap);

        // Плашка повтора: единственное, что видно поверх сцены, пока
        // проигрываются последние секунды гонки.
        this.replayBar = el('div', 'res-replay');
        this.replayLabel = el('div', 'rr-label', 'Повтор финиша');
        this.replayBar.appendChild(this.replayLabel);
        this.replaySkip = el('button', 'btn btn-sm btn-ghost', 'Пропустить');
        this.replaySkip.type = 'button';
        this.replaySkip.addEventListener('click', () => {
            if (this.handlers.onSkipReplay) this.handlers.onSkipReplay();
            else this.endReplay();
        });
        this.replayBar.appendChild(this.replaySkip);
        this.replayBar.hidden = true;
        wrap.appendChild(this.replayBar);

        const panel = el('div', 'panel results-panel');
        this.panel = panel;
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
        this.titleText = el('h2', null, 'Итоги гонки');
        titleText.appendChild(this.titleText);
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

        // --- чемпионат: календарь и общий зачёт -----------------------------
        const champ = el('div', 'champ-block');
        champ.hidden = true;
        this.champBlock = champ;

        // Чемпион: отдельно и празднично, только после последнего этапа.
        this.championBlock = el('div', 'champ-champion');
        this.championBlock.hidden = true;
        const cup = el('div', 'cc-cup');
        cup.innerHTML = '<svg viewBox="0 0 32 32" width="44" height="44" fill="#ffc93c" '
            + 'stroke="#080a12" stroke-width="2.6" stroke-linejoin="round">'
            + '<path d="M9 4h14v7a7 7 0 0 1-14 0z"/>'
            + '<path d="M9 6H5v2a5 5 0 0 0 5 5M23 6h4v2a5 5 0 0 1-5 5" fill="none"/>'
            + '<path d="M14 18h4v4h-4zM10 22h12v4H10z"/></svg>';
        this.championBlock.appendChild(cup);
        const ccText = el('div');
        ccText.appendChild(el('div', 'cc-label', 'Чемпион серии'));
        this.championName = el('div', 'cc-name', '');
        ccText.appendChild(this.championName);
        this.championPoints = el('div', 'cc-points', '');
        ccText.appendChild(this.championPoints);
        this.championBlock.appendChild(ccText);
        champ.appendChild(this.championBlock);

        const champHead = el('div', 'champ-head');
        this.champTitle = el('div', 'champ-h', 'Общий зачёт');
        champHead.appendChild(this.champTitle);
        this.champCalendar = el('div', 'champ-calendar');
        champHead.appendChild(this.champCalendar);
        champ.appendChild(champHead);

        const champRows = el('div', 'champ-rows scroll');
        const champHeadRow = el('div', 'champ-row is-head');
        champHeadRow.appendChild(el('div', 'res-col-head', '#'));
        champHeadRow.appendChild(el('div', 'res-col-head', 'Игрок'));
        champHeadRow.appendChild(el('div', 'res-col-head', ''));
        const thStage = el('div', 'res-col-head', 'Этап');
        thStage.style.textAlign = 'right';
        champHeadRow.appendChild(thStage);
        const thPoints = el('div', 'res-col-head', 'Очки');
        thPoints.style.textAlign = 'right';
        champHeadRow.appendChild(thPoints);
        const thDelta = el('div', 'res-col-head', 'Δ');
        thDelta.style.textAlign = 'right';
        champHeadRow.appendChild(thDelta);
        champRows.appendChild(champHeadRow);

        this.champRows = [];
        for (let i = 0; i < CHAMP_ROWS; i++) {
            const row = el('div', 'champ-row');
            const pos = el('div', 'champ-pos', '—');
            const name = el('div', 'champ-name', '');
            const note = el('div', 'champ-note', '');
            const stage = el('div', 'champ-stage num', '—');
            const points = el('div', 'champ-points num', '0');
            const delta = el('div', 'champ-delta', '');
            row.appendChild(pos);
            row.appendChild(name);
            row.appendChild(note);
            row.appendChild(stage);
            row.appendChild(points);
            row.appendChild(delta);
            row.hidden = true;
            champRows.appendChild(row);
            this.champRows.push({
                node: row, pos: pos, name: name, note: note,
                stage: stage, points: points, delta: delta,
            });
        }
        champ.appendChild(champRows);
        panel.appendChild(champ);

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
