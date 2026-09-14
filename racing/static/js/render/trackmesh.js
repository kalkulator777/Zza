/**
 * trackmesh.js — [render] построение статической геометрии трассы.
 *
 * Экспортирует buildTrackMeshes(track, theme, quality) из контракта (раздел 3).
 * Вход — данные формата 12.1 (плоские массивы x, y, z, tx, tz, nx, nz, hw, s),
 * либо объект Track клиента, хранящий те же поля, либо список samples из 7.2.
 *
 * Бюджет (раздел 1): вся трасса — шесть мешей и не более ~35 тысяч
 * треугольников на среднем пресете, то есть меньше половины кадрового лимита.
 *
 *   road      1 draw call   полотно, вершинные цвета с вариацией
 *   kerbs     1 draw call   бордюры обеих кромок, красно-белые посегментно
 *   markings  1 draw call   осевая прерывистая, кромочные, стартовая клетка
 *   terrain   1 draw call   лента рельефа ~50 м с каждой стороны
 *   ground    1 draw call   общий грунт до горизонта (два треугольника)
 *   arch      1 draw call   стартовая арка
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
    disposeObject,
    countTriangles
} from './geomutil.js';

// ---------------------------------------------------------------------------
// Пресеты качества: реально меняют плотность сетки
// ---------------------------------------------------------------------------

const QUALITY = {
    low: { roadCols: 3, terrainRings: 4, terrainStride: 3, kerbSide: false, edgeLines: true, archDetail: 0 },
    medium: { roadCols: 4, terrainRings: 6, terrainStride: 2, kerbSide: true, edgeLines: true, archDetail: 1 },
    high: { roadCols: 6, terrainRings: 8, terrainStride: 1, kerbSide: true, edgeLines: true, archDetail: 2 }
};

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
const KERB_WIDTH = 0.62; // ширина бордюра, м
const MARK_LIFT = 0.015; // подъём разметки над полотном, м

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
 */
export function buildTrackMeshes(track, theme, quality) {
    const T = readTrack(track);
    const P = QUALITY[quality] || QUALITY.medium;
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

    // нижняя отметка мира: сюда садится и внешнее кольцо рельефа, и плоскость
    // грунта — стык получается без шва
    let minY = Infinity;
    let maxY = -Infinity;
    for (let i = 0; i < T.count; i++) {
        if (T.y[i] < minY) minY = T.y[i];
        if (T.y[i] > maxY) maxY = T.y[i];
    }
    const groundY = minY - pal.terrainDrop - 2.0;

    const road = buildRoad(T, P, colors, pal, surfaceMat);
    const kerbs = buildKerbs(T, P, colors, surfaceMat);
    const markings = buildMarkings(T, P, colors, markMat);
    const terrain = buildTerrain(T, P, colors, pal, groundY, surfaceMat);
    const ground = buildGround(T, colors, groundY, surfaceMat);
    const arch = buildStartArch(T, P, colors, surfaceMat);

    group.add(ground, terrain, road, kerbs, markings, arch);

    const api = {
        group: group,
        road: road,
        kerbs: kerbs,
        markings: markings,
        terrain: terrain,
        ground: ground,
        arch: arch,
        materials: { surface: surfaceMat, markings: markMat },
        bounds: { minY: minY, maxY: maxY, groundY: groundY },
        stats: {
            drawCalls: 6,
            triangles: countTriangles(group)
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

function buildRoad(T, P, colors, pal, material) {
    const b = new MeshBuilder();
    const rng = new Rng(T.seed ^ 0x51ed2701);
    const cols = P.roadCols + 1;
    // заранее посчитанный шум на квад: цвет полотна не должен «мигать» при
    // повторной сборке, поэтому берём его из детерминированного ГПСЧ
    const jitter = new Float32Array(T.count * P.roadCols);
    for (let i = 0; i < jitter.length; i++) jitter[i] = 1 + (rng.next() * 2 - 1) * 0.055;

    const patch = new THREE.Color();

    surfaceGrid(b, {
        rows: T.count,
        cols: cols,
        closed: true,
        point: function (i, j, out) {
            const u = (-1 + (2 * j) / P.roadCols) * T.hw[i];
            out[0] = T.x[i] + T.nx[i] * u;
            out[1] = T.y[i];
            out[2] = T.z[i] + T.nz[i] * u;
        },
        color: function (i, j, c) {
            // траектория по центру чуть темнее (резина), кромки светлее (пыль)
            const mid = (j + 0.5) / P.roadCols; // 0..1 поперёк
            const edge = Math.abs(mid - 0.5) * 2; // 0 в центре, 1 у кромки
            mixColor(patch, colors.asphaltDark, colors.asphalt, 0.25 + 0.75 * edge);
            shade(c, patch, jitter[i * P.roadCols + j]);
        }
    });

    const geom = b.build();
    const mesh = new THREE.Mesh(geom, material);
    mesh.name = 'trackRoad';
    mesh.matrixAutoUpdate = false;
    mesh.updateMatrix();
    return mesh;
}

// ---------------------------------------------------------------------------
// Бордюры
// ---------------------------------------------------------------------------

function buildKerbs(T, P, colors, material) {
    const b = new MeshBuilder();

    // профиль в ортах (u — наружу от кромки, v — вверх от полотна)
    const profileOut = P.kerbSide
        ? [[0.0, 0.005], [KERB_WIDTH * 0.85, 0.075], [KERB_WIDTH, -0.35]]
        : [[0.0, 0.005], [KERB_WIDTH, 0.06]];

    for (let side = 0; side < 2; side++) {
        const sgn = side === 0 ? 1 : -1;
        // для левой стороны профиль отражается, поэтому выворачиваем обход
        extrudeProfile(b, {
            count: T.count,
            closed: true,
            flip: sgn < 0,
            profile: profileOut,
            frame: function (i, f) {
                f[0] = T.x[i] + T.nx[i] * T.hw[i] * sgn;
                f[1] = T.y[i];
                f[2] = T.z[i] + T.nz[i] * T.hw[i] * sgn;
                f[3] = T.nx[i] * sgn;
                f[4] = 0;
                f[5] = T.nz[i] * sgn;
                f[6] = 0;
                f[7] = 1;
                f[8] = 0;
            },
            color: function (i, k, c) {
                if (k === 1) {
                    c.copy(colors.kerbSide); // наружная стенка
                } else {
                    // чередование посегментно: шаг выборки 2 м, полоса = 2 м
                    c.copy((i & 1) === 0 ? colors.kerbA : colors.kerbB);
                }
            }
        });
    }

    const mesh = new THREE.Mesh(b.build(), material);
    mesh.name = 'trackKerbs';
    mesh.matrixAutoUpdate = false;
    mesh.updateMatrix();
    return mesh;
}

// ---------------------------------------------------------------------------
// Разметка
// ---------------------------------------------------------------------------

function buildMarkings(T, P, colors, material) {
    const b = new MeshBuilder();
    const lift = MARK_LIFT;

    // кромочные линии по обеим сторонам, сплошные
    if (P.edgeLines) {
        for (let side = 0; side < 2; side++) {
            const sgn = side === 0 ? 1 : -1;
            surfaceGrid(b, {
                rows: T.count,
                cols: 2,
                closed: true,
                flip: sgn < 0,
                point: function (i, j, out) {
                    const inner = T.hw[i] - 0.55;
                    const outer = T.hw[i] - 0.18;
                    const u = (j === 0 ? inner : outer) * sgn;
                    out[0] = T.x[i] + T.nx[i] * u;
                    out[1] = T.y[i] + lift;
                    out[2] = T.z[i] + T.nz[i] * u;
                },
                color: function (i, j, c) {
                    c.copy(colors.line);
                }
            });
        }
    }

    // осевая прерывистая: три отрезка через три пропуска (6 м штрих / 6 м пусто)
    const dashOn = 3;
    const dashPeriod = 6;
    const half = 0.14;
    for (let i = 0; i < T.count; i++) {
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

    // стартовая клетка: две полосы шахматки поперёк всей ширины
    buildStartLine(b, T, colors, lift);

    const mesh = new THREE.Mesh(b.build(), material);
    mesh.name = 'trackMarkings';
    mesh.matrixAutoUpdate = false;
    mesh.updateMatrix();
    mesh.renderOrder = 1;
    return mesh;
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
 * off — расстояние наружу от кромки полотна в метрах, side — 0 (право) или 1 (лево).
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

function buildTerrain(T, P, colors, pal, groundY, material) {
    const b = new MeshBuilder();
    const tp = terrainParams(T, P, pal, groundY);
    const rings = P.terrainRings;
    const stride = P.terrainStride;
    const rowsAll = Math.max(3, Math.floor(T.count / stride));

    const jitRng = new Rng((T.seed ^ 0x77aa3311) >>> 0);
    const patch = new THREE.Color();
    const grassJit = new Float32Array(rowsAll * rings);
    for (let i = 0; i < grassJit.length; i++) grassJit[i] = 1 + (jitRng.next() * 2 - 1) * 0.09;

    for (let side = 0; side < 2; side++) {
        const sgn = side === 0 ? 1 : -1;
        surfaceGrid(b, {
            rows: rowsAll,
            cols: rings,
            closed: true,
            flip: sgn < 0,
            point: function (ii, j, out) {
                const i = (ii * stride) % T.count;
                const u = (T.hw[i] + tp.offs[j]) * sgn;
                out[0] = T.x[i] + T.nx[i] * u;
                out[1] = ringHeight(T, tp, i, j, side);
                out[2] = T.z[i] + T.nz[i] * u;
            },
            color: function (i, j, c) {
                if (j === 0) {
                    shade(c, colors.shoulder, grassJit[i * rings + j]);
                    return;
                }
                const t = j / (rings - 1);
                mixColor(patch, colors.groundNear, colors.groundFar, t);
                shade(c, patch, grassJit[i * rings + j]);
            }
        });
    }

    const mesh = new THREE.Mesh(b.build(), material);
    mesh.name = 'trackTerrain';
    mesh.matrixAutoUpdate = false;
    mesh.updateMatrix();
    return mesh;
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
    mesh.matrixAutoUpdate = false;
    mesh.updateMatrix();
    return mesh;
}

// ---------------------------------------------------------------------------
// Стартовая арка
// ---------------------------------------------------------------------------

function buildStartArch(T, P, colors, material) {
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

    const mesh = new THREE.Mesh(b.build(), material);
    mesh.name = 'trackArch';
    mesh.matrixAutoUpdate = false;
    mesh.updateMatrix();
    return mesh;
}

export default buildTrackMeshes;
