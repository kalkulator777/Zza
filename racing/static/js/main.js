// Точка входа клиента: склейка всех модулей и главный цикл (раздел 10.1).
//
// Владелец модуля: [client].
//
// ЧТО ЗДЕСЬ ПРОИСХОДИТ
//
//   1. Собираются экраны интерфейса, рендер, звук, ввод и сеть, и связываются
//      колбэками. Экраны переключаются по состоянию комнаты (раздел 9):
//      меню -> лобби -> гонка -> итоги -> лобби.
//   2. Крутится кадровый цикл раздела 10.1: накопитель времени, фиксированный
//      шаг 1/60, предсказание своей машины, отправка ввода, отрисовка с
//      интерполяцией по остатку накопителя.
//   3. Заполняется кадровое состояние HUD (ui/hud.js) и звука (12.9) —
//      оба принимают один заранее выделенный объект и читают из него поля.
//   4. Считается то, что 12.6 закрепило за main.js: признак «едет не туда»
//      проекцией скорости на касательную трассы.
//   5. Оверлей F3 кормится статистикой рендера и сети.
//
// АЛЛОКАЦИИ. В кадровом цикле — ноль. Все объекты состояния (hudState,
// audioState, perfStats) созданы один раз при старте, дальше только
// переписываются по полям. Ни литералов, ни замыканий, ни .map внутри кадра.
//
// ЧУЖИЕ КЛАВИШИ. Tab, F3 и P слушают ui/hud.js и ui/perf.js, C и Shift —
// render/renderer.js (12.6, 12.11). main.js их не трогает и только читает
// rr.lookBack, чтобы положить бит «взгляд назад» в пакет ввода.
//
// ПАУЗА. Бита в маске кнопок под неё нет и не нужно (событие редкое): P и
// кнопка в HUD шлют JSON-событие set_pause, решение принимает сервер, а
// обратно приезжает событие pause. Пока оно не сказало `running`, кадровый
// цикл не делает ни одного фиксированного шага.

import { MenuScreen, showToast, loadUiSettings, saveUiSettings } from './ui/menu.js';
import { LobbyScreen } from './ui/lobby.js';
import { Hud, createHudState } from './ui/hud.js';
import { ResultsScreen } from './ui/results.js';
import { PerfOverlay } from './ui/perf.js';
import { RaceRenderer } from './render/renderer.js';
import { NetClient } from './net.js';
import { createInput } from './input.js';
import { setCatalog, getCatalog } from './cars.js';
import { audio, createAudioState } from './audio.js';
import {
    DT,
    CAR_CONSTANTS,
    BTN_THROTTLE,
    BTN_BRAKE,
    WEATHER_GRIP,
} from './physics.js';
import { MAX_CARS } from './protocol.js';

// ---------------------------------------------------------------------------
// Константы кадрового цикла
// ---------------------------------------------------------------------------

const MAX_STEPS_PER_FRAME = 10;     // больше — пропускаем отставание (12.4)
const MAX_FRAME_DT = 0.25;          // с, потолок реального шага кадра

const SCREEN_MENU = 0;
const SCREEN_LOBBY = 1;
const SCREEN_RACE = 2;
const SCREEN_RESULTS = 3;

// Порог «едет не туда»: проекция скорости на касательную трассы.
const WRONG_WAY_SPEED = -2.5;       // м/с вдоль касательной
const WRONG_WAY_MIN = 3.0;          // м/с общей скорости, ниже не предупреждаем

// Столкновения: в схеме race_event их нет (12.9), считаем сами по провалу
// скорости. Тормоз даёт 26 м/с², то есть 0,43 м/с за шаг и 1,3 м/с за
// снапшот — пороги ниже заведомо выше любого штатного замедления.
const HIT_STEP_DROP = 2.2;          // м/с за один шаг 1/60 (стена, предсказание)
const HIT_SNAP_DROP = 4.0;          // м/с за снапшот 50 мс (чужие машины)
const HIT_FORCE_REF = 14.0;         // м/с, при которой сила удара равна единице
const HIT_COOLDOWN = 0.28;          // с, чтобы один удар не звучал дважды

// --- повтор финиша ---------------------------------------------------------
//
// Последние секунды гонки проигрываются на экране итогов. Пишется КАДРОВОЕ
// состояние машин, уже интерполированное: снапшоты идут 20 Гц, и повтор,
// собранный из них, выглядел бы ступеньками — ровно тем, что 12.12 из игры
// выгоняло. Буфер кольцевой и выделен один раз: в кадровом цикле не
// появляется ни одного объекта (условие приёмки раздела 1).
//
// Цена памяти: 240 кадров * 8 машин * (7 float32 + 2 uint8) плюс метки
// времени кадров — около 58 КБ на всю страницу, один раз.

const REPLAY_SECONDS = 3.0;
const REPLAY_FRAMES = 240;          // 4 с при 60 fps: запас на просадки кадра
const REPLAY_MIN_FRAMES = 20;       // короче — показывать нечего

/** performance.now(), если он есть. */
const nowMs = (typeof performance !== 'undefined' && performance.now)
    ? function () { return performance.now(); }
    : function () { return Date.now(); };

// ---------------------------------------------------------------------------
// Корни разметки (index.html)
// ---------------------------------------------------------------------------

const canvas = document.getElementById('gl');
const rootMenu = document.getElementById('screen-menu');
const rootLobby = document.getElementById('screen-lobby');
const rootHud = document.getElementById('screen-hud');
const rootResults = document.getElementById('screen-results');
const rootCountdown = document.getElementById('overlay-countdown');
const rootPerf = document.getElementById('overlay-perf');

// ---------------------------------------------------------------------------
// Состояние приложения. Один объект на всё время жизни страницы.
// ---------------------------------------------------------------------------

const app = {
    // -1 — «экран ещё не выбран»: первый же setScreen обязан отработать.
    screen: -1,
    localSlot: -1,
    inRoom: false,
    everConnected: false,
    quietReopen: false,
    roomState: '',
    raceBuilt: false,
    racing: false,
    paused: false,          // глобальная пауза гонки (§9, событие pause)
    startSoundDone: false,
    raceCarCount: 0,
    lapsTotal: 3,

    // кадровый цикл
    lastTime: 0,
    accumulator: 0,
    frameDt: 0,

    // измерения
    cpuMs: 0,               // время процессорной части кадра (без ожидания GPU)
    cpuAvg: 0,
    steps: 0,
    dropped: 0,

    // столкновения
    prevLocalSpeed: 0,
    hitCooldown: new Float32Array(MAX_CARS),
    prevCarSpeed: new Float32Array(MAX_CARS),
    prevCarSeen: new Uint8Array(MAX_CARS),

    // повтор финиша
    replayWanted: false,    // галочка комнаты settings.replay
    handicap: 0,            // множитель характеристик своей машины, 0 — нет
    gripMul: 1,             // множитель сцепления по погоде комнаты (1 — сухо)
};

// ---------------------------------------------------------------------------
// Погода: множитель сцепления
// ---------------------------------------------------------------------------
//
// Своей таблицы здесь НЕТ и быть не должно. Числа живут в physics.js рядом
// с самим крюком (и построчно повторены в game/physics.py и server/config.py);
// четвёртая копия — это четвёртое место, где они могут разойтись. main.js
// только решает, КОГДА применить множитель, и берёт значение оттуда.

function weatherGrip(weather) {
    const k = WEATHER_GRIP[weather];
    return k === undefined ? 1 : k;
}

// Кольцевой буфер повтора. Всё выделено один раз на страницу.
const replay = {
    head: 0,                // куда пишется следующий кадр
    count: 0,               // сколько кадров в буфере (<= REPLAY_FRAMES)
    time: new Float64Array(REPLAY_FRAMES),
    present: new Uint8Array(REPLAY_FRAMES * MAX_CARS),
    flags: new Uint8Array(REPLAY_FRAMES * MAX_CARS),
    x: new Float32Array(REPLAY_FRAMES * MAX_CARS),
    z: new Float32Array(REPLAY_FRAMES * MAX_CARS),
    yaw: new Float32Array(REPLAY_FRAMES * MAX_CARS),
    vx: new Float32Array(REPLAY_FRAMES * MAX_CARS),
    vz: new Float32Array(REPLAY_FRAMES * MAX_CARS),
    steer: new Float32Array(REPLAY_FRAMES * MAX_CARS),
    drift: new Float32Array(REPLAY_FRAMES * MAX_CARS),

    playing: false,
    first: 0,               // индекс первого кадра повтора в кольце
    steps: 0,               // сколько кадров от first надо проиграть
    cursor: 0,              // сколько уже проиграно
    startMs: 0,             // момент начала проигрывания (часы страницы)
    baseMs: 0,              // время первого кадра повтора
    camSlot: -1,            // за чьей машиной смотрит камера повтора
};

const settings = loadUiSettings();

// ---------------------------------------------------------------------------
// Модули
// ---------------------------------------------------------------------------

const renderer = new RaceRenderer(canvas, { quality: settings.quality });
renderer.resize(window.innerWidth, window.innerHeight);

const hud = new Hud(rootHud, { onTogglePause: onTogglePause });
const hudState = createHudState();
const audioState = createAudioState();
const perf = new PerfOverlay(rootPerf);

// Статистика для оверлея F3: объект переиспользуется, в кадре не создаётся.
const perfStats = {
    fps: 0,
    frameMs: 0,
    cpuMs: 0,
    drawCalls: 0,
    triangles: 0,
    particles: 0,
    ping: 0,
    snapshotMs: 0,
    quality: settings.quality,
    renderScale: 1,
};

const input = createInput({ onEscape: onEscape });

const net = new NetClient({
    onOpen: onNetOpen,
    onClose: onNetClose,
    onWelcome: onWelcome,
    onRooms: onRooms,
    onRoom: onRoom,
    onRaceInit: onRaceInit,
    onCountdown: onCountdown,
    onRaceEvent: onRaceEvent,
    onResults: onResults,
    onPause: onPause,
    onError: onServerError,
    onSnapshot: onSnapshot,
});

const menu = new MenuScreen(rootMenu, {
    onNameChange: onNameChange,
    onCreateRoom: onCreateRoom,
    onJoinRoom: onJoinRoom,
    onRefreshRooms: onRefreshRooms,
    onSettingsChange: onSettingsChange,
});

const lobby = new LobbyScreen(rootLobby, rootCountdown, {
    onSetCar: onSetCar,
    onSetReady: onSetReady,
    onUpdateSettings: onUpdateSettings,
    onStartRace: onStartRace,
    onChat: onChat,
    onLeave: onLeaveRoom,
});

const results = new ResultsScreen(rootResults, {
    onReturn: onResultsReturn,
    onSkipReplay: stopReplay,
});

// ---------------------------------------------------------------------------
// Экраны
// ---------------------------------------------------------------------------

function setScreen(screen) {
    if (app.screen === screen) return;
    app.screen = screen;

    menu.hide();
    lobby.hide();
    if (screen !== SCREEN_RESULTS) results.hide();
    if (screen !== SCREEN_RACE && screen !== SCREEN_RESULTS) {
        hud.hide();
        lobby.hideCountdown();
    }

    if (screen === SCREEN_MENU) {
        menu.show();
    } else if (screen === SCREEN_LOBBY) {
        lobby.show();
    } else if (screen === SCREEN_RACE) {
        hud.show();
    } else if (screen === SCREEN_RESULTS) {
        hud.hide();
        lobby.hideCountdown();
        results.show();
    }

    // Ввод имеет смысл только в гонке: в лобби те же клавиши — это чат.
    input.setEnabled(screen === SCREEN_RACE);
    if (screen !== SCREEN_RACE) input.releaseAll();
}

/** Разобрать сцену гонки и вернуть рендер в холостой режим. */
function teardownRace() {
    if (!app.raceBuilt) return;
    if (replay.playing) renderer.setLocalSlot(app.localSlot);
    replay.playing = false;
    replay.count = 0;
    replay.head = 0;
    app.raceBuilt = false;
    app.racing = false;
    app.paused = false;
    app.startSoundDone = false;
    renderer.dispose();
    audio.stopRace();
    audioState.engineOn = false;
    audioState.carCount = 0;
    for (let i = 0; i < MAX_CARS; i++) {
        app.hitCooldown[i] = 0;
        app.prevCarSeen[i] = 0;
    }
    app.prevLocalSpeed = 0;
}

// ---------------------------------------------------------------------------
// События сети
// ---------------------------------------------------------------------------

function onNetOpen() {
    // Первое подключение молчит: тост — это новость, а не приветствие.
    // Переподключение по смене имени — тоже наша затея, о ней не сообщаем.
    // ui/menu.js знает две окраски: 'info' (синяя) и всё прочее (красная).
    if (app.everConnected && !app.quietReopen) showToast('Связь восстановлена', 'info');
    app.quietReopen = false;
    app.everConnected = true;
}

function onNetClose(ev, byUs) {
    if (byUs) return;
    app.inRoom = false;
    app.localSlot = -1;
    setLocalSlot(-1);
    teardownRace();
    setScreen(SCREEN_MENU);
    showToast('Связь с сервером потеряна, переподключаюсь…', 'error');
}

function onWelcome(msg) {
    // 12.11: каталог машин должен быть готов до createRace — в race_init
    // есть только идентификатор машины, а рендеру нужна форма.
    setCatalog(msg);
    menu.applyWelcome(msg);
    lobby.applyWelcome(msg);
    results.applyWelcome(msg);
    // 12.8: в welcome слот почти всегда null, боевое значение придёт в room.you.
    setLocalSlot(msg.slot === undefined || msg.slot === null ? -1 : msg.slot | 0);
    setScreen(SCREEN_MENU);
}

function onRooms(msg) {
    menu.applyRooms(msg);
    if (!app.inRoom) setScreen(SCREEN_MENU);
}

function onRoom(msg) {
    app.inRoom = true;
    // 12.8: you приходит в КАЖДОМ событии room, особенного «первого» нет.
    setLocalSlot(msg.you === undefined || msg.you === null ? -1 : msg.you | 0);
    lobby.applyRoom(msg);

    const state = msg.state || 'LOBBY';
    const was = app.roomState;
    app.roomState = state;
    if (state !== 'RACING' && app.paused) {
        // Гонка кончилась прямо на паузе — плашка не должна пережить её.
        app.paused = false;
        hud.setPause('running', '');
    }
    if (msg.settings && msg.settings.laps) app.lapsTotal = msg.settings.laps | 0;
    // Окружение гонки (время суток и погода) живёт в настройках комнаты.
    // Рендер принимает их ОДНИМ плоским объектом и молча пропускает всё
    // лишнее (§12.16), поэтому отдаём настройки целиком — отдельной проводки
    // под погоду не понадобилось. Сообщаем об изменении сразу в лобби —
    // чтобы выбранное было видно, не дожидаясь старта, и чтобы гонка
    // собралась с ним с первого кадра.
    if (msg.settings) {
        renderer.setEnvironment(msg.settings);
        applyWeatherGrip(msg.settings);
    }

    if (state === 'LOBBY') {
        teardownRace();
        setScreen(SCREEN_LOBBY);
    } else if (state === 'COUNTDOWN') {
        app.racing = false;
        audioState.engineOn = true;      // 12.9: в отсчёте двигатель на холостых
        // Вошедшему по ходу гонки race_init не приходит: он ждёт следующего
        // заезда в наблюдателях (раздел 9) и сидит на экране лобби.
        setScreen(app.raceBuilt ? SCREEN_RACE : SCREEN_LOBBY);
    } else if (state === 'RACING') {
        app.racing = true;
        audioState.engineOn = true;
        setScreen(app.raceBuilt ? SCREEN_RACE : SCREEN_LOBBY);
        if (!app.startSoundDone) {
            app.startSoundDone = true;
            // 12.9: отдельного события «старт» в протоколе нет.
            audio.countdown(0);
        }
    } else if (state === 'RESULTS') {
        app.racing = false;
        audioState.engineOn = false;     // 12.9: на экране итогов тишина
        if (was !== 'RESULTS') setScreen(SCREEN_RESULTS);
    }
}

function onRaceInit(msg) {
    const players = msg.players || [];
    app.raceCarCount = players.length;
    app.lapsTotal = msg.laps | 0;
    app.startSoundDone = false;
    app.prevLocalSpeed = 0;
    app.replayWanted = !!(msg.settings && msg.settings.replay);
    replay.playing = false;
    replay.count = 0;
    replay.head = 0;
    for (let i = 0; i < MAX_CARS; i++) {
        app.hitCooldown[i] = 0;
        app.prevCarSeen[i] = 0;
    }

    // net.js уже построил Track из этого же сообщения — отдаём рендеру его же
    // экземпляр, чтобы геометрия не считалась дважды.
    // env — настройки комнаты целиком (см. onRoom): рендер возьмёт из них
    // время суток и погоду, а личные галочки `racing.gfx.timeofday`
    // и `racing.gfx.weather` (кроме `auto`) перебьют комнату уже внутри
    // рендера.
    renderer.createRace(net.track || msg.track, players,
        { localSlot: app.localSlot, env: msg.settings });
    app.raceBuilt = true;

    hud.setupRace(msg, app.localSlot);
    const spec = getCatalog().resolve(localCarId(players));
    // Порядок важен: net.js собрал localStats в _onRaceInit и позвал нас уже
    // после этого, поэтому и гандикап, и погода ложатся на готовый объект.
    applyWeatherGrip(msg.settings);
    const handicap = applyHandicap(players);
    if (spec && spec.stats) {
        hud.setSpeedScale(spec.stats.boost_speed * (handicap || 1));
    }
    hud.setHandicap(handicap);

    audio.setLocalSlot(app.localSlot);
    audio.startRace();
    audioState.engineOn = true;

    setScreen(SCREEN_RACE);
}

/** Идентификатор своей машины из списка race_init.players. */
function localCarId(players) {
    for (let i = 0; i < players.length; i++) {
        if (players[i].slot === app.localSlot) return players[i].car;
    }
    return null;
}

/**
 * Гандикап победителя прошлой гонки: race_init.players[].handicap.
 *
 * Сервер уже замедлил машину у себя, наложив множитель на характеристики.
 * Предсказание обязано считать ТЕМИ ЖЕ числами, иначе своя машина каждый
 * снапшот будет оттягиваться назад реконсиляцией — ровно тот эффект резины,
 * ради отсутствия которого предсказание и существует. net.js собирает
 * localStats из каталога машин в _onRaceInit и зовёт нас уже после этого,
 * поэтому множитель накладывается прямо на готовый объект.
 *
 * Возвращает множитель (0 — гандикапа нет).
 */
function applyHandicap(players) {
    app.handicap = 0;
    let factor = 0;
    for (let i = 0; i < players.length; i++) {
        if (players[i].slot !== app.localSlot) continue;
        const value = +players[i].handicap;
        if (value > 0.5 && value < 1) factor = value;
        break;
    }
    const stats = net.localStats;
    if (!factor || !stats) return 0;
    // Замедляются только скоростные характеристики — тот же список, что
    // в server/config.HANDICAP_STATS.
    stats.engineForce *= factor;
    stats.maxSpeed *= factor;
    stats.boostSpeed *= factor;
    app.handicap = factor;
    return factor;
}

/**
 * Погода комнаты -> множитель сцепления для физики.
 *
 * Крюк в physics.js — `state.gripMul`, по умолчанию 1,0: он домножает
 * и предел поперечного ускорения (шаг 9), и гашение боковой скорости
 * (шаг 10). Пишет его тот, кто включает погоду, — то есть мы.
 *
 * Поле живёт в СОСТОЯНИИ, а не в характеристиках (так решил владелец
 * физики: у CarSpec набор полей фиксирован). Реконсиляция это переживает:
 * кольцо предсказания копирует gripMul как есть, а погода за полсотни
 * шагов отката не меняется. resetCarState поле тоже не трогает — покрытие
 * не относится к движению машины.
 *
 * Присваивание, а не умножение: событие room приходит и по ходу гонки,
 * и повторное применение не должно копить множитель.
 *
 * ВТОРАЯ ПОЛОВИНА — НА СЕРВЕРЕ, в game/sim.py: там то же число обязано
 * лечь в car.state.grip_mul каждой машины. Если стороны разойдутся, своя
 * машина будет оттягиваться реконсиляцией каждый снапшот. game/sim.py
 * в файлы этой задачи не входит — нужная правка описана в отчёте.
 */
function applyWeatherGrip(settings) {
    const k = weatherGrip(settings && settings.weather);
    app.gripMul = k;
    if (net.state) net.state.gripMul = k;
    return k;
}

function onCountdown(msg) {
    const value = msg.value | 0;
    lobby.showCountdown(value);
    if (value > 0) {
        audio.countdown(value);
    } else if (!app.startSoundDone) {
        app.startSoundDone = true;
        audio.countdown(0);
    }
}

function onRaceEvent(msg) {
    hud.pushRaceEvent(msg);
    audio.raceEvent(msg);

    // Рекорд круга: новость для всей комнаты, а не только для ленты HUD.
    if (msg.kind === 'record') {
        showToast(recordText(msg), msg.slot === app.localSlot ? 'ok' : 'info');
    }

    // 12.11: событие hit не несёт координат, взрыв рисуется по позиции
    // пострадавшей машины — её знает рендер.
    if (msg.kind === 'hit' && app.raceBuilt) {
        const slot = msg.slot | 0;
        if (msg.blocked) {
            renderer.shieldBlock(slot);
        } else {
            renderer.hitCar(slot);
        }
    }
}

/**
 * «Вася побил рекорд трассы «Серпантин»: 39.58» (race_event kind=record).
 *
 * Название трассы берётся в кавычки именительным падежом, а не склоняется:
 * «Серпантина» получилось бы, а «Офисный круга» — нет, и склонять русские
 * названия из content/tracks ради одной строки не стоит.
 */
function recordText(msg) {
    const name = msg.name || 'Гонщик';
    const track = msg.track_name || msg.track || '';
    const where = track ? ' трассы «' + track + '»' : ' трассы';
    const time = (+msg.time).toFixed(2);
    if (msg.scope === 'track') {
        return name + (msg.first ? ' открыл рекорд' : ' побил рекорд')
            + where + ': ' + time;
    }
    return name + ' — рекорд на этой машине: ' + time;
}

function onResults(msg) {
    results.applyResults(msg, app.localSlot);
    app.racing = false;
    audioState.engineOn = false;
    setScreen(SCREEN_RESULTS);
    startReplay();
}

// ---------------------------------------------------------------------------
// Повтор финиша
// ---------------------------------------------------------------------------

/**
 * Запись одного кадра в кольцевой буфер. Зовётся из кадрового цикла, поэтому
 * не создаёт ничего: только записи в заранее выделенные типизированные
 * массивы.
 */
function recordReplayFrame(t, localX, localZ, localYaw) {
    const head = replay.head;
    const base = head * MAX_CARS;
    replay.time[head] = t;
    const local = app.localSlot;
    const state = net.state;
    for (let s = 0; s < MAX_CARS; s++) {
        const i = base + s;
        if (!net.viewPresent[s]) { replay.present[i] = 0; continue; }
        const isLocal = s === local;
        replay.present[i] = 1;
        replay.x[i] = isLocal ? localX : net.viewX[s];
        replay.z[i] = isLocal ? localZ : net.viewZ[s];
        replay.yaw[i] = isLocal ? localYaw : net.viewYaw[s];
        replay.vx[i] = isLocal ? state.vx : net.viewVx[s];
        replay.vz[i] = isLocal ? state.vz : net.viewVz[s];
        replay.steer[i] = isLocal ? state.steer : net.viewSteer[s];
        replay.flags[i] = net.viewFlags[s];
        replay.drift[i] = net.viewDrift[s];
    }
    replay.head = head + 1 >= REPLAY_FRAMES ? 0 : head + 1;
    if (replay.count < REPLAY_FRAMES) replay.count++;
}

/** Начать проигрывание последних REPLAY_SECONDS секунд гонки. */
function startReplay() {
    replay.playing = false;
    if (!app.replayWanted || !app.raceBuilt || replay.count < REPLAY_MIN_FRAMES) {
        results.endReplay();
        return;
    }
    const last = (replay.head - 1 + REPLAY_FRAMES) % REPLAY_FRAMES;
    const until = replay.time[last] - REPLAY_SECONDS * 1000;
    // Ищем от свежего к старому: сколько кадров укладывается в окно.
    let steps = 1;
    while (steps < replay.count) {
        const idx = (last - steps + REPLAY_FRAMES) % REPLAY_FRAMES;
        if (replay.time[idx] < until) break;
        steps++;
    }
    if (steps < REPLAY_MIN_FRAMES) {
        results.endReplay();
        return;
    }
    // За кем смотреть. Своя машина — первый выбор, но к концу гонки её
    // в кадре может уже не быть: финишировавший превращается в призрака
    // и через три секунды исчезает. Тогда камера идёт за тем, кто в эти
    // секунды ещё ехал, — обычно за тем, кто и закрывал гонку.
    const base = last * MAX_CARS;
    let camSlot = -1;
    if (app.localSlot >= 0 && replay.present[base + app.localSlot]) {
        camSlot = app.localSlot;
    } else {
        for (let s = 0; s < MAX_CARS; s++) {
            if (replay.present[base + s]) { camSlot = s; break; }
        }
    }
    if (camSlot < 0) {
        results.endReplay();
        return;
    }

    replay.first = (last - steps + 1 + REPLAY_FRAMES) % REPLAY_FRAMES;
    replay.steps = steps;
    replay.cursor = 0;
    replay.baseMs = replay.time[replay.first];
    replay.startMs = nowMs();
    replay.camSlot = camSlot;
    replay.playing = true;
    renderer.setLocalSlot(camSlot);
    results.beginReplay(Math.round((replay.time[last] - replay.baseMs) / 100) / 10);
}

/** Досрочно оборвать повтор (кнопка «Пропустить» или уход с экрана). */
function stopReplay() {
    if (!replay.playing) return;
    replay.playing = false;
    renderer.setLocalSlot(app.localSlot);
    results.endReplay();
}

/** Кадр повтора: машины берутся из кольцевого буфера, а не из сети. */
function drawReplay(dt) {
    const elapsed = nowMs() - replay.startMs;
    let cursor = replay.cursor;
    while (cursor + 1 < replay.steps) {
        const next = (replay.first + cursor + 1) % REPLAY_FRAMES;
        if (replay.time[next] - replay.baseMs > elapsed) break;
        cursor++;
    }
    replay.cursor = cursor;

    const base = ((replay.first + cursor) % REPLAY_FRAMES) * MAX_CARS;
    renderer.beginFrame();
    for (let s = 0; s < MAX_CARS; s++) {
        const i = base + s;
        if (!replay.present[i]) continue;
        renderer.setCarState(s, replay.x[i], replay.z[i], replay.yaw[i],
            replay.vx[i], replay.vz[i], replay.steer[i],
            replay.flags[i], replay.drift[i]);
    }
    renderer.frame(dt);

    if (cursor + 1 >= replay.steps) stopReplay();
}

/**
 * Событие `pause` (§9). Пока фаза не `running`, комната не тикает: шаги
 * предсказания не делаются, ввод никуда не уходит, время гонки и времена
 * кругов стоят (этим занимается net.js своими часами). HUD показывает
 * плашку, обратный отсчёт снятия приезжает обычными событиями countdown.
 */
function onPause(msg) {
    const phase = msg.phase === 'paused' || msg.phase === 'resuming' ? msg.phase : 'running';
    const was = app.paused;
    app.paused = phase !== 'running';
    hud.setPause(phase, msg.name || '');
    if (app.paused) {
        audioState.engineOn = false;
    } else if (was) {
        audioState.engineOn = true;
    }
    // Тост нужен только там, где плашки и отсчёта не хватает: паузу поставил
    // не ты (кто именно — видно и на плашке) или её сняло само время.
    if (phase === 'paused' && msg.by !== app.localSlot) {
        showToast((msg.name || 'Игрок') + ' поставил паузу', 'info');
    } else if (phase === 'running' && was && msg.reason === 'timeout') {
        showToast('Пауза снята: исчерпан предел длительности', 'info');
    }
}

/** Нажали P или кнопку паузы в HUD. Решение принимает сервер. */
function onTogglePause(want) {
    if (!app.racing) return;
    net.send({ t: 'set_pause', paused: !!want });
}

function onServerError(msg) {
    showToast(msg.message || msg.code || 'Ошибка сервера', 'error');
}

/**
 * Снапшот пришёл: авторитетные скорости чужих машин — единственный источник,
 * по которому слышно столкновение (12.9: события для них в протоколе нет).
 */
function onSnapshot(snap) {
    const prevSpeed = app.prevCarSpeed;
    const seen = app.prevCarSeen;
    const cooldown = app.hitCooldown;
    const local = app.localSlot;

    for (let i = 0; i < snap.carCount; i++) {
        const slot = snap.carSlot[i];
        if (slot >= MAX_CARS) continue;
        const vx = snap.carVx[i];
        const vz = snap.carVz[i];
        const speed = Math.sqrt(vx * vx + vz * vz);
        if (seen[slot] && slot !== local && cooldown[slot] <= 0) {
            const drop = prevSpeed[slot] - speed;
            if (drop > HIT_SNAP_DROP) {
                cooldown[slot] = HIT_COOLDOWN;
                let force = drop / HIT_FORCE_REF;
                if (force > 1) force = 1;
                audio.collision(force, slot);
            }
        }
        prevSpeed[slot] = speed;
        seen[slot] = 1;
    }
}

// ---------------------------------------------------------------------------
// Действия интерфейса
// ---------------------------------------------------------------------------

function onNameChange(name) {
    saveUiSettings({ name: name });
    net.setName(name);
    // Имя уезжает на сервер только в hello (раздел 9: события «переименоваться»
    // нет), а набирают его уже после подключения. Пока игрок не в комнате,
    // переподключаемся молча — на локальной сети это мгновенно.
    if (!app.inRoom) {
        app.quietReopen = true;
        net.reconnect();
    }
}

function onCreateRoom(name, roomSettings) {
    net.send({ t: 'create_room', name: name, settings: roomSettings });
}

function onJoinRoom(roomId) {
    net.send({ t: 'join_room', room_id: roomId });
}

function onRefreshRooms() {
    net.send({ t: 'list_rooms' });
}

function onSettingsChange(s) {
    settings.quality = s.quality;
    settings.volume = s.volume;
    settings.muted = s.muted;
    renderer.setQuality(s.quality);
    perfStats.quality = s.quality;
    audio.setVolume(s.volume, s.muted);
}

function onSetCar(carId, color) {
    net.send({ t: 'set_car', car_id: carId, color: color });
}

function onSetReady(ready) {
    net.send({ t: 'set_ready', ready: !!ready });
}

function onUpdateSettings(roomSettings) {
    net.send({ t: 'update_settings', settings: roomSettings });
}

function onStartRace() {
    net.send({ t: 'start_race' });
}

function onChat(text) {
    net.send({ t: 'chat', text: text });
}

function onLeaveRoom() {
    net.send({ t: 'leave_room' });
    app.inRoom = false;
    app.roomState = '';
    setLocalSlot(-1);
    teardownRace();
    setScreen(SCREEN_MENU);
    net.send({ t: 'list_rooms' });
}

/**
 * Кнопка «В лобби» на экране итогов.
 *
 * Комната сама возвращается из RESULTS в LOBBY через пятнадцать секунд
 * (раздел 9), игроки при этом остаются в ней. Кнопка только пропускает
 * таблицу вперёд и НЕ выходит из комнаты: раньше она была повешена на
 * onLeaveRoom, поэтому вместо лобби игрок оказывался в меню, а если он был
 * владельцем комнаты — вместе с ним уезжало и владение.
 */
function onResultsReturn() {
    stopReplay();
    if (!app.inRoom) {
        setScreen(SCREEN_MENU);
        return;
    }
    teardownRace();
    setScreen(SCREEN_LOBBY);
}

/** Esc (раздел 10.4): из гонки и лобби — в меню, из меню — никуда. */
function onEscape() {
    if (app.screen === SCREEN_RACE || app.screen === SCREEN_LOBBY
        || app.screen === SCREEN_RESULTS) {
        onLeaveRoom();
    }
}

/** Разослать слот всем, кому он нужен (12.6, 12.8). */
function setLocalSlot(slot) {
    app.localSlot = slot;
    lobby.setLocalSlot(slot);
    renderer.setLocalSlot(slot);
    audio.setLocalSlot(slot);
    hud.localSlot = slot;
}

// ---------------------------------------------------------------------------
// Главный цикл (раздел 10.1)
// ---------------------------------------------------------------------------

function frame(timestamp) {
    requestAnimationFrame(frame);

    const t0 = nowMs();
    let dt = (timestamp - app.lastTime) * 0.001;
    app.lastTime = timestamp;
    if (!(dt > 0)) dt = 0;
    if (dt > MAX_FRAME_DT) dt = MAX_FRAME_DT;
    app.frameDt = dt;

    // --- фиксированный шаг -------------------------------------------------
    let steps = 0;
    if (app.paused && app.raceBuilt) {
        // Гонка стоит: ни одного шага, ввод никуда не идёт. Накопитель при
        // этом НЕ трогаем — alpha остаётся тем же, и своя машина замирает
        // ровно в той точке, где её застала пауза, без полушага назад.
    } else if (app.racing && app.raceBuilt) {
        // Поправка темпа от net.js: держит число шагов клиента вровень
        // с числом тиков сервера, иначе счётчики расходятся и реконсиляция
        // правит не физику, а разницу в количестве шагов.
        app.accumulator += dt + net.takeClockAdjust();
        if (app.accumulator < 0) app.accumulator = 0;
        while (app.accumulator >= DT && steps < MAX_STEPS_PER_FRAME) {
            input.setLookBack(renderer.lookBack);
            fixedStep(input.buttons);
            app.accumulator -= DT;
            steps++;
        }
        if (app.accumulator >= DT) {
            // Отстали сильно: пропускаем отставание, а не нагоняем (12.4) —
            // лучше рывок, чем спираль смерти.
            app.dropped += Math.floor(app.accumulator / DT);
            app.accumulator = app.accumulator % DT;
        }
    } else {
        app.accumulator = 0;
    }
    app.steps = steps;

    // --- отрисовка ---------------------------------------------------------
    if (replay.playing && app.raceBuilt) {
        drawReplay(dt);
    } else if (app.raceBuilt) {
        const alpha = app.accumulator / DT;
        drawRace(t0, alpha, dt);
    }

    app.cpuMs = nowMs() - t0;
    app.cpuAvg += (app.cpuMs - app.cpuAvg) * 0.05;
    feedPerf();
}

/** Один фиксированный шаг: ввод -> предсказание -> кольцевой буфер -> сеть. */
function fixedStep(buttons) {
    const state = net.state;
    const before = Math.sqrt(state.vx * state.vx + state.vz * state.vz);
    net.stepLocal(buttons);
    const after = Math.sqrt(state.vx * state.vx + state.vz * state.vz);

    // Удар о стену виден сразу в предсказании: провал скорости за один шаг
    // выше любого штатного торможения (12.9 — своего события в протоколе нет).
    const slot = app.localSlot;
    if (slot >= 0 && app.hitCooldown[slot] <= 0) {
        const drop = before - after;
        if (drop > HIT_STEP_DROP) {
            app.hitCooldown[slot] = HIT_COOLDOWN;
            let force = drop / HIT_FORCE_REF;
            if (force > 1) force = 1;
            audio.collision(force, slot);
        }
    }
    app.prevLocalSpeed = after;
}

/** Кадр гонки: интерполяция, машины, HUD, звук, отрисовка. */
function drawRace(t, alpha, dt) {
    net.advanceSmoothing(dt);
    net.interpolate(t);

    const local = app.localSlot;
    const state = net.state;

    renderer.beginFrame();

    // Своя машина — из предсказания с интерполяцией по остатку накопителя
    // (раздел 10.1) и остатком коррекции реконсиляции (10.2).
    let localX = 0, localZ = 0, localYaw = 0;
    if (local >= 0 && net.viewPresent[local]) {
        localX = net.renderX(alpha);
        localZ = net.renderZ(alpha);
        localYaw = net.renderYaw(alpha);
        renderer.setCarState(local, localX, localZ, localYaw,
            state.vx, state.vz, state.steer,
            net.viewFlags[local], net.viewDrift[local]);
    }

    // Чужие — из интерполированного буфера снапшотов.
    for (let s = 0; s < MAX_CARS; s++) {
        if (s === local || !net.viewPresent[s]) continue;
        renderer.setCarState(s, net.viewX[s], net.viewZ[s], net.viewYaw[s],
            net.viewVx[s], net.viewVz[s], net.viewSteer[s],
            net.viewFlags[s], net.viewDrift[s]);
    }

    renderer.applySnapshot(net.snap);
    renderer.frame(dt);

    // Кольцевой буфер повтора финиша пишется только в идущей гонке и только
    // когда галочка комнаты включена: иначе это чистые лишние 48 КБ записи.
    if (app.replayWanted && app.racing && !app.paused) {
        recordReplayFrame(t, localX, localZ, localYaw);
    }


    fillHud(t, localX, localZ);
    fillAudio(local, localX, localZ);
    audio.update(audioState, dt);

    // Остывание счётчиков ударов.
    const cooldown = app.hitCooldown;
    for (let i = 0; i < MAX_CARS; i++) {
        if (cooldown[i] > 0) {
            cooldown[i] -= dt;
            if (cooldown[i] < 0) cooldown[i] = 0;
        }
    }
}

// ---------------------------------------------------------------------------
// Кадровое состояние HUD
// ---------------------------------------------------------------------------

function fillHud(t, localX, localZ) {
    const state = net.state;
    const vx = state.vx;
    const vz = state.vz;
    const speed = Math.sqrt(vx * vx + vz * vz);

    hudState.speed = speed;
    hudState.laps = app.lapsTotal;
    hudState.total = app.raceCarCount;

    let lap = net.lap + 1;
    if (lap < 1) lap = 1;
    if (app.lapsTotal > 0 && lap > app.lapsTotal) lap = app.lapsTotal;
    hudState.lap = lap;
    hudState.place = net.place;

    hudState.lapTime = net.currentLapTime(t);
    hudState.bestLap = net.bestLap;
    hudState.gap = net.gap;

    hudState.item = net.item;
    hudState.itemReady = net.item !== 0;

    const charge = state.driftCharge;
    hudState.driftCharge = charge;
    hudState.driftLevel = charge >= CAR_CONSTANTS.DRIFT_CHARGE_L3 ? 3
        : charge >= CAR_CONSTANTS.DRIFT_CHARGE_L2 ? 2
            : charge >= CAR_CONSTANTS.DRIFT_CHARGE_L1 ? 1 : 0;

    // 12.6: признак «едет не туда» считает main.js проекцией скорости
    // на касательную трассы.
    hudState.wrongWay = isWrongWay(speed, vx, vz);

    // Мини-карта и таблица позиций — из интерполированных значений, чтобы
    // точки не дёргались на 20 Гц.
    const local = app.localSlot;
    let count = 0;
    for (let s = 0; s < MAX_CARS; s++) {
        if (!net.viewPresent[s]) continue;
        hudState.carSlot[count] = s;
        hudState.carX[count] = s === local ? localX : net.viewX[s];
        hudState.carZ[count] = s === local ? localZ : net.viewZ[s];
        hudState.carPlace[count] = net.viewPlace[s];
        hudState.carLap[count] = net.viewLap[s];
        hudState.carFlags[count] = net.viewFlags[s];
        count++;
    }
    hudState.carCount = count;

    hud.update(hudState);
}

/** Проекция скорости на касательную трассы в точке машины (12.6). */
function isWrongWay(speed, vx, vz) {
    const track = net.track;
    if (!track || speed < WRONG_WAY_MIN) return false;
    const i = net.state.sampleIdx;
    if (i < 0 || i >= track.count) return false;
    const along = vx * track.ctx[i] + vz * track.ctz[i];
    return along < WRONG_WAY_SPEED;
}

// ---------------------------------------------------------------------------
// Кадровое состояние звука (12.9)
// ---------------------------------------------------------------------------

function fillAudio(local, localX, localZ) {
    const state = net.state;
    const yaw = state.yaw;
    const fx = Math.sin(yaw);
    const fz = Math.cos(yaw);
    // lateral из раздела 4: (cos yaw, -sin yaw), положительное — левый борт.
    const lx = fz;
    const lz = -fx;

    audioState.forwardSpeed = state.vx * fx + state.vz * fz;
    audioState.lateralSpeed = state.vx * lx + state.vz * lz;

    const buttons = input.buttons;
    audioState.throttle = (buttons & BTN_THROTTLE) !== 0;
    audioState.braking = (buttons & BTN_BRAKE) !== 0;
    audioState.driftActive = state.driftActive;
    audioState.offtrack = state.offtrack;
    audioState.boosting = state.boostTime > 0;
    audioState.spinning = state.spinTime > 0;

    // Слушатель — камера. Направление берём прямо из матрицы: в three.js
    // камера смотрит вдоль своей оси -Z, а физике нужен курс (sin, cos).
    const m = renderer.camera.matrixWorld.elements;
    audioState.camX = m[12];
    audioState.camZ = m[14];
    audioState.camYaw = Math.atan2(-m[8], -m[10]);

    // 12.9: carSpeed в снапшоте нет, его обязан заполнить main.js.
    let count = 0;
    for (let s = 0; s < MAX_CARS; s++) {
        if (!net.viewPresent[s]) continue;
        const isLocal = s === local;
        const vx = isLocal ? state.vx : net.viewVx[s];
        const vz = isLocal ? state.vz : net.viewVz[s];
        audioState.carSlot[count] = s;
        audioState.carX[count] = isLocal ? localX : net.viewX[s];
        audioState.carZ[count] = isLocal ? localZ : net.viewZ[s];
        audioState.carSpeed[count] = Math.sqrt(vx * vx + vz * vz);
        audioState.carFlags[count] = net.viewFlags[s];
        count++;
    }
    audioState.carCount = count;
}

// ---------------------------------------------------------------------------
// Оверлей F3
// ---------------------------------------------------------------------------

function feedPerf() {
    if (!perf.visible) return;
    const rs = renderer.getStats();
    const ns = net.getStats();
    perfStats.fps = rs.fps;
    perfStats.frameMs = rs.frameMs;
    perfStats.cpuMs = app.cpuAvg;
    perfStats.drawCalls = rs.drawCalls;
    perfStats.triangles = rs.triangles;
    perfStats.particles = rs.particles;
    // Сервер меряет RTT сам (ping/pong, §9) и это самый честный замер;
    // свой, по подтверждению ввода, показываем, только пока серверного нет.
    perfStats.ping = ns.serverPing > 0 ? ns.serverPing : ns.ping;
    perfStats.snapshotMs = ns.snapshotMs;
    perfStats.quality = rs.quality;
    perfStats.renderScale = rs.renderScale;
    perf.update(perfStats);
}

// ---------------------------------------------------------------------------
// Запуск
// ---------------------------------------------------------------------------

function wsUrl() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + location.host + '/ws';
}

/** Токен хоста из адресной строки: ?host=<token> (раздел 2). */
function hostToken() {
    const params = new URLSearchParams(location.search);
    return params.get('host') || '';
}

function onResize() {
    renderer.resize(window.innerWidth, window.innerHeight);
}

function boot() {
    window.addEventListener('resize', onResize);
    onResize();

    input.attach();

    // Контекст WebAudio создаётся только по жесту пользователя (раздел 10.5).
    audio.attachUnlockHandlers(window);
    audio.setVolume(settings.volume, settings.muted, false);

    setScreen(SCREEN_MENU);
    net.connect(wsUrl(), menu.getName(), hostToken());

    app.lastTime = nowMs();
    requestAnimationFrame(frame);

    // Отладочный доступ для замеров и автотестов: игра сюда не заглядывает.
    window.__racing = {
        app: app,
        step: fixedStep,
        draw: drawRace,
        net: net,
        input: input,
        renderer: renderer,
        hud: hud,
        hudState: hudState,
        audioState: audioState,
        perfStats: perfStats,
        replay: replay,
        results: results,
        lobby: lobby,
        menu: menu,
    };
}

boot();
