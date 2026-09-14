/**
 * carmesh.js — [render] процедурные модели машин.
 *
 * Экспортирует buildCarMesh(shape, bodyColor, quality) из контракта (раздел 3).
 * shape — объект из поля shape в content/cars.json (формат 12.2).
 *
 * --------------------------------------------------------------------------
 * КАК РЕШЕНО ПЕРЕИСПОЛЬЗОВАНИЕ ГЕОМЕТРИИ МЕЖДУ ИГРОКАМИ
 * --------------------------------------------------------------------------
 * Тяжёлые атрибуты (position, normal) строятся ОДИН РАЗ на пару
 * (стиль машины + пресет качества) и кладутся в модульный кэш «чертежей».
 * Каждому игроку выдаётся собственный THREE.BufferGeometry, который
 * ССЫЛАЕТСЯ на те же самые объекты BufferAttribute: three.js заводит буфер GPU
 * по объекту атрибута, поэтому восемь машин одной модели держат в видеопамяти
 * одну копию вершин и нормалей.
 *
 * Личным у игрока остаётся только атрибут color кузова — маленький
 * Float32Array (около 10 КБ на машину). При сборке чертежа каждая вершина
 * помечается флагом «красится в цвет игрока»; у таких вершин в базовом цвете
 * лежит не цвет, а коэффициент затенения панели (крыша светлее, пороги
 * темнее). Личный цвет получается умножением коэффициента на цвет игрока —
 * то есть машина одного игрока отличается от машины другого одним умножением
 * по массиву, а не пересборкой геометрии.
 *
 * Своя BufferGeometry на игрока нужна ещё и потому, что кэш состояний
 * вершинных атрибутов в three.js завязан на geometry.id: две InstancedMesh с
 * общей геометрией делили бы один VAO, а матрицы инстансов у них разные.
 *
 * Освобождение: каждый чертёж считает ссылки. dispose() машины освобождает её
 * собственные геометрии-обёртки и материалы, а когда исчезает последняя машина
 * стиля — и сам чертёж. Промежуточные dispose() могут освободить общий буфер
 * вершин раньше времени, three.js в этом случае просто зальёт его заново на
 * следующем кадре (это дешевле, чем хранить копию вершин на каждого игрока).
 *
 * --------------------------------------------------------------------------
 * DRAW CALL: ЧЕТЫРЕ НА МАШИНУ
 * --------------------------------------------------------------------------
 *   body        1  весь кузов слитой геометрией с вершинными цветами
 *   wheels      1  InstancedMesh на четыре колеса (крутятся и поворачиваются)
 *   headLights  1  эмиссивные плоскости фар
 *   brakeLights 1  стоп-сигналы отдельным материалом, зажигаются по флагу
 * Восемь машин — 32 draw call, ровно как требует бюджет раздела 1.
 * На пресете high голова водителя выносится отдельным мешем (пятый вызов на
 * машину), на low и medium она слита с кузовом, а узел driver остаётся на
 * месте — рендер анимирует его всегда и без проверок на null.
 */

import * as THREE from 'three';
import { MeshBuilder, mergeGeometries, loft, solidify, toColor, disposeObject } from './geomutil.js';

// ---------------------------------------------------------------------------
// Значения по умолчанию для поля shape (12.2)
// ---------------------------------------------------------------------------

const SHAPE_DEFAULTS = {
    style: 'hatch',
    length: 4.1,
    width: 1.85,
    height: 1.24,
    wheelbase: 2.5,
    track_width: 1.62,
    wheel_radius: 0.34,
    wheel_width: 0.25,
    ride_height: 0.14,
    cabin_start: 0.34,
    cabin_end: 0.78,
    cabin_height: 0.52,
    nose_drop: 0.16,
    tail_drop: 0.08,
    taper_front: 0.82,
    taper_rear: 0.94,
    spoiler: true,
    spoiler_height: 0.34,
    accent: '#1d1f24'
};

const QUALITY = {
    low: { wheelSeg: 8, mirrors: false, extras: 0, separateDriver: false },
    medium: { wheelSeg: 12, mirrors: true, extras: 1, separateDriver: false },
    high: { wheelSeg: 16, mirrors: true, extras: 2, separateDriver: true }
};

/**
 * Характерные диапазоны пропорций стиля. cars.json пишет другой исполнитель, и
 * если его значения выйдут за диапазон, силуэт перестанет читаться (фургон без
 * короткого носа фургоном не выглядит). Поэтому доли кабины зажимаются в рамки
 * стиля: числа из cars.json уважаются, но не ломают образ из таблицы 12.2.
 */
const STYLE_RANGE = {
    hatch: { cabin_start: [0.3, 0.42], cabin_end: [0.72, 0.88] },
    muscle: { cabin_start: [0.44, 0.58], cabin_end: [0.78, 0.93] },
    van: { cabin_start: [0.1, 0.22], cabin_end: [0.88, 0.97] },
    wedge: { cabin_start: [0.4, 0.54], cabin_end: [0.82, 0.95] },
    buggy: { cabin_start: [0.3, 0.55], cabin_end: [0.7, 0.9] }
};

function clamp(v, a, b) {
    return v < a ? a : v > b ? b : v;
}

/** Подстановка умолчаний 12.2 и зажим пропорций в рамки стиля. */
function normalizeShape(shape) {
    const S = Object.assign({}, SHAPE_DEFAULTS, shape || {});
    const r = STYLE_RANGE[S.style] || STYLE_RANGE.hatch;
    S.cabin_start = clamp(S.cabin_start, r.cabin_start[0], r.cabin_start[1]);
    S.cabin_end = clamp(S.cabin_end, r.cabin_end[0], r.cabin_end[1]);
    if (S.cabin_end < S.cabin_start + 0.18) S.cabin_end = S.cabin_start + 0.18;
    S.cabin_height = clamp(S.cabin_height, 0.16, S.height - S.ride_height - 0.2);
    S.wheelbase = clamp(S.wheelbase, S.length * 0.45, S.length * 0.78);
    S.track_width = clamp(S.track_width, S.width * 0.5, S.width * 1.15);
    return S;
}

// коэффициенты затенения панелей кузова: их и умножаем на цвет игрока
const PAINT_TOP = 1.0;
const PAINT_SIDE = 0.9;
const PAINT_LOW = 0.74;
const PAINT_NOSE = 0.96;

const PAINT_FLAG = 1;

const _cacheBlueprints = new Map();

// ---------------------------------------------------------------------------
// Публичная функция
// ---------------------------------------------------------------------------

/**
 * Строит машину.
 * @param {object} shape     поле shape из cars.json (12.2)
 * @param {string|number} bodyColor цвет кузова игрока
 * @param {string} quality   low | medium | high
 * @returns объект с узлами, материалами и dispose()
 */
export function buildCarMesh(shape, bodyColor, quality) {
    const S = normalizeShape(shape);
    const q = QUALITY[quality] ? quality : 'medium';
    const bp = getBlueprint(S, q);
    bp.refs++;

    const paint = toColor(bodyColor === undefined ? '#e5484d' : bodyColor);

    const root = new THREE.Group();
    root.name = 'car_' + S.style;

    // --- кузов: общие position/normal, личный color -------------------------
    const bodyGeom = new THREE.BufferGeometry();
    bodyGeom.setAttribute('position', bp.body.position);
    bodyGeom.setAttribute('normal', bp.body.normal);
    const bodyColorAttr = new THREE.BufferAttribute(new Float32Array(bp.body.base.length), 3);
    bodyGeom.setAttribute('color', bodyColorAttr);
    bodyGeom.boundingSphere = bp.body.boundingSphere;
    bodyGeom.boundingBox = bp.body.boundingBox;

    const bodyMat = new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true });
    bodyMat.name = 'carBody';
    const body = new THREE.Mesh(bodyGeom, bodyMat);
    body.name = 'body';
    root.add(body);

    // --- колёса: InstancedMesh на четыре штуки ------------------------------
    const wheelGeom = new THREE.BufferGeometry();
    wheelGeom.setAttribute('position', bp.wheel.position);
    wheelGeom.setAttribute('normal', bp.wheel.normal);
    wheelGeom.setAttribute('color', bp.wheel.color);
    wheelGeom.boundingSphere = bp.wheel.boundingSphere;
    const wheelMesh = new THREE.InstancedMesh(wheelGeom, bp.wheelMaterial, 4);
    wheelMesh.name = 'wheels';
    wheelMesh.frustumCulled = false; // кузов уже отсекается, колёса всегда рядом
    wheelMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    root.add(wheelMesh);

    // прокси-узлы колёс: рендер крутит их rotation.x и поворачивает rotation.y,
    // затем зовёт syncWheels(). В графе сцены их нет — лишних draw call не будет.
    const wheels = [];
    for (let i = 0; i < 4; i++) {
        const w = new THREE.Object3D();
        w.position.set(bp.wheelPos[i][0], bp.wheelPos[i][1], bp.wheelPos[i][2]);
        w.matrixAutoUpdate = false;
        w.updateMatrix();
        wheelMesh.setMatrixAt(i, w.matrix);
        wheels.push(w);
    }
    wheelMesh.instanceMatrix.needsUpdate = true;

    // --- фары ---------------------------------------------------------------
    const headGeom = new THREE.BufferGeometry();
    headGeom.setAttribute('position', bp.head.position);
    headGeom.setAttribute('normal', bp.head.normal);
    headGeom.setAttribute('color', bp.head.color);
    headGeom.boundingSphere = bp.head.boundingSphere;
    const headMat = new THREE.MeshLambertMaterial({
        vertexColors: true,
        flatShading: true,
        emissive: new THREE.Color('#fff2cc'),
        emissiveIntensity: 0.85
    });
    headMat.name = 'carHeadLights';
    const headLights = new THREE.Mesh(headGeom, headMat);
    headLights.name = 'headLights';
    headLights.frustumCulled = false;
    root.add(headLights);

    // --- стоп-сигналы -------------------------------------------------------
    const brakeGeom = new THREE.BufferGeometry();
    brakeGeom.setAttribute('position', bp.brake.position);
    brakeGeom.setAttribute('normal', bp.brake.normal);
    brakeGeom.setAttribute('color', bp.brake.color);
    brakeGeom.boundingSphere = bp.brake.boundingSphere;
    const brakeMat = new THREE.MeshLambertMaterial({
        vertexColors: true,
        flatShading: true,
        emissive: new THREE.Color('#5a0d0d'),
        emissiveIntensity: 1.0
    });
    brakeMat.name = 'carBrakeLights';
    const brakeLights = new THREE.Mesh(brakeGeom, brakeMat);
    brakeLights.name = 'brakeLights';
    brakeLights.frustumCulled = false;
    root.add(brakeLights);

    // --- водитель -----------------------------------------------------------
    const driver = new THREE.Object3D();
    driver.position.set(bp.driverPos[0], bp.driverPos[1], bp.driverPos[2]);
    root.add(driver);
    let driverMesh = null;
    if (bp.driverHead) {
        const dg = new THREE.BufferGeometry();
        dg.setAttribute('position', bp.driverHead.position);
        dg.setAttribute('normal', bp.driverHead.normal);
        dg.setAttribute('color', bp.driverHead.color);
        dg.boundingSphere = bp.driverHead.boundingSphere;
        driverMesh = new THREE.Mesh(dg, bp.driverMaterial);
        driverMesh.name = 'driverHead';
        driver.add(driverMesh);
    }

    const _brakeOn = new THREE.Color('#ff2a18');
    const _brakeOff = new THREE.Color('#5a0d0d');

    const api = {
        root: root,
        body: body,
        wheels: wheels,
        wheelMesh: wheelMesh,
        brakeLights: brakeLights,
        headLights: headLights,
        driver: driver,
        driverMesh: driverMesh,
        style: S.style,
        shape: S,
        wheelRadius: S.wheel_radius,
        wheelBase: S.wheelbase,
        trackWidth: S.track_width,
        size: { length: S.length, width: S.width, height: S.height },
        materials: { body: bodyMat, wheel: bp.wheelMaterial, head: headMat, brake: brakeMat },
        stats: { drawCalls: driverMesh ? 5 : 4, triangles: bp.triangles + (driverMesh ? bp.driverTris : 0) },

        /** Перекрасить кузов (лобби, смена цвета). Геометрия не пересобирается. */
        setBodyColor: function (color) {
            toColor(color, paint);
            const base = bp.body.base;
            const mask = bp.body.mask;
            const arr = bodyColorAttr.array;
            for (let i = 0, n = mask.length; i < n; i++) {
                const o = i * 3;
                if (mask[i] === PAINT_FLAG) {
                    arr[o] = base[o] * paint.r;
                    arr[o + 1] = base[o + 1] * paint.g;
                    arr[o + 2] = base[o + 2] * paint.b;
                } else {
                    arr[o] = base[o];
                    arr[o + 1] = base[o + 1];
                    arr[o + 2] = base[o + 2];
                }
            }
            bodyColorAttr.needsUpdate = true;
        },

        /** Флаг торможения из снапшота (бит 7). Ноль аллокаций. */
        setBraking: function (on) {
            brakeMat.emissive.copy(on ? _brakeOn : _brakeOff);
        },

        /** Фары можно приглушить, например для машины-призрака. */
        setHeadlights: function (on) {
            headMat.emissiveIntensity = on ? 0.85 : 0.12;
        },

        /** Переписать матрицы инстансов колёс после поворота прокси-узлов. */
        syncWheels: function () {
            for (let i = 0; i < 4; i++) {
                const w = wheels[i];
                w.updateMatrix();
                wheelMesh.setMatrixAt(i, w.matrix);
            }
            wheelMesh.instanceMatrix.needsUpdate = true;
        },

        dispose: function () {
            bodyGeom.dispose();
            wheelGeom.dispose();
            headGeom.dispose();
            brakeGeom.dispose();
            if (driverMesh) driverMesh.geometry.dispose();
            bodyMat.dispose();
            headMat.dispose();
            brakeMat.dispose();
            if (root.parent) root.parent.remove(root);
            releaseBlueprint(bp);
        }
    };

    api.setBodyColor(paint);
    return api;
}

/** Полная очистка кэша чертежей — вызывать при возврате в лобби. */
export function disposeCarCache() {
    _cacheBlueprints.forEach(function (bp) {
        bp.wheelMaterial.dispose();
        if (bp.driverMaterial) bp.driverMaterial.dispose();
    });
    _cacheBlueprints.clear();
}

function releaseBlueprint(bp) {
    bp.refs--;
    if (bp.refs > 0) return;
    _cacheBlueprints.delete(bp.key);
    bp.wheelMaterial.dispose();
    if (bp.driverMaterial) bp.driverMaterial.dispose();
}

// ---------------------------------------------------------------------------
// Чертёж: общие атрибуты одной модели
// ---------------------------------------------------------------------------

function getBlueprint(S, quality) {
    const key = S.style + '|' + quality + '|' + JSON.stringify(S);
    let bp = _cacheBlueprints.get(key);
    if (bp) return bp;
    bp = makeBlueprint(S, quality, key);
    _cacheBlueprints.set(key, bp);
    return bp;
}

function attrsOf(geometry) {
    geometry.computeBoundingSphere();
    geometry.computeBoundingBox();
    return {
        position: geometry.attributes.position,
        normal: geometry.attributes.normal,
        color: geometry.attributes.color,
        boundingSphere: geometry.boundingSphere,
        boundingBox: geometry.boundingBox
    };
}

function makeBlueprint(S, quality, key) {
    const P = QUALITY[quality];
    const accent = toColor(S.accent);

    // --- кузов --------------------------------------------------------------
    const bb = new MeshBuilder();
    const ctx = buildBody(bb, S, P, accent);

    let driverHead = null;
    let driverTris = 0;
    let driverMaterial = null;
    const headPos = ctx.driverPos;
    if (P.separateDriver) {
        const db = new MeshBuilder();
        buildDriverHead(db, S, accent, 0, 0, 0);
        const dg = db.build();
        driverHead = attrsOf(dg);
        driverTris = db.triangleCount;
        driverMaterial = new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true });
        driverMaterial.name = 'carDriver';
    } else {
        buildDriverHead(bb, S, accent, headPos[0], headPos[1], headPos[2]);
    }

    const bodyGeom = bb.build();
    const bodyAttrs = attrsOf(bodyGeom);
    const base = bodyGeom.attributes.color.array;
    const mask = bb.buildFlags();

    // --- колесо -------------------------------------------------------------
    const wheelGeom = buildWheel(S, P);
    const wheelAttrs = attrsOf(wheelGeom);
    const wheelMaterial = new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true });
    wheelMaterial.name = 'carWheel';

    // --- фары и стопы -------------------------------------------------------
    const hb = new MeshBuilder();
    buildHeadLights(hb, S, ctx);
    const headGeomB = hb.build();

    const sb = new MeshBuilder();
    buildBrakeLights(sb, S, ctx);
    const brakeGeomB = sb.build();

    const halfTrack = S.track_width * 0.5;
    const halfBase = S.wheelbase * 0.5;
    const r = S.wheel_radius;

    return {
        key: key,
        refs: 0,
        body: { position: bodyAttrs.position, normal: bodyAttrs.normal, base: base, mask: mask, boundingSphere: bodyAttrs.boundingSphere, boundingBox: bodyAttrs.boundingBox },
        wheel: wheelAttrs,
        wheelMaterial: wheelMaterial,
        head: attrsOf(headGeomB),
        brake: attrsOf(brakeGeomB),
        driverHead: driverHead,
        driverMaterial: driverMaterial,
        driverTris: driverTris,
        driverPos: headPos,
        wheelPos: [
            [halfTrack, r, halfBase],
            [-halfTrack, r, halfBase],
            [halfTrack, r, -halfBase],
            [-halfTrack, r, -halfBase]
        ],
        triangles: bb.triangleCount + hb.triangleCount + sb.triangleCount + (wheelGeom.attributes.position.count / 3) * 4
    };
}

// ---------------------------------------------------------------------------
// Сечения кузова
// ---------------------------------------------------------------------------

/**
 * Сечение кузова: шестиугольник, обход по часовой при взгляде из +Z,
 * тогда нормали лофта смотрят наружу.
 *   k = 0 верх, 1 правый верх, 2 правый низ, 3 низ, 4 левый низ, 5 левый верх
 */
function section(t, L, hw, yb, yt, topK, botK, midK) {
    const w = hw;
    const wt = hw * (topK === undefined ? 0.86 : topK);
    const wb = hw * (botK === undefined ? 0.8 : botK);
    const ym = yb + (yt - yb) * (midK === undefined ? 0.55 : midK);
    return {
        z: (t - 0.5) * L,
        t: t,
        hw: hw,
        yb: yb,
        yt: yt,
        pts: [
            [-wt, yt],
            [wt, yt],
            [w, ym],
            [wb, yb],
            [-wb, yb],
            [-w, ym]
        ]
    };
}

function lerp(a, b, t) {
    return a + (b - a) * t;
}

/** Интерполяция значения по таблице узлов [[t, v], ...]. */
function curve(table, t) {
    if (t <= table[0][0]) return table[0][1];
    for (let i = 1; i < table.length; i++) {
        if (t <= table[i][0]) {
            const k = (t - table[i - 1][0]) / (table[i][0] - table[i - 1][0]);
            return lerp(table[i - 1][1], table[i][1], k);
        }
    }
    return table[table.length - 1][1];
}

// ---------------------------------------------------------------------------
// Кузов: общий каркас + стилевые надстройки
// ---------------------------------------------------------------------------

function buildBody(b, S, P, accent) {
    const L = S.length;
    const W = S.width;
    const H = S.height;
    const hwMax = W * 0.5;
    const RH = S.ride_height;
    const belt = H - S.cabin_height; // линия подоконника (верх кузова)
    const tRear = 0.5 - S.wheelbase / (2 * L);
    const tFront = 0.5 + S.wheelbase / (2 * L);
    const fenderHw = Math.max(hwMax, S.track_width * 0.5 + S.wheel_width * 0.5 + 0.03);

    const dark = toColor('#15171b');
    const paintTop = new THREE.Color(PAINT_TOP, PAINT_TOP, PAINT_TOP);
    const paintSide = new THREE.Color(PAINT_SIDE, PAINT_SIDE, PAINT_SIDE);
    const paintLow = new THREE.Color(PAINT_LOW, PAINT_LOW, PAINT_LOW);
    const paintNose = new THREE.Color(PAINT_NOSE, PAINT_NOSE, PAINT_NOSE);

    const style = S.style;
    const ctx = {
        L: L,
        W: W,
        H: H,
        belt: belt,
        tRear: tRear,
        tFront: tFront,
        hwNose: hwMax * S.taper_front,
        hwTail: hwMax * S.taper_rear,
        yNose: belt - S.nose_drop,
        yTail: belt - S.tail_drop,
        accent: accent,
        driverPos: [0, belt + 0.34, 0]
    };

    // ---- таблицы силуэта по стилям ----
    let stations = [];
    let cabin = null;

    if (style === 'buggy') {
        // багги: короткий узкий корпус, высокий клиренс, открытый верх
        const bh = hwMax * 0.66;
        const yb = RH + 0.06;
        const yt = RH + 0.46;
        stations = [
            section(0.02, L, bh * 0.74, yb + 0.06, yt - 0.06),
            section(0.14, L, bh * 0.96, yb, yt + 0.02),
            section(0.36, L, bh, yb, yt + 0.06),
            section(0.62, L, bh, yb, yt + 0.04),
            section(0.84, L, bh * 0.9, yb + 0.02, yt - 0.06),
            section(0.97, L, bh * 0.6, yb + 0.1, yt - 0.16)
        ];
        ctx.yNose = yt - 0.16;
        ctx.yTail = yt - 0.06;
        ctx.hwNose = bh * 0.6;
        ctx.hwTail = bh * 0.74;
        ctx.driverPos = [0, yt + 0.42, -0.02 * L];
    } else if (style === 'van') {
        // фургон: высокий, почти без сужений, кабина начинается сразу за носом
        const yt = belt;
        stations = [
            section(0.0, L, hwMax * S.taper_rear, RH + 0.02, yt - S.tail_drop, 0.97, 0.9),
            section(0.06, L, hwMax * 0.99, RH, yt, 0.97, 0.9),
            section(tRear, L, fenderHw, RH, yt, 0.96, 0.88),
            section(0.5, L, hwMax, RH, yt, 0.96, 0.88),
            section(tFront, L, fenderHw, RH, yt, 0.96, 0.88),
            section(0.94, L, hwMax * 0.98, RH + 0.02, yt - S.nose_drop * 0.5, 0.95, 0.88),
            section(1.0, L, hwMax * S.taper_front, RH + 0.06, yt - S.nose_drop, 0.94, 0.86)
        ];
        cabin = [
            { t: S.cabin_start, hwK: 0.9, yt: belt + 0.06 },
            { t: S.cabin_start + 0.07, hwK: 0.96, yt: H },
            { t: S.cabin_end - 0.03, hwK: 0.97, yt: H },
            { t: S.cabin_end, hwK: 0.95, yt: H - 0.06 }
        ];
    } else if (style === 'wedge') {
        // клин: нос почти у земли, единая линия до кормы
        const lowNose = RH + 0.1;
        stations = [
            section(0.0, L, hwMax * S.taper_rear, RH + 0.02, belt - S.tail_drop, 0.8, 0.82),
            section(0.1, L, hwMax * 1.01, RH, belt, 0.8, 0.82),
            section(tRear, L, fenderHw * 1.02, RH, belt, 0.78, 0.82),
            section(0.48, L, hwMax * 0.99, RH, belt - 0.05, 0.74, 0.8),
            section(tFront, L, fenderHw, RH, belt - 0.14, 0.7, 0.78),
            section(0.9, L, hwMax * 0.86, RH + 0.02, lowNose + 0.1, 0.62, 0.72),
            section(1.0, L, hwMax * S.taper_front * 0.72, RH + 0.08, lowNose, 0.5, 0.6)
        ];
        ctx.yNose = lowNose;
        ctx.hwNose = hwMax * S.taper_front * 0.72;
        cabin = [
            { t: S.cabin_start, hwK: 0.8, yt: belt - 0.06 },
            { t: S.cabin_start + 0.16, hwK: 0.85, yt: H },
            { t: S.cabin_end - 0.04, hwK: 0.86, yt: H },
            { t: S.cabin_end, hwK: 0.84, yt: belt + 0.02 }
        ];
    } else if (style === 'muscle') {
        // маслкар: длинный плоский капот, широкая корма
        stations = [
            section(0.0, L, hwMax * S.taper_rear, RH + 0.04, belt - S.tail_drop, 0.9, 0.84),
            section(0.08, L, hwMax * 1.03, RH, belt, 0.9, 0.84),
            section(tRear, L, fenderHw * 1.06, RH, belt + 0.02, 0.88, 0.84),
            section(0.46, L, hwMax * 0.98, RH, belt, 0.86, 0.82),
            section(tFront, L, fenderHw * 1.02, RH, belt - 0.02, 0.86, 0.82),
            section(0.92, L, hwMax * 0.95, RH + 0.02, belt - S.nose_drop * 0.7, 0.86, 0.8),
            section(1.0, L, hwMax * S.taper_front, RH + 0.06, belt - S.nose_drop, 0.84, 0.78)
        ];
        cabin = [
            { t: S.cabin_start, hwK: 0.82, yt: belt + 0.02 },
            { t: S.cabin_start + 0.12, hwK: 0.87, yt: H },
            { t: S.cabin_end - 0.05, hwK: 0.87, yt: H },
            { t: S.cabin_end, hwK: 0.8, yt: belt + 0.04 }
        ];
    } else {
        // hatch и всё незнакомое: компактный трёхдверный силуэт
        stations = [
            section(0.0, L, hwMax * S.taper_rear, RH + 0.05, belt - S.tail_drop, 0.88, 0.82),
            section(0.07, L, hwMax * 0.99, RH, belt, 0.88, 0.82),
            section(tRear, L, fenderHw, RH, belt + 0.01, 0.87, 0.82),
            section(0.5, L, hwMax * 0.97, RH, belt, 0.86, 0.8),
            section(tFront, L, fenderHw, RH, belt, 0.86, 0.8),
            section(0.93, L, hwMax * 0.93, RH + 0.02, belt - S.nose_drop * 0.6, 0.85, 0.8),
            section(1.0, L, hwMax * S.taper_front, RH + 0.07, belt - S.nose_drop, 0.82, 0.76)
        ];
        cabin = [
            { t: S.cabin_start, hwK: 0.84, yt: belt + 0.04 },
            { t: S.cabin_start + 0.13, hwK: 0.89, yt: H },
            { t: S.cabin_end - 0.02, hwK: 0.88, yt: H },
            { t: S.cabin_end, hwK: 0.8, yt: belt + 0.1 }
        ];
    }

    // ---- лофт кузова ----
    b.flag = PAINT_FLAG;
    const lastIdx = stations.length - 1;
    loft(b, stations, {
        capStart: true,
        capEnd: true,
        color: function (i, k, c) {
            if (i < 0) {
                // кормовой торец: крышка багажника / задние двери
                b.flag = 0;
                c.copy(accent);
                return;
            }
            if (i >= stations.length) {
                b.flag = 0;
                c.copy(accent);
                return;
            }
            if (k === 3) {
                b.flag = 0;
                c.copy(dark); // днище
                return;
            }
            b.flag = PAINT_FLAG;
            if (k === 0) {
                c.copy(i >= lastIdx - 1 ? paintNose : paintTop);
            } else if (k === 2 || k === 4) {
                c.copy(paintLow);
            } else {
                c.copy(paintSide);
            }
        }
    });
    b.flag = PAINT_FLAG;

    // ---- кабина ----
    if (cabin) {
        const secs = [];
        for (let i = 0; i < cabin.length; i++) {
            const cdef = cabin[i];
            const bodyHw = curve(
                stations.map(function (s) {
                    return [s.t, s.hw];
                }),
                cdef.t
            );
            const bodyTop = curve(
                stations.map(function (s) {
                    return [s.t, s.yt];
                }),
                cdef.t
            );
            secs.push(section(cdef.t, L, bodyHw * cdef.hwK, bodyTop - 0.03, cdef.yt, 0.9, 0.98, 0.6));
        }
        const nl = secs.length - 1;
        loft(b, secs, {
            capStart: false,
            capEnd: false,
            color: function (i, k, c) {
                if (k === 3) {
                    b.flag = 0;
                    c.copy(dark);
                    return;
                }
                if (k === 0) {
                    if (i === 0 || i === nl - 1) {
                        b.flag = 0;
                        c.copy(accent); // лобовое и заднее стекло
                    } else {
                        b.flag = PAINT_FLAG;
                        c.copy(paintTop); // крыша
                    }
                    return;
                }
                if ((k === 1 || k === 5) && i > 0 && i < nl - 1) {
                    b.flag = 0;
                    c.copy(accent); // боковые стёкла
                    return;
                }
                b.flag = PAINT_FLAG;
                c.copy(k === 2 || k === 4 ? paintLow : paintSide);
            }
        });
        b.flag = PAINT_FLAG;
    }

    // ---- бамперы, юбки, зеркала и прочая мелочь ----
    addCommonExtras(b, S, P, ctx, accent, dark, paintSide);

    // ---- стилевые детали ----
    if (style === 'muscle') addMuscleExtras(b, S, P, ctx, accent, dark);
    else if (style === 'buggy') addBuggyExtras(b, S, P, ctx, accent, dark);
    else if (style === 'van') addVanExtras(b, S, P, ctx, accent, dark);
    else if (style === 'wedge') addWedgeExtras(b, S, P, ctx, accent, dark);
    else addHatchExtras(b, S, P, ctx, accent, dark);

    b.flag = 0;
    return ctx;
}

// --- общая мелочь ----------------------------------------------------------

function addCommonExtras(b, S, P, ctx, accent, dark, paintSide) {
    const L = S.length;
    const zNose = L * 0.5;
    const zTail = -L * 0.5;
    const RH = S.ride_height;

    b.flag = 0;
    // передний бампер
    b.box(0, RH + 0.13, zNose - 0.1, ctx.hwNose * 1.9, 0.26, 0.3, accent);
    // задний бампер
    b.box(0, RH + 0.13, zTail + 0.1, ctx.hwTail * 1.9, 0.26, 0.3, accent);
    // пороги между колёсами
    const rockerZ = 0;
    const rockerLen = S.wheelbase * 0.78;
    b.box(S.width * 0.47, RH + 0.04, rockerZ, 0.1, 0.12, rockerLen, dark);
    b.box(-S.width * 0.47, RH + 0.04, rockerZ, 0.1, 0.12, rockerLen, dark);

    // зеркала
    if (P.mirrors && S.style !== 'wedge') {
        const zm = (S.cabin_start - 0.5) * L + 0.16;
        const hwm = S.width * 0.5;
        b.box(hwm * 1.02, ctx.belt + 0.16, zm, 0.18, 0.09, 0.07, accent);
        b.box(-hwm * 1.02, ctx.belt + 0.16, zm, 0.18, 0.09, 0.07, accent);
    }

    // выхлоп
    if (S.style !== 'buggy') {
        b.box(S.width * 0.22, RH + 0.02, zTail - 0.04, 0.09, 0.09, 0.14, toColor('#9aa0a6'));
    }
}

function addHatchExtras(b, S, P, ctx, accent, dark) {
    const L = S.length;
    b.flag = 0;
    // решётка радиатора
    b.box(0, ctx.yNose - 0.12, L * 0.5 - 0.02, ctx.hwNose * 1.1, 0.14, 0.06, dark);
    if (S.spoiler) {
        // козырёк над задним стеклом
        const zr = (S.cabin_end - 0.5) * L - 0.02;
        b.flag = PAINT_FLAG;
        b.box(0, S.height + 0.03, zr, S.width * 0.72, 0.07, 0.3, new THREE.Color(PAINT_TOP, PAINT_TOP, PAINT_TOP));
        b.flag = 0;
    }
    if (P.extras > 0) {
        // накладки на арки
        const fh = S.track_width * 0.5 + S.wheel_width * 0.5;
        for (let s = -1; s <= 1; s += 2) {
            b.box(s * fh, S.wheel_radius + 0.26, S.wheelbase * 0.5, 0.09, 0.1, S.wheel_radius * 2.1, dark);
            b.box(s * fh, S.wheel_radius + 0.26, -S.wheelbase * 0.5, 0.09, 0.1, S.wheel_radius * 2.1, dark);
        }
    }
    if (P.extras > 1) {
        // ручки дверей
        for (let s = -1; s <= 1; s += 2) {
            b.box(s * S.width * 0.5, ctx.belt - 0.06, 0.05, 0.04, 0.05, 0.22, dark);
        }
    }
}

function addMuscleExtras(b, S, P, ctx, accent, dark) {
    const L = S.length;
    const W = S.width;
    b.flag = 0;
    // воздухозаборник на капоте
    const zh = (S.cabin_start - 0.5) * L + L * 0.22;
    b.box(0, ctx.belt + 0.07, zh, W * 0.42, 0.14, L * 0.22, dark);
    b.box(0, ctx.belt + 0.14, zh + L * 0.06, W * 0.3, 0.06, L * 0.08, toColor('#0d0e10'));
    // массивная решётка и «клыки»
    b.box(0, ctx.yNose - 0.14, L * 0.5 - 0.01, ctx.hwNose * 1.5, 0.2, 0.07, toColor('#0d0e10'));
    // боковые патрубки
    for (let s = -1; s <= 1; s += 2) {
        b.box(s * W * 0.52, S.ride_height + 0.1, 0, 0.12, 0.12, S.wheelbase * 0.72, toColor('#b9bec4'));
    }
    // антикрыло на корме
    if (S.spoiler) {
        const zt = -L * 0.5 + 0.26;
        const yw = ctx.belt + S.spoiler_height;
        for (let s = -1; s <= 1; s += 2) {
            b.box(s * W * 0.34, yw - 0.16, zt, 0.09, 0.32, 0.14, dark);
        }
        b.flag = PAINT_FLAG;
        b.box(0, yw, zt, W * 0.94, 0.08, 0.34, new THREE.Color(PAINT_TOP, PAINT_TOP, PAINT_TOP));
        b.flag = 0;
    }
    if (P.extras > 0) {
        // задний фонарь-панель во всю ширину
        b.box(0, ctx.yTail - 0.1, -L * 0.5 - 0.005, ctx.hwTail * 1.7, 0.12, 0.03, toColor('#2a0f10'));
    }
}

function addBuggyExtras(b, S, P, ctx, accent, dark) {
    const L = S.length;
    const W = S.width;
    const RH = S.ride_height;
    const tube = toColor('#c8ccd0');
    const cageTop = ctx.driverPos[1] + 0.34;
    const hw = W * 0.44;
    const zBack = -L * 0.14;
    const zFront = L * 0.2;

    b.flag = 0;
    // каркас безопасности: четыре стойки, два лонжерона, поперечина, раскосы
    for (let s = -1; s <= 1; s += 2) {
        b.box(s * hw, (cageTop + RH) * 0.5, zBack, 0.08, cageTop - RH, 0.08, tube);
        b.box(s * hw, (cageTop + RH) * 0.5 + 0.02, zFront, 0.08, cageTop - RH - 0.1, 0.08, tube);
        b.box(s * hw, cageTop, (zBack + zFront) * 0.5, 0.08, 0.08, zFront - zBack, tube);
        // раскос назад
        b.box(s * hw * 0.98, RH + 0.36, zBack - 0.36, 0.07, 0.62, 0.07, tube);
    }
    b.box(0, cageTop, zBack, hw * 2, 0.08, 0.08, tube);
    b.box(0, cageTop, zFront, hw * 2, 0.08, 0.08, tube);
    b.box(0, ctx.driverPos[1] - 0.1, zBack - 0.02, hw * 2, 0.07, 0.07, tube);

    // мотор за спиной водителя
    b.box(0, RH + 0.34, -L * 0.34, W * 0.46, 0.4, L * 0.2, dark);
    for (let s = -1; s <= 1; s += 2) {
        b.box(s * W * 0.13, RH + 0.68, -L * 0.34, 0.07, 0.3, 0.07, toColor('#8d9298'));
    }
    // сиденье
    b.box(0, ctx.driverPos[1] - 0.26, ctx.driverPos[2] - 0.14, 0.44, 0.46, 0.1, toColor('#2b2f36'));

    // крылья над колёсами
    const fx = S.track_width * 0.5;
    for (let s = -1; s <= 1; s += 2) {
        for (let f = -1; f <= 1; f += 2) {
            b.flag = PAINT_FLAG;
            b.box(
                s * fx,
                S.wheel_radius + 0.2,
                f * S.wheelbase * 0.5,
                S.wheel_width + 0.12,
                0.09,
                S.wheel_radius * 2.2,
                new THREE.Color(PAINT_SIDE, PAINT_SIDE, PAINT_SIDE)
            );
            b.flag = 0;
        }
    }
    // силовой бампер спереди
    b.box(0, RH + 0.24, L * 0.5 - 0.04, W * 0.66, 0.09, 0.09, tube);
    for (let s = -1; s <= 1; s += 2) {
        b.box(s * W * 0.3, RH + 0.14, L * 0.5 - 0.04, 0.08, 0.28, 0.08, tube);
    }
    if (P.extras > 0) {
        // рычаги подвески
        for (let s = -1; s <= 1; s += 2) {
            for (let f = -1; f <= 1; f += 2) {
                b.box(s * fx * 0.62, S.wheel_radius * 0.7, f * S.wheelbase * 0.5, fx * 0.7, 0.07, 0.07, dark);
            }
        }
    }
}

function addVanExtras(b, S, P, ctx, accent, dark) {
    const L = S.length;
    const W = S.width;
    b.flag = 0;
    // широкая решётка и фартук
    b.box(0, ctx.yNose - 0.1, L * 0.5 - 0.01, ctx.hwNose * 1.8, 0.18, 0.05, dark);
    // багажник на крыше
    const rackY = S.height + 0.06;
    for (let s = -1; s <= 1; s += 2) {
        b.box(s * W * 0.4, rackY, 0.02 * L, 0.07, 0.09, L * 0.5, toColor('#b6bbc0'));
    }
    for (let i = -1; i <= 1; i++) {
        b.box(0, rackY + 0.01, i * L * 0.16, W * 0.82, 0.05, 0.09, toColor('#b6bbc0'));
    }
    // молдинг по борту
    for (let s = -1; s <= 1; s += 2) {
        b.box(s * W * 0.5, ctx.belt - 0.12, 0, 0.05, 0.1, L * 0.62, dark);
    }
    if (P.extras > 0) {
        // задние двери: вертикальная стойка
        b.box(0, ctx.belt * 0.6 + 0.2, -L * 0.5 - 0.01, 0.06, ctx.belt * 0.8, 0.04, dark);
    }
}

function addWedgeExtras(b, S, P, ctx, accent, dark) {
    const L = S.length;
    const W = S.width;
    b.flag = 0;
    // боковые воздухозаборники
    for (let s = -1; s <= 1; s += 2) {
        b.box(s * W * 0.49, ctx.belt - 0.14, -L * 0.04, 0.06, 0.16, L * 0.2, toColor('#0d0e10'));
    }
    // сплиттер
    b.box(0, S.ride_height + 0.02, L * 0.5 - 0.14, W * 0.92, 0.05, 0.34, dark);
    // диффузор
    b.box(0, S.ride_height + 0.03, -L * 0.5 + 0.16, W * 0.86, 0.08, 0.3, dark);
    // антикрыло
    if (S.spoiler) {
        const zt = -L * 0.5 + 0.16;
        const yw = ctx.belt + S.spoiler_height + 0.08;
        for (let s = -1; s <= 1; s += 2) {
            b.box(s * W * 0.38, (yw + ctx.belt) * 0.5, zt, 0.07, yw - ctx.belt, 0.16, dark);
        }
        b.flag = PAINT_FLAG;
        b.box(0, yw, zt, W * 1.0, 0.06, 0.38, new THREE.Color(PAINT_TOP, PAINT_TOP, PAINT_TOP));
        b.flag = 0;
        b.box(0, yw + 0.05, zt - 0.16, W * 1.0, 0.09, 0.07, dark);
    }
    // корпуса фар заподлицо
    for (let s = -1; s <= 1; s += 2) {
        b.box(s * ctx.hwNose * 0.62, ctx.yNose + 0.01, L * 0.5 - 0.34, 0.3, 0.06, 0.4, dark);
    }
}

// --- голова водителя -------------------------------------------------------

function buildDriverHead(b, S, accent, ox, oy, oz) {
    const helmet = toColor('#e8eaed');
    const visor = toColor('#12161c');
    const flagSave = b.flag;
    b.flag = 0;
    const r = 0.115;
    // шлем: гранёный многогранник восемью квадами — дёшево и читаемо
    const rows = [
        { y: -r * 0.95, s: 0.45 },
        { y: -r * 0.4, s: 0.95 },
        { y: r * 0.35, s: 0.92 },
        { y: r * 0.95, s: 0.4 }
    ];
    const seg = 6;
    for (let i = 0; i < rows.length - 1; i++) {
        for (let k = 0; k < seg; k++) {
            const a0 = ((k / seg) * Math.PI * 2);
            const a1 = (((k + 1) / seg) * Math.PI * 2);
            const r0 = r * rows[i].s,
                r1 = r * rows[i + 1].s;
            const y0 = oy + rows[i].y,
                y1 = oy + rows[i + 1].y;
            const col = i === 1 && (k === 0 || k === seg - 1) ? visor : helmet;
            b.quad(
                [ox + Math.sin(a0) * r0, y0, oz + Math.cos(a0) * r0],
                [ox + Math.sin(a0) * r1, y1, oz + Math.cos(a0) * r1],
                [ox + Math.sin(a1) * r1, y1, oz + Math.cos(a1) * r1],
                [ox + Math.sin(a1) * r0, y0, oz + Math.cos(a1) * r0],
                col
            );
        }
    }
    // макушка и подбородок
    b.box(ox, oy + r * 1.05, oz, r * 0.8, r * 0.2, r * 0.8, helmet);
    // визор спереди
    b.box(ox, oy - r * 0.05, oz + r * 0.86, r * 1.25, r * 0.62, r * 0.25, visor);
    // плечи
    b.box(ox, oy - r * 2.6, oz - 0.04, 0.42, 0.3, 0.24, toColor('#2f3440'));
    b.flag = flagSave;
}

// --- фары и стопы ----------------------------------------------------------

function buildHeadLights(b, S, ctx) {
    const L = S.length;
    const glass = toColor('#fff6d8');
    const z = L * 0.5 + 0.012;
    const y = ctx.yNose - 0.1;
    const hw = ctx.hwNose;

    if (S.style === 'muscle') {
        // четыре круглые фары
        for (let s = -1; s <= 1; s += 2) {
            for (let i = 0; i < 2; i++) {
                const x = s * (hw * 0.42 + i * 0.2);
                lightQuad(b, x, y, z, 0.17, 0.17, glass, 1);
            }
        }
    } else if (S.style === 'buggy') {
        // пара круглых прожекторов на дуге
        for (let s = -1; s <= 1; s += 2) {
            lightQuad(b, s * S.width * 0.2, ctx.driverPos[1] + 0.18, L * 0.2, 0.2, 0.2, glass, 1);
        }
    } else if (S.style === 'wedge') {
        for (let s = -1; s <= 1; s += 2) {
            lightQuad(b, s * hw * 0.62, ctx.yNose + 0.045, L * 0.5 - 0.34, 0.26, 0.36, glass, 0);
        }
    } else if (S.style === 'van') {
        for (let s = -1; s <= 1; s += 2) {
            lightQuad(b, s * hw * 0.7, y + 0.04, z, 0.34, 0.2, glass, 1);
        }
    } else {
        for (let s = -1; s <= 1; s += 2) {
            lightQuad(b, s * hw * 0.66, y, z, 0.32, 0.15, glass, 1);
        }
    }
}

function buildBrakeLights(b, S, ctx) {
    const L = S.length;
    const lamp = toColor('#ff4d3d');
    const z = -L * 0.5 - 0.012;
    const y = ctx.yTail - 0.1;
    const hw = ctx.hwTail;

    if (S.style === 'muscle') {
        for (let s = -1; s <= 1; s += 2) {
            lightQuad(b, s * hw * 0.55, y, z, hw * 0.76, 0.12, lamp, -1);
        }
    } else if (S.style === 'buggy') {
        for (let s = -1; s <= 1; s += 2) {
            lightQuad(b, s * S.width * 0.26, S.ride_height + 0.5, -L * 0.44, 0.14, 0.14, lamp, -1);
        }
    } else if (S.style === 'van') {
        for (let s = -1; s <= 1; s += 2) {
            lightQuad(b, s * hw * 0.82, y + 0.16, z, 0.16, 0.42, lamp, -1);
        }
    } else if (S.style === 'wedge') {
        for (let s = -1; s <= 1; s += 2) {
            lightQuad(b, s * hw * 0.5, y + 0.04, z, hw * 0.7, 0.1, lamp, -1);
        }
    } else {
        for (let s = -1; s <= 1; s += 2) {
            lightQuad(b, s * hw * 0.72, y + 0.05, z, 0.24, 0.22, lamp, -1);
        }
    }
}

/** Плоский светящийся прямоугольник. dir: +1 вперёд, -1 назад, 0 вверх. */
function lightQuad(b, x, y, z, w, h, color, dir) {
    const hwq = w * 0.5,
        hhq = h * 0.5;
    if (dir === 1) {
        b.quad([x - hwq, y - hhq, z], [x + hwq, y - hhq, z], [x + hwq, y + hhq, z], [x - hwq, y + hhq, z], color);
    } else if (dir === -1) {
        b.quad([x + hwq, y - hhq, z], [x - hwq, y - hhq, z], [x - hwq, y + hhq, z], [x + hwq, y + hhq, z], color);
    } else {
        b.quad([x - hwq, y, z - hhq], [x - hwq, y, z + hhq], [x + hwq, y, z + hhq], [x + hwq, y, z - hhq], color);
    }
}

// --- колесо ----------------------------------------------------------------

function buildWheel(S, P) {
    const r = S.wheel_radius;
    const w = S.wheel_width;
    const seg = P.wheelSeg;
    const tire = toColor('#1b1c1f');
    const rim = toColor('#c6cbd1');
    const hub = toColor('#54585e');

    // протектор: открытый цилиндр вдоль оси X
    const tread = solidify(new THREE.CylinderGeometry(r, r, w, seg, 1, true), tire, function (x, y, z, i, c) {
        // лёгкая грань между блоками протектора
        if ((i >> 1) % 3 === 0) c.setRGB(c.r * 1.25, c.g * 1.25, c.b * 1.25);
    });
    tread.applyMatrix4(new THREE.Matrix4().makeRotationZ(Math.PI * 0.5));

    const parts = [tread];
    const mats = [null];
    for (let s = -1; s <= 1; s += 2) {
        const disc = solidify(new THREE.CircleGeometry(r * 0.995, seg), rim, function (x, y, z, i, c) {
            const d = Math.sqrt(x * x + y * y) / r;
            if (d < 0.35) c.copy(hub);
            else if (d > 0.86) c.setRGB(c.r * 0.55, c.g * 0.55, c.b * 0.55);
        });
        const m = new THREE.Matrix4().makeRotationY((s * Math.PI) / 2);
        m.setPosition(s * (w * 0.5 - 0.012), 0, 0);
        parts.push(disc);
        mats.push(m);
    }
    return mergeGeometries(parts, mats);
}

export default buildCarMesh;
