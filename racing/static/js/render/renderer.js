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
 *   effects      0-5 см. шапку effects.js
 *   cockpit      0-2 панель и руль, только в виде из кокпита
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
import { buildScenery } from './scenery.js';
import { buildCarMesh, disposeCarCache } from './carmesh.js';
import { disposeObject, solidify, toColor, mergeGeometries } from './geomutil.js';
import { Effects, createRadialTexture, writeScaleYaw } from './effects.js';
import { Track } from '../track.js';
import { getCatalog } from '../cars.js';
import {
    MAX_CARS,
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
    low: { renderScale: 0.5, shadowSeg: 8, cockpitDetail: 0, shadowOpacity: 0.34 },
    medium: { renderScale: 0.75, shadowSeg: 14, cockpitDetail: 1, shadowOpacity: 0.4 },
    high: { renderScale: 1.0, shadowSeg: 22, cockpitDetail: 2, shadowOpacity: 0.44 }
};

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
        this.local = false;

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
        this.renderScale = o.renderScale || this.preset.renderScale;

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

        this.shadowMesh = null;
        this.shadowTex = null;
        this.boxMesh = null;
        this.boxCount = 0;

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
     * Сменить пресет качества. Если гонка уже собрана, её приходится
     * пересобрать: плотность сетки трассы и декора заложена в геометрию.
     * Возвращает true, если пересборка состоялась.
     */
    setQuality(quality) {
        if (!QUALITY_PRESETS[quality] || quality === this.quality) return false;
        this.quality = quality;
        this.preset = QUALITY_PRESETS[quality];
        this.renderScale = this.preset.renderScale;
        this.stats.quality = quality;
        this.applySize();
        if (this.raceReady) {
            const track = this.trackSource;
            const players = this.playerSource;
            const local = this.localSlot;
            this.disposeRace();
            this.createRace(track, players, { localSlot: local });
            return true;
        }
        return false;
    }

    // -----------------------------------------------------------------------
    // Сборка гонки
    // -----------------------------------------------------------------------

    /**
     * Собрать сцену гонки.
     * @param {object} track   экземпляр Track из js/track.js либо объект
     *                         race_init.track формата 12.1
     * @param {Array}  players [{slot, color, car|shape}] — расстановка из race_init
     * @param {object} opts    { localSlot }
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

        // 12.6: buildTrackMeshes и buildScenery ОБЯЗАНЫ получить один и тот же
        // quality, иначе декор всплывёт над землёй.
        this.trackMeshes = buildTrackMeshes(this.track, this.theme, quality);
        this.scenery = buildScenery(this.track, this.theme, this.track.decorSeed, quality);
        this.sampler = createTerrainSampler(this.track, this.theme, quality);

        this.scene.add(this.trackMeshes.group);
        this.scene.add(this.scenery.group);

        // туман, фон и свет — как отдаёт buildScenery (12.6)
        const sc = this.scenery;
        this.fog.color.copy(sc.fog.color);
        this.fog.near = sc.fog.near;
        this.fog.far = sc.fog.far;
        this.scene.background = sc.background;
        this.camera.far = sc.fog.far + 700;
        this.camera.updateProjectionMatrix();

        this.ambient.color.copy(sc.light.ambient.color);
        this.ambient.intensity = sc.light.ambient.intensity;
        this.sun.color.copy(sc.light.directional.color);
        this.sun.intensity = sc.light.directional.intensity;
        const d = sc.light.directional.direction;
        const dl = Math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]) || 1;
        this.sun.position.set((d[0] / dl) * 220, (d[1] / dl) * 220, (d[2] / dl) * 220);
        this.sunTarget.position.set(0, 0, 0);

        this.buildShadows();
        this.buildBoxes();

        // --- эффекты ---------------------------------------------------------
        this.effects = new Effects({
            quality: quality,
            fogColor: sc.fog.color,
            heightAt: this._heightAt
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
        for (let i = 0; i < MAX_CARS; i++) this.views[i].local = i === this.localSlot;
    }

    /** Машины из списка игроков race_init. */
    buildCars(players) {
        if (!players) return;
        const catalog = getCatalog();
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
            const mesh = buildCarMesh(shape, p.color || '#e5484d', this.quality);
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
            view.shadowSx = mesh.shape.width * 0.78;
            view.shadowSz = mesh.shape.length * 0.62;
            view.hint = -1;
            view.wheelSpin = 0;
            view.roll = 0;
            view.bodyPitch = 0;
            view.terrainPitch = 0;
        }
        this.stats.cars = this.countCars();
    }

    countCars() {
        let n = 0;
        for (let i = 0; i < MAX_CARS; i++) if (this.views[i].present) n++;
        return n;
    }

    /**
     * Тени машин: ОДИН InstancedMesh на все восемь (12.6). Мягкое тёмное пятно
     * на сгенерированной в canvas текстуре, прижатое к поверхности. Никаких
     * карт теней — их запрещает раздел 1.
     */
    buildShadows() {
        const geom = new THREE.PlaneGeometry(1, 1);
        geom.rotateX(-Math.PI * 0.5); // в плоскость XZ, лицом вверх
        this.shadowTex = createRadialTexture(64, 0.4, 0.78);
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
        const geom = solidify(new THREE.OctahedronGeometry(0.92, 0), toColor('#ffd24a'),
            function (x, y, z, i, c) {
                const t = y > 0 ? 1 : 0;
                c.setRGB(1.0, 0.82 + t * 0.16, 0.28 + t * 0.42);
            });
        geom.scale(1, 1.2, 1);
        const mat = new THREE.MeshLambertMaterial({
            vertexColors: true,
            flatShading: true,
            emissive: new THREE.Color('#ffae1a'),
            emissiveIntensity: 0.55
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
        panel.position.set(0, -0.34, -0.52);
        dash.add(panel);
        root.add(dash);

        // --- руль -----------------------------------------------------------
        const wheelGeom = buildSteeringWheelGeometry(detail);
        const wheelMat = new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true });
        wheelMat.name = 'cockpitWheel';
        const wheel = new THREE.Mesh(wheelGeom, wheelMat);
        wheel.name = 'cockpitWheel';
        wheel.frustumCulled = false;
        wheel.position.set(0, -0.26, -0.44);
        wheel.rotation.x = -0.34;
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
    }

    /** Попадание по машине: взрыв в её точке. */
    hitCar(slot, power) {
        if (slot < 0 || slot >= MAX_CARS || !this.effects) return;
        const v = this.views[slot];
        this.effects.explosion(v.x, v.y, v.z, power);
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

        const shadowArr = this.shadowMesh.instanceMatrix.array;
        let shadowN = 0;

        this.effects.beginFrame();

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

            this.effects.emitFromCar(v, step);
        }

        this.shadowMesh.count = shadowN;
        this.shadowMesh.visible = shadowN > 0;
        if (shadowN > 0) this.shadowMesh.instanceMatrix.needsUpdate = true;

        // камера
        const local = this.localSlot >= 0 ? this.views[this.localSlot] : null;
        const target = local && local.present && local.fresh ? local : this.firstVisibleCar();
        if (target) this.updateCamera(step, target);
        this.camera.updateMatrixWorld(true);

        // купол неба держится над камерой (12.6)
        this.scenery.updateSky(this.camera);

        this.effects.update(step, this.camera, target ? target.speed : 0);
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
        }
        // кэш чертежей общий на все машины (carmesh.js), чистится отдельно
        disposeCarCache();

        if (this.effects) {
            this.scene.remove(this.effects.root);
            this.effects.dispose();
            this.effects = null;
        }
        if (this.shadowMesh) {
            this.scene.remove(this.shadowMesh);
            disposeObject(this.shadowMesh);
            this.shadowTex.dispose();
            this.shadowMesh = null;
            this.shadowTex = null;
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
 * Панель приборов: изогнутый козырёк, два круглых прибора и стойки по краям.
 * Одна геометрия — один draw call.
 */
function buildDashGeometry(detail) {
    const geoms = [];
    const mats = [];
    const dark = toColor('#15181e');
    const plastic = toColor('#23272f');
    const glow = toColor('#48d1a0');
    const m = new THREE.Matrix4();

    // основной массив панели
    const body = solidify(new THREE.BoxGeometry(1.5, 0.3, 0.5), plastic);
    m.makeRotationX(0.22);
    m.setPosition(0, -0.08, 0.02);
    geoms.push(body);
    mats.push(m.clone());

    // козырёк над приборами
    const hood = solidify(new THREE.BoxGeometry(0.72, 0.06, 0.3), dark);
    m.makeRotationX(-0.35);
    m.setPosition(0, 0.14, 0.06);
    geoms.push(hood);
    mats.push(m.clone());

    // два прибора
    const seg = detail > 0 ? 12 : 6;
    for (let k = -1; k <= 1; k += 2) {
        const face = solidify(new THREE.CylinderGeometry(0.12, 0.12, 0.02, seg), dark);
        m.makeRotationX(Math.PI * 0.5 + 0.22);
        m.setPosition(k * 0.17, 0.04, 0.2);
        geoms.push(face);
        mats.push(m.clone());

        const ring = solidify(new THREE.TorusGeometry(0.12, 0.015, 4, seg), glow);
        m.makeRotationX(0.22);
        m.setPosition(k * 0.17, 0.045, 0.21);
        geoms.push(ring);
        mats.push(m.clone());
    }

    if (detail > 0) {
        // стойки по краям кадра
        for (let k = -1; k <= 1; k += 2) {
            const pillar = solidify(new THREE.BoxGeometry(0.1, 1.1, 0.1), dark);
            m.makeRotationZ(k * 0.2);
            m.setPosition(k * 0.82, 0.5, 0.1);
            geoms.push(pillar);
            mats.push(m.clone());
        }
    }
    if (detail > 1) {
        // центральная консоль
        const console3 = solidify(new THREE.BoxGeometry(0.28, 0.12, 0.16), dark);
        m.makeRotationX(0.22);
        m.setPosition(0, 0.02, 0.24);
        geoms.push(console3);
        mats.push(m.clone());
    }

    const merged = mergeGeometries(geoms, mats);
    for (let i = 0; i < geoms.length; i++) geoms[i].dispose();
    return merged;
}

/** Руль: обод, три спицы и ступица. */
function buildSteeringWheelGeometry(detail) {
    const geoms = [];
    const mats = [];
    const rim = toColor('#1b1e24');
    const trim = toColor('#c2c8d2');
    const m = new THREE.Matrix4();
    const seg = detail > 0 ? 16 : 8;

    const ring = solidify(new THREE.TorusGeometry(0.17, 0.022, 4, seg), rim);
    geoms.push(ring);
    mats.push(new THREE.Matrix4());

    for (let k = 0; k < 3; k++) {
        const a = -Math.PI * 0.5 + (k / 3) * Math.PI * 2;
        const spoke = solidify(new THREE.BoxGeometry(0.145, 0.022, 0.02), rim);
        m.makeRotationZ(a);
        const off = new THREE.Matrix4().makeTranslation(0.085, 0, 0);
        m.multiply(off);
        geoms.push(spoke);
        mats.push(m.clone());
    }

    const hub = solidify(new THREE.CylinderGeometry(0.045, 0.045, 0.03, seg > 8 ? 8 : 6), trim);
    m.makeRotationX(Math.PI * 0.5);
    m.setPosition(0, 0, 0.01);
    geoms.push(hub);
    mats.push(m.clone());

    const merged = mergeGeometries(geoms, mats);
    for (let i = 0; i < geoms.length; i++) geoms[i].dispose();
    return merged;
}

export default RaceRenderer;
