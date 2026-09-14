// Сеть клиента: WebSocket, предсказание и реконсиляция своей машины,
// интерполяция чужих машин, счёт всего, чего нет в снапшоте.
//
// Владелец модуля: [client]. Разделы контракта: 5 (протокол), 9 (схемы
// событий), 10.2 (реконсиляция и интерполяция), 12.3, 12.6, 12.8.
//
// ЧТО ЭТОТ ФАЙЛ ДЕЛАЕТ
//
//   1. Держит соединение с /ws, переподключается при обрыве, разбирает
//      JSON-события раздела 9 и раздаёт их наружу колбэками.
//   2. Каждый фиксированный шаг (60 Гц) делает шаг физики своей машины,
//      кладёт (seq, buttons, state) в кольцевой буфер на 120 записей и
//      отправляет бинарный пакет ввода. **Именно бинарный кадр**: текстовый
//      кадр с недопустимым UTF-8 Tornado закрывает как нарушение протокола
//      (12.6), и обрыв выглядит загадочно.
//   3. На каждый снапшот сверяет своё предсказание с авторитетным состоянием
//      того же момента. Расхождение больше RECONCILE_EPS — подставляет
//      авторитетное и переигрывает сохранённые вводы; видимую разницу гасит
//      за RECONCILE_SMOOTH линейным спадом, чтобы коррекция не была рывком.
//      Момент ищется по tick снапшота, а не по ack_seq: почему — большой
//      комментарий в _reconcile, это отступление от буквы §10.2.
//   4. Чужие машины показывает на момент now - INTERP_DELAY: Эрмит по
//      позиции и скорости, углы по кратчайшей дуге, экстраполяция не дольше
//      EXTRAPOLATE_MAX, дальше заморозка.
//   5. Считает то, чего в снапшоте нет (12.6): время текущего круга, лучший
//      круг, отставание от лидера и бонус в руках — из race_event и progress.
//   6. Меряет ping двумя способами. Свой: время от отправки ввода до
//      снапшота, который его подтвердил, минимум по скользящему окну —
//      минимум отбрасывает то, что пакет простоял в очереди снапшота (20 Гц).
//      И запоминает серверный: сервер сам гоняет ping/pong (§9) и кладёт
//      результат в room.players[].ping — это самый чистый замер, но он
//      обновляется только в лобби.
//
// АЛЛОКАЦИИ. В кадровом цикле и в обработке снапшота — ноль. Все кольцевые
// буферы, история снапшотов и выходные массивы выделены один раз в
// конструкторе типизированными массивами. Единственный неизбежный объект —
// DataView на входящий ArrayBuffer, его кэширует сам protocol.js (12.3).
//
// ИСПОЛЬЗОВАНИЕ ИЗ main.js
//
//   const net = new NetClient({
//       onWelcome, onRooms, onRoom, onRaceInit, onCountdown,
//       onRaceEvent, onResults, onError, onOpen, onClose,
//   });
//   net.connect(url, playerName, hostToken);
//   ...
//   net.stepLocal(buttons);       // каждый фиксированный шаг
//   net.interpolate(nowMs);       // раз в кадр, перед отрисовкой
//   net.viewX[slot] ...           // готовые к отрисовке значения

import {
    MAX_CARS,
    encodeInput,
    createSnapshotBuffer,
    decodeSnapshot,
    FLAG_DRIFTING,
    FLAG_OFFTRACK,
    FLAG_BOOST,
    FLAG_SPIN,
    FLAG_SHIELD,
} from './protocol.js';

import { DT, createCarState, createCarStats, step as physicsStep } from './physics.js';
import { Track } from './track.js';
import { getCatalog } from './cars.js';

// ---------------------------------------------------------------------------
// Константы раздела 10.2
// ---------------------------------------------------------------------------

export const RECONCILE_EPS = 0.05;      // м, ниже расхождение не трогаем
export const RECONCILE_SMOOTH = 0.15;   // с, за это время гасится видимый сдвиг
export const INTERP_DELAY = 100;        // мс, на сколько отстаёт показ чужих
export const EXTRAPOLATE_MAX = 250;     // мс, дальше экстраполяции — заморозка
export const INPUT_RING = 120;          // записей в кольцевом буфере вводов
export const SNAP_HISTORY = 32;         // снапшотов в истории (1,6 с при 20 Гц)

const RECONNECT_MIN = 700;              // мс, первая пауза перед переподключением
const RECONNECT_MAX = 5000;             // мс, потолок паузы
const PING_WINDOW = 24;                 // замеров в окне минимума (около 1,2 с)
const SNAP_SNAP_LIMIT = 6.0;            // м, больше — коррекция без сглаживания
const HOLDOVER = 0.25;                  // с, «докрутка» таймеров по флагам снапшота
const GAP_MIN_SPEED = 6.0;              // м/с, нижняя опорная скорость отставания

// Синхронизация темпа шагов с тиком сервера. Обе стороны считают 60 Гц по
// своим часам, поэтому счётчики медленно расходятся: кадровый цикл клиента
// то отстаёт на просадке fps, то догоняет. Расхождение счётчиков — это
// расхождение позиций (шаг на 25 м/с стоит 0,42 м), и именно оно, а не
// физика, кормит реконсиляцию. Держим опережение в узком коридоре, добавляя
// или снимая не больше одного шага на снапшот.
const TICK_LEAD_MIN = 1;                // шагов клиента впереди тика сервера
const TICK_LEAD_MAX = 5;
const TICK_CORRECTION_MAX = 3;          // шагов поправки за один снапшот

// Часы истории снапшотов. Ставить в историю местное время прихода нельзя:
// на просевшем кадре главный поток занят отрисовкой, и два снапшота,
// отправленных через 50 мс, приходят в обработчик почти одновременно —
// шкала времени сминается, а Эрмит по смятому интервалу даёт рывок.
// Поэтому шкалу строим по авторитетному tick снапшота (tick * DT — это в
// точности sim.race_time, 12.4), а к местным часам привязываем одним
// смещением: минимумом (приход − серверное время) по скользящему окну.
// Минимум берётся потому, что задержка доставки бывает только больше нуля:
// самый быстрый пакет окна и есть честная привязка.
const CLOCK_WINDOW = 64;                // замеров смещения (3,2 с при 20 Гц)

const TAU = Math.PI * 2;

// Раскладка кольцевого буфера состояний: один Float64Array со страйдом
// вместо дюжины отдельных массивов — одна аллокация и одна кэш-линия.
const SF = 12;
const SF_X = 0, SF_Z = 1, SF_YAW = 2, SF_VX = 3, SF_VZ = 4, SF_STEER = 5,
      SF_DRIFT = 6, SF_BOOST = 7, SF_SPIN = 8, SF_SHIELD = 9, SF_SLOW = 10,
      SF_PROGRESS = 11;

const SI = 3;
const SI_SAMPLE = 0, SI_LAP = 1, SI_CP = 2;

const SB = 2;
const SB_DRIFT_ACTIVE = 0, SB_OFFTRACK = 1;

// Коды бонусов раздела 8: это данные протокола, а не интерфейса, поэтому
// таблица живёт здесь, а не тянется из ui/.
const ITEM_KIND = Object.create(null);
ITEM_KIND.boost = 1;
ITEM_KIND.rocket = 2;
ITEM_KIND.mine = 3;
ITEM_KIND.shield = 4;
ITEM_KIND.storm = 5;

/** performance.now(), если он есть. */
const now = (typeof performance !== 'undefined' && performance.now)
    ? function () { return performance.now(); }
    : function () { return Date.now(); };

/** Кратчайшая дуга между углами, без циклов. */
function shortAngle(from, to) {
    let d = to - from;
    if (d > Math.PI || d < -Math.PI) d -= TAU * Math.round(d / TAU);
    return d;
}

// ---------------------------------------------------------------------------

export class NetClient {
    /**
     * @param {object} [handlers] колбэки на события раздела 9, все необязательные:
     *        onOpen, onClose, onWelcome, onRooms, onRoom, onRaceInit,
     *        onCountdown, onRaceEvent, onResults, onError, onSnapshot.
     */
    constructor(handlers) {
        this.h = handlers || {};

        // --- соединение ------------------------------------------------------
        this.ws = null;
        this.url = '';
        this.playerName = 'Гонщик';
        this.hostToken = null;
        this.connected = false;
        this.closedByUs = false;
        this.reconnectDelay = RECONNECT_MIN;
        this.reconnectTimer = 0;
        this.attempts = 0;

        // --- состояние комнаты ----------------------------------------------
        this.localSlot = -1;
        this.roomState = '';
        this.racing = false;
        this.lapsTotal = 0;

        // --- снапшот ---------------------------------------------------------
        this.snap = createSnapshotBuffer();
        this.snapRecvMs = 0;            // локальное время прихода последнего
        this.snapPrevRecvMs = 0;
        this.snapIntervalMs = 50;
        this.snapCountTotal = 0;
        this.snapRaceTime = 0;          // tick * DT — авторитетное время гонки
        this.snapTick = 0;
        this.tickLead = 0;
        this.clockAdjust = 0;           // с, поправка накопителя на следующий кадр
        this.clockOffset = 0;           // мс, местное время минус серверное
        this.clockRing = new Float64Array(CLOCK_WINDOW);
        this.clockFill = 0;
        this.clockHead = 0;
        this.lastStamp = 0;

        // --- предсказание своей машины ---------------------------------------
        this.track = null;
        this.localStats = null;
        this.state = createCarState(0, 0, 0);
        this.localInSnapshot = false;   // машина ещё есть в снапшотах сервера
        this.seq = 0;
        this.ackSeq = 0;

        // Позиция перед последним шагом — для интерполяции по остатку
        // накопителя (раздел 10.1).
        this.prevX = 0; this.prevZ = 0; this.prevYaw = 0;

        // Кольцевой буфер (раздел 10.1: 120 записей), выделяется один раз.
        this.ringF = new Float64Array(INPUT_RING * SF);
        this.ringI = new Int32Array(INPUT_RING * SI);
        this.ringB = new Uint8Array(INPUT_RING * SB);
        this.ringButtons = new Uint8Array(INPUT_RING);
        this.ringValid = new Uint8Array(INPUT_RING);
        this.ringStamp = new Float64Array(INPUT_RING);

        // --- сглаживание коррекции (10.2, пункт 5) ---------------------------
        this.smoothX = 0; this.smoothZ = 0; this.smoothYaw = 0;
        this.smoothLeft = 0;            // доля оставшегося сдвига, 1 -> 0

        // --- история снапшотов для интерполяции чужих ------------------------
        const cells = SNAP_HISTORY * MAX_CARS;
        this.snapTime = new Float64Array(SNAP_HISTORY);
        this.snapPresent = new Uint8Array(cells);
        this.snapX = new Float32Array(cells);
        this.snapZ = new Float32Array(cells);
        this.snapYaw = new Float32Array(cells);
        this.snapVx = new Float32Array(cells);
        this.snapVz = new Float32Array(cells);
        this.snapSteer = new Float32Array(cells);
        this.snapFlags = new Uint8Array(cells);
        this.snapDrift = new Uint8Array(cells);
        this.snapLap = new Uint8Array(cells);
        this.snapPlace = new Uint8Array(cells);
        this.snapHead = -1;
        this.snapStored = 0;

        // --- выход интерполяции: то, что читает main.js ----------------------
        this.viewPresent = new Uint8Array(MAX_CARS);
        this.viewX = new Float32Array(MAX_CARS);
        this.viewZ = new Float32Array(MAX_CARS);
        this.viewYaw = new Float32Array(MAX_CARS);
        this.viewVx = new Float32Array(MAX_CARS);
        this.viewVz = new Float32Array(MAX_CARS);
        this.viewSteer = new Float32Array(MAX_CARS);
        this.viewFlags = new Uint8Array(MAX_CARS);
        this.viewDrift = new Uint8Array(MAX_CARS);
        this.viewLap = new Uint8Array(MAX_CARS);
        this.viewPlace = new Uint8Array(MAX_CARS);

        // --- то, чего нет в снапшоте (12.6) ----------------------------------
        this.lapStartTime = 0;          // время гонки, когда начался текущий круг
        this.bestLap = 0;
        this.lapTime = 0;
        this.gap = 0;
        this.item = 0;                  // kind бонуса в руках, 0 — пусто
        this.place = 1;
        this.lap = 0;
        this.progress = new Float64Array(MAX_CARS);
        this.progressHint = new Int32Array(MAX_CARS);
        this.leaderSpeed = GAP_MIN_SPEED;

        // --- ping и измерения -------------------------------------------------
        this.pingSamples = new Float64Array(PING_WINDOW);
        this.pingFill = 0;
        this.pingHead = 0;
        this.ping = 0;
        this.serverPing = 0;            // что намерял сервер, приходит в room

        this.reconcileError = 0;        // м, последнее расхождение
        this.reconcileMax = 0;
        this.reconcileCount = 0;
        this.snapshotCount = 0;

        // Объект статистики переиспользуется: в кадре не создаётся ничего.
        this.stats = {
            ping: 0,
            serverPing: 0,
            snapshotMs: 0,
            reconcileErr: 0,
            reconcileMax: 0,
            reconcileRate: 0,
            seq: 0,
            ackSeq: 0,
            tickLead: 0,
            connected: false,
        };

        // Обработчики сокета создаются один раз.
        const self = this;
        this._onOpen = function () { self._handleOpen(); };
        this._onClose = function (ev) { self._handleClose(ev); };
        this._onError = function () { /* onclose приедет следом, там и решаем */ };
        this._onMessage = function (ev) { self._handleMessage(ev); };
    }

    // =======================================================================
    // Соединение
    // =======================================================================

    /**
     * @param {string} url       ws://host:port/ws
     * @param {string} name      имя игрока для hello
     * @param {string} [token]   host_token из адресной строки, если он есть
     */
    connect(url, name, token) {
        this.url = url;
        if (name) this.playerName = name;
        if (token !== undefined) this.hostToken = token || null;
        this.closedByUs = false;
        this._open();
    }

    setName(name) {
        this.playerName = name || 'Гонщик';
    }

    _open() {
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = 0;
        }
        let ws;
        try {
            ws = new WebSocket(this.url);
        } catch (e) {
            this._scheduleReconnect();
            return;
        }
        ws.binaryType = 'arraybuffer';
        ws.onopen = this._onOpen;
        ws.onclose = this._onClose;
        ws.onerror = this._onError;
        ws.onmessage = this._onMessage;
        this.ws = ws;
    }

    /**
     * Мягкое переподключение: сокет закрывается без объявления обрыва, и
     * hello уходит заново. Нужно из-за того, что имя игрока едет только
     * в hello: событие «переименоваться» контракт не заводил (раздел 9),
     * а имя человек набирает уже после подключения.
     */
    reconnect() {
        const ws = this.ws;
        if (ws) {
            ws.onopen = null;
            ws.onclose = null;
            ws.onerror = null;
            ws.onmessage = null;
            this.ws = null;
            try { ws.close(); } catch (e) { /* уже закрыт */ }
        }
        this.connected = false;
        this.attempts = 0;
        this.reconnectDelay = RECONNECT_MIN;
        this.closedByUs = false;
        this._open();
    }

    /** Закрыть навсегда: переподключения не будет. */
    close() {
        this.closedByUs = true;
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = 0;
        }
        if (this.ws) {
            try { this.ws.close(); } catch (e) { /* уже закрыт */ }
            this.ws = null;
        }
        this.connected = false;
    }

    _handleOpen() {
        this.connected = true;
        this.attempts = 0;
        this.reconnectDelay = RECONNECT_MIN;
        // hello — первое сообщение после коннекта (раздел 9).
        if (this.hostToken) {
            this.send({ t: 'hello', name: this.playerName, host_token: this.hostToken });
        } else {
            this.send({ t: 'hello', name: this.playerName });
        }
        if (this.h.onOpen) this.h.onOpen();
    }

    _handleClose(ev) {
        const wasConnected = this.connected;
        this.connected = false;
        this.ws = null;
        this.racing = false;
        this.localSlot = -1;
        this.roomState = '';
        if (this.h.onClose) this.h.onClose(ev, this.closedByUs);
        if (!this.closedByUs) this._scheduleReconnect(wasConnected);
    }

    _scheduleReconnect(immediateFirst) {
        if (this.closedByUs || this.reconnectTimer) return;
        const delay = immediateFirst && this.attempts === 0 ? RECONNECT_MIN : this.reconnectDelay;
        this.attempts++;
        this.reconnectDelay = Math.min(RECONNECT_MAX, this.reconnectDelay * 2);
        const self = this;
        this.reconnectTimer = setTimeout(function () {
            self.reconnectTimer = 0;
            self._open();
        }, delay);
    }

    /** JSON-событие на сервер. Текстовый кадр — как и положено (раздел 5). */
    send(obj) {
        const ws = this.ws;
        if (!ws || ws.readyState !== 1) return false;
        ws.send(JSON.stringify(obj));
        return true;
    }

    // =======================================================================
    // Приём
    // =======================================================================

    _handleMessage(ev) {
        const data = ev.data;
        if (typeof data === 'string') {
            this._handleJson(data);
            return;
        }
        this._handleSnapshot(data);
    }

    _handleJson(text) {
        let msg;
        try {
            msg = JSON.parse(text);
        } catch (e) {
            return;                     // мусор от сервера молча мимо
        }
        const t = msg.t;
        if (t === 'welcome') {
            this._onWelcome(msg);
        } else if (t === 'rooms') {
            if (this.h.onRooms) this.h.onRooms(msg);
        } else if (t === 'room') {
            this._onRoom(msg);
        } else if (t === 'race_init') {
            this._onRaceInit(msg);
        } else if (t === 'countdown') {
            if (this.h.onCountdown) this.h.onCountdown(msg);
        } else if (t === 'race_event') {
            this._onRaceEvent(msg);
        } else if (t === 'results') {
            this.racing = false;
            if (this.h.onResults) this.h.onResults(msg);
        } else if (t === 'ping') {
            // Замер задержки сервером: отвечаем сразу, без обработки (раздел 9).
            this.send({ t: 'pong', t0: msg.t0 });
        } else if (t === 'error') {
            if (this.h.onError) this.h.onError(msg);
        }
    }

    _onWelcome(msg) {
        this.slotToken = msg.slot_token || '';
        this.isHost = !!msg.is_host;
        this.serverName = msg.server_name || '';
        if (this.h.onWelcome) this.h.onWelcome(msg);
    }

    _onRoom(msg) {
        // 12.8: слот приходит в каждом событии room, ловить «первое» не нужно.
        this.localSlot = (msg.you === undefined || msg.you === null) ? -1 : msg.you | 0;
        const state = msg.state || '';
        const wasRacing = this.racing;
        this.roomState = state;
        this.racing = state === 'RACING';
        if (this.racing && !wasRacing) {
            // Старт: отсчёт seq начинается заново вместе с гонкой.
            this._resetPrediction();
        }
        if (msg.settings && msg.settings.laps) this.lapsTotal = msg.settings.laps | 0;

        // Свой ping, как его намерял сервер (раздел 12.6: ping в миллисекундах).
        const players = msg.players;
        if (players) {
            for (let i = 0; i < players.length; i++) {
                if (players[i].slot === this.localSlot) {
                    this.serverPing = players[i].ping | 0;
                    break;
                }
            }
        }
        if (this.h.onRoom) this.h.onRoom(msg);
    }

    _onRaceInit(msg) {
        this.track = Track.fromServer(msg.track);
        this.lapsTotal = msg.laps | 0;
        this.racing = false;
        this._resetRaceInfo();

        // Расстановка своей машины на решётке и характеристики для физики.
        const players = msg.players || [];
        const catalog = getCatalog();
        let mine = null;
        for (let i = 0; i < players.length; i++) {
            if (players[i].slot === this.localSlot) { mine = players[i]; break; }
        }
        const spec = catalog.resolve(mine ? mine.car : null);
        this.localStats = spec && spec.stats ? createCarStats(spec.stats) : null;

        const state = this.state;
        const grid = mine && mine.grid ? mine.grid : null;
        state.x = grid ? grid.x : 0;
        state.z = grid ? grid.z : 0;
        state.yaw = grid ? grid.yaw : 0;
        state.vx = 0; state.vz = 0;
        state.steer = 0;
        state.driftCharge = 0;
        state.driftActive = false;
        state.boostTime = 0;
        state.spinTime = 0;
        state.shieldTime = 0;
        state.slowTime = 0;
        state.offtrack = false;
        // 12.7: init_state обязателен после расстановки на решётке.
        this.track.initState(state);

        this.prevX = state.x;
        this.prevZ = state.z;
        this.prevYaw = state.yaw;

        this._resetPrediction();
        this._resetSnapshots();

        if (this.h.onRaceInit) this.h.onRaceInit(msg);
    }

    /**
     * 12.6 закрепил за net.js счёт того, чего нет в снапшоте. Здесь ловятся
     * ровно те события, из которых это считается.
     */
    _onRaceEvent(msg) {
        const kind = msg.kind;
        const mine = msg.slot === this.localSlot;
        if (mine) {
            if (kind === 'lap') {
                // Круги идут подряд от нуля, поэтому сумма lap-времён и есть
                // момент старта текущего круга. Так точнее, чем «сейчас»:
                // задержка сети в накопление не попадает.
                this.lapStartTime += msg.time || 0;
                this.bestLap = msg.best || this.bestLap;
            } else if (kind === 'pickup') {
                this.item = ITEM_KIND[msg.item] || 0;
            } else if (kind === 'use') {
                this.item = 0;
            } else if (kind === 'finish') {
                // Симуляция обнуляет бонус финишировавшему без события use.
                this.item = 0;
            }
        }
        if (this.h.onRaceEvent) this.h.onRaceEvent(msg);
    }

    // =======================================================================
    // Снапшот
    // =======================================================================

    _handleSnapshot(buffer) {
        const snap = this.snap;
        if (!decodeSnapshot(buffer, snap)) return;

        const t = now();
        if (this.snapRecvMs > 0) {
            const gap = t - this.snapRecvMs;
            // Сглаженный интервал: для оверлея F3, ожидается 50 мс.
            this.snapIntervalMs += (gap - this.snapIntervalMs) * 0.25;
        }
        this.snapPrevRecvMs = this.snapRecvMs;
        this.snapRecvMs = t;
        this.snapshotCount++;
        this.snapTick = snap.tick;
        this.snapRaceTime = snap.tick * DT;
        // Насколько предсказание убежало вперёд серверного тика. Оба идут
        // 60 Гц, поэтому в норме это небольшое положительное число: сколько
        // шагов клиента ещё не дошло до сервера.
        const lead = this.seq - snap.tick;
        this.tickLead = lead;
        if (this.racing) {
            // Поправка пропорциональна выходу за коридор, но не больше трёх
            // шагов на снапшот: после просадки кадра опережение уезжает сразу
            // на несколько шагов, и возвращать его по одному — это полсекунды
            // заметного расхождения.
            let corr = 0;
            if (lead < TICK_LEAD_MIN) corr = TICK_LEAD_MIN - lead;
            else if (lead > TICK_LEAD_MAX) corr = TICK_LEAD_MAX - lead;
            if (corr > TICK_CORRECTION_MAX) corr = TICK_CORRECTION_MAX;
            else if (corr < -TICK_CORRECTION_MAX) corr = -TICK_CORRECTION_MAX;
            this.clockAdjust += corr * DT;
        }

        this._pushHistory(this._stampOf(snap.tick, t), snap);
        this._measurePing(snap.ackSeq, t);
        this._reconcile(snap);
        this._updateProgress(snap);

        if (this.h.onSnapshot) this.h.onSnapshot(snap);
    }

    /**
     * Метка времени снапшота на местных часах: серверное время тика плюс
     * смещение. Смещение — минимум (приход − серверное время) по окну,
     * поэтому дрожание доставки в шкалу не попадает и соседние снапшоты
     * стоят ровно через 50 мс, как их и отправляли.
     */
    _stampOf(tick, arrivedMs) {
        const serverMs = tick * (DT * 1000);
        this.clockRing[this.clockHead] = arrivedMs - serverMs;
        this.clockHead = this.clockHead + 1 >= CLOCK_WINDOW ? 0 : this.clockHead + 1;
        if (this.clockFill < CLOCK_WINDOW) this.clockFill++;

        let minOff = this.clockRing[0];
        for (let i = 1; i < this.clockFill; i++) {
            const v = this.clockRing[i];
            if (v < minOff) minOff = v;
        }
        this.clockOffset = minOff;

        let stamp = serverMs + minOff;
        // История обязана быть строго возрастающей: дубликат тика или
        // сдвинувшееся смещение не должны сломать поиск пары.
        if (stamp <= this.lastStamp) stamp = this.lastStamp + 0.001;
        this.lastStamp = stamp;
        return stamp;
    }

    /** Положить снапшот в кольцевую историю. Аллокаций нет. */
    _pushHistory(t, snap) {
        let h = this.snapHead + 1;
        if (h >= SNAP_HISTORY) h = 0;
        this.snapHead = h;
        if (this.snapStored < SNAP_HISTORY) this.snapStored++;
        this.snapTime[h] = t;

        const base = h * MAX_CARS;
        const present = this.snapPresent;
        for (let s = 0; s < MAX_CARS; s++) present[base + s] = 0;

        const count = snap.carCount;
        for (let i = 0; i < count; i++) {
            const slot = snap.carSlot[i];
            if (slot >= MAX_CARS) continue;
            const k = base + slot;
            present[k] = 1;
            this.snapX[k] = snap.carX[i];
            this.snapZ[k] = snap.carZ[i];
            this.snapYaw[k] = snap.carYaw[i];
            this.snapVx[k] = snap.carVx[i];
            this.snapVz[k] = snap.carVz[i];
            this.snapSteer[k] = snap.carSteer[i];
            this.snapFlags[k] = snap.carFlags[i];
            this.snapDrift[k] = snap.carDriftCharge[i];
            this.snapLap[k] = snap.carLap[i];
            this.snapPlace[k] = snap.carPlace[i];
        }
    }

    /**
     * RTT по подтверждённому вводу. Пакет мог пролежать в очереди снапшота
     * до 50 мс, поэтому берём минимум по окну: он приходится на ввод,
     * успевший к самому тику отправки, и равен честному RTT.
     */
    _measurePing(ackSeq, t) {
        if (ackSeq <= 0 || ackSeq > this.seq) return;
        if (this.seq - ackSeq >= INPUT_RING) return;
        const idx = ackSeq % INPUT_RING;
        const sent = this.ringStamp[idx];
        if (sent <= 0) return;
        const rtt = t - sent;
        if (rtt < 0 || rtt > 4000) return;

        this.pingSamples[this.pingHead] = rtt;
        this.pingHead = this.pingHead + 1 >= PING_WINDOW ? 0 : this.pingHead + 1;
        if (this.pingFill < PING_WINDOW) this.pingFill++;

        let min = this.pingSamples[0];
        for (let i = 1; i < this.pingFill; i++) {
            const v = this.pingSamples[i];
            if (v < min) min = v;
        }
        this.ping = min;
    }

    // =======================================================================
    // Реконсиляция (раздел 10.2)
    // =======================================================================

    _reconcile(snap) {
        const track = this.track;
        const stats = this.localStats;
        if (!track || !stats || this.localSlot < 0) return;

        const idxInSnap = snap.indexBySlot[this.localSlot];
        this.localInSnapshot = idxInSnap >= 0;
        if (idxInSnap < 0) return;

        const ack = snap.ackSeq;
        this.ackSeq = ack;

        const ax = snap.carX[idxInSnap];
        const az = snap.carZ[idxInSnap];

        // Пока гонка не началась, авторитет безусловен: машина стоит на решётке.
        if (!this.racing || ack <= 0) {
            this._hardReset(snap, idxInSnap);
            return;
        }

        // ЗАЧЕМ ЗДЕСЬ tick, А НЕ ack_seq.
        //
        // §10.2 велит сверяться с собственным состоянием «на момент ack_seq».
        // Это верно для схемы «сервер делает шаг на каждый принятый пакет»,
        // но здесь сервер тикает по своим часам 60 Гц независимо от приёма,
        // а ack_seq — это номер ПОСЛЕДНЕГО ПРИНЯТОГО пакета (sim.set_input
        // хранит last_seq), а не «столько шагов я сделал». Когда кадр клиента
        // просел и за один кадр ушло четыре пакета, сервер принимает все
        // четыре, применяет последний и тикает при этом один раз: ack_seq
        // прыгает на 4, а состояние продвинулось на 1. Сверка по ack_seq
        // сравнивает разные моменты времени и даёт метр-полтора выдуманного
        // расхождения на ровном месте.
        //
        // Зато tick снапшота — это ровно «сколько шагов сделал сервер», а темп
        // шагов клиента привязан к нему (TICK_LEAD_*), поэтому запись кольца
        // с индексом tick — это наше предсказание того же самого момента.
        // Сверка становится точной при любом кадре клиента.
        let base = snap.tick;
        if (base > this.seq) base = this.seq;   // клиент отстал: берём самое свежее
        if (base <= 0 || this.seq - base >= INPUT_RING) {
            this._hardReset(snap, idxInSnap);
            return;
        }

        const slot = base % INPUT_RING;
        if (!this.ringValid[slot]) {
            this._hardReset(snap, idxInSnap);
            return;
        }

        const f = slot * SF;
        const dx = ax - this.ringF[f + SF_X];
        const dz = az - this.ringF[f + SF_Z];
        const err = Math.sqrt(dx * dx + dz * dz);
        this.reconcileError = err;
        if (err > this.reconcileMax) this.reconcileMax = err;
        if (err <= RECONCILE_EPS) return;   // пункт 3: расхождение мало — не трогаем

        this.reconcileCount++;

        // Куда машина смотрела до коррекции — чтобы сдвиг было чем гасить.
        const beforeX = this.state.x;
        const beforeZ = this.state.z;
        const beforeYaw = this.state.yaw;

        // Пункт 4: подставить авторитетное состояние на этот момент...
        this._restoreState(slot);
        this._applyAuthoritative(snap, idxInSnap);
        this._recordState(slot);

        // ...и переиграть сохранённые вводы с base + 1 до текущего seq.
        const state = this.state;
        const buttons = this.ringButtons;
        const valid = this.ringValid;
        for (let s = base + 1; s <= this.seq; s++) {
            const i = s % INPUT_RING;
            if (!valid[i]) continue;
            // step() сам делает шаги 14 и 16 (границы и progress): track
            // передан ему аргументом, звать их отдельно — двойная работа.
            physicsStep(state, stats, buttons[i], DT, track, state.sampleIdx);
            this._recordState(i);
        }

        // Пункт 5: видимую разницу гасим линейно за RECONCILE_SMOOTH.
        const offX = beforeX - state.x;
        const offZ = beforeZ - state.z;
        const offYaw = shortAngle(state.yaw, beforeYaw);
        if (offX * offX + offZ * offZ > SNAP_SNAP_LIMIT * SNAP_SNAP_LIMIT) {
            // Такой скачок сглаживать бессмысленно: это респаун или телепорт.
            this.smoothX = 0; this.smoothZ = 0; this.smoothYaw = 0;
            this.smoothLeft = 0;
        } else {
            this.smoothX = this.smoothX * this.smoothLeft + offX;
            this.smoothZ = this.smoothZ * this.smoothLeft + offZ;
            this.smoothYaw = this.smoothYaw * this.smoothLeft + offYaw;
            this.smoothLeft = 1;
        }

        this.prevX = state.x;
        this.prevZ = state.z;
        this.prevYaw = state.yaw;
    }

    /** Полная пересинхронизация: своё предсказание выбрасывается. */
    _hardReset(snap, idxInSnap) {
        this._applyAuthoritative(snap, idxInSnap);
        const state = this.state;
        if (this.track) {
            state.sampleIdx = this.track.nearestIndex(state.x, state.z, state.sampleIdx);
        }
        this.prevX = state.x;
        this.prevZ = state.z;
        this.prevYaw = state.yaw;
        this.smoothX = 0; this.smoothZ = 0; this.smoothYaw = 0;
        this.smoothLeft = 0;
        this.reconcileError = 0;
        const valid = this.ringValid;
        for (let i = 0; i < INPUT_RING; i++) valid[i] = 0;
    }

    /**
     * Наложить авторитетные поля снапшота на текущее состояние.
     *
     * Снапшот везёт не всё состояние (раздел 5.3): таймеров бонусов в нём нет,
     * есть только флаги. Поэтому таймер, который сервер объявил активным,
     * а мы уже погасили, подтягивается на HOLDOVER — снапшоты идут каждые
     * 50 мс и продлевают его, пока флаг держится. Обратное — флаг снят,
     * а у нас таймер тикает — гасим сразу.
     */
    _applyAuthoritative(snap, i) {
        const state = this.state;
        state.x = snap.carX[i];
        state.z = snap.carZ[i];
        state.yaw = snap.carYaw[i];
        state.vx = snap.carVx[i];
        state.vz = snap.carVz[i];
        state.steer = snap.carSteer[i];
        state.driftCharge = snap.carDriftCharge[i] * 0.01;

        const flags = snap.carFlags[i];
        state.driftActive = (flags & FLAG_DRIFTING) !== 0;
        state.offtrack = (flags & FLAG_OFFTRACK) !== 0;
        state.lap = snap.carLap[i];

        if ((flags & FLAG_BOOST) !== 0) {
            if (state.boostTime < HOLDOVER) state.boostTime = HOLDOVER;
        } else {
            state.boostTime = 0;
        }
        if ((flags & FLAG_SPIN) !== 0) {
            if (state.spinTime < HOLDOVER) state.spinTime = HOLDOVER;
        } else {
            state.spinTime = 0;
        }
        if ((flags & FLAG_SHIELD) !== 0) {
            if (state.shieldTime < HOLDOVER) state.shieldTime = HOLDOVER;
        } else {
            state.shieldTime = 0;
        }
    }

    _recordState(i) {
        const state = this.state;
        const f = i * SF;
        const ring = this.ringF;
        ring[f + SF_X] = state.x;
        ring[f + SF_Z] = state.z;
        ring[f + SF_YAW] = state.yaw;
        ring[f + SF_VX] = state.vx;
        ring[f + SF_VZ] = state.vz;
        ring[f + SF_STEER] = state.steer;
        ring[f + SF_DRIFT] = state.driftCharge;
        ring[f + SF_BOOST] = state.boostTime;
        ring[f + SF_SPIN] = state.spinTime;
        ring[f + SF_SHIELD] = state.shieldTime;
        ring[f + SF_SLOW] = state.slowTime;
        ring[f + SF_PROGRESS] = state.progress;

        const n = i * SI;
        this.ringI[n + SI_SAMPLE] = state.sampleIdx;
        this.ringI[n + SI_LAP] = state.lap;
        this.ringI[n + SI_CP] = state.checkpoint;

        const b = i * SB;
        this.ringB[b + SB_DRIFT_ACTIVE] = state.driftActive ? 1 : 0;
        this.ringB[b + SB_OFFTRACK] = state.offtrack ? 1 : 0;
        this.ringValid[i] = 1;
    }

    _restoreState(i) {
        const state = this.state;
        const f = i * SF;
        const ring = this.ringF;
        state.x = ring[f + SF_X];
        state.z = ring[f + SF_Z];
        state.yaw = ring[f + SF_YAW];
        state.vx = ring[f + SF_VX];
        state.vz = ring[f + SF_VZ];
        state.steer = ring[f + SF_STEER];
        state.driftCharge = ring[f + SF_DRIFT];
        state.boostTime = ring[f + SF_BOOST];
        state.spinTime = ring[f + SF_SPIN];
        state.shieldTime = ring[f + SF_SHIELD];
        state.slowTime = ring[f + SF_SLOW];
        state.progress = ring[f + SF_PROGRESS];

        const n = i * SI;
        state.sampleIdx = this.ringI[n + SI_SAMPLE];
        state.lap = this.ringI[n + SI_LAP];
        state.checkpoint = this.ringI[n + SI_CP];

        const b = i * SB;
        state.driftActive = this.ringB[b + SB_DRIFT_ACTIVE] === 1;
        state.offtrack = this.ringB[b + SB_OFFTRACK] === 1;
    }

    _resetPrediction() {
        this.seq = 0;
        this.ackSeq = 0;
        const valid = this.ringValid;
        const stamp = this.ringStamp;
        for (let i = 0; i < INPUT_RING; i++) { valid[i] = 0; stamp[i] = 0; }
        this.smoothX = 0; this.smoothZ = 0; this.smoothYaw = 0;
        this.smoothLeft = 0;
        this.reconcileError = 0;
        this.reconcileMax = 0;
        this.reconcileCount = 0;
        this.clockAdjust = 0;
        this.tickLead = 0;
    }

    _resetSnapshots() {
        this.snapHead = -1;
        this.snapStored = 0;
        this.localInSnapshot = false;
        this.clockFill = 0;
        this.clockHead = 0;
        this.clockOffset = 0;
        this.lastStamp = 0;
        this.snapRecvMs = 0;
        this.snapPrevRecvMs = 0;
        this.snapshotCount = 0;
        const present = this.viewPresent;
        for (let i = 0; i < MAX_CARS; i++) present[i] = 0;
    }

    _resetRaceInfo() {
        this.lapStartTime = 0;
        this.bestLap = 0;
        this.lapTime = 0;
        this.gap = 0;
        this.item = 0;
        this.place = 1;
        this.lap = 0;
        this.snapRaceTime = 0;
        this.leaderSpeed = GAP_MIN_SPEED;
        const hint = this.progressHint;
        for (let i = 0; i < MAX_CARS; i++) { this.progress[i] = 0; hint[i] = -1; }
    }

    // =======================================================================
    // Шаг предсказания (раздел 10.1)
    // =======================================================================

    /**
     * Один фиксированный шаг: предсказать свою машину, сохранить (seq,
     * buttons, state) и отправить бинарный ввод. Аллокаций нет — encodeInput
     * отдаёт переиспользуемый буфер, ws.send копирует его синхронно.
     *
     * @param {number} buttons маска кнопок из input.js
     */
    stepLocal(buttons) {
        if (!this.racing || !this.track || !this.localStats) return;
        // Финишировавший доживает призраком и через 3 с пропадает из снапшотов
        // (раздел 9). Предсказывать и слать ввод за исчезнувшую машину незачем:
        // сервер такой ввод всё равно отбрасывает, а предсказание без
        // авторитета уехало бы в никуда.
        if (this.snapshotCount > 0 && !this.localInSnapshot) return;

        const state = this.state;
        this.prevX = state.x;
        this.prevZ = state.z;
        this.prevYaw = state.yaw;

        // step() включает шаги 14 (границы) и 16 (progress) — им передан track.
        physicsStep(state, this.localStats, buttons, DT, this.track, state.sampleIdx);

        const seq = this.seq + 1;
        this.seq = seq;
        const i = seq % INPUT_RING;
        this.ringButtons[i] = buttons;
        this.ringStamp[i] = now();
        this._recordState(i);

        const ws = this.ws;
        if (ws && ws.readyState === 1) {
            // Бинарный кадр обязателен (раздел 5, 12.6).
            ws.send(encodeInput(seq, buttons));
        }
    }

    /**
     * Забрать накопленную поправку темпа шагов (и обнулить её). Кадровый цикл
     * прибавляет её к накопителю времени: так число шагов клиента сходится
     * с числом тиков сервера, а реконсиляции остаётся только настоящая
     * разница физики.
     */
    takeClockAdjust() {
        const a = this.clockAdjust;
        this.clockAdjust = 0;
        return a;
    }

    /**
     * Гашение видимого сдвига после коррекции. Зовётся раз в кадр с реальным
     * dt, спад линейный — за RECONCILE_SMOOTH сдвиг гарантированно нулевой.
     */
    advanceSmoothing(dt) {
        if (this.smoothLeft <= 0) return;
        const left = this.smoothLeft - dt / RECONCILE_SMOOTH;
        this.smoothLeft = left > 0 ? left : 0;
    }

    /** Видимая X своей машины: предсказание + остаток коррекции. */
    renderX(alpha) {
        return this.prevX + (this.state.x - this.prevX) * alpha
            + this.smoothX * this.smoothLeft;
    }

    renderZ(alpha) {
        return this.prevZ + (this.state.z - this.prevZ) * alpha
            + this.smoothZ * this.smoothLeft;
    }

    renderYaw(alpha) {
        return this.prevYaw + shortAngle(this.prevYaw, this.state.yaw) * alpha
            + this.smoothYaw * this.smoothLeft;
    }

    // =======================================================================
    // Интерполяция чужих машин (раздел 10.2)
    // =======================================================================

    /**
     * Заполнить view*-массивы состоянием всех машин на момент
     * nowMs - INTERP_DELAY. Зовётся раз в кадр, ничего не создаёт.
     */
    interpolate(nowMs) {
        const present = this.viewPresent;
        if (this.snapStored === 0) {
            for (let s = 0; s < MAX_CARS; s++) present[s] = 0;
            return;
        }

        const renderTime = nowMs - INTERP_DELAY;
        const head = this.snapHead;
        const times = this.snapTime;

        // Найти пару: A — последний снапшот не позже renderTime, B — первый после.
        let ia = -1;
        let ib = -1;
        let next = -1;
        for (let k = 0; k < this.snapStored; k++) {
            let i = head - k;
            if (i < 0) i += SNAP_HISTORY;
            if (times[i] <= renderTime) { ia = i; ib = next; break; }
            next = i;
        }

        if (ia < 0) {
            // renderTime старше всей истории: показываем самый старый кадр.
            let oldest = head - (this.snapStored - 1);
            if (oldest < 0) oldest += SNAP_HISTORY;
            this._copyFrame(oldest);
            return;
        }
        if (ib < 0) {
            // Свежих данных нет: экстраполяция не дольше EXTRAPOLATE_MAX.
            let ahead = renderTime - times[ia];
            if (ahead > EXTRAPOLATE_MAX) ahead = EXTRAPOLATE_MAX;
            this._extrapolateFrame(ia, ahead * 0.001);
            return;
        }

        const t0 = times[ia];
        const t1 = times[ib];
        const span = t1 - t0;
        if (span <= 0) { this._copyFrame(ib); return; }

        const u = (renderTime - t0) / span;
        const hs = span * 0.001;            // длина интервала в секундах
        const u2 = u * u;
        const u3 = u2 * u;
        const h00 = 2 * u3 - 3 * u2 + 1;
        const h10 = u3 - 2 * u2 + u;
        const h01 = -2 * u3 + 3 * u2;
        const h11 = u3 - u2;

        const baseA = ia * MAX_CARS;
        const baseB = ib * MAX_CARS;
        const sp = this.snapPresent;

        for (let s = 0; s < MAX_CARS; s++) {
            const ka = baseA + s;
            const kb = baseB + s;
            if (!sp[kb]) {
                // Машина исчезла к моменту B — показывать её больше нечем.
                present[s] = 0;
                continue;
            }
            present[s] = 1;
            if (!sp[ka]) {
                // Машина появилась между кадрами: без предыстории берём B как есть.
                this._takeFrom(kb, s);
                continue;
            }
            const x0 = this.snapX[ka], x1 = this.snapX[kb];
            const z0 = this.snapZ[ka], z1 = this.snapZ[kb];
            const vx0 = this.snapVx[ka], vx1 = this.snapVx[kb];
            const vz0 = this.snapVz[ka], vz1 = this.snapVz[kb];

            // Эрмит по позиции и скорости (10.2).
            this.viewX[s] = h00 * x0 + h10 * hs * vx0 + h01 * x1 + h11 * hs * vx1;
            this.viewZ[s] = h00 * z0 + h10 * hs * vz0 + h01 * z1 + h11 * hs * vz1;
            this.viewVx[s] = vx0 + (vx1 - vx0) * u;
            this.viewVz[s] = vz0 + (vz1 - vz0) * u;

            // Углы — по кратчайшей дуге (10.2).
            const y0 = this.snapYaw[ka];
            this.viewYaw[s] = y0 + shortAngle(y0, this.snapYaw[kb]) * u;
            const st0 = this.snapSteer[ka];
            this.viewSteer[s] = st0 + (this.snapSteer[kb] - st0) * u;

            this.viewFlags[s] = this.snapFlags[kb];
            this.viewDrift[s] = this.snapDrift[kb];
            this.viewLap[s] = this.snapLap[kb];
            this.viewPlace[s] = this.snapPlace[kb];
        }
    }

    /** Кадр истории как есть (заморозка). */
    _copyFrame(i) {
        const base = i * MAX_CARS;
        const sp = this.snapPresent;
        const present = this.viewPresent;
        for (let s = 0; s < MAX_CARS; s++) {
            const k = base + s;
            if (!sp[k]) { present[s] = 0; continue; }
            present[s] = 1;
            this._takeFrom(k, s);
        }
    }

    /** Кадр истории, сдвинутый вперёд по скорости на ahead секунд. */
    _extrapolateFrame(i, ahead) {
        const base = i * MAX_CARS;
        const sp = this.snapPresent;
        const present = this.viewPresent;
        for (let s = 0; s < MAX_CARS; s++) {
            const k = base + s;
            if (!sp[k]) { present[s] = 0; continue; }
            present[s] = 1;
            this._takeFrom(k, s);
            this.viewX[s] += this.snapVx[k] * ahead;
            this.viewZ[s] += this.snapVz[k] * ahead;
        }
    }

    _takeFrom(k, s) {
        this.viewX[s] = this.snapX[k];
        this.viewZ[s] = this.snapZ[k];
        this.viewYaw[s] = this.snapYaw[k];
        this.viewVx[s] = this.snapVx[k];
        this.viewVz[s] = this.snapVz[k];
        this.viewSteer[s] = this.snapSteer[k];
        this.viewFlags[s] = this.snapFlags[k];
        this.viewDrift[s] = this.snapDrift[k];
        this.viewLap[s] = this.snapLap[k];
        this.viewPlace[s] = this.snapPlace[k];
    }

    // =======================================================================
    // То, чего нет в снапшоте (12.6)
    // =======================================================================

    /**
     * Прогресс каждой машины по трассе — из позиции и числа кругов. Считается
     * раз в снапшот (20 Гц), а не в кадре: nearest_index стоит около 1 мкс
     * с хорошей подсказкой, восемь машин — меньше десяти.
     */
    _updateProgress(snap) {
        const track = this.track;
        if (!track) return;
        const length = track.length;
        const half = track.halfLength;
        const hints = this.progressHint;
        const progress = this.progress;

        let leader = -Infinity;
        let leaderSpeed = 0;

        for (let i = 0; i < snap.carCount; i++) {
            const slot = snap.carSlot[i];
            if (slot >= MAX_CARS) continue;
            const x = snap.carX[i];
            const z = snap.carZ[i];
            const idx = track.nearestIndex(x, z, hints[slot]);
            hints[slot] = idx;

            let along = (x - track.cx[idx]) * track.ctx[idx] + (z - track.cz[idx]) * track.ctz[idx];
            if (along > track.step) along = track.step;
            else if (along < -track.step) along = -track.step;
            let s = track.cs[idx] + along;
            if (s >= length) s -= length;
            else if (s < 0) s += length;

            const lap = snap.carLap[i];
            // Решётка стоит позади линии старта, поэтому на нулевом круге
            // хвост круга — это отрицательный прогресс (та же свёртка, что
            // в Track.initState).
            let p = lap * length + s;
            if (lap === 0 && s > half) p = s - length;
            progress[slot] = p;

            if (p > leader) {
                leader = p;
                const vx = snap.carVx[i];
                const vz = snap.carVz[i];
                leaderSpeed = Math.sqrt(vx * vx + vz * vz);
            }
            if (slot === this.localSlot) {
                this.place = snap.carPlace[i];
                this.lap = lap;
            }
        }

        this.leaderSpeed += (leaderSpeed - this.leaderSpeed) * 0.2;

        // Отставание в секундах. Опорная скорость — сглаженная скорость
        // лидера: она устойчивее собственной, которая у стоящей машины ноль.
        const mine = this.localSlot >= 0 ? this.progress[this.localSlot] : leader;
        let ref = this.leaderSpeed;
        if (!(ref > GAP_MIN_SPEED)) ref = GAP_MIN_SPEED;
        const behind = leader - mine;
        this.gap = behind > 0 ? behind / ref : 0;
    }

    /**
     * Время гонки по авторитетному тику снапшота, доведённое до «сейчас»
     * локальными часами. Отдельного события «время» в протоколе нет,
     * а sim.race_time равен tick * DT (12.4).
     */
    raceTime(nowMs) {
        if (this.snapRecvMs <= 0) return 0;
        // Вне RACING сервер не тикает: в отсчёте часы стоят на нуле, а на
        // экране итогов замирают на времени последнего снапшота.
        if (!this.racing) return this.snapRaceTime;
        const t = this.snapRaceTime + (nowMs - this.snapRecvMs) * 0.001;
        return t > 0 ? t : 0;
    }

    /** Время текущего круга, с. */
    currentLapTime(nowMs) {
        const t = this.raceTime(nowMs) - this.lapStartTime;
        this.lapTime = t > 0 ? t : 0;
        return this.lapTime;
    }

    /** Сводка для оверлея F3 и отладки. Объект переиспользуется. */
    getStats() {
        const s = this.stats;
        s.ping = this.ping;
        s.serverPing = this.serverPing;
        s.snapshotMs = this.snapIntervalMs;
        s.reconcileErr = this.reconcileError;
        s.reconcileMax = this.reconcileMax;
        s.reconcileRate = this.snapshotCount > 0
            ? this.reconcileCount / this.snapshotCount : 0;
        s.seq = this.seq;
        s.ackSeq = this.ackSeq;
        s.tickLead = this.tickLead;
        s.connected = this.connected;
        return s;
    }
}

export default NetClient;
