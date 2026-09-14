/**
 * trackmesh.js — [render] построение статической геометрии трассы.
 *
 * Экспортирует buildTrackMeshes(track, theme, quality) из контракта (раздел 3).
 * Вход — данные формата 12.1 (плоские массивы x, y, z, tx, tz, nx, nz, hw, s),
 * либо экземпляр клиентского Track из js/track.js (те же массивы под именами
 * cx, cy, cz, ctx, ctz, cnx, cnz, chw, cs), либо список samples из 7.2.
 *
 * Бюджет (раздел 1): шесть мешей на всю трассу.
 *
 *   road      полотно, вершинные цвета с вариацией
 *   kerbs     бордюры обеих кромок, красно-белые посегментно
 *   markings  осевая прерывистая, кромочные, стартовая клетка
 *   terrain   лента рельефа ~50 м с каждой стороны
 *   ground    общий грунт до горизонта (два треугольника)
 *   arch      стартовая арка
 *
 * ОТСЕЧЕНИЕ. Первые четыре меша нарезаны на сегменты по дуге (~180 м) и в
 * кадре рисуются только видимыми кусками: 1–4 вызова на меш вместо одного,
 * но вместо всего кольца — только то, что попадает в пирамиду видимости и
 * ближе тумана. На длинной трассе это кратное падение треугольников.
 * Подробности и обоснование — у SegmentedSurface ниже.
 *
 * Разметка приподнята на 15 мм над полотном И имеет polygonOffset — на
 * Intel UHD этого достаточно, чтобы z-fighting не появлялся на дистанции.
 */

import * as THREE from 'three';
import {
    Rng,
    loopNoise,
    MeshBuilder,
    mergeGeometries,
    surfaceGrid,
    extrudeProfile,
    shade,
    mixColor,
    toColor,
    bakeContactAO,
    disposeObject,
    countTriangles,
    countDrawCalls,
    rangeSphere
} from './geomutil.js';

// ---------------------------------------------------------------------------
// Пресеты качества: реально меняют плотность сетки
// ---------------------------------------------------------------------------

const QUALITY = {
    low: { roadCols: 4, terrainRings: 4, terrainStride: 3, kerbSide: false, edgeLines: true, archDetail: 0 },
    medium: { roadCols: 6, terrainRings: 6, terrainStride: 2, kerbSide: true, edgeLines: true, archDetail: 1 },
    high: { roadCols: 8, terrainRings: 8, terrainStride: 1, kerbSide: true, edgeLines: true, archDetail: 2 }
};

// ---------------------------------------------------------------------------
// Запечённое затенение (задача «AO в вершины»)
// ---------------------------------------------------------------------------
//
// Считается один раз при построении и живёт в атрибуте color. В кадре стоит
// ноль. Полосу вдоль кромки полотна даёт вершинный градиент: узлы сетки у
// бордюра темнее, к середине дороги затенение сходит на нет.

const AO_EDGE_DEPTH = 0.46;   // насколько темнеет асфальт у самого бордюра
const AO_EDGE_WIDTH = 2.2;    // м, ширина тёмной полосы вдоль кромки
const AO_SHOULDER = 0.50;     // насколько темнеет обочина у кромки
const AO_SHOULDER_W = 5.0;    // м, на каком удалении затенение обочины сходит
const AO_DITCH = 0.22;        // добавка за провал рельефа ниже полотна

/** Гладкая ступенька smoothstep(0, 1, t). */
function smooth01(t) {
    if (t <= 0) return 0;
    if (t >= 1) return 1;
    return t * t * (3 - 2 * t);
}

/** Множитель затенения асфальта: dist — расстояние от кромки полотна, м. */
function roadEdgeAO(dist) {
    return 1 - AO_EDGE_DEPTH * smooth01(1 - dist / AO_EDGE_WIDTH);
}

// ---------------------------------------------------------------------------
// Палитры тем
// ---------------------------------------------------------------------------

const THEMES = {
    city: {
        asphalt: '#3b3e45',
        asphaltDark: '#33363c',
        line: '#e7ebf0',
        kerbA: '#c8373d',
        kerbB: '#e4e7ea',
        kerbSide: '#4a4d53',
        shoulder: '#6d6f6a',
        groundNear: '#5f6b48',
        groundFar: '#586344',
        rock: '#787a74',
        archBody: '#2c3038',
        archTrim: '#d8a13a',
        terrainDrop: 5.0,
        terrainRise: 1.2,
        roughness: 0.55
    },
    mountain: {
        asphalt: '#383b41',
        asphaltDark: '#2f3238',
        line: '#eef1f5',
        kerbA: '#c23b3b',
        kerbB: '#e8eaec',
        kerbSide: '#43464b',
        shoulder: '#6a6353',
        groundNear: '#4d6038',
        groundFar: '#42502f',
        rock: '#6e675b',
        archBody: '#3a3128',
        archTrim: '#c9d3d8',
        terrainDrop: 7.0,
        terrainRise: 9.0,
        roughness: 1.0
    },
    industrial: {
        asphalt: '#41434a',
        asphaltDark: '#383a40',
        line: '#e4e2d6',
        kerbA: '#c95f28',
        kerbB: '#dcdcd4',
        kerbSide: '#4c4e53',
        shoulder: '#77746a',
        groundNear: '#6b6a58',
        groundFar: '#5e5d4d',
        rock: '#7b7870',
        archBody: '#43474d',
        archTrim: '#d9b93c',
        terrainDrop: 3.5,
        terrainRise: 2.0,
        roughness: 0.7
    }
};

const TERRAIN_WIDTH = 50.0; // ширина ленты рельефа с каждой стороны, м

// ---------------------------------------------------------------------------
// Время суток на полотне
// ---------------------------------------------------------------------------
//
// Само освещение задаёт scenery.js. Здесь правится только то, что запечено
// в вершины: асфальт и трава к ночи уходят в холодную темноту, а разметка,
// наоборот, светлеет и получает слабый эмиссив — это световозвращающая
// краска, без неё в свете фар полотно читается как чёрная яма.

const TOD_SURFACE = {
    day: { k: 1.0, tint: [1.0, 1.0, 1.0], lineK: 1.0, lineEmissive: null },
    dusk: { k: 0.9, tint: [1.04, 0.96, 0.92], lineK: 1.05, lineEmissive: '#1a1712' },
    night: { k: 0.72, tint: [0.84, 0.9, 1.06], lineK: 1.25, lineEmissive: '#31353d' }
};

/** Приглушить и подкрасить цвет под время суток (на месте). */
function todShade(color, tod) {
    const t = tod.tint;
    color.setRGB(
        Math.min(1, color.r * tod.k * t[0]),
        Math.min(1, color.g * tod.k * t[1]),
        Math.min(1, color.b * tod.k * t[2])
    );
    return color;
}

const KERB_WIDTH = 0.62; // ширина бордюра, м
const MARK_LIFT = 0.015; // подъём разметки над полотном, м

// ---------------------------------------------------------------------------
// Нарезка полотна и рельефа на сегменты вдоль дуги
// ---------------------------------------------------------------------------
//
// Прежде вся лента шла в кадр целиком: цена росла прямо пропорционально длине
// круга, и длинные трассы приходилось лечить прореживанием выборки
// (TERRAIN_REF_LENGTH — снят вместе с этим текстом). Теперь полотно, бордюры,
// разметка и рельеф режутся на сегменты по дуге, у каждого сегмента своя
// ограничивающая сфера, и в кадр уходят только видимые.
//
// ШВОВ НЕТ ПО ПОСТРОЕНИЮ: соседние сегменты делят общий ряд выборок. Ряд
// row[k+1] строится и как последний ряд сегмента k, и как первый ряд
// сегмента k+1, из одних и тех же чисел, — вершины совпадают бит в бит,
// щели между сегментами появиться неоткуда.
//
// Как это попадает в GPU одним мешем. Сегменты лежат в буфере подряд, в
// порядке дуги. `geometry.groups` — это список диапазонов, и three.js
// рисует по одному вызову на группу, НО только если материал меша задан
// массивом (проверено по вендоренному r180: `Array.isArray(material)` —
// единственная ветка, где groups вообще читаются). Поэтому материал у
// сегментного меша — массив из MAX_RUNS одинаковых ссылок, а в кадре
// переписывается только длина списка групп и границы диапазонов.
// Ни новых объектов, ни новых материалов при этом не возникает.

const SEGMENT_LENGTH = 180.0; // целевая длина сегмента по дуге, м

/**
 * Потолок числа прогонов на один меш.
 *
 * Видимые сегменты почти всегда идут подряд (камера смотрит вперёд вдоль
 * дуги), но на кольце бывает виден и противоположный виток — это второй
 * прогон. Каждый прогон стоит один draw call, поэтому их число ограничено:
 * если видимых кусков больше, самые близкие друг к другу склеиваются вместе
 * с промежутком. Хуже картинке от этого не будет — только чуть больше
 * треугольников.
 */
const MAX_RUNS = 4;

/** Границы сегментов в индексах выборок осевой линии. */
function planSegments(T) {
    let n = Math.round(T.length / SEGMENT_LENGTH);
    if (n < 3) n = 3;
    if (n > 48) n = 48;
    if (n > T.count >> 2) n = Math.max(1, T.count >> 2);
    const row = new Int32Array(n + 1);
    for (let k = 0; k <= n; k++) row[k] = Math.round((k * T.count) / n);
    row[0] = 0;
    row[n] = T.count;
    // строгая монотонность: на совсем короткой трассе округление может слипнуться
    for (let k = 1; k <= n; k++) if (row[k] <= row[k - 1]) row[k] = row[k - 1] + 1;
    return { count: n, row: row };
}

/**
 * Меш, нарезанный на сегменты вдоль дуги, с покадровым отсечением.
 *
 * starts/counts — вершинные диапазоны сегментов, spheres — по четыре числа
 * (cx, cy, cz, r) на сегмент. Всё рабочее выделено здесь, в кадре только
 * счёт и запись чисел.
 */
class SegmentedSurface {
    constructor(name, geometry, material, starts, counts, spheres) {
        this.n = starts.length;
        this.starts = starts;
        this.counts = counts;
        this.spheres = spheres;
        this.vis = new Uint8Array(this.n);
        this.runStart = new Int32Array(this.n + 1);
        this.runEnd = new Int32Array(this.n + 1);

        // Материал-массив: групп у геометрии без него three.js не читает.
        const mats = [];
        for (let i = 0; i < MAX_RUNS; i++) mats.push(material);
        const mesh = new THREE.Mesh(geometry, mats);
        mesh.name = name;
        mesh.matrixAutoUpdate = false;
        mesh.updateMatrix();
        // Отсечение целого меша бессмысленно — оно и есть наша задача,
        // только по сегментам.
        mesh.frustumCulled = false;
        this.mesh = mesh;

        // Пул объектов групп: в кадре переписываются их поля, а не создаются
        // новые. geometry.groups — живой массив, ему меняется только длина.
        this.pool = [];
        for (let i = 0; i < MAX_RUNS; i++) {
            this.pool.push({ start: 0, count: 0, materialIndex: i });
        }
        this.groups = geometry.groups;
        this.showAll();
    }

    /** Показать всё кольцо (отсечение выключено). */
    showAll() {
        const g = this.pool[0];
        g.start = 0;
        g.count = this.starts.length ? this.starts[this.n - 1] + this.counts[this.n - 1] : 0;
        this.groups[0] = g;
        this.groups.length = 1;
        this.visibleSegments = this.n;
    }

    /** Пересчитать видимые сегменты и собрать из них прогоны. */
    cull(culler) {
        if (!culler || !culler.enabled) {
            this.showAll();
            return;
        }
        const n = this.n;
        const sph = this.spheres;
        const vis = this.vis;
        let shown = 0;
        for (let i = 0; i < n; i++) {
            const o = i * 4;
            const v = sph[o + 3] >= 0 && culler.visible(sph[o], sph[o + 1], sph[o + 2], sph[o + 3]) ? 1 : 0;
            vis[i] = v;
            shown += v;
        }
        this.visibleSegments = shown;
        if (shown === 0) {
            this.groups.length = 0;
            return;
        }

        // прогоны подряд идущих видимых сегментов
        const rs = this.runStart;
        const re = this.runEnd;
        let runN = 0;
        let i = 0;
        while (i < n) {
            if (!vis[i]) { i++; continue; }
            let j = i;
            while (j + 1 < n && vis[j + 1]) j++;
            rs[runN] = i;
            re[runN] = j;
            runN++;
            i = j + 1;
        }

        // склейка лишних прогонов по самому узкому промежутку
        while (runN > MAX_RUNS) {
            let best = 0;
            let bestGap = 0x7fffffff;
            for (let k = 0; k < runN - 1; k++) {
                const gap = rs[k + 1] - re[k] - 1;
                if (gap < bestGap) { bestGap = gap; best = k; }
            }
            re[best] = re[best + 1];
            for (let k = best + 1; k < runN - 1; k++) {
                rs[k] = rs[k + 1];
                re[k] = re[k + 1];
            }
            runN--;
        }

        for (let k = 0; k < runN; k++) {
            const a = rs[k];
            const b = re[k];
            const g = this.pool[k];
            g.start = this.starts[a];
            g.count = this.starts[b] + this.counts[b] - g.start;
            this.groups[k] = g;
        }
        this.groups.length = runN;
    }
}

// ---------------------------------------------------------------------------
// Нормализация входных данных трассы (12.1)
// ---------------------------------------------------------------------------

/**
 * Приводит вход к набору типизированных массивов.
 * Принимает: объект track из race_init, экземпляр клиентского Track с теми же
 * полями, либо объект со списком samples из 7.2 (серверный вид).
 */
export function readTrack(track) {
    if (!track) throw new Error('trackmesh: track не задан');

    if (track.x && track.z && track.hw) {
        const count = track.count || track.x.length;
        return {
            count: count,
            length: track.length || (track.s ? track.s[count - 1] + (track.sample_step || 2) : count * 2),
            step: track.sample_step || 2.0,
            theme: track.theme || 'city',
            seed: track.decor_seed === undefined ? 12345 : track.decor_seed,
            x: track.x,
            y: track.y,
            z: track.z,
            tx: track.tx,
            tz: track.tz,
            nx: track.nx,
            nz: track.nz,
            hw: track.hw,
            s: track.s,
            startGrid: track.start_grid || track.startGrid || null
        };
    }

    if (track.cx && track.cz && track.chw) {
        // экземпляр клиентского Track из js/track.js: те же данные, но поля
        // названы иначе (контракт 12.1 фиксирует формат JSON, а не имена полей
        // класса), поэтому принимаем и такой вид
        const count = track.count || track.cx.length;
        return {
            count: count,
            length: track.length || count * (track.step || 2.0),
            step: track.step || 2.0,
            theme: track.theme || 'city',
            seed: track.decorSeed === undefined ? 12345 : track.decorSeed,
            x: track.cx,
            y: track.cy,
            z: track.cz,
            tx: track.ctx,
            tz: track.ctz,
            nx: track.cnx,
            nz: track.cnz,
            hw: track.chw,
            s: track.cs,
            startGrid: track.startGrid || null
        };
    }

    if (track.samples && track.samples.length) {
        // серверная форма: список объектов
        const n = track.samples.length;
        const out = {
            count: n,
            length: track.length || n * 2,
            step: track.sample_step || 2.0,
            theme: track.theme || 'city',
            seed: track.decor_seed === undefined ? 12345 : track.decor_seed,
            x: new Float32Array(n),
            y: new Float32Array(n),
            z: new Float32Array(n),
            tx: new Float32Array(n),
            tz: new Float32Array(n),
            nx: new Float32Array(n),
            nz: new Float32Array(n),
            hw: new Float32Array(n),
            s: new Float32Array(n),
            startGrid: track.start_grid || null
        };
        for (let i = 0; i < n; i++) {
            const p = track.samples[i];
            out.x[i] = p.x;
            out.y[i] = p.y;
            out.z[i] = p.z;
            out.tx[i] = p.tangent_x !== undefined ? p.tangent_x : p.tx;
            out.tz[i] = p.tangent_z !== undefined ? p.tangent_z : p.tz;
            out.nx[i] = p.normal_x !== undefined ? p.normal_x : p.nx;
            out.nz[i] = p.normal_z !== undefined ? p.normal_z : p.nz;
            out.hw[i] = p.half_width !== undefined ? p.half_width : p.hw;
            out.s[i] = p.s;
        }
        return out;
    }

    throw new Error('trackmesh: неизвестный формат трассы');
}

// ---------------------------------------------------------------------------
// Главная функция
// ---------------------------------------------------------------------------

/**
 * Строит статическую геометрию трассы.
 * @param {object} track   данные формата 12.1
 * @param {string} theme   city | mountain | industrial (по умолчанию из track)
 * @param {string} quality low | medium | high
 * @param {object} [opts]  { ao, timeOfDay } — необязательные параметры,
 *                         добавленные после контракта:
 *                         ao — запекать ли затенение в вершинные цвета,
 *                         timeOfDay — day | dusk | night (приходит из
 *                         настроек комнаты, см. шапку scenery.js).
 */
export function buildTrackMeshes(track, theme, quality, opts) {
    const T = readTrack(track);
    const P = QUALITY[quality] || QUALITY.medium;
    const ao = !opts || opts.ao !== false;
    const todName = opts && TOD_SURFACE[opts.timeOfDay] ? opts.timeOfDay : 'day';
    const tod = TOD_SURFACE[todName];
    const themeName = theme || T.theme || 'city';
    const pal = THEMES[themeName] || THEMES.city;

    const group = new THREE.Group();
    group.name = 'track';

    // общий материал статики: один объект — меньше переключений состояния
    const surfaceMat = new THREE.MeshLambertMaterial({
        vertexColors: true,
        flatShading: true
    });
    surfaceMat.name = 'trackSurface';

    // разметка: тот же материал плюс полигональное смещение к камере
    const markMat = new THREE.MeshLambertMaterial({
        vertexColors: true,
        flatShading: true,
        polygonOffset: true,
        polygonOffsetFactor: -2,
        polygonOffsetUnits: -4
    });
    markMat.name = 'trackMarkings';
    if (tod.lineEmissive) {
        // световозвращающая краска: ночью линии видно и без прямого света
        markMat.emissive = toColor(tod.lineEmissive);
        markMat.emissiveIntensity = 1.0;
    }

    const colors = {
        asphalt: toColor(pal.asphalt),
        asphaltDark: toColor(pal.asphaltDark),
        line: toColor(pal.line),
        kerbA: toColor(pal.kerbA),
        kerbB: toColor(pal.kerbB),
        kerbSide: toColor(pal.kerbSide),
        shoulder: toColor(pal.shoulder),
        groundNear: toColor(pal.groundNear),
        groundFar: toColor(pal.groundFar),
        rock: toColor(pal.rock),
        archBody: toColor(pal.archBody),
        archTrim: toColor(pal.archTrim),
        dark: toColor('#1b1d21'),
        white: toColor('#f2f4f6')
    };

    // время суток: всё, кроме разметки, приглушается и холоднеет
    for (const key in colors) {
        if (key === 'line' || key === 'white') {
            const c = colors[key];
            c.setRGB(Math.min(1, c.r * tod.lineK), Math.min(1, c.g * tod.lineK), Math.min(1, c.b * tod.lineK));
        } else {
            todShade(colors[key], tod);
        }
    }

    // нижняя отметка мира: сюда садится и внешнее кольцо рельефа, и плоскость
    // грунта — стык получается без шва
    let minY = Infinity;
    let maxY = -Infinity;
    for (let i = 0; i < T.count; i++) {
        if (T.y[i] < minY) minY = T.y[i];
        if (T.y[i] > maxY) maxY = T.y[i];
    }
    const groundY = minY - pal.terrainDrop - 2.0;

    const plan = planSegments(T);
    const road = buildRoad(T, P, colors, pal, surfaceMat, ao, plan);
    const kerbs = buildKerbs(T, P, colors, surfaceMat, ao, plan);
    const markings = buildMarkings(T, P, colors, markMat, ao, plan);
    const terrain = buildTerrain(T, P, colors, pal, groundY, surfaceMat, ao, plan);
    const ground = buildGround(T, colors, groundY, surfaceMat);
    const arch = buildStartArch(T, P, colors, surfaceMat, ao);

    // Сегментные поверхности: отсекаются посегментно каждый кадр.
    const segmented = [road, kerbs, markings, terrain];
    group.add(ground, terrain.mesh, road.mesh, kerbs.mesh, markings.mesh, arch);

    const api = {
        group: group,
        road: road.mesh,
        kerbs: kerbs.mesh,
        markings: markings.mesh,
        terrain: terrain.mesh,
        ground: ground,
        arch: arch,
        segments: plan.count,
        materials: { surface: surfaceMat, markings: markMat },
        bounds: { minY: minY, maxY: maxY, groundY: groundY },
        timeOfDay: todName,

        /**
         * Отсечение сегментов по пирамиде видимости. Зовётся раз в кадр из
         * renderer.js, ПОСЛЕ обновления матриц камеры. Ноль аллокаций.
         */
        cull: function (culler) {
            let shown = 0;
            for (let i = 0; i < segmented.length; i++) {
                segmented[i].cull(culler);
                shown += segmented[i].visibleSegments;
            }
            api.stats.visibleSegments = shown;
        },

        stats: {
            drawCalls: countDrawCalls(group),
            triangles: countTriangles(group),
            visibleSegments: plan.count * segmented.length
        },
        dispose: function () {
            disposeObject(group);
        }
    };
    return api;
}

// ---------------------------------------------------------------------------
// Полотно
// ---------------------------------------------------------------------------

function buildRoad(T, P, colors, pal, material, ao, plan) {
    const b = new MeshBuilder();
    const rng = new Rng(T.seed ^ 0x51ed2701);
    const cols = P.roadCols + 1;
    // заранее посчитанный шум на УЗЕЛ сетки: цвет полотна не должен «мигать»
    // при повторной сборке, поэтому берём его из детерминированного ГПСЧ
    const jitter = new Float32Array(T.count * cols);
    for (let i = 0; i < jitter.length; i++) jitter[i] = 1 + (rng.next() * 2 - 1) * 0.05;

    const patch = new THREE.Color();
    // индекс глобального ряда: задаётся снаружи цикла сегментов
    let base = 0;

    function point(ii, j, out) {
        const i = (base + ii) % T.count;
        const u = (-1 + (2 * j) / P.roadCols) * T.hw[i];
        out[0] = T.x[i] + T.nx[i] * u;
        out[1] = T.y[i];
        out[2] = T.z[i] + T.nz[i] * u;
    }

    // Цвет считается на УЗЕЛ, а не на квад: только так тёмная полоса у
    // бордюра получается плавной, а не ступенькой в одну колонку.
    function vertexColor(ii, j, c) {
        const i = (base + ii) % T.count;
        const edge = Math.abs(-1 + (2 * j) / P.roadCols); // 0 в центре, 1 у кромки
        // траектория по центру чуть темнее (резина), кромки светлее (пыль)
        mixColor(patch, colors.asphaltDark, colors.asphalt, 0.25 + 0.75 * edge);
        let k = jitter[i * cols + j];
        if (ao) {
            // запечённый контакт с бордюром и обочиной
            k *= roadEdgeAO(T.hw[i] * (1 - edge));
        }
        shade(c, patch, k);
    }

    const starts = new Int32Array(plan.count);
    const counts = new Int32Array(plan.count);
    for (let k = 0; k < plan.count; k++) {
        base = plan.row[k];
        starts[k] = b.vertexCount;
        surfaceGrid(b, {
            rows: plan.row[k + 1] - base + 1, // +1: замыкающий ряд общий с соседом
            cols: cols,
            closed: false,
            point: point,
            vertexColor: vertexColor
        });
        counts[k] = b.vertexCount - starts[k];
    }

    const geom = b.build();
    const spheres = segmentSpheres(geom, starts, counts);
    return new SegmentedSurface('trackRoad', geom, material, starts, counts, spheres);
}

/** Ограничивающие сферы сегментов по готовой геометрии. */
function segmentSpheres(geom, starts, counts) {
    const pos = geom.attributes.position.array;
    const out = new Float32Array(starts.length * 4);
    for (let k = 0; k < starts.length; k++) {
        rangeSphere(pos, starts[k], counts[k], out, k * 4);
    }
    return out;
}

// ---------------------------------------------------------------------------
// Бордюры
// ---------------------------------------------------------------------------

function buildKerbs(T, P, colors, material, ao, plan) {
    const b = new MeshBuilder();
    const tmp = new THREE.Color();

    // профиль в ортах (u — наружу от кромки, v — вверх от полотна)
    const profileOut = P.kerbSide
        ? [[0.0, 0.005], [KERB_WIDTH * 0.85, 0.075], [KERB_WIDTH, -0.35]]
        : [[0.0, 0.005], [KERB_WIDTH, 0.06]];

    let base = 0;
    let sgn = 1;

    function frame(ii, f) {
        const i = (base + ii) % T.count;
        f[0] = T.x[i] + T.nx[i] * T.hw[i] * sgn;
        f[1] = T.y[i];
        f[2] = T.z[i] + T.nz[i] * T.hw[i] * sgn;
        f[3] = T.nx[i] * sgn;
        f[4] = 0;
        f[5] = T.nz[i] * sgn;
        f[6] = 0;
        f[7] = 1;
        f[8] = 0;
    }

    function color(ii, k, c) {
        const i = (base + ii) % T.count;
        if (k === 1) {
            // наружная стенка смотрит вниз и в грунт — там темнее всего
            if (ao) shade(c, colors.kerbSide, 0.52);
            else c.copy(colors.kerbSide);
        } else {
            // чередование посегментно: шаг выборки 2 м, полоса = 2 м
            // Цвет на квад, а не на узел: иначе красно-белая шашка
            // расплылась бы в градиент и перестала читаться.
            tmp.copy((i & 1) === 0 ? colors.kerbA : colors.kerbB);
            if (ao) shade(c, tmp, 0.88);
            else c.copy(tmp);
        }
    }

    const starts = new Int32Array(plan.count);
    const counts = new Int32Array(plan.count);
    for (let k = 0; k < plan.count; k++) {
        starts[k] = b.vertexCount;
        for (let side = 0; side < 2; side++) {
            sgn = side === 0 ? 1 : -1;
            base = plan.row[k];
            // для левой стороны профиль отражается, поэтому выворачиваем обход
            extrudeProfile(b, {
                count: plan.row[k + 1] - base + 1,
                closed: false,
                flip: sgn < 0,
                profile: profileOut,
                frame: frame,
                color: color
            });
        }
        counts[k] = b.vertexCount - starts[k];
    }

    const geom = b.build();
    const spheres = segmentSpheres(geom, starts, counts);
    return new SegmentedSurface('trackKerbs', geom, material, starts, counts, spheres);
}

// ---------------------------------------------------------------------------
// Разметка
// ---------------------------------------------------------------------------

function buildMarkings(T, P, colors, material, ao, plan) {
    const b = new MeshBuilder();
    const lift = MARK_LIFT;
    const lineC = new THREE.Color();
    // кромочная линия лежит внутри тёмной полосы у бордюра: если оставить её
    // белой, запечённый контакт разрежется пополам яркой чертой
    const edgeLineK = ao ? roadEdgeAO(0.36) : 1;
    shade(lineC, colors.line, edgeLineK);

    let base = 0;
    let sgn = 1;

    // кромочная полоса строится сеткой 2 колонки: внутренняя и внешняя кромка
    function edgePoint(ii, j, out) {
        const i = (base + ii) % T.count;
        const u = (T.hw[i] - (j === 0 ? 0.55 : 0.18)) * sgn;
        out[0] = T.x[i] + T.nx[i] * u;
        out[1] = T.y[i] + lift;
        out[2] = T.z[i] + T.nz[i] * u;
    }

    function edgeColor(ii, j, c) {
        c.copy(lineC);
    }

    // осевая прерывистая: три отрезка через три пропуска (6 м штрих / 6 м пусто)
    const dashOn = 3;
    const dashPeriod = 6;
    const half = 0.14;

    const starts = new Int32Array(plan.count);
    const counts = new Int32Array(plan.count);
    for (let k = 0; k < plan.count; k++) {
        starts[k] = b.vertexCount;
        const i0 = plan.row[k];
        const i1 = plan.row[k + 1];

        // кромочные линии по обеим сторонам, сплошные
        if (P.edgeLines) {
            for (let side = 0; side < 2; side++) {
                sgn = side === 0 ? 1 : -1;
                base = i0;
                surfaceGrid(b, {
                    rows: i1 - i0 + 1,
                    cols: 2,
                    closed: false,
                    flip: sgn < 0,
                    point: edgePoint,
                    color: edgeColor
                });
            }
        }

        for (let i = i0; i < i1; i++) {
            if (i % dashPeriod >= dashOn) continue;
            const i2 = (i + 1) % T.count;
            const ax = T.x[i] + T.nx[i] * -half,
                az = T.z[i] + T.nz[i] * -half;
            const bx = T.x[i] + T.nx[i] * half,
                bz = T.z[i] + T.nz[i] * half;
            const cx = T.x[i2] + T.nx[i2] * half,
                cz = T.z[i2] + T.nz[i2] * half;
            const dx = T.x[i2] + T.nx[i2] * -half,
                dz = T.z[i2] + T.nz[i2] * -half;
            const y0 = T.y[i] + lift,
                y1 = T.y[i2] + lift;
            b.quadRaw(
                ax, y0, az,
                dx, y1, dz,
                cx, y1, cz,
                bx, y0, bz,
                colors.line.r, colors.line.g, colors.line.b
            );
        }

        // стартовая клетка живёт в выборке 0, то есть в самом первом сегменте
        if (k === 0) buildStartLine(b, T, colors, lift);

        counts[k] = b.vertexCount - starts[k];
    }

    const geom = b.build();
    const spheres = segmentSpheres(geom, starts, counts);
    const surf = new SegmentedSurface('trackMarkings', geom, material, starts, counts, spheres);
    surf.mesh.renderOrder = 1;
    return surf;
}

function buildStartLine(b, T, colors, lift) {
    const i0 = 0;
    const rows = 2;
    const cells = 12;
    const hw = T.hw[i0];
    const cellLen = 0.55;

    const px = T.x[i0],
        py = T.y[i0] + lift,
        pz = T.z[i0];
    const tx = T.tx[i0],
        tz = T.tz[i0];
    const nx = T.nx[i0],
        nz = T.nz[i0];

    for (let r = 0; r < rows; r++) {
        const z0 = -0.2 + r * cellLen;
        const z1 = z0 + cellLen;
        for (let c = 0; c < cells; c++) {
            const u0 = -hw + ((2 * hw) / cells) * c;
            const u1 = u0 + (2 * hw) / cells;
            const col = (r + c) & 1 ? colors.white : colors.dark;
            b.quadRaw(
                px + nx * u0 + tx * z0, py, pz + nz * u0 + tz * z0,
                px + nx * u0 + tx * z1, py, pz + nz * u0 + tz * z1,
                px + nx * u1 + tx * z1, py, pz + nz * u1 + tz * z1,
                px + nx * u1 + tx * z0, py, pz + nz * u1 + tz * z0,
                col.r, col.g, col.b
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Рельеф
// ---------------------------------------------------------------------------

/**
 * Параметры ленты рельефа: смещения колец, коэффициенты падения и
 * периодический шум. Выделено отдельно, потому что scenery.js обязан
 * ставить декор ровно на ту же поверхность — он берёт высоту через
 * createTerrainSampler(), а тот использует эти же параметры и то же зерно.
 */
function terrainParams(T, P, pal, groundY) {
    const rng = new Rng((T.seed ^ 0x2f9a7b13) >>> 0);
    const rings = P.terrainRings;
    const offs = new Float64Array(rings);
    const dropK = new Float64Array(rings);
    for (let j = 0; j < rings; j++) {
        const t = j / (rings - 1);
        offs[j] = KERB_WIDTH + 0.04 + (TERRAIN_WIDTH - KERB_WIDTH - 0.04) * Math.pow(t, 2.1);
        dropK[j] = Math.pow(t, 1.4);
    }
    // на каждое кольцо свой шум, чтобы склон не выглядел гофрой;
    // гармоники целые — значит функция периодична по кругу и шва нет
    const noises = [];
    for (let j = 0; j < rings; j++) {
        noises.push(loopNoise(rng, [2, 5, 9, 17], [1.0, 0.55, 0.3, 0.16]));
    }
    const sideNoise = [
        loopNoise(rng, [3, 7, 13], [1.0, 0.5, 0.25]),
        loopNoise(rng, [3, 7, 13], [1.0, 0.5, 0.25])
    ];
    return { rings: rings, offs: offs, dropK: dropK, noises: noises, sideNoise: sideNoise, groundY: groundY, pal: pal };
}

/** Высота рельефа на кольце j в точке выборки i. */
function ringHeight(T, tp, i, j, side) {
    if (j >= tp.rings - 1) return tp.groundY;
    const t = T.s ? T.s[i] / T.length : i / T.count;
    const k = tp.dropK[j];
    const base = T.y[i] - 0.03 - tp.pal.terrainDrop * k;
    const amp = tp.pal.terrainRise * k;
    const n = tp.noises[j](t) * 0.5 + tp.sideNoise[side](t) * 0.5;
    let y = base + n * amp;
    if (j === tp.rings - 2) y = y * 0.45 + tp.groundY * 0.55;
    if (j <= 1) y = Math.min(y, T.y[i] - 0.02);
    return y;
}

/** Высота рельефа на произвольном удалении off от кромки (кусочно-линейно по кольцам). */
function terrainHeight(T, tp, i, off, side) {
    const offs = tp.offs;
    if (off <= offs[0]) return ringHeight(T, tp, i, 0, side);
    for (let j = 1; j < tp.rings; j++) {
        if (off <= offs[j]) {
            const k = (off - offs[j - 1]) / (offs[j] - offs[j - 1]);
            const a = ringHeight(T, tp, i, j - 1, side);
            const b = ringHeight(T, tp, i, j, side);
            return a + (b - a) * k;
        }
    }
    return tp.groundY;
}

/**
 * Сэмплер высоты рельефа для scenery.js.
 * Возвращает функцию (i, off, side) -> y, где i — индекс выборки осевой линии,
 * off — расстояние наружу от кромки полотна в метрах, side — 0 (сторона положительной нормали, то есть левый борт по разделу 4) или 1.
 * Пресет качества обязан совпадать с тем, на котором построена трасса,
 * иначе декор встанет чуть выше или ниже поверхности.
 */
export function createTerrainSampler(track, theme, quality) {
    const T = readTrack(track);
    const P = QUALITY[quality] || QUALITY.medium;
    const pal = THEMES[theme || T.theme] || THEMES.city;
    let minY = Infinity;
    for (let i = 0; i < T.count; i++) if (T.y[i] < minY) minY = T.y[i];
    const groundY = minY - pal.terrainDrop - 2.0;
    const tp = terrainParams(T, P, pal, groundY);
    const sampler = function (i, off, side) {
        return terrainHeight(T, tp, i | 0, off, side ? 1 : 0);
    };
    sampler.groundY = groundY;
    sampler.terrainWidth = TERRAIN_WIDTH;
    return sampler;
}

function buildTerrain(T, P, colors, pal, groundY, material, ao, plan) {
    const b = new MeshBuilder();
    const tp = terrainParams(T, P, pal, groundY);
    const rings = P.terrainRings;
    // Прореживание по длине круга снято: лента отсекается посегментно,
    // и её цена больше не зависит от длины трассы.
    const stride = P.terrainStride;
    const rowsAll = Math.max(3, Math.floor(T.count / stride));

    const jitRng = new Rng((T.seed ^ 0x77aa3311) >>> 0);
    const patch = new THREE.Color();
    const grassJit = new Float32Array(rowsAll * rings);
    for (let i = 0; i < grassJit.length; i++) grassJit[i] = 1 + (jitRng.next() * 2 - 1) * 0.09;

    // затенение кольца: у самой кромки полотна темно, дальше светлеет
    const ringAO = new Float32Array(rings);
    for (let j = 0; j < rings; j++) {
        ringAO[j] = ao ? 1 - AO_SHOULDER * smooth01(1 - tp.offs[j] / AO_SHOULDER_W) : 1;
    }

    // границы сегментов в рядах ленты: те же места дуги, что и у полотна
    const segCount = Math.min(plan.count, Math.max(1, rowsAll >> 1));
    const tRow = new Int32Array(segCount + 1);
    tRow[0] = 0;
    for (let k = 1; k < segCount; k++) {
        let v = Math.round(plan.row[Math.round((k * plan.count) / segCount)] / stride);
        if (v < tRow[k - 1] + 1) v = tRow[k - 1] + 1;
        if (v > rowsAll - (segCount - k)) v = rowsAll - (segCount - k);
        tRow[k] = v;
    }
    tRow[segCount] = rowsAll;

    let base = 0;
    let side = 0;
    let sgn = 1;

    function point(ii, j, out) {
        const row = (base + ii) % rowsAll;
        const i = (row * stride) % T.count;
        const u = (T.hw[i] + tp.offs[j]) * sgn;
        out[0] = T.x[i] + T.nx[i] * u;
        out[1] = ringHeight(T, tp, i, j, side);
        out[2] = T.z[i] + T.nz[i] * u;
    }

    // цвет на узел: тёмная кайма у обочины переходит в траву плавно
    function vertexColor(ii, j, c) {
        const row = (base + ii) % rowsAll;
        const i = (row * stride) % T.count;
        let k = grassJit[row * rings + j] * ringAO[j];
        if (ao) {
            // канава ниже полотна дополнительно затенена
            const drop = T.y[i] - ringHeight(T, tp, i, j, side);
            if (drop > 0) k *= 1 - AO_DITCH * smooth01(drop / 6);
        }
        if (j === 0) {
            shade(c, colors.shoulder, k);
            return;
        }
        const t = j / (rings - 1);
        mixColor(patch, colors.groundNear, colors.groundFar, t);
        shade(c, patch, k);
    }

    const starts = new Int32Array(segCount);
    const counts = new Int32Array(segCount);
    for (let k = 0; k < segCount; k++) {
        starts[k] = b.vertexCount;
        for (side = 0; side < 2; side++) {
            sgn = side === 0 ? 1 : -1;
            base = tRow[k];
            surfaceGrid(b, {
                rows: tRow[k + 1] - base + 1, // замыкающий ряд общий с соседом
                cols: rings,
                closed: false,
                flip: sgn < 0,
                point: point,
                vertexColor: vertexColor
            });
        }
        counts[k] = b.vertexCount - starts[k];
    }

    const geom = b.build();
    const spheres = segmentSpheres(geom, starts, counts);
    return new SegmentedSurface('trackTerrain', geom, material, starts, counts, spheres);
}

// ---------------------------------------------------------------------------
// Общий грунт до горизонта
// ---------------------------------------------------------------------------

function buildGround(T, colors, groundY, material) {
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
    const half = r + 900;

    const b = new MeshBuilder();
    const c = colors.groundFar;
    b.quadRaw(
        cx - half, groundY, cz - half,
        cx - half, groundY, cz + half,
        cx + half, groundY, cz + half,
        cx + half, groundY, cz - half,
        c.r, c.g, c.b
    );
    const mesh = new THREE.Mesh(b.build(), material);
    mesh.name = 'trackGround';
    mesh.frustumCulled = false; // два треугольника под всей картой
    mesh.matrixAutoUpdate = false;
    mesh.updateMatrix();
    return mesh;
}

// ---------------------------------------------------------------------------
// Стартовая арка
// ---------------------------------------------------------------------------

function buildStartArch(T, P, colors, material, ao) {
    const i0 = 0;
    const hw = T.hw[i0];
    const px = T.x[i0],
        py = T.y[i0],
        pz = T.z[i0];
    const nx = T.nx[i0],
        nz = T.nz[i0];
    const tx = T.tx[i0],
        tz = T.tz[i0];

    const b = new MeshBuilder();
    const pillarW = 0.55;
    const archH = 7.2;
    const span = hw + 1.6;

    // строим в локальных ортах: u — поперёк, w — вдоль трассы
    function put(u, y, w, su, sy, sw, color, colorTop) {
        const cxp = px + nx * u + tx * w;
        const czp = pz + nz * u + tz * w;
        // коробка ориентирована по осям трассы: строим руками через квады
        const hu = su * 0.5,
            hy = sy * 0.5,
            hwv = sw * 0.5;
        const pts = [];
        for (let s1 = -1; s1 <= 1; s1 += 2) {
            for (let s2 = -1; s2 <= 1; s2 += 2) {
                pts.push([nx * hu * s1 + tx * hwv * s2, nz * hu * s1 + tz * hwv * s2]);
            }
        }
        // восемь вершин: (u,w) в порядке (-,-) (-,+) (+,-) (+,+)
        const o = [pts[0], pts[1], pts[3], pts[2]];
        const yb = py + y - hy,
            yt = py + y + hy;
        const top = colorTop || color;
        // верх
        b.quadRaw(
            cxp + o[0][0], yt, czp + o[0][1],
            cxp + o[1][0], yt, czp + o[1][1],
            cxp + o[2][0], yt, czp + o[2][1],
            cxp + o[3][0], yt, czp + o[3][1],
            top.r, top.g, top.b
        );
        // низ
        b.quadRaw(
            cxp + o[3][0], yb, czp + o[3][1],
            cxp + o[2][0], yb, czp + o[2][1],
            cxp + o[1][0], yb, czp + o[1][1],
            cxp + o[0][0], yb, czp + o[0][1],
            color.r, color.g, color.b
        );
        // боковые
        for (let k = 0; k < 4; k++) {
            const a = o[k],
                d = o[(k + 1) % 4];
            b.quadRaw(
                cxp + a[0], yb, czp + a[1],
                cxp + d[0], yb, czp + d[1],
                cxp + d[0], yt, czp + d[1],
                cxp + a[0], yt, czp + a[1],
                color.r, color.g, color.b
            );
        }
    }

    // две опоры
    put(span, archH * 0.5, 0, pillarW, archH, pillarW, colors.archBody);
    put(-span, archH * 0.5, 0, pillarW, archH, pillarW, colors.archBody);
    // перекладина
    put(0, archH - 0.45, 0, span * 2 + pillarW, 0.9, 0.7, colors.archBody, colors.archTrim);
    // баннер под перекладиной
    put(0, archH - 1.35, 0.02, span * 1.5, 0.85, 0.12, colors.archTrim);

    if (P.archDetail > 0) {
        // подкосы и «телевизор» над линией
        put(span * 0.55, archH - 1.9, 0, 0.3, 0.28, 0.3, colors.archBody);
        put(-span * 0.55, archH - 1.9, 0, 0.3, 0.28, 0.3, colors.archBody);
        put(0, archH + 0.55, 0, 2.6, 1.2, 0.4, colors.dark, colors.archTrim);
    }
    if (P.archDetail > 1) {
        // фонари на перекладине
        for (let k = -2; k <= 2; k++) {
            put(k * span * 0.42, archH - 1.05, -0.25, 0.22, 0.22, 0.12, colors.kerbA);
        }
    }

    const archGeom = b.build();
    if (ao) {
        // основания опор тонут в тени, верх перекладины видит всё небо
        bakeContactAO(archGeom, { base: py, height: 3.2, floor: 0.5, power: 0.7, sky: 0.3 });
    }
    const mesh = new THREE.Mesh(archGeom, material);
    mesh.name = 'trackArch';
    mesh.matrixAutoUpdate = false;
    mesh.updateMatrix();
    // Арка — один компактный объект у линии старта: её отсекает сам three.js
    // по ограничивающей сфере геометрии, своей нарезки тут не нужно.
    mesh.frustumCulled = true;
    return mesh;
}

export default buildTrackMeshes;
