/**
 * scenery.js — [render] окружение трассы, небо, параметры света и тумана.
 *
 * Экспортирует buildScenery(track, theme, seed, quality) из контракта (раздел 3).
 *
 * Всё, что стоит вдоль трассы, — только InstancedMesh, по одному на тип
 * объекта (требование 10.3). Расстановка полностью детерминирована по
 * decor_seed: один и тот же seed на всех клиентах даёт один и тот же декор.
 * Объекты не ставятся на полотно и не перекрывают его: каждая точка
 * проверяется по сетке ближайших выборок осевой линии, так что декор не влезет
 * ни на свой участок трассы, ни на соседний виток.
 *
 * Высоту поверхности декор берёт из createTerrainSampler() модуля trackmesh.js —
 * это тот же самый шум, которым построена лента рельефа, поэтому предметы
 * стоят на земле, а не висят над ней. Пресет качества обязан совпадать с тем,
 * на котором построена трасса.
 *
 * --------------------------------------------------------------------------
 * ОТСЕЧЕНИЕ ПО ПИРАМИДЕ ВИДИМОСТИ — ПО ИНСТАНСАМ
 * --------------------------------------------------------------------------
 * Отсекать здесь можно только по инстансам: ограничивающая сфера целого
 * InstancedMesh охватывает всю трассу, и отсечение меша целиком не даёт
 * ничего. Поэтому каждый кадр буфер матриц заполняется заново — только
 * видимыми экземплярами, — и выставляется `count`. Невидимые экземпляры
 * не доходят даже до вершинного шейдера.
 *
 * Чтобы поиск видимых был дешёвым, объекты разложены по КОРЗИНАМ вдоль дуги
 * трассы (~45 м на корзину). Трасса — замкнутая дуга, и декор живёт узкой
 * полосой вдоль неё, поэтому одномерная раскладка по пройденному пути
 * описывает пространство не хуже сетки, а стоит одно деление. В кадре
 * проверяются КОРЗИНЫ (несколько десятков сфер на тип), а не тысячи
 * объектов; экземпляры внутри корзины лежат в буфере подряд, так что
 * видимая корзина копируется одним прогоном.
 *
 * Объекты, которым дуга не подходит (горные пики за краем карты), кладутся
 * в «свободный» хвост и проверяются поштучно — их единицы.
 *
 * Плотность декора больше НЕ нормируется по длине круга: нормировка была
 * лечением симптома ровно этой проблемы. Длинная трасса теперь стоит в кадре
 * столько же, сколько короткая, потому что в кадр попадает не круг, а то,
 * что видно.
 *
 * --------------------------------------------------------------------------
 * ОКРУЖЕНИЕ ПРИХОДИТ ПАРАМЕТРОМ
 * --------------------------------------------------------------------------
 * Время суток и погода НЕ зашиты в трассу: это настройки комнаты.
 * buildScenery принимает объект окружения `opts.env` вида
 *
 *     { timeOfDay: 'day' | 'dusk' | 'night',
 *       weather: 'clear' | 'wet' | 'snow' | 'fog' }
 *
 * Принимаются и змеиные имена полей (`time_of_day`), потому что объект
 * приходит из JSON настроек комнаты как есть. Неизвестные значения молча
 * заменяются значениями по умолчанию — сцена обязана собраться всегда.
 * Описание трассы может нести поле `time_of_day` — это ТОЛЬКО значение по
 * умолчанию, которое лобби предлагает при выборе карты.
 *
 * Сумерки не заданы отдельной таблицей: они считаются интерполяцией между
 * днём и ночью с тёплой подмешанной полосой у горизонта. Погода легла на
 * тот же приём: она меняет те же поля (туман, свет, небо) и накладывается
 * поверх времени суток. Сверх таблицы дождь и снег добавляют осадки — один
 * меш, один вызов отрисовки, движение целиком в вершинном шейдере.
 */

import * as THREE from 'three';
import {
    Rng,
    MeshBuilder,
    mergeGeometries,
    solidify,
    primBox,
    primCyl,
    primPoly,
    paintVertices,
    trs,
    toColor,
    bakeContactAO,
    bakeProximityAO,
    disposeObject,
    countTriangles,
    SphereAccum,
    fadeAdditiveFog
} from './geomutil.js';
import { readTrack, createTerrainSampler } from './trackmesh.js';

// ---------------------------------------------------------------------------
// Пресеты качества
// ---------------------------------------------------------------------------

const QUALITY = {
    low: { density: 0.5, seg: 5, clouds: 3, peaks: 10, skySeg: 12, fogFar: 260, stars: 90 },
    medium: { density: 1.1, seg: 6, clouds: 5, peaks: 16, skySeg: 16, fogFar: 360, stars: 150 },
    high: { density: 1.7, seg: 8, clouds: 8, peaks: 22, skySeg: 20, fogFar: 470, stars: 230 }
};

/**
 * Плотность декора — ОТДЕЛЬНАЯ настройка, а не производная пресета
 * (требование заказчика: каждую добавку можно выключить или убавить своей
 * галочкой). Пресет лишь выставляет значение по умолчанию.
 */
const DECOR_DENSITY = { sparse: 0.45, normal: 1.15, dense: 1.9 };

// Горы — самая тяжёлая тема по треугольникам (хвоя), поэтому прибавка
// плотности там сдержаннее: коэффициент на тему.
const THEME_DENSITY = { city: 1.0, mountain: 0.82, industrial: 1.0 };

/** Целевая длина корзины вдоль дуги, м. */
const BUCKET_LENGTH = 45.0;

/**
 * Запас к радиусу ограничивающей сферы экземпляра, м.
 *
 * Деревья, флаги и толпа гнутся в ВЕРШИННОМ шейдере, и геометрия об этом не
 * знает: её ограничивающая сфера посчитана по покоящимся вершинам. Размах
 * качания — сантиметры, но объект у самой кромки кадра не должен мигать
 * из-за них. Полметра запаса стоят долей процента лишних инстансов.
 */
const INSTANCE_MARGIN = 0.5;

// ---------------------------------------------------------------------------
// Окружение: время суток и (задел) погода
// ---------------------------------------------------------------------------

export const TIMES_OF_DAY = ['day', 'dusk', 'night'];
/**
 * Погода. Приходит тем же каналом, что и время суток, и ложится ПОВЕРХ него:
 * меняются те же поля (небо, туман, свет, облака и звёзды), а полотно
 * дополнительно темнеет и получает френелевский блик — это уже trackmesh.js.
 * Сверх таблицы дождь и снег добавляют осадки: один меш, один вызов
 * отрисовки, движение целиком в вершинном шейдере (см. buildPrecipitation).
 *
 * Зеркало server/config.WEATHERS, порядок тот же: первое — умолчание.
 * Значение, которого здесь нет, молча становится `clear`: сцена обязана
 * собраться на любом входе.
 */
export const WEATHERS = ['clear', 'wet', 'snow', 'fog'];

export const ENV_DEFAULT = { timeOfDay: 'day', weather: 'clear' };

/**
 * Привести описание окружения к внутреннему виду.
 * Принимает и `{timeOfDay, weather}`, и `{time_of_day, weather}` — ровно то,
 * что придёт из настроек комнаты, и строку `'night'` как сокращение.
 */
export function normalizeEnvironment(src, fallback) {
    const base = fallback || ENV_DEFAULT;
    let tod = base.timeOfDay;
    let weather = base.weather;
    if (typeof src === 'string') {
        if (TIMES_OF_DAY.indexOf(src) >= 0) tod = src;
    } else if (src) {
        const t = src.timeOfDay !== undefined ? src.timeOfDay : src.time_of_day;
        if (TIMES_OF_DAY.indexOf(t) >= 0) tod = t;
        const w = src.weather;
        if (WEATHERS.indexOf(w) >= 0) weather = w;
    }
    return { timeOfDay: tod || 'day', weather: weather || 'clear' };
}

/**
 * Время суток, предложенное описанием трассы.
 *
 * Это ТОЛЬКО значение по умолчанию: настоящее время суток выбирается в лобби
 * и приходит в настройках комнаты. Поле ищется там, где оно лежит: в объекте
 * трассы из race_init (`track.time_of_day`, контракт 12.1) или в клиентском
 * Track (`track.timeOfDay`).
 */
export function trackDefaultEnvironment(track) {
    if (!track) return ENV_DEFAULT;
    let tod = track.time_of_day !== undefined ? track.time_of_day : track.timeOfDay;
    if (TIMES_OF_DAY.indexOf(tod) < 0) tod = 'day';
    let weather = track.weather;
    if (WEATHERS.indexOf(weather) < 0) weather = 'clear';
    return { timeOfDay: tod, weather: weather };
}

/** Смесь двух цветов-строк: сумерки считаются из дня и ночи, а не пишутся руками. */
function mixHex(a, b, t) {
    const ca = toColor(a);
    ca.lerp(toColor(b), t);
    return '#' + ca.getHexString();
}

function lerp(a, b, t) {
    return a + (b - a) * t;
}

// ---------------------------------------------------------------------------
// Темы: небо, туман, свет, палитры объектов
// ---------------------------------------------------------------------------
//
// На тему задаются ДВА крайних состояния — день и ночь. Сумерки строятся из
// них интерполяцией (makeDusk): так три времени суток стоят одной таблицы,
// а следующая добавка (погода) ляжет тем же способом.

const CITY_DAY = {
    zenith: '#3f6fb5',
    horizon: '#bcc9da',
    fog: '#b9c6d7',
    ambient: { color: '#c3cfe0', intensity: 0.88 },
    dir: { color: '#fff4e2', intensity: 1.05, dir: [0.45, 0.82, 0.35] },
    cloud: '#f4f7fb'
};

const CITY_NIGHT = {
    zenith: '#050813',
    horizon: '#131d35',
    fog: '#0b1222',
    ambient: { color: '#46557a', intensity: 0.40 },
    dir: { color: '#8ea6d8', intensity: 0.30, dir: [-0.38, 0.86, -0.34] },
    cloud: '#101828'
};

const MOUNTAIN_DAY = {
    zenith: '#3877c4',
    horizon: '#d4e3ec',
    fog: '#cddfe9',
    ambient: { color: '#c9d8e6', intensity: 0.85 },
    dir: { color: '#fff0d2', intensity: 1.1, dir: [-0.4, 0.78, 0.48] },
    cloud: '#ffffff'
};

const MOUNTAIN_NIGHT = {
    zenith: '#03060f',
    horizon: '#0d1728',
    fog: '#0a1120',
    ambient: { color: '#3d4e70', intensity: 0.38 },
    dir: { color: '#9cb2e0', intensity: 0.34, dir: [-0.42, 0.84, 0.32] },
    cloud: '#0e1524'
};

const INDUSTRIAL_DAY = {
    zenith: '#5d7fa0',
    horizon: '#d3cbb6',
    fog: '#cfc8b6',
    ambient: { color: '#d0ccbe', intensity: 0.86 },
    dir: { color: '#fff2d6', intensity: 1.0, dir: [0.35, 0.8, -0.45] },
    cloud: '#e8e4d8'
};

const INDUSTRIAL_NIGHT = {
    zenith: '#06080f',
    horizon: '#171b2b',
    fog: '#0d1019',
    ambient: { color: '#4a4d63', intensity: 0.40 },
    dir: { color: '#96a0c2', intensity: 0.28, dir: [0.32, 0.82, -0.4] },
    cloud: '#131624'
};

/** Сумерки: между днём и ночью, плюс тёплая полоса у горизонта. */
function makeDusk(day, night) {
    const t = 0.58;
    return {
        zenith: mixHex(day.zenith, night.zenith, 0.74),
        horizon: mixHex(mixHex(day.horizon, night.horizon, t), '#e8813c', 0.46),
        fog: mixHex(mixHex(day.fog, night.fog, t), '#b06a42', 0.34),
        ambient: {
            color: mixHex(mixHex(day.ambient.color, night.ambient.color, t), '#b07a58', 0.3),
            // земля темнеет медленнее неба: иначе на закате асфальт уже ночной
            intensity: lerp(day.ambient.intensity, night.ambient.intensity, 0.44)
        },
        dir: {
            // солнце у самого горизонта: длинные косые тени в вершинном свете
            color: mixHex(mixHex(day.dir.color, night.dir.color, 0.35), '#ff8f45', 0.6),
            intensity: lerp(day.dir.intensity, night.dir.intensity, 0.42),
            dir: [day.dir.dir[0], 0.22, day.dir.dir[2]]
        },
        cloud: mixHex(mixHex(day.cloud, night.cloud, t), '#c47a64', 0.42)
    };
}

/**
 * Затянуть цвет тучами: убрать цветность и притушить.
 *
 * Именно так, а НЕ подмешиванием фиксированного серого. Серый средней
 * яркости осветлил бы ночное небо, и дождливая ночь вышла бы СВЕТЛЕЕ ясной —
 * ровно наоборот тому, что нужно, и с потерей главного ночного вида
 * (отражения огней в мокром асфальте, §12.16). Обесцвечивание с потемнением
 * работает одинаково на любой исходной яркости: днём даёт свинец, ночью
 * оставляет ночь.
 */
function overcast(hex, desat, dark) {
    const c = toColor(hex);
    const lum = c.r * 0.299 + c.g * 0.587 + c.b * 0.114;
    c.r += (lum - c.r) * desat;
    c.g += (lum - c.g) * desat;
    c.b += (lum - c.b) * desat;
    c.multiplyScalar(dark);
    return '#' + c.getHexString();
}

/**
 * Погода поверх времени суток. Небо затягивает, солнце слабеет, туман
 * подступает и сереет, звёзды пропадают. Стоит это ноль: меняются числа
 * в таблице, из которой и так собирается сцена.
 *
 *   sky, fogc, cloud — пара [обесцветить, притушить] для overcast()
 *   ambc, dirc       — обесцвечивание цвета света (яркость правит *K)
 *   ambK, dirK       — множители яркости рассеянного и направленного света
 *   fogK             — во столько раз ближе туман (он же правит отсечение)
 *   cloudK, starK    — доля облаков и звёзд
 */
const WEATHER_ENV = {
    clear: null,
    // Дождь: свинцовое небо, солнце за облаками, туман ближе и серее.
    wet: {
        sky: [0.62, 0.78], fogc: [0.55, 0.84], cloud: [0.50, 0.80],
        ambc: 0.45, ambK: 0.88,
        dirc: 0.55, dirK: 0.50,
        fogK: 0.82, cloudK: 1.5, starK: 0.0
    },
    // Снегопад светлее дождя, а не темнее: тучи те же, но снизу всё
    // отражает. Поэтому цвета не тушатся вовсе, а рассеянный свет даже
    // чуть сильнее — тени на снегу мягкие.
    snow: {
        sky: [0.74, 1.02], fogc: [0.66, 1.04], cloud: [0.35, 1.0],
        ambc: 0.40, ambK: 1.04,
        dirc: 0.55, dirK: 0.46,
        fogK: 0.70, cloudK: 1.6, starK: 0.0
    },
    // Туман — единственная погода без осадков: полотно сухое, меняется
    // только дальность. Она же правит отсечение (renderer.applyViewDistance
    // считает предел от sc.fog.far), поэтому туман ещё и дешевле ясного дня.
    fog: {
        sky: [0.82, 0.90], fogc: [0.76, 0.98], cloud: [0.6, 0.92],
        ambc: 0.55, ambK: 0.95,
        dirc: 0.62, dirK: 0.32,
        fogK: 0.44, cloudK: 1.2, starK: 0.0
    }
};

/** Наложить погоду на таблицу времени суток. Возвращает НОВЫЙ объект. */
function applyWeather(env, weather) {
    const W = WEATHER_ENV[weather];
    if (!W) return env;
    return {
        zenith: overcast(env.zenith, W.sky[0], W.sky[1]),
        horizon: overcast(env.horizon, W.sky[0], W.sky[1]),
        fog: overcast(env.fog, W.fogc[0], W.fogc[1]),
        ambient: {
            color: overcast(env.ambient.color, W.ambc, 1),
            intensity: env.ambient.intensity * W.ambK
        },
        dir: {
            color: overcast(env.dir.color, W.dirc, 1),
            intensity: env.dir.intensity * W.dirK,
            dir: env.dir.dir.slice()
        },
        cloud: overcast(env.cloud, W.cloud[0], W.cloud[1])
    };
}

// ---------------------------------------------------------------------------
// Осадки
// ---------------------------------------------------------------------------
//
// Дождь и снег — ОДИН обычный меш на всю сцену: один вызов отрисовки, ноль
// аллокаций в кадре и ноль работы для процессора. Частицы не двигаются
// на процессоре вовсе: падение, заворот по высоте, снос ветром и покачивание
// снежинок считает вершинный шейдер от общего uniform времени — того самого,
// который updateAnim() и так обновляет каждый кадр для качания декора.
//
// Коробка осадков ездит за камерой (это делает updateSky, который рендер
// и так зовёт каждый кадр). Частицы разложены по ней равномерно, поэтому
// переезд коробки не виден: в любой момент вокруг камеры одна и та же
// плотность. Ради этого же вокруг камеры оставлена пустая сфера — капля
// в сантиметре от объектива читалась бы как грязь на экране.
//
// Каждая частица — ДВА скрещённых прямоугольника, а не один: меш не
// поворачивается за камерой, и одиночный прямоугольник исчезал бы, если
// смотреть на него с ребра. Скрещённая пара видна с любого направления
// и стоит вдвое дешевле настоящего билборда в шейдере.

/**
 * Осадки по погоде. `count` — потолок при высоком пресете; ниже он
 * умножается на долю пресета и на выбранную плотность декора.
 *
 *   fall   — скорость падения, м/с
 *   len    — длина штриха (снежинки — почти квадрат), м
 *   wide   — половина ширины штриха, м
 *   slant  — насколько ветер кладёт штрих набок
 *   wobble — горизонтальное покачивание (только снег), м
 *   box    — сторона коробки вокруг камеры, м; height — её высота
 *   eye    — где в коробке по высоте сидит камера, доля
 *   hole   — радиус пустоты вокруг камеры, м
 */
const PRECIP = {
    wet: {
        count: 820, fall: 26.0, len: 1.15, wide: 0.027,
        color: '#cfdced', alpha: 0.34, slant: 0.55, wobble: 0.0,
        box: 58, height: 26, eye: 0.42, hole: 2.4
    },
    snow: {
        count: 900, fall: 3.4, len: 0.085, wide: 0.072,
        color: '#ffffff', alpha: 0.82, slant: 0.35, wobble: 0.5,
        box: 46, height: 24, eye: 0.42, hole: 1.8
    }
};

/** Доля частиц от пресета качества. Низкий пресет осадки не отменяет. */
const PRECIP_QUALITY = { low: 0.34, medium: 0.7, high: 1.0 };

/**
 * Плотность декора правит и осадки: это одна и та же жалоба игрока
 * («слишком много всего») и один и тот же переключатель в меню.
 * Разброс тут мягче, чем у декора: осадки в полтора раза гуще уже мешают
 * видеть трассу, а это запрещено.
 */
const PRECIP_DECOR_K = { sparse: 0.62, normal: 1.0, dense: 1.3 };

/**
 * Снос ветром: ровный фон плюс встречный поток от скорости машины.
 *
 * Скорость берётся из смещения камеры, ПОДЕЛЕННОГО НА ШАГ ВРЕМЕНИ, а не из
 * смещения за кадр: иначе на просевшем кадре струи ложились бы плашмя, и
 * дождь застил бы обзор ровно тогда, когда видеть трассу важнее всего.
 */
const WIND_BASE_X = 0.40;
const WIND_BASE_Z = 0.22;
const WIND_FROM_SPEED = 0.055;   // метров сноса на каждый м/с скорости
const WIND_MAX = 1.9;            // потолок сноса, м: дальше струи ложатся плашмя
const WIND_SMOOTH = 0.12;        // доля нового замера в сглаженном сносе
const WIND_DT_MAX = 0.2;         // шаг длиннее считаем разрывом, а не кадром

function clamp(v, lo, hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

/**
 * Построить осадки выбранной погоды или вернуть null, если их нет.
 *
 * @param {string} weather ключ PRECIP
 * @param {string} qName   пресет качества
 * @param {string} decor   выбранная плотность декора или null
 * @param {object} timeUniform общий {value} времени (его двигает updateAnim)
 * @param {Rng} rng детерминированная расстановка
 */
function buildPrecipitation(weather, qName, decor, timeUniform, rng) {
    const P = PRECIP[weather];
    if (!P) return null;
    const qk = PRECIP_QUALITY[qName] === undefined ? 1 : PRECIP_QUALITY[qName];
    const dk = PRECIP_DECOR_K[decor] === undefined ? 1 : PRECIP_DECOR_K[decor];
    const count = Math.max(40, Math.round(P.count * qk * dk));

    const half = P.box * 0.5;
    const hole2 = P.hole * P.hole;
    // 2 прямоугольника * 2 треугольника * 3 вершины
    const verts = count * 12;
    const pos = new Float32Array(verts * 3);
    const corner = new Float32Array(verts * 3);
    const col = new Float32Array(verts * 3);
    const base = toColor(P.color);
    const tint = new THREE.Color();

    // Углы прямоугольника в порядке обхода: два треугольника (0,1,2) и (0,2,3).
    const ORDER = [0, 1, 2, 0, 2, 3];
    const halfLen = P.len * 0.5;
    const w = P.wide;
    // плоскость A развёрнута по X, плоскость B — по Z
    const planes = [
        [[-w, -halfLen, 0], [w, -halfLen, 0], [w, halfLen, 0], [-w, halfLen, 0]],
        [[0, -halfLen, -w], [0, -halfLen, w], [0, halfLen, w], [0, halfLen, -w]]
    ];

    let v = 0;
    for (let i = 0; i < count; i++) {
        let hx = 0, hz = 0;
        // Пустая сфера вокруг камеры: капля вплотную к объективу выглядит
        // грязью на стекле, а не дождём. Отбор, а не сдвиг — сдвиг сбил бы
        // равномерность и дал бы кольцо.
        for (let tries = 0; tries < 8; tries++) {
            hx = rng.range(-half, half);
            hz = rng.range(-half, half);
            if (hx * hx + hz * hz > hole2) break;
        }
        const hy = rng.range(0, P.height);
        // Яркость вразнобой: ровная стена одинаковых штрихов читается сеткой.
        tint.copy(base).multiplyScalar(rng.range(0.72, 1.0));
        for (let p = 0; p < 2; p++) {
            const quad = planes[p];
            for (let k = 0; k < 6; k++) {
                const c = quad[ORDER[k]];
                const o = v * 3;
                pos[o] = hx; pos[o + 1] = hy; pos[o + 2] = hz;
                corner[o] = c[0]; corner[o + 1] = c[1]; corner[o + 2] = c[2];
                col[o] = tint.r; col[o + 1] = tint.g; col[o + 2] = tint.b;
                v++;
            }
        }
    }

    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geom.setAttribute('aCorner', new THREE.BufferAttribute(corner, 3));
    geom.setAttribute('color', new THREE.BufferAttribute(col, 3));
    // Меш всегда вокруг камеры целиком: считать по нему сферу нечего,
    // а автоматический расчёт отсёк бы его на краю кадра.
    geom.boundingSphere = new THREE.Sphere(new THREE.Vector3(0, P.height * 0.5, 0),
        P.box * 1.5);

    const windUniform = { value: new THREE.Vector3(WIND_BASE_X, 0, WIND_BASE_Z) };
    const mat = makePrecipMaterial(weather, P, timeUniform, windUniform);
    const mesh = new THREE.Mesh(geom, mat);
    mesh.name = 'precipitation';
    mesh.frustumCulled = false;
    // Осадки рисуются последними: они полупрозрачны и не пишут глубину.
    mesh.renderOrder = 6;

    const group = new THREE.Group();
    group.name = 'precip';
    group.add(mesh);

    let hasLast = false;
    let lastX = 0, lastZ = 0, lastT = 0;
    const wind = windUniform.value;

    return {
        group: group,
        mesh: mesh,
        material: mat,
        weather: weather,
        count: count,

        /**
         * Держать коробку осадков вокруг камеры. Зовётся из updateSky, то есть
         * раз в кадр и без единой аллокации.
         */
        follow: function (camera) {
            // Кубическая камера съёмки отражений — не камера (у неё нет
            // isCamera), и осадки в отражении всё равно замерли бы: прячем.
            const main = camera.isCamera === true;
            mesh.visible = main;
            if (!main) return;
            const px = camera.position.x;
            const pz = camera.position.z;
            group.position.set(px, camera.position.y - P.height * P.eye, pz);
            // Снос: струи не должны падать отвесно, когда машина едет 50 м/с.
            // Шаг времени берётся из того же общего uniform, который обновляет
            // updateAnim: отдельного часового механизма заводить не пришлось.
            // Он отстаёт на кадр (updateSky зовут раньше updateAnim) — для
            // сглаженной оценки скорости это ровно ничего не значит.
            const t = timeUniform.value;
            const dt = t - lastT;
            let tx = WIND_BASE_X;
            let tz = WIND_BASE_Z;
            if (hasLast && dt > 1e-4 && dt < WIND_DT_MAX) {
                tx += clamp(-(px - lastX) / dt * WIND_FROM_SPEED, -WIND_MAX, WIND_MAX);
                tz += clamp(-(pz - lastZ) / dt * WIND_FROM_SPEED, -WIND_MAX, WIND_MAX);
            }
            hasLast = true;
            lastX = px;
            lastZ = pz;
            lastT = t;
            wind.x += (tx - wind.x) * WIND_SMOOTH;
            wind.z += (tz - wind.z) * WIND_SMOOTH;
        }
    };
}

/**
 * Материал осадков: вся работа в вершинном шейдере.
 *
 * Числа вшиты в исходник (glslNum), а не приехали uniform'ами: погода одна
 * на сцену, программа компилируется один раз, и константа в шейдере дешевле
 * чтения uniform на каждую вершину. Ключ кэша программы свой на погоду —
 * без него three.js переиспользовал бы программу дождя для снега (12.16).
 */
function makePrecipMaterial(weather, P, timeUniform, windUniform) {
    const mat = new THREE.MeshBasicMaterial({
        vertexColors: true,
        transparent: true,
        opacity: P.alpha,
        depthWrite: false,
        fog: true,
        side: THREE.DoubleSide,
        forceSinglePass: true
    });
    mat.name = 'precip_' + weather;

    const body = [
        'vec3 precipHome = vec3( position );',
        // фазовый разброс из самой позиции: лишнего атрибута не нужно
        'float precipPhase = precipHome.x * 0.41 + precipHome.z * 0.73;',
        'float precipY = mod( precipHome.y - uTime * ' + glslNum(P.fall)
            + ' + precipPhase, ' + glslNum(P.height) + ' );',
        'vec3 transformed = vec3( precipHome.x, precipY, precipHome.z );',
        P.wobble > 0
            ? 'transformed.x += sin( uTime * 1.3 + precipPhase * 3.1 ) * '
                + glslNum(P.wobble) + ';\n'
              + 'transformed.z += cos( uTime * 1.1 + precipPhase * 2.3 ) * '
                + glslNum(P.wobble) + ';'
            : '',
        // сама частица плюс наклон штриха по ветру
        'transformed += aCorner;',
        'transformed.xz += uWind.xz * aCorner.y * ' + glslNum(P.slant) + ';'
    ].join('\n');

    mat.onBeforeCompile = function (shader) {
        shader.uniforms.uTime = timeUniform;
        shader.uniforms.uWind = windUniform;
        shader.vertexShader = 'uniform float uTime;\nuniform vec3 uWind;\n'
            + 'attribute vec3 aCorner;\n'
            + shader.vertexShader.replace('#include <begin_vertex>', body);
    };
    mat.customProgramCacheKey = function () {
        return 'precip:' + weather;
    };
    return mat;
}

/**
 * Параметры, зависящие от времени суток, но не от темы:
 *  fogK     — во столько раз ближе туман (ночью воздух «плотнее»)
 *  lights   — строить ли ночные огни (фонари, окна, щиты)
 *  lampK    — яркость этих огней
 *  stars    — доля звёзд от пресета (0 — не строить)
 *  clouds   — доля облаков (ночью они только мешают звёздам)
 */
const TOD_PARAMS = {
    day: { fogK: 1.0, lights: false, lampK: 0.0, stars: 0.0, clouds: 1.0, moon: 0.0 },
    dusk: { fogK: 0.92, lights: true, lampK: 0.55, stars: 0.35, clouds: 0.8, moon: 0.45 },
    night: { fogK: 0.78, lights: true, lampK: 1.0, stars: 1.0, clouds: 0.0, moon: 1.0 }
};

const THEME_ENV = {
    city: {
        day: CITY_DAY,
        dusk: makeDusk(CITY_DAY, CITY_NIGHT),
        night: CITY_NIGHT,
        palette: {
            concrete: ['#b9bfc6', '#cbcabe', '#a8aeb6', '#c4bbab', '#9aa2ac'],
            accent: ['#d0552f', '#3f7ea8', '#cbb04a'],
            metal: '#9fa6ad',
            rail: '#c9ced3',
            post: '#5d646c',
            cone: '#e0632a',
            crowd: ['#d94f4f', '#4f7fd9', '#e0c04a', '#4fb87a', '#d97fd0', '#eaeaea']
        }
    },
    mountain: {
        day: MOUNTAIN_DAY,
        dusk: makeDusk(MOUNTAIN_DAY, MOUNTAIN_NIGHT),
        night: MOUNTAIN_NIGHT,
        palette: {
            needle: ['#3d7042', '#4a7d48', '#336038', '#578a48'],
            trunk: '#4a3a2c',
            rock: ['#75705f', '#867e6d', '#635e51'],
            wood: '#7a5c3c',
            roof: ['#8c4a3a', '#4a5a6a', '#6a5a48'],
            snow: '#eef4f8',
            cone: '#e0632a',
            crowd: ['#d94f4f', '#4f7fd9', '#e0c04a', '#4fb87a', '#eaeaea']
        }
    },
    industrial: {
        day: INDUSTRIAL_DAY,
        dusk: makeDusk(INDUSTRIAL_DAY, INDUSTRIAL_NIGHT),
        night: INDUSTRIAL_NIGHT,
        palette: {
            hangar: ['#8d9298', '#7c8a90', '#98917f', '#6f7a80'],
            tank: ['#b0b4b0', '#9aa39a', '#c0b49a'],
            container: ['#bf5b32', '#3d6f8e', '#c9a63c', '#4a7a52', '#8d4a58'],
            metal: '#a2a8ad',
            post: '#565c62',
            cone: '#e0632a',
            crowd: ['#d94f4f', '#4f7fd9', '#e0c04a', '#eaeaea']
        }
    }
};

// ---------------------------------------------------------------------------
// Главная функция
// ---------------------------------------------------------------------------

/**
 * Строит окружение трассы.
 * @param {object} track   данные формата 12.1
 * @param {string} theme   city | mountain | industrial
 * @param {number} seed    decor_seed (если не задан — берётся из track)
 * @param {string} quality low | medium | high
 * @param {object} [opts]  отдельные настройки графики, добавлены после
 *                         контракта и НЕОБЯЗАТЕЛЬНЫ:
 *                         { ao, decor: sparse|normal|dense, anim,
 *                           env: {timeOfDay, weather}, nightLights: 0..1.5 }
 */
export function buildScenery(track, theme, seed, quality, opts) {
    const T = readTrack(track);
    const qName = QUALITY[quality] ? quality : 'medium';
    const Q = QUALITY[qName];
    const themeName = theme || T.theme || 'city';
    const themeEnv = THEME_ENV[themeName] || THEME_ENV.city;
    const baseSeed = (seed === undefined || seed === null ? T.seed : seed) >>> 0;

    const o = opts || {};
    const ao = o.ao !== false;
    const anim = o.anim !== false;
    const environment = normalizeEnvironment(o.env, ENV_DEFAULT);
    const todName = environment.timeOfDay;
    const tod = TOD_PARAMS[todName] || TOD_PARAMS.day;
    // Яркость ночных огней — отдельная галочка; 0 полностью снимает их
    // геометрию, то есть и draw call, и треугольники.
    const nightK = o.nightLights === undefined ? 1 : Math.max(0, o.nightLights);
    const lampK = tod.lampK * nightK;
    const lights = tod.lights && lampK > 0.01;
    const weatherName = environment.weather;
    const wx = WEATHER_ENV[weatherName] || null;
    const env = applyWeather(themeEnv[todName] || themeEnv.day, weatherName);

    const decorLevel = DECOR_DENSITY[o.decor] !== undefined ? o.decor : null;
    // Нормировка плотности по длине круга снята: отсечение по пирамиде
    // видимости сделало её ненужной (см. шапку файла).
    const density = (decorLevel ? DECOR_DENSITY[decorLevel] : Q.density)
        * (THEME_DENSITY[themeName] || 1);

    const sampler = createTerrainSampler(track, themeName, qName);
    const clearance = makeClearance(T);
    const occupancy = makeOccupancy();

    const group = new THREE.Group();
    group.name = 'scenery';

    // один материал на весь твёрдый декор: меньше переключений состояния
    const material = new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true });
    material.name = 'sceneryLambert';

    // Общее время для вершинной анимации. Объект уходит в uniforms шейдера
    // как есть, поэтому обновление сводится к записи одного числа в кадре —
    // ни аллокаций, ни перекомпиляции.
    const timeUniform = { value: 0 };

    // корзины вдоль дуги: столько же для всех типов декора
    const bucketCount = Math.max(6, Math.ceil(T.length / BUCKET_LENGTH));

    const ctx = {
        T: T,
        Q: Q,
        env: env,
        pal: themeEnv.palette,
        sampler: sampler,
        clearance: clearance,
        occupancy: occupancy,
        locate: makeLocator(T),
        bucketCount: bucketCount,
        bucketLen: T.length / bucketCount,
        group: group,
        material: material,
        instances: {},
        sets: [],
        materials: [material],
        density: density,
        ao: ao,
        anim: anim,
        timeOfDay: todName,
        lights: lights,
        lampK: lampK,
        tod: tod,
        glowMat: null,
        emitMat: null,
        timeUniform: timeUniform,
        swayMats: {}
    };

    if (themeName === 'mountain') buildMountainTheme(ctx, baseSeed);
    else if (themeName === 'industrial') buildIndustrialTheme(ctx, baseSeed);
    else buildCityTheme(ctx, baseSeed);

    // общее для всех тем: трибуны, зрители и конусы
    buildGrandstands(ctx, baseSeed ^ 0x5ab3);
    buildCrowd(ctx, baseSeed ^ 0x77c1);
    buildCones(ctx, baseSeed ^ 0x1d4f);

    // небо: купол, облака, звёзды с луной
    const skyGroup = new THREE.Group();
    skyGroup.name = 'sky';
    const sky = buildSkyDome(env, Q);
    skyGroup.add(sky);
    ctx.materials.push(sky.material);
    let clouds = null;
    const cloudShare = tod.clouds * (wx ? wx.cloudK : 1);
    if (cloudShare > 0.01) {
        clouds = buildClouds(env, Q, new Rng(baseSeed ^ 0x0c10ad), cloudShare);
        skyGroup.add(clouds);
        ctx.materials.push(clouds.material);
    }
    let stars = null;
    // под тучами звёзд не видно
    if (tod.stars * (wx ? wx.starK : 1) > 0.01) {
        stars = buildStars(Q, new Rng(baseSeed ^ 0x57a25), tod);
        skyGroup.add(stars);
        ctx.materials.push(stars.material);
    }
    group.add(skyGroup);

    // Осадки: один меш, один вызов отрисовки, движение в вершинном шейдере.
    // Плотность — от пресета качества и от той же настройки «плотность
    // декора», которой игрок и так убавляет всё лишнее в кадре.
    const precip = buildPrecipitation(weatherName, qName, decorLevel,
        timeUniform, new Rng(baseSeed ^ 0x9a1f));
    if (precip) {
        group.add(precip.group);
        ctx.materials.push(precip.material);
    }

    const fogColor = toColor(env.fog);
    const fogFar = Q.fogFar * tod.fogK * (wx ? wx.fogK : 1);
    const sets = ctx.sets;
    const api = {
        group: group,
        sky: sky,
        clouds: clouds,
        stars: stars,
        skyGroup: skyGroup,
        instances: ctx.instances,
        // наборы инстансов с раскладкой по корзинам: нужны отсечению,
        // а заодно автотесту, который проверяет его на корректность
        sets: sets,
        theme: themeName,
        quality: qName,
        timeOfDay: todName,
        weather: weatherName,
        environment: environment,
        // осадки: null, если у этой погоды их нет (ясно, туман)
        precip: precip,

        // параметры, которые применяет renderer.js
        fog: { color: fogColor, near: fogFar * 0.35, far: fogFar },
        background: fogColor.clone(),
        light: {
            ambient: { color: toColor(env.ambient.color), intensity: env.ambient.intensity },
            directional: {
                color: toColor(env.dir.color),
                intensity: env.dir.intensity,
                direction: env.dir.dir.slice()
            }
        },

        /**
         * Держать купол неба над камерой. Вызывается раз в кадр, ничего не
         * аллоцирует. Высота купола зафиксирована на нуле мира, чтобы горизонт
         * не «плавал» на рельефе.
         */
        updateSky: function (camera) {
            skyGroup.position.x = camera.position.x;
            skyGroup.position.z = camera.position.z;
            // Коробка осадков ездит за камерой тем же вызовом: отдельного
            // обхода кадра под неё заводить не пришлось.
            if (precip) precip.follow(camera);
        },

        /**
         * Время вершинной анимации декора: качание деревьев и флагов,
         * шевеление толпы. Одна запись числа в общий uniform — вся работа
         * происходит в вершинном шейдере и для процессора стоит ноль.
         * Если анимация выключена, вызов тоже ничего не стоит.
         */
        animated: anim,
        updateAnim: function (t) {
            timeUniform.value = t;
        },

        /**
         * Отсечение декора по инстансам. Зовётся раз в кадр из renderer.js,
         * ПОСЛЕ обновления матриц камеры. Ноль аллокаций.
         */
        cull: function (culler) {
            let shown = 0;
            let total = 0;
            for (let i = 0; i < sets.length; i++) {
                sets[i].cull(culler);
                shown += sets[i].mesh.count;
                total += sets[i].total;
            }
            api.stats.visibleInstances = shown;
            api.stats.totalInstances = total;
        },

        materials: ctx.materials,
        stats: {
            drawCalls: countInstanced(group),
            triangles: countSceneryTriangles(group, sets),
            totalInstances: 0,
            visibleInstances: 0
        },

        dispose: function () {
            disposeObject(group);
        }
    };
    let totalInstances = 0;
    for (let i = 0; i < sets.length; i++) totalInstances += sets[i].total;
    api.stats.totalInstances = totalInstances;
    api.stats.visibleInstances = totalInstances;
    return api;
}

function countInstanced(root) {
    let n = 0;
    root.traverse(function (o) {
        if (o.isMesh || o.isInstancedMesh) n++;
    });
    return n;
}

/**
 * Треугольники всего декора при ПОЛНОЙ загрузке инстансов.
 * countTriangles() смотрит на mesh.count, а он после сборки равен нулю:
 * буфер заполняет первый же вызов cull(). Для отчёта нужен потолок.
 */
function countSceneryTriangles(root, sets) {
    let total = countTriangles(root);
    for (let i = 0; i < sets.length; i++) {
        const set = sets[i];
        const g = set.mesh.geometry;
        const verts = g.index ? g.index.count : g.attributes.position.count;
        total += (verts / 3) * (set.total - set.mesh.count);
    }
    return Math.round(total);
}

// ---------------------------------------------------------------------------
// Вершинная анимация декора
// ---------------------------------------------------------------------------
//
// Качание деревьев и флагов и шевеление толпы делает ВЕРШИННЫЙ ШЕЙДЕР.
// Для процессора это ровно одна запись числа в кадр на всю сцену, а на GPU
// добавляется пара синусов на вершину — на интегрированной графике это
// дешевле, чем переписывать матрицы инстансов.
//
// Фаза берётся из матрицы инстанса (instanceMatrix[3] — перенос), поэтому
// соседние деревья качаются вразнобой без единого дополнительного атрибута.
// Нормали намеренно не пересчитываются: у гранёной низкополигональной хвои
// перекос освещения при отклонении в десяток сантиметров не виден.

const SWAY_MODES = {
    // ось Y: вес растёт от основания к верхушке (деревья, кусты)
    tree: { axis: 'y', base: 0.4, weight: 0.055, speed: 1.25, lift: 0 },
    bush: { axis: 'y', base: 0.05, weight: 0.1, speed: 2.0, lift: 0 },
    // ось X: полотнище флага висит вдоль +X, свободный край гуляет сильнее
    flag: { axis: 'x', base: 0.02, weight: 0.22, speed: 3.6, lift: 0.55 },
    // толпа: мелкое покачивание плюс подпрыгивание
    crowd: { axis: 'y', base: 0.18, weight: 0.05, speed: 2.7, lift: 1.2 }
};

function glslNum(v) {
    const s = String(v);
    return s.indexOf('.') >= 0 || s.indexOf('e') >= 0 ? s : s + '.0';
}

/**
 * Материал декора с качанием в вершинном шейдере.
 * @param {object} timeUniform общий {value} — его обновляет updateAnim()
 * @param {string} mode ключ SWAY_MODES
 */
function makeSwayMaterial(timeUniform, mode) {
    const M = SWAY_MODES[mode] || SWAY_MODES.tree;
    const mat = new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true });
    mat.name = 'scenerySway_' + mode;
    const coord = M.axis === 'x' ? 'abs(position.x)' : 'position.y';
    const body = [
        'vec3 transformed = vec3( position );',
        '#ifdef USE_INSTANCING',
        '  float swayPhase = instanceMatrix[3].x * 0.61 + instanceMatrix[3].z * 0.43;',
        '#else',
        '  float swayPhase = 0.0;',
        '#endif',
        'float swayW = max(' + coord + ' - ' + glslNum(M.base) + ', 0.0) * ' + glslNum(M.weight) + ';',
        'float swayT = uTime * ' + glslNum(M.speed) + ' + swayPhase;',
        'transformed.x += sin(swayT) * swayW;',
        'transformed.z += cos(swayT * 0.79 + swayPhase * 1.7) * swayW * 0.7;',
        M.lift > 0
            ? 'transformed.y += abs(sin(swayT * 0.87)) * swayW * ' + glslNum(M.lift) + ';'
            : ''
    ].join('\n');

    mat.onBeforeCompile = function (shader) {
        shader.uniforms.uTime = timeUniform;
        shader.vertexShader = 'uniform float uTime;\n' + shader.vertexShader
            .replace('#include <begin_vertex>', body);
    };
    // Разные режимы — разный исходник вершинного шейдера. Без своего ключа
    // three.js переиспользовал бы программу первого попавшегося режима.
    mat.customProgramCacheKey = function () {
        return 'sceneSway:' + mode;
    };
    return mat;
}

/** Материал качания нужного режима, по одному на сцену. */
function swayMaterial(ctx, mode) {
    if (!ctx.anim) return ctx.material;
    let m = ctx.swayMats[mode];
    if (!m) {
        m = makeSwayMaterial(ctx.timeUniform, mode);
        ctx.swayMats[mode] = m;
        ctx.materials.push(m);
    }
    return m;
}

// ---------------------------------------------------------------------------
// Проверки расстановки
// ---------------------------------------------------------------------------

const CELL = 16.0;

/** Сетка выборок осевой линии: запас до кромки полотна в произвольной точке. */
function makeClearance(T) {
    const map = new Map();
    for (let i = 0; i < T.count; i++) {
        const cx = Math.floor(T.x[i] / CELL);
        const cz = Math.floor(T.z[i] / CELL);
        const key = cx * 100003 + cz;
        let arr = map.get(key);
        if (!arr) {
            arr = [];
            map.set(key, arr);
        }
        arr.push(i);
    }
    return function (x, z) {
        const cx = Math.floor(x / CELL);
        const cz = Math.floor(z / CELL);
        let best = Infinity;
        for (let dx = -1; dx <= 1; dx++) {
            for (let dz = -1; dz <= 1; dz++) {
                const arr = map.get((cx + dx) * 100003 + (cz + dz));
                if (!arr) continue;
                for (let k = 0; k < arr.length; k++) {
                    const i = arr[k];
                    const ddx = x - T.x[i];
                    const ddz = z - T.z[i];
                    const d = Math.sqrt(ddx * ddx + ddz * ddz) - T.hw[i];
                    if (d < best) best = d;
                }
            }
        }
        // ничего в радиусе 16 м — значит до полотна заведомо далеко
        return best === Infinity ? 99 : best;
    };
}

/** Сетка занятости: не даём декору влезать друг в друга. */
function makeOccupancy() {
    const map = new Map();
    const cell = 8.0;
    return function (x, z, r) {
        const cx = Math.floor(x / cell);
        const cz = Math.floor(z / cell);
        for (let dx = -1; dx <= 1; dx++) {
            for (let dz = -1; dz <= 1; dz++) {
                const arr = map.get((cx + dx) * 100003 + (cz + dz));
                if (!arr) continue;
                for (let k = 0; k < arr.length; k += 3) {
                    const ddx = x - arr[k];
                    const ddz = z - arr[k + 1];
                    const rr = r + arr[k + 2];
                    if (ddx * ddx + ddz * ddz < rr * rr) return false;
                }
            }
        }
        const key = cx * 100003 + cz;
        let arr = map.get(key);
        if (!arr) {
            arr = [];
            map.set(key, arr);
        }
        arr.push(x, z, r);
        return true;
    };
}

function trackYaw(T, i) {
    return Math.atan2(T.tx[i], T.tz[i]);
}

/**
 * Проход вдоль трассы с детерминированной расстановкой.
 * opts: step, jitter, sides, offMin, offMax, radius, chance, sMin, sMax, sink
 * cb получает объект-размещение {x, y, z, yaw, i, side, off}.
 */
function scatter(ctx, rng, opts, cb) {
    const T = ctx.T;
    const step = Math.max(2, opts.step);
    const n = Math.max(1, Math.round(T.length / step));
    const sides = opts.sides || [1, -1];
    const chance = opts.chance === undefined ? 1 : opts.chance;
    const sMin = opts.sMin === undefined ? -1e9 : opts.sMin;
    const sMax = opts.sMax === undefined ? 1e9 : opts.sMax;

    for (let k = 0; k < n; k++) {
        let s = k * step + (opts.jitter ? rng.spread(opts.jitter) : 0);
        s = ((s % T.length) + T.length) % T.length;
        if (s < sMin || s > sMax) continue;
        const i = Math.min(T.count - 1, Math.max(0, Math.round(s / T.step) % T.count));
        for (let si = 0; si < sides.length; si++) {
            if (rng.next() > chance) continue;
            const side = sides[si];
            const u = opts.offBias ? Math.pow(rng.next(), opts.offBias) : rng.next();
            const off = opts.offMin + (opts.offMax - opts.offMin) * u;
            const lat = (T.hw[i] + off) * side;
            const x = T.x[i] + T.nx[i] * lat;
            const z = T.z[i] + T.nz[i] * lat;
            const radius = opts.radius || 1;
            if (ctx.clearance(x, z) < radius + (opts.margin === undefined ? 1.5 : opts.margin)) continue;
            if (!ctx.occupancy(x, z, radius)) continue;
            const y = ctx.sampler(i, off, side < 0 ? 1 : 0) - (opts.sink || 0);
            cb({ x: x, y: y, z: z, yaw: trackYaw(T, i), i: i, side: side, off: off, rng: rng });
        }
    }
}

/**
 * Поиск ближайшей выборки осевой линии по мировой точке.
 *
 * Нужен один раз при сборке: по нему каждый экземпляр декора получает свою
 * корзину вдоль дуги. Сетка та же по духу, что у makeClearance, но шаг
 * крупнее: точность до выборки здесь не важна, важно попасть в корзину.
 * Возвращает -1, если рядом трассы нет (горные пики за краем карты) — такие
 * объекты уходят в «свободный» хвост и проверяются поштучно.
 */
function makeLocator(T) {
    const cell = 32.0;
    const map = new Map();
    for (let i = 0; i < T.count; i++) {
        const key = Math.floor(T.x[i] / cell) * 100003 + Math.floor(T.z[i] / cell);
        let arr = map.get(key);
        if (!arr) {
            arr = [];
            map.set(key, arr);
        }
        arr.push(i);
    }
    return function (x, z) {
        const cx = Math.floor(x / cell);
        const cz = Math.floor(z / cell);
        let best = -1;
        let bestD2 = Infinity;
        for (let ring = 1; ring <= 3; ring++) {
            for (let dx = -ring; dx <= ring; dx++) {
                for (let dz = -ring; dz <= ring; dz++) {
                    const arr = map.get((cx + dx) * 100003 + (cz + dz));
                    if (!arr) continue;
                    for (let k = 0; k < arr.length; k++) {
                        const i = arr[k];
                        const ddx = x - T.x[i];
                        const ddz = z - T.z[i];
                        const d2 = ddx * ddx + ddz * ddz;
                        if (d2 < bestD2) {
                            bestD2 = d2;
                            best = i;
                        }
                    }
                }
            }
            if (best >= 0) return best;
        }
        return -1;
    };
}

/**
 * Один тип декора: источник матриц, раскладка по корзинам и покадровое
 * отсечение.
 *
 * Экземпляры в источнике отсортированы по корзинам, поэтому видимая корзина
 * копируется одним прогоном подряд идущих чисел. За корзинами идёт
 * «свободный» хвост — объекты без привязки к дуге, они проверяются поштучно.
 *
 * В кадре: bucketCount проверок сферы плюс копирование только видимых
 * матриц. Ни одной аллокации — в том числе диапазон загрузки в GPU
 * (updateRanges) описывается заранее выделенным объектом.
 */
class InstancedSet {
    constructor(mesh, total, bucketCount, far) {
        this.mesh = mesh;
        this.total = total;
        this.bucketCount = bucketCount;
        this.far = !!far;
        this.srcM = new Float32Array(total * 16);
        this.srcC = null;
        this.sphere = new Float32Array(total * 4);
        this.bucketStart = new Int32Array(bucketCount + 1);
        this.bucketSphere = new Float32Array(bucketCount * 4);
        this.freeStart = total;
        this.rangeM = { start: 0, count: 0 };
        this.rangeC = { start: 0, count: 0 };
    }

    /** Показать все экземпляры (отсечение выключено). */
    showAll() {
        const mesh = this.mesh;
        const dst = mesh.instanceMatrix.array;
        const src = this.srcM;
        for (let k = 0, n = src.length; k < n; k++) dst[k] = src[k];
        if (this.srcC && mesh.instanceColor) {
            const dc = mesh.instanceColor.array;
            const sc = this.srcC;
            for (let k = 0, n = sc.length; k < n; k++) dc[k] = sc[k];
        }
        this.apply(this.total);
    }

    cull(culler) {
        if (!culler || !culler.enabled) {
            this.showAll();
            return;
        }
        const mesh = this.mesh;
        const dst = mesh.instanceMatrix.array;
        const dstC = mesh.instanceColor ? mesh.instanceColor.array : null;
        const srcM = this.srcM;
        const srcC = this.srcC;
        const bs = this.bucketStart;
        const bsp = this.bucketSphere;
        const far = this.far;
        let n = 0;

        for (let b = 0; b < this.bucketCount; b++) {
            const a0 = bs[b];
            const a1 = bs[b + 1];
            if (a1 <= a0) continue;
            const o = b * 4;
            const r = bsp[o + 3];
            if (r < 0) continue;
            const ok = far
                ? culler.inFrustum(bsp[o], bsp[o + 1], bsp[o + 2], r)
                : culler.visible(bsp[o], bsp[o + 1], bsp[o + 2], r);
            if (!ok) continue;
            let so = a0 * 16;
            let dofs = n * 16;
            const cnt = (a1 - a0) * 16;
            for (let k = 0; k < cnt; k++) dst[dofs + k] = srcM[so + k];
            if (dstC) {
                let sc = a0 * 3;
                let dc = n * 3;
                const c3 = (a1 - a0) * 3;
                for (let k = 0; k < c3; k++) dstC[dc + k] = srcC[sc + k];
            }
            n += a1 - a0;
        }

        // «свободный» хвост: объекты вне дуги, проверяются по одному
        const sph = this.sphere;
        for (let i = this.freeStart; i < this.total; i++) {
            const o = i * 4;
            const r = sph[o + 3];
            if (r < 0) continue;
            const ok = far
                ? culler.inFrustum(sph[o], sph[o + 1], sph[o + 2], r)
                : culler.visible(sph[o], sph[o + 1], sph[o + 2], r);
            if (!ok) continue;
            const so = i * 16;
            const dofs = n * 16;
            for (let k = 0; k < 16; k++) dst[dofs + k] = srcM[so + k];
            if (dstC) {
                const sc = i * 3;
                const dc = n * 3;
                dstC[dc] = srcC[sc];
                dstC[dc + 1] = srcC[sc + 1];
                dstC[dc + 2] = srcC[sc + 2];
            }
            n++;
        }

        this.apply(n);
    }

    /**
     * Выставить count и отметить к загрузке только занятую часть буфера.
     *
     * Диапазон загрузки важен: буфер матриц рассчитан на ВЕСЬ декор трассы,
     * а видима обычно четверть. Без диапазона three.js гнал бы в GPU весь
     * буфер каждый кадр и половина выигрыша ушла бы в шину.
     */
    apply(n) {
        const mesh = this.mesh;
        mesh.count = n;
        mesh.visible = n > 0;
        if (n === 0) return;
        const im = mesh.instanceMatrix;
        im.updateRanges.length = 0;
        this.rangeM.start = 0;
        this.rangeM.count = n * 16;
        im.updateRanges.push(this.rangeM);
        im.needsUpdate = true;
        if (mesh.instanceColor) {
            const ic = mesh.instanceColor;
            ic.updateRanges.length = 0;
            this.rangeC.start = 0;
            this.rangeC.count = n * 3;
            ic.updateRanges.push(this.rangeC);
            ic.needsUpdate = true;
        }
    }
}

/**
 * Сборка InstancedMesh из списка размещений.
 * opts: { ao } — параметры запекания контактного затенения в вершинные цвета
 *       (см. bakeContactAO), { material } — свой материал вместо общего,
 *       { far } — объект заведомо дальше тумана и отсекается только
 *       по пирамиде (горные пики).
 *
 * Здесь же экземпляры раскладываются по корзинам вдоль дуги и считаются их
 * ограничивающие сферы: всё, что нужно покадровому отсечению.
 */
function addInstanced(ctx, name, geometry, placements, opts) {
    if (!placements.length) {
        geometry.dispose();
        return null;
    }
    const o = opts || {};
    if (ctx.ao && o.ao) bakeContactAO(geometry, o.ao);
    if (!geometry.boundingSphere) geometry.computeBoundingSphere();

    const n = placements.length;
    const bucketCount = ctx.bucketCount;

    // 1. корзина каждого размещения: -1 — вне дуги
    const bucketOf = new Int32Array(n);
    const counts = new Int32Array(bucketCount + 1); // последняя ячейка — свободные
    for (let i = 0; i < n; i++) {
        const p = placements[i];
        const si = ctx.locate(p.x, p.z);
        let b = bucketCount; // свободный хвост
        if (si >= 0) {
            const s = ctx.T.s ? ctx.T.s[si] : si * ctx.T.step;
            b = Math.floor(s / ctx.bucketLen);
            if (b < 0) b = 0;
            else if (b >= bucketCount) b = bucketCount - 1;
        }
        bucketOf[i] = b;
        counts[b]++;
    }

    // 2. префиксные суммы -> место каждого экземпляра в источнике
    const start = new Int32Array(bucketCount + 2);
    for (let b = 0; b <= bucketCount; b++) start[b + 1] = start[b] + counts[b];
    const cursor = new Int32Array(bucketCount + 1);
    for (let b = 0; b <= bucketCount; b++) cursor[b] = start[b];

    const mesh = new THREE.InstancedMesh(geometry, o.material || ctx.material, n);
    mesh.name = name;
    mesh.frustumCulled = false;
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    mesh.count = 0;
    mesh.visible = false;

    // ignoreColor: размещения переиспользуются другим типом (окна берут
    // расстановку зданий), и цвет бетона там только испортил бы свет
    let anyColor = false;
    if (!o.ignoreColor) {
        for (let i = 0; i < n; i++) if (placements[i].color) { anyColor = true; break; }
    }
    if (anyColor) {
        // 12.11: instanceColor доходит до шейдера только при vertexColors
        mesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(n * 3).fill(1), 3);
        mesh.instanceColor.setUsage(THREE.DynamicDrawUsage);
    }

    const set = new InstancedSet(mesh, n, bucketCount, !!o.far);
    set.freeStart = start[bucketCount];
    for (let b = 0; b <= bucketCount; b++) set.bucketStart[b] = start[b];
    if (anyColor) set.srcC = new Float32Array(n * 3).fill(1);

    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const pos = new THREE.Vector3();
    const scl = new THREE.Vector3();
    const axis = new THREE.Vector3(0, 1, 0);
    const col = new THREE.Color();
    const sph = new THREE.Sphere();
    const accums = [];
    for (let b = 0; b < bucketCount; b++) accums.push(new SphereAccum());

    for (let i = 0; i < n; i++) {
        const p = placements[i];
        const b = bucketOf[i];
        const slot = cursor[b]++;

        q.setFromAxisAngle(axis, p.yaw || 0);
        pos.set(p.x, p.y, p.z);
        scl.set(p.sx === undefined ? 1 : p.sx, p.sy === undefined ? 1 : p.sy, p.sz === undefined ? 1 : p.sz);
        m.compose(pos, q, scl);
        m.toArray(set.srcM, slot * 16);

        // ограничивающая сфера экземпляра: сфера геометрии под его матрицей
        sph.copy(geometry.boundingSphere).applyMatrix4(m);
        const so = slot * 4;
        set.sphere[so] = sph.center.x;
        set.sphere[so + 1] = sph.center.y;
        set.sphere[so + 2] = sph.center.z;
        const r = sph.radius + INSTANCE_MARGIN;
        set.sphere[so + 3] = r;
        if (b < bucketCount) accums[b].add(sph.center.x, sph.center.y, sph.center.z, r);

        if (p.color && set.srcC && !o.ignoreColor) {
            toColor(p.color, col);
            set.srcC[slot * 3] = col.r;
            set.srcC[slot * 3 + 1] = col.g;
            set.srcC[slot * 3 + 2] = col.b;
        }
    }

    for (let b = 0; b < bucketCount; b++) accums[b].writeTo(set.bucketSphere, b * 4);

    ctx.group.add(mesh);
    ctx.instances[name] = mesh;
    ctx.sets.push(set);
    return mesh;
}

// ---------------------------------------------------------------------------
// Геометрия типовых объектов (единичный масштаб, база на y = 0)
// ---------------------------------------------------------------------------

const WHITE = new THREE.Color(1, 1, 1);

/** Коробка 1x1x1 с базой на нуле и горизонтальными полосами «этажей». */
function boxBands(bands, bandShade, topShade, vertical) {
    const b = new MeshBuilder();
    const c = new THREE.Color();
    const top = new THREE.Color(topShade, topShade, topShade);
    for (let k = 0; k < bands; k++) {
        const t0 = k / bands;
        const t1 = (k + 1) / bands;
        const sh = k % 2 === 1 ? bandShade : 1.0;
        c.setRGB(sh, sh, sh);
        if (vertical) {
            // вертикальные рёбра: делим по горизонтали
            const u0 = -0.5 + t0,
                u1 = -0.5 + t1;
            b.quad([u0, 0, 0.5], [u1, 0, 0.5], [u1, 1, 0.5], [u0, 1, 0.5], c);
            b.quad([u1, 0, -0.5], [u0, 0, -0.5], [u0, 1, -0.5], [u1, 1, -0.5], c);
            b.quad([0.5, 0, -u0], [0.5, 0, -u1], [0.5, 1, -u1], [0.5, 1, -u0], c);
            b.quad([-0.5, 0, u0], [-0.5, 0, u1], [-0.5, 1, u1], [-0.5, 1, u0], c);
        } else {
            b.quad([-0.5, t0, 0.5], [0.5, t0, 0.5], [0.5, t1, 0.5], [-0.5, t1, 0.5], c);
            b.quad([0.5, t0, -0.5], [-0.5, t0, -0.5], [-0.5, t1, -0.5], [0.5, t1, -0.5], c);
            b.quad([0.5, t0, 0.5], [0.5, t0, -0.5], [0.5, t1, -0.5], [0.5, t1, 0.5], c);
            b.quad([-0.5, t0, -0.5], [-0.5, t0, 0.5], [-0.5, t1, 0.5], [-0.5, t1, -0.5], c);
        }
    }
    b.quad([-0.5, 1, -0.5], [-0.5, 1, 0.5], [0.5, 1, 0.5], [0.5, 1, -0.5], top);
    return b.build();
}

/** Двускатная крыша: конёк вдоль оси Z, основание на высоте baseY. */
function gableRoof(w, h, d, color, gable, baseY) {
    const b = new MeshBuilder();
    const hw = w * 0.5,
        hd = d * 0.5;
    const yt = baseY + h;
    // скаты
    b.quad([-hw, baseY, -hd], [-hw, baseY, hd], [0, yt, hd], [0, yt, -hd], color);
    b.quad([0, yt, -hd], [0, yt, hd], [hw, baseY, hd], [hw, baseY, -hd], color);
    // фронтоны
    b.tri([-hw, baseY, hd], [hw, baseY, hd], [0, yt, hd], gable);
    b.tri([hw, baseY, -hd], [-hw, baseY, -hd], [0, yt, -hd], gable);
    return b.build();
}

/** Столб освещения: мачта, кронштейн, плафон. */
function lampGeometry(pal, seg) {
    const post = toColor(pal.post);
    const metal = toColor(pal.metal);
    const parts = [
        primCyl(0.07, 0.11, 6.2, seg, post, 0, 3.1, 0),
        primBox(0.9, 0.12, 0.12, post, 0.42, 6.1, 0),
        primBox(0.5, 0.12, 0.26, metal, 0.78, 5.98, 0),
        primBox(0.42, 0.06, 0.2, toColor('#fff0c0'), 0.78, 5.9, 0)
    ];
    return mergeGeometries(parts);
}

/** Секция отбойника длиной 6 м вдоль оси Z. */
function guardrailGeometry(pal) {
    const rail = toColor(pal.rail);
    const post = toColor(pal.post);
    const parts = [
        primBox(0.09, 0.3, 6.0, rail, 0, 0.62, 0),
        primBox(0.14, 0.06, 6.0, rail, 0, 0.78, 0),
        primBox(0.12, 0.66, 0.12, post, 0, 0.33, -2.4),
        primBox(0.12, 0.66, 0.12, post, 0, 0.33, 2.4)
    ];
    return mergeGeometries(parts);
}

/** Рекламный щит: две опоры, рама, полотно. */
function billboardGeometry(pal) {
    const post = toColor(pal.post);
    const parts = [
        primBox(0.18, 4.2, 0.18, post, -1.6, 2.1, 0),
        primBox(0.18, 4.2, 0.18, post, 1.6, 2.1, 0),
        primBox(5.0, 2.6, 0.16, toColor('#2a2d33'), 0, 5.0, 0),
        primBox(4.7, 2.3, 0.1, WHITE, 0, 5.0, 0.1)
    ];
    return mergeGeometries(parts);
}

/** Дорожный конус. */
function coneGeometry(pal, seg) {
    const c = toColor(pal.cone);
    const parts = [
        primBox(0.42, 0.05, 0.42, toColor('#2b2e33'), 0, 0.025, 0),
        primCyl(0.03, 0.17, 0.62, seg, c, 0, 0.33, 0),
        primCyl(0.115, 0.13, 0.1, seg, WHITE, 0, 0.36, 0)
    ];
    return mergeGeometries(parts);
}

/** Зритель: корпус, голова, руки-палки. */
function spectatorGeometry() {
    const parts = [
        primBox(0.42, 0.72, 0.26, WHITE, 0, 0.94, 0),
        primBox(0.16, 0.2, 0.17, toColor('#c79a77'), 0, 1.42, 0),
        primBox(0.34, 0.56, 0.2, toColor('#3b4048'), 0, 0.3, 0)
    ];
    return mergeGeometries(parts);
}

/**
 * Лиственное дерево: ствол и две гранёные кроны. Дешевле хвойного и нужно,
 * чтобы город и промзона перестали быть голыми коробками.
 */
function broadleafGeometry(seg, trunkColor, leafColor) {
    const trunk = toColor(trunkColor);
    const leaf = toColor(leafColor);
    const dark = new THREE.Color(leaf.r * 0.72, leaf.g * 0.74, leaf.b * 0.7);
    return mergeGeometries([
        primCyl(0.13, 0.19, 2.2, Math.max(4, seg - 2), trunk, 0, 1.1, 0),
        primPoly(1.15, 0, leaf, 0, 3.1, 0),
        primPoly(0.78, 0, dark, 0.42, 2.4, 0.2)
    ]);
}

/**
 * Мягкая вариация яркости для инстансов, у которых цвет УЖЕ запечён в
 * геометрию. Инстансный цвет домножает все вершины подряд, поэтому красить
 * дерево целиком в зелёный нельзя — вместе с кроной позеленел бы и ствол.
 * Здесь вместо покраски идёт разброс яркости с лёгким сдвигом оттенка.
 */
function tintVariation(rng) {
    const k = rng.range(0.82, 1.12);
    return new THREE.Color(
        k * rng.range(0.88, 1.02),
        k * rng.range(0.94, 1.1),
        k * rng.range(0.82, 1.0)
    );
}

/** Куст: приплюснутый многогранник у самой земли. */
function bushGeometry(color) {
    const g = primPoly(0.8, 0, toColor(color), 0, 0.42, 0);
    g.scale(1.0, 0.62, 1.0);
    return g;
}

/** Бочка: цилиндр с двумя ободами. */
function barrelGeometry(seg, color) {
    const body = toColor(color);
    const ring = new THREE.Color(body.r * 0.7, body.g * 0.7, body.b * 0.7);
    return mergeGeometries([
        primCyl(0.29, 0.29, 0.88, seg, body, 0, 0.44, 0),
        primCyl(0.31, 0.31, 0.07, seg, ring, 0, 0.28, 0),
        primCyl(0.31, 0.31, 0.07, seg, ring, 0, 0.62, 0)
    ]);
}

/**
 * Флаг на мачте. Полотнище лежит вдоль +X от мачты и нарезано на сегменты:
 * вершинный шейдер гнёт его волной, поэтому сегменты обязаны быть.
 * Ткань строится двумя гранями подряд (лицом в +Z и в -Z), чтобы флаг
 * читался с обеих сторон БЕЗ двустороннего материала: прозрачности здесь
 * нет, но лишний материал — лишняя программа, а так их ноль.
 */
function flagGeometry(seg, poleColor, clothColor) {
    const pole = toColor(poleColor);
    const cloth = toColor(clothColor);
    const clothDark = new THREE.Color(cloth.r * 0.74, cloth.g * 0.74, cloth.b * 0.74);
    const b = new MeshBuilder();
    const segs = 5;
    const x0 = 0.07;
    const len = 1.55;
    const yb = 2.62;
    const yt = 3.62;
    for (let k = 0; k < segs; k++) {
        const ax = x0 + (len * k) / segs;
        const bx = x0 + (len * (k + 1)) / segs;
        // покоящаяся волна: даже без анимации полотнище не плоская доска
        const az = Math.sin(k * 0.9) * 0.05;
        const bz = Math.sin((k + 1) * 0.9) * 0.05;
        const c = k & 1 ? clothDark : cloth;
        b.quad([ax, yb, az], [bx, yb, bz], [bx, yt, bz], [ax, yt, az], c);
        b.quad([bx, yb, bz], [ax, yb, az], [ax, yt, az], [bx, yt, bz], c);
    }
    const clothGeom = b.build();
    return mergeGeometries([
        primCyl(0.045, 0.07, 4.1, Math.max(4, seg - 3), pole, 0, 2.05, 0),
        primCyl(0.09, 0.09, 0.1, Math.max(4, seg - 3), pole, 0, 4.12, 0),
        clothGeom
    ]);
}

/** Трибуна: четыре ступени и задняя стенка, 18 м по фронту. */
function standGeometry(pal) {
    const frame = toColor('#8e949b');
    const seatColors = ['#c8503f', '#3f6ec8', '#c8a83f', '#4aa06a'];
    const parts = [];
    for (let k = 0; k < 4; k++) {
        const y = 0.5 + k * 0.72;
        const z = -k * 1.15;
        parts.push(primBox(18.0, 0.24, 1.1, toColor(seatColors[k % seatColors.length]), 0, y + 0.5, z));
        parts.push(primBox(18.0, 0.7 + k * 0.72, 0.16, frame, 0, (0.7 + k * 0.72) * 0.5, z + 0.55));
    }
    parts.push(primBox(18.4, 4.2, 0.25, frame, 0, 2.1, -4.0));
    parts.push(primBox(18.4, 0.35, 5.6, frame, 0, 0.17, -1.7));
    return mergeGeometries(parts);
}

// ---------------------------------------------------------------------------
// Тема: город
// ---------------------------------------------------------------------------

function buildCityTheme(ctx, seed) {
    const pal = ctx.pal;
    const d = ctx.density;
    const seg = ctx.Q.seg;

    // здания
    const rngB = new Rng(seed ^ 0x00b1);
    const buildings = [];
    scatter(ctx, rngB, { step: 26 / d, jitter: 6, offMin: 13, offMax: 38, offBias: 1.5, radius: 9, margin: 3, chance: 0.8 }, function (p) {
        const w = p.rng.range(8, 17);
        const h = p.rng.range(6, 30);
        const dep = p.rng.range(8, 16);
        buildings.push({
            x: p.x,
            y: p.y - 0.4,
            z: p.z,
            yaw: p.yaw + (p.rng.chance(0.35) ? Math.PI * 0.5 : 0) + p.rng.spread(0.12),
            sx: w,
            sy: h,
            sz: dep,
            color: p.rng.pick(pal.concrete)
        });
    });
    addInstanced(ctx, 'buildings', boxBands(6, 0.62, 1.1, false), buildings, {
        // здание — единичный куб, растянутый до 30 м: высота затенения тоже
        // задаётся в долях куба, иначе тень у основания уползёт на пол-этажа
        ao: { base: 0, height: 0.15, floor: 0.36, power: 0.75, sky: 0.26 }
    });
    // ночью в зданиях горят окна: тот же набор размещений, своя геометрия
    if (ctx.lights && buildings.length) {
        addInstanced(ctx, 'windows',
            windowsGeometry(new Rng(seed ^ 0x00b7), 4, 7, ctx.lampK),
            buildings, { material: emitMaterial(ctx), ignoreColor: true });
    }

    // фонари вдоль кромки
    const rngL = new Rng(seed ^ 0x00c2);
    const lamps = [];
    scatter(ctx, rngL, { step: 30 / d, jitter: 1.5, offMin: 2.2, offMax: 3.2, radius: 0.6, margin: 0.6, chance: 0.5 }, function (p) {
        lamps.push({ x: p.x, y: p.y, z: p.z, yaw: p.yaw + (p.side > 0 ? Math.PI : 0), sy: p.rng.range(0.92, 1.08) });
    });
    addInstanced(ctx, 'lamps', lampGeometry(pal, seg), lamps, {
        ao: { base: 0, height: 1.5, floor: 0.42, power: 0.65, sky: 0.3 }
    });
    if (ctx.lights && lamps.length) {
        // конус света и пятно на асфальте — один меш на все фонари
        addInstanced(ctx, 'lampGlow', lampGlowGeometry(seg, ctx.lampK), lamps,
            { material: glowMaterial(ctx) });
    }

    // отбойники: сплошные участки по 6 м
    const rngG = new Rng(seed ^ 0x00d3);
    const rails = [];
    scatter(ctx, rngG, { step: 6.0, jitter: 0, offMin: 1.5, offMax: 1.5, radius: 0.4, margin: 0.4, chance: 0.55 }, function (p) {
        rails.push({ x: p.x, y: p.y, z: p.z, yaw: p.yaw });
    });
    addInstanced(ctx, 'guardrails', guardrailGeometry(pal), rails, {
        ao: { base: 0, height: 0.75, floor: 0.44, power: 0.7, sky: 0.3 }
    });

    // рекламные щиты
    const rngA = new Rng(seed ^ 0x00e4);
    const boards = [];
    scatter(ctx, rngA, { step: 150 / d, jitter: 20, offMin: 7, offMax: 13, radius: 3, margin: 2, chance: 0.7 }, function (p) {
        boards.push({
            x: p.x,
            y: p.y,
            z: p.z,
            yaw: p.yaw + Math.PI * 0.5 + (p.side > 0 ? Math.PI : 0),
            sx: p.rng.range(0.9, 1.2),
            sy: p.rng.range(0.85, 1.15),
            color: p.rng.pick(pal.accent)
        });
    });
    addInstanced(ctx, 'billboards', billboardGeometry(pal), boards, {
        ao: { base: 0, height: 2.0, floor: 0.46, power: 0.7, sky: 0.28 }
    });
    if (ctx.lights && boards.length) {
        // подсветка щита: самосветящееся полотно плюс мягкий ореол перед ним.
        // Полотно красится инстансным цветом — щиты остаются разными.
        addInstanced(ctx, 'billboardGlow', billboardGlowGeometry(ctx.lampK), boards,
            { material: emitMaterial(ctx) });
    }

    // уличные деревья вдоль тротуара: город перестаёт быть голыми коробками
    const rngT = new Rng(seed ^ 0x00f5);
    const trees = [];
    scatter(ctx, rngT, { step: 17 / d, jitter: 3.5, offMin: 3.4, offMax: 9, radius: 1.5, margin: 1.2, chance: 0.66 }, function (p) {
        const sc = p.rng.range(0.8, 1.35);
        trees.push({
            x: p.x, y: p.y - 0.1, z: p.z,
            yaw: p.rng.range(0, Math.PI * 2),
            sx: sc * p.rng.range(0.9, 1.1),
            sy: sc * p.rng.range(0.95, 1.3),
            sz: sc * p.rng.range(0.9, 1.1),
            color: tintVariation(p.rng)
        });
    });
    addInstanced(ctx, 'cityTrees', broadleafGeometry(seg, '#5a4632', '#4f8a48'), trees, {
        material: swayMaterial(ctx, 'tree'),
        ao: { base: 0, height: 1.7, floor: 0.42, power: 0.6, sky: 0.34, down: 0.7 }
    });

    buildFlags(ctx, seed ^ 0x00a6, ['#d0552f', '#3f7ea8', '#cbb04a', '#eaeaea']);
}

// ---------------------------------------------------------------------------
// Тема: горы
// ---------------------------------------------------------------------------

/** Хвойное дерево: ствол и несколько ярусов кроны. */
function pineGeometry(pal, seg, tiers, spread, color) {
    const trunk = toColor(pal.trunk);
    const needle = toColor(color);
    const parts = [primCyl(0.12, 0.2, 1.4, Math.max(4, seg - 2), trunk, 0, 0.7, 0)];
    for (let k = 0; k < tiers; k++) {
        const t = k / tiers;
        const r = spread * (1 - t * 0.55);
        const h = 1.5 - t * 0.35;
        parts.push(primCyl(k === tiers - 1 ? 0.0 : r * 0.45, r, h, seg, needle, 0, 1.1 + k * (h * 0.62), 0));
    }
    return mergeGeometries(parts);
}

function buildMountainTheme(ctx, seed) {
    const pal = ctx.pal;
    const d = ctx.density;
    const seg = ctx.Q.seg;

    // три вида хвойных
    const kinds = [
        { name: 'pineTall', geom: pineGeometry(pal, seg, 3, 1.35, pal.needle[0]), scale: [1.5, 2.4] },
        { name: 'pineWide', geom: pineGeometry(pal, seg, 2, 1.95, pal.needle[1]), scale: [1.1, 1.7] },
        { name: 'pineYoung', geom: pineGeometry(pal, Math.max(4, seg - 1), 2, 1.15, pal.needle[3]), scale: [0.7, 1.1] }
    ];
    const lists = [[], [], []];
    const rngT = new Rng(seed ^ 0x0f01);
    scatter(ctx, rngT, { step: 7 / d, jitter: 3.0, offMin: 6, offMax: 42, offBias: 1.8, radius: 2.2, margin: 1.5, chance: 0.9 }, function (p) {
        const k = p.off > 18 ? p.rng.int(0, 2) : p.rng.int(1, 2);
        const s = p.rng.range(kinds[k].scale[0], kinds[k].scale[1]);
        lists[k].push({
            x: p.x,
            y: p.y - 0.15,
            z: p.z,
            yaw: p.rng.range(0, Math.PI * 2),
            sx: s * p.rng.range(0.9, 1.1),
            sy: s * p.rng.range(0.95, 1.25),
            sz: s * p.rng.range(0.9, 1.1)
        });
    });
    const pineAO = { base: 0, height: 1.5, floor: 0.4, power: 0.6, sky: 0.34, down: 0.68 };
    const pineMat = swayMaterial(ctx, 'tree');
    for (let k = 0; k < 3; k++) {
        addInstanced(ctx, kinds[k].name, kinds[k].geom, lists[k], { material: pineMat, ao: pineAO });
    }

    // валуны
    const rngR = new Rng(seed ^ 0x0f02);
    const rocks = [];
    const rockGeom = primPoly(1.0, 0, toColor(pal.rock[0]), 0, 0.45, 0);
    scatter(ctx, rngR, { step: 13 / d, jitter: 5, offMin: 3.5, offMax: 36, offBias: 1.6, radius: 1.4, margin: 1.0, chance: 0.7 }, function (p) {
        const s = p.rng.range(0.6, 2.4);
        rocks.push({
            x: p.x,
            y: p.y - s * 0.2,
            z: p.z,
            yaw: p.rng.range(0, Math.PI * 2),
            sx: s * p.rng.range(0.8, 1.3),
            sy: s * p.rng.range(0.5, 0.9),
            sz: s * p.rng.range(0.8, 1.3),
            color: p.rng.pick(pal.rock)
        });
    });
    addInstanced(ctx, 'rocks', rockGeom, rocks, {
        ao: { height: 1.3, floor: 0.46, power: 0.7, sky: 0.3 }
    });

    // кусты в подлеске: мелкая зелень, которая шевелится на ветру
    const rngBu = new Rng(seed ^ 0x0f07);
    const bushes = [];
    scatter(ctx, rngBu, { step: 9 / d, jitter: 3.5, offMin: 3.0, offMax: 30, offBias: 1.5, radius: 0.9, margin: 0.9, chance: 0.55 }, function (p) {
        const sc = p.rng.range(0.7, 1.5);
        bushes.push({
            x: p.x, y: p.y - 0.08, z: p.z,
            yaw: p.rng.range(0, Math.PI * 2),
            sx: sc, sy: sc * p.rng.range(0.7, 1.2), sz: sc,
            color: p.rng.pick(['#416a3a', '#4d7742', '#375d33', '#5a8147'])
        });
    });
    addInstanced(ctx, 'bushes', bushGeometry('#ffffff'), bushes, {
        material: swayMaterial(ctx, 'bush'),
        ao: { height: 0.7, floor: 0.44, power: 0.7, sky: 0.32, down: 0.7 }
    });

    // деревянные ограждения по кромке
    const rngF = new Rng(seed ^ 0x0f03);
    const fence = mergeGeometries([
        primBox(0.12, 1.05, 0.12, toColor(pal.wood), 0, 0.52, -1.9),
        primBox(0.12, 1.05, 0.12, toColor(pal.wood), 0, 0.52, 1.9),
        primBox(0.07, 0.16, 4.0, toColor(pal.wood), 0, 0.92, 0),
        primBox(0.07, 0.16, 4.0, toColor(pal.wood), 0, 0.55, 0)
    ]);
    const fences = [];
    scatter(ctx, rngF, { step: 4.0, jitter: 0, offMin: 1.6, offMax: 1.6, radius: 0.4, margin: 0.4, chance: 0.5 }, function (p) {
        fences.push({ x: p.x, y: p.y, z: p.z, yaw: p.yaw });
    });
    addInstanced(ctx, 'fences', fence, fences, {
        ao: { base: 0, height: 0.95, floor: 0.44, power: 0.7, sky: 0.3 }
    });

    // редкие домики
    const rngH = new Rng(seed ^ 0x0f04);
    const hutGeom = mergeGeometries([
        primBox(6.0, 3.0, 5.0, WHITE, 0, 1.5, 0),
        primBox(6.6, 0.35, 5.6, toColor('#6a5a48'), 0, 3.1, 0),
        primCyl(0.0, 4.6, 2.2, 4, toColor('#8c4a3a'), 0, 4.2, Math.PI * 0.25),
        primBox(0.5, 1.4, 0.12, toColor('#4a3a2c'), 0, 0.7, 2.55)
    ]);
    const huts = [];
    scatter(ctx, rngH, { step: 210 / d, jitter: 30, offMin: 16, offMax: 34, radius: 6, margin: 3, chance: 0.6 }, function (p) {
        huts.push({
            x: p.x,
            y: p.y - 0.2,
            z: p.z,
            yaw: p.rng.range(0, Math.PI * 2),
            color: p.rng.pick(['#d8cdb4', '#c8b89c', '#bfc4c0'])
        });
    });
    addInstanced(ctx, 'huts', hutGeom, huts, {
        ao: { base: 0, height: 1.8, floor: 0.38, power: 0.7, sky: 0.28 }
    });

    buildFlags(ctx, seed ^ 0x0f06, ['#c23b3b', '#e8eaec', '#3d7042', '#c9d3d8']);

    // дальние вершины по горизонту
    buildPeaks(ctx, seed ^ 0x0f05);
}

/** Дальние горы: большие конусы со снежными шапками за туманом. */
function buildPeaks(ctx, seed) {
    const T = ctx.T;
    const rng = new Rng(seed >>> 0);
    let cx = 0,
        cz = 0,
        r = 0;
    for (let i = 0; i < T.count; i++) {
        cx += T.x[i];
        cz += T.z[i];
    }
    cx /= T.count;
    cz /= T.count;
    for (let i = 0; i < T.count; i++) {
        const dx = T.x[i] - cx,
            dz = T.z[i] - cz;
        const d = Math.sqrt(dx * dx + dz * dz);
        if (d > r) r = d;
    }
    const snow = toColor(ctx.pal.snow);
    const rockC = toColor(ctx.pal.rock[0]);
    const peak = solidify(new THREE.CylinderGeometry(0.0, 1.0, 1.0, 7, 2, false), rockC, function (x, y, z, i, c) {
        if (y > 0.27) c.copy(snow);
    });
    peak.applyMatrix4(trs(0, 0.5, 0, 0));

    const list = [];
    const n = ctx.Q.peaks;
    for (let k = 0; k < n; k++) {
        const a = (k / n) * Math.PI * 2 + rng.spread(0.14);
        const dist = r + rng.range(90, 280);
        const h = rng.range(45, 120);
        list.push({
            x: cx + Math.sin(a) * dist,
            y: ctx.sampler.groundY - 6,
            z: cz + Math.cos(a) * dist,
            yaw: rng.range(0, Math.PI * 2),
            sx: h * rng.range(1.1, 1.9),
            sy: h,
            sz: h * rng.range(1.1, 1.9)
        });
    }
    // Пики стоят за краем карты и за пределом тумана, но обязаны быть
    // видны силуэтом на небе — значит отсекаются только по пирамиде.
    addInstanced(ctx, 'peaks', peak, list, { far: true });
}

// ---------------------------------------------------------------------------
// Тема: промзона
// ---------------------------------------------------------------------------

function buildIndustrialTheme(ctx, seed) {
    const pal = ctx.pal;
    const d = ctx.density;
    const seg = ctx.Q.seg;

    // ангары: коробка со скатной крышей
    const hangar = mergeGeometries([
        primBox(1.0, 0.62, 1.0, WHITE, 0, 0.31, 0),
        primBox(1.04, 0.05, 1.04, toColor('#6d757b'), 0, 0.65, 0),
        gableRoof(1.04, 0.3, 1.04, toColor('#858d93'), toColor('#6d757b'), 0.66),
        primBox(0.34, 0.44, 0.05, toColor('#3a3f45'), 0, 0.22, 0.51)
    ]);
    const rngH = new Rng(seed ^ 0x1a01);
    const hangars = [];
    scatter(ctx, rngH, { step: 60 / d, jitter: 12, offMin: 14, offMax: 34, radius: 10, margin: 3, chance: 0.8 }, function (p) {
        hangars.push({
            x: p.x,
            y: p.y - 0.3,
            z: p.z,
            yaw: p.yaw + (p.rng.chance(0.5) ? Math.PI * 0.5 : 0),
            sx: p.rng.range(14, 26),
            sy: p.rng.range(7, 12),
            sz: p.rng.range(12, 20),
            color: p.rng.pick(pal.hangar)
        });
    });
    addInstanced(ctx, 'hangars', hangar, hangars, {
        ao: { base: 0, height: 0.22, floor: 0.36, power: 0.75, sky: 0.26 }
    });
    if (ctx.lights && hangars.length) {
        // ангар — тот же единичный куб, что и здание в городе, поэтому
        // сетка окон подходит без правок; рядов меньше, здание низкое
        addInstanced(ctx, 'windows',
            windowsGeometry(new Rng(seed ^ 0x1a07), 5, 2, ctx.lampK * 0.85),
            hangars, { material: emitMaterial(ctx), ignoreColor: true });
    }

    // цистерны
    const tank = mergeGeometries([
        primCyl(1.0, 1.0, 1.0, seg + 2, WHITE, 0, 0.5, 0),
        primCyl(0.62, 1.0, 0.2, seg + 2, toColor('#8e949a'), 0, 1.06, 0),
        primCyl(1.06, 1.06, 0.12, seg + 2, toColor('#6f757b'), 0, 0.06, 0),
        primBox(0.16, 1.0, 0.16, toColor('#6f757b'), 1.1, 0.5, 0)
    ]);
    const rngT = new Rng(seed ^ 0x1a02);
    const tanks = [];
    scatter(ctx, rngT, { step: 95 / d, jitter: 14, offMin: 10, offMax: 28, radius: 6, margin: 2.5, chance: 0.75 }, function (p) {
        const s = p.rng.range(3.2, 6.4);
        tanks.push({
            x: p.x,
            y: p.y - 0.2,
            z: p.z,
            yaw: p.rng.range(0, Math.PI * 2),
            sx: s,
            sy: s * p.rng.range(1.1, 1.9),
            sz: s,
            color: p.rng.pick(pal.tank)
        });
    });
    addInstanced(ctx, 'tanks', tank, tanks, {
        ao: { base: 0, height: 0.26, floor: 0.45, power: 0.7, sky: 0.3 }
    });

    // эстакады труб
    const pipeGeom = mergeGeometries(
        [
            primCyl(0.35, 0.35, 12.0, seg + 1, toColor('#9aa0a6')),
            primCyl(0.26, 0.26, 12.0, seg + 1, toColor('#b6a06a')),
            primBox(0.3, 3.4, 0.3, toColor('#6f757b'), 0, -1.7, -5.2),
            primBox(0.3, 3.4, 0.3, toColor('#6f757b'), 0, -1.7, 5.2)
        ],
        [
            new THREE.Matrix4().makeRotationX(Math.PI * 0.5).setPosition(0, 3.4, 0),
            new THREE.Matrix4().makeRotationX(Math.PI * 0.5).setPosition(0, 4.1, 0),
            trs(0, 3.4, 0, 0),
            trs(0, 3.4, 0, 0)
        ]
    );
    const rngP = new Rng(seed ^ 0x1a03);
    const pipes = [];
    scatter(ctx, rngP, { step: 130 / d, jitter: 16, offMin: 9, offMax: 20, radius: 6, margin: 2.5, chance: 0.7 }, function (p) {
        pipes.push({ x: p.x, y: p.y, z: p.z, yaw: p.yaw + p.rng.spread(0.2) });
    });
    addInstanced(ctx, 'pipes', pipeGeom, pipes, {
        ao: { base: 0, height: 1.9, floor: 0.46, power: 0.7, sky: 0.3 }
    });

    // контейнеры, иногда в два яруса
    const container = boxBands(7, 0.88, 1.04, true);
    const rngC = new Rng(seed ^ 0x1a04);
    const containers = [];
    scatter(ctx, rngC, { step: 34 / d, jitter: 8, offMin: 6.5, offMax: 24, radius: 3.4, margin: 1.6, chance: 0.8 }, function (p) {
        const stack = p.rng.chance(0.32) ? 2 : 1;
        for (let k = 0; k < stack; k++) {
            containers.push({
                x: p.x,
                y: p.y - 0.15 + k * 2.6,
                z: p.z,
                yaw: p.yaw + (p.rng.chance(0.25) ? Math.PI * 0.5 : 0) + p.rng.spread(0.06),
                sx: 2.5,
                sy: 2.6,
                sz: 6.1,
                color: p.rng.pick(pal.container)
            });
        }
    });
    addInstanced(ctx, 'containers', container, containers, {
        ao: { base: 0, height: 0.32, floor: 0.4, power: 0.75, sky: 0.28 }
    });

    // бочки: мелочь у оснований, за которую цепляется глаз
    const rngB = new Rng(seed ^ 0x1a07);
    const barrels = [];
    scatter(ctx, rngB, { step: 21 / d, jitter: 5, offMin: 4.5, offMax: 16, radius: 0.5, margin: 1.0, chance: 0.6 }, function (p) {
        const n = p.rng.int(1, 3);
        for (let k = 0; k < n; k++) {
            barrels.push({
                x: p.x + p.rng.spread(0.7),
                y: p.y,
                z: p.z + p.rng.spread(0.7),
                yaw: p.rng.range(0, Math.PI * 2),
                color: p.rng.pick(['#bf5b32', '#3d6f8e', '#c9a63c', '#6f757b'])
            });
        }
    });
    addInstanced(ctx, 'barrels', barrelGeometry(Math.max(5, seg), '#ffffff'), barrels, {
        ao: { base: 0, height: 0.7, floor: 0.46, power: 0.7, sky: 0.3 }
    });

    // деревья по краю промзоны
    const rngTr = new Rng(seed ^ 0x1a08);
    const trees = [];
    scatter(ctx, rngTr, { step: 24 / d, jitter: 5, offMin: 5, offMax: 22, offBias: 1.3, radius: 1.6, margin: 1.3, chance: 0.5 }, function (p) {
        const sc = p.rng.range(0.75, 1.25);
        trees.push({
            x: p.x, y: p.y - 0.1, z: p.z,
            yaw: p.rng.range(0, Math.PI * 2),
            sx: sc, sy: sc * p.rng.range(0.9, 1.2), sz: sc,
            color: tintVariation(p.rng)
        });
    });
    addInstanced(ctx, 'cityTrees', broadleafGeometry(seg, '#57493a', '#5d7a45'), trees, {
        material: swayMaterial(ctx, 'tree'),
        ao: { base: 0, height: 1.7, floor: 0.42, power: 0.6, sky: 0.34, down: 0.7 }
    });

    // сетчатый забор: одна текстурированная плоскость на секцию
    buildChainFence(ctx, seed ^ 0x1a05);

    // отбойники у самой кромки
    const rngG = new Rng(seed ^ 0x1a06);
    const rails = [];
    scatter(ctx, rngG, { step: 6.0, jitter: 0, offMin: 1.5, offMax: 1.5, radius: 0.4, margin: 0.4, chance: 0.6 }, function (p) {
        rails.push({ x: p.x, y: p.y, z: p.z, yaw: p.yaw });
    });
    addInstanced(ctx, 'guardrails', guardrailGeometry(pal), rails, {
        ao: { base: 0, height: 0.75, floor: 0.44, power: 0.7, sky: 0.3 }
    });

    buildFlags(ctx, seed ^ 0x1a09, ['#c95f28', '#d9b93c', '#3d6f8e', '#dcdcd4']);
}

/**
 * Сетка-рабица: единственное место, где нужна текстура. Файлов нет — рисуем
 * её в canvas, включаем alphaTest, и вся ограда остаётся одним draw call.
 */
function buildChainFence(ctx, seed) {
    const cnv = document.createElement('canvas');
    cnv.width = 64;
    cnv.height = 64;
    const g = cnv.getContext('2d');
    g.clearRect(0, 0, 64, 64);
    g.strokeStyle = '#cdd2d8';
    g.lineWidth = 1.6;
    g.beginPath();
    for (let i = -64; i <= 128; i += 9) {
        g.moveTo(i, 0);
        g.lineTo(i + 64, 64);
        g.moveTo(i, 64);
        g.lineTo(i + 64, 0);
    }
    g.stroke();
    g.fillStyle = '#aeb4ba';
    g.fillRect(0, 0, 4, 64);
    g.fillRect(60, 0, 4, 64);
    g.fillRect(0, 0, 64, 4);

    const tex = new THREE.CanvasTexture(cnv);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.wrapS = THREE.RepeatWrapping;
    tex.wrapT = THREE.ClampToEdgeWrapping;
    tex.repeat.set(2, 1);
    tex.magFilter = THREE.LinearFilter;
    tex.minFilter = THREE.LinearMipmapLinearFilter;

    const mat = new THREE.MeshLambertMaterial({
        map: tex,
        vertexColors: true,
        flatShading: true,
        alphaTest: 0.5,
        side: THREE.DoubleSide
    });
    mat.name = 'chainFence';
    ctx.materials.push(mat);

    const plane = new THREE.PlaneGeometry(4.0, 2.2);
    plane.translate(0, 1.1, 0);
    plane.rotateY(Math.PI * 0.5); // полотно вдоль оси Z, как и отбойники
    const cols = new Float32Array(plane.attributes.position.count * 3).fill(1);
    plane.setAttribute('color', new THREE.BufferAttribute(cols, 3));

    const rng = new Rng(seed >>> 0);
    const list = [];
    scatter(ctx, rng, { step: 4.0, jitter: 0, offMin: 5.5, offMax: 5.5, radius: 0.5, margin: 0.5, chance: 0.55 }, function (p) {
        list.push({ x: p.x, y: p.y, z: p.z, yaw: p.yaw });
    });
    if (!list.length) {
        plane.dispose();
        mat.dispose();
        return;
    }
    if (ctx.ao) bakeContactAO(plane, { base: 0, height: 1.2, floor: 0.5, power: 0.7, sky: 0.2 });
    const mesh = new THREE.InstancedMesh(plane, mat, list.length);
    mesh.name = 'chainFence';
    mesh.frustumCulled = false;
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const pos = new THREE.Vector3();
    const scl = new THREE.Vector3(1, 1, 1);
    const axis = new THREE.Vector3(0, 1, 0);
    for (let i = 0; i < list.length; i++) {
        q.setFromAxisAngle(axis, list[i].yaw);
        pos.set(list[i].x, list[i].y, list[i].z);
        m.compose(pos, q, scl);
        mesh.setMatrixAt(i, m);
    }
    mesh.instanceMatrix.needsUpdate = true;
    ctx.group.add(mesh);
    ctx.instances.chainFence = mesh;
}

// ---------------------------------------------------------------------------
// Общее: флаги, трибуны, зрители, конусы
// ---------------------------------------------------------------------------

/**
 * Флаги на мачтах вдоль кромки. Полотнище качает вершинный шейдер — это
 * самая заметная «жизнь» в кадре и для процессора она стоит ноль.
 */
function buildFlags(ctx, seed, colors) {
    const rng = new Rng(seed >>> 0);
    const list = [];
    scatter(ctx, rng, { step: 46 / ctx.density, jitter: 7, offMin: 2.6, offMax: 5.5, radius: 0.6, margin: 0.8, chance: 0.5 }, function (p) {
        list.push({
            x: p.x,
            y: p.y,
            z: p.z,
            // полотнище смотрит поперёк трассы, чтобы его было видно с дороги
            yaw: p.yaw + (p.side > 0 ? Math.PI * 0.5 : Math.PI * 1.5),
            sy: p.rng.range(0.88, 1.15),
            color: p.rng.pick(colors)
        });
    });
    addInstanced(ctx, 'flags', flagGeometry(ctx.Q.seg, '#2c3036', '#ffffff'), list, {
        material: swayMaterial(ctx, 'flag'),
        ao: { base: 0, height: 1.6, floor: 0.5, power: 0.6, sky: 0.22 }
    });
}

function buildGrandstands(ctx, seed) {
    const rng = new Rng(seed >>> 0);
    const T = ctx.T;
    const list = [];
    // две трибуны по сторонам стартовой прямой
    for (let side = -1; side <= 1; side += 2) {
        for (let k = 0; k < 2; k++) {
            const s = 14 + k * 20;
            const i = Math.round(s / T.step) % T.count;
            const off = 7.5;
            const lat = (T.hw[i] + off) * side;
            const x = T.x[i] + T.nx[i] * lat;
            const z = T.z[i] + T.nz[i] * lat;
            if (ctx.clearance(x, z) < 6) continue;
            ctx.occupancy(x, z, 11);
            list.push({
                x: x,
                y: ctx.sampler(i, off, side < 0 ? 1 : 0) - 0.1,
                z: z,
                yaw: trackYaw(T, i) + (side > 0 ? Math.PI * 1.5 : Math.PI * 0.5)
            });
        }
    }
    addInstanced(ctx, 'stands', standGeometry(ctx.pal), list, {
        // ниши под ступенями и задняя стенка тонут в тени, верхние ряды светлее
        ao: { base: 0, height: 2.8, floor: 0.32, power: 0.8, sky: 0.34, down: 0.5 }
    });
    ctx._standSeats = list;
    ctx._standRng = rng;
}

function buildCrowd(ctx, seed) {
    const rng = new Rng(seed >>> 0);
    const list = [];
    const stands = ctx._standSeats || [];
    const cos = Math.cos,
        sin = Math.sin;
    // зрители на ступенях трибун
    for (let k = 0; k < stands.length; k++) {
        const st = stands[k];
        const rows = Math.max(2, Math.round(4 * ctx.density));
        for (let r = 0; r < rows; r++) {
            const count = Math.round(14 * ctx.density) + 4;
            for (let c = 0; c < count; c++) {
                if (!rng.chance(0.72)) continue;
                const lx = (c / (count - 1) - 0.5) * 17.0 + rng.spread(0.25);
                const lz = -r * 1.15 + 0.1;
                const ly = 0.72 + r * 0.72;
                list.push({
                    x: st.x + lx * cos(st.yaw) + lz * sin(st.yaw),
                    y: st.y + ly,
                    z: st.z - lx * sin(st.yaw) + lz * cos(st.yaw),
                    yaw: st.yaw + Math.PI + rng.spread(0.3),
                    sy: rng.range(0.9, 1.1),
                    color: rng.pick(ctx.pal.crowd)
                });
            }
        }
    }
    // редкие зрители вдоль остальной трассы
    scatter(ctx, rng, { step: 34 / ctx.density, jitter: 8, offMin: 5, offMax: 11, radius: 0.6, margin: 1.2, chance: 0.22 }, function (p) {
        const n = p.rng.int(1, 3);
        for (let k = 0; k < n; k++) {
            list.push({
                x: p.x + p.rng.spread(1.6),
                y: p.y,
                z: p.z + p.rng.spread(1.6),
                yaw: p.yaw + Math.PI * 0.5 + p.rng.spread(0.5),
                sy: p.rng.range(0.88, 1.12),
                color: p.rng.pick(ctx.pal.crowd)
            });
        }
    });
    addInstanced(ctx, 'spectators', spectatorGeometry(), list, {
        material: swayMaterial(ctx, 'crowd'),
        ao: { base: 0, height: 1.1, floor: 0.6, power: 0.8, sky: 0.22 }
    });
}

function buildCones(ctx, seed) {
    const rng = new Rng(seed >>> 0);
    const T = ctx.T;
    const list = [];
    // конусы ставим там, где трасса заметно поворачивает: по внутренней кромке
    const look = Math.max(2, Math.round(14 / T.step));
    for (let i = 0; i < T.count; i += Math.max(1, Math.round(6 / ctx.density))) {
        const j = (i + look) % T.count;
        const cross = T.tx[i] * T.tz[j] - T.tz[i] * T.tx[j];
        if (Math.abs(cross) < 0.22) continue;
        // знак векторного произведения касательных даёт сторону поворота;
        // конусы ставим по внутренней кромке — там, где режут апекс
        const side = cross > 0 ? -1 : 1;
        const off = rng.range(0.9, 1.7);
        const lat = (T.hw[i] + off) * side;
        const x = T.x[i] + T.nx[i] * lat;
        const z = T.z[i] + T.nz[i] * lat;
        if (ctx.clearance(x, z) < 0.6) continue;
        list.push({
            x: x,
            y: ctx.sampler(i, off, side < 0 ? 1 : 0),
            z: z,
            yaw: rng.range(0, Math.PI * 2),
            sy: rng.range(0.9, 1.1)
        });
    }
    addInstanced(ctx, 'cones', coneGeometry(ctx.pal, Math.max(5, ctx.Q.seg)), list, {
        ao: { base: 0, height: 0.45, floor: 0.5, power: 0.7, sky: 0.26 }
    });
}

// ---------------------------------------------------------------------------
// Небо
// ---------------------------------------------------------------------------

/**
 * Градиентная полусфера на вершинных цветах. Материал неосвещаемый: Lambert
 * подмешал бы к нему направленный свет и градиент поплыл бы. Туман для купола
 * выключен, иначе небо схлопнется в один цвет тумана.
 */
/** Гладкая ступенька 0..1 на отрезке [0, edge]. */
function smoothFade(t, edge) {
    if (t >= edge) return 1;
    const u = t / edge;
    return u * u * (3 - 2 * u);
}

function buildSkyDome(env, Q) {
    const seg = Q.skySeg;
    const geom = new THREE.SphereGeometry(900, seg, Math.max(6, seg >> 1), 0, Math.PI * 2, 0, Math.PI * 0.56);
    const g = geom.index ? geom.toNonIndexed() : geom;
    for (const name of Object.keys(g.attributes)) {
        if (name !== 'position' && name !== 'normal') g.deleteAttribute(name);
    }
    const zen = toColor(env.zenith);
    const hor = toColor(env.horizon);
    const fog = toColor(env.fog);
    paintVertices(g, function (x, y, z, i, c) {
        const t = Math.min(1, Math.max(0, y / 900));
        const k = Math.pow(t, 0.55);
        let r = hor.r + (zen.r - hor.r) * k;
        let gg = hor.g + (zen.g - hor.g) * k;
        let b = hor.b + (zen.b - hor.b) * k;
        // Самая нижняя полоса купола растворяется в цвете тумана.
        //
        // Это не украшательство, а условие корректности отсечения по
        // дальности: объект за чертой тумана закрашен цветом тумана
        // ЦЕЛИКОМ, и если он торчит над линией горизонта, то на фоне неба
        // он всё-таки виден силуэтом. Совпадающий с туманом горизонт делает
        // такой силуэт неотличимым от неба — и отсечение перестаёт что-либо
        // менять в картинке. Замерено: без этой полосы сравнение кадров
        // «с отсечением» и «без» расходилось на 0,004 % пикселей у самого
        // горизонта, с ней — ровно ноль.
        const fk = smoothFade(t, 0.14);
        r = fog.r + (r - fog.r) * fk;
        gg = fog.g + (gg - fog.g) * fk;
        b = fog.b + (b - fog.b) * fk;
        c.setRGB(r, gg, b);
    });
    const mat = new THREE.MeshBasicMaterial({
        vertexColors: true,
        side: THREE.BackSide,
        fog: false,
        depthWrite: false
    });
    mat.name = 'skyDome';
    const mesh = new THREE.Mesh(g, mat);
    mesh.name = 'skyDome';
    mesh.frustumCulled = false;
    mesh.renderOrder = -10;
    return mesh;
}

/** Плоские облака: несколько сплюснутых кластеров одной слитой геометрией. */
function buildClouds(env, Q, rng, share) {
    const b = new MeshBuilder();
    const base = toColor(env.cloud);
    const shadow = new THREE.Color(base.r * 0.82, base.g * 0.85, base.b * 0.9);
    const n = Math.max(1, Math.round(Q.clouds * (share === undefined ? 1 : share)));
    for (let k = 0; k < n; k++) {
        const a = (k / n) * Math.PI * 2 + rng.spread(0.4);
        const dist = rng.range(280, 620);
        const cx = Math.sin(a) * dist;
        const cz = Math.cos(a) * dist;
        const cy = rng.range(130, 210);
        const blobs = 3 + Math.floor(rng.next() * 3);
        for (let i = 0; i < blobs; i++) {
            const w = rng.range(50, 130);
            const d = rng.range(30, 80);
            const ox = rng.spread(70);
            const oz = rng.spread(45);
            const oy = rng.spread(12);
            const c = i === 0 ? base : shadow;
            b.quad(
                [cx + ox - w, cy + oy, cz + oz - d],
                [cx + ox - w, cy + oy, cz + oz + d],
                [cx + ox + w, cy + oy, cz + oz + d],
                [cx + ox + w, cy + oy, cz + oz - d],
                c
            );
            b.quad(
                [cx + ox + w * 0.8, cy + oy + 14, cz + oz - d * 0.7],
                [cx + ox + w * 0.8, cy + oy + 14, cz + oz + d * 0.7],
                [cx + ox - w * 0.8, cy + oy + 14, cz + oz + d * 0.7],
                [cx + ox - w * 0.8, cy + oy + 14, cz + oz - d * 0.7],
                c
            );
        }
    }
    const mat = new THREE.MeshBasicMaterial({
        vertexColors: true,
        fog: false,
        transparent: true,
        opacity: 0.9,
        depthWrite: false,
        side: THREE.DoubleSide,
        // 12.11: прозрачный двусторонний материал рисуется В ДВА ПРОХОДА.
        // Одна строка возвращает в бюджет целый draw call.
        forceSinglePass: true
    });
    mat.name = 'clouds';
    const mesh = new THREE.Mesh(b.build(), mat);
    mesh.name = 'clouds';
    mesh.frustumCulled = false;
    mesh.renderOrder = -9;
    return mesh;
}


// ---------------------------------------------------------------------------
// Ночное небо: звёзды и луна
// ---------------------------------------------------------------------------

/**
 * Звёздное небо одной геометрией: маленькие квадраты, развёрнутые к центру
 * купола, плюс диск луны с ореолом. Один меш, один draw call, ни одной
 * текстуры-файла. Днём не строится вовсе.
 */
function buildStars(Q, rng, tod) {
    const b = new MeshBuilder();
    const n = Math.max(12, Math.round(Q.stars * tod.stars));
    const R = 860;
    const c = new THREE.Color();

    // базис квадрата, перпендикулярного направлению на звезду
    const ux = [0, 0, 0];
    const vx = [0, 0, 0];
    const pa = [0, 0, 0], pb = [0, 0, 0], pc = [0, 0, 0], pd = [0, 0, 0];

    function quadAt(dx, dy, dz, size, color, edge) {
        // орт «вправо» — векторное произведение направления и оси Y
        let rx = dz, ry = 0, rz = -dx;
        let l = Math.sqrt(rx * rx + rz * rz);
        if (l < 1e-6) { rx = 1; rz = 0; l = 1; }
        rx /= l; rz /= l;
        // орт «вверх» — направление на звезду, векторно на «вправо»
        const uxx = dy * rz - dz * 0;
        const uyy = dz * rx - dx * rz;
        const uzz = dx * 0 - dy * rx;
        const ul = Math.sqrt(uxx * uxx + uyy * uyy + uzz * uzz) || 1;
        ux[0] = rx * size; ux[1] = 0; ux[2] = rz * size;
        vx[0] = (uxx / ul) * size; vx[1] = (uyy / ul) * size; vx[2] = (uzz / ul) * size;
        const cx = dx * R, cy = dy * R, cz = dz * R;
        pa[0] = cx - ux[0] - vx[0]; pa[1] = cy - ux[1] - vx[1]; pa[2] = cz - ux[2] - vx[2];
        pb[0] = cx + ux[0] - vx[0]; pb[1] = cy + ux[1] - vx[1]; pb[2] = cz + ux[2] - vx[2];
        pc[0] = cx + ux[0] + vx[0]; pc[1] = cy + ux[1] + vx[1]; pc[2] = cz + ux[2] + vx[2];
        pd[0] = cx - ux[0] + vx[0]; pd[1] = cy - ux[1] + vx[1]; pd[2] = cz - ux[2] + vx[2];
        if (edge) b.quadVC(pa, pb, pc, pd, edge, edge, edge, edge);
        else b.quad(pa, pb, pc, pd, color);
    }

    for (let k = 0; k < n; k++) {
        // равномерно по верхней полусфере, но гуще к зениту: у горизонта
        // звёзды всё равно съест туман
        const u = rng.range(0.06, 1.0);
        const y = Math.pow(u, 0.7);
        const r = Math.sqrt(Math.max(0, 1 - y * y));
        const a = rng.range(0, Math.PI * 2);
        const dx = Math.cos(a) * r;
        const dz = Math.sin(a) * r;
        const bright = rng.range(0.35, 1.0);
        const warm = rng.next() < 0.25;
        c.setRGB(bright * (warm ? 1.0 : 0.82), bright * 0.9, bright * (warm ? 0.78 : 1.0));
        quadAt(dx, y, dz, rng.range(1.4, 3.6) + bright * 2.2, c, null);
    }

    // луна: диск и мягкий ореол вокруг
    if (tod.moon > 0.01) {
        const ma = 2.15;
        const my = 0.46;
        const mr = Math.sqrt(Math.max(0, 1 - my * my));
        const mx = Math.cos(ma) * mr;
        const mz = Math.sin(ma) * mr;
        const halo = new THREE.Color(0.13 * tod.moon, 0.16 * tod.moon, 0.24 * tod.moon);
        quadAt(mx, my, mz, 62, halo, halo);
        const mid = new THREE.Color(0.3 * tod.moon, 0.33 * tod.moon, 0.42 * tod.moon);
        quadAt(mx, my, mz, 34, mid, mid);
        const disc = new THREE.Color(0.93 * tod.moon, 0.94 * tod.moon, 0.88 * tod.moon);
        quadAt(mx, my, mz, 17, disc, disc);
        // кратер: чуть тусклее, сдвинут от центра
        const crater = new THREE.Color(0.74 * tod.moon, 0.76 * tod.moon, 0.72 * tod.moon);
        quadAt(mx + 0.006, my + 0.004, mz - 0.003, 5, crater, crater);
    }

    const mat = new THREE.MeshBasicMaterial({
        vertexColors: true,
        fog: false,
        transparent: true,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        side: THREE.DoubleSide,
        // 12.11: прозрачный двусторонний материал иначе рисуется в два прохода
        forceSinglePass: true
    });
    mat.name = 'stars';
    const mesh = new THREE.Mesh(b.build(), mat);
    mesh.name = 'stars';
    mesh.frustumCulled = false;
    mesh.renderOrder = -8;
    return mesh;
}

// ---------------------------------------------------------------------------
// Ночные огни: конусы фонарей, окна, подсветка щитов
// ---------------------------------------------------------------------------
//
// Никакой постобработки и никаких источников света сверх одного ambient и
// одного directional (раздел 10.3) — свет рисуется ГЕОМЕТРИЕЙ:
//   * конус под плафоном и пятно на асфальте — аддитивные, гаснут к краю
//     вершинными цветами (чёрное в аддитивном смешивании невидимо);
//   * окна и полотна щитов — обычный MeshBasicMaterial, то есть НЕ зависят
//     от освещения: ночью они и должны быть единственным, что светится само.
// Обе группы — такие же InstancedMesh, как весь декор, и отсекаются вместе
// с ним по тем же корзинам.

/** Общий аддитивный материал ореолов, один на сцену. */
function glowMaterial(ctx) {
    if (!ctx.glowMat) {
        const m = new THREE.MeshBasicMaterial({
            vertexColors: true,
            transparent: true,
            blending: THREE.AdditiveBlending,
            depthWrite: false,
            fog: true,
            side: THREE.DoubleSide,
            forceSinglePass: true
        });
        m.name = 'sceneryGlow';
        fadeAdditiveFog(m, 'sceneryGlow');
        ctx.glowMat = m;
        ctx.materials.push(m);
    }
    return ctx.glowMat;
}

/** Общий неосвещаемый материал самосветящихся поверхностей (окна, щиты). */
function emitMaterial(ctx) {
    if (!ctx.emitMat) {
        // DoubleSide здесь ничего не стоит: материал непрозрачный, второго
        // прохода не будет (12.11 — про прозрачные), зато окно на любой
        // грани коробки гарантированно видно, как бы ни легла его намотка
        const m = new THREE.MeshBasicMaterial({
            vertexColors: true,
            fog: true,
            side: THREE.DoubleSide
        });
        m.name = 'sceneryEmissive';
        ctx.emitMat = m;
        ctx.materials.push(m);
    }
    return ctx.emitMat;
}

/**
 * Свет уличного фонаря: конус от плафона к земле плюс пятно на асфальте.
 * Геометрия строится в тех же локальных координатах, что lampGeometry:
 * плафон висит на кронштейне в точке (0.78, 5.9).
 */
function lampGlowGeometry(seg, k) {
    const b = new MeshBuilder();
    // Граней вдвое больше, чем у самого фонаря: восьмигранный конус света
    // читается как пирамида, а не как луч. Треугольники тут дешёвые —
    // фонарей в кадре десятки, а не тысячи.
    const n = Math.max(10, seg * 2);
    const cx = 0.78;
    const yTop = 5.84;
    const rTop = 0.34;
    const rBot = 3.8;
    const yBot = 0.07;
    // Яркость подобрана по ночному кадру и она НАМНОГО меньше, чем кажется
    // по числам. Причин две: стенок у конуса две (материал двусторонний,
    // обе прибавляются), и цвет здесь линейный — на экран он выходит через
    // sRGB, где 0,06 линейных это уже четверть шкалы. Первая версия с 0,55
    // давала белую пирамиду вместо луча.
    const hot = new THREE.Color(0.0082 * k, 0.0071 * k, 0.0047 * k);
    // пятно на асфальте, наоборот, должно читаться: односторонняя плоскость
    const mid = new THREE.Color(0.21 * k, 0.18 * k, 0.115 * k);
    const zero = new THREE.Color(0, 0, 0);
    const a = [0, 0, 0], b2 = [0, 0, 0], c2 = [0, 0, 0], d2 = [0, 0, 0];

    // Две вложенные оболочки вместо одной. Луч зрения через середину конуса
    // пересекает четыре стенки, через край — две, и шахта получает мягкий
    // поперечный градиент. Одна оболочка светится ровно до самого силуэта и
    // читается плоской пирамидой; это дешевле любого шейдера.
    const shells = [1.0, 0.52];
    for (let sh = 0; sh < shells.length; sh++) {
        const kr = shells[sh];
        for (let i = 0; i < n; i++) {
            const t0 = (i / n) * Math.PI * 2;
            const t1 = ((i + 1) / n) * Math.PI * 2;
            a[0] = cx + Math.cos(t0) * rTop * kr; a[1] = yTop; a[2] = Math.sin(t0) * rTop * kr;
            b2[0] = cx + Math.cos(t1) * rTop * kr; b2[1] = yTop; b2[2] = Math.sin(t1) * rTop * kr;
            c2[0] = cx + Math.cos(t1) * rBot * kr; c2[1] = yBot; c2[2] = Math.sin(t1) * rBot * kr;
            d2[0] = cx + Math.cos(t0) * rBot * kr; d2[1] = yBot; d2[2] = Math.sin(t0) * rBot * kr;
            b.quadVC(a, b2, c2, d2, hot, hot, zero, zero);
        }
    }

    // пятно на земле: веер от яркой середины к нулю по краю
    const centre = [cx, yBot, 0];
    const p1 = [0, 0, 0], p2 = [0, 0, 0];
    for (let i = 0; i < n; i++) {
        const t0 = (i / n) * Math.PI * 2;
        const t1 = ((i + 1) / n) * Math.PI * 2;
        p1[0] = cx + Math.cos(t0) * rBot; p1[1] = yBot; p1[2] = Math.sin(t0) * rBot;
        p2[0] = cx + Math.cos(t1) * rBot; p2[1] = yBot; p2[2] = Math.sin(t1) * rBot;
        b.triVC(centre, p1, p2, mid, zero, zero);
    }

    // Само пятно плафона — здесь же, в этой геометрии: отдельный меш стоил бы
    // целый draw call ради восьми треугольников. Аддитивный октаэдр читается
    // как горящая лампа с любой стороны.
    const bulb = new THREE.Color(0.62 * k, 0.52 * k, 0.33 * k);
    const oct = [
        [0, 0.55, 0], [0.55, 0, 0], [0, 0, 0.55], [-0.55, 0, 0], [0, 0, -0.55], [0, -0.55, 0]
    ];
    for (let i = 0; i < 4; i++) {
        const q = oct[1 + i];
        const r = oct[1 + ((i + 1) & 3)];
        b.tri([cx + oct[0][0], yTop + 0.06 + oct[0][1], oct[0][2]],
            [cx + q[0], yTop + 0.06 + q[1], q[2]],
            [cx + r[0], yTop + 0.06 + r[1], r[2]], bulb);
        b.tri([cx + oct[5][0], yTop + 0.06 + oct[5][1], oct[5][2]],
            [cx + r[0], yTop + 0.06 + r[1], r[2]],
            [cx + q[0], yTop + 0.06 + q[1], q[2]], bulb);
    }
    return b.build();
}

/**
 * Светящиеся окна на гранях единичного куба (здания строятся именно так).
 * Рисунок детерминирован: тот же seed — те же горящие окна у всех клиентов.
 */
function windowsGeometry(rng, cols, rows, k) {
    const b = new MeshBuilder();
    const c = new THREE.Color();
    const warm = [
        [1.0, 0.86, 0.55],
        [1.0, 0.92, 0.72],
        [0.86, 0.9, 1.0],
        [1.0, 0.74, 0.42]
    ];
    const wq = [0, 0, 0], wb = [0, 0, 0], wc = [0, 0, 0], wd = [0, 0, 0];
    const off = 0.503;
    const wq2 = 0.62 / cols;   // ширина окна в долях грани
    const hq = 0.66 / rows;

    for (let face = 0; face < 4; face++) {
        for (let r = 0; r < rows; r++) {
            for (let cix = 0; cix < cols; cix++) {
                if (rng.next() > 0.42) continue;
                const tint = warm[(rng.next() * warm.length) | 0];
                const bright = rng.range(0.55, 1.0) * k;
                c.setRGB(Math.min(1, tint[0] * bright), Math.min(1, tint[1] * bright), Math.min(1, tint[2] * bright));
                const u0 = -0.5 + (cix + 0.5) / cols - wq2 * 0.5;
                const u1 = u0 + wq2;
                const y0 = 0.06 + (r + 0.5) * ((1 - 0.12) / rows) - hq * 0.5;
                const y1 = y0 + hq;
                if (face === 0) {        // +Z
                    wq[0] = u0; wq[1] = y0; wq[2] = off;
                    wb[0] = u1; wb[1] = y0; wb[2] = off;
                    wc[0] = u1; wc[1] = y1; wc[2] = off;
                    wd[0] = u0; wd[1] = y1; wd[2] = off;
                } else if (face === 1) { // -Z
                    wq[0] = u1; wq[1] = y0; wq[2] = -off;
                    wb[0] = u0; wb[1] = y0; wb[2] = -off;
                    wc[0] = u0; wc[1] = y1; wc[2] = -off;
                    wd[0] = u1; wd[1] = y1; wd[2] = -off;
                } else if (face === 2) { // +X
                    wq[0] = off; wq[1] = y0; wq[2] = -u0;
                    wb[0] = off; wb[1] = y0; wb[2] = -u1;
                    wc[0] = off; wc[1] = y1; wc[2] = -u1;
                    wd[0] = off; wd[1] = y1; wd[2] = -u0;
                } else {                 // -X
                    wq[0] = -off; wq[1] = y0; wq[2] = u0;
                    wb[0] = -off; wb[1] = y0; wb[2] = u1;
                    wc[0] = -off; wc[1] = y1; wc[2] = u1;
                    wd[0] = -off; wd[1] = y1; wd[2] = u0;
                }
                b.quad(wq, wb, wc, wd, c);
            }
        }
    }
    return b.build();
}

/** Подсвеченное полотно рекламного щита (координаты billboardGeometry). */
function billboardGlowGeometry(k) {
    const b = new MeshBuilder();
    const face = new THREE.Color(0.95 * k, 0.92 * k, 0.85 * k);
    const a = [-2.35, 3.86, 0.17];
    const b2 = [2.35, 3.86, 0.17];
    const c2 = [2.35, 6.14, 0.17];
    const d2 = [-2.35, 6.14, 0.17];
    b.quad(a, b2, c2, d2, face);
    return b.build();
}

export default buildScenery;
