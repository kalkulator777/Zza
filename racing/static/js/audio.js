/*
 * Звук гонки: только WebAudio, ни одного файла-ассета (раздел 10.5 контракта).
 *
 * Сцена состоит из трёх слоёв:
 *
 *   1. Непрерывный слой — двигатель своей машины (пила + прямоугольник + саб
 *      через один фильтр нижних частот), до четырёх ближайших чужих двигателей
 *      с панорамой и затуханием по расстоянию, шум заноса и шорох травы.
 *      Все узлы этого слоя создаются один раз при инициализации и дальше
 *      только меняют параметры.
 *   2. Событийный слой — пулы голосов с жёстким потолком: тональные голоса
 *      (пила/прямоугольник/треугольник/синус через фильтр и панораму),
 *      шумовые голоса (общий зацикленный буфер через фильтр) и голоса
 *      интерфейса. Пул исчерпан — вытесняется самый старый голос, новых узлов
 *      не создаётся никогда.
 *   3. Шины: engine / fx / ui -> общий регулятор -> ограничитель -> выход.
 *      Ограничитель (DynamicsCompressor) стоит последним и страхует от
 *      клиппинга, когда всё звучит одновременно.
 *
 * ПОЧЕМУ ИМЕННО ТАК (бюджет раздела 1):
 *
 *   - Осцилляторы и источники шума нельзя перезапускать: `start()` у них
 *     одноразовый. Поэтому все они запускаются один раз и живут до конца,
 *     а «выключенный» голос — это голос с нулевой громкостью. Это же правило
 *     даёт ноль аллокаций в кадре: `update()` пишет только в AudioParam.
 *   - Буферы шума (белый и коричневый) генерируются один раз при сборке графа.
 *   - Каждое значение параметра кэшируется: если оно не изменилось заметно,
 *     вызова в аудиопоток не будет вовсе. Вызов AudioParam — это сообщение
 *     между потоками, на N5105 их стоит экономить.
 *   - Никаких присваиваний `param.value` в кадре: только `setTargetAtTime`
 *     и `linearRampToValueAtTime`, иначе в наушниках щелчки.
 *
 * ИСПОЛЬЗОВАНИЕ ИЗ main.js:
 *
 *   import { RaceAudio, createAudioState } from './audio.js';
 *
 *   const audio = new RaceAudio();
 *   audio.attachUnlockHandlers();          // контекст родится на первом клике
 *   const audioState = createAudioState(); // один объект на всё время жизни
 *
 *   // настройки из меню (раздел 12.6):
 *   audio.setVolume(s.volume, s.muted);
 *
 *   // на race_init:
 *   audio.setLocalSlot(mySlot);
 *   audio.startRace();
 *
 *   // каждый кадр: заполнить поля audioState и один вызов
 *   audio.update(audioState, frameDt);
 *
 *   // события сервера:
 *   audio.raceEvent(msg);      // kind: pickup|use|hit|lap|finish|drift_boost
 *   audio.countdown(msg.value);// 3, 2, 1 и 0 — «старт»
 *   audio.collision(force, slot);  // столкновения событием не приходят
 *
 *   // конец гонки:
 *   audio.stopRace();
 */

// --- ключи localStorage, раздел 12.6 ----------------------------------------

const LS_VOLUME = 'racing.volume';
const LS_MUTED = 'racing.muted';

// --- размеры сцены ----------------------------------------------------------

const MAX_SLOTS = 8;              // раздел 12.3, MAX_CARS
const REMOTE_ENGINES = 4;         // потолок одновременно звучащих чужих машин
const TONE_VOICES = 6;            // пул тональных голосов эффектов
const NOISE_VOICES = 4;           // пул шумовых голосов (2 белых + 2 коричневых)
const UI_VOICES = 3;              // пул голосов интерфейса
const NOISE_SECONDS = 2.0;        // длина зацикленного буфера шума

// --- шины -------------------------------------------------------------------

const BUS_ENGINE = 0;
const BUS_FX = 1;
const BUS_UI = 2;
const BUS_DEFAULT = [0.55, 0.62, 0.55];

// --- двигатель --------------------------------------------------------------

const IDLE_RPM = 0.12;            // холостые обороты в долях от отсечки
const ENGINE_F_BASE = 42.0;       // Гц на холостых (rpm = 0)
const ENGINE_F_SPAN = 232.0;      // Гц добавки к отсечке (rpm = 1)
const ENGINE_CUT_BASE = 300.0;    // Гц, срез фильтра на холостых
const ENGINE_CUT_SPAN = 2600.0;   // Гц, добавка среза к отсечке
const ENGINE_LEVEL_BASE = 0.16;   // громкость холостого гула
const ENGINE_LEVEL_SPAN = 0.34;   // добавка громкости к отсечке
const SHIFT_TIME = 0.16;          // с, длительность «переключения»
const SHIFT_DUCK = 0.42;          // во сколько раз проседает звук при сбросе
const REVERSE_TOP = 9.0;          // м/с, потолок заднего хода (REVERSE_MAX_SPEED)

/*
 * Имитация передач. Шесть передач, диапазоны СПЕЦИАЛЬНО перекрываются:
 * верх передачи g лежит выше низа передачи g+1, поэтому после переключения
 * обороты падают примерно до четверти шкалы — это и есть слышимый «сброс».
 * Числа подобраны под разброс машин из cars.json (max_speed 38..50 м/с,
 * boost_speed до 58 м/с), верх шестой передачи взят с запасом на турбо.
 */
const GEAR_COUNT = 6;
const GEAR_LOW = [0.0, 8.0, 15.0, 22.5, 31.0, 40.0];
const GEAR_HIGH = [10.5, 18.0, 26.0, 35.0, 45.0, 64.0];
const SHIFT_UP_MARGIN = 0.15;     // м/с до верха передачи, когда пора вверх
const SHIFT_DOWN_MARGIN = 0.9;    // м/с ниже низа передачи, когда пора вниз

// --- чужие двигатели --------------------------------------------------------

const REMOTE_MAX_DIST = 75.0;     // м, дальше машина молчит
const REMOTE_REF_DIST = 13.0;     // м, расстояние половинного затухания
const REMOTE_LEVEL = 0.30;        // громкость чужого двигателя вплотную
const REMOTE_F_BASE = 58.0;       // Гц на месте
const REMOTE_F_SPAN = 185.0;      // Гц добавки на максимальной скорости
const REMOTE_SPEED_REF = 45.0;    // м/с, скорость, считающаяся максимальной
const REMOTE_DETUNE = [-14, 9, -6, 17];   // центы, чтобы машины не сливались

// --- занос и трава ----------------------------------------------------------

const SKID_SLIP_MIN = 0.10;       // отношение |vLat|/|vFwd|, ниже тишина
const SKID_SLIP_SPAN = 0.45;      // ширина диапазона до полной громкости
const SKID_LEVEL = 0.42;
const SKID_F_BASE = 1250.0;
const SKID_F_SPAN = 2100.0;
const GRASS_LEVEL = 0.34;
const GRASS_F_BASE = 900.0;
const GRASS_F_SPAN = 1600.0;

// --- события ----------------------------------------------------------------

const EVENT_REF_DIST = 22.0;      // м, затухание событий чужих машин
const EVENT_MIN_GAIN = 0.06;      // тише этого событие не играется вовсе
const FADE = 0.004;               // с, гашение вытесняемого голоса

// Флаги машины в снапшоте — зеркало protocol.js (раздел 12.3). Дублируются,
// чтобы модуль звука оставался независимым от порядка загрузки протокола.
const FLAG_GHOST = 1 << 6;

// --- прочее -----------------------------------------------------------------

const TAU_FAST = 0.02;            // постоянные времени сглаживания параметров
const TAU_MED = 0.05;
const TAU_SLOW = 0.09;
const REMOTE_EVERY = 2;           // чужие двигатели обновляются через кадр
const SELECT_EVERY = 6;           // выбор ближайших — раз в шесть кадров

// Индексы кэша параметров (см. _write). Кэш нужен, чтобы не слать в аудиопоток
// значения, которые не изменились.
const P_ENG_FA = 0;
const P_ENG_FB = 1;
const P_ENG_FSUB = 2;
const P_ENG_CUT = 3;
const P_ENG_GAIN = 4;
const P_ENG_NOISE = 5;
const P_SKID_GAIN = 6;
const P_SKID_FREQ = 7;
const P_GRASS_GAIN = 8;
const P_GRASS_FREQ = 9;
const P_MASTER = 10;
const P_BUS = 11;                 // 11, 12, 13
const P_REMOTE = 16;              // по четыре ячейки на голос: gain, freq, cut, pan
const PARAM_CACHE_SIZE = P_REMOTE + REMOTE_ENGINES * 4;

// Скалярное состояние, которое меняется каждый кадр, живёт в Float64Array:
// запись double в обычное поле объекта V8 оборачивает в HeapNumber, то есть
// даёт аллокацию на каждый кадр. Запись в типизированный массив — нет.
const F_RPM = 0;          // текущие обороты, 0..1
const F_REV = 1;          // раскрутка на месте
const F_SHIFT = 2;        // остаток времени переключения передачи, с
const F_GRASS = 3;        // всплеск шороха травы, 0..1
const F_LX = 4;           // позиция слушателя
const F_LZ = 5;
const F_LFX = 6;          // вектор «вперёд» слушателя
const F_LFZ = 7;
const F_EV_PAN = 8;       // результат _locate()
const F_EV_GAIN = 9;
const FRAME_STATE_SIZE = 10;

// Пороги «заметного» изменения параметра.
const EPS_GAIN = 0.004;
const EPS_FREQ = 0.5;
const EPS_CUT = 8.0;
const EPS_PAN = 0.02;

// Частоты нот для сигналов (равномерный строй, A4 = 440).
const N_C5 = 523.25;
const N_A5 = 880.0;
const N_E5 = 659.25;
const N_G5 = 783.99;
const N_C6 = 1046.5;
const N_D6 = 1174.66;
const N_E6 = 1318.51;
const N_G6 = 1567.98;

// Рабочий массив частот арпеджио. Модуль однопоточный и синхронный, поэтому
// один буфер на всех безопасен и не даёт аллокаций.
const ARP = new Float64Array(6);

function clamp(v, lo, hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

/** Безопасное чтение localStorage: в приватном окне доступ может бросать. */
function lsGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
}

function lsSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* нет хранилища — живём без него */ }
}

/**
 * Снять запланированную автоматизацию, удержав текущее значение.
 * Firefox не знает `cancelAndHoldAtTime`, поэтому есть запасной путь.
 */
function cancelHold(param, now) {
    if (param.cancelAndHoldAtTime) {
        param.cancelAndHoldAtTime(now);
    } else {
        const v = param.value;
        param.cancelScheduledValues(now);
        param.setValueAtTime(v, now);
    }
}

/** Контекст по умолчанию. Зовётся только из unlock(), то есть по жесту. */
function defaultContextFactory() {
    if (typeof window === 'undefined') return null;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    return new Ctor({ latencyHint: 'interactive' });
}

/**
 * Кадровое состояние звука. Один объект на всё время жизни клиента: main.js
 * переписывает поля, RaceAudio.update() только читает. Массивы — по слотам
 * снапшота (раздел 12.3), carCount задаёт длину значащей части.
 *
 *   engineOn      двигатель своей машины звучит (гонка или обратный отсчёт)
 *   forwardSpeed  продольная скорость своей машины со знаком, м/с
 *   lateralSpeed  боковое скольжение своей машины, м/с
 *   throttle      нажат газ
 *   braking       нажат тормоз
 *   driftActive   идёт занос (ручник)
 *   offtrack      своя машина вне трассы
 *   boosting      активно ускорение
 *   spinning      машину крутит после попадания
 *   camX,camZ     позиция слушателя (камеры), м
 *   camYaw        курс камеры, рад; вперёд = (sin yaw, cos yaw), как в физике
 *   carSlot/X/Z   позиции всех машин из снапшота, включая свою
 *   carSpeed      модуль скорости машины, м/с
 *   carFlags      флаги машины из снапшота (protocol.js, FLAG_*)
 */
export function createAudioState() {
    return {
        engineOn: false,
        forwardSpeed: 0,
        lateralSpeed: 0,
        throttle: false,
        braking: false,
        driftActive: false,
        offtrack: false,
        boosting: false,
        spinning: false,
        camX: 0,
        camZ: 0,
        camYaw: 0,
        carCount: 0,
        carSlot: new Uint8Array(MAX_SLOTS),
        carX: new Float32Array(MAX_SLOTS),
        carZ: new Float32Array(MAX_SLOTS),
        carSpeed: new Float32Array(MAX_SLOTS),
        carFlags: new Uint8Array(MAX_SLOTS),
    };
}

export class RaceAudio {
    /**
     * @param {object} [options]
     *        createContext — фабрика контекста; подменяется в тестах, чтобы
     *                        отрендерить сцену в OfflineAudioContext;
     *        suspendWhenMuted — гасить контекст при выключенном звуке
     *                        (по умолчанию да: на N5105 это заметная экономия).
     */
    constructor(options) {
        const opts = options || null;
        this.createContext = (opts && opts.createContext) || defaultContextFactory;
        this.suspendWhenMuted = !(opts && opts.suspendWhenMuted === false);

        this.ctx = null;
        this.offline = false;       // контекст рендерится в буфер (тесты)
        this.active = false;        // граф собран и контекст играет
        this.failed = false;        // WebAudio недоступен, живём молча

        // Настройки берём из localStorage по ключам раздела 12.6. Меню пишет
        // туда же через saveUiSettings, дублирование записи безвредно.
        const volRaw = parseFloat(lsGet(LS_VOLUME));
        this.volume = Number.isFinite(volRaw) ? clamp(volRaw, 0, 1) : 0.7;
        this.muted = lsGet(LS_MUTED) === '1';

        this.busLevel = new Float64Array(3);
        this.busLevel[BUS_ENGINE] = BUS_DEFAULT[BUS_ENGINE];
        this.busLevel[BUS_FX] = BUS_DEFAULT[BUS_FX];
        this.busLevel[BUS_UI] = BUS_DEFAULT[BUS_UI];

        this.localSlot = -1;
        this.nodeCount = 0;         // счётчик созданных узлов, см. stats()
        this.frameNo = 0;

        // --- состояние имитации передач ---
        this.gear = 0;              // целое, в HeapNumber не уходит
        this.wasOfftrack = false;
        this.f = new Float64Array(FRAME_STATE_SIZE);   // см. F_* выше

        // --- кэш параметров ---
        this.paramCache = new Float64Array(PARAM_CACHE_SIZE);
        this.paramCache.fill(NaN);

        // --- позиции машин для панорамы событий ---
        this.slotX = new Float64Array(MAX_SLOTS);
        this.slotZ = new Float64Array(MAX_SLOTS);
        this.slotKnown = new Uint8Array(MAX_SLOTS);
        this.f[F_LFZ] = 1;
        this.f[F_EV_GAIN] = 1;

        // --- узлы: заполняются в _build() ---
        this.limiter = null;
        this.master = null;
        this.bus = null;            // [engine, fx, ui]
        this.whiteSrc = null;
        this.brownSrc = null;

        this.engOscA = null;
        this.engOscB = null;
        this.engOscSub = null;
        this.engFilter = null;
        this.engGain = null;
        this.engNoiseGain = null;
        this.engLfo = null;

        this.skidFilter = null;
        this.skidGain = null;
        this.grassFilter = null;
        this.grassGain = null;

        this.remoteOsc = new Array(REMOTE_ENGINES);
        this.remoteFilter = new Array(REMOTE_ENGINES);
        this.remotePan = new Array(REMOTE_ENGINES);
        this.remoteGain = new Array(REMOTE_ENGINES);
        this.remoteSlot = new Int8Array(REMOTE_ENGINES);
        this.remoteSlot.fill(-1);

        this.toneOsc = new Array(TONE_VOICES);
        this.toneFilter = new Array(TONE_VOICES);
        this.tonePan = new Array(TONE_VOICES);
        this.toneGain = new Array(TONE_VOICES);
        this.toneFree = new Float64Array(TONE_VOICES);
        this.toneStart = new Float64Array(TONE_VOICES);

        this.noiseFilter = new Array(NOISE_VOICES);
        this.noisePan = new Array(NOISE_VOICES);
        this.noiseGain = new Array(NOISE_VOICES);
        this.noiseFree = new Float64Array(NOISE_VOICES);
        this.noiseStart = new Float64Array(NOISE_VOICES);

        this.uiOsc = new Array(UI_VOICES);
        this.uiGain = new Array(UI_VOICES);
        this.uiFree = new Float64Array(UI_VOICES);
        this.uiStart = new Float64Array(UI_VOICES);

        // Выбор ближайших чужих машин: буферы фиксированного размера.
        this.nearSlot = new Int8Array(REMOTE_ENGINES);
        this.nearDist2 = new Float64Array(REMOTE_ENGINES);
        this.nearSpeed = new Float64Array(REMOTE_ENGINES);
        this.nearTaken = new Uint8Array(REMOTE_ENGINES);
        this.voiceTaken = new Uint8Array(REMOTE_ENGINES);
        this.voiceSpeed = new Float64Array(REMOTE_ENGINES);   // скорость машины голоса

        // Объект статистики переиспользуется, чтобы stats() не аллоцировал.
        this.statsOut = {
            nodeCount: 0, state: 'none', rpm: 0, gear: 0,
            voicesBusy: 0, remoteVoices: 0, volume: 0, muted: false,
        };

        this.unlockTarget = null;
        this.unlockHandler = null;
        this.muteTimer = 0;
    }

    // =======================================================================
    // Жизненный цикл контекста
    // =======================================================================

    /**
     * Повесить обработчики первого действия пользователя. Контекст создаётся
     * ровно в них: браузер не разрешает автозапуск звука до жеста.
     */
    attachUnlockHandlers(target) {
        if (this.unlockHandler || this.failed) return;
        const t = target || (typeof window !== 'undefined' ? window : null);
        if (!t) return;
        this.unlockTarget = t;
        this.unlockHandler = () => {
            if (this.unlock()) this._detachUnlockHandlers();
        };
        t.addEventListener('pointerdown', this.unlockHandler, true);
        t.addEventListener('keydown', this.unlockHandler, true);
        t.addEventListener('touchend', this.unlockHandler, true);
    }

    _detachUnlockHandlers() {
        const t = this.unlockTarget;
        const h = this.unlockHandler;
        if (!t || !h) return;
        t.removeEventListener('pointerdown', h, true);
        t.removeEventListener('keydown', h, true);
        t.removeEventListener('touchend', h, true);
        this.unlockTarget = null;
        this.unlockHandler = null;
    }

    /**
     * Создать контекст (если ещё нет) и попробовать его запустить.
     * Возвращает true, если звук играет. Звать только из обработчика жеста.
     */
    unlock() {
        if (this.failed) return false;
        if (this.ctx === null) {
            let ctx = null;
            try {
                ctx = this.createContext();
            } catch (e) {
                ctx = null;
            }
            if (!ctx) {
                // WebAudio нет или контекст не создался: дальше молчим, но
                // все публичные методы обязаны продолжать работать вхолостую.
                this.failed = true;
                return false;
            }
            this.ctx = ctx;
            this.offline = typeof ctx.startRendering === 'function';
            this._build();
            if (!this.offline) {
                ctx.onstatechange = () => this._syncState();
            }
        }
        this._resumeRaw();
        this._syncState();
        return this.active;
    }

    /** Возобновление после сворачивания вкладки или снятия выключателя. */
    resume() {
        if (this.ctx === null) return this.unlock();
        this._resumeRaw();
        this._syncState();
        return this.active;
    }

    /** Приостановить контекст (вкладка ушла в фон). */
    suspend() {
        if (this.ctx === null || this.offline) return;
        if (this.ctx.state === 'running') {
            const p = this.ctx.suspend();
            if (p && p.catch) p.catch(noop);
        }
        this._syncState();
    }

    _resumeRaw() {
        const ctx = this.ctx;
        if (!ctx || this.offline) return;
        if (ctx.state === 'suspended' && !(this.muted && this.suspendWhenMuted)) {
            const p = ctx.resume();
            if (p && p.catch) p.catch(noop);
        }
    }

    _syncState() {
        const ctx = this.ctx;
        if (!ctx) { this.active = false; return; }
        this.active = this.offline || ctx.state === 'running';
    }

    /** Играет ли звук прямо сейчас (контекст разрешён и не выключен). */
    isRunning() {
        return this.active && !this.muted;
    }

    /** Полностью освободить ресурсы: выход из гонки в меню этого не требует. */
    dispose() {
        this._detachUnlockHandlers();
        if (this.muteTimer) { clearTimeout(this.muteTimer); this.muteTimer = 0; }
        const ctx = this.ctx;
        this.ctx = null;
        this.active = false;
        if (ctx && !this.offline && ctx.close) {
            const p = ctx.close();
            if (p && p.catch) p.catch(noop);
        }
    }

    // =======================================================================
    // Громкость
    // =======================================================================

    /**
     * Регулятор из меню. `muted` можно не передавать — тогда выключатель
     * не трогается. Состояние пишется в localStorage (раздел 12.6).
     */
    setVolume(volume, muted, persist) {
        if (volume !== undefined && volume !== null && Number.isFinite(volume)) {
            this.volume = clamp(volume, 0, 1);
        }
        if (muted !== undefined && muted !== null) {
            this.muted = !!muted;
        }
        if (persist !== false) {
            lsSet(LS_VOLUME, String(this.volume));
            lsSet(LS_MUTED, this.muted ? '1' : '0');
        }
        this._applyMaster();
    }

    getVolume() { return this.volume; }

    isMuted() { return this.muted; }

    setMuted(muted) { this.setVolume(undefined, muted); }

    /**
     * Отдельная шина: 'engine' | 'fx' | 'ui'. Нужна, чтобы двигатель можно
     * было приглушить, не трогая сигналы интерфейса.
     */
    setBusVolume(name, value) {
        const idx = name === 'engine' ? BUS_ENGINE
            : name === 'fx' ? BUS_FX
                : name === 'ui' ? BUS_UI : -1;
        if (idx < 0 || !Number.isFinite(value)) return;
        this.busLevel[idx] = clamp(value, 0, 2);
        if (!this.bus) return;
        const now = this.ctx.currentTime;
        this._write(this.bus[idx].gain, this.busLevel[idx], P_BUS + idx, EPS_GAIN, now, TAU_MED);
    }

    _applyMaster() {
        if (!this.master) return;
        const target = this.muted ? 0 : this.volume;
        const now = this.ctx.currentTime;
        this._write(this.master.gain, target, P_MASTER, 0, now, 0.03);

        if (this.offline || !this.suspendWhenMuted) return;
        // Выключенный звук незачем считать: гасим контекст, но только после
        // того, как громкость успела съехать в ноль — иначе будет щелчок.
        if (this.muteTimer) { clearTimeout(this.muteTimer); this.muteTimer = 0; }
        if (this.muted) {
            this.muteTimer = setTimeout(() => {
                this.muteTimer = 0;
                if (this.muted) this.suspend();
            }, 250);
        } else {
            this._resumeRaw();
            this._syncState();
        }
    }

    // =======================================================================
    // Сборка графа. Всё, что вообще будет звучать, создаётся здесь и только
    // здесь. После выхода из _build() узлы больше не создаются никогда.
    // =======================================================================

    _build() {
        const ctx = this.ctx;
        const t0 = ctx.currentTime;

        // --- выход: регулятор и ограничитель ---
        this.limiter = this._count(ctx.createDynamicsCompressor());
        this.limiter.threshold.value = -8;
        this.limiter.knee.value = 8;
        this.limiter.ratio.value = 12;
        this.limiter.attack.value = 0.004;
        this.limiter.release.value = 0.25;
        this.limiter.connect(ctx.destination);

        this.master = this._gain(this.muted ? 0 : this.volume);
        this.master.connect(this.limiter);
        this.paramCache[P_MASTER] = this.muted ? 0 : this.volume;

        this.bus = [
            this._gain(this.busLevel[BUS_ENGINE]),
            this._gain(this.busLevel[BUS_FX]),
            this._gain(this.busLevel[BUS_UI]),
        ];
        for (let i = 0; i < 3; i++) {
            this.bus[i].connect(this.master);
            this.paramCache[P_BUS + i] = this.busLevel[i];
        }

        // --- источники шума: два зацикленных буфера на всю сцену ---
        this.whiteSrc = this._noiseSource(false, t0);
        this.brownSrc = this._noiseSource(true, t0);

        this._buildOwnEngine(t0);
        this._buildSurfaces();
        this._buildRemoteEngines(t0);
        this._buildVoices(t0);
    }

    _buildOwnEngine(t0) {
        const ctx = this.ctx;

        this.engGain = this._gain(0);
        this.engGain.connect(this.bus[BUS_ENGINE]);

        this.engFilter = this._count(ctx.createBiquadFilter());
        this.engFilter.type = 'lowpass';
        this.engFilter.frequency.value = ENGINE_CUT_BASE;
        this.engFilter.Q.value = 5.0;
        this.engFilter.connect(this.engGain);

        // Пила — основное тело звука, прямоугольник даёт «жестяной» призвук,
        // синус октавой ниже — низ. Балансные гейны выставляются один раз.
        this.engOscA = this._osc('sawtooth', ENGINE_F_BASE, t0);
        this.engOscA.detune.value = -6;
        const mixA = this._gain(0.55);
        this.engOscA.connect(mixA);
        mixA.connect(this.engFilter);

        this.engOscB = this._osc('square', ENGINE_F_BASE * 1.005, t0);
        this.engOscB.detune.value = 9;
        const mixB = this._gain(0.26);
        this.engOscB.connect(mixB);
        mixB.connect(this.engFilter);

        this.engOscSub = this._osc('sine', ENGINE_F_BASE * 0.5, t0);
        const mixSub = this._gain(0.5);
        this.engOscSub.connect(mixSub);
        mixSub.connect(this.engFilter);

        // Лёгкое «дыхание» холостого хода: медленный LFO в расстройку пилы.
        this.engLfo = this._osc('sine', 5.5, t0);
        const lfoDepth = this._gain(7);       // центы
        this.engLfo.connect(lfoDepth);
        lfoDepth.connect(this.engOscA.detune);

        // Воздух: полосовой шум поверх осцилляторов, слышен на высоких оборотах
        // и особенно на турбо.
        const noiseFilter = this._count(ctx.createBiquadFilter());
        noiseFilter.type = 'bandpass';
        noiseFilter.frequency.value = 900;
        noiseFilter.Q.value = 0.8;
        this.engNoiseGain = this._gain(0);
        this.whiteSrc.connect(noiseFilter);
        noiseFilter.connect(this.engNoiseGain);
        this.engNoiseGain.connect(this.bus[BUS_ENGINE]);
    }

    _buildSurfaces() {
        const ctx = this.ctx;

        // Занос: полосовой белый шум, громкость от бокового скольжения.
        this.skidFilter = this._count(ctx.createBiquadFilter());
        this.skidFilter.type = 'bandpass';
        this.skidFilter.frequency.value = SKID_F_BASE;
        this.skidFilter.Q.value = 0.9;
        this.skidGain = this._gain(0);
        this.whiteSrc.connect(this.skidFilter);
        this.skidFilter.connect(this.skidGain);
        this.skidGain.connect(this.bus[BUS_FX]);

        // Трава: белый шум через широкую полосу — шорох и шелест. Коричневый
        // шум здесь не годится: почти вся его энергия ниже 100 Гц, на полосе
        // около 1,5 кГц от него не остаётся ничего слышимого (замерено).
        this.grassFilter = this._count(ctx.createBiquadFilter());
        this.grassFilter.type = 'bandpass';
        this.grassFilter.frequency.value = GRASS_F_BASE;
        this.grassFilter.Q.value = 0.5;
        this.grassGain = this._gain(0);
        this.whiteSrc.connect(this.grassFilter);
        this.grassFilter.connect(this.grassGain);
        this.grassGain.connect(this.bus[BUS_FX]);
    }

    _buildRemoteEngines(t0) {
        const ctx = this.ctx;
        for (let i = 0; i < REMOTE_ENGINES; i++) {
            const gain = this._gain(0);
            gain.connect(this.bus[BUS_ENGINE]);

            const pan = this._count(ctx.createStereoPanner());
            pan.connect(gain);

            const filter = this._count(ctx.createBiquadFilter());
            filter.type = 'lowpass';
            filter.frequency.value = 900;
            filter.Q.value = 3.0;
            filter.connect(pan);

            const osc = this._osc('sawtooth', REMOTE_F_BASE, t0);
            osc.detune.value = REMOTE_DETUNE[i];
            osc.connect(filter);

            this.remoteOsc[i] = osc;
            this.remoteFilter[i] = filter;
            this.remotePan[i] = pan;
            this.remoteGain[i] = gain;
        }
    }

    _buildVoices(t0) {
        const ctx = this.ctx;

        // Тональные голоса эффектов.
        for (let i = 0; i < TONE_VOICES; i++) {
            const gain = this._gain(0);
            const pan = this._count(ctx.createStereoPanner());
            const filter = this._count(ctx.createBiquadFilter());
            filter.type = 'lowpass';
            filter.frequency.value = 6000;
            filter.Q.value = 1.0;
            const osc = this._osc('square', 440, t0);
            osc.connect(filter);
            filter.connect(pan);
            pan.connect(gain);
            gain.connect(this.bus[BUS_FX]);
            this.toneOsc[i] = osc;
            this.toneFilter[i] = filter;
            this.tonePan[i] = pan;
            this.toneGain[i] = gain;
        }

        // Шумовые голоса: первая половина от белого шума, вторая — от
        // коричневого. Источники общие, у голоса свой фильтр и своя панорама.
        for (let i = 0; i < NOISE_VOICES; i++) {
            const gain = this._gain(0);
            const pan = this._count(ctx.createStereoPanner());
            const filter = this._count(ctx.createBiquadFilter());
            filter.type = 'lowpass';
            filter.frequency.value = 2000;
            filter.Q.value = 1.0;
            const src = i < (NOISE_VOICES >> 1) ? this.whiteSrc : this.brownSrc;
            src.connect(filter);
            filter.connect(pan);
            pan.connect(gain);
            gain.connect(this.bus[BUS_FX]);
            this.noiseFilter[i] = filter;
            this.noisePan[i] = pan;
            this.noiseGain[i] = gain;
        }

        // Голоса интерфейса: без фильтра и панорамы, всегда по центру.
        for (let i = 0; i < UI_VOICES; i++) {
            const gain = this._gain(0);
            const osc = this._osc('triangle', 440, t0);
            osc.connect(gain);
            gain.connect(this.bus[BUS_UI]);
            this.uiOsc[i] = osc;
            this.uiGain[i] = gain;
        }
    }

    // --- фабрики узлов: единственное место, где узлы создаются -------------

    _count(node) {
        this.nodeCount++;
        return node;
    }

    _gain(value) {
        const g = this._count(this.ctx.createGain());
        g.gain.value = value;
        return g;
    }

    _osc(type, freq, when) {
        const o = this._count(this.ctx.createOscillator());
        o.type = type;
        o.frequency.value = freq;
        o.start(when);
        return o;
    }

    /**
     * Зацикленный буфер шума. Генерируется один раз: в кадре шум не рождается
     * никогда. Коричневый вариант — проинтегрированный белый с нормировкой,
     * он даёт низкий шорох для травы и глухие удары.
     */
    _noiseSource(brown, when) {
        const ctx = this.ctx;
        const len = Math.floor(ctx.sampleRate * NOISE_SECONDS);
        const buf = ctx.createBuffer(1, len, ctx.sampleRate);
        const data = buf.getChannelData(0);
        if (brown) {
            let last = 0;
            let peak = 1e-6;
            for (let i = 0; i < len; i++) {
                const w = Math.random() * 2 - 1;
                last = (last + 0.035 * w) / 1.035;
                data[i] = last;
                const a = last < 0 ? -last : last;
                if (a > peak) peak = a;
            }
            const norm = 0.9 / peak;
            for (let i = 0; i < len; i++) data[i] *= norm;
        } else {
            for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1;
        }
        // Сшивка петли: первые и последние 256 отсчётов сводятся крест-накрест,
        // иначе на стыке щёлкает раз в две секунды.
        const fade = 256;
        for (let i = 0; i < fade; i++) {
            const k = i / fade;
            const a = data[i];
            const b = data[len - fade + i];
            data[i] = a * k + b * (1 - k);
        }
        const src = this._count(ctx.createBufferSource());
        src.buffer = buf;
        src.loop = true;
        src.start(when);
        return src;
    }

    // =======================================================================
    // Кадровое обновление
    // =======================================================================

    /** Гонка началась: сбросить передачу и обороты. */
    startRace() {
        this.gear = 0;
        this.f[F_RPM] = 0;
        this.f[F_REV] = 0;
        this.f[F_SHIFT] = 0;
        this.f[F_GRASS] = 0;
        this.wasOfftrack = false;
        for (let i = 0; i < REMOTE_ENGINES; i++) this.remoteSlot[i] = -1;
        for (let i = 0; i < MAX_SLOTS; i++) this.slotKnown[i] = 0;
    }

    /** Гонка кончилась: всё непрерывное плавно в ноль. */
    stopRace() {
        if (!this.active) return;
        const now = this.ctx.currentTime;
        this._write(this.engGain.gain, 0, P_ENG_GAIN, 0, now, 0.12);
        this._write(this.engNoiseGain.gain, 0, P_ENG_NOISE, 0, now, 0.12);
        this._write(this.skidGain.gain, 0, P_SKID_GAIN, 0, now, 0.08);
        this._write(this.grassGain.gain, 0, P_GRASS_GAIN, 0, now, 0.08);
        for (let i = 0; i < REMOTE_ENGINES; i++) {
            this._write(this.remoteGain[i].gain, 0, P_REMOTE + i * 4, 0, now, 0.12);
            this.remoteSlot[i] = -1;
        }
        this.f[F_RPM] = 0;
        this.gear = 0;
    }

    /** Свой слот из welcome/race_init: по нему отличаем свои события от чужих. */
    setLocalSlot(slot) {
        this.localSlot = (slot === undefined || slot === null) ? -1 : slot | 0;
    }

    /**
     * Единственный вызов в кадровом цикле. Ничего не создаёт и не аллоцирует:
     * только считает скаляры и пишет в AudioParam.
     */
    update(state, dt) {
        if (!this.active || !state) return;
        const d = (dt > 0 && dt < 0.25) ? dt : 0.016;
        const now = this.ctx.currentTime;
        this.frameNo++;

        this._updateListener(state);
        this._updateEngine(state, d, now);
        this._updateSurfaces(state, d, now);
        if ((this.frameNo % REMOTE_EVERY) === 0) {
            this._updateRemote(state, now, (this.frameNo % SELECT_EVERY) === 0);
        }
    }

    /** Запомнить позу камеры и позиции машин — нужны для панорамы событий. */
    _updateListener(state) {
        const f = this.f;
        f[F_LX] = state.camX;
        f[F_LZ] = state.camZ;
        const yaw = state.camYaw;
        f[F_LFX] = Math.sin(yaw);
        f[F_LFZ] = Math.cos(yaw);

        const known = this.slotKnown;
        for (let i = 0; i < MAX_SLOTS; i++) known[i] = 0;
        const n = state.carCount < MAX_SLOTS ? state.carCount : MAX_SLOTS;
        for (let i = 0; i < n; i++) {
            const slot = state.carSlot[i];
            if (slot >= MAX_SLOTS) continue;
            this.slotX[slot] = state.carX[i];
            this.slotZ[slot] = state.carZ[i];
            known[slot] = 1;
        }
    }

    /**
     * Двигатель своей машины.
     *
     * Обороты считаются от продольной скорости через таблицу передач:
     * внутри передачи обороты линейно идут от низа к верху диапазона, на
     * верхней границе происходит переключение — и, поскольку диапазоны
     * перекрываются, обороты падают примерно до четверти шкалы. На время
     * переключения звук дополнительно приглушается (сброс газа), а
     * сглаживание оборотов делается вдвое быстрее, чтобы падение читалось
     * как переключение, а не как торможение.
     */
    _updateEngine(state, dt, now) {
        const fwd = state.forwardSpeed;
        const aspeed = fwd < 0 ? -fwd : fwd;
        let rpmTarget;

        const f = this.f;
        if (f[F_SHIFT] > 0) f[F_SHIFT] -= dt;

        if (!state.engineOn) {
            rpmTarget = 0;
            this.gear = 0;
            f[F_REV] = 0;
        } else if (fwd < -0.5) {
            // Задний ход: одна «передача», вой выше и тише.
            if (this.gear !== -1) {
                this.gear = -1;
                f[F_SHIFT] = SHIFT_TIME * 0.5;
            }
            rpmTarget = IDLE_RPM + 0.55 * clamp(aspeed / REVERSE_TOP, 0, 1);
        } else {
            if (this.gear < 0) {
                this.gear = 0;
                f[F_SHIFT] = SHIFT_TIME * 0.5;
            }
            let g = this.gear;
            if (f[F_SHIFT] <= 0) {
                if (g < GEAR_COUNT - 1 && aspeed > GEAR_HIGH[g] - SHIFT_UP_MARGIN) {
                    g++;
                    f[F_SHIFT] = SHIFT_TIME;
                } else if (g > 0 && aspeed < GEAR_LOW[g] - SHIFT_DOWN_MARGIN) {
                    g--;
                    f[F_SHIFT] = SHIFT_TIME * 0.6;
                }
                this.gear = g;
            }
            const lo = GEAR_LOW[g];
            const hi = GEAR_HIGH[g];
            rpmTarget = IDLE_RPM + (1 - IDLE_RPM) * clamp((aspeed - lo) / (hi - lo), 0, 1);

            // Раскрутка на месте: газ в пол на старте поднимает обороты
            // без скорости — сцепление проскальзывает.
            if (aspeed < 2.0) {
                f[F_REV] = clamp(f[F_REV] + (state.throttle ? dt * 2.0 : -dt * 2.8), 0, 1);
                const revRpm = IDLE_RPM + 0.42 * f[F_REV];
                if (revRpm > rpmTarget) rpmTarget = revRpm;
            } else if (f[F_REV] > 0) {
                f[F_REV] = clamp(f[F_REV] - dt * 2.8, 0, 1);
            }

            if (!state.throttle && aspeed > 2.0) rpmTarget *= 0.88;
            if (state.braking) rpmTarget *= 0.94;
        }

        if (state.boosting && rpmTarget > 0) {
            rpmTarget = clamp(rpmTarget * 1.05 + 0.05, 0, 1.08);
        }
        if (state.spinning && rpmTarget > 0) {
            rpmTarget *= 0.8;   // машину крутит, газ не в счёт
        }

        // Сглаживание оборотов: на переключении быстрее, иначе мягче.
        const tau = f[F_SHIFT] > 0 ? 0.05 : 0.11;
        const rpm = f[F_RPM] + (rpmTarget - f[F_RPM]) * clamp(dt / tau, 0, 1);
        f[F_RPM] = rpm;

        let freq = ENGINE_F_BASE + ENGINE_F_SPAN * rpm;
        if (this.gear === -1) freq *= 1.22;

        let level = state.engineOn ? (ENGINE_LEVEL_BASE + ENGINE_LEVEL_SPAN * rpm) : 0;
        if (state.engineOn) {
            if (!state.throttle) level *= 0.74;
            if (f[F_SHIFT] > 0) level *= SHIFT_DUCK;
            if (this.gear === -1) level *= 0.8;
        }

        let cut = ENGINE_CUT_BASE + ENGINE_CUT_SPAN * rpm;
        if (state.throttle) cut += 500;
        if (state.boosting) cut += 800;

        const noiseLevel = state.engineOn
            ? (0.010 + 0.030 * rpm + (state.boosting ? 0.055 : 0))
            : 0;

        this._write(this.engOscA.frequency, freq, P_ENG_FA, EPS_FREQ, now, TAU_FAST);
        this._write(this.engOscB.frequency, freq * 1.005, P_ENG_FB, EPS_FREQ, now, TAU_FAST);
        this._write(this.engOscSub.frequency, freq * 0.5, P_ENG_FSUB, EPS_FREQ, now, TAU_FAST);
        this._write(this.engFilter.frequency, cut, P_ENG_CUT, EPS_CUT, now, TAU_MED);
        this._write(this.engGain.gain, level, P_ENG_GAIN, EPS_GAIN, now, TAU_MED);
        this._write(this.engNoiseGain.gain, noiseLevel, P_ENG_NOISE, EPS_GAIN, now, TAU_MED);
    }

    /** Занос и трава: два непрерывных шумовых слоя. */
    _updateSurfaces(state, dt, now) {
        const fwd = state.forwardSpeed;
        const aspeed = fwd < 0 ? -fwd : fwd;
        const lat = state.lateralSpeed < 0 ? -state.lateralSpeed : state.lateralSpeed;

        // Отношение бокового скольжения к продольной скорости: именно оно, а не
        // абсолютная величина, определяет, «визжит» ли резина.
        const slip = lat / (aspeed > 6 ? aspeed : 6);
        let skid = clamp((slip - SKID_SLIP_MIN) / SKID_SLIP_SPAN, 0, 1);
        skid *= clamp(aspeed / 12, 0, 1);
        if (state.driftActive) skid = clamp(skid + 0.22, 0, 1);
        if (state.offtrack) skid *= 0.45;      // на траве резина не визжит
        if (!state.engineOn) skid = 0;

        const skidLevel = skid * SKID_LEVEL;
        const skidFreq = SKID_F_BASE + SKID_F_SPAN * clamp(slip, 0, 1);

        // Трава: шорох от скорости плюс всплеск в момент вылета.
        const f = this.f;
        if (state.offtrack && !this.wasOfftrack) f[F_GRASS] = 1.0;
        this.wasOfftrack = !!state.offtrack;
        if (f[F_GRASS] > 0) f[F_GRASS] = clamp(f[F_GRASS] - dt * 2.2, 0, 1);

        let grass = 0;
        if (state.engineOn && state.offtrack) {
            grass = clamp(aspeed / 22, 0, 1) * (1 + 0.8 * f[F_GRASS]);
        }
        const grassLevel = clamp(grass, 0, 1.4) * GRASS_LEVEL;
        const grassFreq = GRASS_F_BASE + GRASS_F_SPAN * clamp(aspeed / 30, 0, 1);

        this._write(this.skidGain.gain, skidLevel, P_SKID_GAIN, EPS_GAIN, now, 0.035);
        this._write(this.skidFilter.frequency, skidFreq, P_SKID_FREQ, EPS_CUT, now, TAU_MED);
        this._write(this.grassGain.gain, grassLevel, P_GRASS_GAIN, EPS_GAIN, now, 0.04);
        this._write(this.grassFilter.frequency, grassFreq, P_GRASS_FREQ, EPS_CUT, now, TAU_MED);
    }

    /**
     * Чужие двигатели. Звучат только четыре ближайшие машины: дальше разницы
     * всё равно не слышно, а процессор платит за каждый голос.
     */
    _updateRemote(state, now, reselect) {
        if (reselect) this._selectNearest(state);

        for (let v = 0; v < REMOTE_ENGINES; v++) {
            const base = P_REMOTE + v * 4;
            const slot = this.remoteSlot[v];
            if (slot < 0) {
                this._write(this.remoteGain[v].gain, 0, base, EPS_GAIN, now, TAU_SLOW);
                continue;
            }
            const dx = this.slotX[slot] - this.f[F_LX];
            const dz = this.slotZ[slot] - this.f[F_LZ];
            const dist2 = dx * dx + dz * dz;
            const dist = Math.sqrt(dist2);

            // Затухание: спад примерно как 1/(1 + (d/ref)^2) плюс жёсткий срез
            // на REMOTE_MAX_DIST, чтобы дальние машины не подмешивали кашу.
            let att = 0;
            if (dist < REMOTE_MAX_DIST) {
                const k = dist / REMOTE_REF_DIST;
                att = 1 / (1 + k * k);
                att *= clamp((REMOTE_MAX_DIST - dist) / 20, 0, 1);
            }
            const speed = this.voiceSpeed[v];
            const gain = att * REMOTE_LEVEL;
            const freq = REMOTE_F_BASE + REMOTE_F_SPAN * clamp(speed / REMOTE_SPEED_REF, 0, 1);
            const cut = 420 + 1500 * clamp(speed / REMOTE_SPEED_REF, 0, 1);

            // Панорама: проекция на вектор «вправо» камеры = (cos yaw, -sin yaw).
            let pan = 0;
            if (dist > 0.001) {
                pan = clamp((dx * this.f[F_LFZ] - dz * this.f[F_LFX]) / dist, -1, 1);
                pan *= clamp(dist / 4, 0, 1);   // вплотную — по центру, без скачков
            }

            this._write(this.remoteGain[v].gain, gain, base, EPS_GAIN, now, TAU_SLOW);
            this._write(this.remoteOsc[v].frequency, freq, base + 1, EPS_FREQ, now, TAU_MED);
            this._write(this.remoteFilter[v].frequency, cut, base + 2, EPS_CUT, now, TAU_MED);
            this._write(this.remotePan[v].pan, pan, base + 3, EPS_PAN, now, TAU_SLOW);
        }
    }

    /**
     * Выбрать REMOTE_ENGINES ближайших чужих машин и раздать их голосам.
     * Голос по возможности сохраняет свою машину: перескок голоса с машины
     * на машину слышен как щелчок тембра.
     */
    _selectNearest(state) {
        const nearSlot = this.nearSlot;
        const nearDist2 = this.nearDist2;
        const nearSpeed = this.nearSpeed;
        let count = 0;

        const n = state.carCount < MAX_SLOTS ? state.carCount : MAX_SLOTS;
        for (let i = 0; i < n; i++) {
            const slot = state.carSlot[i];
            if (slot === this.localSlot || slot >= MAX_SLOTS) continue;
            const flags = state.carFlags[i];
            if (flags & FLAG_GHOST) continue;   // отключившийся молчит
            const dx = state.carX[i] - this.f[F_LX];
            const dz = state.carZ[i] - this.f[F_LZ];
            const d2 = dx * dx + dz * dz;
            if (d2 > REMOTE_MAX_DIST * REMOTE_MAX_DIST) continue;

            // Вставка в отсортированный список фиксированной длины.
            let pos = count < REMOTE_ENGINES ? count : REMOTE_ENGINES;
            while (pos > 0 && nearDist2[pos - 1] > d2) pos--;
            if (pos >= REMOTE_ENGINES) continue;
            for (let k = (count < REMOTE_ENGINES ? count : REMOTE_ENGINES - 1); k > pos; k--) {
                nearDist2[k] = nearDist2[k - 1];
                nearSlot[k] = nearSlot[k - 1];
                nearSpeed[k] = nearSpeed[k - 1];
            }
            nearDist2[pos] = d2;
            nearSlot[pos] = slot;
            nearSpeed[pos] = state.carSpeed[i];
            if (count < REMOTE_ENGINES) count++;
        }

        const taken = this.nearTaken;
        const used = this.voiceTaken;
        for (let i = 0; i < REMOTE_ENGINES; i++) { taken[i] = 0; used[i] = 0; }

        // Шаг 1: голоса, чья машина всё ещё в списке, остаются на ней.
        for (let v = 0; v < REMOTE_ENGINES; v++) {
            const slot = this.remoteSlot[v];
            if (slot < 0) continue;
            let found = -1;
            for (let i = 0; i < count; i++) {
                if (nearSlot[i] === slot && !taken[i]) { found = i; break; }
            }
            if (found >= 0) {
                taken[found] = 1;
                used[v] = 1;
                this.voiceSpeed[v] = nearSpeed[found];
            } else {
                this.remoteSlot[v] = -1;
            }
        }
        // Шаг 2: оставшиеся машины раздаются свободным голосам.
        for (let i = 0; i < count; i++) {
            if (taken[i]) continue;
            for (let v = 0; v < REMOTE_ENGINES; v++) {
                if (used[v]) continue;
                used[v] = 1;
                taken[i] = 1;
                this.remoteSlot[v] = nearSlot[i];
                this.voiceSpeed[v] = nearSpeed[i];
                // Голос переезжает на другую машину: сбрасываем кэш всех
                // четырёх параметров, чтобы тот же кадр записал новые значения.
                // Гасить голос рампой здесь НЕЛЬЗЯ — рампа встанет в списке
                // автоматизации после setTargetAtTime из _updateRemote и
                // намертво прижмёт громкость к нулю. Плавность даёт сам
                // setTargetAtTime: и громкость, и частота приезжают за ~0,1 с.
                const base = P_REMOTE + v * 4;
                this.paramCache[base] = NaN;
                this.paramCache[base + 1] = NaN;
                this.paramCache[base + 2] = NaN;
                this.paramCache[base + 3] = NaN;
                break;
            }
        }
        // Скорости голосов без машины обнуляем, чтобы не тянуть старое значение.
        for (let v = 0; v < REMOTE_ENGINES; v++) {
            if (this.remoteSlot[v] < 0) this.voiceSpeed[v] = 0;
        }
    }

    /** Запись в AudioParam с кэшем: одинаковые значения в аудиопоток не идут. */
    _write(param, value, idx, eps, now, tau) {
        const prev = this.paramCache[idx];
        if (prev === prev) {                       // не NaN
            const diff = value > prev ? value - prev : prev - value;
            if (diff <= eps) return;
        }
        this.paramCache[idx] = value;
        param.setTargetAtTime(value, now, tau);
    }

    // =======================================================================
    // Пулы голосов
    // =======================================================================

    /** Свободный тональный голос; если свободных нет — вытесняется старейший. */
    _allocTone(now) {
        let oldest = 0;
        let oldestAt = Infinity;
        for (let i = 0; i < TONE_VOICES; i++) {
            if (this.toneFree[i] <= now) return i;
            if (this.toneStart[i] < oldestAt) { oldestAt = this.toneStart[i]; oldest = i; }
        }
        return oldest;
    }

    _allocNoise(now, brown) {
        const half = NOISE_VOICES >> 1;
        const from = brown ? half : 0;
        const to = brown ? NOISE_VOICES : half;
        let oldest = from;
        let oldestAt = Infinity;
        for (let i = from; i < to; i++) {
            if (this.noiseFree[i] <= now) return i;
            if (this.noiseStart[i] < oldestAt) { oldestAt = this.noiseStart[i]; oldest = i; }
        }
        return oldest;
    }

    _allocUi(now) {
        let oldest = 0;
        let oldestAt = Infinity;
        for (let i = 0; i < UI_VOICES; i++) {
            if (this.uiFree[i] <= now) return i;
            if (this.uiStart[i] < oldestAt) { oldestAt = this.uiStart[i]; oldest = i; }
        }
        return oldest;
    }

    /**
     * Конверт громкости голоса. Начинается с гашения того, что играло раньше
     * (вытеснение из пула), дальше атака и спад. Резких присваиваний нет:
     * только линейные рампы и setTargetAtTime.
     * Возвращает время, когда голос освободится.
     */
    _envelope(param, now, peak, attack, decay) {
        cancelHold(param, now);
        param.linearRampToValueAtTime(0, now + FADE);
        const top = now + FADE + attack;
        param.linearRampToValueAtTime(peak, top);
        param.setTargetAtTime(0, top, decay * 0.32);
        const end = top + decay;
        param.linearRampToValueAtTime(0, end);
        return end;
    }

    /**
     * Тональный голос: волна `type`, частота идёт от f0 к f1 за `sweep`,
     * фильтр от cut0 к cut1, громкость по конверту attack/decay.
     */
    _tone(type, f0, f1, sweep, peak, attack, decay, cut0, cut1, pan) {
        const now = this.ctx.currentTime;
        const i = this._allocTone(now);
        const osc = this.toneOsc[i];
        const filter = this.toneFilter[i];

        osc.type = type;
        // Скачок частоты фазу не рвёт, щелчка не даёт: сначала ставим точку
        // отсчёта, потом ведём рампой.
        osc.frequency.cancelScheduledValues(now);
        osc.frequency.setValueAtTime(f0, now);
        if (sweep > 0 && f1 !== f0) {
            osc.frequency.exponentialRampToValueAtTime(f1 > 1 ? f1 : 1, now + sweep);
        }
        filter.type = 'lowpass';
        filter.frequency.cancelScheduledValues(now);
        filter.frequency.setValueAtTime(cut0, now);
        if (cut1 !== cut0) {
            filter.frequency.exponentialRampToValueAtTime(cut1 > 1 ? cut1 : 1, now + (sweep > 0 ? sweep : decay));
        }
        this.tonePan[i].pan.setTargetAtTime(pan, now, 0.003);

        this.toneStart[i] = now;
        this.toneFree[i] = this._envelope(this.toneGain[i].gain, now, peak, attack, decay);
        return i;
    }

    /**
     * Шумовой голос: полоса фильтра идёт от f0 к f1, тип фильтра задаётся
     * явно (lowpass — удары и гром, bandpass — свист и шорох).
     */
    _noise(brown, filterType, f0, f1, q, peak, attack, decay, pan) {
        const now = this.ctx.currentTime;
        const i = this._allocNoise(now, brown);
        const filter = this.noiseFilter[i];
        filter.type = filterType;
        filter.Q.setValueAtTime(q, now);
        filter.frequency.cancelScheduledValues(now);
        filter.frequency.setValueAtTime(f0, now);
        if (f1 !== f0) {
            filter.frequency.exponentialRampToValueAtTime(f1 > 1 ? f1 : 1, now + attack + decay);
        }
        this.noisePan[i].pan.setTargetAtTime(pan, now, 0.003);

        this.noiseStart[i] = now;
        this.noiseFree[i] = this._envelope(this.noiseGain[i].gain, now, peak, attack, decay);
        return i;
    }

    /**
     * Арпеджио на голосе интерфейса: `count` нот из ARP, по `step` секунд.
     * Одним голосом — потому что ноты не перекрываются.
     */
    _uiArp(type, count, step, peak, noteDecay) {
        const now = this.ctx.currentTime;
        const i = this._allocUi(now);
        const osc = this.uiOsc[i];
        const gain = this.uiGain[i].gain;
        osc.type = type;
        osc.frequency.cancelScheduledValues(now);

        cancelHold(gain, now);
        gain.linearRampToValueAtTime(0, now + FADE);
        let t = now + FADE;
        for (let k = 0; k < count; k++) {
            osc.frequency.setValueAtTime(ARP[k], t);
            gain.linearRampToValueAtTime(peak, t + 0.010);
            gain.setTargetAtTime(0, t + 0.010, noteDecay * 0.3);
            if (k < count - 1) gain.linearRampToValueAtTime(peak * 0.05, t + step);
            t += step;
        }
        gain.linearRampToValueAtTime(0, t + noteDecay);
        this.uiStart[i] = now;
        this.uiFree[i] = t + noteDecay;
        return i;
    }

    /**
     * Арпеджио на тональном голосе эффектов: ноты из ARP, но с фильтром и
     * панорамой — для событий, привязанных к машине на трассе.
     */
    _fxArp(type, count, step, peak, noteDecay, cut, pan) {
        const now = this.ctx.currentTime;
        const i = this._allocTone(now);
        const osc = this.toneOsc[i];
        const gain = this.toneGain[i].gain;
        const filter = this.toneFilter[i];
        osc.type = type;
        osc.frequency.cancelScheduledValues(now);
        filter.type = 'lowpass';
        filter.frequency.cancelScheduledValues(now);
        filter.frequency.setValueAtTime(cut, now);
        this.tonePan[i].pan.setTargetAtTime(pan, now, 0.003);

        cancelHold(gain, now);
        gain.linearRampToValueAtTime(0, now + FADE);
        let t = now + FADE;
        for (let k = 0; k < count; k++) {
            osc.frequency.setValueAtTime(ARP[k], t);
            gain.linearRampToValueAtTime(peak, t + 0.008);
            gain.setTargetAtTime(0, t + 0.008, noteDecay * 0.3);
            if (k < count - 1) gain.linearRampToValueAtTime(peak * 0.06, t + step);
            t += step;
        }
        gain.linearRampToValueAtTime(0, t + noteDecay);
        this.toneStart[i] = now;
        this.toneFree[i] = t + noteDecay;
        return i;
    }

    /** Одиночный сигнал интерфейса. */
    _uiTone(type, f0, f1, peak, attack, decay) {
        const now = this.ctx.currentTime;
        const i = this._allocUi(now);
        const osc = this.uiOsc[i];
        osc.type = type;
        osc.frequency.cancelScheduledValues(now);
        osc.frequency.setValueAtTime(f0, now);
        if (f1 !== f0) osc.frequency.exponentialRampToValueAtTime(f1 > 1 ? f1 : 1, now + attack + decay);
        this.uiStart[i] = now;
        this.uiFree[i] = this._envelope(this.uiGain[i].gain, now, peak, attack, decay);
        return i;
    }

    /**
     * Панорама и громкость события по слоту: своё — по центру и в полную
     * громкость, чужое — по последней известной позиции машины.
     * Результат кладётся в поля evPan/evGain, чтобы не аллоцировать объект.
     */
    _locate(slot) {
        this.f[F_EV_PAN] = 0;
        this.f[F_EV_GAIN] = 1;
        if (slot === undefined || slot === null) return true;
        const s = slot | 0;
        if (s === this.localSlot) return true;
        if (s < 0 || s >= MAX_SLOTS || !this.slotKnown[s]) {
            // Позиция неизвестна (машина вне снапшота) — играем тихо по центру.
            this.f[F_EV_GAIN] = 0.35;
            return true;
        }
        const dx = this.slotX[s] - this.f[F_LX];
        const dz = this.slotZ[s] - this.f[F_LZ];
        const dist = Math.sqrt(dx * dx + dz * dz);
        const k = dist / EVENT_REF_DIST;
        const gain = 1 / (1 + k * k);
        this.f[F_EV_GAIN] = gain;
        if (gain < EVENT_MIN_GAIN) return false;
        this.f[F_EV_PAN] = dist > 0.001
            ? clamp((dx * this.f[F_LFZ] - dz * this.f[F_LFX]) / dist, -1, 1) * clamp(dist / 4, 0, 1)
            : 0;
        return true;
    }

    // =======================================================================
    // Событийные звуки
    // =======================================================================

    /**
     * Событие гонки по схеме раздела 9. Принимает объект как есть:
     * {kind, slot, item, by, blocked, level, place, lap}.
     */
    raceEvent(msg) {
        if (!this.active || !msg) return;
        const kind = msg.kind;
        if (kind === 'pickup') {
            if (!this._locate(msg.slot)) return;
            this._pickup(this.f[F_EV_GAIN], this.f[F_EV_PAN]);
        } else if (kind === 'use') {
            if (!this._locate(msg.slot)) return;
            this._useItem(msg.item, this.f[F_EV_GAIN], this.f[F_EV_PAN]);
        } else if (kind === 'hit') {
            if (!this._locate(msg.slot)) return;
            if (msg.blocked) this._shieldBlock(this.f[F_EV_GAIN], this.f[F_EV_PAN]);
            else this._hit(this.f[F_EV_GAIN], this.f[F_EV_PAN]);
        } else if (kind === 'drift_boost') {
            if (!this._locate(msg.slot)) return;
            this._driftBoost(msg.level | 0, this.f[F_EV_GAIN], this.f[F_EV_PAN]);
        } else if (kind === 'lap') {
            if ((msg.slot | 0) !== this.localSlot) return;   // чужие круги молчат
            this._lap();
        } else if (kind === 'finish') {
            this._finish((msg.slot | 0) === this.localSlot, msg.place | 0);
        }
    }

    /** Обратный отсчёт: 3, 2, 1 и 0 — «поехали». */
    countdown(value) {
        if (!this.active) return;
        const v = value | 0;
        if (v > 0) {
            // 3 — 560 Гц, 2 — 620 Гц, 1 — 680 Гц: отсчёт слышно без экрана.
            const f = 560 + (3 - clamp(v, 1, 3)) * 60;
            this._uiTone('square', f, f, 0.20, 0.008, 0.20);
        } else {
            this._uiTone('square', 1180, 1180, 0.26, 0.008, 0.55);
            this._tone('sawtooth', 180, 620, 0.35, 0.16, 0.02, 0.45, 700, 3200, 0);
            this._noise(false, 'bandpass', 600, 2600, 0.7, 0.10, 0.01, 0.4, 0);
        }
    }

    /** Явный «старт»: то же, что countdown(0). */
    start() { this.countdown(0); }

    /**
     * Столкновение. В схеме race_event столкновений нет (их считает сервер
     * в resolve_collisions и наружу не отдаёт), поэтому зовётся напрямую:
     * force — сила удара 0..1, slot — чья машина (для панорамы).
     */
    collision(force, slot) {
        if (!this.active) return;
        if (!this._locate(slot)) return;
        const f = clamp(force === undefined ? 0.6 : force, 0.05, 1);
        const g = this.f[F_EV_GAIN];
        const pan = this.f[F_EV_PAN];
        // Глухой удар: коричневый шум через нижние частоты плюс просадка тона.
        this._noise(true, 'lowpass', 900 + 700 * f, 180, 1.2, 0.42 * f * g, 0.004, 0.22 + 0.1 * f, pan);
        this._tone('square', 150 + 90 * f, 60, 0.12, 0.20 * f * g, 0.004, 0.18, 900, 300, pan);
    }

    /** Вылет на траву отдельным сигналом (обычно хватает слоя из update). */
    offroad(force, slot) {
        if (!this.active) return;
        if (!this._locate(slot)) return;
        const f = clamp(force === undefined ? 0.7 : force, 0.05, 1);
        this._noise(false, 'bandpass', 700, 2200, 0.6, 0.50 * f * this.f[F_EV_GAIN], 0.03, 0.35, this.f[F_EV_PAN]);
    }

    // --- конкретные тембры -------------------------------------------------

    /** Подбор бонуса: короткое восходящее арпеджио из трёх нот. */
    _pickup(gain, pan) {
        ARP[0] = N_A5;
        ARP[1] = N_D6;
        ARP[2] = N_G6;
        this._fxArp('triangle', 3, 0.055, 0.17 * gain, 0.13, 6500, pan);
    }

    /**
     * Применение бонуса. У каждого из пяти свой тембр — раздел 8:
     * турбо, ракета, мина, щит, гроза.
     */
    _useItem(item, gain, pan) {
        if (item === 'boost') {
            // Турбо: восходящий свист плюс воздух.
            this._tone('sawtooth', 200, 900, 0.30, 0.20 * gain, 0.015, 0.40, 600, 4000, pan);
            this._noise(false, 'bandpass', 500, 3400, 0.8, 0.16 * gain, 0.02, 0.42, pan);
        } else if (item === 'rocket') {
            // Ракета: резкий нисходящий «пиу» и шипящий след.
            this._tone('square', 1500, 320, 0.22, 0.19 * gain, 0.004, 0.30, 5000, 1200, pan);
            this._noise(false, 'bandpass', 3200, 700, 1.4, 0.14 * gain, 0.01, 0.45, pan);
        } else if (item === 'mine') {
            // Мина: короткий глухой «клац» с металлическим щелчком.
            this._tone('square', 150, 95, 0.10, 0.22 * gain, 0.003, 0.16, 700, 260, pan);
            this._noise(false, 'bandpass', 2600, 1400, 3.0, 0.10 * gain, 0.002, 0.07, pan);
        } else if (item === 'shield') {
            // Щит: мягкий восходящий аккорд, две расстроенные волны.
            this._tone('triangle', 520, 700, 0.30, 0.15 * gain, 0.05, 0.55, 3000, 5200, pan);
            this._tone('sine', 784, 1046, 0.30, 0.12 * gain, 0.06, 0.60, 4000, 6000, pan);
        } else if (item === 'storm') {
            // Гроза: раскат — шум с уходящим вниз срезом и низкий гул.
            this._noise(true, 'lowpass', 2200, 180, 0.9, 0.34 * gain, 0.02, 0.85, pan);
            this._tone('sine', 78, 44, 0.7, 0.22 * gain, 0.03, 0.80, 400, 160, pan);
        } else {
            // Неизвестный бонус: нейтральный сигнал, молчать нельзя.
            this._tone('triangle', 660, 880, 0.12, 0.14 * gain, 0.01, 0.20, 4000, 5000, pan);
        }
    }

    /** Попадание: жёсткий удар с нисходящим тоном. */
    _hit(gain, pan) {
        this._noise(false, 'lowpass', 2600, 320, 1.0, 0.40 * gain, 0.003, 0.34, pan);
        this._tone('sawtooth', 340, 80, 0.25, 0.24 * gain, 0.004, 0.30, 1800, 400, pan);
    }

    /** Щит погасил попадание: звонкий металлический отклик. */
    _shieldBlock(gain, pan) {
        this._tone('sine', 1320, 1320, 0, 0.20 * gain, 0.003, 0.35, 7000, 7000, pan);
        this._tone('triangle', 1980, 1975, 0.3, 0.11 * gain, 0.004, 0.28, 8000, 6000, pan);
        this._noise(false, 'bandpass', 4200, 2600, 4.0, 0.09 * gain, 0.002, 0.10, pan);
    }

    /** Ускорение за занос: чем выше уровень, тем ярче и длиннее. */
    _driftBoost(level, gain, pan) {
        const l = clamp(level, 1, 3);
        const dur = 0.22 + 0.10 * l;
        const top = 900 + 700 * l;
        this._noise(false, 'bandpass', 320, top, 0.9, (0.14 + 0.04 * l) * gain, 0.02, dur, pan);
        this._tone('sawtooth', 190 + 40 * l, 420 + 190 * l, dur * 0.8,
            (0.12 + 0.035 * l) * gain, 0.015, dur, 800, 2600 + 700 * l, pan);
    }

    /** Пересечение линии круга: двойной сигнал. */
    _lap() {
        ARP[0] = N_C6;
        ARP[1] = N_E6;
        this._uiArp('triangle', 2, 0.085, 0.18, 0.10);
    }

    /** Финиш: своя машина получает фанфару, чужая — короткую отметку. */
    _finish(isLocal, place) {
        if (isLocal) {
            ARP[0] = N_C5;
            ARP[1] = N_E5;
            ARP[2] = N_G5;
            ARP[3] = place === 1 ? N_C6 : N_G5;
            this._uiArp('triangle', 4, 0.13, 0.22, 0.30);
            this._tone('sine', 261.63, 261.63, 0, 0.10, 0.05, 0.9, 2000, 2000, 0);
        } else {
            ARP[0] = N_G5;
            ARP[1] = N_C6;
            this._uiArp('sine', 2, 0.09, 0.09, 0.14);
        }
    }

    // =======================================================================
    // Диагностика (оверлей F3)
    // =======================================================================

    /** Счётчики для перф-оверлея. Объект переиспользуется. */
    stats() {
        const out = this.statsOut;
        out.nodeCount = this.nodeCount;
        out.state = this.ctx ? this.ctx.state : (this.failed ? 'failed' : 'none');
        out.rpm = this.f[F_RPM];
        out.gear = this.gear;
        out.volume = this.volume;
        out.muted = this.muted;
        let busy = 0;
        if (this.ctx) {
            const now = this.ctx.currentTime;
            for (let i = 0; i < TONE_VOICES; i++) if (this.toneFree[i] > now) busy++;
            for (let i = 0; i < NOISE_VOICES; i++) if (this.noiseFree[i] > now) busy++;
            for (let i = 0; i < UI_VOICES; i++) if (this.uiFree[i] > now) busy++;
        }
        out.voicesBusy = busy;
        let remote = 0;
        for (let i = 0; i < REMOTE_ENGINES; i++) if (this.remoteSlot[i] >= 0) remote++;
        out.remoteVoices = remote;
        return out;
    }
}

function noop() { /* отклонённый промис контекста нас не интересует */ }

/** Общий экземпляр: клиенту нужен ровно один звуковой контекст. */
export const audio = new RaceAudio();
