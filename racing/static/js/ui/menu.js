/*
 * Главное меню: имя игрока, список комнат сервера, список других серверов
 * в сети, форма создания комнаты и локальные настройки клиента.
 *
 * Работает по схемам раздела 9 контракта:
 *   welcome {slot_token, is_host, server_name, content:{tracks, cars, colors}}
 *   rooms   {rooms:[{id,name,track,laps,players,max_players,state}],
 *            servers:[{name,url,players,rooms}]}
 *
 * Использование из main.js:
 *
 *   import { MenuScreen, showToast, loadUiSettings } from './ui/menu.js';
 *
 *   const menu = new MenuScreen(document.getElementById('screen-menu'), {
 *       onNameChange:     (name) => { ... },
 *       onCreateRoom:     (name, settings) => ws.send(JSON.stringify(
 *                             {t: 'create_room', name, settings})),
 *       onJoinRoom:       (roomId) => ws.send(JSON.stringify(
 *                             {t: 'join_room', room_id: roomId})),
 *       onRefreshRooms:   () => ws.send(JSON.stringify({t: 'list_rooms'})),
 *       onSettingsChange: (s) => { renderer.setQuality(s.quality);
 *                                  audio.setVolume(s.volume, s.muted); },
 *   });
 *   menu.show();
 *   menu.applyWelcome(msg);   // на событие welcome
 *   menu.applyRooms(msg);     // на событие rooms
 *
 * Модуль также владеет мелкими общими примитивами интерфейса, которыми
 * пользуются соседние экраны: иконки бонусов, тост об ошибке и
 * localStorage-настройки клиента.
 */

// --- локальные настройки клиента -------------------------------------------

const LS_NAME = 'racing.name';
const LS_QUALITY = 'racing.quality';
const LS_VOLUME = 'racing.volume';
const LS_MUTED = 'racing.muted';

export const QUALITY_PRESETS = ['low', 'medium', 'high'];
const QUALITY_LABELS = { low: 'Низкое', medium: 'Среднее', high: 'Высокое' };

/** Безопасное чтение localStorage: в приватном окне доступ может бросать. */
function lsGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
}

function lsSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* нет хранилища — живём без него */ }
}

/**
 * Текущие настройки клиента. Пресет качества по умолчанию — среднее
 * (раздел 10.3 контракта).
 */
export function loadUiSettings() {
    const quality = lsGet(LS_QUALITY);
    const volumeRaw = parseFloat(lsGet(LS_VOLUME));
    return {
        name: lsGet(LS_NAME) || '',
        quality: QUALITY_PRESETS.indexOf(quality) >= 0 ? quality : 'medium',
        volume: Number.isFinite(volumeRaw) ? Math.min(1, Math.max(0, volumeRaw)) : 0.7,
        muted: lsGet(LS_MUTED) === '1',
    };
}

/** Записать часть настроек. Возвращает полный объект настроек после записи. */
export function saveUiSettings(patch) {
    if (patch.name !== undefined) lsSet(LS_NAME, patch.name);
    if (patch.quality !== undefined) lsSet(LS_QUALITY, patch.quality);
    if (patch.volume !== undefined) lsSet(LS_VOLUME, String(patch.volume));
    if (patch.muted !== undefined) lsSet(LS_MUTED, patch.muted ? '1' : '0');
    return loadUiSettings();
}

// --- бонусы: описания и иконки ---------------------------------------------

/**
 * Пять бонусов раздела 8. `kind` совпадает с числовым кодом в снапшоте,
 * `instr` — творительный падеж для строк вида «Вас задело ракетой».
 */
export const ITEM_DEFS = [
    { id: 'boost',  kind: 1, name: 'Турбо',  instr: 'турбо',   color: '#ffc93c' },
    { id: 'rocket', kind: 2, name: 'Ракета', instr: 'ракетой', color: '#ff5a5f' },
    { id: 'mine',   kind: 3, name: 'Мина',   instr: 'миной',   color: '#b06bff' },
    { id: 'shield', kind: 4, name: 'Щит',    instr: 'щитом',   color: '#4aa8ff' },
    { id: 'storm',  kind: 5, name: 'Гроза',  instr: 'грозой',  color: '#3ddc84' },
];

export const ITEM_BY_ID = Object.create(null);
export const ITEM_BY_KIND = Object.create(null);
for (let i = 0; i < ITEM_DEFS.length; i++) {
    ITEM_BY_ID[ITEM_DEFS[i].id] = ITEM_DEFS[i];
    ITEM_BY_KIND[ITEM_DEFS[i].kind] = ITEM_DEFS[i];
}

// Тела иконок в системе координат 0..48. Обводка рисуется общим stroke.
const ITEM_PATHS = {
    // Турбо: тройной шеврон, остриём вперёд
    boost: '<path d="M44 24 30 8v10L18 8v10L6 8v32l12-10v10l12-10v10z"/>',
    // Ракета: корпус с носом и стабилизаторами
    rocket: '<path d="M24 4c7 6 10 13 10 21v9H14v-9c0-8 3-15 10-21z"/>'
          + '<path d="M14 26 6 36v8l8-6zM34 26l8 10v8l-8-6z"/>'
          + '<path d="M19 34h10v6l-5 6-5-6z"/>',
    // Мина: круг с шипами
    mine: '<path d="M24 2v8M24 38v8M2 24h8M38 24h8M8.5 8.5l5.7 5.7M33.8 33.8l5.7 5.7M39.5 8.5l-5.7 5.7M14.2 33.8l-5.7 5.7" stroke-width="5" stroke-linecap="round"/>'
        + '<circle cx="24" cy="24" r="12"/>',
    // Щит: классический геральдический щит
    shield: '<path d="M24 3 42 10v14c0 10-7 17-18 21C13 41 6 34 6 24V10z"/>',
    // Гроза: туча с молнией
    storm: '<path d="M14 26a8 8 0 0 1 1-16 11 11 0 0 1 21 2 7 7 0 0 1-2 14z"/>'
         + '<path d="M25 24 15 40h8l-3 8 13-18h-9l3-6z"/>',
};

/**
 * Инлайновый SVG иконки бонуса. Ни одного файла-картинки: разметка строится
 * строкой и вставляется через innerHTML один раз при сборке экрана.
 */
export function itemIconSvg(itemId, size, fill) {
    const def = ITEM_BY_ID[itemId];
    if (!def) return '';
    const color = fill || def.color;
    return '<svg class="ic" viewBox="0 0 48 48" width="' + size + '" height="' + size
        + '" fill="' + color + '" stroke="#080a12" stroke-width="3" stroke-linejoin="round">'
        + ITEM_PATHS[def.id] + '</svg>';
}

/** Силуэт трассы для карточки выбора: три разных контура по теме. */
export function trackIconSvg(theme, size) {
    const paths = {
        city: 'M12 20c0-6 8-8 18-8s28 2 28 10-10 8-18 10-14 4-14 10 8 8 16 8h10',
        mountain: 'M8 46c8 0 6-14 14-14s8 12 16 12 6-16 14-16 8 10 4 18',
        industrial: 'M10 14h20l8 10h20M10 14v18h22l10 12h10',
    };
    const colors = { city: '#4aa8ff', mountain: '#3ddc84', industrial: '#ff9b3d' };
    const d = paths[theme] || paths.city;
    const c = colors[theme] || colors.city;
    return '<svg viewBox="0 0 68 56" width="' + size + '" height="' + (size * 56 / 68)
        + '" fill="none" stroke-linecap="round" stroke-linejoin="round">'
        + '<path d="' + d + '" stroke="#080a12" stroke-width="11"/>'
        + '<path d="' + d + '" stroke="' + c + '" stroke-width="6"/>'
        + '</svg>';
}

// --- тост ------------------------------------------------------------------

let toastTimer = 0;

/**
 * Короткое сообщение поверх всего. Используется для события `error`
 * (раздел 9) из любого состояния клиента.
 */
export function showToast(text, kind) {
    const node = document.getElementById('toast');
    if (!node) return;
    node.textContent = text;
    node.className = 'toast' + (kind === 'info' ? ' toast-info' : '');
    node.hidden = false;
    node.classList.remove('is-hiding');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        node.classList.add('is-hiding');
        toastTimer = setTimeout(() => { node.hidden = true; toastTimer = 0; }, 220);
    }, 3600);
}

// --- мелкие помощники сборки DOM -------------------------------------------

function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
}

/** Разметка состояния комнаты: цветная «таблетка». */
const STATE_LABELS = {
    LOBBY: ['Лобби', 'pill-lobby'],
    COUNTDOWN: ['Старт', 'pill-countdown'],
    RACING: ['Гонка', 'pill-racing'],
    RESULTS: ['Итоги', 'pill-results'],
};

// Настройки комнаты по умолчанию — ровно схема из раздела 9.
function defaultRoomSettings() {
    return {
        track: 'office',
        laps: 3,
        max_players: 8,
        items_enabled: true,
        items: ['boost', 'rocket', 'mine', 'shield', 'storm'],
        collisions: true,
        mirror: false,
    };
}

// ---------------------------------------------------------------------------

export class MenuScreen {
    /**
     * @param {HTMLElement} root корень экрана (#screen-menu)
     * @param {object} handlers коллбеки наружу, все необязательные
     */
    constructor(root, handlers) {
        this.root = root;
        this.handlers = handlers || {};

        this.content = null;      // welcome.content
        this.isHost = false;      // welcome.is_host — право создавать комнаты
        this.serverName = '';
        this.rooms = [];
        this.servers = [];

        this.settings = loadUiSettings();
        this.draft = defaultRoomSettings();

        this._build();
        this._applyLocalSettings();
        this.hide();
    }

    // --- жизненный цикл ----------------------------------------------------

    show() {
        this.root.hidden = false;
        this.visible = true;
        if (this.handlers.onRefreshRooms) this.handlers.onRefreshRooms();
    }

    hide() {
        this.root.hidden = true;
        this.visible = false;
        this._closeModal();
    }

    /** Имя игрока, которое надо отправить в `hello`. */
    getName() {
        return this.nameInput.value.trim() || 'Гонщик';
    }

    // --- входящие события --------------------------------------------------

    /** Событие `welcome`: контент сервера и право на создание комнат. */
    applyWelcome(msg) {
        this.content = msg.content || null;
        this.isHost = !!msg.is_host;
        this.serverName = msg.server_name || '';

        this.serverBadgeName.textContent = this.serverName || 'этот компьютер';
        this._buildTrackCards();
        this._updateCreateAvailability();
    }

    /** Событие `rooms`: комнаты этого сервера и другие серверы в сети. */
    applyRooms(msg) {
        this.rooms = msg.rooms || [];
        this.servers = msg.servers || [];
        this._renderRooms();
        this._renderServers();
    }

    /** Событие `error`: показать текст рядом со списком комнат. */
    setError(message) {
        showToast(message || 'Ошибка', 'error');
    }

    // --- сборка разметки ---------------------------------------------------

    _build() {
        const root = this.root;
        root.innerHTML = '';
        root.appendChild(el('div', 'screen-bg'));
        root.appendChild(el('div', 'screen-stripes'));

        const wrap = el('div', 'menu-root');
        root.appendChild(wrap);

        wrap.appendChild(this._buildLeft());
        wrap.appendChild(this._buildRight());
        root.appendChild(this._buildModal());
    }

    _buildLeft() {
        const left = el('div', 'menu-left');

        // Шапка с логотипом
        const brand = el('div', 'brand');
        const mark = el('div', 'brand-mark');
        mark.innerHTML = '<svg viewBox="0 0 80 40" width="46" height="23" fill="none"'
            + ' stroke="#080a12" stroke-width="3.4" stroke-linejoin="round" stroke-linecap="round">'
            + '<path d="M5 30 6 19H33L40 11H59L63 19H76L77 30Z" fill="#ff5a5f"/>'
            + '<path d="M36 18 41 13H57L60 18Z" fill="#141826"/>'
            + '<circle cx="20" cy="30" r="7" fill="#141826"/>'
            + '<circle cx="62" cy="30" r="7" fill="#141826"/>'
            + '</svg>';
        const titles = el('div');
        titles.appendChild(el('div', 'brand-title', 'Гонки'));
        titles.appendChild(el('div', 'brand-sub', 'локальная сеть'));
        brand.appendChild(mark);
        brand.appendChild(titles);
        left.appendChild(brand);

        const badge = el('div', 'server-badge');
        badge.appendChild(el('span', 'dot'));
        badge.appendChild(el('span', null, 'Сервер:'));
        this.serverBadgeName = el('strong', null, '…');
        badge.appendChild(this.serverBadgeName);
        left.appendChild(badge);

        // Имя игрока
        const namePanel = el('div', 'panel');
        const nameBody = el('div', 'panel-body');
        const nameField = el('div', 'field');
        nameField.appendChild(el('div', 'label', 'Ваше имя'));
        this.nameInput = el('input', 'input');
        this.nameInput.type = 'text';
        this.nameInput.maxLength = 18;
        this.nameInput.placeholder = 'Гонщик';
        this.nameInput.value = this.settings.name;
        this.nameInput.addEventListener('change', () => this._onNameCommit());
        this.nameInput.addEventListener('blur', () => this._onNameCommit());
        nameField.appendChild(this.nameInput);
        nameBody.appendChild(nameField);
        namePanel.appendChild(nameBody);
        left.appendChild(namePanel);

        // Настройки клиента
        const setPanel = el('div', 'panel');
        const setHead = el('div', 'panel-head');
        setHead.appendChild(el('h2', null, 'Настройки'));
        setPanel.appendChild(setHead);

        const setBody = el('div', 'panel-body');

        const qField = el('div', 'field');
        qField.appendChild(el('div', 'label', 'Качество картинки'));
        const seg = el('div', 'segmented');
        this.qualityButtons = [];
        for (let i = 0; i < QUALITY_PRESETS.length; i++) {
            const preset = QUALITY_PRESETS[i];
            const btn = el('button', 'seg', QUALITY_LABELS[preset]);
            btn.type = 'button';
            btn.addEventListener('click', () => this._setQuality(preset));
            seg.appendChild(btn);
            this.qualityButtons.push(btn);
        }
        qField.appendChild(seg);
        setBody.appendChild(qField);

        const vField = el('div', 'field');
        const vHead = el('div', 'row-between');
        vHead.appendChild(el('div', 'label', 'Громкость'));
        this.volumeValue = el('div', 'label num', '70');
        vHead.appendChild(this.volumeValue);
        vField.appendChild(vHead);
        this.volumeInput = el('input', 'range');
        this.volumeInput.type = 'range';
        this.volumeInput.min = '0';
        this.volumeInput.max = '100';
        this.volumeInput.step = '1';
        this.volumeInput.addEventListener('input', () => this._setVolume(this.volumeInput.value / 100));
        vField.appendChild(this.volumeInput);
        setBody.appendChild(vField);

        const mField = el('div', 'field');
        const muteLabel = el('label', 'toggle');
        this.muteInput = el('input');
        this.muteInput.type = 'checkbox';
        this.muteInput.addEventListener('change', () => this._setMuted(this.muteInput.checked));
        muteLabel.appendChild(this.muteInput);
        muteLabel.appendChild(el('span', 'toggle-track'));
        muteLabel.appendChild(el('span', null, 'Звук включён'));
        mField.appendChild(muteLabel);
        setBody.appendChild(mField);

        setPanel.appendChild(setBody);
        left.appendChild(setPanel);

        // Подсказка по управлению — чтобы меню не выглядело пустым
        const keys = el('div', 'panel');
        const keysBody = el('div', 'panel-body');
        keysBody.appendChild(el('div', 'label', 'Управление'));
        const keysText = el('div', 'hint');
        keysText.innerHTML = '<b>W&nbsp;A&nbsp;S&nbsp;D</b> или стрелки — руль и газ · '
            + '<b>Пробел</b> — дрифт · <b>E</b> — бонус · <b>C</b> — камера · '
            + '<b>Shift</b> — назад · <b>Tab</b> — таблица · <b>F3</b> — отладка';
        keysText.style.marginTop = '8px';
        keysBody.appendChild(keysText);
        keys.appendChild(keysBody);
        left.appendChild(keys);

        return left;
    }

    _buildRight() {
        const right = el('div', 'menu-right');

        // Комнаты
        const roomsPanel = el('div', 'panel rooms-panel');
        const head = el('div', 'panel-head');
        head.appendChild(el('h2', null, 'Комнаты'));
        const spacer = el('div');
        spacer.style.flex = '1';
        head.appendChild(spacer);

        this.refreshButton = el('button', 'btn btn-sm btn-ghost', 'Обновить');
        this.refreshButton.type = 'button';
        this.refreshButton.addEventListener('click', () => {
            if (this.handlers.onRefreshRooms) this.handlers.onRefreshRooms();
        });
        head.appendChild(this.refreshButton);

        this.createButton = el('button', 'btn btn-sm', 'Создать комнату');
        this.createButton.type = 'button';
        this.createButton.addEventListener('click', () => this._openModal());
        head.appendChild(this.createButton);
        roomsPanel.appendChild(head);

        this.hostHint = el('div', 'hint hint-warn');
        this.hostHint.style.margin = '12px 12px 0';
        this.hostHint.textContent = 'Создавать комнаты может только тот, кто запустил сервер. '
            + 'Присоединяйтесь к готовой комнате или попросите хозяина запустить run.py с ключом --guest-rooms.';
        this.hostHint.hidden = true;
        roomsPanel.appendChild(this.hostHint);

        this.roomsList = el('div', 'rooms-list scroll');
        roomsPanel.appendChild(this.roomsList);
        right.appendChild(roomsPanel);

        // Серверы сети
        const serversPanel = el('div', 'panel servers-panel');
        const sHead = el('div', 'panel-head');
        sHead.appendChild(el('h2', null, 'Другие серверы в сети'));
        serversPanel.appendChild(sHead);
        this.serversList = el('div', 'servers-list');
        serversPanel.appendChild(this.serversList);
        right.appendChild(serversPanel);

        return right;
    }

    // --- форма создания комнаты -------------------------------------------

    _buildModal() {
        const back = el('div', 'modal-back');
        back.hidden = true;
        back.addEventListener('mousedown', (e) => { if (e.target === back) this._closeModal(); });
        this.modalBack = back;

        const modal = el('div', 'panel modal');
        back.appendChild(modal);

        const head = el('div', 'panel-head');
        head.appendChild(el('h2', null, 'Новая комната'));
        modal.appendChild(head);

        const body = el('div', 'modal-body');
        const grid = el('div', 'form-grid');

        // Название
        const nameField = el('div', 'field form-wide');
        nameField.appendChild(el('div', 'label', 'Название комнаты'));
        this.roomNameInput = el('input', 'input');
        this.roomNameInput.type = 'text';
        this.roomNameInput.maxLength = 28;
        this.roomNameInput.placeholder = 'Заезд в обед';
        grid.appendChild(nameField);
        nameField.appendChild(this.roomNameInput);

        // Трасса
        const trackField = el('div', 'field form-wide');
        trackField.appendChild(el('div', 'label', 'Трасса'));
        this.trackCards = el('div', 'track-cards');
        trackField.appendChild(this.trackCards);
        grid.appendChild(trackField);

        // Круги
        const lapsField = el('div', 'field');
        lapsField.appendChild(el('div', 'label', 'Кругов'));
        this.lapsStepper = this._buildStepper(1, 9, (v) => { this.draft.laps = v; });
        lapsField.appendChild(this.lapsStepper.node);
        grid.appendChild(lapsField);

        // Максимум игроков
        const maxField = el('div', 'field');
        maxField.appendChild(el('div', 'label', 'Максимум игроков'));
        this.maxStepper = this._buildStepper(2, 8, (v) => { this.draft.max_players = v; });
        maxField.appendChild(this.maxStepper.node);
        grid.appendChild(maxField);

        // Бонусы
        const itemsField = el('div', 'field form-wide');
        const itemsHead = el('div', 'row-between');
        itemsHead.appendChild(el('div', 'label', 'Бонусы'));
        const itemsToggle = el('label', 'toggle');
        this.itemsEnabledInput = el('input');
        this.itemsEnabledInput.type = 'checkbox';
        this.itemsEnabledInput.addEventListener('change', () => {
            this.draft.items_enabled = this.itemsEnabledInput.checked;
            this._syncItemChecks();
        });
        itemsToggle.appendChild(this.itemsEnabledInput);
        itemsToggle.appendChild(el('span', 'toggle-track'));
        itemsToggle.appendChild(el('span', null, 'Включены'));
        itemsHead.appendChild(itemsToggle);
        itemsField.appendChild(itemsHead);

        const itemsGrid = el('div', 'items-grid');
        this.itemChecks = [];
        for (let i = 0; i < ITEM_DEFS.length; i++) {
            const def = ITEM_DEFS[i];
            const label = el('label', 'item-check');
            const input = el('input');
            input.type = 'checkbox';
            input.addEventListener('change', () => this._onItemToggle(def.id, input.checked));
            label.appendChild(input);
            const icon = el('span');
            icon.innerHTML = itemIconSvg(def.id, 34);
            label.appendChild(icon.firstChild);
            label.appendChild(el('span', 'nm', def.name));
            itemsGrid.appendChild(label);
            this.itemChecks.push({ def: def, label: label, input: input });
        }
        itemsField.appendChild(itemsGrid);
        grid.appendChild(itemsField);

        // Столкновения и зеркало
        const flagsField = el('div', 'field form-wide');
        const flagsRow = el('div', 'row');
        flagsRow.style.gap = '26px';

        const collLabel = el('label', 'toggle');
        this.collisionsInput = el('input');
        this.collisionsInput.type = 'checkbox';
        this.collisionsInput.addEventListener('change', () => {
            this.draft.collisions = this.collisionsInput.checked;
        });
        collLabel.appendChild(this.collisionsInput);
        collLabel.appendChild(el('span', 'toggle-track'));
        collLabel.appendChild(el('span', null, 'Столкновения машин'));
        flagsRow.appendChild(collLabel);

        const mirrorLabel = el('label', 'toggle');
        this.mirrorInput = el('input');
        this.mirrorInput.type = 'checkbox';
        this.mirrorInput.addEventListener('change', () => {
            this.draft.mirror = this.mirrorInput.checked;
        });
        mirrorLabel.appendChild(this.mirrorInput);
        mirrorLabel.appendChild(el('span', 'toggle-track'));
        mirrorLabel.appendChild(el('span', null, 'Зеркальная трасса'));
        flagsRow.appendChild(mirrorLabel);

        flagsField.appendChild(flagsRow);
        grid.appendChild(flagsField);

        body.appendChild(grid);
        modal.appendChild(body);

        const foot = el('div', 'modal-foot');
        const cancel = el('button', 'btn btn-ghost', 'Отмена');
        cancel.type = 'button';
        cancel.addEventListener('click', () => this._closeModal());
        foot.appendChild(cancel);

        const create = el('button', 'btn btn-green', 'Создать и войти');
        create.type = 'button';
        create.addEventListener('click', () => this._submitRoom());
        foot.appendChild(create);
        modal.appendChild(foot);

        return back;
    }

    _buildStepper(min, max, onChange) {
        const node = el('div', 'stepper');
        const minus = el('button', 'step', '–');
        minus.type = 'button';
        const value = el('div', 'val', String(min));
        const plus = el('button', 'step', '+');
        plus.type = 'button';
        node.appendChild(minus);
        node.appendChild(value);
        node.appendChild(plus);

        const api = {
            node: node,
            value: min,
            set(v) {
                api.value = Math.min(max, Math.max(min, v | 0));
                value.textContent = String(api.value);
                minus.disabled = api.value <= min;
                plus.disabled = api.value >= max;
                onChange(api.value);
            },
        };
        minus.addEventListener('click', () => api.set(api.value - 1));
        plus.addEventListener('click', () => api.set(api.value + 1));
        return api;
    }

    _buildTrackCards() {
        this.trackCards.innerHTML = '';
        const tracks = (this.content && this.content.tracks) || [];
        this.trackButtons = [];
        for (let i = 0; i < tracks.length; i++) {
            const track = tracks[i];
            const card = el('button', 'track-card');
            card.type = 'button';

            const art = el('div');
            art.innerHTML = trackIconSvg(track.theme, 68);
            card.appendChild(art);

            card.appendChild(el('div', 't-name', track.name || track.id));
            card.appendChild(el('div', 't-desc', track.desc || ''));

            const diff = el('div', 'diff');
            diff.appendChild(el('span', null, 'Сложность'));
            const dots = el('div', 'diff-dots');
            const level = track.difficulty || 1;
            for (let d = 1; d <= 3; d++) {
                dots.appendChild(el('span', d <= level ? 'diff-dot on' : 'diff-dot'));
            }
            diff.appendChild(dots);
            card.appendChild(diff);

            card.addEventListener('click', () => this._selectTrack(track.id));
            this.trackCards.appendChild(card);
            this.trackButtons.push({ id: track.id, node: card });
        }
        if (tracks.length && !this._hasTrack(this.draft.track)) {
            this.draft.track = tracks[0].id;
        }
        this._syncTrackCards();
    }

    _hasTrack(id) {
        const tracks = (this.content && this.content.tracks) || [];
        for (let i = 0; i < tracks.length; i++) if (tracks[i].id === id) return true;
        return false;
    }

    _selectTrack(id) {
        this.draft.track = id;
        this._syncTrackCards();
    }

    _syncTrackCards() {
        if (!this.trackButtons) return;
        for (let i = 0; i < this.trackButtons.length; i++) {
            const entry = this.trackButtons[i];
            entry.node.classList.toggle('is-active', entry.id === this.draft.track);
        }
    }

    _onItemToggle(itemId, on) {
        const list = this.draft.items;
        const idx = list.indexOf(itemId);
        if (on && idx < 0) list.push(itemId);
        else if (!on && idx >= 0) list.splice(idx, 1);
        this._syncItemChecks();
    }

    _syncItemChecks() {
        const enabled = this.draft.items_enabled;
        this.itemsEnabledInput.checked = enabled;
        for (let i = 0; i < this.itemChecks.length; i++) {
            const entry = this.itemChecks[i];
            const on = this.draft.items.indexOf(entry.def.id) >= 0;
            entry.input.checked = on;
            entry.input.disabled = !enabled;
            entry.label.classList.toggle('is-on', on && enabled);
            entry.label.classList.toggle('is-locked', !enabled);
        }
    }

    _openModal() {
        if (!this.isHost) return;
        this.draft = defaultRoomSettings();
        if (!this._hasTrack(this.draft.track)) {
            const tracks = (this.content && this.content.tracks) || [];
            if (tracks.length) this.draft.track = tracks[0].id;
        }
        this.roomNameInput.value = 'Комната: ' + this.getName();
        this.lapsStepper.set(this.draft.laps);
        this.maxStepper.set(this.draft.max_players);
        this.collisionsInput.checked = this.draft.collisions;
        this.mirrorInput.checked = this.draft.mirror;
        this._syncTrackCards();
        this._syncItemChecks();
        this.modalBack.hidden = false;
        this.roomNameInput.focus();
        this.roomNameInput.select();
    }

    _closeModal() {
        if (this.modalBack) this.modalBack.hidden = true;
    }

    _submitRoom() {
        const name = this.roomNameInput.value.trim() || ('Комната: ' + this.getName());
        // Отправляем ровно схему раздела 9: лишних полей быть не должно.
        const settings = {
            track: this.draft.track,
            laps: this.draft.laps,
            max_players: this.draft.max_players,
            items_enabled: this.draft.items_enabled,
            items: this.draft.items.slice(),
            collisions: this.draft.collisions,
            mirror: this.draft.mirror,
        };
        this._closeModal();
        if (this.handlers.onCreateRoom) this.handlers.onCreateRoom(name, settings);
    }

    _updateCreateAvailability() {
        this.createButton.disabled = !this.isHost;
        this.createButton.title = this.isHost ? ''
            : 'Сервер не дал этому клиенту права создавать комнаты';
        this.hostHint.hidden = this.isHost;
    }

    // --- отрисовка списков -------------------------------------------------

    _trackLabel(trackRef) {
        const tracks = (this.content && this.content.tracks) || [];
        for (let i = 0; i < tracks.length; i++) {
            if (tracks[i].id === trackRef) return tracks[i];
        }
        return { id: trackRef, name: trackRef, theme: 'city' };
    }

    _renderRooms() {
        const list = this.roomsList;
        list.innerHTML = '';

        if (!this.rooms.length) {
            const note = el('div', 'empty-note');
            note.textContent = this.isHost
                ? 'Комнат пока нет. Создайте первую — остальные увидят её сразу.'
                : 'Комнат пока нет. Подождите, пока хозяин сервера создаст заезд.';
            list.appendChild(note);
            return;
        }

        for (let i = 0; i < this.rooms.length; i++) {
            const room = this.rooms[i];
            const track = this._trackLabel(room.track);
            const row = el('div', 'room-row');

            row.appendChild(el('div', 'room-name', room.name || 'Комната'));

            const trackCell = el('div', 'room-track');
            trackCell.appendChild(el('span', 'theme-chip theme-' + (track.theme || 'city')));
            trackCell.appendChild(el('span', null, track.name || track.id));
            row.appendChild(trackCell);

            row.appendChild(el('div', 'room-laps num', room.laps + ' круг' + lapSuffix(room.laps)));

            const players = el('div', 'room-players num');
            const full = room.players >= room.max_players;
            players.appendChild(el('span', null, room.players + '/' + room.max_players));
            if (full) {
                const tag = el('span', 'label');
                tag.textContent = 'полно';
                players.appendChild(tag);
            }
            row.appendChild(players);

            const stateInfo = STATE_LABELS[room.state] || ['—', 'pill-lobby'];
            row.appendChild(el('div', 'pill ' + stateInfo[1], stateInfo[0]));

            const join = el('button', 'btn btn-sm', full ? 'Полно' : 'Играть');
            join.type = 'button';
            join.disabled = full;
            join.addEventListener('click', () => {
                if (this.handlers.onJoinRoom) this.handlers.onJoinRoom(room.id);
            });
            row.appendChild(join);

            list.appendChild(row);
        }
    }

    _renderServers() {
        const list = this.serversList;
        list.innerHTML = '';

        if (!this.servers.length) {
            const note = el('div', 'empty-note');
            note.style.padding = '10px 6px';
            note.textContent = 'Других серверов в сети не найдено.';
            list.appendChild(note);
            return;
        }

        for (let i = 0; i < this.servers.length; i++) {
            const server = this.servers[i];
            const card = el('a', 'server-card');
            card.href = server.url;

            const icon = el('span');
            icon.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" '
                + 'stroke="#4aa8ff" stroke-width="2.6" stroke-linecap="round">'
                + '<rect x="3" y="4" width="18" height="7" rx="2"/>'
                + '<rect x="3" y="13" width="18" height="7" rx="2"/>'
                + '<path d="M7 7.5h.01M7 16.5h.01"/></svg>';
            card.appendChild(icon.firstChild);

            const text = el('div');
            text.appendChild(el('div', 's-name', server.name || server.url));
            text.appendChild(el('div', 's-meta',
                'игроков: ' + (server.players | 0) + ' · комнат: ' + (server.rooms | 0)));
            card.appendChild(text);

            list.appendChild(card);
        }
    }

    // --- локальные настройки ----------------------------------------------

    _applyLocalSettings() {
        const s = this.settings;
        for (let i = 0; i < this.qualityButtons.length; i++) {
            this.qualityButtons[i].classList.toggle('is-active', QUALITY_PRESETS[i] === s.quality);
        }
        this.volumeInput.value = String(Math.round(s.volume * 100));
        this.volumeValue.textContent = String(Math.round(s.volume * 100));
        this.muteInput.checked = !s.muted;
        this._updateCreateAvailability();
    }

    _emitSettings() {
        if (this.handlers.onSettingsChange) this.handlers.onSettingsChange(this.settings);
    }

    _setQuality(preset) {
        this.settings = saveUiSettings({ quality: preset });
        this._applyLocalSettings();
        this._emitSettings();
    }

    _setVolume(value) {
        this.settings = saveUiSettings({ volume: value });
        this.volumeValue.textContent = String(Math.round(value * 100));
        this._emitSettings();
    }

    _setMuted(soundOn) {
        this.settings = saveUiSettings({ muted: !soundOn });
        this._emitSettings();
    }

    _onNameCommit() {
        const name = this.getName();
        this.nameInput.value = name;
        this.settings = saveUiSettings({ name: name });
        if (this.handlers.onNameChange) this.handlers.onNameChange(name);
    }
}

/** «1 круг», «3 круга», «5 кругов» — окончание по числу. */
function lapSuffix(n) {
    const mod100 = n % 100;
    if (mod100 >= 11 && mod100 <= 14) return 'ов';
    const mod10 = n % 10;
    if (mod10 === 1) return '';
    if (mod10 >= 2 && mod10 <= 4) return 'а';
    return 'ов';
}
