/*
 * HUD гонки: скорость, тахометр, круг, позиция, времена, слот бонуса,
 * заряд дрифта, мини-карта, лента событий, таблица позиций по Tab,
 * предупреждение о движении не в ту сторону и глобальная пауза.
 *
 * ЭТО САМЫЙ ГОРЯЧИЙ КОД ИНТЕРФЕЙСА: update() зовётся каждый кадр рядом с
 * рендером, бюджет — десятые доли миллисекунды. Отсюда правила, которым
 * подчинён весь файл:
 *
 *   1. Все узлы создаются один раз (конструктор и setupRace). В update()
 *      не создаётся ни одного элемента и не трогается innerHTML.
 *   2. Меняются только textContent, className, style.transform, style.opacity
 *      и атрибут transform у SVG — ничего, что вызывает layout соседей.
 *   3. Каждое значение сравнивается с закэшированным прошлым: если число не
 *      изменилось, DOM не трогается вовсе.
 *   4. Ноль аллокаций в кадре: никаких шаблонных строк, конкатенаций, .map,
 *      литералов и замыканий. Все строки, которые могут понадобиться
 *      (числа 0..999, «00».."99", transform-строки), посчитаны заранее
 *      в таблицах на этапе загрузки модуля.
 *   5. Мини-карта — два маленьких canvas 2D друг над другом: контур трассы
 *      рисуется один раз при старте гонки, каждый кадр перерисовываются
 *      только точки машин на верхнем канвасе.
 *
 * Использование из main.js:
 *
 *   import { Hud, createHudState } from './ui/hud.js';
 *
 *   const hud = new Hud(document.getElementById('screen-hud'));
 *   const hudState = createHudState();        // один объект на всю гонку
 *
 *   // на событие race_init:
 *   hud.setupRace(msg, mySlot);
 *   hud.show();
 *
 *   // каждый кадр: заполнить поля hudState из снапшота и локального
 *   // предсказания, затем один вызов
 *   hud.update(hudState);
 *
 *   // на событие race_event:
 *   hud.pushRaceEvent(msg);
 *
 *   // на событие pause: 'running' | 'paused' | 'resuming'
 *   hud.setPause(msg.phase, msg.name);
 *
 * ПАУЗА. Клавиша P и кнопка в HUD — оба пути зовут один обработчик
 * onTogglePause(wantPause) из options. Сам HUD состояние паузы не хранит:
 * плашку он рисует по событию сервера, а не по нажатию, иначе у игроков
 * разъедется картинка. Плашка и кнопка живут вне кадрового цикла.
 */

import { ITEM_DEFS, ITEM_BY_ID, ITEM_BY_KIND, itemIconSvg } from './menu.js';

// ---------------------------------------------------------------------------
// Предрасчитанные таблицы строк. Всё, что может попасть в textContent или
// в transform внутри кадра, лежит здесь готовой строкой.
// ---------------------------------------------------------------------------

const INT_STR = new Array(1000);          // "0".."999"
for (let i = 0; i < 1000; i++) INT_STR[i] = String(i);

const PAD2 = new Array(100);              // "00".."99"
for (let i = 0; i < 100; i++) PAD2[i] = i < 10 ? '0' + i : String(i);

// transform для полосы заряда дрифта: 101 шаг
const SCALE_X = new Array(101);
for (let i = 0; i <= 100; i++) SCALE_X[i] = 'scaleX(' + (i / 100) + ')';

// Геометрия дуги тахометра (совпадает с path в разметке ниже)
const TACH_CX = 120;
const TACH_CY = 106;
const TACH_R = 84;
const TACH_SWEEP = 180;                   // градусов, от -90 до +90 (полукруг)
const TACH_LEN = 2 * Math.PI * TACH_R * (TACH_SWEEP / 360);

// stroke-dashoffset заполненной части дуги: 101 шаг
const DASH_OFF = new Array(101);
for (let i = 0; i <= 100; i++) DASH_OFF[i] = String(TACH_LEN * (1 - i / 100));

// transform стрелки: 201 шаг по одному градусу
const NEEDLE_ROT = new Array(TACH_SWEEP + 1);
for (let i = 0; i <= TACH_SWEEP; i++) {
    NEEDLE_ROT[i] = 'rotate(' + (i - TACH_SWEEP / 2) + ' ' + TACH_CX + ' ' + TACH_CY + ')';
}

const SPEED_MAX_KMH = 230;                // потолок шкалы по умолчанию: буст 58 м/с + запас
const MS_TO_KMH = 3.6;
const TAU = Math.PI * 2;

const MAX_SLOTS = 8;
const FEED_ROWS = 5;                      // одновременно видимых строк ленты
const FEED_LIFETIME = 4200;               // мс жизни строки
const STANDINGS_PERIOD = 125;             // мс между пересчётами таблицы Tab

// Пороги уровней заряда дрифта (раздел 6.3)
const DRIFT_L3 = 2.4;

// Флаги машины из снапшота (раздел 5.3) — нужны только эти три.
const FLAG_SHIELD = 1 << 4;
const FLAG_FINISHED = 1 << 5;
const FLAG_GHOST = 1 << 6;

// Готовые строки статуса для таблицы позиций: в кадре только присваиваются.
const STATUS_TEXT = ['', 'финиш', 'вышел', 'щит'];
const STATUS_CLASS = ['st-gap', 'st-gap fin', 'st-gap out', 'st-gap'];

// --- иконки ленты событий --------------------------------------------------

function feedIcon(color, body) {
    return '<svg class="fi" viewBox="0 0 24 24" fill="none" stroke="' + color
        + '" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">' + body + '</svg>';
}

const FEED_ICONS = {
    lap: feedIcon('#4aa8ff', '<path d="M12 4a8 8 0 1 1-7 4"/><path d="M5 3v5h5"/>'),
    finish: feedIcon('#ffc93c', '<path d="M5 21V4"/><path d="M5 5h14l-3 4 3 4H5z"/>'),
    hit: feedIcon('#ff5a5f', '<path d="m4 4 16 16M20 4 4 20"/>'),
    shield: feedIcon('#4aa8ff', '<path d="M12 3 19 6v6c0 4-3 7-7 8-4-1-7-4-7-8V6z"/>'),
    pickup: feedIcon('#3ddc84', '<path d="M12 3v18M3 12h18"/>'),
    use: feedIcon('#ff9b3d', '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>'),
    drift: feedIcon('#b06bff', '<path d="M4 17c6 0 6-10 12-10"/><path d="M14 4h3v3"/>'),
    kill: feedIcon('#3ddc84', '<path d="m5 13 4 4 10-10"/>'),
};

function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
}

/**
 * Пустой объект состояния HUD. main.js заводит его один раз и каждый кадр
 * перезаписывает поля — новых объектов в кадре не появляется.
 *
 * Поля:
 *   speed        м/с, модуль скорости своей машины
 *   lap          текущий круг, 1..laps
 *   laps         всего кругов; 0 — оставить значение из race_init
 *   place        место, 1..8
 *   total        машин в гонке; 0 — оставить значение из race_init
 *   lapTime      с, время текущего круга
 *   bestLap      с, лучший круг, 0 — ещё нет
 *   gap          с, отставание от лидера, 0 у лидера
 *   item         код бонуса в руках: 0 — пусто, иначе kind 1..5 (раздел 8)
 *   itemReady    бонус готов к применению (пульсация слота)
 *   driftCharge  с, накопленный заряд дрифта
 *   driftLevel   0..3, уровень заряда для цвета полосы
 *   wrongWay     едет против направления трассы
 *   carCount, carSlot, carX, carZ  — из снапшота, раздел 12.3, для мини-карты
 *   carPlace, carLap, carFlags     — из снапшота, для таблицы по Tab
 */
export function createHudState() {
    return {
        speed: 0,
        lap: 1, laps: 0,
        place: 1, total: 0,
        lapTime: 0, bestLap: 0, gap: 0,
        item: 0, itemReady: false,
        driftCharge: 0, driftLevel: 0,
        wrongWay: false,
        carCount: 0,
        carSlot: new Uint8Array(MAX_SLOTS),
        carX: new Float32Array(MAX_SLOTS),
        carZ: new Float32Array(MAX_SLOTS),
        carPlace: new Uint8Array(MAX_SLOTS),
        carLap: new Uint8Array(MAX_SLOTS),
        carFlags: new Uint8Array(MAX_SLOTS),
    };
}

/**
 * Поле времени вида M:SS.CC. Разбито на три отдельных текстовых узла —
 * минуты, секунды и сотые обновляются независимо, каждый берёт готовую
 * строку из таблицы. Ни одной конкатенации в кадре.
 */
class TimeField {
    constructor(parent, cls) {
        const wrap = el('span', cls);
        this.m = el('span', null, '0');
        this.s = el('span', null, '00');
        this.cs = el('span', 'cs', '00');
        wrap.appendChild(this.m);
        wrap.appendChild(el('span', null, ':'));
        wrap.appendChild(this.s);
        wrap.appendChild(el('span', null, '.'));
        wrap.appendChild(this.cs);
        this.node = wrap;
        this.lastM = 0;
        this.lastS = 0;
        this.lastCs = 0;
    }

    set(seconds) {
        let hundredths = seconds > 0 ? (seconds * 100 + 0.5) | 0 : 0;
        if (hundredths > 5999999) hundredths = 5999999;
        const cs = hundredths % 100;
        const totalSec = (hundredths / 100) | 0;
        const s = totalSec % 60;
        let m = (totalSec / 60) | 0;
        if (m > 99) m = 99;

        if (cs !== this.lastCs) { this.cs.textContent = PAD2[cs]; this.lastCs = cs; }
        if (s !== this.lastS) { this.s.textContent = PAD2[s]; this.lastS = s; }
        if (m !== this.lastM) { this.m.textContent = INT_STR[m]; this.lastM = m; }
    }

    reset() {
        this.lastM = -1; this.lastS = -1; this.lastCs = -1;
        this.set(0);
    }
}

// ---------------------------------------------------------------------------

export class Hud {
    /**
     * @param {HTMLElement} root корень слоя HUD (#screen-hud)
     * @param {object} [options] {bindKeys: true} — самому слушать Tab и P;
     *        {onTogglePause(want)} — нажали P или кнопку паузы
     */
    constructor(root, options) {
        this.root = root;
        const opts = options || {};

        this.localSlot = -1;
        this.speedMaxKmh = SPEED_MAX_KMH;
        this.mapScale = 1;
        this.mapOffX = 0;
        this.mapOffY = 0;
        this.mapReady = false;

        // Имена и цвета по слотам — заполняются в setupRace, в кадре только читаются.
        this.slotColors = new Array(MAX_SLOTS);
        this.slotNames = new Array(MAX_SLOTS);
        this.slotActive = new Uint8Array(MAX_SLOTS);
        for (let i = 0; i < MAX_SLOTS; i++) {
            this.slotColors[i] = '#8899bb';
            this.slotNames[i] = '';
        }

        // Порядок мест для таблицы Tab: переиспользуемый массив индексов.
        this.standingsOrder = new Int8Array(MAX_SLOTS);

        this._resetCache();
        this._build();

        this.standingsVisible = false;
        this.standingsNextUpdate = 0;

        this.onTogglePause = opts.onTogglePause || null;
        this.pausePhase = 'running';

        this.feedHead = 0;
        this.feedNextExpiry = Infinity;

        if (opts.bindKeys !== false) this._bindKeys();
        this.hide();
    }

    // --- жизненный цикл ----------------------------------------------------

    show() { this.root.hidden = false; this.visible = true; }

    hide() {
        this.root.hidden = true;
        this.visible = false;
        this.setStandingsVisible(false);
        this.setPause('running', '');
    }

    /**
     * Событие `pause` (§9). Фазы: `paused` — стоим, плашка на весь экран;
     * `resuming` — идёт обратный отсчёт, его рисует оверлей отсчёта, плашка
     * уходит; `running` — едем.
     */
    setPause(phase, name) {
        const want = phase === 'paused' || phase === 'resuming' ? phase : 'running';
        if (want === this.pausePhase) return;
        this.pausePhase = want;
        this.pauseNode.hidden = want !== 'paused';
        if (want === 'paused') {
            this.pauseWho.textContent = name ? 'поставил ' + name : 'поставлена';
        }
        this.pauseButton.textContent = want === 'paused' ? 'Продолжить · P' : 'Пауза · P';
        this.pauseButton.className = want === 'paused' ? 'hud-pause-btn on' : 'hud-pause-btn';
    }

    /**
     * Нажали P или кнопку: просим сервер поставить или снять паузу.
     * Во время отсчёта снятия (`resuming`) нажатие означает «стоп, обратно на
     * паузу»: передумать до того, как машины поедут, — обычное дело.
     */
    togglePause() {
        if (!this.visible || !this.onTogglePause) return;
        this.onTogglePause(this.pausePhase !== 'paused');
    }

    /**
     * Подготовка к гонке по событию `race_init` (раздел 9 и формат 12.1).
     * Здесь строится контур мини-карты и подписи таблицы позиций — всё,
     * что потом в кадре только читается.
     */
    /**
     * Собственный слот игрока. Раздел 12.6 требует этот метод от всех
     * модулей, принимающих слот; у Hud его не было, и клиенту
     * приходилось писать поле напрямую.
     */
    setLocalSlot(slot) {
        this.localSlot = slot === undefined || slot === null ? -1 : slot | 0;
    }

    setupRace(raceInit, localSlot) {
        this.localSlot = localSlot === undefined || localSlot === null ? -1 : localSlot | 0;

        const players = raceInit.players || [];
        for (let i = 0; i < MAX_SLOTS; i++) {
            this.slotColors[i] = '#8899bb';
            this.slotNames[i] = '';
            this.slotActive[i] = 0;
        }
        for (let i = 0; i < players.length; i++) {
            const slot = players[i].slot | 0;
            if (slot < 0 || slot >= MAX_SLOTS) continue;
            this.slotColors[slot] = players[i].color || '#8899bb';
            this.slotNames[slot] = players[i].name || 'Гонщик';
            this.slotActive[slot] = 1;
        }

        this._buildStandingsRows(players);
        this._drawTrackOutline(raceInit.track);

        this.reset();
        // Значения из race_init становятся текущими; update() перезапишет их
        // только если main.js явно положит в state.laps / state.total > 0.
        this.cLaps = Math.min(999, raceInit.laps | 0);
        this.cTotal = Math.min(999, players.length);
        this.lapsTotal.textContent = INT_STR[this.cLaps];
        this.placeTotal.textContent = INT_STR[this.cTotal];
    }

    /**
     * Потолок шкалы тахометра, м/с. Стоит выставить по boost_speed выбранной
     * машины из cars.json, чтобы стрелка доходила до конца именно на бусте.
     */
    setSpeedScale(maxSpeedMs) {
        const kmh = maxSpeedMs * MS_TO_KMH;
        this.speedMaxKmh = kmh > 20 ? kmh : SPEED_MAX_KMH;
        this.cSpeedKmh = -1;              // пересчитать дугу и стрелку на следующем кадре
    }

    /** Сбросить кэш и показания между заездами. */
    reset() {
        this._resetCache();
        this.lapTime.reset();
        this.bestTime.reset();
        this.speedValue.textContent = '0';
        this.lapValue.textContent = '1';
        this.placeValue.textContent = '1';
        this.gapSeconds.textContent = '0';
        this.gapCs.textContent = '00';
        this.driftBox.className = 'hud-box hud-drift';
        this.driftFill.style.transform = SCALE_X[0];
        this.wrongWayNode.className = 'hud-wrongway';
        this._showItem(0, false);
        this._clearFeed();
        this.setPause('running', '');
    }

    _resetCache() {
        this.cSpeedKmh = -1;
        this.cTachStep = -1;
        this.cNeedle = -1;
        this.cTachHot = -1;
        this.cLap = -1;
        this.cLaps = -1;
        this.cPlace = -1;
        this.cTotal = -1;
        this.cItem = -1;
        this.cItemReady = -1;
        this.cDriftStep = -1;
        this.cDriftLevel = -1;
        this.cDriftOn = -1;
        this.cBest = -1;
        this.cGapSec = -1;
        this.cGapCs = -1;
        this.cGapLeader = -1;
        this.cWrongWay = -1;
    }

    // =======================================================================
    // КАДРОВЫЙ ПУТЬ. Ниже — всё, что зовётся 60 раз в секунду.
    // =======================================================================

    /**
     * Один вызов на кадр. Разделён на часто меняющееся (скорость, тахометр,
     * время круга, отставание, дрифт, мини-карта) и редко меняющееся
     * (круг, позиция, лучший круг, бонус, предупреждение) — второе почти
     * всегда выходит по сравнению с кэшем, не трогая DOM.
     */
    update(state) {
        // --- часто меняющееся ---------------------------------------------
        this._updateSpeed(state.speed);
        this._updateLapTime(state.lapTime);
        this._updateGap(state.gap);
        this._updateDrift(state.driftCharge, state.driftLevel);
        if (this.mapReady) this._updateMinimap(state);

        // --- редко меняющееся: ранний выход по кэшу ------------------------
        const lap = state.lap | 0;
        if (lap !== this.cLap) {
            this.cLap = lap;
            this.lapValue.textContent = INT_STR[lap < 0 ? 0 : (lap > 999 ? 999 : lap)];
        }

        const laps = state.laps | 0;
        if (laps > 0 && laps !== this.cLaps) {
            this.cLaps = laps;
            this.lapsTotal.textContent = INT_STR[laps > 999 ? 999 : laps];
        }

        const total = state.total | 0;
        if (total > 0 && total !== this.cTotal) {
            this.cTotal = total;
            this.placeTotal.textContent = INT_STR[total > 999 ? 999 : total];
        }

        const place = state.place | 0;
        if (place !== this.cPlace) {
            this.cPlace = place;
            this.placeValue.textContent = INT_STR[place < 0 ? 0 : (place > 999 ? 999 : place)];
            this.placeBox.className = place === 1 ? 'hud-stat place p1'
                : place === 2 ? 'hud-stat place p2'
                : place === 3 ? 'hud-stat place p3' : 'hud-stat place';
        }

        const best = state.bestLap;
        if (best !== this.cBest) {
            this.cBest = best;
            this.bestTime.set(best);
        }

        const item = state.item | 0;
        const ready = state.itemReady ? 1 : 0;
        if (item !== this.cItem || ready !== this.cItemReady) {
            this.cItem = item;
            this.cItemReady = ready;
            this._showItem(item, ready === 1);
        }

        const wrong = state.wrongWay ? 1 : 0;
        if (wrong !== this.cWrongWay) {
            this.cWrongWay = wrong;
            this.wrongWayNode.className = wrong ? 'hud-wrongway on' : 'hud-wrongway';
        }

        // --- лента и таблица: по таймеру, не каждый кадр -------------------
        const now = performance.now();
        if (now >= this.feedNextExpiry) this._expireFeed(now);
        if (this.standingsVisible && now >= this.standingsNextUpdate) {
            this.standingsNextUpdate = now + STANDINGS_PERIOD;
            this._updateStandings(state);
        }
    }

    _updateSpeed(speed) {
        let kmh = speed * MS_TO_KMH + 0.5;
        kmh = kmh > 0 ? kmh | 0 : 0;
        if (kmh > 999) kmh = 999;

        if (kmh !== this.cSpeedKmh) {
            this.cSpeedKmh = kmh;
            this.speedValue.textContent = INT_STR[kmh];

            // Дуга: 101 шаг, стрелка: 201 шаг. Пересчёт только при смене шага.
            let norm = kmh / this.speedMaxKmh;
            if (norm > 1) norm = 1;

            const step = (norm * 100 + 0.5) | 0;
            if (step !== this.cTachStep) {
                this.cTachStep = step;
                this.tachFill.style.strokeDashoffset = DASH_OFF[step];
                const hot = step >= 80 ? 1 : 0;
                if (hot !== this.cTachHot) {
                    this.cTachHot = hot;
                    this.tachFill.style.stroke = hot ? '#ff5a5f' : '#ffc93c';
                }
            }

            const angle = (norm * TACH_SWEEP + 0.5) | 0;
            if (angle !== this.cNeedle) {
                this.cNeedle = angle;
                this.needle.setAttribute('transform', NEEDLE_ROT[angle]);
            }
        }
    }

    _updateLapTime(seconds) {
        this.lapTime.set(seconds);
    }

    _updateGap(gap) {
        const leader = gap <= 0.0005 ? 1 : 0;
        if (leader !== this.cGapLeader) {
            this.cGapLeader = leader;
            this.gapRow.className = leader ? 'hud-time-row small gap leader' : 'hud-time-row small gap';
            this.gapSign.textContent = leader ? '' : '+';
        }
        if (leader) {
            if (this.cGapSec !== 0) { this.cGapSec = 0; this.gapSeconds.textContent = '0'; }
            if (this.cGapCs !== 0) { this.cGapCs = 0; this.gapCs.textContent = '00'; }
            return;
        }
        let hundredths = (gap * 100 + 0.5) | 0;
        if (hundredths > 99999) hundredths = 99999;
        const cs = hundredths % 100;
        const sec = (hundredths / 100) | 0;
        if (sec !== this.cGapSec) { this.cGapSec = sec; this.gapSeconds.textContent = INT_STR[sec]; }
        if (cs !== this.cGapCs) { this.cGapCs = cs; this.gapCs.textContent = PAD2[cs]; }
    }

    _updateDrift(charge, level) {
        const on = charge > 0.02 ? 1 : 0;
        let norm = charge / DRIFT_L3;
        if (norm > 1) norm = 1;
        if (norm < 0) norm = 0;
        const step = (norm * 100 + 0.5) | 0;

        if (step !== this.cDriftStep) {
            this.cDriftStep = step;
            this.driftFill.style.transform = SCALE_X[step];
        }
        // Уровень определяет цвет; ниже первого порога полосу прячем.
        const lv = level | 0;
        if (on !== this.cDriftOn || lv !== this.cDriftLevel) {
            this.cDriftOn = on;
            this.cDriftLevel = lv;
            this.driftBox.className = on
                ? (lv >= 3 ? 'hud-box hud-drift on lv3'
                    : lv >= 2 ? 'hud-box hud-drift on lv2' : 'hud-box hud-drift on')
                : 'hud-box hud-drift';
        }
    }

    /**
     * Перерисовка точек машин. Контур трассы лежит на нижнем канвасе и не
     * трогается — здесь только clearRect и carCount кружков.
     */
    _updateMinimap(state) {
        const ctx = this.dotsCtx;
        const size = this.mapPixels;
        ctx.clearRect(0, 0, size, size);

        const count = state.carCount | 0;
        const scale = this.mapScale;
        const offX = this.mapOffX;
        const offY = this.mapOffY;
        const local = this.localSlot;

        // Первый проход — тёмная обводка под всеми точками.
        ctx.fillStyle = '#080a12';
        for (let i = 0; i < count; i++) {
            ctx.beginPath();
            ctx.arc(state.carX[i] * scale + offX, state.carZ[i] * scale + offY, 6.2, 0, TAU);
            ctx.fill();
        }
        // Второй проход — цвета машин.
        for (let i = 0; i < count; i++) {
            const slot = state.carSlot[i];
            ctx.fillStyle = this.slotColors[slot];
            ctx.beginPath();
            ctx.arc(state.carX[i] * scale + offX, state.carZ[i] * scale + offY,
                slot === local ? 4.6 : 3.8, 0, TAU);
            ctx.fill();
        }
        // Своя машина получает белое кольцо, чтобы находиться взглядом мгновенно.
        for (let i = 0; i < count; i++) {
            if (state.carSlot[i] !== local) continue;
            ctx.strokeStyle = '#ffffff';
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.arc(state.carX[i] * scale + offX, state.carZ[i] * scale + offY, 7.6, 0, TAU);
            ctx.stroke();
            break;
        }
    }

    // =======================================================================
    // ВНЕ КАДРА: события, таблица, построение разметки
    // =======================================================================

    /**
     * Строка в ленту событий по событию `race_event` (раздел 9).
     * Зовётся из обработчика WebSocket, не из кадрового цикла.
     */
    pushRaceEvent(ev) {
        const kind = ev.kind;
        const slot = ev.slot;
        const mine = slot === this.localSlot;
        let text = '';
        let icon = FEED_ICONS.pickup;
        let tone = '';

        if (kind === 'pickup') {
            if (!mine) return;
            const def = ITEM_BY_ID[ev.item];
            text = 'Бонус: ' + (def ? def.name : ev.item);
            icon = FEED_ICONS.pickup;
            tone = 'good';
        } else if (kind === 'use') {
            if (!mine) return;
            const def = ITEM_BY_ID[ev.item];
            text = 'Применено: ' + (def ? def.name : ev.item);
            icon = FEED_ICONS.use;
            tone = 'warn';
        } else if (kind === 'hit') {
            const def = ITEM_BY_ID[ev.item];
            const instr = def ? def.instr : ev.item;
            if (mine) {
                text = ev.blocked ? 'Щит поглотил удар' : 'Вас задело ' + instr;
                icon = ev.blocked ? FEED_ICONS.shield : FEED_ICONS.hit;
                tone = ev.blocked ? '' : 'bad';
            } else if (ev.by === this.localSlot) {
                text = (ev.blocked ? 'Щит у ' : 'Попадание: ') + this._nameOf(slot);
                icon = ev.blocked ? FEED_ICONS.shield : FEED_ICONS.kill;
                tone = ev.blocked ? '' : 'good';
            } else {
                return;
            }
        } else if (kind === 'lap') {
            if (!mine) return;
            text = 'Круг ' + ev.lap + ' — ' + formatSeconds(ev.time);
            icon = FEED_ICONS.lap;
            tone = (ev.best !== undefined && ev.time <= ev.best + 1e-6) ? 'good' : '';
        } else if (kind === 'finish') {
            text = mine
                ? 'Финиш — ' + ev.place + ' место'
                : this._nameOf(slot) + ' финишировал — ' + ev.place + ' место';
            icon = FEED_ICONS.finish;
            tone = mine ? 'warn' : '';
        } else if (kind === 'drift_boost') {
            if (!mine) return;
            text = 'Дрифт-буст ×' + ev.level;
            icon = FEED_ICONS.drift;
            tone = 'good';
        } else {
            return;
        }

        this._pushFeedRow(icon, text, tone);
    }

    /** Показать или спрятать таблицу позиций (клавиша Tab). */
    setStandingsVisible(on) {
        const want = !!on;
        if (want === this.standingsVisible) return;
        this.standingsVisible = want;
        this.standingsNode.className = want ? 'hud-standings on' : 'hud-standings';
        this.standingsNode.hidden = !want;
        this.standingsNextUpdate = 0;
    }

    _nameOf(slot) {
        if (slot === undefined || slot < 0 || slot >= MAX_SLOTS) return 'Гонщик';
        return this.slotNames[slot] || 'Гонщик';
    }

    _bindKeys() {
        // Tab по коду клавиши, не по символу: раскладка значения не имеет (10.4).
        this._onKeyDown = (e) => {
            if (e.code === 'KeyP') {
                // P — глобальная пауза (доработка «офисная игра»). В поле
                // ввода P остаётся буквой.
                const node = e.target;
                const tag = node && node.tagName;
                if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
                if (node && node.isContentEditable) return;
                if (!this.visible || e.repeat) return;
                e.preventDefault();
                this.togglePause();
                return;
            }
            if (e.code !== 'Tab' || !this.visible) return;
            e.preventDefault();
            if (!this.standingsVisible) this.setStandingsVisible(true);
        };
        this._onKeyUp = (e) => {
            // Пока таблица не показана, Tab остаётся обычной навигацией по форме.
            if (e.code !== 'Tab' || !this.standingsVisible) return;
            e.preventDefault();
            this.setStandingsVisible(false);
        };
        // Уход со страницы с зажатым Tab не должен оставлять таблицу висеть.
        this._onBlur = () => this.setStandingsVisible(false);
        window.addEventListener('keydown', this._onKeyDown);
        window.addEventListener('keyup', this._onKeyUp);
        window.addEventListener('blur', this._onBlur);
    }

    // --- лента -------------------------------------------------------------

    _pushFeedRow(iconSvg, text, tone) {
        const row = this.feedRows[this.feedHead];
        this.feedHead = (this.feedHead + 1) % FEED_ROWS;

        row.icon.innerHTML = iconSvg;
        row.text.textContent = text;
        row.node.className = 'feed-row ' + tone;
        // Перезапуск анимации появления
        void row.node.offsetWidth;
        row.node.className = 'feed-row show ' + tone;
        row.expiry = performance.now() + FEED_LIFETIME;
        if (row.expiry < this.feedNextExpiry) this.feedNextExpiry = row.expiry;
        // Свежая строка всегда снизу списка
        this.feedNode.appendChild(row.node);
    }

    _expireFeed(now) {
        let next = Infinity;
        for (let i = 0; i < FEED_ROWS; i++) {
            const row = this.feedRows[i];
            if (row.expiry === 0) continue;
            if (now >= row.expiry) {
                row.expiry = 0;
                row.node.className = 'feed-row';
            } else if (row.expiry < next) {
                next = row.expiry;
            }
        }
        this.feedNextExpiry = next;
    }

    _clearFeed() {
        for (let i = 0; i < FEED_ROWS; i++) {
            this.feedRows[i].expiry = 0;
            this.feedRows[i].node.className = 'feed-row';
        }
        this.feedNextExpiry = Infinity;
        this.feedHead = 0;
    }

    // --- таблица позиций ---------------------------------------------------

    _updateStandings(state) {
        const count = state.carCount | 0;
        const order = this.standingsOrder;
        for (let i = 0; i < MAX_SLOTS; i++) order[i] = -1;

        // Раскладываем индексы машин по местам: place из снапшота, 1..8.
        let overflow = 0;
        for (let i = 0; i < count; i++) {
            const place = state.carPlace[i] - 1;
            if (place >= 0 && place < MAX_SLOTS && order[place] < 0) order[place] = i;
            else overflow++;
        }
        if (overflow) {
            // Сервер прислал повторяющиеся места — заполняем дыры по порядку.
            for (let i = 0; i < count; i++) {
                let seen = false;
                for (let p = 0; p < MAX_SLOTS; p++) if (order[p] === i) { seen = true; break; }
                if (seen) continue;
                for (let p = 0; p < MAX_SLOTS; p++) if (order[p] < 0) { order[p] = i; break; }
            }
        }

        let shown = 0;
        for (let p = 0; p < MAX_SLOTS; p++) {
            const row = this.standingsRows[p];
            const idx = order[p];
            if (idx < 0) { row.node.hidden = true; continue; }
            const slot = state.carSlot[idx];
            row.node.hidden = false;
            shown++;
            if (row.cachedSlot !== slot) {
                row.cachedSlot = slot;
                row.name.textContent = this._nameOf(slot);
                row.color.style.background = this.slotColors[slot];
                row.node.className = slot === this.localSlot ? 'st-row is-me' : 'st-row';
            }
            const lap = state.carLap[idx] + 1;
            if (row.cachedLap !== lap) {
                row.cachedLap = lap;
                row.lap.textContent = INT_STR[lap > 999 ? 999 : lap];
            }
            // Статус из флагов снапшота (раздел 5.3): финиш, призрак, щит.
            const flags = state.carFlags ? state.carFlags[idx] : 0;
            const status = (flags & FLAG_FINISHED) ? 1
                : (flags & FLAG_GHOST) ? 2
                : (flags & FLAG_SHIELD) ? 3 : 0;
            if (row.cachedStatus !== status) {
                row.cachedStatus = status;
                row.gap.textContent = STATUS_TEXT[status];
                row.gap.className = STATUS_CLASS[status];
            }
        }
        if (this.standingsCount !== shown) {
            this.standingsCount = shown;
            this.standingsTotal.textContent = INT_STR[shown];
        }
    }

    _buildStandingsRows(players) {
        for (let p = 0; p < MAX_SLOTS; p++) {
            const row = this.standingsRows[p];
            row.cachedSlot = -1;
            row.cachedLap = -1;
            row.cachedStatus = -1;
            row.node.hidden = p >= players.length;
        }
        this.standingsCount = -1;
    }

    // --- мини-карта: контур трассы ----------------------------------------

    /**
     * Контур трассы рисуется один раз из плоских массивов формата 12.1.
     * В кадре этот канвас больше не трогается.
     */
    _drawTrackOutline(track) {
        this.mapReady = false;
        if (!track || !track.count || !track.x || !track.z) return;

        const count = track.count | 0;
        const xs = track.x;
        const zs = track.z;
        const hws = track.hw;

        let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
        let hwSum = 0;
        for (let i = 0; i < count; i++) {
            const x = xs[i], z = zs[i];
            if (x < minX) minX = x;
            if (x > maxX) maxX = x;
            if (z < minZ) minZ = z;
            if (z > maxZ) maxZ = z;
            hwSum += hws ? hws[i] : 7;
        }
        const hwAvg = hwSum / count;

        // Канвасы под свой CSS-размер; плотность пикселей ограничена как и рендер.
        const cssSize = this.mapStatic.clientWidth || 166;
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const pixels = Math.round(cssSize * dpr);
        this.mapPixels = pixels;
        this.mapStatic.width = pixels;
        this.mapStatic.height = pixels;
        this.mapDots.width = pixels;
        this.mapDots.height = pixels;

        const pad = 14 * dpr;
        const spanX = Math.max(maxX - minX, 1e-3);
        const spanZ = Math.max(maxZ - minZ, 1e-3);
        const scale = (pixels - pad * 2) / Math.max(spanX, spanZ);
        this.mapScale = scale;
        this.mapOffX = pixels / 2 - (minX + maxX) * 0.5 * scale;
        this.mapOffY = pixels / 2 - (minZ + maxZ) * 0.5 * scale;

        const ctx = this.staticCtx;
        ctx.clearRect(0, 0, pixels, pixels);

        const widthPx = Math.max(7 * dpr, Math.min(16 * dpr, hwAvg * 2 * scale));

        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(xs[0] * scale + this.mapOffX, zs[0] * scale + this.mapOffY);
        for (let i = 1; i < count; i++) {
            ctx.lineTo(xs[i] * scale + this.mapOffX, zs[i] * scale + this.mapOffY);
        }
        ctx.closePath();

        ctx.strokeStyle = '#080a12';
        ctx.lineWidth = widthPx + 5 * dpr;
        ctx.stroke();
        ctx.strokeStyle = '#57649a';
        ctx.lineWidth = widthPx;
        ctx.stroke();

        // Линия старта поперёк полотна
        const nx = track.nx ? track.nx[0] : 1;
        const nz = track.nz ? track.nz[0] : 0;
        const hw0 = (hws ? hws[0] : hwAvg) * scale + 2 * dpr;
        const sx = xs[0] * scale + this.mapOffX;
        const sy = zs[0] * scale + this.mapOffY;
        ctx.strokeStyle = '#ffc93c';
        ctx.lineWidth = 3 * dpr;
        ctx.beginPath();
        ctx.moveTo(sx - nx * hw0, sy - nz * hw0);
        ctx.lineTo(sx + nx * hw0, sy + nz * hw0);
        ctx.stroke();

        this.mapReady = true;
    }

    // --- слот бонуса -------------------------------------------------------

    _showItem(kind, ready) {
        for (let i = 0; i < this.itemIcons.length; i++) {
            this.itemIcons[i].node.hidden = this.itemIcons[i].kind !== kind;
        }
        this.itemEmpty.hidden = kind !== 0;
        this.itemBox.className = kind !== 0 && ready ? 'hud-box hud-item ready' : 'hud-box hud-item';
        this.itemHint.hidden = kind === 0;
    }

    // --- разметка ----------------------------------------------------------

    _build() {
        const root = this.root;
        root.innerHTML = '';
        const wrap = el('div', 'hud-root');
        root.appendChild(wrap);

        // Круг и позиция
        const topLeft = el('div', 'hud-box hud-topleft');
        const lapBox = el('div', 'hud-stat');
        lapBox.appendChild(el('div', 'k', 'Круг'));
        const lapValueWrap = el('div', 'v');
        this.lapValue = el('span', null, '1');
        this.lapsTotal = el('span', 'sub', '3');
        lapValueWrap.appendChild(this.lapValue);
        lapValueWrap.appendChild(el('span', 'sub', '/'));
        lapValueWrap.appendChild(this.lapsTotal);
        lapBox.appendChild(lapValueWrap);
        topLeft.appendChild(lapBox);

        this.placeBox = el('div', 'hud-stat place');
        this.placeBox.appendChild(el('div', 'k', 'Место'));
        const placeWrap = el('div', 'v');
        this.placeValue = el('span', null, '1');
        this.placeTotal = el('span', 'sub', '8');
        placeWrap.appendChild(this.placeValue);
        placeWrap.appendChild(el('span', 'sub', '/'));
        placeWrap.appendChild(this.placeTotal);
        this.placeBox.appendChild(placeWrap);
        topLeft.appendChild(this.placeBox);
        wrap.appendChild(topLeft);

        // Времена
        const times = el('div', 'hud-box hud-times');

        const lapRow = el('div', 'hud-time-row');
        lapRow.appendChild(el('div', 'k', 'Круг'));
        this.lapTime = new TimeField(null, 't');
        lapRow.appendChild(this.lapTime.node);
        times.appendChild(lapRow);

        const bestRow = el('div', 'hud-time-row small best');
        bestRow.appendChild(el('div', 'k', 'Лучший'));
        this.bestTime = new TimeField(null, 't');
        bestRow.appendChild(this.bestTime.node);
        times.appendChild(bestRow);

        this.gapRow = el('div', 'hud-time-row small gap leader');
        this.gapRow.appendChild(el('div', 'k', 'От лидера'));
        const gapWrap = el('span', 't');
        this.gapSign = el('span', null, '');
        this.gapSeconds = el('span', null, '0');
        this.gapCs = el('span', 'cs', '00');
        gapWrap.appendChild(this.gapSign);
        gapWrap.appendChild(this.gapSeconds);
        gapWrap.appendChild(el('span', null, '.'));
        gapWrap.appendChild(this.gapCs);
        this.gapRow.appendChild(gapWrap);
        times.appendChild(this.gapRow);
        wrap.appendChild(times);

        // Слот бонуса
        this.itemBox = el('div', 'hud-box hud-item');
        this.itemEmpty = el('div', 'slot-empty');
        this.itemBox.appendChild(this.itemEmpty);
        this.itemIcons = [];
        for (let i = 0; i < ITEM_DEFS.length; i++) {
            const def = ITEM_DEFS[i];
            const holder = el('div', 'icon-wrap');
            holder.innerHTML = itemIconSvg(def.id, 62);
            holder.hidden = true;
            this.itemBox.appendChild(holder);
            this.itemIcons.push({ node: holder, kind: def.kind });
        }
        this.itemHint = el('div', 'hint-key', 'E');
        this.itemHint.hidden = true;
        this.itemBox.appendChild(this.itemHint);
        wrap.appendChild(this.itemBox);

        // Спидометр
        const speedo = el('div', 'hud-box hud-speedo');
        speedo.innerHTML = this._tachSvg();
        this.tachFill = speedo.querySelector('.tach-fill');
        this.needle = speedo.querySelector('.tach-needle');
        const speedRow = el('div', 'hud-speed-val num');
        this.speedValue = el('span', null, '0');
        speedRow.appendChild(this.speedValue);
        speedRow.appendChild(el('span', 'unit', 'км/ч'));
        speedo.appendChild(speedRow);
        wrap.appendChild(speedo);

        // Заряд дрифта
        this.driftBox = el('div', 'hud-box hud-drift');
        this.driftFill = el('div', 'fill');
        this.driftBox.appendChild(this.driftFill);
        this.driftBox.appendChild(el('div', 'cap', 'Дрифт'));
        wrap.appendChild(this.driftBox);

        // Мини-карта
        const minimap = el('div', 'hud-box hud-minimap');
        this.mapStatic = document.createElement('canvas');
        this.mapDots = document.createElement('canvas');
        minimap.appendChild(this.mapStatic);
        minimap.appendChild(this.mapDots);
        wrap.appendChild(minimap);
        this.staticCtx = this.mapStatic.getContext('2d');
        this.dotsCtx = this.mapDots.getContext('2d');
        this.mapPixels = 166;

        // Лента событий
        this.feedNode = el('div', 'hud-feed');
        this.feedRows = [];
        for (let i = 0; i < FEED_ROWS; i++) {
            const node = el('div', 'feed-row');
            const icon = el('span', 'fi-wrap');
            const text = el('span', 'ft', '');
            node.appendChild(icon);
            node.appendChild(text);
            this.feedNode.appendChild(node);
            this.feedRows.push({ node: node, icon: icon, text: text, expiry: 0 });
        }
        wrap.appendChild(this.feedNode);

        // Предупреждение «не туда»
        this.wrongWayNode = el('div', 'hud-wrongway');
        this.wrongWayNode.innerHTML = '<svg viewBox="0 0 24 24" width="28" height="28" fill="none" '
            + 'stroke="#080a12" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">'
            + '<path d="M12 3v18M12 3 6 9M12 3l6 6"/></svg><span>Не туда!</span>';
        wrap.appendChild(this.wrongWayNode);

        // Пауза: плашка на весь экран и кнопка рядом с блоком времён.
        // Стили инлайновые: правка css/style.css в эту задачу не входит.
        this.pauseNode = el('div', 'hud-pause');
        this.pauseNode.style.cssText = 'position:absolute;inset:0;display:grid;'
            + 'place-items:center;pointer-events:none;'
            + 'background:radial-gradient(closest-side,rgba(8,10,18,.88),'
            + 'rgba(8,10,18,.66) 55%,rgba(8,10,18,.4))';
        const pauseCard = el('div');
        pauseCard.style.cssText = 'text-align:center';
        const pauseTitle = el('div', null, 'ПАУЗА');
        pauseTitle.style.cssText = 'font-size:132px;font-weight:900;line-height:1;'
            + 'letter-spacing:10px;color:#ffc93c;-webkit-text-stroke:11px #080a12;'
            + 'paint-order:stroke fill;text-shadow:0 10px 0 rgba(8,10,18,.6)';
        pauseCard.appendChild(pauseTitle);
        this.pauseWho = el('div', null, 'поставлена');
        this.pauseWho.style.cssText = 'margin-top:18px;font-size:26px;font-weight:900;'
            + 'color:#eef2ff;-webkit-text-stroke:6px #080a12;paint-order:stroke fill';
        pauseCard.appendChild(this.pauseWho);
        const pauseHint = el('div', null, 'P или кнопка в углу — продолжить');
        pauseHint.style.cssText = 'margin-top:10px;font-size:15px;font-weight:700;'
            + 'color:#94a1c6;-webkit-text-stroke:5px #080a12;paint-order:stroke fill';
        pauseCard.appendChild(pauseHint);
        this.pauseNode.appendChild(pauseCard);
        this.pauseNode.hidden = true;
        wrap.appendChild(this.pauseNode);

        this.pauseButton = el('button', 'hud-pause-btn', 'Пауза · P');
        this.pauseButton.type = 'button';
        this.pauseButton.style.cssText = 'position:absolute;top:18px;right:254px;'
            + 'pointer-events:auto;font:inherit;font-size:13px;font-weight:900;'
            + 'padding:9px 14px;border-radius:14px;cursor:pointer;color:#eef2ff;'
            + 'background:rgba(16,20,33,.8);border:3px solid #080a12;'
            + 'box-shadow:0 4px 0 rgba(8,10,18,.8)';
        this.pauseButton.addEventListener('click', () => this.togglePause());
        wrap.appendChild(this.pauseButton);

        // Таблица позиций
        this.standingsNode = el('div', 'hud-standings');
        this.standingsNode.hidden = true;
        const head = el('div', 'standings-head');
        head.appendChild(el('span', null, 'Позиции · Tab'));
        const headRight = el('span');
        headRight.style.float = 'right';
        this.standingsTotal = el('span', null, '0');
        headRight.appendChild(this.standingsTotal);
        headRight.appendChild(document.createTextNode(' машин'));
        head.appendChild(headRight);
        this.standingsNode.appendChild(head);

        const body = el('div', 'standings-body');
        this.standingsRows = [];
        for (let p = 0; p < MAX_SLOTS; p++) {
            const row = el('div', 'st-row');
            const place = el('div', 'st-place', INT_STR[p + 1]);
            const color = el('div', 'st-color');
            const name = el('div', 'st-name', '');
            const lapWrap = el('div', 'st-lap');
            const lap = el('span', null, '1');
            lapWrap.appendChild(document.createTextNode('круг '));
            lapWrap.appendChild(lap);
            const gap = el('div', 'st-gap', '');
            row.appendChild(place);
            row.appendChild(color);
            row.appendChild(name);
            row.appendChild(lapWrap);
            row.appendChild(gap);
            row.hidden = true;
            body.appendChild(row);
            this.standingsRows.push({
                node: row, color: color, name: name, lap: lap, gap: gap,
                cachedSlot: -1, cachedLap: -1, cachedStatus: -1,
            });
        }
        this.standingsNode.appendChild(body);
        wrap.appendChild(this.standingsNode);
    }

    _tachSvg() {
        const a0 = (-TACH_SWEEP / 2) * Math.PI / 180;
        const a1 = (TACH_SWEEP / 2) * Math.PI / 180;
        const x0 = (TACH_CX + TACH_R * Math.sin(a0)).toFixed(2);
        const y0 = (TACH_CY - TACH_R * Math.cos(a0)).toFixed(2);
        const x1 = (TACH_CX + TACH_R * Math.sin(a1)).toFixed(2);
        const y1 = (TACH_CY - TACH_R * Math.cos(a1)).toFixed(2);
        const arc = 'M' + x0 + ' ' + y0 + 'A' + TACH_R + ' ' + TACH_R + ' 0 1 1 ' + x1 + ' ' + y1;

        // Засечки шкалы — статика, строится один раз вместе с дугой.
        let ticks = '';
        for (let i = 0; i <= 10; i++) {
            const a = (-TACH_SWEEP / 2 + i * TACH_SWEEP / 10) * Math.PI / 180;
            const sin = Math.sin(a), cos = Math.cos(a);
            const major = i % 5 === 0;
            const r1 = TACH_R - 16, r2 = TACH_R - (major ? 27 : 22);
            ticks += '<line x1="' + (TACH_CX + r1 * sin).toFixed(1)
                + '" y1="' + (TACH_CY - r1 * cos).toFixed(1)
                + '" x2="' + (TACH_CX + r2 * sin).toFixed(1)
                + '" y2="' + (TACH_CY - r2 * cos).toFixed(1)
                + '" stroke="' + (major ? '#7a87b8' : '#4d5880')
                + '" stroke-width="' + (major ? 4 : 2.5) + '" stroke-linecap="round"/>';
        }

        return '<svg viewBox="0 0 240 190" preserveAspectRatio="xMidYMid meet">'
            + '<path d="' + arc + '" fill="none" stroke="#080a12" stroke-width="26" stroke-linecap="round"/>'
            + '<path d="' + arc + '" fill="none" stroke="#232b42" stroke-width="18" stroke-linecap="round"/>'
            + '<path class="tach-fill" d="' + arc + '" fill="none" stroke="#ffc93c" stroke-width="18"'
            + ' stroke-linecap="round" stroke-dasharray="' + TACH_LEN.toFixed(2) + ' ' + (TACH_LEN * 2).toFixed(2)
            + '" stroke-dashoffset="' + TACH_LEN.toFixed(2) + '"/>'
            + ticks
            + '<g class="tach-needle" transform="' + NEEDLE_ROT[0] + '">'
            + '<path d="M' + (TACH_CX - 8) + ' ' + TACH_CY + ' L' + TACH_CX + ' ' + (TACH_CY - TACH_R + 4)
            + ' L' + (TACH_CX + 8) + ' ' + TACH_CY + ' Z" fill="#ff5a5f" stroke="#080a12" stroke-width="3.5"'
            + ' stroke-linejoin="round"/>'
            + '</g>'
            + '<circle cx="' + TACH_CX + '" cy="' + TACH_CY + '" r="12" fill="#232b42" stroke="#080a12" stroke-width="3"/>'
            + '</svg>';
    }
}

/** Формат «48.31» для строк ленты (вне кадрового цикла). */
function formatSeconds(t) {
    if (!(t > 0)) return '0.00';
    const hundredths = Math.round(t * 100);
    const cs = hundredths % 100;
    const totalSec = (hundredths - cs) / 100;
    const s = totalSec % 60;
    const m = (totalSec - s) / 60;
    if (m > 0) return m + ':' + PAD2[s] + '.' + PAD2[cs];
    return s + '.' + PAD2[cs];
}

/** Код бонуса по строковому id — для main.js, если под рукой только id. */
export function itemKindOf(itemId) {
    const def = ITEM_BY_ID[itemId];
    return def ? def.kind : 0;
}

/** Обратное отображение: kind -> описание бонуса. */
export function itemDefOfKind(kind) {
    return ITEM_BY_KIND[kind] || null;
}
