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
 * ПРИМЕЧАНИЕ ПО КОНТРАКТУ: в схемах раздела 9 нет поля, по которому клиент
 * узнаёт собственный слот (`welcome` отдаёт только `slot_token`, `room` —
 * массив игроков без пометки «это ты»). Поэтому слот сюда передаётся снаружи
 * через setLocalSlot(); пока он не задан, экран работает в режиме наблюдателя.
 */

import { ITEM_DEFS, itemIconSvg, trackIconSvg, showToast } from './menu.js';

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

        this._build();
        this._buildCountdown();
        this.hide();
    }

    // --- жизненный цикл ----------------------------------------------------

    show() { this.root.hidden = false; this.visible = true; }

    hide() { this.root.hidden = true; this.visible = false; }

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

        this._renderPlayers();
        this._syncCarCards();
        this._syncColorButtons();
        this._syncReadyButton(me);
        this._applySettings(msg.settings || {});
        this._syncOwnerControls(msg);
        this._renderChat(msg.chat || []);
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
        return col;
    }

    _buildCenterColumn() {
        const col = el('div', 'lobby-col');

        // Машины
        const carsPanel = el('div', 'panel');
        const carsHead = el('div', 'panel-head');
        carsHead.appendChild(el('h2', null, 'Машина'));
        carsPanel.appendChild(carsHead);
        this.carsGrid = el('div', 'cars-grid');
        carsPanel.appendChild(this.carsGrid);
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
        const setPanel = el('div', 'panel');
        const setHead = el('div', 'panel-head');
        setHead.appendChild(el('h2', null, 'Настройки'));
        const spacer = el('div');
        spacer.style.flex = '1';
        setHead.appendChild(spacer);
        this.ownerTag = el('div', 'label', 'только чтение');
        setHead.appendChild(this.ownerTag);
        setPanel.appendChild(setHead);

        const body = el('div', 'settings-view');

        // Трасса
        body.appendChild(el('div', 'label', 'Трасса'));
        this.trackChoice = el('div', 'set-track-row');
        body.appendChild(this.trackChoice);

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
        const send = el('button', 'btn btn-sm', 'Send');
        send.textContent = 'Отправить';
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
            card.appendChild(el('div', 'c-desc', car.desc || ''));

            const bars = el('div', 'bars');
            for (let b = 0; b < BAR_LABELS.length; b++) {
                const key = BAR_LABELS[b][0];
                const row = el('div', 'bar-row');
                row.dataset.bar = key;
                row.appendChild(el('div', 'bar-name', BAR_LABELS[b][1]));
                const track = el('div', 'bar-track');
                const value = (car.bars && car.bars[key]) | 0;
                for (let s = 1; s <= 5; s++) {
                    track.appendChild(el('div', s <= value ? 'bar-seg on' : 'bar-seg'));
                }
                row.appendChild(track);
                bars.appendChild(row);
            }
            card.appendChild(bars);

            card.addEventListener('click', () => this._selectCar(car.id));
            this.carsGrid.appendChild(card);
            this.carCards.push({ id: car.id, index: i, node: card, art: art });
        }
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
        for (let i = 0; i < this.carCards.length; i++) {
            const entry = this.carCards[i];
            const active = entry.id === this.myCar;
            entry.node.classList.toggle('is-active', active);
            entry.art.innerHTML = carIconSvg(entry.id, entry.index,
                active ? bodyColor : '#4d587a', 76);
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

        if (this.trackButtons) {
            for (let i = 0; i < this.trackButtons.length; i++) {
                this.trackButtons[i].node.classList.toggle('is-active',
                    this.trackButtons[i].id === settings.track);
            }
        }
        this.lapsControl.set(settings.laps || 3, true);
        this.maxControl.set(settings.max_players || 8, true);
        this.itemsEnabledInput.checked = !!settings.items_enabled;
        this.collisionsInput.checked = !!settings.collisions;
        this.mirrorInput.checked = !!settings.mirror;

        const items = settings.items || [];
        for (let i = 0; i < this.itemButtons.length; i++) {
            const entry = this.itemButtons[i];
            const on = items.indexOf(entry.def.id) >= 0 && settings.items_enabled;
            entry.node.classList.toggle('on', on);
        }
    }

    _syncOwnerControls(room) {
        const editable = this.isOwner && room.state === 'LOBBY';
        this.ownerTag.textContent = this.isOwner ? 'вы хозяин' : 'только чтение';
        this.ownerTag.style.color = this.isOwner ? 'var(--accent)' : '';

        this.lapsControl.setEditable(editable);
        this.maxControl.setEditable(editable);
        this.itemsEnabledInput.disabled = !editable;
        this.collisionsInput.disabled = !editable;
        this.mirrorInput.disabled = !editable;
        if (this.trackButtons) {
            for (let i = 0; i < this.trackButtons.length; i++) {
                this.trackButtons[i].node.disabled = !editable;
            }
        }
        for (let i = 0; i < this.itemButtons.length; i++) {
            this.itemButtons[i].node.disabled = !editable || !this.itemsEnabledInput.checked;
        }

        this.startButton.hidden = !this.isOwner;
        const players = room.players || [];
        this.startButton.disabled = room.state !== 'LOBBY' || players.length === 0;
        this.startButton.title = room.state !== 'LOBBY'
            ? 'Гонка уже идёт' : (players.length ? '' : 'В комнате никого нет');
    }

    _toggleItem(itemId) {
        const settings = this.currentSettings || {};
        const items = (settings.items || []).slice();
        const idx = items.indexOf(itemId);
        if (idx >= 0) items.splice(idx, 1); else items.push(itemId);
        this._pushSettings({ items: items });
    }

    /**
     * Отправить изменение настроек владельцем. Схема раздела 9 требует
     * целиком объект settings, поэтому шлём текущий с наложенным изменением.
     */
    _pushSettings(patch) {
        if (!this.isOwner || !this.room || this.room.state !== 'LOBBY') return;
        const current = this.currentSettings || {};
        const settings = {
            track: patch.track !== undefined ? patch.track : current.track,
            laps: patch.laps !== undefined ? patch.laps : current.laps,
            max_players: patch.max_players !== undefined ? patch.max_players : current.max_players,
            items_enabled: patch.items_enabled !== undefined ? patch.items_enabled : current.items_enabled,
            items: patch.items !== undefined ? patch.items : (current.items || []).slice(),
            collisions: patch.collisions !== undefined ? patch.collisions : current.collisions,
            mirror: patch.mirror !== undefined ? patch.mirror : current.mirror,
        };
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
