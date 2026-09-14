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
 *
 * Голова водителя намеренно слита с кузовом: отдельный меш стоил бы ещё восемь
 * draw call на восьмерых игроков (13 % всего кадрового бюджета) ради детали
 * высотой в несколько пикселей. Узел driver при этом возвращается всегда — это
 * точка головы водителя в системе координат машины: рендер вешает на него
 * камеру из кокпита (10.3) и всё, что должно ехать вместе с головой.
 */

import * as THREE from 'three';
import {
    MeshBuilder,
    mergeGeometries,
    loft,
    solidify,
    toColor,
    bakeContactAO,
    bakeProximityAO
} from './geomutil.js';

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
    low: { wheelSeg: 8, mirrors: false, extras: 0 },
    medium: { wheelSeg: 12, mirrors: true, extras: 1 },
    high: { wheelSeg: 16, mirrors: true, extras: 2 }
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
    wedge: { cabin_start: [0.38, 0.54], cabin_end: [0.7, 0.95] },
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
export function buildCarMesh(shape, bodyColor, quality, opts) {
    const S = normalizeShape(shape);
    const q = QUALITY[quality] ? quality : 'medium';
    // Запекание затенения — отдельная настройка графики. Она меняет атрибут
    // color чертежа, поэтому входит в ключ кэша: машины с затенением и без
    // него не должны делить один буфер вершин.
    const ao = !opts || opts.ao !== false;
    const bp = getBlueprint(S, q, ao);
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

    // --- водитель: узел-якорь в точке головы (сама голова слита с кузовом) ---
    const driver = new THREE.Object3D();
    driver.name = 'driver';
    driver.position.set(bp.driverPos[0], bp.driverPos[1], bp.driverPos[2]);
    root.add(driver);

    const _brakeOn = new THREE.Color('#ff2a18');
    const _brakeOff = new THREE.Color('#5a0d0d');
    // Ночной множитель фар и стопов. Днём фары — декоративная деталь силуэта,
    // ночью они главный источник картинки, поэтому эмиссив растёт.
    let headBase = 0.85;
    let headOn = true;
    let nightK = 0;

    const api = {
        root: root,
        body: body,
        wheels: wheels,
        wheelMesh: wheelMesh,
        brakeLights: brakeLights,
        headLights: headLights,
        driver: driver,
        style: S.style,
        shape: S,
        wheelRadius: S.wheel_radius,
        wheelBase: S.wheelbase,
        trackWidth: S.track_width,
        size: { length: S.length, width: S.width, height: S.height },
        materials: { body: bodyMat, wheel: bp.wheelMaterial, head: headMat, brake: brakeMat },
        /**
         * Якоря накладного свечения в системе координат машины: плоские
         * массивы x, y, z, размер по четыре числа на лампу. Их читает
         * renderer.js один раз при сборке гонки и раскладывает по CarView,
         * чтобы в кадре не ходить по объектам модели.
         */
        lamps: { head: bp.headLamps, brake: bp.brakeLamps, exhaust: bp.exhaust },
        stats: { drawCalls: 4, triangles: bp.triangles },

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
            headOn = !!on;
            headMat.emissiveIntensity = headOn ? headBase : headBase * 0.14;
        },

        /**
         * Ночной режим: 0 — день, 0.5 — сумерки, 1 — ночь.
         * Поднимает эмиссив фар и подогревает их цвет, а тлеющие стопы делает
         * заметнее. Ни геометрия, ни цвет кузова при этом не трогаются —
         * значит переключение стоит двух записей в материал.
         */
        setNight: function (k) {
            nightK = k > 0 ? (k > 1.5 ? 1.5 : k) : 0;
            headBase = 0.85 + nightK * 1.5;
            // значения в рабочем (линейном) пространстве: '#fff2cc' — это
            // примерно (1.00, 0.88, 0.64), ночью цвет чуть белее
            headMat.emissive.setRGB(1.0, 0.88 + nightK * 0.05, 0.64 + nightK * 0.12);
            headMat.emissiveIntensity = headOn ? headBase : headBase * 0.14;
            // '#5a0d0d' — примерно (0.105, 0.007, 0.007)
            _brakeOff.setRGB(0.105 + nightK * 0.14, 0.007 + nightK * 0.008, 0.007 + nightK * 0.008);
            brakeMat.emissive.copy(_brakeOff);
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
    });
    _cacheBlueprints.clear();
}

function releaseBlueprint(bp) {
    bp.refs--;
    if (bp.refs > 0) return;
    _cacheBlueprints.delete(bp.key);
    bp.wheelMaterial.dispose();
}

// ---------------------------------------------------------------------------
// Чертёж: общие атрибуты одной модели
// ---------------------------------------------------------------------------

function getBlueprint(S, quality, ao) {
    const key = S.style + '|' + quality + '|' + (ao ? 'ao' : 'flat') + '|' + JSON.stringify(S);
    let bp = _cacheBlueprints.get(key);
    if (bp) return bp;
    bp = makeBlueprint(S, quality, key, ao);
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

function makeBlueprint(S, quality, key, ao) {
    const P = QUALITY[quality];
    const accent = toColor(S.accent);

    // --- кузов --------------------------------------------------------------
    const bb = new MeshBuilder();
    const ctx = buildBody(bb, S, P, accent);
    const headPos = ctx.driverPos;
    buildDriverHead(bb, S, accent, headPos[0], headPos[1], headPos[2]);

    const bodyGeom = bb.build();
    if (ao) bakeCarAO(bodyGeom, S);
    const bodyAttrs = attrsOf(bodyGeom);
    const base = bodyGeom.attributes.color.array;
    const mask = bb.buildFlags();

    // --- колесо -------------------------------------------------------------
    const wheelGeom = buildWheel(S, P);
    if (ao) {
        // Колесо вращается, поэтому запекать в него направленное затенение
        // нельзя — тень крутилась бы вместе с диском. Колесу достаётся ровное
        // приглушение: оно и правда сидит в тени арки.
        const wc = wheelGeom.attributes.color.array;
        for (let i = 0; i < wc.length; i++) wc[i] *= 0.84;
    }
    const wheelAttrs = attrsOf(wheelGeom);
    const wheelMaterial = new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true });
    wheelMaterial.name = 'carWheel';

    // --- фары и стопы -------------------------------------------------------
    const hb = new MeshBuilder();
    const headLamps = buildHeadLights(hb, S, ctx);
    const headGeomB = hb.build();

    const sb = new MeshBuilder();
    const brakeLamps = buildBrakeLights(sb, S, ctx);
    const brakeGeomB = sb.build();

    const halfTrack = S.track_width * 0.5;
    const halfBase = S.wheelbase * 0.5;
    const r = S.wheel_radius;

    // Точки выхлопа для следа турбо: две трубы по бортам за кормой.
    const exhaust = new Float32Array([
        S.width * 0.22, S.ride_height + 0.1, -S.length * 0.5 - 0.06, 0.34,
        -S.width * 0.22, S.ride_height + 0.1, -S.length * 0.5 - 0.06, 0.34
    ]);

    return {
        key: key,
        refs: 0,
        body: { position: bodyAttrs.position, normal: bodyAttrs.normal, base: base, mask: mask, boundingSphere: bodyAttrs.boundingSphere, boundingBox: bodyAttrs.boundingBox },
        wheel: wheelAttrs,
        wheelMaterial: wheelMaterial,
        head: attrsOf(headGeomB),
        brake: attrsOf(brakeGeomB),
        // якоря накладного свечения (effects.js): x, y, z, размер — по четыре
        headLamps: headLamps,
        brakeLamps: brakeLamps,
        exhaust: exhaust,
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
    const fenderHw = Math.max(hwMax, S.track_width * 0.5 + S.wheel_width * 0.5 + 0.04);

    // Доли cabin_start/cabin_end в 12.2 отсчитываются ОТ НОСА, а станции кузова
    // здесь нумеруются от кормы (t = 0) к носу (t = 1) — переводим.
    const tCabRear = 1 - S.cabin_end;
    const tCabFront = 1 - S.cabin_start;

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
        RH: RH,
        tRear: tRear,
        tFront: tFront,
        tCabRear: tCabRear,
        tCabFront: tCabFront,
        hwNose: hwMax * S.taper_front,
        hwTail: hwMax * S.taper_rear,
        yNose: belt - S.nose_drop,
        yTail: belt - S.tail_drop,
        rockerX: hwMax * 0.72,
        accent: accent,
        driverPos: [0, belt + 0.34, 0]
    };

    // ---- таблицы силуэта по стилям ----
    // botK — ширина низа кузова. Она заметно меньше ширины по «талии»: именно
    // за счёт этого колёса видны в арках, а не утоплены заподлицо в борт.
    let stations = [];
    let cabin = null;

    if (style === 'buggy') {
        // багги: короткий узкий корпус, высокий клиренс, открытый верх
        const bh = hwMax * 0.64;
        const yb = RH + 0.06;
        const yt = RH + 0.46;
        stations = [
            section(0.02, L, bh * 0.74, yb + 0.06, yt - 0.06, 0.9, 0.86, 0.6),
            section(0.14, L, bh * 0.96, yb, yt + 0.02, 0.9, 0.86, 0.6),
            section(0.36, L, bh, yb, yt + 0.06, 0.9, 0.86, 0.6),
            section(0.62, L, bh, yb, yt + 0.04, 0.9, 0.86, 0.6),
            section(0.84, L, bh * 0.9, yb + 0.02, yt - 0.06, 0.9, 0.86, 0.6),
            section(0.97, L, bh * 0.6, yb + 0.1, yt - 0.16, 0.9, 0.86, 0.6)
        ];
        ctx.yNose = yt - 0.16;
        ctx.yTail = yt - 0.06;
        ctx.hwNose = bh * 0.6;
        ctx.hwTail = bh * 0.74;
        ctx.rockerX = bh * 0.9;
        ctx.driverPos = [0, yt + 0.44, -0.03 * L];
    } else if (style === 'van') {
        // фургон: высокий, почти без сужений, кабина начинается сразу за носом
        const yt = belt;
        const bk = 0.85;
        stations = [
            section(0.0, L, hwMax * S.taper_rear, RH + 0.02, yt - S.tail_drop, 0.97, bk, 0.6),
            section(0.06, L, hwMax * 0.99, RH, yt, 0.97, bk, 0.6),
            section(tRear, L, fenderHw, RH, yt, 0.96, bk, 0.6),
            section(0.5, L, hwMax, RH, yt, 0.96, bk, 0.6),
            section(tFront, L, fenderHw, RH, yt, 0.96, bk, 0.6),
            section(0.94, L, hwMax * 0.98, RH + 0.02, yt - S.nose_drop * 0.5, 0.95, bk, 0.6),
            section(1.0, L, hwMax * S.taper_front, RH + 0.06, yt - S.nose_drop, 0.94, bk - 0.04, 0.6)
        ];
        ctx.rockerX = hwMax * bk;
        cabin = [
            { t: tCabRear, hwK: 0.95, yt: H - 0.08 },
            { t: tCabRear + 0.04, hwK: 0.97, yt: H },
            { t: tCabFront - 0.08, hwK: 0.96, yt: H },
            { t: tCabFront, hwK: 0.9, yt: belt + 0.06 }
        ];
    } else if (style === 'wedge') {
        // клин: нос почти у земли, единая линия до кормы
        const lowNose = RH + 0.1;
        const bk = 0.7;
        stations = [
            section(0.0, L, hwMax * S.taper_rear, RH + 0.02, belt - S.tail_drop, 0.82, bk, 0.68),
            section(0.1, L, hwMax * 1.01, RH, belt, 0.82, bk, 0.68),
            section(tRear, L, fenderHw * 1.04, RH, belt, 0.8, bk, 0.68),
            section(0.48, L, hwMax * 0.99, RH, belt - 0.05, 0.76, bk, 0.68),
            section(tFront, L, fenderHw, RH, belt - 0.14, 0.72, bk, 0.68),
            section(0.9, L, hwMax * 0.86, RH + 0.02, lowNose + 0.1, 0.64, bk - 0.04, 0.68),
            section(1.0, L, hwMax * S.taper_front * 0.72, RH + 0.08, lowNose, 0.52, bk - 0.1, 0.68)
        ];
        ctx.yNose = lowNose;
        ctx.hwNose = hwMax * S.taper_front * 0.72;
        ctx.rockerX = hwMax * bk;
        cabin = [
            { t: tCabRear, hwK: 0.84, yt: belt + 0.02 },
            { t: tCabRear + 0.05, hwK: 0.86, yt: H },
            { t: tCabFront - 0.16, hwK: 0.85, yt: H },
            { t: tCabFront, hwK: 0.78, yt: belt - 0.07 }
        ];
    } else if (style === 'muscle') {
        // маслкар: длинный плоский капот, широкая корма
        const bk = 0.74;
        stations = [
            section(0.0, L, hwMax * S.taper_rear, RH + 0.04, belt - S.tail_drop, 0.92, bk, 0.64),
            section(0.08, L, hwMax * 1.03, RH, belt, 0.92, bk, 0.64),
            section(tRear, L, fenderHw * 1.07, RH, belt + 0.02, 0.9, bk, 0.64),
            section(0.46, L, hwMax * 0.98, RH, belt, 0.88, bk, 0.64),
            section(tFront, L, fenderHw * 1.03, RH, belt - 0.02, 0.88, bk, 0.64),
            section(0.92, L, hwMax * 0.95, RH + 0.02, belt - S.nose_drop * 0.7, 0.88, bk, 0.64),
            section(1.0, L, hwMax * S.taper_front, RH + 0.06, belt - S.nose_drop, 0.86, bk - 0.04, 0.64)
        ];
        ctx.rockerX = hwMax * bk;
        cabin = [
            { t: tCabRear, hwK: 0.8, yt: belt + 0.05 },
            { t: tCabRear + 0.06, hwK: 0.87, yt: H },
            { t: tCabFront - 0.13, hwK: 0.87, yt: H },
            { t: tCabFront, hwK: 0.82, yt: belt + 0.02 }
        ];
    } else {
        // hatch и всё незнакомое: компактный трёхдверный силуэт
        const bk = 0.72;
        stations = [
            section(0.0, L, hwMax * S.taper_rear, RH + 0.05, belt - S.tail_drop, 0.9, bk, 0.62),
            section(0.07, L, hwMax * 0.99, RH, belt, 0.9, bk, 0.62),
            section(tRear, L, fenderHw, RH, belt + 0.01, 0.88, bk, 0.62),
            section(0.5, L, hwMax * 0.97, RH, belt, 0.88, bk, 0.62),
            section(tFront, L, fenderHw, RH, belt, 0.88, bk, 0.62),
            section(0.93, L, hwMax * 0.93, RH + 0.02, belt - S.nose_drop * 0.6, 0.86, bk, 0.62),
            section(1.0, L, hwMax * S.taper_front, RH + 0.07, belt - S.nose_drop, 0.84, bk - 0.04, 0.62)
        ];
        ctx.rockerX = hwMax * bk;
        cabin = [
            { t: Math.max(0.08, tCabRear - 0.07), hwK: 0.8, yt: belt + 0.12 },
            { t: tCabRear + 0.06, hwK: 0.88, yt: H },
            { t: tCabFront - 0.14, hwK: 0.89, yt: H },
            { t: tCabFront, hwK: 0.84, yt: belt + 0.04 }
        ];
    }

    if (cabin) {
        // страховка от вырожденной кабины, если cars.json пришлёт крайние доли
        for (let i = 1; i < cabin.length; i++) {
            if (cabin[i].t <= cabin[i - 1].t + 0.01) cabin[i].t = cabin[i - 1].t + 0.01;
        }
        const tMid = cabin[0].t * 0.55 + cabin[cabin.length - 1].t * 0.45;
        ctx.driverPos = [0, belt + (H - belt) * 0.42, (tMid - 0.5) * L];
    }

    // ---- лофт кузова ----
    b.flag = PAINT_FLAG;
    const lastIdx = stations.length - 1;
    loft(b, stations, {
        capStart: true,
        capEnd: true,
        color: function (i, k, c) {
            if (i < 0) {
                b.flag = PAINT_FLAG; // кормовая панель — в цвет игрока
                c.copy(paintLow);
                return;
            }
            if (i >= stations.length) {
                b.flag = 0;
                c.copy(accent); // передняя маска с решёткой
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
        const hwTable = stations.map(function (s) {
            return [s.t, s.hw];
        });
        const ytTable = stations.map(function (s) {
            return [s.t, s.yt];
        });
        for (let i = 0; i < cabin.length; i++) {
            const cdef = cabin[i];
            const bodyHw = curve(hwTable, cdef.t);
            const bodyTop = curve(ytTable, cdef.t);
            secs.push(section(cdef.t, L, bodyHw * cdef.hwK, bodyTop - 0.03, cdef.yt, 0.9, 0.98, 0.62));
        }
        const nl = secs.length - 1;
        loft(b, secs, {
            capStart: true,
            capEnd: true,
            color: function (i, k, c) {
                b.flag = 0;
                if (i < 0 || i >= secs.length) {
                    c.copy(accent); // торцы теплицы: заднее стекло и лобовое
                    return;
                }
                if (k === 3) {
                    c.copy(dark);
                    return;
                }
                if (k === 0) {
                    if (i === 0 || i === nl - 1) {
                        c.copy(accent); // скаты: заднее стекло и лобовое
                    } else {
                        b.flag = PAINT_FLAG;
                        c.copy(paintTop); // крыша
                    }
                    return;
                }
                if ((k === 1 || k === 5) && i > 0 && i < nl - 1) {
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
    b.box(0, RH + 0.11, zNose - 0.12, ctx.hwNose * 1.8, 0.22, 0.26, accent);
    // задний бампер
    b.box(0, RH + 0.11, zTail + 0.12, ctx.hwTail * 1.8, 0.22, 0.26, accent);
    // пороги идут по фактической ширине низа кузова, а не по габариту
    const rockerLen = S.wheelbase * 0.8;
    b.box(ctx.rockerX + 0.03, RH + 0.05, 0, 0.08, 0.14, rockerLen, dark);
    b.box(-(ctx.rockerX + 0.03), RH + 0.05, 0, 0.08, 0.14, rockerLen, dark);

    // зеркала у основания лобового стекла
    if (P.mirrors && S.style !== 'wedge') {
        const zm = (ctx.tCabFront - 0.5) * L - 0.1;
        const hwm = S.width * 0.5;
        b.box(hwm * 0.99, ctx.belt + 0.14, zm, 0.2, 0.09, 0.07, accent);
        b.box(-hwm * 0.99, ctx.belt + 0.14, zm, 0.2, 0.09, 0.07, accent);
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
    b.box(0, ctx.RH + (ctx.yNose - ctx.RH) * 0.62, L * 0.5 - 0.02, ctx.hwNose * 1.15, 0.16, 0.06, dark);
    if (S.spoiler) {
        // козырёк над задним стеклом, на задней кромке крыши
        const zr = (ctx.tCabRear - 0.5) * L + 0.16;
        b.flag = PAINT_FLAG;
        b.box(0, S.height + 0.02, zr, S.width * 0.7, 0.06, 0.28, new THREE.Color(PAINT_TOP, PAINT_TOP, PAINT_TOP));
        b.flag = 0;
    }
    if (P.extras > 0) {
        // ручки дверей
        for (let s = -1; s <= 1; s += 2) {
            b.box(s * S.width * 0.49, ctx.belt - 0.08, (ctx.tCabRear - 0.5) * L + 0.5, 0.04, 0.05, 0.22, dark);
        }
    }
    if (P.extras > 1) {
        // антенна на крыше
        b.box(0, S.height + 0.11, (ctx.tCabRear - 0.5) * L + 0.12, 0.04, 0.22, 0.04, dark);
    }
}

function addMuscleExtras(b, S, P, ctx, accent, dark) {
    const L = S.length;
    const W = S.width;
    b.flag = 0;
    // воздухозаборник на капоте
    const zh = (ctx.tCabFront - 0.5) * L + L * 0.16;
    b.box(0, ctx.belt + 0.07, zh, W * 0.42, 0.14, L * 0.22, dark);
    b.box(0, ctx.belt + 0.14, zh + L * 0.06, W * 0.3, 0.06, L * 0.08, toColor('#0d0e10'));
    // массивная решётка и «клыки»
    b.box(0, ctx.RH + (ctx.yNose - ctx.RH) * 0.62, L * 0.5 - 0.01, ctx.hwNose * 1.5, 0.22, 0.07, toColor('#0d0e10'));
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
                S.wheel_radius * 1.16,
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
    if (S.spoiler) {
        // антикрыло на каркасе
        const yw = cageTop + S.spoiler_height * 0.5;
        for (let s = -1; s <= 1; s += 2) {
            b.box(s * hw * 0.8, (yw + cageTop) * 0.5, zBack - 0.3, 0.06, yw - cageTop, 0.08, tube);
        }
        b.flag = PAINT_FLAG;
        b.box(0, yw, zBack - 0.3, hw * 1.9, 0.06, 0.3, new THREE.Color(PAINT_TOP, PAINT_TOP, PAINT_TOP));
        b.flag = 0;
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
    b.box(0, ctx.RH + (ctx.yNose - ctx.RH) * 0.62, L * 0.5 - 0.01, ctx.hwNose * 1.8, 0.24, 0.05, dark);
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
        b.box(s * W * 0.46, ctx.belt - 0.13, (ctx.tCabRear - 0.5) * L + 0.1, 0.05, 0.14, L * 0.14, toColor('#0d0e10'));
    }
    // сплиттер
    b.box(0, S.ride_height + 0.02, L * 0.5 - 0.2, W * 0.62, 0.05, 0.3, dark);
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
        b.box(s * ctx.hwNose * 0.66, ctx.yNose + 0.1, L * 0.5 - 0.36, 0.32, 0.07, 0.42, dark);
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

// --- запечённое затенение кузова -------------------------------------------

/**
 * Затенение машины, посчитанное один раз на чертёж.
 *
 * Три слагаемых, и все три видны на глаз:
 *  - низ кузова и пороги темнее: снизу вершину закрывает асфальт;
 *  - днище (грани нормалью вниз) почти чёрное;
 *  - колёсные арки: вершины рядом с колесом затемняются по близости, так что
 *    крыло получает честную тень от колеса, а не ровную заливку.
 *
 * Затенение кладётся в БАЗОВЫЙ цвет чертежа. Перекрашиваемые панели хранят
 * там коэффициент яркости, поэтому цвет игрока (setBodyColor) домножается на
 * уже затенённую базу и тень никуда не девается при смене цвета.
 */
function bakeCarAO(geometry, S) {
    const halfTrack = S.track_width * 0.5;
    const halfBase = S.wheelbase * 0.5;
    const r = S.wheel_radius;

    // земля под машиной
    bakeContactAO(geometry, {
        base: 0,
        height: S.height * 0.85,
        floor: 0.58,
        power: 0.75,
        sky: 0.26,
        down: 0.45
    });

    // четыре колеса как окклюдеры: их радиус и даёт тень в арке
    const wheels = new Float32Array([
        halfTrack, r, halfBase, r * 2.1,
        -halfTrack, r, halfBase, r * 2.1,
        halfTrack, r, -halfBase, r * 2.1,
        -halfTrack, r, -halfBase, r * 2.1
    ]);
    bakeProximityAO(geometry, wheels, 0.34);
    return geometry;
}

// --- фары и стопы ----------------------------------------------------------

function buildHeadLights(b, S, ctx) {
    const L = S.length;
    const glass = toColor('#fff6d8');
    _lampPoints.length = 0;
    const z = L * 0.5 + 0.02;
    // середина передней маски: между бампером и верхней кромкой носа
    const y = ctx.RH + (ctx.yNose - ctx.RH) * 0.62;
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
            lightQuad(b, s * hw * 0.66, ctx.yNose + 0.145, L * 0.5 - 0.36, 0.26, 0.36, glass, 0);
        }
    } else if (S.style === 'van') {
        for (let s = -1; s <= 1; s += 2) {
            lightQuad(b, s * hw * 0.68, y, z, 0.36, 0.22, glass, 1);
        }
    } else {
        for (let s = -1; s <= 1; s += 2) {
            lightQuad(b, s * hw * 0.64, y, z, 0.34, 0.17, glass, 1);
        }
    }
    return new Float32Array(_lampPoints);
}

function buildBrakeLights(b, S, ctx) {
    const L = S.length;
    const lamp = toColor('#ff4d3d');
    _lampPoints.length = 0;
    const z = -L * 0.5 - 0.02;
    const y = ctx.RH + (ctx.yTail - ctx.RH) * 0.66;
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
    return new Float32Array(_lampPoints);
}

/**
 * Якоря накладного свечения: x, y, z, размер по четыре числа на лампу.
 * Список общий и переиспользуемый — он живёт только на время сборки чертежа,
 * в кадровый цикл ничего отсюда не попадает.
 */
const _lampPoints = [];

/** Плоский светящийся прямоугольник. dir: +1 вперёд, -1 назад, 0 вверх. */
function lightQuad(b, x, y, z, w, h, color, dir) {
    const hwq = w * 0.5,
        hhq = h * 0.5;
    // точка свечения чуть впереди стекла, чтобы ореол не тонул в кузове
    _lampPoints.push(
        x,
        y + (dir === 0 ? 0.02 : 0),
        z + (dir === 0 ? 0 : dir * 0.04),
        Math.max(w, h) * 1.9 + 0.12
    );
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
    const rim = toColor('#cdd2d8');
    const hub = toColor('#4e535a');

    // протектор: открытый цилиндр вдоль оси X
    const tread = solidify(new THREE.CylinderGeometry(r, r, w, seg, 1, true), tire, function (x, y, z, i, c) {
        // лёгкая грань между блоками протектора
        if ((i >> 1) % 3 === 0) c.setRGB(c.r * 1.3, c.g * 1.3, c.b * 1.3);
    });
    tread.applyMatrix4(new THREE.Matrix4().makeRotationZ(Math.PI * 0.5));

    const parts = [tread];
    const mats = [null];
    for (let side = -1; side <= 1; side += 2) {
        // боковина покрышки — тёмное кольцо, диск — светлый круг с тёмной ступицей
        const wall = solidify(new THREE.RingGeometry(r * 0.62, r * 0.998, seg, 1), tire);
        const disc = solidify(new THREE.CircleGeometry(r * 0.63, seg), rim, function (x, y, z, i, c) {
            const d = Math.sqrt(x * x + y * y) / (r * 0.63);
            if (d < 0.42) c.copy(hub);
        });
        const mWall = new THREE.Matrix4().makeRotationY((side * Math.PI) / 2);
        mWall.setPosition(side * (w * 0.5 - 0.004), 0, 0);
        const mDisc = new THREE.Matrix4().makeRotationY((side * Math.PI) / 2);
        mDisc.setPosition(side * (w * 0.5 - 0.03), 0, 0);
        parts.push(wall, disc);
        mats.push(mWall, mDisc);
    }
    return mergeGeometries(parts, mats);
}

export default buildCarMesh;
