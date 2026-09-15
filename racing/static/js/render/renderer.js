/**
 * renderer.js — [render] сцена, камеры, кадровый цикл.
 *
 * Собирает гонку из готовых кирпичей: buildTrackMeshes и createTerrainSampler
 * (trackmesh.js), buildScenery (scenery.js), buildCarMesh (carmesh.js),
 * Effects (effects.js). Сам добавляет то, что 12.6 закрепило за этим файлом:
 * тени под машинами и меши боксов с бонусами — по одному InstancedMesh на всё.
 *
 * --------------------------------------------------------------------------
 * БЮДЖЕТ (раздел 1: 60 draw call, 150 000 треугольников на кадр)
 * --------------------------------------------------------------------------
 * Трасса 6 + декор до 13 + восемь машин по 4 = до 51 draw call занято другими
 * исполнителями. Свои вызовы этот файл держит в двух, эффекты — ещё до пяти:
 *
 *   shadowMesh   1   круглые тени всех восьми машин (никаких shadow map)
 *   boxMesh      1   все боксы с бонусами трассы
 *   trafficMesh  0-2 ВЕСЬ траффик: по одному InstancedMesh на силуэт
 *   effects      0-7 см. шапку effects.js
 *   cockpit      0-2 панель и руль, только в виде из кокпита
 *
 * Траффик отдельно. Гонщик стоит четыре вызова (кузов, колёса, фары, стопы),
 * то есть двенадцать болванок наивным способом стоили бы 48 вызовов — весь
 * бюджет кадра. Поэтому болванке собирается ОДНА слитая геометрия (кузов,
 * четыре колеса на своих местах, фары и стопы одним материалом) и все
 * болванки одного силуэта рисуются одним InstancedMesh с инстансным цветом.
 * Силуэтов два, значит потолок — два вызова на весь поток. Колёса у болванки
 * не крутятся: это единственное, чем платим, и на скорости потока это
 * не видно.
 *
 * Кадровый цикл не аллоцирует: все векторы, кватернионы и матрицы — модульные
 * константы ниже, матрицы инстансов пишутся числами прямо в буфер.
 *
 * --------------------------------------------------------------------------
 * КАК ЭТИМ ПОЛЬЗУЕТСЯ main.js
 * --------------------------------------------------------------------------
 *   const rr = new RaceRenderer(canvas, { quality: 'medium' });
 *   rr.resize(window.innerWidth, window.innerHeight);
 *   rr.createRace(track, players, { localSlot: mySlot });   // track — Track или race_init.track
 *   // в кадре:
 *   rr.beginFrame();
 *   для каждой машины: rr.setCarState(slot, x, z, yaw, vx, vz, steer, flags, driftCharge);
 *   rr.applySnapshot(snapshot);      // снаряды и маска боксов
 *   rr.frame(dtSeconds);             // анимация + камера + эффекты + отрисовка
 *   hud.update(rr.getStats());
 *   // при возврате в лобби:
 *   rr.dispose();                    // сцена, пулы, кэш моделей; контекст GL жив
 *   // при выходе из приложения:
 *   rr.destroy();
 */

import * as THREE from 'three';
import { buildTrackMeshes, createTerrainSampler } from './trackmesh.js';
import { buildScenery, normalizeEnvironment, ENV_DEFAULT, trackDefaultEnvironment } from './scenery.js';
import { buildCarMesh, disposeCarCache } from './carmesh.js';
import { disposeObject, solidify, toColor, mergeGeometries, ViewCuller } from './geomutil.js';
import { Effects, createRadialTexture, writeScaleYaw } from './effects.js';
import { Track } from '../track.js';
import { getCatalog } from '../cars.js';
import {
    MAX_CARS,
    MAX_TRAFFIC,
    FLAG_OFFTRACK,
    FLAG_DRIFTING,
    FLAG_BOOST,
    FLAG_SPIN,
    FLAG_GHOST,
    FLAG_BRAKING
} from '../protocol.js';

// ---------------------------------------------------------------------------
// Пресеты качества (12.6: имена low / medium / high)
// ---------------------------------------------------------------------------

export const QUALITY_PRESETS = {
    low: { renderScale: 0.5, cockpitDetail: 0, shadowOpacity: 0.5 },
    medium: { renderScale: 0.75, cockpitDetail: 1, shadowOpacity: 0.56 },
    high: { renderScale: 1.0, cockpitDetail: 2, shadowOpacity: 0.6 }
};

// ---------------------------------------------------------------------------
// Отдельные настройки графики
// ---------------------------------------------------------------------------
//
// Требование заказчика: каждая добавка к картинке обязана выключаться своей
// галочкой, а пресеты low/medium/high остаются быстрыми заготовками, которые
// выставляют эти галочки скопом.
//
// Хранилище — localStorage, ключи в стиле 12.6 (`racing.*`). Владелец
// значений здесь, в рендере: меню только читает и пишет их через эти функции,
// а применение целиком на стороне рендера (setQuality -> refreshGfx).

export const GFX_KEYS = {
    ao: 'racing.gfx.ao',
    glow: 'racing.gfx.glow',
    decor: 'racing.gfx.decor',
    decorAnim: 'racing.gfx.decoranim',
    particles: 'racing.gfx.particles',
    renderScale: 'racing.gfx.renderscale',
    // добавлены вместе с отсечением и временем суток
    viewDistance: 'racing.gfx.viewdistance',
    nightLights: 'racing.gfx.nightlights',
    timeOfDay: 'racing.gfx.timeofday',
    // добавлены вместе с запечённым светом, погодой, следами шин,
    // отражением на кузове и тональной коррекцией
    bakedLight: 'racing.gfx.bakedlight',
    wetRoad: 'racing.gfx.wetroad',
    weather: 'racing.gfx.weather',
    tireMarks: 'racing.gfx.tiremarks',
    carReflect: 'racing.gfx.carreflect',
    toneMap: 'racing.gfx.tonemap'
};

export const DECOR_LEVELS = ['sparse', 'normal', 'dense'];
export const PARTICLE_LEVELS = ['off', 'few', 'normal', 'many'];
export const RENDER_SCALES = [0.5, 0.75, 1.0];

/**
 * Дальность отрисовки — множитель к дальности тумана из buildScenery.
 * Туман и отсечение обязаны идти вместе: отсечение режет ровно по той
 * границе, за которой туман и так подменил цвет объекта своим. Поэтому это
 * одна настройка, а не две.
 */
export const VIEW_DISTANCES = ['near', 'normal', 'far'];
const VIEW_DISTANCE_K = { near: 0.75, normal: 1.0, far: 1.3 };

/** Яркость ночных огней: фонари, окна, подсветка щитов, фары. */
export const NIGHT_LIGHT_LEVELS = ['off', 'normal', 'bright'];
const NIGHT_LIGHT_K = { off: 0, normal: 1.0, bright: 1.45 };

/**
 * Время суток. `auto` — то, что выбрано в настройках комнаты; остальные три
 * значения перебивают выбор комнаты локально, на этом клиенте.
 */
export const TIME_OF_DAY_MODES = ['auto', 'day', 'dusk', 'night'];

/**
 * Погода. Устроена ровно как время суток: `auto` — как в комнате, остальное
 * перебивает её локально. Комната погоду пока не рассылает (это следующий
 * этап, строка под неё в серверной таблице ENV_FIELDS лежит закомментированной),
 * поэтому до тех пор `auto` означает «сухо».
 */
export const WEATHER_MODES = ['auto', 'clear', 'wet'];

/** Сила отражения окружения на кузове по времени суток. */
const REFLECT_K = { day: 0.40, dusk: 0.48, night: 0.60 };

/**
 * Тональная коррекция. Ставится ОДИН РАЗ на рендер, при создании и при
 * пересборке: смена на лету пересобрала бы все шейдерные программы сцены,
 * а Firefox компилирует их синхронно (KHR_parallel_shader_compile у него нет).
 * В кадре стоит несколько ALU на пиксель и ноль проходов.
 */
const TONE_EXPOSURE = 1.05;

/** Что выставляет каждый пресет. Это ТОЛЬКО умолчания: галочки главнее. */
export const GFX_PRESETS = {
    low: {
        ao: true, glow: false, decor: 'sparse', decorAnim: false, particles: 'few',
        renderScale: 0.5, viewDistance: 'near', nightLights: 'normal', timeOfDay: 'auto',
        // запечённый свет включён и на низком: он не тратит ресурсы,
        // а освобождает — треть памяти геометрии и 12 байт выборки на вершину
        bakedLight: true, wetRoad: false, weather: 'auto', tireMarks: false,
        carReflect: false, toneMap: false
    },
    medium: {
        ao: true, glow: true, decor: 'normal', decorAnim: true, particles: 'normal',
        renderScale: 0.75, viewDistance: 'normal', nightLights: 'normal', timeOfDay: 'auto',
        bakedLight: true, wetRoad: true, weather: 'auto', tireMarks: true,
        carReflect: true, toneMap: true
    },
    high: {
        ao: true, glow: true, decor: 'dense', decorAnim: true, particles: 'many',
        renderScale: 1.0, viewDistance: 'far', nightLights: 'bright', timeOfDay: 'auto',
        bakedLight: true, wetRoad: true, weather: 'auto', tireMarks: true,
        carReflect: true, toneMap: true
    }
};

/** Настройки, ради которых сцену приходится пересобирать. */
const GFX_REBUILD = [
    'ao', 'decor', 'decorAnim', 'particles', 'nightLights', 'timeOfDay',
    // запечённый свет меняет материалы и выбрасывает атрибут нормалей;
    // погода и мокрый асфальт запечены в вершинные цвета; следы шин — это
    // атрибут uv и текстура; отражение — снятая кубкарта; тональная
    // коррекция — пересборка всех шейдерных программ
    'bakedLight', 'wetRoad', 'weather', 'tireMarks', 'carReflect', 'toneMap'
];

function lsGet(key) {
    try {
        return window.localStorage.getItem(key);
    } catch (e) {
        return null; // приватное окно: живём без хранилища
    }
}

function lsSet(key, value) {
    try {
        window.localStorage.setItem(key, value);
    } catch (e) {
        /* нет хранилища — настройка проживёт до перезагрузки */
    }
}

function readBool(key, def) {
    const v = lsGet(key);
    if (v === '1') return true;
    if (v === '0') return false;
    return def;
}

function readEnum(key, list, def) {
    const v = lsGet(key);
    return list.indexOf(v) >= 0 ? v : def;
}

/**
 * Текущие настройки графики. Значение по умолчанию берётся из пресета,
 * поэтому свежий профиль сразу получает осмысленный набор.
 */
export function loadGfxSettings(quality) {
    const preset = GFX_PRESETS[quality] || GFX_PRESETS.medium;
    const scaleRaw = parseFloat(lsGet(GFX_KEYS.renderScale));
    return {
        ao: readBool(GFX_KEYS.ao, preset.ao),
        glow: readBool(GFX_KEYS.glow, preset.glow),
        decor: readEnum(GFX_KEYS.decor, DECOR_LEVELS, preset.decor),
        decorAnim: readBool(GFX_KEYS.decorAnim, preset.decorAnim),
        particles: readEnum(GFX_KEYS.particles, PARTICLE_LEVELS, preset.particles),
        viewDistance: readEnum(GFX_KEYS.viewDistance, VIEW_DISTANCES, preset.viewDistance),
        nightLights: readEnum(GFX_KEYS.nightLights, NIGHT_LIGHT_LEVELS, preset.nightLights),
        timeOfDay: readEnum(GFX_KEYS.timeOfDay, TIME_OF_DAY_MODES, preset.timeOfDay),
        bakedLight: readBool(GFX_KEYS.bakedLight, preset.bakedLight),
        wetRoad: readBool(GFX_KEYS.wetRoad, preset.wetRoad),
        weather: readEnum(GFX_KEYS.weather, WEATHER_MODES, preset.weather),
        tireMarks: readBool(GFX_KEYS.tireMarks, preset.tireMarks),
        carReflect: readBool(GFX_KEYS.carReflect, preset.carReflect),
        toneMap: readBool(GFX_KEYS.toneMap, preset.toneMap),
        renderScale: RENDER_SCALES.indexOf(scaleRaw) >= 0 ? scaleRaw : preset.renderScale
    };
}

/** Записать часть настроек. Возвращает полный набор после записи. */
export function saveGfxSettings(patch, quality) {
    if (patch.ao !== undefined) lsSet(GFX_KEYS.ao, patch.ao ? '1' : '0');
    if (patch.glow !== undefined) lsSet(GFX_KEYS.glow, patch.glow ? '1' : '0');
    if (patch.decor !== undefined) lsSet(GFX_KEYS.decor, patch.decor);
    if (patch.decorAnim !== undefined) lsSet(GFX_KEYS.decorAnim, patch.decorAnim ? '1' : '0');
    if (patch.particles !== undefined) lsSet(GFX_KEYS.particles, patch.particles);
    if (patch.renderScale !== undefined) lsSet(GFX_KEYS.renderScale, String(patch.renderScale));
    if (patch.viewDistance !== undefined) lsSet(GFX_KEYS.viewDistance, patch.viewDistance);
    if (patch.nightLights !== undefined) lsSet(GFX_KEYS.nightLights, patch.nightLights);
    if (patch.timeOfDay !== undefined) lsSet(GFX_KEYS.timeOfDay, patch.timeOfDay);
    if (patch.bakedLight !== undefined) lsSet(GFX_KEYS.bakedLight, patch.bakedLight ? '1' : '0');
    if (patch.wetRoad !== undefined) lsSet(GFX_KEYS.wetRoad, patch.wetRoad ? '1' : '0');
    if (patch.weather !== undefined) lsSet(GFX_KEYS.weather, patch.weather);
    if (patch.tireMarks !== undefined) lsSet(GFX_KEYS.tireMarks, patch.tireMarks ? '1' : '0');
    if (patch.carReflect !== undefined) lsSet(GFX_KEYS.carReflect, patch.carReflect ? '1' : '0');
    if (patch.toneMap !== undefined) lsSet(GFX_KEYS.toneMap, patch.toneMap ? '1' : '0');
    return loadGfxSettings(quality);
}

/**
 * Описание всех отдельных настроек графики в одном месте.
 *
 * Владелец значений — этот файл (12.14), меню только показывает и пишет их
 * через loadGfxSettings/saveGfxSettings. Таблица нужна, чтобы новая галочка
 * появлялась в меню сама: `ui/menu.js` обходит её и строит по строке на
 * запись, вместо того чтобы перечислять настройки второй раз у себя.
 *
 *   key    — поле в объекте настроек и в GFX_KEYS;
 *   kind   — 'toggle' (галочка) или 'enum' (сегментированный выбор);
 *   values — допустимые значения для 'enum';
 *   label  — подпись, hint — пояснение под ней.
 */
export const GFX_OPTIONS = [
    { key: 'ao', kind: 'toggle', label: 'Запечённое затенение',
      hint: 'Тени в стыках и у оснований. В кадре бесплатно.' },
    { key: 'bakedLight', kind: 'toggle', label: 'Запечённый свет',
      hint: 'Свет солнца и неба считается один раз при сборке трассы. Освобождает треть памяти геометрии.' },
    { key: 'glow', kind: 'toggle', label: 'Свечение огней',
      hint: 'Фары, стоп-сигналы, турбо, маяки мин.' },
    { key: 'carReflect', kind: 'toggle', label: 'Отражение на кузове',
      hint: 'Небо и дома отражаются в краске. Ночью в отражении видны окна.' },
    { key: 'wetRoad', kind: 'toggle', label: 'Мокрый асфальт',
      hint: 'Тёмное полотно и блик отражённого неба в дождливую погоду.' },
    { key: 'weather', kind: 'enum', values: WEATHER_MODES, label: 'Погода',
      hint: 'Обычно берётся из настроек комнаты. Здесь можно перебить для себя.' },
    { key: 'tireMarks', kind: 'toggle', label: 'Следы шин',
      hint: 'Чёрные полосы там, где несло и тормозили юзом. Копятся за гонку.' },
    { key: 'toneMap', kind: 'toggle', label: 'Тональная коррекция',
      hint: 'Мягкие света и глубокие тени вместо плоской заливки.' },
    { key: 'decorAnim', kind: 'toggle', label: 'Анимация декора',
      hint: 'Качание деревьев и флагов, движение толпы.' },
    { key: 'decor', kind: 'enum', values: DECOR_LEVELS, label: 'Плотность декора' },
    { key: 'particles', kind: 'enum', values: PARTICLE_LEVELS, label: 'Частицы' },
    { key: 'renderScale', kind: 'enum', values: RENDER_SCALES, label: 'Чёткость картинки' },
    { key: 'viewDistance', kind: 'enum', values: VIEW_DISTANCES, label: 'Дальность отрисовки',
      hint: 'Докуда тянется туман и что попадает в кадр.' },
    { key: 'nightLights', kind: 'enum', values: NIGHT_LIGHT_LEVELS, label: 'Яркость ночных огней',
      hint: 'Фонари, окна, подсветка щитов и пятна фар.' },
    { key: 'timeOfDay', kind: 'enum', values: TIME_OF_DAY_MODES, label: 'Время суток',
      hint: 'Обычно берётся из настроек комнаты. Здесь можно перебить для себя.' }
];

/** Выставить все галочки по пресету — это и есть «быстрая заготовка». */
export function applyGfxPreset(quality) {
    const preset = GFX_PRESETS[quality] || GFX_PRESETS.medium;
    return saveGfxSettings(preset, quality);
}

export const CAMERA_CHASE = 0;
export const CAMERA_COCKPIT = 1;

// --- камера от третьего лица ------------------------------------------------
const CHASE_DIST = 6.4;          // м за кормой на нуле скорости
const CHASE_DIST_SPEED = 1.9;    // добавка на полной скорости
const CHASE_HEIGHT = 2.45;       // м над машиной
const CHASE_HEIGHT_SPEED = 0.45;
const CHASE_LOOK_AHEAD = 7.0;    // м перед машиной, куда смотрит камера
const CHASE_LOOK_UP = 1.15;
const CHASE_POS_LERP = 7.5;      // 1/с, пружина позиции
const CHASE_TARGET_LERP = 11.0;  // 1/с, пружина точки взгляда
const DRIFT_CAM_OFFSET = 0.16;   // м на каждый м/с бокового скольжения
const DRIFT_CAM_MAX = 2.0;

// --- поле зрения ------------------------------------------------------------
const FOV_BASE = 62;
const FOV_SPEED = 16;            // градусов прибавки на полной скорости
const FOV_BOOST = 7;
const FOV_LERP = 4.0;
const FOV_REF_SPEED = 44.0;      // м/с, опорная «полная скорость» для эффектов

// --- тряска -----------------------------------------------------------------
const SHAKE_SPEED = 0.030;
const SHAKE_OFFTRACK = 0.075;
const SHAKE_BOOST = 0.045;
const SHAKE_SPIN = 0.09;

// --- анимация машины --------------------------------------------------------
const WHEEL_STEER_MAX = 0.52;    // рад, максимальный выворот передних колёс
const ROLL_FROM_SLIP = 0.016;    // рад на м/с бокового скольжения
const ROLL_FROM_STEER = 0.052;   // рад на единицу руля при полной скорости
const ROLL_MAX = 0.16;
const PITCH_FROM_ACCEL = 0.0075; // рад на м/с² продольного ускорения
const PITCH_MAX = 0.075;
const BODY_LERP = 9.0;           // 1/с, сглаживание кренов
const TERRAIN_LERP = 12.0;
const SLOPE_SAMPLE = 1.4;        // м вперёд/назад для замера уклона

// --- пятно света фар на асфальте (только ночью) ------------------------------
const BEAM_AHEAD = 8.0;   // м, центр пятна перед машиной
const BEAM_LENGTH = 17.0; // м, длина пятна вдоль курса
const BEAM_WIDTH = 6.0;   // м, ширина пятна

// --- отражение окружения на кузове ------------------------------------------
const ENV_CUBE_SIZE = 128; // грань кубкарты: 0,38 МБ на всю сцену

// --- следы шин ---------------------------------------------------------------
const MARK_MIN_SPEED = 3.0; // м/с, ниже неё след не пишется (стоящая машина)

// ---------------------------------------------------------------------------
// Внешность траффика
// ---------------------------------------------------------------------------
//
// Двенадцать видов: силуэт плюс цвет. Силуэтов два — фургон и маслкар: оба
// читаются как «гражданская машина», а не как гоночная. Палитра намеренно
// приглушённая и намеренно НЕ пересекается с восемью яркими цветами игроков
// (config.COLORS): болванку нельзя спутать с соперником даже боковым зрением.
//
// Номер вида едет в снапшоте в старшей половине байта ident, поэтому видов
// не больше шестнадцати (protocol.TRAFFIC_LOOKS = 12).

const TRAFFIC_STYLES = ['van', 'muscle'];

// [индекс силуэта, цвет]
const TRAFFIC_LOOKS_TABLE = [
    [0, 0xd7dae0],   // белый фургон
    [0, 0x8d949c],   // серый фургон
    [0, 0x6f7a86],   // графитовый фургон
    [0, 0xb9ac8e],   // бежевый фургон
    [0, 0x5d6b62],   // оливковый фургон
    [0, 0x9ca7b4],   // серо-голубой фургон
    [1, 0xc9cdd3],   // серебристый седан
    [1, 0x7a8391],   // мышиный седан
    [1, 0x8c5f52],   // кирпичный седан
    [1, 0x4f5a6b],   // тёмно-синий седан
    [1, 0xa39a6f],   // песочный седан
    [1, 0x66707a]    // асфальтовый седан
];

const CAM_GROUND_CLEARANCE = 0.75; // м, ниже рельефа камера не опускается
const NEAR_PLANE = 0.12;

// ---------------------------------------------------------------------------
// Модульные временные величины: в кадре ничего не создаётся
// ---------------------------------------------------------------------------

/** performance.now() там, где он есть; Date.now() — запасной путь. */
const now =
    typeof performance !== 'undefined' && performance.now
        ? function () { return performance.now(); }
        : function () { return Date.now(); };

// ---------------------------------------------------------------------------
// Состояние одной машины в пуле
// ---------------------------------------------------------------------------

/**
 * Видимое состояние машины. Объект создаётся один раз на слот и дальше только
 * перезаписывается. Поля x, y, z, yaw, speed, vfwd, vlat, steer, flags,
 * driftCharge, halfWidth, halfLength, rearZ читает effects.js.
 */
class CarView {
    constructor(slot) {
        this.slot = slot;
        this.mesh = null;
        this.present = false;   // машина есть в гонке
        this.fresh = false;     // состояние пришло в этом кадре

        this.x = 0; this.y = 0; this.z = 0; this.yaw = 0;
        this.vx = 0; this.vz = 0;
        this.speed = 0; this.vfwd = 0; this.vlat = 0;
        this.steer = 0; this.flags = 0; this.driftCharge = 0;

        this.hint = -1;
        this.wheelSpin = 0;
        this.roll = 0;
        this.bodyPitch = 0;
        this.terrainPitch = 0;
        this.prevVfwd = 0;
        this.accel = 0;

        this.halfWidth = 0.8;
        this.halfLength = 2.0;
        this.rearZ = -1.2;
        this.wheelRadius = 0.34;
        this.shadowSx = 1.2;
        this.shadowSz = 2.2;

        // Якоря накладного свечения в системе координат машины (carmesh.js):
        // плоские массивы x, y, z, размер. Ссылки на чертёж, а не копии —
        // в кадре по ним только читают.
        this.lampHead = null;
        this.lampBrake = null;
        this.lampExhaust = null;
    }
}

// ---------------------------------------------------------------------------
// RaceRenderer
// ---------------------------------------------------------------------------

export class RaceRenderer {
    /**
     * @param {HTMLCanvasElement} canvas
     * @param {object} opts { quality, renderScale, bindKeys }
     */
    constructor(canvas, opts) {
        const o = opts || {};
        this.canvas = canvas;
        this.quality = QUALITY_PRESETS[o.quality] ? o.quality : 'medium';
        this.preset = QUALITY_PRESETS[this.quality];
        // Отдельные галочки графики читаются из localStorage и главнее пресета.
        this.gfx = loadGfxSettings(this.quality);
        this.renderScale = o.renderScale || this.gfx.renderScale;

        // Целевое железо — Intel UHD 24 EU: сглаживание и высокий пиксель-рейт
        // не по карману (раздел 1).
        this.renderer = new THREE.WebGLRenderer({
            canvas: canvas,
            antialias: false,
            powerPreference: 'high-performance',
            alpha: false,
            stencil: false,
            depth: true,
            preserveDrawingBuffer: false
        });
        this.renderer.setClearColor(0x0b0e16, 1);
        this.renderer.autoClear = true;
        this.renderer.shadowMap.enabled = false;
        this.renderer.info.autoReset = true;
        this.applyToneMapping();

        this.scene = new THREE.Scene();
        this.camera = new THREE.PerspectiveCamera(FOV_BASE, 16 / 9, NEAR_PLANE, 1200);
        this.scene.add(this.camera);

        this.width = o.width || canvas.clientWidth || 1280;
        this.height = o.height || canvas.clientHeight || 720;

        // --- свет (один ambient + один directional, теней нет — 10.3) -------
        this.ambient = new THREE.AmbientLight(0xffffff, 0.85);
        this.sun = new THREE.DirectionalLight(0xffffff, 1.0);
        this.sun.castShadow = false;
        this.sun.position.set(60, 120, 40);
        this.sunTarget = new THREE.Object3D();
        this.scene.add(this.ambient, this.sun, this.sunTarget);
        this.sun.target = this.sunTarget;

        this.fog = new THREE.Fog(0xb9c6d7, 120, 360);
        this.scene.fog = this.fog;
        this.scene.background = new THREE.Color(0xb9c6d7);

        // --- состояние гонки ------------------------------------------------
        this.track = null;
        this.trackMeshes = null;
        this.scenery = null;
        this.sampler = null;
        this.effects = null;
        this.raceReady = false;
        this.theme = 'city';

        // Окружение приходит СНАРУЖИ (настройки комнаты), а не из трассы:
        // время суток выбирается в лобби. Здесь лежит последнее принятое
        // значение; галочка `timeOfDay` в настройках графики может его
        // локально перебить.
        this.environment = normalizeEnvironment(null, ENV_DEFAULT);
        this.envExplicit = false;   // приходило ли окружение снаружи хоть раз
        this.timeOfDay = 'day';
        this.weather = 'clear';

        // Отсечение по пирамиде видимости: один объект на рендер, в кадре
        // только перечитывается камера. Владеет им renderer, применяют его
        // trackMeshes.cull() и scenery.cull().
        this.culler = new ViewCuller();
        this.cullDistance = 1e9;
        this.nightK = 0;

        // Высота поверхности для effects.js: ссылка создаётся один раз, чтобы
        // в кадре не появлялось замыкание. Своя подсказка индекса — чтобы
        // запросы эффектов не сбивали локальный поиск машин и камеры.
        this._fxHint = -1;
        const self = this;
        this._heightAt = function (x, z) {
            if (!self.track) return 0;
            const s = self.track.surface(x, z, self._fxHint);
            self._fxHint = s.index;
            const lat = s.lateral;
            const hw = s.halfWidth;
            if (lat > hw) return self.sampler(s.index, lat - hw, 0);
            if (lat < -hw) return self.sampler(s.index, -lat - hw, 1);
            return s.y;
        };

        this.views = [];
        for (let i = 0; i < MAX_CARS; i++) this.views.push(new CarView(i));
        this.localSlot = -1;

        // Кубическая карта окружения для отражения на кузове: снимается один
        // раз при сборке гонки, в кадре стоит ноль.
        this.envCubeRT = null;

        this.shadowMesh = null;
        this.shadowTex = null;
        this.beamMesh = null;
        this.beamTex = null;
        this.boxMesh = null;
        this.boxCount = 0;

        // Траффик: по одному InstancedMesh на силуэт, оба с инстансным цветом.
        this.trafficMeshes = null;
        this.trafficGeoms = null;
        this.trafficMats = null;
        this.trafficCount = 0;      // болванок в последнем снапшоте
        this.trafficLook = new Uint8Array(MAX_TRAFFIC);
        this.trafficX = new Float32Array(MAX_TRAFFIC);
        this.trafficY = new Float32Array(MAX_TRAFFIC);
        this.trafficZ = new Float32Array(MAX_TRAFFIC);
        this.trafficYaw = new Float32Array(MAX_TRAFFIC);

        this.cockpit = null;
        this.cockpitWheel = null;

        // --- камера ----------------------------------------------------------
        this.cameraMode = CAMERA_CHASE;
        this.lookBack = false;
        this.camX = 0; this.camY = 4; this.camZ = 10;
        this.tgtX = 0; this.tgtY = 0; this.tgtZ = 0;
        this.camReady = false;
        this.camHint = -1;
        this.fov = FOV_BASE;
        this.time = 0;

        // --- статистика для оверлея F3 (объект переиспользуется) ------------
        this.stats = {
            fps: 0,
            frameMs: 0,
            drawCalls: 0,
            triangles: 0,
            programs: 0,
            geometries: 0,
            textures: 0,
            particles: 0,
            cars: 0,
            quality: this.quality,
            renderScale: this.renderScale
        };
        this._frameAcc = 0;
        this._frameCount = 0;
        this._lastFrameMs = 0;
        this._fps = 0;
        this._t0 = 0;

        this.applySize();

        // Клавиши C и Shift в input.js не заводятся (12.6 отдаёт Tab и F3
        // модулям интерфейса по той же причине — чтобы не было двойной
        // привязки). Здесь их можно отключить и дёргать методы руками.
        this.bindKeys = o.bindKeys !== false;
        this._onKeyDown = null;
        this._onKeyUp = null;
        if (this.bindKeys) this.attachKeys();
    }

    // -----------------------------------------------------------------------
    // Размер и качество
    // -----------------------------------------------------------------------

    /** Размер вывода в CSS-пикселях. */
    resize(width, height) {
        this.width = Math.max(1, width | 0);
        this.height = Math.max(1, height | 0);
        this.applySize();
    }

    applySize() {
        // Пиксель-рейт ограничен единицей (раздел 1), сверху накладывается
        // настраиваемый render scale 50/75/100 %.
        const dpr = Math.min(typeof devicePixelRatio === 'number' ? devicePixelRatio : 1, 1);
        this.renderer.setPixelRatio(dpr * this.renderScale);
        this.renderer.setSize(this.width, this.height, false);
        this.camera.aspect = this.width / this.height;
        this.camera.updateProjectionMatrix();
        this.stats.renderScale = this.renderScale;
    }

    /** Render scale 0.5 / 0.75 / 1.0 поверх пиксель-рейта. */
    setRenderScale(scale) {
        const s = scale > 1 ? scale / 100 : scale;
        this.renderScale = Math.min(1, Math.max(0.25, s));
        this.applySize();
    }

    /**
     * Применить настройки графики: пресет качества плюс отдельные галочки.
     *
     * Это ЕДИНСТВЕННАЯ точка входа для меню (main.js зовёт её на каждое
     * изменение настроек). Галочки перечитываются из localStorage, и если
     * поменялось что-то, запечённое в геометрию, гонка пересобирается —
     * без перезагрузки страницы.
     *
     * Возвращает true, если пересборка состоялась.
     */
    setQuality(quality) {
        if (QUALITY_PRESETS[quality] && quality !== this.quality) {
            this.quality = quality;
            this.preset = QUALITY_PRESETS[quality];
            this.stats.quality = quality;
        }
        return this.refreshGfx();
    }

    /**
     * Перечитать отдельные настройки графики и применить их.
     * Что можно — применяется на месте (render scale, свечение), остальное
     * требует пересборки сцены: затенение и плотность декора запечены
     * в вершины и в матрицы инстансов.
     */
    refreshGfx() {
        const next = loadGfxSettings(this.quality);
        const prev = this.gfx;
        let rebuild = false;
        for (let i = 0; i < GFX_REBUILD.length; i++) {
            const k = GFX_REBUILD[i];
            if (prev[k] !== next[k]) rebuild = true;
        }
        this.gfx = next;

        if (next.renderScale !== this.renderScale) {
            this.renderScale = next.renderScale;
            this.applySize();
        }
        this.applyToneMapping();
        // свечение включается на живой сцене, без пересборки
        if (this.effects) this.effects.setGlowEnabled(next.glow);
        // дальность отрисовки — тоже без пересборки: она правит только
        // туман, дальнюю плоскость камеры и предел отсечения
        if (this.scenery) this.applyViewDistance();

        if (rebuild && this.raceReady) {
            const track = this.trackSource;
            const players = this.playerSource;
            const local = this.localSlot;
            this.disposeRace();
            this.createRace(track, players, { localSlot: local });
            return true;
        }
        return false;
    }

    /**
     * Окружение гонки: время суток и (задел) погода. Приходит из настроек
     * комнаты, а не из описания трассы.
     *
     * Принимает `{timeOfDay|time_of_day, weather}` или просто строку
     * `'night'`. Если сцена уже собрана, она пересобирается: небо, свет,
     * туман и ночные огни запечены в геометрию.
     *
     * @returns {boolean} состоялась ли пересборка
     */
    setEnvironment(env) {
        const next = normalizeEnvironment(env, this.environment);
        if (next.timeOfDay === this.environment.timeOfDay
            && next.weather === this.environment.weather) return false;
        this.environment = next;
        if (!this.raceReady) return false;
        const track = this.trackSource;
        const players = this.playerSource;
        const local = this.localSlot;
        this.disposeRace();
        this.createRace(track, players, { localSlot: local });
        return true;
    }

    /** Время суток, которым реально собирается сцена (галочка главнее комнаты). */
    resolveTimeOfDay() {
        const forced = this.gfx.timeOfDay;
        if (forced && forced !== 'auto') return forced;
        return this.environment.timeOfDay;
    }

    /**
     * Погода, которой реально собирается сцена. Галочка «мокрый асфальт»
     * главнее всего: снятая, она возвращает сухую картинку целиком, чтобы
     * выключенная тема не стоила ни пикселя разницы.
     */
    resolveWeather() {
        if (!this.gfx.wetRoad) return 'clear';
        const forced = this.gfx.weather;
        if (forced && forced !== 'auto') return forced;
        return this.environment.weather;
    }

    /**
     * Тональная коррекция. Ноль вызовов, ноль треугольников, ноль фрагментов —
     * несколько ALU на пиксель. Ставится до сборки сцены: смена на живой сцене
     * пересобрала бы все шейдерные программы разом, а в Firefox компиляция
     * синхронна.
     */
    applyToneMapping() {
        const on = this.gfx.toneMap;
        this.renderer.toneMapping = on ? THREE.ACESFilmicToneMapping : THREE.NoToneMapping;
        this.renderer.toneMappingExposure = on ? TONE_EXPOSURE : 1.0;
    }

    /**
     * Дальность тумана, дальняя плоскость камеры и предел отсечения.
     * Всё три — одно и то же число: отсечение режет ровно там, где туман уже
     * подменил цвет объекта своим, поэтому картинка не меняется.
     */
    applyViewDistance() {
        const sc = this.scenery;
        if (!sc) return;
        const k = VIEW_DISTANCE_K[this.gfx.viewDistance] || 1;
        const far = sc.fog.far * k;
        this.fog.near = far * 0.35;
        this.fog.far = far;
        // запас в 12 м: на самой границе тумана объект ещё виден на пиксель
        this.cullDistance = far + 12;
        this.camera.far = far + 700;
        this.camera.updateProjectionMatrix();
    }

    // -----------------------------------------------------------------------
    // Сборка гонки
    // -----------------------------------------------------------------------

    /**
     * Собрать сцену гонки.
     * @param {object} track   экземпляр Track из js/track.js либо объект
     *                         race_init.track формата 12.1
     * @param {Array}  players [{slot, color, car|shape}] — расстановка из race_init
     * @param {object} opts    { localSlot, env }
     *   env — окружение из настроек комнаты: {timeOfDay, weather}. Если не
     *   задано, берётся ранее принятое setEnvironment(), а на первой гонке —
     *   умолчание описания трассы.
     */
    createRace(track, players, opts) {
        const o = opts || {};
        this.disposeRace();

        this.trackSource = track;
        this.playerSource = players;

        // Track нужен для запросов к поверхности; сырые данные принимаем тоже.
        this.track = track && typeof track.surface === 'function' ? track : new Track(track);
        this.theme = this.track.theme || 'city';
        const quality = this.quality;

        // Окружение: явно переданное > ранее принятое > умолчание трассы.
        if (o.env !== undefined && o.env !== null) {
            this.environment = normalizeEnvironment(o.env, trackDefaultEnvironment(track));
        } else if (!this.envExplicit) {
            this.environment = trackDefaultEnvironment(track);
        }
        if (o.env !== undefined && o.env !== null) this.envExplicit = true;
        const todName = this.resolveTimeOfDay();
        this.timeOfDay = todName;
        this.nightK = todName === 'night' ? 1 : todName === 'dusk' ? 0.5 : 0;

        // 12.6: buildTrackMeshes и buildScenery ОБЯЗАНЫ получить один и тот же
        // quality, иначе декор всплывёт над землёй.
        const gfx = this.gfx;
        const nightLights = NIGHT_LIGHT_K[gfx.nightLights] === undefined ? 1 : NIGHT_LIGHT_K[gfx.nightLights];
        const weather = this.resolveWeather();
        this.weather = weather;
        this.applyToneMapping();

        // Окружение строится ПЕРВЫМ: из него берутся параметры света, а их
        // запекание в вершины полотна — и есть тема «запечённый свет».
        this.scenery = buildScenery(this.track, this.theme, this.track.decorSeed, quality, {
            ao: gfx.ao,
            decor: gfx.decor,
            anim: gfx.decorAnim,
            env: { timeOfDay: todName, weather: weather },
            nightLights: nightLights
        });
        this.trackMeshes = buildTrackMeshes(this.track, this.theme, quality, {
            ao: gfx.ao,
            timeOfDay: todName,
            weather: weather,
            wet: gfx.wetRoad,
            marks: gfx.tireMarks,
            // Запекается ТОЛЬКО неинстансная статика. Инстансный декор
            // остаётся Ламбертом: поворот экземпляра вокруг Y в нормаль
            // геометрии не входит, и запекание увело бы 14 % пикселей.
            light: gfx.bakedLight ? this.scenery.light : null,
            sky: this.scenery.fog.color
        });
        this.sampler = createTerrainSampler(this.track, this.theme, quality);

        this.scene.add(this.trackMeshes.group);
        this.scene.add(this.scenery.group);

        // туман, фон и свет — как отдаёт buildScenery (12.6)
        const sc = this.scenery;
        this.fog.color.copy(sc.fog.color);
        this.scene.background = sc.background;
        this.applyViewDistance();

        this.ambient.color.copy(sc.light.ambient.color);
        this.ambient.intensity = sc.light.ambient.intensity;
        this.sun.color.copy(sc.light.directional.color);
        this.sun.intensity = sc.light.directional.intensity;
        const d = sc.light.directional.direction;
        const dl = Math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]) || 1;
        this.sun.position.set((d[0] / dl) * 220, (d[1] / dl) * 220, (d[2] / dl) * 220);
        this.sunTarget.position.set(0, 0, 0);

        // Кубкарта снимается, пока в сцене только статика: машин, теней,
        // боксов и эффектов в отражении быть не должно. Заодно это прогрев
        // шейдеров — шесть сторон видят то, чего первый кадр не видит,
        // а Firefox компилирует синхронно.
        if (gfx.carReflect) this.bakeEnvMap();

        this.buildShadows();
        this.buildBeams(nightLights);
        this.buildBoxes();
        this.buildTraffic();

        // --- эффекты ---------------------------------------------------------
        this.effects = new Effects({
            quality: quality,
            particles: gfx.particles,
            glow: gfx.glow,
            fogColor: sc.fog.color,
            heightAt: this._heightAt,
            night: this.nightK * nightLights,
            // брызги из-под колёс — тот же пул дыма, другой цвет и другое
            // условие рождения: ноль дополнительных вызовов отрисовки
            wet: weather === 'wet'
        });
        this.scene.add(this.effects.root);
        if (this.boxMesh) {
            this.effects.attachBoxes(this.boxMesh, this.boxX, this.boxY, this.boxZ, this.boxCount);
        }

        this.buildCars(players);
        this.setLocalSlot(o.localSlot === undefined ? this.localSlot : o.localSlot);
        this.buildCockpit();

        this.camReady = false;
        this.raceReady = true;
        return this;
    }

    /** Слот своей машины: за ней идёт камера, её скорость даёт полосы скорости. */
    setLocalSlot(slot) {
        this.localSlot = slot === undefined || slot === null ? -1 : slot;
    }

    /** Машины из списка игроков race_init. */
    buildCars(players) {
        if (!players) return;
        const catalog = getCatalog();
        const nl = NIGHT_LIGHT_K[this.gfx.nightLights] === undefined ? 1 : NIGHT_LIGHT_K[this.gfx.nightLights];
        const night = this.nightK * nl;
        for (let i = 0; i < players.length; i++) {
            const p = players[i];
            if (!p) continue;
            const slot = p.slot === undefined ? i : p.slot;
            if (slot < 0 || slot >= MAX_CARS) continue;

            let shape = p.shape || null;
            if (!shape && p.car) {
                if (typeof p.car === 'string') {
                    const spec = catalog.resolve(p.car);
                    shape = spec ? spec.shape : null;
                } else {
                    shape = p.car.shape || p.car;
                }
            }

            const view = this.views[slot];
            const mesh = buildCarMesh(shape, p.color || '#e5484d', this.quality, {
                ao: this.gfx.ao,
                envMap: this.envCubeRT ? this.envCubeRT.texture : null,
                // ночью в отражении появляются окна домов — самое красивое,
                // что даёт тема; 0,70 уже вымывает краску
                reflect: REFLECT_K[this.timeOfDay] === undefined ? 0.4 : REFLECT_K[this.timeOfDay]
            });
            if (mesh.setNight) mesh.setNight(night);
            // Колёса крутятся вокруг своей оси УЖЕ ПОВЁРНУТОЙ рулём, поэтому
            // порядок Эйлера обязан быть YXZ: при XYZ спин ушёл бы вокруг оси
            // кузова и колесо «виляло» бы вместо вращения.
            for (let w = 0; w < 4; w++) mesh.wheels[w].rotation.order = 'YXZ';
            mesh.root.rotation.order = 'YXZ';
            mesh.root.matrixAutoUpdate = true;
            mesh.root.visible = false;
            this.scene.add(mesh.root);

            view.mesh = mesh;
            view.present = true;
            view.fresh = false;
            view.halfWidth = mesh.shape.track_width * 0.5;
            view.halfLength = mesh.shape.length * 0.5;
            view.rearZ = -mesh.shape.wheelbase * 0.5 - 0.1;
            view.wheelRadius = mesh.shape.wheel_radius;
            // Пятно тени чуть ШИРЕ габарита: иначе оно целиком скрывается под
            // кузовом и машина выглядит висящей в воздухе.
            view.shadowSx = mesh.shape.width * 1.8;
            view.shadowSz = mesh.shape.length * 1.3;
            view.hint = -1;
            view.wheelSpin = 0;
            view.roll = 0;
            view.bodyPitch = 0;
            view.terrainPitch = 0;
            const lamps = mesh.lamps;
            view.lampHead = lamps ? lamps.head : null;
            view.lampBrake = lamps ? lamps.brake : null;
            view.lampExhaust = lamps ? lamps.exhaust : null;
        }
        this.stats.cars = this.countCars();
    }

    countCars() {
        let n = 0;
        for (let i = 0; i < MAX_CARS; i++) if (this.views[i].present) n++;
        return n;
    }

    /**
     * Снять кубическую карту окружения для отражения на кузове.
     *
     * Цена разовая: шесть отрисовок статики в грани 128² (0,38 МБ), то есть
     * доли секунды при загрузке трассы — соизмеримо с самой сборкой геометрии.
     * В кадре это ноль вызовов, ноль треугольников и ноль лишних фрагментов:
     * одна кубическая выборка в пикселях кузова.
     *
     * Карта снимается из ОДНОЙ точки — центра трассы. Для мультяшного стиля
     * этого достаточно; «правильное» отражение потребовало бы карты на каждую
     * четверть круга (4 x 0,38 МБ) и переключения по прогрессу.
     */
    bakeEnvMap() {
        const T = this.track;
        let cx = 0,
            cz = 0,
            maxY = -1e9;
        for (let i = 0; i < T.count; i++) {
            cx += T.cx[i];
            cz += T.cz[i];
            if (T.cy[i] > maxY) maxY = T.cy[i];
        }
        cx /= T.count;
        cz /= T.count;

        const rt = new THREE.WebGLCubeRenderTarget(ENV_CUBE_SIZE, {
            generateMipmaps: false,
            minFilter: THREE.LinearFilter,
            magFilter: THREE.LinearFilter
        });
        rt.texture.name = 'carEnvCube';
        const cam = new THREE.CubeCamera(1.0, 1500, rt);
        cam.position.set(cx, maxY + 6, cz);

        // Посегментное отсечение настроено под главную камеру — на время
        // съёмки показываем всё кольцо, иначе в отражении будут дыры.
        this.trackMeshes.cull(null);
        this.scenery.cull(null);
        this.scenery.updateSky(cam);
        // тональная коррекция применится уже к самому кадру: если снимать
        // карту через неё, отражение окажется скорректированным дважды
        const tone = this.renderer.toneMapping;
        this.renderer.toneMapping = THREE.NoToneMapping;
        cam.update(this.renderer, this.scene);
        this.renderer.toneMapping = tone;
        this.scenery.updateSky(this.camera);
        this.envCubeRT = rt;
    }

    /**
     * Тени машин: ОДИН InstancedMesh на все восемь (12.6). Мягкое тёмное пятно
     * на сгенерированной в canvas текстуре, прижатое к поверхности. Никаких
     * карт теней — их запрещает раздел 1.
     */
    buildShadows() {
        const geom = new THREE.PlaneGeometry(1, 1);
        geom.rotateX(-Math.PI * 0.5); // в плоскость XZ, лицом вверх
        this.shadowTex = createRadialTexture(64, 0.6, 0.94);
        const mat = new THREE.MeshBasicMaterial({
            map: this.shadowTex,
            color: 0x06080c,
            transparent: true,
            opacity: this.preset.shadowOpacity,
            depthWrite: false,
            fog: true
        });
        mat.name = 'carShadow';
        const mesh = new THREE.InstancedMesh(geom, mat, MAX_CARS);
        mesh.name = 'carShadows';
        mesh.frustumCulled = false;
        mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        mesh.renderOrder = 3;
        mesh.count = 0;
        mesh.visible = false;
        this.shadowMesh = mesh;
        this.scene.add(mesh);
    }

    /**
     * Пятна света фар на асфальте: ОДИН InstancedMesh на все восемь машин.
     *
     * Ночью фары обязаны не просто светиться сами, а освещать дорогу впереди.
     * Настоящего источника света здесь быть не может (раздел 1: один ambient
     * и один directional, никаких теней), поэтому пятно рисуется вытянутым
     * аддитивным билбордом, лежащим на поверхности перед машиной. Днём меш
     * не строится вовсе — ни вызова, ни треугольника.
     */
    buildBeams(nightLights) {
        if (this.nightK <= 0 || nightLights <= 0) return;
        const geom = new THREE.PlaneGeometry(1, 1);
        geom.rotateX(-Math.PI * 0.5);
        this.beamTex = createRadialTexture(64, 0.08, 0.8);
        const mat = new THREE.MeshBasicMaterial({
            map: this.beamTex,
            color: 0xfff0cc,
            transparent: true,
            blending: THREE.AdditiveBlending,
            opacity: 0.62 * this.nightK * Math.min(1.45, nightLights),
            depthWrite: false,
            fog: true
        });
        mat.name = 'carBeam';
        const mesh = new THREE.InstancedMesh(geom, mat, MAX_CARS);
        mesh.name = 'carBeams';
        mesh.frustumCulled = false;
        mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        mesh.renderOrder = 4;
        mesh.count = 0;
        mesh.visible = false;
        this.beamMesh = mesh;
        this.scene.add(mesh);
    }

    /**
     * Боксы с бонусами: ОДИН InstancedMesh на все боксы трассы (12.6).
     * Вращение, покачивание и подсветку делает effects.js.
     */
    buildBoxes() {
        const boxes = this.track.itemBoxes;
        const n = boxes ? boxes.length : 0;
        this.boxCount = n;
        if (n === 0) return;

        this.boxX = new Float32Array(n);
        this.boxY = new Float32Array(n);
        this.boxZ = new Float32Array(n);
        let hint = -1;
        for (let i = 0; i < n; i++) {
            const b = boxes[i];
            this.boxX[i] = b.x;
            this.boxZ[i] = b.z;
            const s = this.track.surface(b.x, b.z, hint);
            hint = s.index;
            this.boxY[i] = s.y;
        }

        // восьмигранник с градиентом: читается как аркадный бонус-куб
        const geom = solidify(new THREE.OctahedronGeometry(0.5, 0), toColor('#ffd24a'),
            function (x, y, z, i, c) {
                // нижние грани иначе уходят в тень и бокс читается как камень
                const t = y > 0 ? 1 : 0;
                c.setRGB(1.0, 0.88 + t * 0.1, 0.42 + t * 0.36);
            });
        geom.scale(1, 1.22, 1);
        const mat = new THREE.MeshLambertMaterial({
            vertexColors: true,
            flatShading: true,
            emissive: new THREE.Color('#ffa010'),
            emissiveIntensity: 0.8
        });
        mat.name = 'itemBox';
        const mesh = new THREE.InstancedMesh(geom, mat, n);
        mesh.name = 'itemBoxes';
        mesh.frustumCulled = false;
        mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        mesh.count = 0;
        mesh.visible = false;
        this.boxMesh = mesh;
        this.scene.add(mesh);
    }

    /**
     * Траффик: на каждый силуэт ОДИН InstancedMesh со слитой геометрией.
     *
     * Модель берётся та же, что у гонщиков (``buildCarMesh`` из carmesh.js),
     * и тут же разбирается на атрибуты: кузов, четыре колеса на своих местах,
     * фары и стопы сливаются в одну геометрию с вершинными цветами, а сам
     * временный меш выбрасывается. Личный цвет болванки задаётся инстансным
     * цветом — он умножается на вершинный (12.11: для этого у материала
     * обязан быть vertexColors, а у геометрии атрибут color; оба есть).
     *
     * Кузов строится БЕЛЫМ. Тогда в атрибуте color у красящихся вершин лежит
     * чистый коэффициент затенения панели, и умножение на инстансный цвет
     * даёт ровно то же, что даёт перекраска гонщика, — только бесплатно.
     *
     * Палитра намеренно другая: приглушённые «гражданские» цвета против
     * восьми ярких из config.COLORS. Спутать болванку с гонщиком нельзя ни
     * по цвету, ни по силуэту, ни по скорости.
     *
     * Качество модели всегда ``low`` независимо от пресета: болванок до
     * двенадцати, и тратить на фон высокую детализацию колёс и зеркала
     * незачем — это прямая экономия треугольников.
     */
    buildTraffic() {
        const catalog = getCatalog();
        if (!catalog) return;
        const meshes = [];
        const geoms = [];
        const mats = [];
        for (let s = 0; s < TRAFFIC_STYLES.length; s++) {
            const spec = catalog.resolve(TRAFFIC_STYLES[s]);
            const shape = spec ? spec.shape : null;
            if (!shape) continue;
            const geom = buildTrafficGeometry(shape, this.gfx.ao);
            if (!geom) continue;
            const mat = new THREE.MeshLambertMaterial({
                vertexColors: true,
                flatShading: true
            });
            mat.name = 'trafficBody' + s;
            const mesh = new THREE.InstancedMesh(geom, mat, MAX_TRAFFIC);
            mesh.name = 'traffic' + s;
            mesh.frustumCulled = false;
            mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
            mesh.instanceColor = new THREE.InstancedBufferAttribute(
                new Float32Array(MAX_TRAFFIC * 3).fill(1), 3);
            mesh.instanceColor.setUsage(THREE.DynamicDrawUsage);
            mesh.count = 0;
            mesh.visible = false;
            this.scene.add(mesh);
            meshes.push(mesh);
            geoms.push(geom);
            mats.push(mat);
        }
        if (!meshes.length) return;
        this.trafficMeshes = meshes;
        this.trafficGeoms = geoms;
        this.trafficMats = mats;
    }

    /**
     * Кокпит: панель и руль перед глазами. Два меша, и оба рисуются только
     * в виде из кабины. Узлы висят на камере, поэтому едут вместе с ней без
     * единого пересчёта в кадре.
     */
    buildCockpit() {
        const detail = this.preset.cockpitDetail;
        const root = new THREE.Group();
        root.name = 'cockpit';
        root.visible = false;

        // --- панель ---------------------------------------------------------
        const dash = new THREE.Group();
        const panelGeom = buildDashGeometry(detail);
        const panelMat = new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true });
        panelMat.name = 'cockpitDash';
        const panel = new THREE.Mesh(panelGeom, panelMat);
        panel.name = 'cockpitDash';
        panel.frustumCulled = false;
        dash.add(panel);
        root.add(dash);

        // --- руль -----------------------------------------------------------
        const wheelGeom = buildSteeringWheelGeometry(detail);
        const wheelMat = new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true });
        wheelMat.name = 'cockpitWheel';
        const wheel = new THREE.Mesh(wheelGeom, wheelMat);
        wheel.name = 'cockpitWheel';
        wheel.frustumCulled = false;
        wheel.position.set(0, -0.5, -0.84);
        wheel.rotation.x = -0.36;
        root.add(wheel);

        this.camera.add(root);
        this.cockpit = root;
        this.cockpitWheel = wheel;
    }

    // -----------------------------------------------------------------------
    // Приём состояния
    // -----------------------------------------------------------------------

    /** Начало кадра: машины, которым не пришло состояние, спрячутся. */
    beginFrame() {
        for (let i = 0; i < MAX_CARS; i++) this.views[i].fresh = false;
    }

    /**
     * Состояние одной машины на этот кадр (уже интерполированное в net.js).
     * @param {number} slot 0..7
     */
    setCarState(slot, x, z, yaw, vx, vz, steer, flags, driftCharge) {
        if (slot < 0 || slot >= MAX_CARS) return;
        const v = this.views[slot];
        if (!v.present) return;
        v.x = x;
        v.z = z;
        v.yaw = yaw;
        v.vx = vx;
        v.vz = vz;
        v.steer = steer;
        v.flags = flags;
        v.driftCharge = driftCharge;
        v.fresh = true;
    }

    /**
     * Снаряды и маска боксов прямо из буфера снапшота (12.3).
     * Машины сюда НЕ берутся: их состояние приходит интерполированным
     * из net.js через setCarState.
     */
    applySnapshot(snap) {
        if (!this.effects) return;
        this.effects.setProjectiles(snap);
        this.effects.setBoxMask(snap);
        this.takeTraffic(snap);
        this.effects.setRoadEvents(snap);
    }

    /**
     * Болванки из буфера снапшота. Значения там уже мировые и уже на момент
     * показа: интерполяцию по дуге сделал net.js (см. его _trafficPair).
     */
    takeTraffic(snap) {
        let n = snap.trafViewCount || 0;
        if (n > MAX_TRAFFIC) n = MAX_TRAFFIC;
        for (let i = 0; i < n; i++) {
            this.trafficLook[i] = snap.trafViewLook[i];
            this.trafficX[i] = snap.trafViewX[i];
            this.trafficY[i] = snap.trafViewY[i];
            this.trafficZ[i] = snap.trafViewZ[i];
            this.trafficYaw[i] = snap.trafViewYaw[i];
        }
        this.trafficCount = n;
    }



    /**
     * Запасной путь: взять машины прямо из снапшота без интерполяции.
     * Нужен стендам и записи повторов; в игре машины идут через setCarState.
     */
    applySnapshotCars(snap) {
        if (!snap || !snap.valid) return;
        for (let i = 0; i < snap.carCount; i++) {
            this.setCarState(
                snap.carSlot[i], snap.carX[i], snap.carZ[i], snap.carYaw[i],
                snap.carVx[i], snap.carVz[i], snap.carSteer[i],
                snap.carFlags[i], snap.carDriftCharge[i]
            );
        }
    }

    /** Взрыв в мировой точке: попадание ракеты, подрыв мины. */
    explosionAt(x, z, power) {
        if (!this.effects) return;
        this.effects.explosion(x, this._heightAt(x, z), z, power);
        this.sootAt(x, z, power);
    }

    /**
     * Круг копоти в карту полотна. Карта уже натянута ради следов шин,
     * поэтому любая новая декаль в неё стоит ноль вызовов и ноль треугольников:
     * это самый выгодный рычаг из всего списка, и открывается он один раз.
     */
    sootAt(x, z, power) {
        const marks = this.trackMeshes && this.trackMeshes.marks;
        if (!marks) return;
        const s = this.track.surface(x, z, this._fxHint);
        this._fxHint = s.index;
        const p = power === undefined ? 1 : power;
        marks.blob(this.track.cs[s.index] + (x - this.track.cx[s.index]) * this.track.ctx[s.index]
            + (z - this.track.cz[s.index]) * this.track.ctz[s.index],
            s.lateral, s.halfWidth, 0.42, 1.6 + 0.8 * p);
    }

    /** Попадание по машине: взрыв в её точке. */
    hitCar(slot, power) {
        if (slot < 0 || slot >= MAX_CARS || !this.effects) return;
        const v = this.views[slot];
        this.effects.explosion(v.x, v.y, v.z, power);
        this.sootAt(v.x, v.z, power);
    }

    /** Щит погасил попадание. */
    shieldBlock(slot) {
        if (slot < 0 || slot >= MAX_CARS || !this.effects) return;
        const v = this.views[slot];
        this.effects.flashShield(slot, v.x, v.y, v.z);
    }

    /** Сменить цвет кузова (лобби, перекраска по ходу). */
    setCarColor(slot, color) {
        if (slot < 0 || slot >= MAX_CARS) return;
        const v = this.views[slot];
        if (v.mesh) v.mesh.setBodyColor(color);
    }

    // -----------------------------------------------------------------------
    // Кадр
    // -----------------------------------------------------------------------

    /** Анимация, камера, эффекты и отрисовка одним вызовом. */
    frame(dt) {
        this.update(dt);
        this.render();
    }

    update(dt) {
        if (!this.raceReady) return;
        const step = dt > 0.1 ? 0.1 : dt < 0 ? 0 : dt;
        this.time += step;

        const marks = this.trackMeshes.marks;
        const shadowArr = this.shadowMesh.instanceMatrix.array;
        let shadowN = 0;
        const beamArr = this.beamMesh ? this.beamMesh.instanceMatrix.array : null;
        let beamN = 0;

        this.effects.beginFrame();
        // В виде из кокпита своя машина скрыта (12.11) — вместе с ней
        // обязано исчезнуть и её накладное свечение.
        this.effects.setHiddenSlot(this.cameraMode === CAMERA_COCKPIT ? this.localSlot : -1);

        for (let i = 0; i < MAX_CARS; i++) {
            const v = this.views[i];
            if (!v.present) continue;
            if (!v.fresh) {
                if (v.mesh.root.visible) v.mesh.root.visible = false;
                continue;
            }
            this.updateCarView(v, step);
            v.mesh.root.visible = true;

            // тень: плоское пятно по курсу машины, прижатое к поверхности
            writeScaleYaw(shadowArr, shadowN * 16,
                v.x, v.y + 0.045, v.z, v.yaw,
                v.shadowSx, 1, v.shadowSz);
            shadowN++;

            // пятно фар: лежит перед машиной, тянется по её курсу
            if (beamArr && (v.flags & FLAG_GHOST) === 0) {
                const bx = v.x + Math.sin(v.yaw) * BEAM_AHEAD;
                const bz = v.z + Math.cos(v.yaw) * BEAM_AHEAD;
                writeScaleYaw(beamArr, beamN * 16,
                    bx, this.heightAt(bx, bz, v) + 0.055, bz, v.yaw,
                    BEAM_WIDTH, 1, BEAM_LENGTH);
                beamN++;
            }

            if (marks) this.stampMarks(marks, v);

            this.effects.emitFromCar(v, step);
        }

        if (marks) {
            // Выцветание идёт полосами, догрузка — прямоугольниками: карта
            // конечная, и без выцветания за сессию полотно станет чёрным.
            marks.fade(step);
            marks.upload(this.renderer);
        }

        // --- траффик и перевёрнутые машины ----------------------------------
        // Всё это идёт в те же один-два InstancedMesh, поэтому цена
        // постоянная: два вызова отрисовки на любое число болванок.
        shadowN = this.writeTraffic(shadowArr, shadowN);

        this.shadowMesh.count = shadowN;
        this.shadowMesh.visible = shadowN > 0;
        if (shadowN > 0) this.shadowMesh.instanceMatrix.needsUpdate = true;
        if (this.beamMesh) {
            this.beamMesh.count = beamN;
            this.beamMesh.visible = beamN > 0;
            if (beamN > 0) this.beamMesh.instanceMatrix.needsUpdate = true;
        }

        // камера
        const local = this.localSlot >= 0 ? this.views[this.localSlot] : null;
        const target = local && local.present && local.fresh ? local : this.firstVisibleCar();
        if (target) this.updateCamera(step, target);
        this.camera.updateMatrixWorld(true);

        // купол неба держится над камерой (12.6)
        this.scenery.updateSky(this.camera);
        // время вершинной анимации декора: одна запись числа на всю сцену
        this.scenery.updateAnim(this.time);

        // Отсечение по пирамиде видимости: сначала перечитываем камеру,
        // потом каждый владелец геометрии сам решает, что показать.
        // Порядок важен — камера к этому моменту уже посчитала матрицы.
        this.culler.update(this.camera, this.cullDistance);
        this.trackMeshes.cull(this.culler);
        this.scenery.cull(this.culler);

        this.effects.update(step, this.camera, target ? target.speed : 0);
    }

    /**
     * Матрицы болванок и перевёрнутых машин в инстансные меши траффика.
     *
     * Возвращает новое число теней: болванка отбрасывает такую же тень,
     * как гонщик, и идёт в тот же InstancedMesh — лишних вызовов ноль.
     */
    writeTraffic(shadowArr, shadowN) {
        const meshes = this.trafficMeshes;
        if (!meshes) return shadowN;
        const counts = _trafficCounts;
        for (let m = 0; m < meshes.length; m++) counts[m] = 0;

        const n = this.trafficCount;
        for (let i = 0; i < n; i++) {
            const look = TRAFFIC_LOOKS_TABLE[this.trafficLook[i] % TRAFFIC_LOOKS_TABLE.length];
            const which = look[0] < meshes.length ? look[0] : 0;
            const mesh = meshes[which];
            const slot = counts[which];
            if (slot >= MAX_TRAFFIC) continue;
            const x = this.trafficX[i];
            const y = this.trafficY[i];
            const z = this.trafficZ[i];
            const yaw = this.trafficYaw[i];
            writeScaleYaw(mesh.instanceMatrix.array, slot * 16, x, y, z, yaw, 1, 1, 1);
            writeInstanceColor(mesh.instanceColor.array, slot * 3, look[1]);
            counts[which] = slot + 1;
            if (shadowN < MAX_CARS + MAX_TRAFFIC) {
                writeScaleYaw(shadowArr, shadowN * 16, x, y + 0.045, z, yaw,
                    TRAFFIC_SHADOW_W, 1, TRAFFIC_SHADOW_L);
                shadowN++;
            }
            // Янтарный проблесковый ореол на крыше: он и есть главный знак
            // «это не гонщик». Идёт в общий меш свечения — ноль вызовов.
            if (this.effects) {
                this.effects.lamp(x, y + TRAFFIC_BEACON_Y, z, TRAFFIC_BEACON_SIZE,
                    1.0, 0.62, 0.16);
            }
        }

        // Перевёрнутая машина как происшествие: та же геометрия болванки,
        // поставленная на крышу. Отдельной модели под это заводить незачем —
        // и вызовов отрисовки она бы стоила отдельных.
        const wrecks = this.effects ? this.effects.wreckCount : 0;
        for (let w = 0; w < wrecks; w++) {
            const mesh = meshes[0];
            const slot = counts[0];
            if (slot >= MAX_TRAFFIC) break;
            const x = this.effects.wreckX[w];
            const y = this.effects.wreckY[w];
            const z = this.effects.wreckZ[w];
            const yaw = this.effects.wreckYaw[w];
            writeUpsideDown(mesh.instanceMatrix.array, slot * 16, x, y, z, yaw);
            writeInstanceColor(mesh.instanceColor.array, slot * 3, WRECK_COLOR);
            counts[0] = slot + 1;
            if (shadowN < MAX_CARS + MAX_TRAFFIC) {
                writeScaleYaw(shadowArr, shadowN * 16, x, y + 0.04, z, yaw,
                    TRAFFIC_SHADOW_W, 1, TRAFFIC_SHADOW_L);
                shadowN++;
            }
        }

        for (let m = 0; m < meshes.length; m++) {
            const mesh = meshes[m];
            mesh.count = counts[m];
            mesh.visible = counts[m] > 0;
            if (counts[m] > 0) {
                mesh.instanceMatrix.needsUpdate = true;
                mesh.instanceColor.needsUpdate = true;
            }
        }
        return shadowN;
    }

    /**
     * Отпечатать след задних колёс в карту полотна.
     *
     * Ноль вызовов отрисовки, ноль треугольников, ноль фрагментов: след — это
     * несколько байт, дописанных в текстуру, которую полотно и так читает.
     * Вариант с растущей лентой квадов стоил бы 96 тыс. треугольников за гонку
     * (весь бюджет кадра), а декали — столько же.
     *
     * Точка контакта колеса не ищется отдельным запросом к трассе: смещение
     * колеса от центра машины раскладывается по касательной и нормали осевой
     * линии. На двух метрах выборки кривизна ничего не меняет, а поиск
     * ближайшей точки экономится шестнадцать раз за кадр.
     */
    stampMarks(marks, v) {
        const slot = v.slot;
        const b0 = slot * 2;
        const drifting = (v.flags & FLAG_DRIFTING) !== 0;
        const braking = (v.flags & FLAG_BRAKING) !== 0 && v.vfwd > MARK_MIN_SPEED;
        const dust = (v.flags & FLAG_OFFTRACK) !== 0;
        if ((v.flags & FLAG_GHOST) || v.speed < MARK_MIN_SPEED || (!drifting && !braking && !dust)) {
            marks.release(b0);
            marks.release(b0 + 1);
            return;
        }

        const T = this.track;
        const s = T.surface(v.x, v.z, v.hint);
        v.hint = s.index;
        const i = s.index;
        const tx = T.ctx[i],
            tz = T.ctz[i];
        const nx = T.cnx[i],
            nz = T.cnz[i];
        // путь до ближайшей выборки плюс продольная поправка: без неё след
        // прыгал бы по двухметровой сетке выборок
        const arc = T.cs[i] + (v.x - T.cx[i]) * tx + (v.z - T.cz[i]) * tz;

        let strength;
        if (drifting) {
            const slip = Math.abs(v.vlat);
            strength = 0.18 + 0.42 * Math.min(1, slip / 9);
        } else if (braking) {
            strength = 0.26;
        } else {
            strength = 0.13; // пыль и примятая трава вне трассы
        }

        const sinY = Math.sin(v.yaw);
        const cosY = Math.cos(v.yaw);
        const fx = sinY, fz = cosY;
        const lx = cosY, lz = -sinY;
        for (let k = 0; k < 2; k++) {
            const side = k === 0 ? 1 : -1;
            const ox = fx * v.rearZ + lx * v.halfWidth * side;
            const oz = fz * v.rearZ + lz * v.halfWidth * side;
            marks.stroke(b0 + k,
                arc + ox * tx + oz * tz,
                s.lateral + ox * nx + oz * nz,
                s.halfWidth, strength, 1);
        }
    }

    firstVisibleCar() {
        for (let i = 0; i < MAX_CARS; i++) {
            const v = this.views[i];
            if (v.present && v.fresh) return v;
        }
        return null;
    }

    /** Анимация одной машины: колёса, крены, посадка на рельеф, стопы. */
    updateCarView(v, dt) {
        const mesh = v.mesh;
        const sinY = Math.sin(v.yaw);
        const cosY = Math.cos(v.yaw);
        // forward = (sin yaw, cos yaw), lateral = (cos yaw, -sin yaw) — раздел 4
        const fx = sinY, fz = cosY;
        const lx = cosY, lz = -sinY;

        const vfwd = v.vx * fx + v.vz * fz;
        const vlat = v.vx * lx + v.vz * lz;
        v.vfwd = vfwd;
        v.vlat = vlat;
        v.speed = Math.sqrt(v.vx * v.vx + v.vz * v.vz);

        // продольное ускорение — для клевка кузова
        const rawAccel = dt > 0 ? (vfwd - v.prevVfwd) / dt : 0;
        v.prevVfwd = vfwd;
        v.accel += (rawAccel - v.accel) * Math.min(1, dt * 10);

        // --- посадка на поверхность -----------------------------------------
        v.y = this.heightAt(v.x, v.z, v);
        const yFront = this.heightAt(v.x + fx * SLOPE_SAMPLE, v.z + fz * SLOPE_SAMPLE, v);
        const yBack = this.heightAt(v.x - fx * SLOPE_SAMPLE, v.z - fz * SLOPE_SAMPLE, v);
        // подъём (нос выше кормы) — нос задирается, то есть rotation.x < 0
        const slopePitch = -Math.atan((yFront - yBack) / (2 * SLOPE_SAMPLE));
        v.terrainPitch += (slopePitch - v.terrainPitch) * Math.min(1, dt * TERRAIN_LERP);

        // --- крен ------------------------------------------------------------
        const speedT = Math.min(1, v.speed / FOV_REF_SPEED);
        // поворот налево (steer > 0) уводит машину вправо: vlat < 0, кузов
        // валится наружу — правый борт вниз, то есть rotation.z > 0
        let roll = -vlat * ROLL_FROM_SLIP + v.steer * speedT * ROLL_FROM_STEER;
        if (roll > ROLL_MAX) roll = ROLL_MAX;
        else if (roll < -ROLL_MAX) roll = -ROLL_MAX;
        v.roll += (roll - v.roll) * Math.min(1, dt * BODY_LERP);

        // --- клевок ----------------------------------------------------------
        let pitch = -v.accel * PITCH_FROM_ACCEL;
        if (pitch > PITCH_MAX) pitch = PITCH_MAX;
        else if (pitch < -PITCH_MAX) pitch = -PITCH_MAX;
        v.bodyPitch += (pitch - v.bodyPitch) * Math.min(1, dt * BODY_LERP);

        const root = mesh.root;
        root.position.set(v.x, v.y, v.z);
        root.rotation.set(v.terrainPitch + v.bodyPitch, v.yaw, v.roll);

        // --- колёса ----------------------------------------------------------
        v.wheelSpin += (vfwd / v.wheelRadius) * dt;
        if (v.wheelSpin > 6283.18) v.wheelSpin -= 6283.18;
        else if (v.wheelSpin < -6283.18) v.wheelSpin += 6283.18;
        const steerAngle = v.steer * WHEEL_STEER_MAX;
        const wheels = mesh.wheels;
        wheels[0].rotation.y = steerAngle;
        wheels[1].rotation.y = steerAngle;
        wheels[0].rotation.x = v.wheelSpin;
        wheels[1].rotation.x = v.wheelSpin;
        wheels[2].rotation.x = v.wheelSpin;
        wheels[3].rotation.x = v.wheelSpin;
        mesh.syncWheels();

        // --- стоп-сигналы и призрак -------------------------------------------
        mesh.setBraking((v.flags & FLAG_BRAKING) !== 0);
        mesh.setHeadlights((v.flags & FLAG_GHOST) === 0);
    }

    // -----------------------------------------------------------------------
    // Камеры
    // -----------------------------------------------------------------------

    updateCamera(dt, v) {
        if (this.cameraMode === CAMERA_COCKPIT) this.updateCockpitCamera(dt, v);
        else this.updateChaseCamera(dt, v);
    }

    /** Камера от третьего лица: пружина, FOV от скорости, тряска, вынос в дрифте. */
    updateChaseCamera(dt, v) {
        const sinY = Math.sin(v.yaw);
        const cosY = Math.cos(v.yaw);
        const fx = sinY, fz = cosY;
        const lx = cosY, lz = -sinY;
        const dir = this.lookBack ? -1 : 1;

        const speedT = Math.min(1, v.speed / FOV_REF_SPEED);
        const dist = CHASE_DIST + speedT * CHASE_DIST_SPEED;
        const height = CHASE_HEIGHT + speedT * CHASE_HEIGHT_SPEED;

        // в дрифте камеру уносит НАРУЖУ поворота: скольжение влево (vlat > 0)
        // означает правый поворот, наружу — это влево, то есть +lateral
        let drift = 0;
        if (v.flags & FLAG_DRIFTING) {
            drift = v.vlat * DRIFT_CAM_OFFSET;
            if (drift > DRIFT_CAM_MAX) drift = DRIFT_CAM_MAX;
            else if (drift < -DRIFT_CAM_MAX) drift = -DRIFT_CAM_MAX;
        }

        const dx = v.x - fx * dist * dir + lx * drift;
        const dy = v.y + height;
        const dz = v.z - fz * dist * dir + lz * drift;

        const tx = v.x + fx * CHASE_LOOK_AHEAD * dir;
        const ty = v.y + CHASE_LOOK_UP;
        const tz = v.z + fz * CHASE_LOOK_AHEAD * dir;

        if (!this.camReady) {
            this.camX = dx; this.camY = dy; this.camZ = dz;
            this.tgtX = tx; this.tgtY = ty; this.tgtZ = tz;
            this.camReady = true;
        } else {
            const kp = Math.min(1, dt * CHASE_POS_LERP);
            this.camX += (dx - this.camX) * kp;
            this.camY += (dy - this.camY) * kp;
            this.camZ += (dz - this.camZ) * kp;
            const kt = Math.min(1, dt * CHASE_TARGET_LERP);
            this.tgtX += (tx - this.tgtX) * kt;
            this.tgtY += (ty - this.tgtY) * kt;
            this.tgtZ += (tz - this.tgtZ) * kt;
        }

        // камера не проваливается сквозь рельеф
        const gy = this.heightAtCamera(this.camX, this.camZ);
        if (this.camY < gy + CAM_GROUND_CLEARANCE) this.camY = gy + CAM_GROUND_CLEARANCE;

        const shake = this.shakeAmount(v);
        const t = this.time;
        this.camera.position.set(
            this.camX + Math.sin(t * 37.1) * shake,
            this.camY + Math.sin(t * 29.3) * shake * 1.3,
            this.camZ + Math.sin(t * 41.7) * shake
        );
        this.camera.up.set(0, 1, 0);
        this.camera.lookAt(this.tgtX, this.tgtY, this.tgtZ);
        // лёгкий крен камеры за машиной — читается как скорость
        this.camera.rotateZ(v.roll * 0.35);

        this.applyFov(dt, v, speedT);
        if (this.cockpit && this.cockpit.visible) this.cockpit.visible = false;
    }

    /** Камера из кокпита: крепится к узлу driver модели машины. */
    updateCockpitCamera(dt, v) {
        const mesh = v.mesh;
        mesh.root.updateMatrixWorld(true);
        const e = mesh.driver.matrixWorld.elements;
        const hx = e[12], hy = e[13], hz = e[14];
        // Камера сидит внутри кузова: свою машину прячем. Это и правильнее
        // (изнутри видны только изнаночные грани), и возвращает в бюджет
        // четыре draw call — ровно те два, что забирают панель и руль, плюс
        // запас. Матрицы узлов от видимости не зависят, якорь driver жив.
        mesh.root.visible = false;

        const sinY = Math.sin(v.yaw);
        const cosY = Math.cos(v.yaw);
        const dir = this.lookBack ? -1 : 1;
        const fx = sinY * dir, fz = cosY * dir;

        const shake = this.shakeAmount(v) * 0.55;
        const t = this.time;
        // взгляд чуть вперёд от затылка, чтобы шлем не лез в кадр
        this.camera.position.set(
            hx + sinY * 0.16 + Math.sin(t * 43.3) * shake,
            hy + 0.04 + Math.sin(t * 31.7) * shake * 1.2,
            hz + cosY * 0.16 + Math.sin(t * 47.9) * shake
        );

        const gy = this.heightAtCamera(this.camera.position.x, this.camera.position.z);
        if (this.camera.position.y < gy + 0.25) this.camera.position.y = gy + 0.25;

        this.camera.up.set(0, 1, 0);
        this.camera.lookAt(
            this.camera.position.x + fx * 12 + Math.sin(v.yaw + Math.PI * 0.5) * v.steer * 1.6,
            this.camera.position.y + 0.9 + v.terrainPitch * 12,
            this.camera.position.z + fz * 12 + Math.cos(v.yaw + Math.PI * 0.5) * v.steer * 1.6
        );
        this.camera.rotateZ(v.roll * 0.9);

        this.camX = this.camera.position.x;
        this.camY = this.camera.position.y;
        this.camZ = this.camera.position.z;
        this.camReady = false; // при возврате в третье лицо камера не тянется через полкарты

        const speedT = Math.min(1, v.speed / FOV_REF_SPEED);
        this.applyFov(dt, v, speedT);

        if (this.cockpit) {
            this.cockpit.visible = !this.lookBack;
            // руль крутится по steer: положительный steer — налево (раздел 4),
            // значит верх обода уходит на экране влево, то есть в +X
            this.cockpitWheel.rotation.z = -v.steer * 2.1;
        }
    }

    /** FOV растёт со скоростью и на ускорении. */
    applyFov(dt, v, speedT) {
        let want = FOV_BASE + speedT * FOV_SPEED;
        if (v.flags & FLAG_BOOST) want += FOV_BOOST;
        if (this.cameraMode === CAMERA_COCKPIT) want -= 4;
        this.fov += (want - this.fov) * Math.min(1, dt * FOV_LERP);
        if (Math.abs(this.camera.fov - this.fov) > 0.02) {
            this.camera.fov = this.fov;
            this.camera.updateProjectionMatrix();
        }
    }

    shakeAmount(v) {
        const speedT = Math.min(1, v.speed / FOV_REF_SPEED);
        let s = speedT * speedT * SHAKE_SPEED;
        if (v.flags & FLAG_OFFTRACK) s += SHAKE_OFFTRACK * speedT;
        if (v.flags & FLAG_BOOST) s += SHAKE_BOOST;
        if (v.flags & FLAG_SPIN) s += SHAKE_SPIN;
        return s;
    }

    /** Переключить вид (клавиша C по 10.4). */
    toggleCamera() {
        this.setCameraMode(this.cameraMode === CAMERA_CHASE ? CAMERA_COCKPIT : CAMERA_CHASE);
    }

    setCameraMode(mode) {
        if (mode === this.cameraMode) return;
        this.cameraMode = mode;
        this.camReady = false;
        if (this.cockpit) this.cockpit.visible = mode === CAMERA_COCKPIT;
    }

    /** Взгляд назад на удержание Shift (10.4). */
    setLookBack(on) {
        this.lookBack = !!on;
    }

    // -----------------------------------------------------------------------
    // Поверхность
    // -----------------------------------------------------------------------

    /**
     * Высота поверхности в точке. На полотне берётся из осевой линии трассы,
     * за кромкой — из createTerrainSampler (то же зерно и тот же пресет, что
     * у меша рельефа, иначе машина «утонет» или «всплывёт»).
     * Подсказка индекса живёт в объекте hintOwner, чтобы поиск был локальным.
     */
    heightAt(x, z, hintOwner) {
        const track = this.track;
        const hint = hintOwner ? hintOwner.hint : -1;
        const s = track.surface(x, z, hint);
        if (hintOwner) hintOwner.hint = s.index;
        const lat = s.lateral;
        const hw = s.halfWidth;
        if (lat > hw) return this.sampler(s.index, lat - hw, 0);
        if (lat < -hw) return this.sampler(s.index, -lat - hw, 1);
        return s.y;
    }

    /** Та же высота, но со своей подсказкой камеры. */
    heightAtCamera(x, z) {
        const track = this.track;
        const s = track.surface(x, z, this.camHint);
        this.camHint = s.index;
        const lat = s.lateral;
        const hw = s.halfWidth;
        if (lat > hw) return this.sampler(s.index, lat - hw, 0);
        if (lat < -hw) return this.sampler(s.index, -lat - hw, 1);
        return s.y;
    }

    // -----------------------------------------------------------------------
    // Отрисовка и статистика
    // -----------------------------------------------------------------------

    render() {
        this.renderer.render(this.scene, this.camera);
        // Время кадра меряется между двумя соседними отрисовками: так в него
        // попадает и работа main.js, а не только вызов render().
        const t = now();
        if (this._t0 > 0) {
            const ms = t - this._t0;
            this._lastFrameMs = ms;
            this._frameAcc += ms;
            this._frameCount++;
            if (this._frameAcc >= 400) {
                this._fps = (this._frameCount * 1000) / this._frameAcc;
                this._frameAcc = 0;
                this._frameCount = 0;
            }
        }
        this._t0 = t;
    }

    /**
     * Статистика для оверлея F3 (ui/perf.js ждёт fps, frameMs, drawCalls,
     * triangles). Объект переиспользуется — в кадре ничего не создаётся.
     */
    getStats() {
        const s = this.stats;
        const info = this.renderer.info;
        s.fps = this._fps;
        s.frameMs = this._lastFrameMs;
        s.drawCalls = info.render.calls;
        s.triangles = info.render.triangles;
        s.programs = info.programs ? info.programs.length : 0;
        s.geometries = info.memory.geometries;
        s.textures = info.memory.textures;
        s.particles = this.effects ? this.effects.activeParticles : 0;
        s.cars = this.countCars();
        // отсечение: сколько экземпляров декора и сегментов ленты дожило до кадра
        s.decorVisible = this.scenery ? this.scenery.stats.visibleInstances : 0;
        s.decorTotal = this.scenery ? this.scenery.stats.totalInstances : 0;
        s.trackSegments = this.trackMeshes ? this.trackMeshes.stats.visibleSegments : 0;
        s.timeOfDay = this.timeOfDay;
        s.weather = this.weather;
        s.quality = this.quality;
        s.renderScale = this.renderScale;
        return s;
    }

    // -----------------------------------------------------------------------
    // Клавиши
    // -----------------------------------------------------------------------

    /**
     * C — смена камеры, Shift — взгляд назад (10.4). Раскладка по event.code,
     * чтобы не зависеть от языка ввода.
     */
    attachKeys() {
        const self = this;
        this._onKeyDown = function (ev) {
            if (ev.code === 'KeyC') {
                self.toggleCamera();
            } else if (ev.code === 'ShiftLeft' || ev.code === 'ShiftRight') {
                self.lookBack = true;
            }
        };
        this._onKeyUp = function (ev) {
            if (ev.code === 'ShiftLeft' || ev.code === 'ShiftRight') {
                self.lookBack = false;
            }
        };
        window.addEventListener('keydown', this._onKeyDown);
        window.addEventListener('keyup', this._onKeyUp);
    }

    detachKeys() {
        if (this._onKeyDown) window.removeEventListener('keydown', this._onKeyDown);
        if (this._onKeyUp) window.removeEventListener('keyup', this._onKeyUp);
        this._onKeyDown = null;
        this._onKeyUp = null;
    }

    // -----------------------------------------------------------------------
    // Уборка
    // -----------------------------------------------------------------------

    /**
     * Полная очистка сцены гонки: трасса, декор, машины, тени, боксы, эффекты
     * и кэш чертежей машин. Контекст WebGL остаётся жив — он переиспользуется
     * следующей гонкой. Именно это зовётся при возврате в лобби.
     */
    dispose() {
        this.disposeRace();
        this.renderer.renderLists.dispose();
    }

    disposeRace() {
        for (let i = 0; i < MAX_CARS; i++) {
            const v = this.views[i];
            if (v.mesh) {
                this.scene.remove(v.mesh.root);
                v.mesh.dispose();
                v.mesh = null;
            }
            v.present = false;
            v.fresh = false;
            v.hint = -1;
            v.lampHead = null;
            v.lampBrake = null;
            v.lampExhaust = null;
        }
        // кэш чертежей общий на все машины (carmesh.js), чистится отдельно
        disposeCarCache();

        if (this.effects) {
            this.scene.remove(this.effects.root);
            this.effects.dispose();
            this.effects = null;
        }
        if (this.envCubeRT) {
            // 12.11: текстуры обязаны возвращаться в ноль
            this.envCubeRT.dispose();
            this.envCubeRT = null;
        }
        if (this.shadowMesh) {
            this.scene.remove(this.shadowMesh);
            disposeObject(this.shadowMesh);
            this.shadowTex.dispose();
            this.shadowMesh = null;
            this.shadowTex = null;
        }
        if (this.beamMesh) {
            this.scene.remove(this.beamMesh);
            disposeObject(this.beamMesh);
            this.beamTex.dispose();
            this.beamMesh = null;
            this.beamTex = null;
        }
        if (this.boxMesh) {
            this.scene.remove(this.boxMesh);
            disposeObject(this.boxMesh);
            this.boxMesh = null;
        }
        this.boxCount = 0;
        this.boxX = null;
        this.boxY = null;
        this.boxZ = null;

        if (this.cockpit) {
            this.camera.remove(this.cockpit);
            disposeObject(this.cockpit);
            this.cockpit = null;
            this.cockpitWheel = null;
        }
        if (this.trackMeshes) {
            this.scene.remove(this.trackMeshes.group);
            this.trackMeshes.dispose();
            this.trackMeshes = null;
        }
        if (this.scenery) {
            this.scene.remove(this.scenery.group);
            this.scenery.dispose();
            this.scenery = null;
        }
        this.track = null;
        this.sampler = null;
        this.trackSource = null;
        this.playerSource = null;
        this.raceReady = false;
        this.camReady = false;
        this.camHint = -1;
        this._fxHint = -1;
    }

    /** Окончательное закрытие: освобождает и контекст WebGL. */
    destroy() {
        this.dispose();
        this.detachKeys();
        this.renderer.dispose();
    }
}

// ---------------------------------------------------------------------------
// Геометрия кокпита
// ---------------------------------------------------------------------------

/**
 * Панель приборов, стойки и линия крыши. Одна геометрия — один draw call.
 *
 * Все детали строятся сразу в СИСТЕМЕ КАМЕРЫ: начало координат — глаз
 * водителя, -Z смотрит вперёд, +Y вверх. Узел висит на камере, поэтому в
 * кадре его не надо ни двигать, ни пересчитывать. Размеры подобраны под
 * вертикальный угол обзора около 58 градусов: панель занимает нижнюю
 * четверть кадра, стойки стоят по краям, крыша срезает верх.
 */
function buildDashGeometry(detail) {
    const geoms = [];
    const mats = [];
    const dark = toColor('#14171d');
    const plastic = toColor('#262b33');
    const glow = toColor('#4de3a8');
    const m = new THREE.Matrix4();

    function part(geom, x, y, z, rx) {
        const mm = new THREE.Matrix4().makeRotationX(rx || 0);
        mm.setPosition(x, y, z);
        geoms.push(geom);
        mats.push(mm);
    }

    // верхняя плоскость панели и её передняя стенка
    part(solidify(new THREE.BoxGeometry(2.6, 0.1, 0.8), plastic), 0, -0.56, -1.16, 0.12);
    part(solidify(new THREE.BoxGeometry(2.6, 0.6, 0.12), dark), 0, -0.9, -0.8, 0);

    // козырёк над приборами
    part(solidify(new THREE.BoxGeometry(0.82, 0.05, 0.28), dark), 0, -0.36, -1.0, -0.4);

    // два круглых прибора, повёрнутых лицом к водителю
    const seg = detail > 0 ? 14 : 7;
    for (let k = -1; k <= 1; k += 2) {
        part(solidify(new THREE.CylinderGeometry(0.1, 0.1, 0.02, seg), dark),
            k * 0.17, -0.47, -0.96, Math.PI * 0.5 + 0.2);
        part(solidify(new THREE.TorusGeometry(0.1, 0.012, 4, seg), glow),
            k * 0.17, -0.47, -0.95, 0.2);
    }

    if (detail > 0) {
        // Стойки лобового стекла. Стоят далеко и узко: вблизи они съедали бы
        // треть кадра, а кокпит должен рамку намекать, а не закрывать дорогу.
        for (let k = -1; k <= 1; k += 2) {
            const pillar = solidify(new THREE.BoxGeometry(0.1, 1.7, 0.1), dark);
            const mm = new THREE.Matrix4().makeRotationZ(k * 0.13);
            mm.setPosition(k * 1.26, 0.05, -1.6);
            geoms.push(pillar);
            mats.push(mm);
        }
    }
    if (detail > 1) {
        // центральная консоль и два дефлектора обдува
        part(solidify(new THREE.BoxGeometry(0.3, 0.1, 0.14), dark), 0, -0.52, -0.92, 0.12);
        for (let k = -1; k <= 1; k += 2) {
            part(solidify(new THREE.BoxGeometry(0.14, 0.05, 0.08), plastic),
                k * 0.56, -0.52, -0.94, 0.12);
        }
    }

    void m;
    const merged = mergeGeometries(geoms, mats);
    for (let i = 0; i < geoms.length; i++) geoms[i].dispose();
    return merged;
}

/**
 * Руль: обод, три спицы и ступица. Строится в плоскости XY с осью вдоль Z,
 * то есть уже «лицом» к водителю; наклон задаёт rotation.x узла.
 */
function buildSteeringWheelGeometry(detail) {
    const geoms = [];
    const mats = [];
    const rim = toColor('#181b21');
    const trim = toColor('#b9c0ca');
    const seg = detail > 0 ? 18 : 9;

    geoms.push(solidify(new THREE.TorusGeometry(0.19, 0.024, 4, seg), rim));
    mats.push(new THREE.Matrix4());

    for (let k = 0; k < 3; k++) {
        const a = -Math.PI * 0.5 + (k / 3) * Math.PI * 2;
        const spoke = solidify(new THREE.BoxGeometry(0.16, 0.025, 0.022), rim);
        const mm = new THREE.Matrix4().makeRotationZ(a);
        mm.multiply(new THREE.Matrix4().makeTranslation(0.095, 0, 0));
        geoms.push(spoke);
        mats.push(mm);
    }

    const hub = solidify(new THREE.CylinderGeometry(0.05, 0.05, 0.03, seg > 9 ? 8 : 6), trim);
    const hm = new THREE.Matrix4().makeRotationX(Math.PI * 0.5);
    hm.setPosition(0, 0, 0.012);
    geoms.push(hub);
    mats.push(hm);

    const merged = mergeGeometries(geoms, mats);
    for (let i = 0; i < geoms.length; i++) geoms[i].dispose();
    return merged;
}

/**
 * Слитая геометрия одной болванки: кузов + четыре колеса + фары + стопы.
 *
 * Собирается через тот же ``buildCarMesh``, что и машина гонщика, — иначе
 * это была бы вторая модель машины, которую пришлось бы сопровождать. Меш
 * тут же разбирается на геометрии, сливается в одну и выбрасывается.
 *
 * Колёса лежат в InstancedMesh со своими матрицами: их достаём и передаём
 * в mergeGeometries как матрицы частей, поэтому колесо оказывается там же,
 * где оно у гонщика, — под аркой, а не в начале координат.
 */
function buildTrafficGeometry(shape, ao) {
    const built = buildCarMesh(shape, '#ffffff', 'low', { ao: ao !== false });
    const geoms = [];
    const mats = [];
    let wheelMesh = null;
    built.root.traverse(function (node) {
        if (!node.isMesh && !node.isInstancedMesh) return;
        if (node.isInstancedMesh) { wheelMesh = node; return; }
        geoms.push(node.geometry);
        mats.push(null);
    });
    if (wheelMesh) {
        for (let i = 0; i < wheelMesh.count; i++) {
            const m = new THREE.Matrix4();
            wheelMesh.getMatrixAt(i, m);
            geoms.push(wheelMesh.geometry);
            mats.push(m);
        }
    }
    let merged = null;
    try {
        merged = mergeGeometries(geoms, mats);
    } catch (err) {
        merged = null;
    }
    if (built.dispose) built.dispose();
    return merged;
}

export default RaceRenderer;
