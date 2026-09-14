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
 * Инстансы не отсекаются по пирамиде видимости (ограничивающая сфера охватила
 * бы всю трассу и толку от проверки нет), поэтому плотность подобрана так,
 * чтобы весь декор целиком укладывался в свою долю бюджета треугольников.
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
    trs,
    toColor,
    disposeObject,
    countTriangles
} from './geomutil.js';
import { readTrack, createTerrainSampler } from './trackmesh.js';

// ---------------------------------------------------------------------------
// Пресеты качества
// ---------------------------------------------------------------------------

const QUALITY = {
    low: { density: 0.45, seg: 5, clouds: 3, peaks: 10, skySeg: 12, fogFar: 260 },
    medium: { density: 1.0, seg: 6, clouds: 5, peaks: 16, skySeg: 16, fogFar: 360 },
    high: { density: 1.6, seg: 8, clouds: 8, peaks: 22, skySeg: 20, fogFar: 470 }
};

// ---------------------------------------------------------------------------
// Темы: небо, туман, свет, палитры объектов
// ---------------------------------------------------------------------------

const THEME_ENV = {
    city: {
        zenith: '#3f6fb5',
        horizon: '#bcc9da',
        fog: '#b9c6d7',
        ambient: { color: '#b9c6da', intensity: 0.62 },
        dir: { color: '#fff4e2', intensity: 0.95, dir: [0.45, 0.82, 0.35] },
        cloud: '#f4f7fb',
        palette: {
            concrete: ['#9aa1a8', '#b3b6ae', '#8d949c', '#a7a094', '#7f8790'],
            accent: ['#d0552f', '#3f7ea8', '#cbb04a'],
            metal: '#9fa6ad',
            rail: '#c9ced3',
            post: '#5d646c',
            cone: '#e0632a',
            crowd: ['#d94f4f', '#4f7fd9', '#e0c04a', '#4fb87a', '#d97fd0', '#eaeaea']
        }
    },
    mountain: {
        zenith: '#3877c4',
        horizon: '#d4e3ec',
        fog: '#cddfe9',
        ambient: { color: '#c2d2e0', intensity: 0.58 },
        dir: { color: '#fff0d2', intensity: 1.05, dir: [-0.4, 0.78, 0.48] },
        cloud: '#ffffff',
        palette: {
            needle: ['#2f5a34', '#39663a', '#274c2d', '#456f3d'],
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
        zenith: '#5d7fa0',
        horizon: '#d3cbb6',
        fog: '#cfc8b6',
        ambient: { color: '#c8c4b4', intensity: 0.6 },
        dir: { color: '#fff2d6', intensity: 0.9, dir: [0.35, 0.8, -0.45] },
        cloud: '#e8e4d8',
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
 */
export function buildScenery(track, theme, seed, quality) {
    const T = readTrack(track);
    const qName = QUALITY[quality] ? quality : 'medium';
    const Q = QUALITY[qName];
    const themeName = theme || T.theme || 'city';
    const env = THEME_ENV[themeName] || THEME_ENV.city;
    const baseSeed = (seed === undefined || seed === null ? T.seed : seed) >>> 0;

    const sampler = createTerrainSampler(track, themeName, qName);
    const clearance = makeClearance(T);
    const occupancy = makeOccupancy();

    const group = new THREE.Group();
    group.name = 'scenery';

    // один материал на весь твёрдый декор: меньше переключений состояния
    const material = new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true });
    material.name = 'sceneryLambert';

    const ctx = {
        T: T,
        Q: Q,
        env: env,
        pal: env.palette,
        sampler: sampler,
        clearance: clearance,
        occupancy: occupancy,
        group: group,
        material: material,
        instances: {},
        materials: [material],
        density: Q.density
    };

    if (themeName === 'mountain') buildMountainTheme(ctx, baseSeed);
    else if (themeName === 'industrial') buildIndustrialTheme(ctx, baseSeed);
    else buildCityTheme(ctx, baseSeed);

    // общее для всех тем: трибуны, зрители и конусы
    buildGrandstands(ctx, baseSeed ^ 0x5ab3);
    buildCrowd(ctx, baseSeed ^ 0x77c1);
    buildCones(ctx, baseSeed ^ 0x1d4f);

    // небо и облака
    const skyGroup = new THREE.Group();
    skyGroup.name = 'sky';
    const sky = buildSkyDome(env, Q);
    const clouds = buildClouds(env, Q, new Rng(baseSeed ^ 0x0c10ad));
    skyGroup.add(sky, clouds);
    group.add(skyGroup);
    ctx.materials.push(sky.material, clouds.material);

    const fogColor = toColor(env.fog);
    const api = {
        group: group,
        sky: sky,
        clouds: clouds,
        skyGroup: skyGroup,
        instances: ctx.instances,
        theme: themeName,
        quality: qName,

        // параметры, которые применяет renderer.js
        fog: { color: fogColor, near: Q.fogFar * 0.35, far: Q.fogFar },
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
        },

        stats: {
            drawCalls: countInstanced(group),
            triangles: countTriangles(group)
        },

        dispose: function () {
            disposeObject(group);
        }
    };
    return api;
}

function countInstanced(root) {
    let n = 0;
    root.traverse(function (o) {
        if (o.isMesh || o.isInstancedMesh) n++;
    });
    return n;
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
            const off = rng.range(opts.offMin, opts.offMax);
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

/** Сборка InstancedMesh из списка размещений. */
function addInstanced(ctx, name, geometry, placements) {
    if (!placements.length) {
        geometry.dispose();
        return null;
    }
    const mesh = new THREE.InstancedMesh(geometry, ctx.material, placements.length);
    mesh.name = name;
    mesh.frustumCulled = false;
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const pos = new THREE.Vector3();
    const scl = new THREE.Vector3();
    const axis = new THREE.Vector3(0, 1, 0);
    const col = new THREE.Color();
    let anyColor = false;
    for (let i = 0; i < placements.length; i++) {
        const p = placements[i];
        q.setFromAxisAngle(axis, p.yaw || 0);
        pos.set(p.x, p.y, p.z);
        scl.set(p.sx === undefined ? 1 : p.sx, p.sy === undefined ? 1 : p.sy, p.sz === undefined ? 1 : p.sz);
        m.compose(pos, q, scl);
        mesh.setMatrixAt(i, m);
        if (p.color) {
            toColor(p.color, col);
            mesh.setColorAt(i, col);
            anyColor = true;
        }
    }
    mesh.instanceMatrix.needsUpdate = true;
    if (anyColor && mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    ctx.group.add(mesh);
    ctx.instances[name] = mesh;
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
    scatter(ctx, rngB, { step: 26 / d, jitter: 6, offMin: 13, offMax: 38, radius: 9, margin: 3, chance: 0.8 }, function (p) {
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
    addInstanced(ctx, 'buildings', boxBands(6, 0.42, 1.08, false), buildings);

    // фонари вдоль кромки
    const rngL = new Rng(seed ^ 0x00c2);
    const lamps = [];
    scatter(ctx, rngL, { step: 30 / d, jitter: 1.5, offMin: 2.2, offMax: 3.2, radius: 0.6, margin: 0.6, chance: 0.5 }, function (p) {
        lamps.push({ x: p.x, y: p.y, z: p.z, yaw: p.yaw + (p.side > 0 ? Math.PI : 0), sy: p.rng.range(0.92, 1.08) });
    });
    addInstanced(ctx, 'lamps', lampGeometry(pal, seg), lamps);

    // отбойники: сплошные участки по 6 м
    const rngG = new Rng(seed ^ 0x00d3);
    const rails = [];
    scatter(ctx, rngG, { step: 6.0, jitter: 0, offMin: 1.5, offMax: 1.5, radius: 0.4, margin: 0.4, chance: 0.55 }, function (p) {
        rails.push({ x: p.x, y: p.y, z: p.z, yaw: p.yaw });
    });
    addInstanced(ctx, 'guardrails', guardrailGeometry(pal), rails);

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
    addInstanced(ctx, 'billboards', billboardGeometry(pal), boards);
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
        { name: 'pineTall', geom: pineGeometry(pal, seg, 3, 1.5, pal.needle[0]), scale: [1.1, 1.9] },
        { name: 'pineWide', geom: pineGeometry(pal, seg, 2, 2.1, pal.needle[1]), scale: [0.9, 1.4] },
        { name: 'pineYoung', geom: pineGeometry(pal, Math.max(4, seg - 1), 2, 1.2, pal.needle[3]), scale: [0.6, 1.0] }
    ];
    const lists = [[], [], []];
    const rngT = new Rng(seed ^ 0x0f01);
    scatter(ctx, rngT, { step: 9 / d, jitter: 3.5, offMin: 6, offMax: 44, radius: 2.0, margin: 1.5, chance: 0.85 }, function (p) {
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
    for (let k = 0; k < 3; k++) addInstanced(ctx, kinds[k].name, kinds[k].geom, lists[k]);

    // валуны
    const rngR = new Rng(seed ^ 0x0f02);
    const rocks = [];
    const rockGeom = primPoly(1.0, 0, toColor(pal.rock[0]), 0, 0.45, 0);
    scatter(ctx, rngR, { step: 13 / d, jitter: 5, offMin: 3.5, offMax: 40, radius: 1.4, margin: 1.0, chance: 0.7 }, function (p) {
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
    addInstanced(ctx, 'rocks', rockGeom, rocks);

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
    addInstanced(ctx, 'fences', fence, fences);

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
    addInstanced(ctx, 'huts', hutGeom, huts);

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
        const dist = r + rng.range(220, 620);
        const h = rng.range(60, 165);
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
    addInstanced(ctx, 'peaks', peak, list);
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
        primBox(1.04, 0.06, 1.04, toColor('#6d757b'), 0, 0.64, 0),
        primCyl(0.0, 0.72, 0.34, 4, toColor('#7d858b'), 0, 0.82, Math.PI * 0.25),
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
    addInstanced(ctx, 'hangars', hangar, hangars);

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
    addInstanced(ctx, 'tanks', tank, tanks);

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
    addInstanced(ctx, 'pipes', pipeGeom, pipes);

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
    addInstanced(ctx, 'containers', container, containers);

    // сетчатый забор: одна текстурированная плоскость на секцию
    buildChainFence(ctx, seed ^ 0x1a05);

    // отбойники у самой кромки
    const rngG = new Rng(seed ^ 0x1a06);
    const rails = [];
    scatter(ctx, rngG, { step: 6.0, jitter: 0, offMin: 1.5, offMax: 1.5, radius: 0.4, margin: 0.4, chance: 0.6 }, function (p) {
        rails.push({ x: p.x, y: p.y, z: p.z, yaw: p.yaw });
    });
    addInstanced(ctx, 'guardrails', guardrailGeometry(pal), rails);
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
// Общее: трибуны, зрители, конусы
// ---------------------------------------------------------------------------

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
            ctx.occupancy(x, z, 9);
            list.push({
                x: x,
                y: ctx.sampler(i, off, side < 0 ? 1 : 0) - 0.1,
                z: z,
                yaw: trackYaw(T, i) + (side > 0 ? Math.PI * 1.5 : Math.PI * 0.5)
            });
        }
    }
    addInstanced(ctx, 'stands', standGeometry(ctx.pal), list);
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
    scatter(ctx, rng, { step: 26 / ctx.density, jitter: 6, offMin: 4.5, offMax: 9, radius: 0.6, margin: 1.2, chance: 0.35 }, function (p) {
        const n = p.rng.int(2, 5);
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
    addInstanced(ctx, 'spectators', spectatorGeometry(), list);
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
        const side = cross > 0 ? 1 : -1;
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
    addInstanced(ctx, 'cones', coneGeometry(ctx.pal, Math.max(5, ctx.Q.seg)), list);
}

// ---------------------------------------------------------------------------
// Небо
// ---------------------------------------------------------------------------

/**
 * Градиентная полусфера на вершинных цветах. Материал неосвещаемый: Lambert
 * подмешал бы к нему направленный свет и градиент поплыл бы. Туман для купола
 * выключен, иначе небо схлопнется в один цвет тумана.
 */
function buildSkyDome(env, Q) {
    const seg = Q.skySeg;
    const geom = new THREE.SphereGeometry(900, seg, Math.max(6, seg >> 1), 0, Math.PI * 2, 0, Math.PI * 0.56);
    const g = geom.index ? geom.toNonIndexed() : geom;
    for (const name of Object.keys(g.attributes)) {
        if (name !== 'position' && name !== 'normal') g.deleteAttribute(name);
    }
    const pos = g.attributes.position;
    const col = new Float32Array(pos.count * 3);
    const zen = toColor(env.zenith);
    const hor = toColor(env.horizon);
    const tmp = new THREE.Color();
    for (let i = 0; i < pos.count; i++) {
        const t = Math.min(1, Math.max(0, pos.getY(i) / 900));
        const k = Math.pow(t, 0.55);
        tmp.setRGB(hor.r + (zen.r - hor.r) * k, hor.g + (zen.g - hor.g) * k, hor.b + (zen.b - hor.b) * k);
        col[i * 3] = tmp.r;
        col[i * 3 + 1] = tmp.g;
        col[i * 3 + 2] = tmp.b;
    }
    g.setAttribute('color', new THREE.BufferAttribute(col, 3));
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
function buildClouds(env, Q, rng) {
    const b = new MeshBuilder();
    const base = toColor(env.cloud);
    const shadow = new THREE.Color(base.r * 0.82, base.g * 0.85, base.b * 0.9);
    const n = Q.clouds;
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
        side: THREE.DoubleSide
    });
    mat.name = 'clouds';
    const mesh = new THREE.Mesh(b.build(), mat);
    mesh.name = 'clouds';
    mesh.frustumCulled = false;
    mesh.renderOrder = -9;
    return mesh;
}

export default buildScenery;
