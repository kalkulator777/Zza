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

import {
    loadGfxSettings,
    saveGfxSettings,
    applyGfxPreset,
    GFX_OPTIONS
} from '../render/renderer.js';

const LS_NAME = 'racing.name';
const LS_QUALITY = 'racing.quality';
const LS_VOLUME = 'racing.volume';
const LS_MUTED = 'racing.muted';

export const QUALITY_PRESETS = ['low', 'medium', 'high'];
const QUALITY_LABELS = { low: 'Низкое', medium: 'Среднее', high: 'Высокое' };

// Подписи ЗНАЧЕНИЙ настроек графики, по ключу настройки. Пресет качества
// выше — быстрая заготовка, которая выставляет их скопом; дальше каждую
// настройку можно крутить руками.
//
// Сам список настроек — какие есть, какого они вида и как называются —
// живёт ОДНИМ местом: таблицей GFX_OPTIONS в render/renderer.js, по ней
// блок и строится. Здесь остаётся только то, чего в таблице нет: как
// назвать по-русски каждое значение. Ключа тут может и не быть — тогда
// на кнопке окажется само значение, и новая настройка всё равно появится
// в меню, пусть и с техническими подписями.
const GFX_VALUE_LABELS = {
    decor:        { sparse: 'Реже', normal: 'Обычно', dense: 'Гуще' },
    particles:    { off: 'Выкл', few: 'Мало', normal: 'Норма', many: 'Много' },
    renderScale:  { 0.5: '50 %', 0.75: '75 %', 1: '100 %' },
    viewDistance: { near: 'Ближе', normal: 'Обычно', far: 'Дальше' },
    nightLights:  { off: 'Выкл', normal: 'Норма', bright: 'Ярче' },
    timeOfDay:    { auto: 'Авто', day: 'День', dusk: 'Закат', night: 'Ночь' },
    weather:      { auto: 'Авто', clear: 'Ясно', wet: 'Дождь', snow: 'Снег', fog: 'Туман' },
};

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
    const preset = QUALITY_PRESETS.indexOf(quality) >= 0 ? quality : 'medium';
    return {
        name: lsGet(LS_NAME) || '',
        quality: preset,
        volume: Number.isFinite(volumeRaw) ? Math.min(1, Math.max(0, volumeRaw)) : 0.7,
        muted: lsGet(LS_MUTED) === '1',
        // Отдельные галочки графики. Их владелец — render/renderer.js: он же
        // их применяет. Меню только показывает и записывает.
        gfx: loadGfxSettings(preset),
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

// --- время суток и погода комнаты -------------------------------------------
//
// Это НАСТРОЙКИ КОМНАТЫ (settings.time_of_day, settings.weather), а не личные
// галочки графики: их видят все игроки. Личное переопределение живёт отдельно,
// в блоке настроек графики (`racing.gfx.timeofday` и `racing.gfx.weather`,
// значение `auto` = «как в комнате»).
//
// Списки и подписи держатся здесь одним местом: их берёт и форма создания
// комнаты, и панель настроек лобби. Погода устроена ровно как время суток —
// той же тройкой (значения, подписи, иконка) и тем же правилом при смене
// карты, поэтому оба блока строятся почти одинаковым кодом.

// --- режим комнаты: одиночная гонка или чемпионат ---------------------------
//
// Зеркало server/config.ROOM_MODES и STAGES_MIN/STAGES_MAX. Сервер всё равно
// проверяет каждое поле сам (коды ошибок bad_mode и bad_stages), здесь это
// только для формы и подписей.

export const ROOM_MODES = ['single', 'championship'];
export const ROOM_MODE_LABELS = { single: 'Одна гонка', championship: 'Чемпионат' };
export const STAGES_MIN = 3;
export const STAGES_MAX = 7;
export const STAGES_DEFAULT = 3;

// Короткие: подпись стоит в шапке панели рядом с заголовком, длинная
// строка там переносится на три строки и давит заголовок.
export const CHAMP_PHASE_LABELS = {
    lobby: 'ждём старта',
    break: 'пауза',
    racing: 'этап идёт',
    results: 'итоги этапа',
    final: 'сыграна',
};

export const ROOM_TIMES_OF_DAY = ['day', 'dusk', 'night'];

export const ROOM_TOD_LABELS = { day: 'День', dusk: 'Закат', night: 'Ночь' };

const ROOM_TOD_DESC = {
    day: 'Солнце высоко, всё видно',
    dusk: 'Низкое солнце и длинные тени',
    night: 'Фары, фонари и окна',
};

// Рисунок иконки: один и тот же контур обводится дважды — тёмным «карандашом»
// и цветом, как у силуэта трассы, чтобы кнопки стояли в одном стиле.
const TOD_ART = {
    day: '<circle cx="34" cy="27" r="8"/>'
        + '<path d="M34 10v4M34 40v4M13 27h4M51 27h4M20 13l3 3M45 38l3 3M48 13l-3 3M23 38l-3 3"/>',
    dusk: '<path d="M10 41h48"/><path d="M21 41a13 13 0 0 1 26 0"/>'
        + '<path d="M34 13v5M14 22l4 4M54 22l-4 4"/>',
    night: '<path d="M39 13a15 15 0 1 0 13 24A17 17 0 0 1 39 13z"/>'
        + '<path d="M19 18v.01M15 30v.01M25 38v.01"/>',
};

const TOD_COLORS = { day: '#ffc53d', dusk: '#ff8a3d', night: '#7aa7ff' };

/** Иконка времени суток в том же кадре 68x56, что и силуэт трассы. */
export function timeOfDayIconSvg(tod, size) {
    const art = TOD_ART[tod] || TOD_ART.day;
    const color = TOD_COLORS[tod] || TOD_COLORS.day;
    return envIconSvg(art, color, size);
}

// --- погода комнаты ---------------------------------------------------------
//
// Зеркало server/config.WEATHERS. Сервер всё равно проверяет поле сам
// (код ошибки bad_weather), здесь это только для формы и подписей.
//
// Порядок значений тот же, что на сервере: первое — умолчание.

export const ROOM_WEATHERS = ['clear', 'wet', 'snow', 'fog'];

export const ROOM_WEATHER_LABELS = {
    clear: 'Ясно', wet: 'Дождь', snow: 'Снег', fog: 'Туман',
};

// Пояснение говорит про сцепление, а не только про картинку: погода —
// не украшение, она меняет то, как машина едет.
const ROOM_WEATHER_DESC = {
    clear: 'Сухо, полное сцепление',
    wet: 'Мокрое полотно, сцепления меньше',
    snow: 'Снегопад, скользко сильнее всего',
    fog: 'Сухо, но видно недалеко',
};

// Тот же приём, что у TOD_ART: контур из штрихов, обводится дважды.
const WEATHER_ART = {
    clear: '<circle cx="34" cy="28" r="8"/>'
        + '<path d="M34 10v4M34 42v4M12 28h4M52 28h4M19 13l3 3M46 40l3 3'
        + 'M49 13l-3 3M22 40l-3 3"/>',
    wet: '<path d="M20 31a8 8 0 0 1 1-15 12 12 0 0 1 23 2 7 7 0 0 1 0 13z"/>'
        + '<path d="M23 38l-3 8M34 38l-3 8M45 38l-3 8"/>',
    snow: '<path d="M34 10v36M18 19l32 18M50 19L18 37"/>'
        + '<path d="M29 14l5 5 5-5M29 42l5-5 5 5"/>',
    fog: '<path d="M13 18h30M22 27h33M11 36h28M20 45h32"/>',
};

const WEATHER_COLORS = {
    clear: '#ffc53d', wet: '#5aa9ff', snow: '#cfe8ff', fog: '#9aa6b5',
};

/** Иконка погоды в том же кадре 68x56, что и силуэт трассы. */
export function weatherIconSvg(weather, size) {
    const art = WEATHER_ART[weather] || WEATHER_ART.clear;
    const color = WEATHER_COLORS[weather] || WEATHER_COLORS.clear;
    return envIconSvg(art, color, size);
}

// --- модель физики комнаты (§12.23, §12.24) ---------------------------------
//
// Зеркало server/config.PHYSICS_MODES. Сервер проверяет поле сам (код ошибки
// bad_physics), здесь это только форма и подписи. Порядок тот же, что на
// сервере: первое значение — умолчание.
//
// Переключатель имеет смысл ТОЛЬКО когда сервер поднят с --physics rapier:
// при прежней физике режим не меняет в заезде ничего, и показывать его
// значило бы врать игроку. Прячет его лобби, по welcome.physics_backend.

export const ROOM_PHYSICS = ['arcade', 'sim'];

export const ROOM_PHYSICS_LABELS = {
    arcade: 'Аркада', sim: 'Симулятор',
};

// Пояснение — про руль в руках, а не про движок: игроку выбирать ощущение.
export const ROOM_PHYSICS_DESC = {
    arcade: 'Цепко и послушно: короткий тормоз, крутая дуга, ручник помогает',
    sim: 'Честно и без помощи: длинный тормоз, широкая дуга, возить рулём',
};

const PHYSICS_ART = {
    arcade: '<path d="M38 8L18 33h13l-3 15 20-25H35z"/>',
    sim: '<path d="M11 42a23 23 0 0 1 46 0"/><path d="M34 42l15-13"/>'
        + '<path d="M16 42v.01M22 28v.01M34 22v.01"/>',
};

const PHYSICS_COLORS = { arcade: '#ffb224', sim: '#5aa9ff' };

/** Иконка модели физики в том же кадре 68x56, что и силуэт трассы. */
export function physicsIconSvg(mode, size) {
    const art = PHYSICS_ART[mode] || PHYSICS_ART.arcade;
    const color = PHYSICS_COLORS[mode] || PHYSICS_COLORS.arcade;
    return envIconSvg(art, color, size);
}

/** Общий кадр иконок окружения: тёмный «карандаш» под цветным контуром. */
function envIconSvg(art, color, size) {
    return '<svg viewBox="0 0 68 56" width="' + size + '" height="' + (size * 56 / 68)
        + '" fill="none" stroke-linecap="round" stroke-linejoin="round">'
        + '<g stroke="#080a12" stroke-width="10">' + art + '</g>'
        + '<g stroke="' + color + '" stroke-width="5">' + art + '</g>'
        + '</svg>';
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
    // Допустимые kind: 'error' (по умолчанию, красный), 'info' (синий),
    // 'ok' (зелёный). Раньше всё, кроме 'info', красилось в тревожный
    // красный — хорошая новость выглядела как ошибка.
    const TOAST_KIND = { info: ' toast-info', ok: ' toast-ok', error: '' };
    node.className = 'toast' + (TOAST_KIND[kind] !== undefined ? TOAST_KIND[kind] : '');
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

// --- траффик и происшествия на дороге ---------------------------------------
//
// Зеркало game/traffic.py:TRAFFIC_LEVELS и game/events.py:EVENT_LEVELS.
// Сервер всё равно проверяет оба поля сам (коды ошибок bad_traffic
// и bad_events), здесь это только форма и подписи.
//
// Порядок значений тот же, что в модулях: первое — умолчание, то есть
// «выключено». Комната без этих полей обязана вести себя как прежде.
//
// У обоих полей, в отличие от времени суток и погоды, НЕТ умолчания
// от карты: поток и происшествия — это режим игры, а не свойство места.
// Поэтому строка «Карта предлагает...» у них всегда пустая.

export const ROOM_TRAFFIC = ['off', 'sparse', 'dense'];

export const ROOM_TRAFFIC_LABELS = {
    off: 'Нет', sparse: 'Редкий', dense: 'Плотный',
};

const ROOM_TRAFFIC_DESC = {
    off: 'Трасса пустая, только гонщики',
    sparse: 'Редкие машины, обгон изредка',
    dense: 'Поток машин, обгон постоянно',
};

// Тот же приём, что у TOD_ART: контур из штрихов, обводится дважды.
// Смысл рисунка — сколько машин на полотне: пусто, одна, три.
const TRAFFIC_ART = {
    off: '<path d="M14 46h40M22 12v24M46 12v24"/>'
        + '<path d="M26 20l16 16M42 20l-16 16"/>',
    sparse: '<path d="M14 46h40M22 12v24M46 12v24"/>'
        + '<rect x="28" y="22" width="12" height="16" rx="3"/>',
    dense: '<path d="M14 46h40M22 10v28M46 10v28"/>'
        + '<rect x="26" y="12" width="10" height="13" rx="3"/>'
        + '<rect x="26" y="30" width="10" height="13" rx="3"/>'
        + '<rect x="38" y="21" width="10" height="13" rx="3"/>',
};

const TRAFFIC_COLORS = { off: '#7e8794', sparse: '#57c07a', dense: '#ffb224' };

/** Иконка плотности траффика в том же кадре 68x56, что и силуэт трассы. */
export function trafficIconSvg(level, size) {
    const art = TRAFFIC_ART[level] || TRAFFIC_ART.off;
    const color = TRAFFIC_COLORS[level] || TRAFFIC_COLORS.off;
    return envIconSvg(art, color, size);
}

export const ROOM_EVENTS = ['off', 'rare', 'often'];

export const ROOM_EVENT_LABELS = {
    off: 'Нет', rare: 'Редко', often: 'Часто',
};

// Пояснение говорит, что происшествия ВИДНО заранее: это не лотерея,
// а проверка реакции, и игрок должен понимать это до старта.
const ROOM_EVENT_DESC = {
    off: 'Дорога без сюрпризов',
    rare: 'Изредка, маяки предупреждают',
    often: 'Регулярно, маяки предупреждают',
};

// Предупреждающий треугольник: пусто, один, три.
const EVENT_ART = {
    off: '<path d="M34 14L52 44H16z"/><path d="M26 24l16 14M42 24l-16 14"/>',
    rare: '<path d="M34 12L54 46H14z"/><path d="M34 24v10M34 39v.01"/>',
    often: '<path d="M24 12L42 42H6z"/><path d="M24 22v8M24 35v.01"/>'
        + '<path d="M46 24L60 47H32z"/><path d="M46 32v6M46 42v.01"/>',
};

const EVENT_COLORS = { off: '#7e8794', rare: '#ffb224', often: '#ff6b57' };

/** Иконка частоты происшествий на дороге. */
export function roadEventIconSvg(level, size) {
    const art = EVENT_ART[level] || EVENT_ART.off;
    const color = EVENT_COLORS[level] || EVENT_COLORS.off;
    return envIconSvg(art, color, size);
}

// Настройки комнаты по умолчанию — ровно схема из раздела 9.
// time_of_day и weather здесь только заглушки: настоящее умолчание приходит
// из описания выбранной карты (welcome.content.tracks[] несёт оба поля —
// сервер кладёт их туда таблицей config.ENV_FIELDS).
function defaultRoomSettings() {
    return {
        track: 'office',
        time_of_day: ROOM_TIMES_OF_DAY[0],
        weather: ROOM_WEATHERS[0],
        laps: 3,
        max_players: 8,
        items_enabled: true,
        items: ['boost', 'rocket', 'mine', 'shield', 'storm'],
        collisions: true,
        mirror: false,
        // Поток машин и происшествия на дороге: по умолчанию выключены,
        // то есть новая комната ведёт себя ровно как до их появления.
        traffic: ROOM_TRAFFIC[0],
        events: ROOM_EVENTS[0],
        mode: ROOM_MODES[0],
        stages: STAGES_DEFAULT,
        // Обе доработки — за галочками и по умолчанию выключены.
        handicap: false,
        replay: false,
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
        // welcome.is_host — просто признак «этот клиент запустил сервер».
        // Правом создавать комнаты он больше НЕ управляет: заказчик снял
        // ограничение, серверная проверка убрана, код ошибки not_host не
        // выдаётся. Комнату создаёт любой подключившийся.
        this.isHost = false;
        this.serverName = '';
        this.rooms = [];
        this.servers = [];
        this.records = [];
        this.recordsEnabled = true;

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

    /** Событие `welcome`: контент сервера и имя сервера. */
    applyWelcome(msg) {
        this.content = msg.content || null;
        this.isHost = !!msg.is_host;
        this.serverName = msg.server_name || '';

        this.serverBadgeName.textContent = this.serverName || 'этот компьютер';
        this._buildTrackCards();
    }

    /** Событие `rooms`: комнаты, соседи по сети и рекорды кругов. */
    applyRooms(msg) {
        this.rooms = msg.rooms || [];
        this.servers = msg.servers || [];
        this.records = msg.records || [];
        this.recordsEnabled = msg.records_enabled !== false;
        this._renderRooms();
        this._renderServers();
        this._renderRecords();
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

        // --- отдельные переключатели графики -----------------------------
        // Требование заказчика: всё новое обязано выключаться по отдельности,
        // а не только тремя пресетами скопом. Блок свёрнут, чтобы меню не
        // разрослось: кому надо — раскроет.
        setBody.appendChild(this._buildGfx());

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

    /**
     * Блок отдельных настроек графики. Каждая строка пишет свой ключ
     * localStorage (`racing.gfx.*`) и тут же зовёт onSettingsChange —
     * рендер применяет изменение без перезагрузки страницы.
     */
    _buildGfx() {
        const box = el('details', 'gfx');
        const head = el('summary', 'gfx-summary', 'Отдельные настройки графики');
        box.appendChild(head);
        const body = el('div', 'gfx-body');

        this.gfxToggles = {};
        const self = this;

        function addToggle(key, caption, hint) {
            const field = el('div', 'field');
            const label = el('label', 'toggle');
            const input = el('input');
            input.type = 'checkbox';
            input.addEventListener('change', function () {
                const patch = {};
                patch[key] = input.checked;
                self._setGfx(patch);
            });
            label.appendChild(input);
            label.appendChild(el('span', 'toggle-track'));
            label.appendChild(el('span', null, caption));
            field.appendChild(label);
            if (hint) field.appendChild(el('div', 'gfx-hint', hint));
            body.appendChild(field);
            self.gfxToggles[key] = input;
        }

        function addSegmented(key, caption, values, labels, hint) {
            const field = el('div', 'field');
            field.appendChild(el('div', 'label', caption));
            const row = el('div', 'segmented segmented-tight');
            const buttons = [];
            for (let i = 0; i < values.length; i++) {
                const v = values[i];
                const btn = el('button', 'seg',
                    (labels && labels[v] !== undefined) ? labels[v] : String(v));
                btn.type = 'button';
                btn.addEventListener('click', function () {
                    const patch = {};
                    patch[key] = v;
                    self._setGfx(patch);
                });
                row.appendChild(btn);
                buttons.push(btn);
            }
            field.appendChild(row);
            if (hint) field.appendChild(el('div', 'gfx-hint', hint));
            body.appendChild(field);
            self.gfxSegments[key] = { values: values, buttons: buttons };
        }

        this.gfxSegments = {};
        // Строки берутся из GFX_OPTIONS — таблицы, которую ведёт владелец
        // рендера. Раньше список жил в двух местах, и новая настройка в
        // renderer.js просто не появлялась в меню, пока про меню не вспомнят.
        for (let i = 0; i < GFX_OPTIONS.length; i++) {
            const opt = GFX_OPTIONS[i];
            if (opt.kind === 'toggle') {
                addToggle(opt.key, opt.label, opt.hint);
            } else if (opt.kind === 'enum') {
                addSegmented(opt.key, opt.label, opt.values,
                    GFX_VALUE_LABELS[opt.key] || null, opt.hint);
            } else {
                console.warn('menu.js: настройка графики %s неизвестного вида %s',
                             opt.key, opt.kind);
            }
        }

        box.appendChild(body);
        return box;
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

        this.roomsList = el('div', 'rooms-list scroll');
        roomsPanel.appendChild(this.roomsList);
        right.appendChild(roomsPanel);

        // Рекорды кругов: живут между сессиями сервера (server/records.py)
        // и приезжают вместе со списком комнат — тем же событием `rooms`.
        const recPanel = el('div', 'panel records-panel');
        const rHead = el('div', 'panel-head');
        rHead.appendChild(el('h2', null, 'Рекорды кругов'));
        const rSpacer = el('div');
        rSpacer.style.flex = '1';
        rHead.appendChild(rSpacer);
        this.recordsNote = el('div', 'label', '');
        rHead.appendChild(this.recordsNote);
        recPanel.appendChild(rHead);
        this.recordsList = el('div', 'records-list scroll');
        recPanel.appendChild(this.recordsList);
        right.appendChild(recPanel);

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

        // Режим: одна гонка или чемпионат из нескольких этапов.
        const modeField = el('div', 'field form-wide');
        modeField.appendChild(el('div', 'label', 'Режим'));
        const modeRow = el('div', 'row');
        modeRow.style.gap = '10px';
        const modeSeg = el('div', 'segmented');
        this.modeButtons = [];
        for (let i = 0; i < ROOM_MODES.length; i++) {
            const mode = ROOM_MODES[i];
            const btn = el('button', 'seg', ROOM_MODE_LABELS[mode]);
            btn.type = 'button';
            btn.addEventListener('click', () => this._selectMode(mode));
            modeSeg.appendChild(btn);
            this.modeButtons.push({ id: mode, node: btn });
        }
        modeRow.appendChild(modeSeg);
        this.stagesWrap = el('div', 'row');
        this.stagesWrap.style.gap = '9px';
        this.stagesWrap.appendChild(el('div', 'label', 'этапов'));
        this.stagesStepper = this._buildStepper(STAGES_MIN, STAGES_MAX,
            (v) => { this.draft.stages = v; });
        this.stagesWrap.appendChild(this.stagesStepper.node);
        modeRow.appendChild(this.stagesWrap);
        modeField.appendChild(modeRow);
        this.modeHint = el('div', 'gfx-hint', '');
        modeField.appendChild(this.modeHint);
        grid.appendChild(modeField);

        // Трасса
        const trackField = el('div', 'field form-wide');
        trackField.appendChild(el('div', 'label', 'Трасса'));
        this.trackCards = el('div', 'track-cards');
        trackField.appendChild(this.trackCards);
        grid.appendChild(trackField);

        // Окружение: время суток и погода. Оба — настройки комнаты, оба
        // стоят сразу под выбором карты, потому что умолчание обоих берётся
        // именно из карты. Ряды строятся одним и тем же кодом: это два поля
        // одного механизма (§12.16), и выглядеть они обязаны одинаково.
        const tod = this._buildEnvField(grid, 'Время суток', ROOM_TIMES_OF_DAY,
            ROOM_TOD_LABELS, ROOM_TOD_DESC, timeOfDayIconSvg,
            (id) => this._selectTimeOfDay(id));
        this.todButtons = tod.buttons;
        this.todHint = tod.hint;

        const weather = this._buildEnvField(grid, 'Погода', ROOM_WEATHERS,
            ROOM_WEATHER_LABELS, ROOM_WEATHER_DESC, weatherIconSvg,
            (id) => this._selectWeather(id));
        this.weatherButtons = weather.buttons;
        this.weatherHint = weather.hint;

        // Поток машин и происшествия на дороге. Стоят следом за окружением
        // и строятся тем же кодом: для игрока это такие же три кнопки,
        // и учиться новому элементу управления не приходится.
        const traffic = this._buildEnvField(grid, 'Поток машин', ROOM_TRAFFIC,
            ROOM_TRAFFIC_LABELS, ROOM_TRAFFIC_DESC, trafficIconSvg,
            (id) => this._selectTraffic(id));
        this.trafficButtons = traffic.buttons;

        const events = this._buildEnvField(grid, 'Происшествия на дороге',
            ROOM_EVENTS, ROOM_EVENT_LABELS, ROOM_EVENT_DESC, roadEventIconSvg,
            (id) => this._selectRoadEvents(id));
        this.eventButtons = events.buttons;

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

        // Гандикап и повтор финиша: отдельным блоком, обе выключены.
        const extraField = el('div', 'field form-wide');
        const extraRow = el('div', 'row');
        extraRow.style.gap = '26px';

        const handicapLabel = el('label', 'toggle');
        this.handicapInput = el('input');
        this.handicapInput.type = 'checkbox';
        this.handicapInput.addEventListener('change', () => {
            this.draft.handicap = this.handicapInput.checked;
            this._syncExtraHints();
        });
        handicapLabel.appendChild(this.handicapInput);
        handicapLabel.appendChild(el('span', 'toggle-track'));
        handicapLabel.appendChild(el('span', null, 'Гандикап победителю'));
        extraRow.appendChild(handicapLabel);

        const replayLabel = el('label', 'toggle');
        this.replayInput = el('input');
        this.replayInput.type = 'checkbox';
        this.replayInput.addEventListener('change', () => {
            this.draft.replay = this.replayInput.checked;
            this._syncExtraHints();
        });
        replayLabel.appendChild(this.replayInput);
        replayLabel.appendChild(el('span', 'toggle-track'));
        replayLabel.appendChild(el('span', null, 'Повтор финиша'));
        extraRow.appendChild(replayLabel);

        extraField.appendChild(extraRow);
        this.extraHint = el('div', 'gfx-hint', '');
        extraField.appendChild(this.extraHint);
        grid.appendChild(extraField);

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

    /**
     * Ряд карточек выбора для поля окружения (время суток, погода).
     *
     * Карточки те же, что у выбора трассы: иконка, название, пояснение.
     * Сетка задана в стилях на три колонки; когда значений больше, число
     * колонок правится здесь же инлайном — так ряд из четырёх карточек
     * не переносится на вторую строку, а стили трогать не приходится.
     */
    _buildEnvField(grid, title, values, labels, descs, iconSvg, onPick) {
        const field = el('div', 'field form-wide');
        field.appendChild(el('div', 'label', title));
        const cards = el('div', 'track-cards');
        if (values.length !== 3) {
            cards.style.gridTemplateColumns = 'repeat(' + values.length + ', 1fr)';
        }
        const buttons = [];
        for (let i = 0; i < values.length; i++) {
            const id = values[i];
            const card = el('button', 'track-card');
            card.type = 'button';
            const art = el('div');
            art.innerHTML = iconSvg(id, 68);
            card.appendChild(art);
            card.appendChild(el('div', 't-name', labels[id] || id));
            card.appendChild(el('div', 't-desc', descs[id] || ''));
            card.addEventListener('click', () => onPick(id));
            cards.appendChild(card);
            buttons.push({ id: id, node: card });
        }
        field.appendChild(cards);
        const hint = el('div', 'gfx-hint');
        field.appendChild(hint);
        grid.appendChild(field);
        return { field: field, cards: cards, buttons: buttons, hint: hint };
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
            this.draft.time_of_day = this._trackTimeOfDay(this.draft.track);
            this.draft.weather = this._trackWeather(this.draft.track);
        }
        this._syncTrackCards();
        this._syncEnvironment();
    }

    _hasTrack(id) {
        const tracks = (this.content && this.content.tracks) || [];
        for (let i = 0; i < tracks.length; i++) if (tracks[i].id === id) return true;
        return false;
    }

    _selectTrack(id) {
        // Правило то же, что на сервере (см. validate_settings) и одно на оба
        // поля окружения: значение, равное предложению ПРЕДЫДУЩЕЙ карты, ехало
        // за картой — пусть едет и за новой. Выбранное руками (отличное от
        // предложения) остаётся.
        if (this.draft.time_of_day === this._trackTimeOfDay(this.draft.track)) {
            this.draft.time_of_day = this._trackTimeOfDay(id);
        }
        if (this.draft.weather === this._trackWeather(this.draft.track)) {
            this.draft.weather = this._trackWeather(id);
        }
        this.draft.track = id;
        this._syncTrackCards();
        this._syncEnvironment();
    }

    /** Поле окружения, предложенное описанием карты (welcome.content.tracks[]). */
    _trackEnvField(id, field, values) {
        const tracks = (this.content && this.content.tracks) || [];
        for (let i = 0; i < tracks.length; i++) {
            if (tracks[i].id === id) {
                const value = tracks[i][field];
                if (values.indexOf(value) >= 0) return value;
            }
        }
        return values[0];
    }

    _trackTimeOfDay(id) {
        return this._trackEnvField(id, 'time_of_day', ROOM_TIMES_OF_DAY);
    }

    _trackWeather(id) {
        return this._trackEnvField(id, 'weather', ROOM_WEATHERS);
    }

    _selectTimeOfDay(tod) {
        this.draft.time_of_day = tod;
        this._syncEnvironment();
    }

    _selectWeather(weather) {
        this.draft.weather = weather;
        this._syncEnvironment();
    }

    _selectTraffic(level) {
        this.draft.traffic = level;
        this._syncRoadRows();
    }

    _selectRoadEvents(level) {
        this.draft.events = level;
        this._syncRoadRows();
    }

    /** Подсветка выбранного в рядах потока и происшествий. */
    _syncRoadRows() {
        const rows = [
            [this.trafficButtons, this.draft.traffic],
            [this.eventButtons, this.draft.events],
        ];
        for (let r = 0; r < rows.length; r++) {
            const buttons = rows[r][0];
            if (!buttons) continue;
            for (let i = 0; i < buttons.length; i++) {
                buttons[i].node.classList.toggle('is-active',
                    buttons[i].id === rows[r][1]);
            }
        }
    }

    _selectMode(mode) {
        this.draft.mode = mode;
        this._syncMode();
    }

    _syncMode() {
        const mode = this.draft.mode;
        for (let i = 0; i < this.modeButtons.length; i++) {
            this.modeButtons[i].node.classList.toggle('is-active',
                this.modeButtons[i].id === mode);
        }
        const series = mode === 'championship';
        this.stagesWrap.hidden = !series;
        this.modeHint.textContent = series
            ? 'Трасса выше — первый этап. Остальные сервер выберет случайно '
              + 'без повторов и покажет календарь целиком.'
            : '';
    }

    _syncExtraHints() {
        const parts = [];
        if (this.draft.handicap) {
            parts.push('Гандикап: победитель прошлой гонки едет на 3 % медленнее.');
        }
        if (this.draft.replay) {
            parts.push('Повтор: последние 3 секунды гонки на экране итогов.');
        }
        this.extraHint.textContent = parts.join(' ');
    }

    /** Оба ряда окружения: подсветка выбранного и предложение карты. */
    _syncEnvironment() {
        this._syncEnvRow(this.todButtons, this.todHint, this.draft.time_of_day,
            this._trackTimeOfDay(this.draft.track), ROOM_TOD_LABELS);
        this._syncRoadRows();
        this._syncEnvRow(this.weatherButtons, this.weatherHint, this.draft.weather,
            this._trackWeather(this.draft.track), ROOM_WEATHER_LABELS);
    }

    _syncEnvRow(buttons, hint, chosen, proposed, labels) {
        if (!buttons) return;
        for (let i = 0; i < buttons.length; i++) {
            buttons[i].node.classList.toggle('is-active', buttons[i].id === chosen);
        }
        // Выбор руками не перетирается сменой карты, поэтому расхождение
        // с предложением карты показываем прямо здесь, а не молчим о нём.
        hint.textContent = proposed === chosen
            ? '' : 'Карта предлагает: ' + (labels[proposed] || proposed);
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
        this.draft = defaultRoomSettings();
        if (!this._hasTrack(this.draft.track)) {
            const tracks = (this.content && this.content.tracks) || [];
            if (tracks.length) this.draft.track = tracks[0].id;
        }
        // Форма открывается с умолчанием выбранной карты — как и в лобби.
        this.draft.time_of_day = this._trackTimeOfDay(this.draft.track);
        this.draft.weather = this._trackWeather(this.draft.track);
        this.roomNameInput.value = 'Комната: ' + this.getName();
        this.lapsStepper.set(this.draft.laps);
        this.maxStepper.set(this.draft.max_players);
        this.stagesStepper.set(this.draft.stages);
        this.collisionsInput.checked = this.draft.collisions;
        this.mirrorInput.checked = this.draft.mirror;
        this.handicapInput.checked = this.draft.handicap;
        this.replayInput.checked = this.draft.replay;
        this._syncTrackCards();
        this._syncEnvironment();
        this._syncItemChecks();
        this._syncMode();
        this._syncExtraHints();
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
            time_of_day: this.draft.time_of_day,
            weather: this.draft.weather,
            laps: this.draft.laps,
            max_players: this.draft.max_players,
            items_enabled: this.draft.items_enabled,
            items: this.draft.items.slice(),
            collisions: this.draft.collisions,
            mirror: this.draft.mirror,
            traffic: this.draft.traffic,
            events: this.draft.events,
            mode: this.draft.mode,
            stages: this.draft.stages,
            handicap: this.draft.handicap,
            replay: this.draft.replay,
        };
        this._closeModal();
        if (this.handlers.onCreateRoom) this.handlers.onCreateRoom(name, settings);
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
            note.textContent = 'Комнат пока нет. Создайте первую — остальные увидят её сразу.';
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

            const laps = el('div', 'room-laps num');
            laps.appendChild(el('span', null, room.laps + ' круг' + lapSuffix(room.laps)));
            // Идущая серия — главный повод зайти именно в эту комнату.
            if (room.mode === 'championship') {
                laps.appendChild(el('span', 'room-champ',
                    room.stages ? 'этап ' + room.stage + '/' + room.stages : 'чемпионат'));
            }
            row.appendChild(laps);

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

    /** Название машины по id (welcome.content.cars). */
    _carName(carId) {
        const cars = (this.content && this.content.cars) || [];
        for (let i = 0; i < cars.length; i++) {
            if (cars[i].id === carId) return cars[i].name || carId;
        }
        return carId || '—';
    }

    /**
     * Таблица рекордов. Строка на трассу: абсолютный рекорд крупно, под ним
     * рекорды по машинам мелко — именно эти две вещи и просят посмотреть
     * («а кто быстрее всех на фургоне?»).
     */
    _renderRecords() {
        const list = this.recordsList;
        list.innerHTML = '';
        if (!this.recordsEnabled) {
            this.recordsNote.textContent = 'выключены';
            const note = el('div', 'empty-note');
            note.textContent = 'Сервер запущен с ключом --no-records: '
                + 'рекорды не сохраняются.';
            list.appendChild(note);
            return;
        }
        this.recordsNote.textContent = this.records.length
            ? this.records.length + ' трасс' + trackSuffix(this.records.length) : '';
        if (!this.records.length) {
            const note = el('div', 'empty-note');
            note.textContent = 'Рекордов пока нет. Первый круг на любой трассе — '
                + 'уже рекорд, и он переживёт перезапуск сервера.';
            list.appendChild(note);
            return;
        }

        for (let i = 0; i < this.records.length; i++) {
            const entry = this.records[i];
            const track = this._trackLabel(entry.track);
            const row = el('div', 'record-row');

            const head = el('div', 'rec-head');
            head.appendChild(el('span', 'theme-chip theme-' + (track.theme || 'city')));
            const title = el('span', 'rec-track',
                (track.name || entry.track) + (entry.mirror ? ' (зеркало)' : ''));
            head.appendChild(title);
            if (entry.best) {
                head.appendChild(el('span', 'rec-best num', formatLap(entry.best.time)));
                head.appendChild(el('span', 'rec-who', entry.best.name || 'Гонщик'));
                head.appendChild(el('span', 'rec-date', entry.best.date || ''));
            }
            row.appendChild(head);

            const cars = entry.cars || [];
            if (cars.length) {
                const carsRow = el('div', 'rec-cars');
                for (let k = 0; k < cars.length; k++) {
                    const car = cars[k];
                    const chip = el('div', 'rec-car');
                    chip.appendChild(el('span', 'rc-name', this._carName(car.car)));
                    chip.appendChild(el('span', 'rc-time num', formatLap(car.time)));
                    chip.appendChild(el('span', 'rc-who', car.name || ''));
                    carsRow.appendChild(chip);
                }
                row.appendChild(carsRow);
            }
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
        this._applyGfxState();
    }

    /** Разложить текущие галочки графики по элементам управления. */
    _applyGfxState() {
        const g = this.settings.gfx;
        if (!g || !this.gfxToggles) return;
        for (const key in this.gfxToggles) {
            this.gfxToggles[key].checked = !!g[key];
        }
        for (const key in this.gfxSegments) {
            const seg = this.gfxSegments[key];
            for (let i = 0; i < seg.buttons.length; i++) {
                seg.buttons[i].classList.toggle('is-active', seg.values[i] === g[key]);
            }
        }
    }

    /**
     * Изменить одну настройку графики. Значение уходит в localStorage, а
     * onSettingsChange заставляет рендер перечитать его и применить —
     * при необходимости с пересборкой сцены, но без перезагрузки страницы.
     */
    _setGfx(patch) {
        this.settings.gfx = saveGfxSettings(patch, this.settings.quality);
        this._applyGfxState();
        this._emitSettings();
    }

    _emitSettings() {
        if (this.handlers.onSettingsChange) this.handlers.onSettingsChange(this.settings);
    }

    _setQuality(preset) {
        this.settings = saveUiSettings({ quality: preset });
        // Пресет — быстрая заготовка: он выставляет ВСЕ отдельные галочки.
        this.settings.gfx = applyGfxPreset(preset);
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

/** «1:02.31» или «39.58» — время круга в таблице рекордов. */
function formatLap(seconds) {
    if (!(seconds > 0)) return '—';
    const hundredths = Math.round(seconds * 100);
    const cs = hundredths % 100;
    const total = (hundredths - cs) / 100;
    const s = total % 60;
    const m = (total - s) / 60;
    const csText = cs < 10 ? '0' + cs : String(cs);
    if (m > 0) return m + ':' + (s < 10 ? '0' + s : s) + '.' + csText;
    return s + '.' + csText;
}

/** «1 трасса», «3 трассы», «5 трасс» — окончание по числу. */
function trackSuffix(n) {
    const mod100 = n % 100;
    if (mod100 >= 11 && mod100 <= 14) return '';
    const mod10 = n % 10;
    if (mod10 === 1) return 'а';
    if (mod10 >= 2 && mod10 <= 4) return 'ы';
    return '';
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
