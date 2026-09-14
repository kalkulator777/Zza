/**
 * effects.js — [render] дым, искры, буст, снаряды, взрывы, щиты, боксы.
 *
 * Всё в этом файле живёт на заранее выделенных пулах инстансов. В кадровом
 * цикле не создаётся ни одного объекта: ни THREE.Vector3, ни литерала массива,
 * ни замыкания. Частицы лежат в типизированных массивах (SoA), матрицы
 * инстансов пишутся числами прямо в instanceMatrix.array.
 *
 * --------------------------------------------------------------------------
 * БЮДЖЕТ DRAW CALL (раздел 1: всего 60 на кадр, на рендер остаётся ~9-13)
 * --------------------------------------------------------------------------
 * Шесть инстансированных мешей, и ни одним больше:
 *
 *   smokeMesh   1   дым дрифта, пыль вне трассы, дым взрыва и раскрутки
 *   glowMesh    1   искры заряда, след буста, вспышка взрыва, полосы скорости
 *   lampMesh    1   накладное свечение: фары, стопы, турбо, маяки мин, щиты
 *   rocketMesh  1   летящие ракеты (снаряды kind = 2)
 *   mineMesh    1   лежащие мины (снаряды kind = 3)
 *   shieldMesh  1   купол щита вокруг машины
 *
 * Меш с нулём активных инстансов прячется (visible = false) и в
 * renderer.info.render.calls не попадает вовсе. Боксы с бонусами — меш
 * из renderer.js (12.6), здесь только вращение, покачивание и подсветка:
 * ноль дополнительных вызовов.
 *
 * --------------------------------------------------------------------------
 * ПОЧЕМУ ДВА ПУЛА ЧАСТИЦ, А НЕ ОДИН
 * --------------------------------------------------------------------------
 * У искр и следа буста аддитивное смешивание (светятся), у дыма — обычное
 * (перекрывает). Это разные материалы, а значит разные draw call в любом
 * случае. Прозрачность у инстансов задать нельзя: three.js даёт на инстанс
 * только RGB (instanceColor), альфы там нет. Поэтому:
 *   - аддитивный пул гасит частицу умножением цвета на альфу (чёрное в
 *     аддитивном смешивании невидимо) — это математически точная прозрачность;
 *   - пул дыма гасит частицу сжатием размера к нулю и уводом цвета в цвет
 *     тумана, то есть частица растворяется в среде, а не пропадает рывком.
 *
 * ВАЖНО про instanceColor: в шейдере three.js r180 (chunk color_fragment)
 * vColor попадает в diffuseColor только при определённом USE_COLOR, то есть
 * при material.vertexColors === true. Поэтому у геометрии частицы ОБЯЗАН быть
 * атрибут color, залитый единицами, иначе инстансный цвет не применится
 * (а без атрибута color вершины станут чёрными). Проверено на вендоренном
 * бандле.
 */

import * as THREE from 'three';
import { MeshBuilder, solidify, mergeGeometries, toColor, disposeObject } from './geomutil.js';
import {
    FLAG_OFFTRACK,
    FLAG_DRIFTING,
    FLAG_BOOST,
    FLAG_SPIN,
    FLAG_SHIELD,
    FLAG_GHOST,
    FLAG_BRAKING,
    MAX_CARS,
    MAX_PROJECTILES,
    driftChargeSeconds
} from '../protocol.js';

// ---------------------------------------------------------------------------
// Пресеты качества: потолок частиц и интенсивность эмиссии
// ---------------------------------------------------------------------------

export const EFFECT_QUALITY = {
    // «частицы выключены» — отдельный уровень настройки, а не пресет качества:
    // пулы схлопываются в один элемент, все частоты нулевые, ни одна частица
    // не рождается. Дым, искры и полосы скорости пропадают, свечение и
    // снаряды остаются — это разные галочки.
    off: {
        smoke: 1, glow: 1,
        smokeRate: 0, sparkRate: 0, boostRate: 0, dustRate: 0,
        speedLines: 0, shieldDetail: 0, explosionScale: 0
    },
    low: {
        smoke: 96, glow: 176,
        smokeRate: 22, sparkRate: 24, boostRate: 30, dustRate: 12,
        speedLines: 0, shieldDetail: 0, explosionScale: 0.6
    },
    medium: {
        smoke: 168, glow: 312,
        smokeRate: 34, sparkRate: 40, boostRate: 48, dustRate: 20,
        speedLines: 28, shieldDetail: 1, explosionScale: 1.0
    },
    high: {
        smoke: 264, glow: 496,
        smokeRate: 48, sparkRate: 56, boostRate: 66, dustRate: 30,
        speedLines: 48, shieldDetail: 1, explosionScale: 1.35
    }
};

/**
 * Уровень частиц — ОТДЕЛЬНАЯ настройка графики (галочка под пресетом).
 * Значение переводится в пресет пулов выше.
 */
export const PARTICLE_LEVELS = { off: 'off', few: 'low', normal: 'medium', many: 'high' };

// ---------------------------------------------------------------------------
// Накладное свечение геометрией (задача «свечение фар, стопов и турбо»)
// ---------------------------------------------------------------------------
//
// Полноэкранный bloom на UHD 24 EU не по карману: он требует второго
// фреймбуфера в разрешении экрана плюс несколько проходов размытия, то есть
// десятки мегабайт трафика видеопамяти на кадр. Вместо него — накладные
// плоскости с мягким градиентом, развёрнутые к камере, с аддитивным
// смешиванием. Все они лежат в ОДНОМ InstancedMesh: один draw call на всё
// свечение сцены, а при выключенной галочке меш просто прячется.

const LAMP_CAP = MAX_CARS * 8 + MAX_PROJECTILES * 2 + 16;
const FLASH_CAP = 16;

const LAMP_HEAD_R = 1.0, LAMP_HEAD_G = 0.93, LAMP_HEAD_B = 0.74;
const LAMP_BRAKE_R = 1.0, LAMP_BRAKE_G = 0.13, LAMP_BRAKE_B = 0.06;
const LAMP_BOOST_R = 1.0, LAMP_BOOST_G = 0.62, LAMP_BOOST_B = 0.18;

// цвета искр по уровням заряда дрифта (6.3): 1 синий, 2 оранжевый, 3 фиолетовый
const SPARK_L1 = [0.32, 0.66, 1.0];
const SPARK_L2 = [1.0, 0.56, 0.13];
const SPARK_L3 = [0.76, 0.36, 1.0];

const DRIFT_L1 = 0.7; // с, пороги из 6.3
const DRIFT_L2 = 1.4;
const DRIFT_L3 = 2.4;

const SPEED_LINE_MIN = 26.0; // м/с, с которой появляются полосы скорости

// ---------------------------------------------------------------------------
// Модульные временные величины: всё, что нужно кадру, выделено один раз
// ---------------------------------------------------------------------------

// параметры рождения частицы; заполняются перед вызовом emit()
const SP = {
    x: 0, y: 0, z: 0,
    vx: 0, vy: 0, vz: 0,
    life: 1,
    s0: 1, s1: 1,
    r0: 1, g0: 1, b0: 1,
    r1: 0, g1: 0, b1: 0,
    damp: 0, grav: 0,
    rot: 0, rotV: 0,
    mode: 0, stretch: 1
};

const _m4 = new THREE.Matrix4();
const _col = new THREE.Color();

function clamp(v, a, b) {
    return v < a ? a : v > b ? b : v;
}

// ---------------------------------------------------------------------------
// Текстуры частиц: генерируются в canvas, файлов в проекте нет (10.3)
// ---------------------------------------------------------------------------

/**
 * Мягкое круглое пятно с радиальным градиентом альфы.
 * hardness — доля радиуса, до которой держится почти полная непрозрачность.
 * Используется и частицами, и тенями машин в renderer.js.
 */
export function createRadialTexture(size, hardness, softness) {
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const g = canvas.getContext('2d');
    const h = size * 0.5;
    const grad = g.createRadialGradient(h, h, 0, h, h, h);
    grad.addColorStop(0, 'rgba(255,255,255,1)');
    grad.addColorStop(clamp(hardness, 0.01, 0.95), 'rgba(255,255,255,' + softness + ')');
    grad.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = grad;
    g.fillRect(0, 0, size, size);
    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    // Мипмапы частицам не нужны: они всегда крупные на экране, зато при
    // выборе старшего уровня квад превращается в ровный светлый прямоугольник
    // с жёсткими краями — этот артефакт ловился на взрывах.
    tex.generateMipmaps = false;
    tex.minFilter = THREE.LinearFilter;
    tex.magFilter = THREE.LinearFilter;
    tex.wrapS = THREE.ClampToEdgeWrapping;
    tex.wrapT = THREE.ClampToEdgeWrapping;
    tex.needsUpdate = true;
    return tex;
}

/** Квад 1x1 с атрибутом color из единиц — без него instanceColor не сработает. */
function makeParticleQuad() {
    const g = new THREE.PlaneGeometry(1, 1);
    const n = g.attributes.position.count;
    const col = new Float32Array(n * 3);
    col.fill(1);
    g.setAttribute('color', new THREE.BufferAttribute(col, 3));
    return g;
}

// ---------------------------------------------------------------------------
// Пул частиц
// ---------------------------------------------------------------------------

/**
 * Плотно упакованный пул: живые частицы всегда лежат в [0, n), поэтому
 * mesh.count = n и GPU не обрабатывает мёртвые инстансы. Удаление —
 * перестановкой с последней живой (swap-remove), порядок частиц не важен.
 */
class ParticlePool {
    constructor(capacity) {
        const c = capacity;
        this.cap = c;
        this.n = 0;
        this.px = new Float32Array(c);
        this.py = new Float32Array(c);
        this.pz = new Float32Array(c);
        this.vx = new Float32Array(c);
        this.vy = new Float32Array(c);
        this.vz = new Float32Array(c);
        this.life = new Float32Array(c);
        this.invLife = new Float32Array(c);
        this.s0 = new Float32Array(c);
        this.s1 = new Float32Array(c);
        this.r0 = new Float32Array(c);
        this.g0 = new Float32Array(c);
        this.b0 = new Float32Array(c);
        this.r1 = new Float32Array(c);
        this.g1 = new Float32Array(c);
        this.b1 = new Float32Array(c);
        this.damp = new Float32Array(c);
        this.grav = new Float32Array(c);
        this.rot = new Float32Array(c);
        this.rotV = new Float32Array(c);
        this.stretch = new Float32Array(c);
        this.mode = new Uint8Array(c);
    }

    /** Родить частицу по данным SP. Возвращает false, если пул переполнен. */
    spawn() {
        if (this.n >= this.cap) return false;
        const i = this.n++;
        this.px[i] = SP.x;
        this.py[i] = SP.y;
        this.pz[i] = SP.z;
        this.vx[i] = SP.vx;
        this.vy[i] = SP.vy;
        this.vz[i] = SP.vz;
        this.life[i] = SP.life;
        this.invLife[i] = 1 / SP.life;
        this.s0[i] = SP.s0;
        this.s1[i] = SP.s1;
        this.r0[i] = SP.r0;
        this.g0[i] = SP.g0;
        this.b0[i] = SP.b0;
        this.r1[i] = SP.r1;
        this.g1[i] = SP.g1;
        this.b1[i] = SP.b1;
        this.damp[i] = SP.damp;
        this.grav[i] = SP.grav;
        this.rot[i] = SP.rot;
        this.rotV[i] = SP.rotV;
        this.stretch[i] = SP.stretch;
        this.mode[i] = SP.mode;
        return true;
    }

    /** Перенести частицу from на место to (используется при swap-remove). */
    move(from, to) {
        this.px[to] = this.px[from];
        this.py[to] = this.py[from];
        this.pz[to] = this.pz[from];
        this.vx[to] = this.vx[from];
        this.vy[to] = this.vy[from];
        this.vz[to] = this.vz[from];
        this.life[to] = this.life[from];
        this.invLife[to] = this.invLife[from];
        this.s0[to] = this.s0[from];
        this.s1[to] = this.s1[from];
        this.r0[to] = this.r0[from];
        this.g0[to] = this.g0[from];
        this.b0[to] = this.b0[from];
        this.r1[to] = this.r1[from];
        this.g1[to] = this.g1[from];
        this.b1[to] = this.b1[from];
        this.damp[to] = this.damp[from];
        this.grav[to] = this.grav[from];
        this.rot[to] = this.rot[from];
        this.rotV[to] = this.rotV[from];
        this.stretch[to] = this.stretch[from];
        this.mode[to] = this.mode[from];
    }

    clear() {
        this.n = 0;
    }
}

// ---------------------------------------------------------------------------
// Геометрия снарядов
// ---------------------------------------------------------------------------

/** Ракета: носовой конус, корпус, три пера. Нос смотрит в +Z (раздел 4). */
function buildRocketGeometry() {
    const bodyCol = toColor('#d8dde4');
    const noseCol = toColor('#e24a3a');
    const finCol = toColor('#2c3038');
    const geoms = [];
    const mats = [];

    const nose = solidify(new THREE.ConeGeometry(0.15, 0.42, 8), noseCol);
    _m4.makeRotationX(Math.PI * 0.5);
    _m4.setPosition(0, 0, 0.35);
    geoms.push(nose);
    mats.push(_m4.clone());

    const body = solidify(new THREE.CylinderGeometry(0.15, 0.13, 0.62, 8), bodyCol);
    _m4.makeRotationX(Math.PI * 0.5);
    _m4.setPosition(0, 0, -0.17);
    geoms.push(body);
    mats.push(_m4.clone());

    for (let k = 0; k < 3; k++) {
        const a = (k / 3) * Math.PI * 2;
        const fin = solidify(new THREE.BoxGeometry(0.03, 0.26, 0.24), finCol);
        const rot = new THREE.Matrix4().makeRotationZ(a);
        const off = new THREE.Matrix4().makeTranslation(0, 0.2, 0);
        const place = new THREE.Matrix4().makeTranslation(0, 0, -0.36);
        rot.multiply(off);
        place.multiply(rot);
        geoms.push(fin);
        mats.push(place);
    }

    const merged = mergeGeometries(geoms, mats);
    for (let i = 0; i < geoms.length; i++) geoms[i].dispose();
    return merged;
}

/**
 * Мина. Заказчик: «сейчас мина теряется на асфальте, игрок узнаёт о ней после
 * попадания». Поэтому силуэт переделан целиком:
 *
 *  - предупреждающий круг на земле радиусом 1,35 м с чёрно-жёлтыми секторами.
 *    Радиус срабатывания 2,4 м (12.10), то есть круг ЗАМЕТНО МЕНЬШЕ опасной
 *    зоны: мина осталась опасной для невнимательного, а не только для слепого;
 *  - корпус вдвое выше прежнего, с чёрно-жёлтой «дорожной» юбкой;
 *  - вертикальная стойка с маяком наверху: она торчит над асфальтом и видна
 *    издалека даже под острым углом камеры;
 *  - три косых шипа, ломающие круглый силуэт.
 *
 * Круг лежит на 2 см над полотном, а материал мины получает polygonOffset —
 * без этого на дистанции появился бы z-fighting с асфальтом.
 */
const MINE_MARK_RADIUS = 1.35;
const MINE_ARM = 0.5;   // с, взведение мины (12.10)

function buildMineGeometry() {
    const shell = toColor('#26292f');
    const warn = toColor('#ffc21a');
    const trim = toColor('#e0632a');
    const beacon = toColor('#ff3a24');
    const b = new MeshBuilder();

    // --- предупреждающий круг на земле: чередование секторов ---------------
    const sectors = 12;
    const rIn = 0.62;
    const rOut = MINE_MARK_RADIUS;
    const y = 0.02;
    for (let k = 0; k < sectors; k++) {
        const a0 = (k / sectors) * Math.PI * 2;
        const a1 = ((k + 1) / sectors) * Math.PI * 2;
        const c = k & 1 ? warn : shell;
        b.quad(
            [Math.sin(a0) * rIn, y, Math.cos(a0) * rIn],
            [Math.sin(a0) * rOut, y, Math.cos(a0) * rOut],
            [Math.sin(a1) * rOut, y, Math.cos(a1) * rOut],
            [Math.sin(a1) * rIn, y, Math.cos(a1) * rIn],
            c
        );
    }

    const geoms = [b.build()];
    const mats = [null];

    // --- корпус: широкая юбка, чёрно-жёлтый пояс, тёмная крышка ------------
    geoms.push(solidify(new THREE.CylinderGeometry(0.46, 0.6, 0.1, 8), shell));
    mats.push(new THREE.Matrix4().makeTranslation(0, 0.06, 0));
    geoms.push(solidify(new THREE.CylinderGeometry(0.4, 0.46, 0.2, 8), warn, function (x, yy, z, i, c) {
        // грани через одну тёмные — получается «дорожная» разметка по кругу
        if (((i / 6) | 0) % 2 === 0) c.copy(shell);
    }));
    mats.push(new THREE.Matrix4().makeTranslation(0, 0.21, 0));
    geoms.push(solidify(new THREE.CylinderGeometry(0.26, 0.38, 0.16, 8), shell));
    mats.push(new THREE.Matrix4().makeTranslation(0, 0.39, 0));

    // --- стойка с маяком: то, что видно издалека ---------------------------
    geoms.push(solidify(new THREE.CylinderGeometry(0.05, 0.07, 0.52, 5), shell));
    mats.push(new THREE.Matrix4().makeTranslation(0, 0.72, 0));
    geoms.push(solidify(new THREE.OctahedronGeometry(0.19, 0), beacon));
    mats.push(new THREE.Matrix4().makeTranslation(0, 1.04, 0));

    // --- шипы --------------------------------------------------------------
    for (let k = 0; k < 3; k++) {
        const a = (k / 3) * Math.PI * 2 + 0.5;
        const spike = solidify(new THREE.ConeGeometry(0.08, 0.3, 4), trim);
        const m = new THREE.Matrix4().makeRotationY(a);
        const tilt = new THREE.Matrix4().makeRotationZ(-0.95);
        const off = new THREE.Matrix4().makeTranslation(0, 0.3, 0);
        tilt.multiply(off);
        m.multiply(tilt);
        const shift = new THREE.Matrix4().makeTranslation(0, 0.26, 0);
        shift.multiply(m);
        geoms.push(spike);
        mats.push(shift);
    }

    const merged = mergeGeometries(geoms, mats);
    for (let i = 0; i < geoms.length; i++) geoms[i].dispose();
    return merged;
}

// ---------------------------------------------------------------------------
// Effects
// ---------------------------------------------------------------------------

/**
 * Все эффекты гонки. Создаётся один раз на гонку из renderer.js.
 *
 * Порядок вызовов в кадре:
 *   effects.beginFrame();
 *   для каждой видимой машины: effects.emitFromCar(view, dt);
 *   effects.setProjectiles(snapshot);
 *   effects.setBoxMask(snapshot);
 *   effects.update(dt, camera, localSpeed);
 *
 * view — объект CarView из renderer.js, поля читаются только на чтение:
 *   x, y, z, yaw, speed, vfwd, vlat, steer, flags, driftCharge, slot,
 *   halfWidth, halfLength, wheelRadius, rearZ, rearY.
 */
export class Effects {
    /**
     * @param {object} opts { quality, particles, glow, fogColor, heightAt, night }
     *   heightAt(x, z) — высота поверхности, нужна дыму, минам и взрывам;
     *   night — 0 день, 0.5 сумерки, 1 ночь (до 1.5 на ярких огнях).
     */
    constructor(opts) {
        const o = opts || {};
        // Уровень частиц — своя настройка: пресет качества задаёт умолчание,
        // но галочка в меню может увести его в любую сторону, вплоть до нуля.
        const level = PARTICLE_LEVELS[o.particles];
        const qName = EFFECT_QUALITY[level] ? level : EFFECT_QUALITY[o.quality] ? o.quality : 'medium';
        this.quality = qName;
        const Q = EFFECT_QUALITY[qName];
        this.Q = Q;
        this.particlesOff = qName === 'off';
        // Свечение геометрией — отдельная галочка, независимая от частиц.
        this.glowOn = o.glow !== false;
        // Ночь: 0 — день, 0.5 — сумерки, 1 — ночь, до 1.5 на ярких огнях.
        // Фары ночью становятся главным источником картинки, стопы и турбо
        // читаются издалека. Вспышки взрывов, наоборот, слегка придерживаем:
        // аддитив по тёмному фону иначе выбивает белое пятно.
        this.setNight(o.night);

        this.time = 0;
        this.heightAt = o.heightAt || null;

        this.fogR = 0.72;
        this.fogG = 0.78;
        this.fogB = 0.85;
        if (o.fogColor) this.setFogColor(o.fogColor);

        this.group = new THREE.Group();
        this.group.name = 'effects';

        // --- текстуры -------------------------------------------------------
        this.glowTex = createRadialTexture(64, 0.18, 0.72);
        this.smokeTex = createRadialTexture(64, 0.42, 0.62);

        // --- пул аддитивных частиц -----------------------------------------
        this.glow = new ParticlePool(Q.glow);
        this.glowMat = new THREE.MeshBasicMaterial({
            map: this.glowTex,
            vertexColors: true,
            transparent: true,
            blending: THREE.AdditiveBlending,
            depthWrite: false,
            fog: false,
            // Билборд всегда развёрнут нормалью к камере, поэтому FrontSide
            // достаточно. DoubleSide тут стоил бы вдвое: three.js рисует
            // прозрачный двусторонний материал двумя проходами.
            side: THREE.FrontSide,
            forceSinglePass: true
        });
        this.glowMat.name = 'fxGlow';
        this.glowMesh = makeInstanced(makeParticleQuad(), this.glowMat, Q.glow, 'fxGlowMesh');
        this.glowMesh.renderOrder = 6;
        this.group.add(this.glowMesh);

        // --- пул дыма -------------------------------------------------------
        this.smoke = new ParticlePool(Q.smoke);
        this.smokeMat = new THREE.MeshBasicMaterial({
            map: this.smokeTex,
            vertexColors: true,
            transparent: true,
            opacity: 0.34,
            depthWrite: false,
            side: THREE.FrontSide,
            forceSinglePass: true
        });
        this.smokeMat.name = 'fxSmoke';
        this.smokeMesh = makeInstanced(makeParticleQuad(), this.smokeMat, Q.smoke, 'fxSmokeMesh');
        this.smokeMesh.renderOrder = 5;
        this.group.add(this.smokeMesh);

        // --- ракеты ---------------------------------------------------------
        this.rocketGeom = buildRocketGeometry();
        this.rocketMat = new THREE.MeshLambertMaterial({
            vertexColors: true,
            flatShading: true,
            emissive: new THREE.Color('#401008'),
            emissiveIntensity: 1.0
        });
        this.rocketMat.name = 'fxRocket';
        this.rocketMesh = new THREE.InstancedMesh(this.rocketGeom, this.rocketMat, MAX_PROJECTILES);
        this.rocketMesh.name = 'fxRockets';
        this.rocketMesh.frustumCulled = false;
        this.rocketMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        this.rocketMesh.count = 0;
        this.rocketMesh.visible = false;
        this.group.add(this.rocketMesh);

        // --- мины -----------------------------------------------------------
        this.mineGeom = buildMineGeometry();
        this.mineMat = new THREE.MeshLambertMaterial({
            vertexColors: true,
            flatShading: true,
            // эмиссив слабый и общий на все мины: разницу «взведена или нет»
            // несёт инстансный цвет, а он на эмиссив не влияет
            emissive: new THREE.Color('#2a0a00'),
            emissiveIntensity: 1.0,
            // предупреждающий круг лежит в двух сантиметрах над полотном:
            // без смещения полигонов он замерцает на дистанции
            polygonOffset: true,
            polygonOffsetFactor: -3,
            polygonOffsetUnits: -6
        });
        this.mineMat.name = 'fxMine';
        this.mineMesh = new THREE.InstancedMesh(this.mineGeom, this.mineMat, MAX_PROJECTILES);
        this.mineMesh.name = 'fxMines';
        this.mineMesh.frustumCulled = false;
        this.mineMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        // 12.11: instanceColor доходит до шейдера только при vertexColors и
        // атрибуте color у геометрии — оба условия здесь выполнены.
        this.mineMesh.instanceColor = new THREE.InstancedBufferAttribute(
            new Float32Array(MAX_PROJECTILES * 3).fill(1), 3
        );
        this.mineMesh.instanceColor.setUsage(THREE.DynamicDrawUsage);
        this.mineMesh.count = 0;
        this.mineMesh.visible = false;
        this.group.add(this.mineMesh);

        // --- накладное свечение: фары, стопы, турбо, искры, маяки мин -------
        this.lampMat = new THREE.MeshBasicMaterial({
            map: this.glowTex,
            vertexColors: true,
            transparent: true,
            blending: THREE.AdditiveBlending,
            depthWrite: false,
            fog: true,
            side: THREE.FrontSide,
            forceSinglePass: true
        });
        this.lampMat.name = 'fxLamp';
        this.lampMesh = makeInstanced(makeParticleQuad(), this.lampMat, LAMP_CAP, 'fxLampMesh');
        this.lampMesh.renderOrder = 7;
        this.group.add(this.lampMesh);

        // Накопитель ореолов кадра: заполняется в emitFromCar и в разборе
        // снарядов, а разворачивается к камере уже в update(), когда известен
        // базис камеры. Массивы выделены один раз — в кадре ноль аллокаций.
        this.lampN = 0;
        this.lampX = new Float32Array(LAMP_CAP);
        this.lampY = new Float32Array(LAMP_CAP);
        this.lampZ = new Float32Array(LAMP_CAP);
        this.lampS = new Float32Array(LAMP_CAP);
        this.lampR = new Float32Array(LAMP_CAP);
        this.lampG = new Float32Array(LAMP_CAP);
        this.lampB = new Float32Array(LAMP_CAP);

        // --- щиты -----------------------------------------------------------
        // Купол ярче по «экватору» и глуше к полюсу: аддитивная сфера с
        // ровным цветом выглядит сплошным пятном, а так читается пузырь.
        this.shieldGeom = solidify(
            new THREE.IcosahedronGeometry(1, Q.shieldDetail),
            toColor('#6fd0ff'),
            function (x, y, z, i, c) {
                const k = 1 - Math.abs(y);
                c.setRGB(0.28 + k * 0.5, 0.62 + k * 0.38, 0.9 + k * 0.1);
            }
        );
        this.shieldMat = new THREE.MeshBasicMaterial({
            vertexColors: true,
            transparent: true,
            opacity: 0.16,
            blending: THREE.AdditiveBlending,
            depthWrite: false,
            fog: false,
            // ближняя полусфера купола: аддитивное свечение и так читается,
            // а второй проход по дальней стоил бы ещё один draw call
            side: THREE.FrontSide,
            forceSinglePass: true
        });
        this.shieldMat.name = 'fxShield';
        this.shieldMesh = new THREE.InstancedMesh(this.shieldGeom, this.shieldMat, MAX_CARS);
        this.shieldMesh.name = 'fxShields';
        this.shieldMesh.frustumCulled = false;
        this.shieldMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        this.shieldMesh.instanceColor = new THREE.InstancedBufferAttribute(
            new Float32Array(MAX_CARS * 3).fill(1), 3
        );
        this.shieldMesh.instanceColor.setUsage(THREE.DynamicDrawUsage);
        this.shieldMesh.count = 0;
        this.shieldMesh.visible = false;
        this.group.add(this.shieldMesh);

        // Слот, машина которого в этом кадре не рисуется (вид из кокпита —
        // своя машина спрятана, 12.11). Её ореолы обязаны исчезнуть вместе
        // с ней, иначе фары светят прямо в объектив.
        this.hiddenSlot = -1;

        // --- состояние по слотам (аккумуляторы частоты эмиссии) -------------
        this.accSmoke = new Float32Array(MAX_CARS);
        this.accSpark = new Float32Array(MAX_CARS);
        this.accBoost = new Float32Array(MAX_CARS);
        this.accDust = new Float32Array(MAX_CARS);
        this.shieldFlash = new Float32Array(MAX_CARS);
        this.shieldCount = 0;

        // --- снаряды --------------------------------------------------------
        // Возраст снаряда нужен мине: первые MINE_ARM секунд она НЕ взведена
        // и обязана выглядеть иначе (12.10). Идентификатор снаряда в снапшоте
        // есть (5.3), поэтому возраст считается по нему: таблица «видели id —
        // во столько-то» переживает кадр и не требует ни одной аллокации.
        this.projBorn = new Float32Array(MAX_PROJECTILES);
        this.projSeenId = new Uint16Array(MAX_PROJECTILES);
        this.projSeenBorn = new Float32Array(MAX_PROJECTILES);
        this.projSeenN = 0;
        this.projCount = 0;
        this.projKind = new Uint8Array(MAX_PROJECTILES);
        this.projX = new Float32Array(MAX_PROJECTILES);
        this.projZ = new Float32Array(MAX_PROJECTILES);
        this.projY = new Float32Array(MAX_PROJECTILES);
        this.projYaw = new Float32Array(MAX_PROJECTILES);
        this.accRocket = 0;

        // --- боксы с бонусами (меш строит renderer.js, 12.6) ----------------
        this.boxMesh = null;
        this.boxMat = null;
        this.boxCount = 0;
        this.boxX = null;
        this.boxY = null;
        this.boxZ = null;
        this.boxGrow = null;
        this.boxMask = null;
        this.boxMaskLen = 0;

        // --- полосы скорости ------------------------------------------------
        this.accLines = 0;

        // Вспышки взрывов и щитов: короткоживущие ореолы. Живут отдельно от
        // частиц, поэтому взрыв остаётся заметным даже при выключенных
        // частицах. Пул плотно упакован, потолок фиксирован.
        this.flashN = 0;
        this.flashX = new Float32Array(FLASH_CAP);
        this.flashY = new Float32Array(FLASH_CAP);
        this.flashZ = new Float32Array(FLASH_CAP);
        this.flashLife = new Float32Array(FLASH_CAP);
        this.flashInv = new Float32Array(FLASH_CAP);
        this.flashSize = new Float32Array(FLASH_CAP);
        this.flashR = new Float32Array(FLASH_CAP);
        this.flashG = new Float32Array(FLASH_CAP);
        this.flashB = new Float32Array(FLASH_CAP);

        // счётчик активных частиц для оверлея F3
        this.activeParticles = 0;
    }

    /**
     * Ночной режим накладного свечения. Зовётся при сборке гонки и при смене
     * времени суток; в кадре только читаются готовые множители.
     */
    setNight(k) {
        const n = k > 0 ? (k > 1.5 ? 1.5 : k) : 0;
        this.nightK = n;
        this.headSize = 1.0 + n * 1.35;   // ореол фары заметно крупнее
        this.headGain = 1.0 + n * 0.55;
        this.tailSize = 1.0 + n * 0.45;
        this.tailGain = 1.0 + n * 0.5;
        this.boostGain = 1.0 + n * 0.35;
        this.flashGain = 1.0 - n * 0.18;  // чтобы взрыв не пересвечивал
    }

    /**
     * Слот, чья машина в этом кадре скрыта (вид из кокпита). Её накладное
     * свечение не выкладывается.
     */
    setHiddenSlot(slot) {
        this.hiddenSlot = slot === undefined || slot === null ? -1 : slot;
    }

    /**
     * Включить или выключить накладное свечение на живой сцене.
     * Пересборка не нужна: ореолы рождаются каждый кадр заново.
     */
    setGlowEnabled(on) {
        this.glowOn = on !== false;
        if (!this.glowOn) {
            this.lampN = 0;
            this.lampMesh.count = 0;
            this.lampMesh.visible = false;
        }
    }

    /** Цвет тумана: в него растворяется дым, чтобы не пропадать рывком. */
    setFogColor(color) {
        toColor(color, _col);
        this.fogR = _col.r;
        this.fogG = _col.g;
        this.fogB = _col.b;
    }

    /** Корневой узел — renderer.js добавляет его в сцену. */
    get root() {
        return this.group;
    }

    /**
     * Меш боксов с бонусами. Сам меш строит renderer.js (12.6), здесь только
     * анимация: вращение, покачивание, подсветка и появление после респауна.
     * @param {THREE.InstancedMesh} mesh
     * @param {Float32Array} xs, ys, zs  позиции боксов
     * @param {number} count
     */
    attachBoxes(mesh, xs, ys, zs, count) {
        this.boxMesh = mesh;
        this.boxMat = mesh ? mesh.material : null;
        this.boxX = xs;
        this.boxY = ys;
        this.boxZ = zs;
        this.boxCount = count;
        this.boxGrow = new Float32Array(count);
        this.boxGrow.fill(1);
    }

    // -----------------------------------------------------------------------
    // Кадр
    // -----------------------------------------------------------------------

    /** Сброс покадровых счётчиков. Зовётся до emitFromCar. */
    beginFrame() {
        this.shieldCount = 0;
        this.lampN = 0;
    }

    /**
     * Записать ореол в накопитель кадра. Разворот к камере произойдёт позже,
     * в update(), когда известен базис камеры. Ни одной аллокации.
     */
    lamp(x, y, z, size, r, g, b) {
        const i = this.lampN;
        if (i >= LAMP_CAP) return;
        this.lampN = i + 1;
        this.lampX[i] = x;
        this.lampY[i] = y;
        this.lampZ[i] = z;
        this.lampS[i] = size;
        this.lampR[i] = r;
        this.lampG[i] = g;
        this.lampB[i] = b;
    }

    /**
     * Ореолы ламп одной машины: фары горят всегда, стопы тлеют и вспыхивают
     * при торможении, выхлоп светится на турбо.
     * points — плоский массив x, y, z, размер в системе координат машины.
     */
    lampRow(view, points, k, r, g, b, sizeK, fx, fz, lx, lz) {
        if (!points) return;
        for (let i = 0; i < points.length; i += 4) {
            const px = points[i], py = points[i + 1], pz = points[i + 2];
            this.lamp(
                view.x + px * lx + pz * fx,
                view.y + py,
                view.z + px * lz + pz * fz,
                points[i + 3] * sizeK,
                r * k, g * k, b * k
            );
        }
    }

    /**
     * Эффекты, которые рождает одна машина: дым дрифта, искры заряда,
     * след буста, пыль вне трассы, дым раскрутки, купол щита.
     */
    emitFromCar(view, dt) {
        const slot = view.slot;
        const flags = view.flags;
        if (flags & FLAG_GHOST) return;

        const sinY = Math.sin(view.yaw);
        const cosY = Math.cos(view.yaw);
        // forward = (sin yaw, cos yaw), lateral = (cos yaw, -sin yaw) — раздел 4
        const fx = sinY, fz = cosY;
        const lx = cosY, lz = -sinY;
        const speed = view.speed;
        const halfTrack = view.halfWidth;
        const rearZ = view.rearZ;

        // --- накладное свечение ламп ----------------------------------------
        const lampsOn = this.glowOn && slot !== this.hiddenSlot;
        if (lampsOn) {
            // фары горят постоянно (10.3), кроме машины-призрака — её уже
            // отсекли по FLAG_GHOST выше
            this.lampRow(view, view.lampHead, this.headGain, LAMP_HEAD_R, LAMP_HEAD_G, LAMP_HEAD_B,
                this.headSize, fx, fz, lx, lz);

            // стоп-сигналы: тлеют всегда, при торможении вспыхивают втрое
            const braking = (flags & FLAG_BRAKING) !== 0;
            this.lampRow(view, view.lampBrake, (braking ? 1.0 : 0.34) * this.tailGain,
                LAMP_BRAKE_R, LAMP_BRAKE_G, LAMP_BRAKE_B,
                (braking ? 1.5 : 0.85) * this.tailSize, fx, fz, lx, lz);

            // выхлоп на турбо: пульсирующий горячий ореол
            if (flags & FLAG_BOOST) {
                const puls = 0.82 + Math.sin(this.time * 27 + slot) * 0.18;
                this.lampRow(view, view.lampExhaust, puls * this.boostGain,
                    LAMP_BOOST_R, LAMP_BOOST_G, LAMP_BOOST_B,
                    1.9 * puls, fx, fz, lx, lz);
            }
        }

        // --- дым от дрифта --------------------------------------------------
        const drifting = (flags & FLAG_DRIFTING) !== 0;
        if (drifting && speed > 4.0) {
            const rate = this.Q.smokeRate * clamp(speed / 18, 0.35, 1.6);
            this.accSmoke[slot] += rate * dt;
            while (this.accSmoke[slot] >= 1) {
                this.accSmoke[slot] -= 1;
                const side = Math.random() < 0.5 ? 1 : -1;
                const ox = fx * rearZ + lx * halfTrack * side;
                const oz = fz * rearZ + lz * halfTrack * side;
                SP.x = view.x + ox + (Math.random() - 0.5) * 0.25;
                SP.y = view.y + 0.12;
                SP.z = view.z + oz + (Math.random() - 0.5) * 0.25;
                SP.vx = -fx * speed * 0.12 + (Math.random() - 0.5) * 1.1 - view.vlat * 0.15 * lx;
                SP.vy = 0.7 + Math.random() * 0.9;
                SP.vz = -fz * speed * 0.12 + (Math.random() - 0.5) * 1.1 - view.vlat * 0.15 * lz;
                SP.life = 0.5 + Math.random() * 0.35;
                SP.s0 = 0.32;
                SP.s1 = 1.25 + Math.random() * 0.6;
                SP.r0 = 0.93; SP.g0 = 0.93; SP.b0 = 0.95;
                SP.r1 = this.fogR; SP.g1 = this.fogG; SP.b1 = this.fogB;
                SP.damp = 1.6;
                SP.grav = 0.25;
                SP.rot = Math.random() * 6.283;
                SP.rotV = (Math.random() - 0.5) * 1.6;
                SP.mode = 0;
                SP.stretch = 1;
                this.smoke.spawn();
            }

            // --- искры заряда дрифта, цвет по уровню (6.3) ------------------
            const charge = driftChargeSeconds(view.driftCharge);
            let level = 0;
            if (charge >= DRIFT_L3) level = 3;
            else if (charge >= DRIFT_L2) level = 2;
            else if (charge >= DRIFT_L1) level = 1;
            if (level > 0) {
                const col = level === 3 ? SPARK_L3 : level === 2 ? SPARK_L2 : SPARK_L1;
                if (lampsOn) {
                    // ореол у задних колёс: уровень заряда виден и по цвету,
                    // и по размеру, даже если частицы выключены
                    const puls = 0.7 + Math.sin(this.time * (14 + level * 6) + slot) * 0.3;
                    const size = (0.34 + level * 0.16) * (0.75 + puls * 0.35);
                    for (let sgn = -1; sgn <= 1; sgn += 2) {
                        const ox = fx * rearZ + lx * halfTrack * sgn;
                        const oz = fz * rearZ + lz * halfTrack * sgn;
                        this.lamp(view.x + ox, view.y + 0.16, view.z + oz, size,
                            col[0] * puls, col[1] * puls, col[2] * puls);
                    }
                }
                const rateS = this.Q.sparkRate * (0.6 + level * 0.25);
                this.accSpark[slot] += rateS * dt;
                while (this.accSpark[slot] >= 1) {
                    this.accSpark[slot] -= 1;
                    const side = Math.random() < 0.5 ? 1 : -1;
                    const ox = fx * rearZ + lx * halfTrack * side;
                    const oz = fz * rearZ + lz * halfTrack * side;
                    SP.x = view.x + ox;
                    SP.y = view.y + 0.1;
                    SP.z = view.z + oz;
                    const sp = 2.5 + Math.random() * 4.5;
                    SP.vx = -fx * sp + lx * side * (0.6 + Math.random() * 2.2) + (Math.random() - 0.5);
                    SP.vy = 1.4 + Math.random() * 3.0;
                    SP.vz = -fz * sp + lz * side * (0.6 + Math.random() * 2.2) + (Math.random() - 0.5);
                    SP.life = 0.2 + Math.random() * 0.2;
                    SP.s0 = 0.075 + level * 0.012;
                    SP.s1 = 0.015;
                    SP.r0 = col[0]; SP.g0 = col[1]; SP.b0 = col[2];
                    SP.r1 = 0; SP.g1 = 0; SP.b1 = 0;
                    SP.damp = 1.1;
                    SP.grav = -9.0;
                    SP.rot = 0;
                    SP.rotV = 0;
                    SP.mode = 1;
                    SP.stretch = 9.0;
                    this.glow.spawn();
                }
            }
        } else {
            this.accSmoke[slot] = 0;
            this.accSpark[slot] = 0;
        }

        // --- след ускорения -------------------------------------------------
        if (flags & FLAG_BOOST) {
            this.accBoost[slot] += this.Q.boostRate * dt;
            while (this.accBoost[slot] >= 1) {
                this.accBoost[slot] -= 1;
                const side = Math.random() < 0.5 ? 1 : -1;
                SP.x = view.x + fx * (rearZ - 0.15) + lx * side * halfTrack * 0.42;
                SP.y = view.y + 0.32;
                SP.z = view.z + fz * (rearZ - 0.15) + lz * side * halfTrack * 0.42;
                SP.vx = -fx * (5.0 + Math.random() * 5.0) + (Math.random() - 0.5) * 0.7;
                SP.vy = 0.4 + Math.random() * 0.8;
                SP.vz = -fz * (5.0 + Math.random() * 5.0) + (Math.random() - 0.5) * 0.7;
                SP.life = 0.22 + Math.random() * 0.18;
                SP.s0 = 0.2;
                SP.s1 = 0.04;
                const hot = Math.random();
                SP.r0 = 1.0;
                SP.g0 = 0.5 + hot * 0.42;
                SP.b0 = 0.1 + hot * 0.55;
                SP.r1 = 0.25; SP.g1 = 0.08; SP.b1 = 0.0;
                SP.damp = 2.2;
                SP.grav = 1.4;
                SP.rot = 0;
                SP.rotV = 0;
                SP.mode = 1;
                SP.stretch = 7.0;
                this.glow.spawn();
            }
        } else {
            this.accBoost[slot] = 0;
        }

        // --- пыль вне трассы ------------------------------------------------
        if ((flags & FLAG_OFFTRACK) && speed > 3.0) {
            this.accDust[slot] += this.Q.dustRate * clamp(speed / 16, 0.3, 1.4) * dt;
            while (this.accDust[slot] >= 1) {
                this.accDust[slot] -= 1;
                const side = Math.random() < 0.5 ? 1 : -1;
                SP.x = view.x + fx * rearZ + lx * halfTrack * side;
                SP.y = view.y + 0.1;
                SP.z = view.z + fz * rearZ + lz * halfTrack * side;
                SP.vx = -fx * speed * 0.2 + (Math.random() - 0.5) * 1.4;
                SP.vy = 0.9 + Math.random() * 1.1;
                SP.vz = -fz * speed * 0.2 + (Math.random() - 0.5) * 1.4;
                SP.life = 0.45 + Math.random() * 0.3;
                SP.s0 = 0.35;
                SP.s1 = 1.5;
                SP.r0 = 0.64; SP.g0 = 0.56; SP.b0 = 0.42;
                SP.r1 = this.fogR; SP.g1 = this.fogG; SP.b1 = this.fogB;
                SP.damp = 1.8;
                SP.grav = 0.1;
                SP.rot = Math.random() * 6.283;
                SP.rotV = (Math.random() - 0.5) * 1.2;
                SP.mode = 0;
                SP.stretch = 1;
                this.smoke.spawn();
            }
        } else {
            this.accDust[slot] = 0;
        }

        // --- раскрутка после попадания --------------------------------------
        if (flags & FLAG_SPIN) {
            this.accDust[slot] += 26 * dt;
            while (this.accDust[slot] >= 1) {
                this.accDust[slot] -= 1;
                SP.x = view.x + (Math.random() - 0.5) * 1.4;
                SP.y = view.y + 0.5 + Math.random() * 0.5;
                SP.z = view.z + (Math.random() - 0.5) * 1.4;
                SP.vx = (Math.random() - 0.5) * 1.6;
                SP.vy = 1.2 + Math.random() * 1.2;
                SP.vz = (Math.random() - 0.5) * 1.6;
                SP.life = 0.45 + Math.random() * 0.25;
                SP.s0 = 0.3;
                SP.s1 = 1.1;
                SP.r0 = 0.32; SP.g0 = 0.32; SP.b0 = 0.34;
                SP.r1 = this.fogR; SP.g1 = this.fogG; SP.b1 = this.fogB;
                SP.damp = 1.5;
                SP.grav = 0.3;
                SP.rot = Math.random() * 6.283;
                SP.rotV = (Math.random() - 0.5) * 2.4;
                SP.mode = 0;
                SP.stretch = 1;
                this.smoke.spawn();
            }
        }

        // --- купол щита -----------------------------------------------------
        if (flags & FLAG_SHIELD) {
            const idx = this.shieldCount;
            if (idx < MAX_CARS) {
                this.shieldCount++;
                const flash = this.shieldFlash[slot];
                const pulse = 1 + Math.sin(this.time * 5.0 + slot) * 0.035 + flash * 0.25;
                const r = (view.halfLength + 0.2) * pulse;
                const arr = this.shieldMesh.instanceMatrix.array;
                const o = idx * 16;
                writeScaleYaw(arr, o, view.x, view.y + view.halfLength * 0.42, view.z,
                    this.time * 0.6 + slot, r, r * 0.78, r);
                const c = this.shieldMesh.instanceColor.array;
                const k = 1 + flash * 3.2;
                c[idx * 3] = k * 0.85;
                c[idx * 3 + 1] = k;
                c[idx * 3 + 2] = k;
                if (lampsOn) {
                    // купол читается и днём: мягкий холодный ореол по контуру
                    const gl = 0.32 + Math.sin(this.time * 3.2 + slot) * 0.08 + flash * 0.8;
                    this.lamp(view.x, view.y + view.halfLength * 0.5, view.z, r * 2.6,
                        gl * 0.42, gl * 0.78, gl);
                }
            }
        }
        if (this.shieldFlash[slot] > 0) {
            this.shieldFlash[slot] = Math.max(0, this.shieldFlash[slot] - dt * 3.0);
        }
    }

    /** Короткоживущий ореол: взрыв, срабатывание щита. */
    addFlash(x, y, z, size, life, r, g, b) {
        const i = this.flashN;
        if (i >= FLASH_CAP) return;
        this.flashN = i + 1;
        this.flashX[i] = x;
        this.flashY[i] = y;
        this.flashZ[i] = z;
        this.flashSize[i] = size;
        this.flashLife[i] = life;
        this.flashInv[i] = 1 / life;
        this.flashR[i] = r;
        this.flashG[i] = g;
        this.flashB[i] = b;
    }

    /** Вспышка щита: бонус погасил попадание. */
    flashShield(slot, x, y, z) {
        if (slot < 0 || slot >= MAX_CARS) return;
        this.shieldFlash[slot] = 1;
        this.addFlash(x, y + 0.9, z, 4.2, 0.34, 0.45, 0.85, 1.0);
        const n = 18;
        for (let i = 0; i < n; i++) {
            const a = (i / n) * Math.PI * 2;
            const sa = Math.sin(a), ca = Math.cos(a);
            SP.x = x + sa * 1.2;
            SP.y = y + 0.7 + (Math.random() - 0.5) * 0.6;
            SP.z = z + ca * 1.2;
            SP.vx = sa * 5.5;
            SP.vy = 1.4 + Math.random() * 1.6;
            SP.vz = ca * 5.5;
            SP.life = 0.3 + Math.random() * 0.18;
            SP.s0 = 0.12;
            SP.s1 = 0.02;
            SP.r0 = 0.5; SP.g0 = 0.85; SP.b0 = 1.0;
            SP.r1 = 0; SP.g1 = 0; SP.b1 = 0;
            SP.damp = 2.6;
            SP.grav = -1.0;
            SP.rot = 0;
            SP.rotV = 0;
            SP.mode = 1;
            SP.stretch = 7.5;
            this.glow.spawn();
        }
    }

    /**
     * Взрыв: вспышка, разлёт искр и дымные клубы.
     * @param {number} power 1 — попадание ракеты, 0.7 — мина
     */
    explosion(x, y, z, power) {
        const p = power === undefined ? 1 : power;
        const scale = this.Q.explosionScale * p;

        // Ореол взрыва живёт в системе свечения, а не в пуле частиц: так
        // попадание видно даже с выключенными частицами.
        this.addFlash(x, y + 1.0, z, 6.5 * p, 0.4, 1.0, 0.62, 0.22);
        this.addFlash(x, y + 0.7, z, 3.2 * p, 0.16, 1.0, 0.95, 0.8);

        // Ядро вспышки. Билборд вертикальный, поэтому его нижняя половина
        // уходит под полотно и обрезается по глубине ровной прямой линией.
        // Чтобы этот срез не читался как прямоугольник поперёк дороги,
        // вспышка поднята выше кузова и заметно компактнее.
        SP.x = x; SP.y = y + 1.15; SP.z = z;
        SP.vx = 0; SP.vy = 1.6; SP.vz = 0;
        SP.life = 0.24;
        SP.s0 = 1.0 * scale;
        SP.s1 = 2.9 * scale;
        SP.r0 = 1.0; SP.g0 = 0.92; SP.b0 = 0.7;
        SP.r1 = 0.3; SP.g1 = 0.06; SP.b1 = 0.0;
        SP.damp = 3.0; SP.grav = 0;
        SP.rot = 0; SP.rotV = 0; SP.mode = 0; SP.stretch = 1;
        this.glow.spawn();

        // вторая, короткая и очень яркая вспышка в самой точке попадания
        SP.y = y + 0.75;
        SP.vy = 0.6;
        SP.life = 0.12;
        SP.s0 = 0.5 * scale;
        SP.s1 = 1.5 * scale;
        SP.r0 = 1.0; SP.g0 = 1.0; SP.b0 = 0.95;
        SP.r1 = 1.0; SP.g1 = 0.5; SP.b1 = 0.1;
        this.glow.spawn();

        const sparks = Math.round(22 * scale);
        for (let i = 0; i < sparks; i++) {
            const a = Math.random() * Math.PI * 2;
            const e = Math.random() * 1.2;
            const sp = 6 + Math.random() * 12;
            SP.x = x; SP.y = y + 0.5; SP.z = z;
            SP.vx = Math.sin(a) * Math.cos(e) * sp;
            SP.vy = Math.sin(e) * sp * 0.8 + 2;
            SP.vz = Math.cos(a) * Math.cos(e) * sp;
            SP.life = 0.35 + Math.random() * 0.4;
            SP.s0 = 0.13;
            SP.s1 = 0.02;
            SP.r0 = 1.0; SP.g0 = 0.55 + Math.random() * 0.3; SP.b0 = 0.15;
            SP.r1 = 0.3; SP.g1 = 0.05; SP.b1 = 0;
            SP.damp = 1.2;
            SP.grav = -11.0;
            SP.rot = 0; SP.rotV = 0;
            SP.mode = 1; SP.stretch = 8.0;
            this.glow.spawn();
        }

        const puffs = Math.round(12 * scale);
        for (let i = 0; i < puffs; i++) {
            const a = Math.random() * Math.PI * 2;
            const sp = 1.5 + Math.random() * 4.0;
            SP.x = x + Math.sin(a) * 0.4;
            SP.y = y + 0.4 + Math.random() * 0.6;
            SP.z = z + Math.cos(a) * 0.4;
            SP.vx = Math.sin(a) * sp;
            SP.vy = 1.6 + Math.random() * 2.4;
            SP.vz = Math.cos(a) * sp;
            SP.life = 0.7 + Math.random() * 0.5;
            SP.s0 = 0.6 * scale;
            SP.s1 = 2.6 * scale;
            SP.r0 = 0.26; SP.g0 = 0.24; SP.b0 = 0.24;
            SP.r1 = this.fogR; SP.g1 = this.fogG; SP.b1 = this.fogB;
            SP.damp = 1.4;
            SP.grav = 0.6;
            SP.rot = Math.random() * 6.283;
            SP.rotV = (Math.random() - 0.5) * 2.0;
            SP.mode = 0; SP.stretch = 1;
            this.smoke.spawn();
        }
    }

    /**
     * Снаряды из снапшота (5.3): projX, projZ, projYaw, projKind.
     * Высоту берём из рельефа — физика двумерная (раздел 4).
     */
    setProjectiles(snap) {
        if (!snap || !snap.valid) {
            this.projCount = 0;
            return;
        }
        const n = snap.projCount < MAX_PROJECTILES ? snap.projCount : MAX_PROJECTILES;
        this.projCount = n;
        const h = this.heightAt;
        const seenN = this.projSeenN;
        for (let i = 0; i < n; i++) {
            const x = snap.projX[i];
            const z = snap.projZ[i];
            this.projKind[i] = snap.projKind[i];
            this.projX[i] = x;
            this.projZ[i] = z;
            this.projYaw[i] = snap.projYaw[i];
            this.projY[i] = h ? h(x, z) : 0;
            // время появления: ищем идентификатор в таблице прошлого кадра
            const id = snap.projId ? snap.projId[i] : 0;
            let born = -1;
            for (let k = 0; k < seenN; k++) {
                if (this.projSeenId[k] === id) {
                    born = this.projSeenBorn[k];
                    break;
                }
            }
            this.projBorn[i] = born < 0 ? this.time : born;
        }
        for (let i = 0; i < n; i++) {
            this.projSeenId[i] = snap.projId ? snap.projId[i] : 0;
            this.projSeenBorn[i] = this.projBorn[i];
        }
        this.projSeenN = n;
    }

    /** Маска активных боксов из снапшота (12.3). Ссылка на массив, не копия. */
    setBoxMask(snap) {
        if (!snap || !snap.valid) return;
        this.boxMask = snap.boxMask;
        this.boxMaskLen = snap.boxMaskLen;
    }

    /**
     * Шаг всех эффектов и запись инстансных матриц.
     * @param {number} dt          секунды
     * @param {THREE.Camera} camera уже с актуальной matrixWorld
     * @param {number} localSpeed  скорость своей машины, м/с (полосы скорости)
     */
    update(dt, camera, localSpeed) {
        this.time += dt;

        const e = camera.matrixWorld.elements;
        const rx = e[0], ry = e[1], rz = e[2];
        const ux = e[4], uy = e[5], uz = e[6];
        const bx = e[8], by = e[9], bz = e[10];
        const cxp = e[12], cyp = e[13], czp = e[14];

        // --- полосы скорости ------------------------------------------------
        const lines = this.Q.speedLines;
        if (lines > 0 && localSpeed > SPEED_LINE_MIN) {
            const k = (localSpeed - SPEED_LINE_MIN) / 18;
            this.accLines += lines * clamp(k, 0, 1.5) * dt;
            while (this.accLines >= 1) {
                this.accLines -= 1;
                const a = Math.random() * Math.PI * 2;
                const r = 2.2 + Math.random() * 4.0;
                const d = 5 + Math.random() * 14;
                const ox = rx * Math.sin(a) * r + ux * Math.cos(a) * r - bx * d;
                const oy = ry * Math.sin(a) * r + uy * Math.cos(a) * r - by * d;
                const oz = rz * Math.sin(a) * r + uz * Math.cos(a) * r - bz * d;
                SP.x = cxp + ox;
                SP.y = cyp + oy;
                SP.z = czp + oz;
                const sp = localSpeed * 1.4;
                SP.vx = bx * sp;
                SP.vy = by * sp;
                SP.vz = bz * sp;
                SP.life = 0.16 + Math.random() * 0.1;
                SP.s0 = 0.09;
                SP.s1 = 0.02;
                SP.r0 = 0.7; SP.g0 = 0.82; SP.b0 = 1.0;
                SP.r1 = 0; SP.g1 = 0; SP.b1 = 0;
                SP.damp = 0; SP.grav = 0;
                SP.rot = 0; SP.rotV = 0;
                SP.mode = 1; SP.stretch = 26;
                this.glow.spawn();
            }
        } else {
            this.accLines = 0;
        }

        // --- след ракет -----------------------------------------------------
        let rocketN = 0;
        let mineN = 0;
        const rArr = this.rocketMesh.instanceMatrix.array;
        const mArr = this.mineMesh.instanceMatrix.array;
        this.accRocket += dt * 90;
        let trailShots = 0;
        while (this.accRocket >= 1) {
            this.accRocket -= 1;
            trailShots++;
        }
        for (let i = 0; i < this.projCount; i++) {
            const kind = this.projKind[i];
            const x = this.projX[i];
            const z = this.projZ[i];
            const y = this.projY[i];
            if (kind === 3) {
                // --- мина ---------------------------------------------------
                // Первые MINE_ARM секунд мина НЕ взведена (12.10): она
                // разворачивается из сложенного состояния, светится холодным
                // серым и не пульсирует. Взведённая — крупная, с жёлто-чёрным
                // кругом, красным маяком и тревожной пульсацией.
                const age = this.time - this.projBorn[i];
                const arming = age < MINE_ARM;
                const t = arming ? age / MINE_ARM : 1;
                const grow = arming ? 0.42 + t * 0.58 : 1;
                const puls = arming ? 1 : 1 + Math.sin(this.time * 7.5 + i * 1.7) * 0.07;
                const sc = grow * puls;
                const spin = arming ? this.time * 5.0 + i : this.time * 0.9 + i;
                writeScaleYaw(mArr, mineN * 16, x, y + 0.02, z, spin, sc, sc, sc);
                const mc = this.mineMesh.instanceColor.array;
                const mo = mineN * 3;
                if (arming) {
                    // невзведённая: холодная, тусклая, без маяка — она ещё
                    // разворачивается и пока не опасна
                    mc[mo] = 0.3;
                    mc[mo + 1] = 0.42;
                    mc[mo + 2] = 0.6;
                } else {
                    const beat = 0.88 + Math.sin(this.time * 7.5 + i * 1.7) * 0.32;
                    mc[mo] = beat;
                    mc[mo + 1] = beat * 0.8;
                    mc[mo + 2] = beat * 0.62;
                    if (this.glowOn) {
                        // маяк над миной: её видно издалека и под острым углом
                        const gl = 0.55 + Math.sin(this.time * 7.5 + i * 1.7) * 0.45;
                        this.lamp(x, y + 1.06, z, 0.85 + gl * 0.5,
                            1.0 * gl, 0.2 * gl, 0.06 * gl);
                        this.lamp(x, y + 0.06, z, 2.0,
                            0.34 * gl, 0.16 * gl, 0.0);
                    }
                }
                mineN++;
            } else {
                // ракета: летит над поверхностью, слегка покачивается
                const yy = y + 0.72 + Math.sin(this.time * 9 + i) * 0.05;
                writeScaleYaw(rArr, rocketN * 16, x, yy, z, this.projYaw[i], 1, 1, 1);
                rocketN++;
                if (this.glowOn) {
                    // факел в сопле
                    const fx0 = Math.sin(this.projYaw[i]);
                    const fz0 = Math.cos(this.projYaw[i]);
                    const gl = 0.8 + Math.sin(this.time * 31 + i) * 0.2;
                    this.lamp(x - fx0 * 0.45, yy, z - fz0 * 0.45, 1.15,
                        1.0 * gl, 0.55 * gl, 0.16 * gl);
                }
                for (let t = 0; t < trailShots; t++) {
                    const fx = Math.sin(this.projYaw[i]);
                    const fz = Math.cos(this.projYaw[i]);
                    SP.x = x - fx * 0.55 + (Math.random() - 0.5) * 0.1;
                    SP.y = yy + (Math.random() - 0.5) * 0.1;
                    SP.z = z - fz * 0.55 + (Math.random() - 0.5) * 0.1;
                    SP.vx = -fx * 3.0 + (Math.random() - 0.5) * 1.2;
                    SP.vy = 0.6 + Math.random() * 0.8;
                    SP.vz = -fz * 3.0 + (Math.random() - 0.5) * 1.2;
                    SP.life = 0.28 + Math.random() * 0.18;
                    SP.s0 = 0.17;
                    SP.s1 = 0.03;
                    SP.r0 = 1.0; SP.g0 = 0.62; SP.b0 = 0.2;
                    SP.r1 = 0.2; SP.g1 = 0.04; SP.b1 = 0;
                    SP.damp = 2.4; SP.grav = 1.2;
                    SP.rot = 0; SP.rotV = 0;
                    SP.mode = 1; SP.stretch = 6.5;
                    this.glow.spawn();
                }
            }
        }
        this.rocketMesh.count = rocketN;
        this.rocketMesh.visible = rocketN > 0;
        if (rocketN > 0) this.rocketMesh.instanceMatrix.needsUpdate = true;
        this.mineMesh.count = mineN;
        this.mineMesh.visible = mineN > 0;
        if (mineN > 0) {
            this.mineMesh.instanceMatrix.needsUpdate = true;
            this.mineMesh.instanceColor.needsUpdate = true;
        }
        this.rocketMat.emissiveIntensity = 0.8 + Math.sin(this.time * 14) * 0.2;
        this.mineMat.emissiveIntensity = 0.45 + Math.sin(this.time * 7.5) * 0.4;

        // --- щиты -----------------------------------------------------------
        this.shieldMesh.count = this.shieldCount;
        this.shieldMesh.visible = this.shieldCount > 0;
        if (this.shieldCount > 0) {
            this.shieldMesh.instanceMatrix.needsUpdate = true;
            this.shieldMesh.instanceColor.needsUpdate = true;
        }

        // --- боксы с бонусами -----------------------------------------------
        this.updateBoxes(dt);

        // --- короткоживущие вспышки -----------------------------------------
        let f = 0;
        while (f < this.flashN) {
            const life = this.flashLife[f] - dt;
            if (life <= 0) {
                const last = --this.flashN;
                if (f !== last) {
                    this.flashX[f] = this.flashX[last];
                    this.flashY[f] = this.flashY[last];
                    this.flashZ[f] = this.flashZ[last];
                    this.flashLife[f] = this.flashLife[last];
                    this.flashInv[f] = this.flashInv[last];
                    this.flashSize[f] = this.flashSize[last];
                    this.flashR[f] = this.flashR[last];
                    this.flashG[f] = this.flashG[last];
                    this.flashB[f] = this.flashB[last];
                }
                continue;
            }
            this.flashLife[f] = life;
            const k = life * this.flashInv[f];      // 1 в начале, 0 в конце
            if (this.glowOn) {
                const fk = k * this.flashGain;
                this.lamp(this.flashX[f], this.flashY[f], this.flashZ[f],
                    this.flashSize[f] * (1.35 - k * 0.35),
                    this.flashR[f] * fk, this.flashG[f] * fk, this.flashB[f] * fk);
            }
            f++;
        }

        // --- выкладка ореолов: билборды к камере -----------------------------
        this.flushLamps(rx, ry, rz, ux, uy, uz, bx, by, bz);

        // --- частицы --------------------------------------------------------
        this.stepPool(this.glow, this.glowMesh, dt, rx, ry, rz, ux, uy, uz, bx, by, bz, cxp, cyp, czp);
        this.stepPool(this.smoke, this.smokeMesh, dt, rx, ry, rz, ux, uy, uz, bx, by, bz, cxp, cyp, czp);
        this.activeParticles = this.glow.n + this.smoke.n;
    }

    /**
     * Развернуть накопленные ореолы к камере и записать матрицы инстансов.
     * Один draw call на всё свечение сцены; при нуле ореолов меш прячется и
     * в renderer.info.render.calls не попадает вовсе.
     */
    flushLamps(rx, ry, rz, ux, uy, uz, bx, by, bz) {
        const mesh = this.lampMesh;
        const n = this.lampN;
        if (n === 0) {
            mesh.count = 0;
            mesh.visible = false;
            return;
        }
        const mArr = mesh.instanceMatrix.array;
        const cArr = mesh.instanceColor.array;
        for (let i = 0; i < n; i++) {
            writeBillboard(mArr, i * 16, this.lampX[i], this.lampY[i], this.lampZ[i],
                this.lampS[i], 0, rx, ry, rz, ux, uy, uz, bx, by, bz);
            const o = i * 3;
            cArr[o] = this.lampR[i];
            cArr[o + 1] = this.lampG[i];
            cArr[o + 2] = this.lampB[i];
        }
        mesh.count = n;
        mesh.visible = true;
        mesh.instanceMatrix.needsUpdate = true;
        mesh.instanceColor.needsUpdate = true;
    }

    /** Вращение, покачивание и подсветка боксов (12.6). */
    updateBoxes(dt) {
        const mesh = this.boxMesh;
        if (!mesh || this.boxCount === 0) return;
        const arr = mesh.instanceMatrix.array;
        const mask = this.boxMask;
        const maskLen = this.boxMaskLen;
        const grow = this.boxGrow;
        const t = this.time;
        let n = 0;
        for (let i = 0; i < this.boxCount; i++) {
            let active = true;
            if (mask && maskLen > 0) {
                const byteIndex = i >> 3;
                active = byteIndex < maskLen && ((mask[byteIndex] >> (i & 7)) & 1) === 1;
            }
            let g = grow[i];
            if (active) {
                g += dt * 3.2;
                if (g > 1) g = 1;
            } else {
                g -= dt * 6.0;
                if (g < 0) g = 0;
            }
            grow[i] = g;
            if (g <= 0.001) continue;
            const bob = Math.sin(t * 2.0 + i * 0.9) * 0.13;
            const s = g * (0.92 + Math.sin(t * 4.0 + i) * 0.05);
            writeScaleYaw(arr, n * 16,
                this.boxX[i], this.boxY[i] + 0.78 + bob, this.boxZ[i],
                t * 1.5 + i * 0.7, s, s, s);
            n++;
        }
        mesh.count = n;
        mesh.visible = n > 0;
        if (n > 0) mesh.instanceMatrix.needsUpdate = true;
        if (this.boxMat) {
            this.boxMat.emissiveIntensity = 0.42 + Math.sin(t * 3.4) * 0.22;
        }
    }

    /**
     * Шаг одного пула: интегрирование, отбраковка мёртвых, запись матриц
     * и цветов инстансов. Ни одной аллокации.
     */
    stepPool(pool, mesh, dt, rx, ry, rz, ux, uy, uz, bx, by, bz, camX, camY, camZ) {
        const mArr = mesh.instanceMatrix.array;
        const cArr = mesh.instanceColor.array;
        let i = 0;
        while (i < pool.n) {
            let life = pool.life[i] - dt;
            if (life <= 0) {
                const last = --pool.n;
                if (i !== last) pool.move(last, i);
                continue;
            }
            pool.life[i] = life;
            const t = 1 - life * pool.invLife[i];

            let vx = pool.vx[i];
            let vy = pool.vy[i] + pool.grav[i] * dt;
            let vz = pool.vz[i];
            let d = 1 - pool.damp[i] * dt;
            if (d < 0) d = 0;
            vx *= d; vy *= d; vz *= d;
            pool.vx[i] = vx;
            pool.vy[i] = vy;
            pool.vz[i] = vz;

            const px = pool.px[i] + vx * dt;
            const py = pool.py[i] + vy * dt;
            const pz = pool.pz[i] + vz * dt;
            pool.px[i] = px;
            pool.py[i] = py;
            pool.pz[i] = pz;

            const size = pool.s0[i] + (pool.s1[i] - pool.s0[i]) * t;
            const o = i * 16;

            if (pool.mode[i] === 1) {
                // вытянутая по вектору скорости: искры, след буста, полосы
                let dx = vx, dy = vy, dz = vz;
                let len = Math.sqrt(dx * dx + dy * dy + dz * dz);
                if (len < 1e-4) {
                    writeBillboard(mArr, o, px, py, pz, size, 0, rx, ry, rz, ux, uy, uz, bx, by, bz);
                } else {
                    dx /= len; dy /= len; dz /= len;
                    const tx = camX - px, ty = camY - py, tz = camZ - pz;
                    let ax = dy * tz - dz * ty;
                    let ay = dz * tx - dx * tz;
                    let az = dx * ty - dy * tx;
                    const al = Math.sqrt(ax * ax + ay * ay + az * az);
                    if (al < 1e-4) {
                        writeBillboard(mArr, o, px, py, pz, size, 0, rx, ry, rz, ux, uy, uz, bx, by, bz);
                    } else {
                        ax /= al; ay /= al; az /= al;
                        const nx = ay * dz - az * dy;
                        const ny = az * dx - ax * dz;
                        const nz = ax * dy - ay * dx;
                        const L = size * pool.stretch[i];
                        mArr[o] = ax * size; mArr[o + 1] = ay * size; mArr[o + 2] = az * size; mArr[o + 3] = 0;
                        mArr[o + 4] = dx * L; mArr[o + 5] = dy * L; mArr[o + 6] = dz * L; mArr[o + 7] = 0;
                        mArr[o + 8] = nx; mArr[o + 9] = ny; mArr[o + 10] = nz; mArr[o + 11] = 0;
                        mArr[o + 12] = px; mArr[o + 13] = py; mArr[o + 14] = pz; mArr[o + 15] = 1;
                    }
                }
            } else {
                const rot = pool.rot[i] + pool.rotV[i] * dt;
                pool.rot[i] = rot;
                writeBillboard(mArr, o, px, py, pz, size, rot, rx, ry, rz, ux, uy, uz, bx, by, bz);
            }

            const co = i * 3;
            cArr[co] = pool.r0[i] + (pool.r1[i] - pool.r0[i]) * t;
            cArr[co + 1] = pool.g0[i] + (pool.g1[i] - pool.g0[i]) * t;
            cArr[co + 2] = pool.b0[i] + (pool.b1[i] - pool.b0[i]) * t;
            i++;
        }

        mesh.count = pool.n;
        mesh.visible = pool.n > 0;
        if (pool.n > 0) {
            mesh.instanceMatrix.needsUpdate = true;
            mesh.instanceColor.needsUpdate = true;
        }
    }

    /** Погасить всё: перезапуск гонки без пересоздания пулов. */
    reset() {
        this.glow.clear();
        this.smoke.clear();
        this.projCount = 0;
        this.projSeenN = 0;
        this.shieldCount = 0;
        this.flashN = 0;
        this.lampN = 0;
        this.accSmoke.fill(0);
        this.accSpark.fill(0);
        this.accBoost.fill(0);
        this.accDust.fill(0);
        this.shieldFlash.fill(0);
        this.glowMesh.visible = false;
        this.smokeMesh.visible = false;
        this.rocketMesh.visible = false;
        this.mineMesh.visible = false;
        this.shieldMesh.visible = false;
        this.lampMesh.visible = false;
    }

    /** Полное освобождение: геометрии, материалы, текстуры. */
    dispose() {
        this.boxMesh = null;
        this.boxMat = null;
        disposeObject(this.group);
        this.glowTex.dispose();
        this.smokeTex.dispose();
    }
}

// ---------------------------------------------------------------------------
// Помощники записи матриц (числами, без THREE.Matrix4 в кадре)
// ---------------------------------------------------------------------------

/** Инстансированный меш пула частиц с готовым instanceColor. */
function makeInstanced(geometry, material, capacity, name) {
    const mesh = new THREE.InstancedMesh(geometry, material, capacity);
    mesh.name = name;
    mesh.frustumCulled = false;
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    mesh.instanceColor = new THREE.InstancedBufferAttribute(
        new Float32Array(capacity * 3).fill(1), 3
    );
    mesh.instanceColor.setUsage(THREE.DynamicDrawUsage);
    mesh.count = 0;
    mesh.visible = false;
    return mesh;
}

/** Квад, развёрнутый к камере, с поворотом rot вокруг взгляда. */
function writeBillboard(a, o, px, py, pz, size, rot, rx, ry, rz, ux, uy, uz, bx, by, bz) {
    const c = Math.cos(rot) * size;
    const s = Math.sin(rot) * size;
    a[o] = rx * c + ux * s;
    a[o + 1] = ry * c + uy * s;
    a[o + 2] = rz * c + uz * s;
    a[o + 3] = 0;
    a[o + 4] = -rx * s + ux * c;
    a[o + 5] = -ry * s + uy * c;
    a[o + 6] = -rz * s + uz * c;
    a[o + 7] = 0;
    a[o + 8] = bx;
    a[o + 9] = by;
    a[o + 10] = bz;
    a[o + 11] = 0;
    a[o + 12] = px;
    a[o + 13] = py;
    a[o + 14] = pz;
    a[o + 15] = 1;
}

/**
 * Матрица «поворот вокруг Y + масштаб + перенос» прямо в массив инстансов.
 * Столбцы: (cos*sx, 0, -sin*sx), (0, sy, 0), (sin*sz, 0, cos*sz).
 */
export function writeScaleYaw(a, o, x, y, z, yaw, sx, sy, sz) {
    const c = Math.cos(yaw);
    const s = Math.sin(yaw);
    a[o] = c * sx;
    a[o + 1] = 0;
    a[o + 2] = -s * sx;
    a[o + 3] = 0;
    a[o + 4] = 0;
    a[o + 5] = sy;
    a[o + 6] = 0;
    a[o + 7] = 0;
    a[o + 8] = s * sz;
    a[o + 9] = 0;
    a[o + 10] = c * sz;
    a[o + 11] = 0;
    a[o + 12] = x;
    a[o + 13] = y;
    a[o + 14] = z;
    a[o + 15] = 1;
}

export default Effects;
