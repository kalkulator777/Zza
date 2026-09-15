/*
 * Экран лобби: игроки в слотах, выбор машины и цвета, готовность, чат,
 * настройки комнаты и обратный отсчёт перед стартом.
 *
 * Работает по схемам раздела 9 контракта:
 *   room      {id,name,owner_slot,state,settings,
 *              players:[{slot,name,car,color,ready,spectator,ping}],
 *              chat:[{slot,name,text,ts}]}
 *   countdown {value}
 *   welcome   {content:{cars:[{id,name,desc,bars}], tracks:[...], colors:[...]}}
 *
 * Использование из main.js:
 *
 *   import { LobbyScreen } from './ui/lobby.js';
 *
 *   const lobby = new LobbyScreen(
 *       document.getElementById('screen-lobby'),
 *       document.getElementById('overlay-countdown'),
 *       {
 *           onSetCar:         (carId, color) => send({t:'set_car', car_id: carId, color}),
 *           onSetReady:       (ready)       => send({t:'set_ready', ready}),
 *           onUpdateSettings: (settings)    => send({t:'update_settings', settings}),
 *           onStartRace:      ()            => send({t:'start_race'}),
 *           onChat:           (text)        => send({t:'chat', text}),
 *           onLeave:          ()            => send({t:'leave_room'}),
 *       });
 *   lobby.applyWelcome(msg);
 *   lobby.setLocalSlot(mySlot);   // см. примечание ниже
 *   lobby.applyRoom(msg);
 *   lobby.showCountdown(msg.value);
 *
 * ПРИМЕЧАНИЕ ПО КОНТРАКТУ: собственный слот игрока приходит в поле `room.you`
 * каждого события `room` (разделы 9 и 12.8); в `welcome` он почти всегда пуст,
 * потому что слот выдаётся только при входе в комнату. Клиент передаёт его
 * сюда через setLocalSlot(); пока слот не задан, экран работает в режиме
 * наблюдателя.
 */

import {
    ITEM_DEFS,
    itemIconSvg,
    trackIconSvg,
    timeOfDayIconSvg,
    weatherIconSvg,
    ROOM_TIMES_OF_DAY,
    ROOM_TOD_LABELS,
    ROOM_WEATHERS,
    ROOM_WEATHER_LABELS,
    trafficIconSvg,
    roadEventIconSvg,
    ROOM_TRAFFIC,
    ROOM_TRAFFIC_LABELS,
    ROOM_EVENTS,
    ROOM_EVENT_LABELS,
    ROOM_MODES,
    ROOM_MODE_LABELS,
    CHAMP_PHASE_LABELS,
    STAGES_MIN,
    STAGES_MAX,
    showToast
} from './menu.js';

// --- силуэты машин ---------------------------------------------------------

// Пять стилей из раздела 12.2. Вид сбоку, viewBox 0 0 80 40.
// body — контур кузова, extra — надстройки (каркас багги, стойки фургона),
// wheels — [x, радиус] на каждое колесо.
const CAR_SHAPES = {
    hatch: {
        body: 'M8 30 10 20H24L31 10H52L58 20H72L74 30Z',
        glass: 'M27 19 32 12H50L55 19Z',
        extra: '',
        wheels: [[23, 6.5], [58, 6.5]],
    },
    muscle: {
        body: 'M5 30 6 19H33L40 11H59L63 19H76L77 30Z',
        glass: 'M36 18 41 13H57L60 18Z',
        extra: 'M62 11h13v4H62z',
        wheels: [[20, 7], [62, 7]],
    },
    buggy: {
        body: 'M11 29 15 21H65L69 29Z',
        glass: '',
        extra: 'M23 22 30 7H52L60 22M30 7l24 7M30 14h24',
        wheels: [[23, 8.5], [60, 8.5]],
    },
    van: {
        body: 'M9 30 9 11 49 8 60 15 73 17 73 30Z',
        glass: 'M14 13 14 21H32V12ZM37 12 48 12 56 17 37 18Z',
        extra: '',
        wheels: [[22, 7], [60, 7]],
    },
    wedge: {
        body: 'M4 30 7 24 33 15 58 13 70 19 76 30Z',
        glass: 'M33 18 40 15H55L60 19Z',
        extra: 'M60 12h15v3H60z',
        wheels: [[21, 6], [61, 6]],
    },
};

const CAR_STYLE_ORDER = ['hatch', 'muscle', 'buggy', 'van', 'wedge'];

/**
 * Инлайновый SVG-силуэт машины для карточки лобби. Цвет кузова — выбранный
 * игроком, стёкла и пластик — тёмный акцент.
 */
function carIconSvg(carId, index, bodyColor, width) {
    const key = CAR_SHAPES[carId] ? carId : CAR_STYLE_ORDER[index % CAR_STYLE_ORDER.length];
    const shape = CAR_SHAPES[key];
    let svg = '<svg viewBox="0 0 80 40" width="' + width + '" height="' + (width * 40 / 80)
        + '" fill="none" stroke="#080a12" stroke-width="3" stroke-linejoin="round" stroke-linecap="round">';
    svg += '<path d="' + shape.body + '" fill="' + bodyColor + '"/>';
    if (shape.glass) svg += '<path d="' + shape.glass + '" fill="#1d1f24"/>';
    if (shape.extra) svg += '<path d="' + shape.extra + '" fill="#1d1f24"/>';
    for (let i = 0; i < shape.wheels.length; i++) {
        const w = shape.wheels[i];
        svg += '<circle cx="' + w[0] + '" cy="30" r="' + w[1] + '" fill="#15171d"/>';
        svg += '<circle cx="' + w[0] + '" cy="30" r="' + (w[1] * 0.42).toFixed(1) + '" fill="#9aa6c4" stroke-width="2"/>';
    }
    svg += '</svg>';
    return svg;
}

const BAR_LABELS = [
    ['speed', 'Скор.'],
    ['accel', 'Разгон'],
    ['grip', 'Сцепл.'],
    ['weight', 'Вес'],
];

const STATE_TEXT = {
    LOBBY: 'Лобби',
    COUNTDOWN: 'Старт!',
    RACING: 'Идёт гонка',
    RESULTS: 'Итоги',
};

function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
}

const MAX_SLOTS = 8;

// ---------------------------------------------------------------------------

export class LobbyScreen {
    /**
     * @param {HTMLElement} root корень экрана (#screen-lobby)
     * @param {HTMLElement} countdownRoot слой отсчёта поверх игры (#overlay-countdown)
     * @param {object} handlers коллбеки наружу, все необязательные
     */
    constructor(root, countdownRoot, handlers) {
        this.root = root;
        this.countdownRoot = countdownRoot;
        this.handlers = handlers || {};

        this.content = null;
        this.cars = [];
        this.colors = [];
        this.tracks = [];

        this.localSlot = -1;
        this.room = null;
        this.isOwner = false;
        this.myCar = '';
        this.myColor = '';
        this.myReady = false;

        this.chatRendered = 0;
        this.chatKey = '';
        this.countdownTimer = 0;
        this.champ = null;            // поле championship последнего room
        this.breakTimer = 0;
        this.breakLeft = 0;

        this._build();
        this._buildCountdown();
        this.hide();
    }

    // --- жизненный цикл ----------------------------------------------------

    show() {
        this.root.hidden = false;
        this.visible = true;
        // Прокрутка возможна только когда элемент уже имеет высоту.
        this.chatLog.scrollTop = this.chatLog.scrollHeight;
    }

    hide() {
        this.root.hidden = true;
        this.visible = false;
        this._stopBreakTimer();
    }

    setLocalSlot(slot) {
        this.localSlot = (slot === undefined || slot === null) ? -1 : slot | 0;
        if (this.room) this.applyRoom(this.room);
    }

    setError(message) { showToast(message || 'Ошибка', 'error'); }

    // --- входящие события --------------------------------------------------

    /** Событие `welcome`: машины, цвета и трассы сервера. */
    applyWelcome(msg) {
        this.content = msg.content || null;
        this.cars = (this.content && this.content.cars) || [];
        this.colors = (this.content && this.content.colors) || [];
        this.tracks = (this.content && this.content.tracks) || [];
        this._buildCarCards();
        this._buildColorButtons();
        this._buildTrackChoice();
    }

    /** Событие `room`: полное состояние комнаты. */
    applyRoom(msg) {
        this.room = msg;
        this.isOwner = this.localSlot >= 0 && msg.owner_slot === this.localSlot;

        this.titleNode.textContent = msg.name || 'Комната';
        this.stateNode.textContent = STATE_TEXT[msg.state] || msg.state || '';
        this.stateNode.className = 'pill pill-' + String(msg.state || 'lobby').toLowerCase();

        const me = this._findMe();
        if (me) {
            this.myCar = me.car || '';
            this.myColor = me.color || '';
            this.myReady = !!me.ready;
        }

        // Чемпионат разбирается ПЕРВЫМ: от него зависят и кнопка готовности,
        // и подсветка трассы в настройках — там ещё стоит трасса прошлого
        // этапа, она сменится только при старте следующего.
        this._applyChampionship(msg.championship || null);

        this._renderPlayers();
        this._syncCarCards();
        this._syncColorButtons();
        this._syncReadyButton(me);
        this._applySettings(msg.settings || {});
        this._syncOwnerControls(msg);
        this._renderChat(msg.chat || []);
    }

    // --- чемпионат ----------------------------------------------------------

    /**
     * Поле `championship` события room. Пусто — панель прячется, и лобби
     * выглядит ровно как в одиночной комнате.
     *
     * Обратный отсчёт паузы между этапами тикает здесь, а не приходит с
     * сервера каждую секунду: сервер присылает остаток один раз (`break_left`),
     * клиент досчитывает сам. Тридцать лишних рассылок состояния комнаты на
     * каждый этап — не та цена, которую стоит платить за секундную точность.
     */
    _applyChampionship(champ) {
        this.champ = champ;
        this._stopBreakTimer();
        const on = !!(champ && champ.active);
        this.champPanel.hidden = !on;
        if (!on) return;

        this.champTitle.textContent = champ.phase === 'final'
            ? 'Чемпионат завершён'
            : 'Чемпионат · этап ' + champ.stage + ' из ' + champ.stages;
        this.champPhase.textContent = CHAMP_PHASE_LABELS[champ.phase] || '';

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

        this.champTable.innerHTML = '';
        const table = champ.table || [];
        for (let i = 0; i < table.length; i++) {
            const row = table[i];
            const node = el('div', 'champ-lobby-row'
                + (row.slot === this.localSlot ? ' is-me' : '')
                + (row.online === false ? ' offline' : ''));
            node.appendChild(el('div', 'clr-pos', String(row.pos)));
            node.appendChild(el('div', 'clr-name', row.name || 'Гонщик'));
            node.appendChild(el('div', 'clr-note',
                row.online === false ? 'не в сети' : ''));
            node.appendChild(el('div', 'clr-points num', String(row.points)));
            this.champTable.appendChild(node);
        }

        const breaking = champ.phase === 'break' && champ.break_left > 0;
        this.champBreak.hidden = !breaking;
        if (breaking) this._startBreakTimer(champ.break_left);
    }

    _startBreakTimer(seconds) {
        this.breakLeft = Math.max(0, Math.round(seconds));
        this.champBreakValue.textContent = String(this.breakLeft);
        this.breakTimer = setInterval(() => {
            this.breakLeft--;
            if (this.breakLeft <= 0) {
                this.breakLeft = 0;
                this._stopBreakTimer();
            }
            this.champBreakValue.textContent = String(this.breakLeft);
        }, 1000);
    }

    _stopBreakTimer() {
        if (this.breakTimer) { clearInterval(this.breakTimer); this.breakTimer = 0; }
    }

    /** Событие `countdown`: 3, 2, 1, 0 крупно поверх всего экрана. */
    showCountdown(value) {
        const node = this.countdownNumber;
        this.countdownRoot.hidden = false;
        if (value > 0) {
            node.textContent = String(value);
            node.className = 'countdown-num';
        } else {
            node.textContent = 'ПОЕХАЛИ';
            node.className = 'countdown-num go';
        }
        // Перезапуск анимации: снять класс, форсировать reflow, вернуть.
        node.classList.remove('pop');
        void node.offsetWidth;
        node.classList.add('pop');

        if (this.countdownTimer) clearTimeout(this.countdownTimer);
        if (value <= 0) {
            this.countdownTimer = setTimeout(() => this.hideCountdown(), 700);
        }
    }

    hideCountdown() {
        if (this.countdownTimer) { clearTimeout(this.countdownTimer); this.countdownTimer = 0; }
        this.countdownRoot.hidden = true;
    }

    // --- сборка разметки ---------------------------------------------------

    _build() {
        const root = this.root;
        root.innerHTML = '';
        root.appendChild(el('div', 'screen-bg'));
        root.appendChild(el('div', 'screen-stripes'));

        const wrap = el('div', 'lobby-root');
        root.appendChild(wrap);

        // Верхняя полоса
        const top = el('div', 'panel lobby-top');
        this.titleNode = el('div', 'lobby-title', 'Комната');
        top.appendChild(this.titleNode);
        this.stateNode = el('div', 'pill pill-lobby', 'Лобби');
        top.appendChild(this.stateNode);
        top.appendChild(el('div', 'lobby-spacer'));

        this.leaveButton = el('button', 'btn btn-sm btn-ghost', 'Выйти в меню');
        this.leaveButton.type = 'button';
        this.leaveButton.addEventListener('click', () => {
            if (this.handlers.onLeave) this.handlers.onLeave();
        });
        top.appendChild(this.leaveButton);

        this.startButton = el('button', 'btn btn-green', 'Старт гонки');
        this.startButton.type = 'button';
        this.startButton.addEventListener('click', () => {
            if (this.handlers.onStartRace) this.handlers.onStartRace();
        });
        top.appendChild(this.startButton);
        wrap.appendChild(top);

        wrap.appendChild(this._buildPlayersColumn());
        wrap.appendChild(this._buildCenterColumn());
        wrap.appendChild(this._buildRightColumn());
    }

    _buildPlayersColumn() {
        const col = el('div', 'lobby-col');
        const panel = el('div', 'panel players-panel');
        const head = el('div', 'panel-head');
        head.appendChild(el('h2', null, 'Игроки'));
        const spacer = el('div');
        spacer.style.flex = '1';
        head.appendChild(spacer);
        this.playersCount = el('div', 'label num', '0/8');
        head.appendChild(this.playersCount);
        panel.appendChild(head);

        this.playersList = el('div', 'players-list scroll');
        // Восемь строк создаются один раз: обновляется только их содержимое.
        this.playerRows = [];
        for (let i = 0; i < MAX_SLOTS; i++) {
            const row = el('div', 'player-row is-empty');
            const slotNum = el('div', 'p-slot', String(i + 1));
            const color = el('div', 'p-color');
            const main = el('div', 'p-main');
            const name = el('div', 'p-name', 'Свободно');
            const car = el('div', 'p-car', '');
            main.appendChild(name);
            main.appendChild(car);

            const right = el('div', 'p-right');
            const crown = el('span', 'crown');
            crown.innerHTML = '<svg viewBox="0 0 24 24" width="17" height="17" fill="#ffc93c" '
                + 'stroke="#080a12" stroke-width="2.4" stroke-linejoin="round">'
                + '<path d="M3 18 4 7l5 4 3-6 3 6 5-4 1 11z"/></svg>';
            crown.hidden = true;
            const ping = el('div', 'p-ping', '');
            const ready = el('div', 'p-ready');
            ready.innerHTML = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" '
                + 'stroke="#080a12" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round">'
                + '<path d="M5 12.5 10 17.5 19 7"/></svg>';
            ready.firstChild.style.opacity = '0';
            right.appendChild(crown);
            right.appendChild(ping);
            right.appendChild(ready);

            row.appendChild(slotNum);
            row.appendChild(color);
            row.appendChild(main);
            row.appendChild(right);
            this.playersList.appendChild(row);
            this.playerRows.push({
                row: row, color: color, name: name, car: car,
                crown: crown, ping: ping, ready: ready, check: ready.firstChild,
            });
        }
        panel.appendChild(this.playersList);
        col.appendChild(panel);

        // --- чемпионат ------------------------------------------------------
        // Панель показывается только когда серия идёт: в одиночной комнате
        // экран лобби обязан выглядеть ровно как прежде.
        const champPanel = el('div', 'panel champ-panel');
        champPanel.hidden = true;
        this.champPanel = champPanel;
        const champHead = el('div', 'panel-head');
        this.champTitle = el('h2', null, 'Чемпионат');
        champHead.appendChild(this.champTitle);
        const champSpacer = el('div');
        champSpacer.style.flex = '1';
        champHead.appendChild(champSpacer);
        this.champPhase = el('div', 'label', '');
        champHead.appendChild(this.champPhase);
        champPanel.appendChild(champHead);

        const champBody = el('div', 'champ-lobby');
        this.champCalendar = el('div', 'champ-calendar');
        champBody.appendChild(this.champCalendar);

        this.champBreak = el('div', 'champ-break');
        this.champBreakValue = el('div', 'n', '0');
        this.champBreak.appendChild(this.champBreakValue);
        this.champBreakText = el('span', null, 'с до следующего этапа — успейте сменить машину');
        this.champBreak.appendChild(this.champBreakText);
        this.champBreak.hidden = true;
        champBody.appendChild(this.champBreak);

        this.champTable = el('div', 'champ-lobby-rows');
        champBody.appendChild(this.champTable);
        champPanel.appendChild(champBody);
        col.appendChild(champPanel);

        return col;
    }

    _buildCenterColumn() {
        const col = el('div', 'lobby-col');

        // Машины: ряд компактных карточек и подробности по выбранной
        const carsPanel = el('div', 'panel');
        const carsHead = el('div', 'panel-head');
        carsHead.appendChild(el('h2', null, 'Машина'));
        carsPanel.appendChild(carsHead);
        this.carsGrid = el('div', 'cars-grid');
        carsPanel.appendChild(this.carsGrid);

        const detail = el('div', 'car-detail');
        this.detailArt = el('div', 'cd-art');
        detail.appendChild(this.detailArt);
        const info = el('div', 'cd-info');
        this.detailName = el('div', 'cd-name', '');
        this.detailDesc = el('div', 'cd-desc', '');
        info.appendChild(this.detailName);
        info.appendChild(this.detailDesc);

        // Полоски характеристик: пять сегментов на каждую, значения 1..5
        this.detailBars = [];
        const bars = el('div', 'bars');
        for (let b = 0; b < BAR_LABELS.length; b++) {
            const row = el('div', 'bar-row');
            row.dataset.bar = BAR_LABELS[b][0];
            row.appendChild(el('div', 'bar-name', BAR_LABELS[b][1]));
            const track = el('div', 'bar-track');
            const segs = [];
            for (let k = 0; k < 5; k++) {
                const seg = el('div', 'bar-seg');
                track.appendChild(seg);
                segs.push(seg);
            }
            row.appendChild(track);
            bars.appendChild(row);
            this.detailBars.push({ key: BAR_LABELS[b][0], segs: segs });
        }
        info.appendChild(bars);
        detail.appendChild(info);
        carsPanel.appendChild(detail);
        col.appendChild(carsPanel);

        // Цвета
        const colorPanel = el('div', 'panel');
        const colorHead = el('div', 'panel-head');
        colorHead.appendChild(el('h2', null, 'Цвет'));
        colorPanel.appendChild(colorHead);
        this.colorsRow = el('div', 'colors-row');
        colorPanel.appendChild(this.colorsRow);
        col.appendChild(colorPanel);

        // Готовность
        const readyWrap = el('div', 'row');
        readyWrap.style.justifyContent = 'center';
        readyWrap.style.marginTop = 'auto';
        this.readyButton = el('button', 'btn btn-lg', 'Я готов');
        this.readyButton.type = 'button';
        this.readyButton.addEventListener('click', () => {
            this.myReady = !this.myReady;
            if (this.handlers.onSetReady) this.handlers.onSetReady(this.myReady);
            this._syncReadyButton(this._findMe());
        });
        readyWrap.appendChild(this.readyButton);
        col.appendChild(readyWrap);

        return col;
    }

    _buildRightColumn() {
        const col = el('div', 'lobby-col');

        // Настройки комнаты
        const setPanel = el('div', 'panel settings-panel');
        const setHead = el('div', 'panel-head');
        setHead.appendChild(el('h2', null, 'Настройки'));
        const spacer = el('div');
        spacer.style.flex = '1';
        setHead.appendChild(spacer);
        this.ownerTag = el('div', 'label', 'только чтение');
        setHead.appendChild(this.ownerTag);
        setPanel.appendChild(setHead);

        const body = el('div', 'settings-view scroll');

        // Режим комнаты: одиночная гонка или серия этапов.
        const modeRow = el('div', 'set-row');
        modeRow.appendChild(el('div', 'k', 'Режим'));
        const modeSeg = el('div', 'segmented');
        this.modeButtons = [];
        for (let i = 0; i < ROOM_MODES.length; i++) {
            const mode = ROOM_MODES[i];
            const btn = el('button', 'seg', ROOM_MODE_LABELS[mode]);
            btn.type = 'button';
            btn.addEventListener('click', () => this._pushSettings({ mode: mode }));
            modeSeg.appendChild(btn);
            this.modeButtons.push({ id: mode, node: btn });
        }
        modeRow.appendChild(modeSeg);
        body.appendChild(modeRow);

        this.stagesRow = el('div', 'set-row');
        this.stagesRow.appendChild(el('div', 'k', 'Этапов'));
        this.stagesControl = this._buildMiniStepper(STAGES_MIN, STAGES_MAX,
            (v) => this._pushSettings({ stages: v }));
        this.stagesRow.appendChild(this.stagesControl.node);
        body.appendChild(this.stagesRow);

        // Трасса
        body.appendChild(el('div', 'label', 'Трасса'));
        this.trackChoice = el('div', 'set-track-row');
        body.appendChild(this.trackChoice);

        // Окружение: время суток и погода. Обе — настройки комнаты (§9),
        // а не личные галочки графики, и обе стоят сразу под картой, потому
        // что умолчание обеих берётся из карты. Это два поля одного
        // механизма (§12.16), поэтому ряды строит один и тот же код.
        const tod = this._buildEnvRow(body, 'Время суток', ROOM_TIMES_OF_DAY,
            ROOM_TOD_LABELS, timeOfDayIconSvg,
            (id) => this._pushSettings({ time_of_day: id }));
        this.todButtons = tod.buttons;
        this.todHint = tod.hint;

        const weather = this._buildEnvRow(body, 'Погода', ROOM_WEATHERS,
            ROOM_WEATHER_LABELS, weatherIconSvg,
            (id) => this._pushSettings({ weather: id }));
        this.weatherButtons = weather.buttons;
        this.weatherHint = weather.hint;

        // Поток машин и происшествия на дороге. Это настройки комнаты, и
        // меняет их владелец в лобби — так же, как время суток и погоду.
        // Умолчания от карты у них нет (поток и ДТП — режим игры, а не
        // свойство места), поэтому строка подсказки всегда пустая.
        const traffic = this._buildEnvRow(body, 'Поток машин', ROOM_TRAFFIC,
            ROOM_TRAFFIC_LABELS, trafficIconSvg,
            (id) => this._pushSettings({ traffic: id }));
        this.trafficButtons = traffic.buttons;
        this.trafficHint = traffic.hint;

        const events = this._buildEnvRow(body, 'Происшествия', ROOM_EVENTS,
            ROOM_EVENT_LABELS, roadEventIconSvg,
            (id) => this._pushSettings({ events: id }));
        this.eventButtons = events.buttons;
        this.eventHint = events.hint;

        // Круги и игроки
        const lapsRow = el('div', 'set-row');
        lapsRow.appendChild(el('div', 'k', 'Кругов'));
        this.lapsControl = this._buildMiniStepper(1, 9, (v) => this._pushSettings({ laps: v }));
        lapsRow.appendChild(this.lapsControl.node);
        body.appendChild(lapsRow);

        const maxRow = el('div', 'set-row');
        maxRow.appendChild(el('div', 'k', 'Максимум игроков'));
        this.maxControl = this._buildMiniStepper(2, 8, (v) => this._pushSettings({ max_players: v }));
        maxRow.appendChild(this.maxControl.node);
        body.appendChild(maxRow);

        // Бонусы
        const itemsRow = el('div', 'set-row');
        itemsRow.appendChild(el('div', 'k', 'Бонусы'));
        const itemsToggle = el('label', 'toggle');
        this.itemsEnabledInput = el('input');
        this.itemsEnabledInput.type = 'checkbox';
        this.itemsEnabledInput.addEventListener('change', () =>
            this._pushSettings({ items_enabled: this.itemsEnabledInput.checked }));
        itemsToggle.appendChild(this.itemsEnabledInput);
        itemsToggle.appendChild(el('span', 'toggle-track'));
        itemsRow.appendChild(itemsToggle);
        body.appendChild(itemsRow);

        const itemsBar = el('div', 'set-items');
        this.itemButtons = [];
        for (let i = 0; i < ITEM_DEFS.length; i++) {
            const def = ITEM_DEFS[i];
            const btn = el('button', 'set-item-btn');
            btn.type = 'button';
            btn.title = def.name;
            btn.innerHTML = itemIconSvg(def.id, 24);
            btn.addEventListener('click', () => this._toggleItem(def.id));
            itemsBar.appendChild(btn);
            this.itemButtons.push({ def: def, node: btn });
        }
        body.appendChild(itemsBar);

        // Столкновения и зеркало
        const collRow = el('div', 'set-row');
        collRow.appendChild(el('div', 'k', 'Столкновения'));
        const collToggle = el('label', 'toggle');
        this.collisionsInput = el('input');
        this.collisionsInput.type = 'checkbox';
        this.collisionsInput.addEventListener('change', () =>
            this._pushSettings({ collisions: this.collisionsInput.checked }));
        collToggle.appendChild(this.collisionsInput);
        collToggle.appendChild(el('span', 'toggle-track'));
        collRow.appendChild(collToggle);
        body.appendChild(collRow);

        const mirrorRow = el('div', 'set-row');
        mirrorRow.appendChild(el('div', 'k', 'Зеркальная трасса'));
        const mirrorToggle = el('label', 'toggle');
        this.mirrorInput = el('input');
        this.mirrorInput.type = 'checkbox';
        this.mirrorInput.addEventListener('change', () =>
            this._pushSettings({ mirror: this.mirrorInput.checked }));
        mirrorToggle.appendChild(this.mirrorInput);
        mirrorToggle.appendChild(el('span', 'toggle-track'));
        mirrorRow.appendChild(mirrorToggle);
        body.appendChild(mirrorRow);

        // Гандикап и повтор финиша: обе по умолчанию выключены.
        const handicapRow = el('div', 'set-row');
        const handicapKey = el('div', 'k', 'Гандикап');
        handicapKey.title = 'Победитель прошлой гонки едет следующую чуть медленнее';
        handicapRow.appendChild(handicapKey);
        const handicapToggle = el('label', 'toggle');
        this.handicapInput = el('input');
        this.handicapInput.type = 'checkbox';
        this.handicapInput.addEventListener('change', () =>
            this._pushSettings({ handicap: this.handicapInput.checked }));
        handicapToggle.appendChild(this.handicapInput);
        handicapToggle.appendChild(el('span', 'toggle-track'));
        handicapRow.appendChild(handicapToggle);
        body.appendChild(handicapRow);

        const replayRow = el('div', 'set-row');
        const replayKey = el('div', 'k', 'Повтор финиша');
        replayKey.title = 'Последние три секунды гонки на экране итогов';
        replayRow.appendChild(replayKey);
        const replayToggle = el('label', 'toggle');
        this.replayInput = el('input');
        this.replayInput.type = 'checkbox';
        this.replayInput.addEventListener('change', () =>
            this._pushSettings({ replay: this.replayInput.checked }));
        replayToggle.appendChild(this.replayInput);
        replayToggle.appendChild(el('span', 'toggle-track'));
        replayRow.appendChild(replayToggle);
        body.appendChild(replayRow);

        setPanel.appendChild(body);
        col.appendChild(setPanel);

        // Чат
        const chatPanel = el('div', 'panel chat-panel');
        const chatHead = el('div', 'panel-head');
        chatHead.appendChild(el('h2', null, 'Чат'));
        chatPanel.appendChild(chatHead);
        this.chatLog = el('div', 'chat-log scroll');
        chatPanel.appendChild(this.chatLog);

        const form = el('form', 'chat-form');
        this.chatInput = el('input', 'input');
        this.chatInput.type = 'text';
        this.chatInput.maxLength = 140;
        this.chatInput.placeholder = 'Сообщение…';
        form.appendChild(this.chatInput);
        const send = el('button', 'btn btn-sm', 'Отправить');
        send.type = 'submit';
        form.appendChild(send);
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            const text = this.chatInput.value.trim();
            if (!text) return;
            this.chatInput.value = '';
            if (this.handlers.onChat) this.handlers.onChat(text);
        });
        chatPanel.appendChild(form);
        col.appendChild(chatPanel);

        return col;
    }

    _buildCountdown() {
        this.countdownRoot.innerHTML = '';
        const wrap = el('div', 'countdown-root');
        this.countdownNumber = el('div', 'countdown-num', '3');
        wrap.appendChild(this.countdownNumber);
        this.countdownRoot.appendChild(wrap);
        this.countdownRoot.hidden = true;
    }

    /**
     * Ряд кнопок выбора для поля окружения (время суток, погода).
     *
     * Кнопки те же, что у выбора трассы: иконка и подпись. Сетка задана
     * в стилях на три колонки; когда значений больше, число колонок и размер
     * иконки правятся здесь же инлайном — ряд из четырёх не должен
     * переноситься на вторую строку, а стили трогать нельзя.
     */
    _buildEnvRow(body, title, values, labels, iconSvg, onPick) {
        body.appendChild(el('div', 'label', title));
        const row = el('div', 'set-track-row');
        const wide = values.length > 3;
        if (wide) row.style.gridTemplateColumns = 'repeat(' + values.length + ', 1fr)';
        const buttons = [];
        for (let i = 0; i < values.length; i++) {
            const id = values[i];
            const btn = el('button', 'set-track-btn');
            btn.type = 'button';
            btn.title = labels[id] || id;
            const art = el('span');
            art.innerHTML = iconSvg(id, wide ? 34 : 40);
            btn.appendChild(art.firstChild);
            btn.appendChild(el('span', 'tn', labels[id] || id));
            btn.addEventListener('click', () => onPick(id));
            row.appendChild(btn);
            buttons.push({ id: id, node: btn });
        }
        body.appendChild(row);
        // Выбор руками сменой карты не перетирается, поэтому расхождение
        // с предложением карты видно строкой, а не остаётся сюрпризом.
        const hint = el('div', 'gfx-hint');
        body.appendChild(hint);
        return { row: row, buttons: buttons, hint: hint };
    }

    _buildMiniStepper(min, max, onChange) {
        const node = el('div', 'stepper mini');
        const minus = el('button', 'step', '–');
        minus.type = 'button';
        const value = el('div', 'val', String(min));
        const plus = el('button', 'step', '+');
        plus.type = 'button';
        node.appendChild(minus);
        node.appendChild(value);
        node.appendChild(plus);

        const api = {
            node: node, value: min, editable: false,
            set(v, silent) {
                api.value = Math.min(max, Math.max(min, v | 0));
                value.textContent = String(api.value);
                api.refresh();
                if (!silent) onChange(api.value);
            },
            setEditable(on) { api.editable = on; api.refresh(); },
            refresh() {
                minus.disabled = !api.editable || api.value <= min;
                plus.disabled = !api.editable || api.value >= max;
            },
        };
        minus.addEventListener('click', () => api.set(api.value - 1));
        plus.addEventListener('click', () => api.set(api.value + 1));
        api.refresh();
        return api;
    }

    // --- машины и цвета ----------------------------------------------------

    _buildCarCards() {
        this.carsGrid.innerHTML = '';
        this.carCards = [];
        for (let i = 0; i < this.cars.length; i++) {
            const car = this.cars[i];
            const card = el('button', 'car-card');
            card.type = 'button';
            const art = el('div', 'c-art');
            card.appendChild(art);
            card.appendChild(el('div', 'c-name', car.name || car.id));
            card.addEventListener('click', () => this._selectCar(car.id));
            this.carsGrid.appendChild(card);
            this.carCards.push({ id: car.id, index: i, node: card, art: art, car: car });
        }
        if (!this.myCar && this.cars.length) this.myCar = this.cars[0].id;
        this._syncCarCards();
    }

    _buildColorButtons() {
        this.colorsRow.innerHTML = '';
        this.colorButtons = [];
        for (let i = 0; i < this.colors.length; i++) {
            const color = this.colors[i];
            const btn = el('button', 'color-btn');
            btn.type = 'button';
            btn.style.background = color;
            btn.title = color;
            const taken = el('span', 'taken', '×');
            taken.hidden = true;
            btn.appendChild(taken);
            btn.addEventListener('click', () => this._selectColor(color));
            this.colorsRow.appendChild(btn);
            this.colorButtons.push({ color: color, node: btn, taken: taken });
        }
        this._syncColorButtons();
    }

    _buildTrackChoice() {
        this.trackChoice.innerHTML = '';
        this.trackButtons = [];
        for (let i = 0; i < this.tracks.length; i++) {
            const track = this.tracks[i];
            const btn = el('button', 'set-track-btn');
            btn.type = 'button';
            btn.title = (track.name || track.id) + (track.desc ? ' — ' + track.desc : '');
            const art = el('span');
            art.innerHTML = trackIconSvg(track.theme, 40);
            btn.appendChild(art.firstChild);
            btn.appendChild(el('span', 'tn', track.name || track.id));
            btn.addEventListener('click', () => this._pushSettings({ track: track.id }));
            this.trackChoice.appendChild(btn);
            this.trackButtons.push({ id: track.id, node: btn });
        }
    }

    _selectCar(carId) {
        this.myCar = carId;
        if (!this.myColor) this.myColor = this._firstFreeColor();
        this._syncCarCards();
        if (this.handlers.onSetCar) this.handlers.onSetCar(this.myCar, this.myColor);
    }

    _selectColor(color) {
        if (this._colorTakenByOther(color)) return;
        this.myColor = color;
        if (!this.myCar && this.cars.length) this.myCar = this.cars[0].id;
        this._syncColorButtons();
        this._syncCarCards();
        if (this.handlers.onSetCar) this.handlers.onSetCar(this.myCar, this.myColor);
    }

    _firstFreeColor() {
        for (let i = 0; i < this.colors.length; i++) {
            if (!this._colorTakenByOther(this.colors[i])) return this.colors[i];
        }
        return this.colors.length ? this.colors[0] : '#ffffff';
    }

    _colorTakenByOther(color) {
        if (!this.room || !this.room.players) return false;
        const players = this.room.players;
        for (let i = 0; i < players.length; i++) {
            if (players[i].slot !== this.localSlot && players[i].color === color) return true;
        }
        return false;
    }

    _syncCarCards() {
        if (!this.carCards) return;
        const bodyColor = this.myColor || '#ffc93c';
        let selected = null;
        for (let i = 0; i < this.carCards.length; i++) {
            const entry = this.carCards[i];
            const active = entry.id === this.myCar;
            if (active) selected = entry;
            entry.node.classList.toggle('is-active', active);
            entry.art.innerHTML = carIconSvg(entry.id, entry.index,
                active ? bodyColor : '#4d587a', 68);
        }
        if (!selected) selected = this.carCards[0];
        if (!selected) return;

        const car = selected.car;
        this.detailArt.innerHTML = carIconSvg(selected.id, selected.index, bodyColor, 148);
        this.detailName.textContent = car.name || car.id;
        this.detailDesc.textContent = car.desc || '';
        for (let b = 0; b < this.detailBars.length; b++) {
            const bar = this.detailBars[b];
            const value = (car.bars && car.bars[bar.key]) | 0;
            for (let k = 0; k < bar.segs.length; k++) {
                bar.segs[k].className = k < value ? 'bar-seg on' : 'bar-seg';
            }
        }
    }

    _syncColorButtons() {
        if (!this.colorButtons) return;
        for (let i = 0; i < this.colorButtons.length; i++) {
            const entry = this.colorButtons[i];
            const taken = this._colorTakenByOther(entry.color);
            entry.node.disabled = taken;
            entry.taken.hidden = !taken;
            entry.node.classList.toggle('is-active', entry.color === this.myColor);
        }
    }

    _syncReadyButton(me) {
        const spectator = me ? !!me.spectator : this.localSlot < 0;
        this.readyButton.disabled = spectator || !this.room || this.room.state !== 'LOBBY';
        if (spectator) {
            this.readyButton.textContent = 'Наблюдатель';
            this.readyButton.className = 'btn btn-lg btn-ghost';
            return;
        }
        // В идущей серии готовность не спрашивают (см. Room.start_race):
        // кнопка, которая ни на что не влияет, — обман, поэтому она гаснет.
        const champ = this.champ;
        if (champ && champ.active && champ.phase !== 'final') {
            this.readyButton.disabled = true;
            this.readyButton.textContent = 'Этап стартует сам';
            this.readyButton.className = 'btn btn-lg btn-ghost';
            return;
        }
        this.readyButton.textContent = this.myReady ? 'Готов — отменить' : 'Я готов';
        this.readyButton.className = this.myReady ? 'btn btn-lg btn-green' : 'btn btn-lg';
    }

    // --- игроки ------------------------------------------------------------

    _findMe() {
        if (this.localSlot < 0 || !this.room || !this.room.players) return null;
        const players = this.room.players;
        for (let i = 0; i < players.length; i++) {
            if (players[i].slot === this.localSlot) return players[i];
        }
        return null;
    }

    _carName(carId) {
        for (let i = 0; i < this.cars.length; i++) {
            if (this.cars[i].id === carId) return this.cars[i].name || carId;
        }
        return carId || 'машина не выбрана';
    }

    _renderPlayers() {
        const players = (this.room && this.room.players) || [];
        const bySlot = this._slotMap(players);
        const maxPlayers = (this.room && this.room.settings && this.room.settings.max_players) || MAX_SLOTS;
        this.playersCount.textContent = players.length + '/' + maxPlayers;

        for (let slot = 0; slot < MAX_SLOTS; slot++) {
            const ui = this.playerRows[slot];
            const player = bySlot[slot];
            ui.row.hidden = slot >= maxPlayers && !player;

            if (!player) {
                ui.row.className = 'player-row is-empty';
                ui.name.textContent = 'Свободно';
                ui.car.textContent = '';
                ui.color.style.background = 'transparent';
                ui.ping.textContent = '';
                ui.ping.className = 'p-ping';
                ui.ready.className = 'p-ready';
                ui.check.style.opacity = '0';
                ui.crown.hidden = true;
                continue;
            }

            ui.row.className = 'player-row' + (player.slot === this.localSlot ? ' is-me' : '');
            ui.name.textContent = player.name || 'Гонщик';
            ui.car.textContent = player.spectator ? 'наблюдатель' : this._carName(player.car);
            ui.color.style.background = player.color || 'transparent';
            ui.crown.hidden = !(this.room && this.room.owner_slot === player.slot);

            const ping = player.ping | 0;
            ui.ping.textContent = ping + ' мс';
            ui.ping.className = 'p-ping ' + (ping < 60 ? 'good' : ping < 120 ? 'mid' : 'bad');

            if (player.spectator) {
                ui.ready.className = 'p-ready spectator';
                ui.check.style.opacity = '0';
            } else {
                ui.ready.className = player.ready ? 'p-ready on' : 'p-ready';
                ui.check.style.opacity = player.ready ? '1' : '0';
            }
        }
    }

    _slotMap(players) {
        const map = this._slotMapCache || (this._slotMapCache = new Array(MAX_SLOTS));
        for (let i = 0; i < MAX_SLOTS; i++) map[i] = null;
        for (let i = 0; i < players.length; i++) {
            const slot = players[i].slot | 0;
            if (slot >= 0 && slot < MAX_SLOTS) map[slot] = players[i];
        }
        return map;
    }

    // --- настройки комнаты -------------------------------------------------

    _applySettings(settings) {
        this.currentSettings = settings;

        let shownTrack = settings.track;
        const champ = this.champ;
        if (champ && champ.active && champ.phase !== 'final') {
            for (let i = 0; i < (champ.calendar || []).length; i++) {
                if (champ.calendar[i].current) { shownTrack = champ.calendar[i].track; break; }
            }
        }
        if (this.trackButtons) {
            for (let i = 0; i < this.trackButtons.length; i++) {
                this.trackButtons[i].node.classList.toggle('is-active',
                    this.trackButtons[i].id === shownTrack);
            }
        }
        this._syncEnvironment(settings);
        this.lapsControl.set(settings.laps || 3, true);
        this.maxControl.set(settings.max_players || 8, true);
        this.itemsEnabledInput.checked = !!settings.items_enabled;
        this.collisionsInput.checked = !!settings.collisions;
        this.mirrorInput.checked = !!settings.mirror;
        this.handicapInput.checked = !!settings.handicap;
        this.replayInput.checked = !!settings.replay;

        const mode = ROOM_MODES.indexOf(settings.mode) >= 0 ? settings.mode : ROOM_MODES[0];
        for (let i = 0; i < this.modeButtons.length; i++) {
            this.modeButtons[i].node.classList.toggle('is-active',
                this.modeButtons[i].id === mode);
        }
        this.stagesRow.hidden = mode !== 'championship';
        this.stagesControl.set(settings.stages || STAGES_MIN, true);

        const items = settings.items || [];
        for (let i = 0; i < this.itemButtons.length; i++) {
            const entry = this.itemButtons[i];
            const on = items.indexOf(entry.def.id) >= 0 && settings.items_enabled;
            entry.node.classList.toggle('on', on);
        }
    }

    /** Окружение комнаты: подсветка выбранного и предложение карты. */
    _syncEnvironment(settings) {
        this._syncEnvRow(this.todButtons, this.todHint, settings.time_of_day,
            this._trackTimeOfDay(settings.track), ROOM_TOD_LABELS);
        this._syncEnvRow(this.weatherButtons, this.weatherHint, settings.weather,
            this._trackWeather(settings.track), ROOM_WEATHER_LABELS);
        // Предложение карты совпадает с выбранным намеренно: подсказки
        // «карта предлагает» у этих двух полей быть не должно.
        this._syncEnvRow(this.trafficButtons, this.trafficHint,
            settings.traffic, settings.traffic, ROOM_TRAFFIC_LABELS);
        this._syncEnvRow(this.eventButtons, this.eventHint,
            settings.events, settings.events, ROOM_EVENT_LABELS);
    }

    _syncEnvRow(buttons, hint, chosen, proposed, labels) {
        if (!buttons) return;
        for (let i = 0; i < buttons.length; i++) {
            buttons[i].node.classList.toggle('is-active', buttons[i].id === chosen);
        }
        hint.textContent = (!chosen || proposed === chosen)
            ? '' : 'Карта предлагает: ' + (labels[proposed] || proposed);
    }

    /** Поле окружения, предложенное описанием карты (welcome.content.tracks[]). */
    _trackEnvField(trackId, field, values) {
        for (let i = 0; i < this.tracks.length; i++) {
            if (this.tracks[i].id === trackId) {
                const value = this.tracks[i][field];
                if (values.indexOf(value) >= 0) return value;
            }
        }
        return values[0];
    }

    _trackTimeOfDay(trackId) {
        return this._trackEnvField(trackId, 'time_of_day', ROOM_TIMES_OF_DAY);
    }

    _trackWeather(trackId) {
        return this._trackEnvField(trackId, 'weather', ROOM_WEATHERS);
    }

    _syncOwnerControls(room) {
        const editable = this.isOwner && room.state === 'LOBBY';
        // Пока серия идёт, трассу задаёт календарь, а режим и длину серии
        // менять поздно: очки уже начислены (та же проверка на сервере).
        const seriesRunning = !!(this.champ && this.champ.active);
        const seriesEditable = editable && !seriesRunning;
        this.ownerTag.textContent = this.isOwner ? 'вы хозяин' : 'только чтение';
        this.ownerTag.style.color = this.isOwner ? 'var(--accent)' : '';

        this.lapsControl.setEditable(editable);
        this.maxControl.setEditable(editable);
        this.stagesControl.setEditable(seriesEditable);
        this.itemsEnabledInput.disabled = !editable;
        this.collisionsInput.disabled = !editable;
        this.mirrorInput.disabled = !editable;
        this.handicapInput.disabled = !editable;
        this.replayInput.disabled = !editable;
        for (let i = 0; i < this.modeButtons.length; i++) {
            this.modeButtons[i].node.disabled = !seriesEditable;
        }
        if (this.trackButtons) {
            for (let i = 0; i < this.trackButtons.length; i++) {
                this.trackButtons[i].node.disabled = !seriesEditable;
            }
        }
        // Оба ряда окружения: владелец меняет, остальные видят только чтение.
        this._setEnvRowEditable(this.todButtons, editable);
        this._setEnvRowEditable(this.weatherButtons, editable);
        this._setEnvRowEditable(this.trafficButtons, editable);
        this._setEnvRowEditable(this.eventButtons, editable);
        for (let i = 0; i < this.itemButtons.length; i++) {
            this.itemButtons[i].node.disabled = !editable || !this.itemsEnabledInput.checked;
        }

        this.startButton.hidden = !this.isOwner;
        const players = room.players || [];
        this.startButton.disabled = room.state !== 'LOBBY' || players.length === 0;
        const champ = this.champ;
        if (champ && champ.active && champ.phase !== 'final') {
            this.startButton.textContent = 'Этап ' + champ.stage + ' — начать сейчас';
        } else if ((room.settings || {}).mode === 'championship') {
            this.startButton.textContent = 'Старт чемпионата';
        } else {
            this.startButton.textContent = 'Старт гонки';
        }
        this.startButton.title = room.state !== 'LOBBY'
            ? 'Гонка уже идёт' : (players.length ? '' : 'В комнате никого нет');
    }

    _setEnvRowEditable(buttons, editable) {
        if (!buttons) return;
        for (let i = 0; i < buttons.length; i++) {
            const node = buttons[i].node;
            node.disabled = !editable;
            // У .seg-подобных кнопок нет стиля для disabled, а у выбора
            // трассы он есть: повторяем его, чтобы «только чтение»
            // выглядело одинаково во всех рядах.
            node.style.opacity = (!editable && !node.classList.contains('is-active'))
                ? '0.5' : '';
        }
    }

    _toggleItem(itemId) {
        const settings = this.currentSettings || {};
        const items = (settings.items || []).slice();
        const idx = items.indexOf(itemId);
        if (idx >= 0) items.splice(idx, 1); else items.push(itemId);
        this._pushSettings({ items: items });
    }

    /**
     * Поле окружения для следующего пакета настроек.
     *
     * Правило то же, что на сервере (см. validate_settings в server/room.py),
     * и одно на оба поля: значение, равное предложению ПРЕДЫДУЩЕЙ карты,
     * ехало за картой — пусть едет и за новой; выбранное руками остаётся как
     * есть. Считаем это здесь же, чтобы кнопки переключились сразу,
     * не дожидаясь ответа сервера.
     */
    _nextEnvField(current, patch, field, values) {
        if (patch[field] !== undefined) return patch[field];
        const chosen = current[field];
        if (patch.track !== undefined
            && chosen === this._trackEnvField(current.track, field, values)) {
            return this._trackEnvField(patch.track, field, values);
        }
        return chosen;
    }

    /**
     * Отправить изменение настроек владельцем. Схема раздела 9 требует
     * целиком объект settings, поэтому шлём текущий с наложенным изменением.
     *
     * Настройки копируются как есть, а не переписываются полем за полем:
     * поле, которого этот экран ещё не знает, уедет обратно нетронутым,
     * а сервер всё равно проверяет каждое сам.
     */
    _pushSettings(patch) {
        if (!this.isOwner || !this.room || this.room.state !== 'LOBBY') return;
        const current = this.currentSettings || {};
        const settings = Object.assign({}, current, patch);
        settings.items = (settings.items || []).slice();
        settings.time_of_day = this._nextEnvField(current, patch,
            'time_of_day', ROOM_TIMES_OF_DAY);
        settings.weather = this._nextEnvField(current, patch,
            'weather', ROOM_WEATHERS);
        this._applySettings(settings);
        this._syncOwnerControls(this.room);
        if (this.handlers.onUpdateSettings) this.handlers.onUpdateSettings(settings);
    }

    // --- чат ---------------------------------------------------------------

    _renderChat(chat) {
        // Обычный случай — в конец списка добавилась пара строк: дописываем их,
        // не пересобирая лог. Если история обрезана сервером — пересобираем.
        const key = chat.length ? String(chat[0].ts) + '|' + chat[0].slot : '';
        if (key !== this.chatKey || chat.length < this.chatRendered) {
            this.chatLog.innerHTML = '';
            this.chatRendered = 0;
            this.chatKey = key;
        }
        for (let i = this.chatRendered; i < chat.length; i++) {
            const entry = chat[i];
            const line = el('div', 'chat-line' + (entry.slot === undefined || entry.slot < 0 ? ' sys' : ''));
            if (entry.slot !== undefined && entry.slot >= 0) {
                const who = el('span', 'who', (entry.name || 'Гонщик') + ': ');
                who.style.color = this._colorOfSlot(entry.slot);
                line.appendChild(who);
            }
            line.appendChild(el('span', 'txt', entry.text || ''));
            this.chatLog.appendChild(line);
        }
        if (chat.length !== this.chatRendered) {
            this.chatRendered = chat.length;
            this.chatLog.scrollTop = this.chatLog.scrollHeight;
        }
    }

    _colorOfSlot(slot) {
        const players = (this.room && this.room.players) || [];
        for (let i = 0; i < players.length; i++) {
            if (players[i].slot === slot) return players[i].color || 'var(--text)';
        }
        return 'var(--text)';
    }
}
