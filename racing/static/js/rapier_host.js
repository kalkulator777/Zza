// Хозяин модуля физики в браузере: WebAssembly.instantiate + плоский ABI.
//
// Зеркало game/rapier_host.py. Модуль физики один и тот же файл
// (native/testbed/racing_physics.wasm), импортов у него ноль — поэтому
// объект импортов пустой, и никакого рантайма подтягивать не надо.
//
// Раскладку общей памяти ЗДЕСЬ НЕ ОПИСЫВАЮТ: она импортируется из
// native/abi/abi_layout.js, выпущенного tools/gen_abi.py. Три описания
// одной структуры — ровно та болезнь, ради которой делался этап 4a.
//
// Линейная память wasm умеет расти, и при росте старый ArrayBuffer
// отсоединяется, а все виды поверх него становятся мусором. Поэтому виды
// пересоздаются в _sync() по смене buffer — перед каждым шагом и после
// каждой аллокации внутри модуля.

// Адреса на сервере (см. маршрут /native/... в server/app.py, он появляется
// только при поднятом флаге --physics).
export const WASM_URL = '/native/testbed/racing_physics.wasm';
export const ABI_URL = '/native/abi/abi_layout.js';

// Шаг записи коробки трамплина (§12.30). Импортируется, а не повторяется:
// формат объявлен один раз, в game/track.py, и track.js его зеркалит.
import { RAMP_BOX_FLOATS } from './track.js';

// Геометрии полотна ЗДЕСЬ НЕТ. Модуль физики игровых констант не знает, и обе
// стороны обязаны передать ему одно и то же; на 4b ради этого в каждом хозяине
// лежало по копии WALL_HEIGHT и TRACK_FRICTION. Теперь все три числа объявлены
// один раз в game/track.py и приезжают в race_init.track (§12.23) — отсюда их
// и берёт meshParams(). Копии нет, и расходиться нечему.

/**
 * Три числа сетки полотна из записи трассы: [wallMargin, wallHeight, friction].
 * Принимает и объект Track (camelCase), и сырой race_init.track (12.1).
 */
export function meshParams(track) {
    const margin = track.wallMargin !== undefined ? track.wallMargin : track.wall_margin;
    const height = track.wallHeight !== undefined ? track.wallHeight : track.wall_height;
    const rub = track.friction;
    if (margin === undefined || height === undefined || rub === undefined) {
        throw new HostError('в записи трассы нет чисел сетки полотна ' +
            '(wall_margin, wall_height, friction) — строить её не из чего (§12.23)');
    }
    return [margin, height, rub];
}

// Порядок столбцов залитой осевой линии — 12.1. Первое имя — как поле зовётся
// в сыром race_init.track, второе — как его назвал Track.fromServer у себя.
// Оба источника несут ОДНИ И ТЕ ЖЕ числа (round(v, 3) в f32), и брать их надо
// уметь из обоих: рендеру уходит объект Track, а по проводу идёт словарь.
const COLUMNS = ['x', 'y', 'z', 'tx', 'tz', 'nx', 'nz', 'hw', 's'];
const COLUMNS_TRACK = ['cx', 'cy', 'cz', 'ctx', 'ctz', 'cnx', 'cnz', 'chw', 'cs'];

export class HostError extends Error {}

function hex32(value) {
    return (value >>> 0).toString(16).padStart(8, '0');
}

/** Байты модуля: URL, ArrayBuffer, Uint8Array или Response. */
async function wasmBytes(source) {
    if (source instanceof Uint8Array) return source;
    if (source instanceof ArrayBuffer) return new Uint8Array(source);
    if (typeof source === 'string') {
        const reply = await fetch(source);
        if (!reply.ok) throw new HostError('модуль физики не отдался: ' + reply.status);
        return new Uint8Array(await reply.arrayBuffer());
    }
    throw new HostError('непонятный источник модуля физики');
}

/** Раскладка: либо уже импортированный модуль, либо адрес для import(). */
async function loadAbi(source) {
    if (source && typeof source === 'object') return source;
    return import(source || ABI_URL);
}

export class RapierHost {
    /**
     * Загрузить модуль и создать мир.
     * @param {object} opts
     *   wasm  — адрес .wasm либо его байты (умолчание WASM_URL);
     *   abi   — адрес abi_layout.js либо сам модуль (умолчание ABI_URL);
     *   preset — 0 аркада, 1 симулятор.
     */
    static async load(opts = {}) {
        const abi = await loadAbi(opts.abi);
        const bytes = await wasmBytes(opts.wasm || WASM_URL);
        // Импортов у модуля нет — объект импортов пустой (§12.21).
        const { instance } = await WebAssembly.instantiate(bytes, {});
        return new RapierHost(instance, abi, opts.preset || 0);
    }

    constructor(instance, abi, preset = 0) {
        this.abi = abi;
        this.x = instance.exports;
        this.mem = this.x.memory;
        this._buf = null;

        const version = this.x.rp_abi_version();
        if (version !== abi.ABI_VERSION) {
            throw new HostError('ABI модуля ' + version + ', раскладки ' + abi.ABI_VERSION);
        }
        if (this.x.rp_output_stride() !== abi.CarOut.SIZE) {
            throw new HostError('шаг записи вывода не совпал с раскладкой');
        }
        if (this.x.rp_effect_floats() !== abi.CarEffect.FLOATS) {
            throw new HostError('запись воздействия не совпала с раскладкой');
        }
        if (this.x.rp_max_cars() !== abi.MAX_CARS) {
            throw new HostError('число машин в модуле не совпало с раскладкой');
        }
        if (this.x.rp_track_columns() !== abi.TRACK_COLUMNS) {
            throw new HostError('столбцов осевой в модуле не столько, сколько в раскладке');
        }
        this.dt = this.x.rp_dt();
        this.reset(preset);
    }

    // --- память ---------------------------------------------------------

    _sync(force) {
        const buffer = this.mem.buffer;
        if (!force && buffer === this._buf) return;
        this._buf = buffer;
        const a = this.abi;
        const x = this.x;
        this.inputs = new Float32Array(buffer, x.rp_inputs_ptr(), a.MAX_CARS * a.CarInput.FLOATS);
        // Внешние воздействия (§12.25): доза на один шаг, модуль её стирает.
        this.effects = new Float32Array(buffer, x.rp_effects_ptr(), a.MAX_CARS * a.CarEffect.FLOATS);
        this.outputs = new Float32Array(buffer, x.rp_outputs_ptr(), a.MAX_CARS * a.CarOut.FLOATS);
        this.descs = new Float32Array(buffer, x.rp_descs_ptr(), a.MAX_CARS * a.CarDesc.FLOATS);
        this.tuning = new Float32Array(buffer, x.rp_tuning_ptr(), a.CarTuning.FLOATS);
        this.props = new Float32Array(buffer, x.rp_props_ptr(), a.MAX_PROPS * a.PropOut.FLOATS);
        this.saves = new Float32Array(buffer, x.rp_saves_ptr(), a.MAX_CARS * a.CarSave.FLOATS);
    }

    // --- мир ------------------------------------------------------------

    reset(preset = 0) {
        this.x.rp_world_reset(preset | 0);
        this._sync(true);
    }

    /**
     * Залить осевую линию и построить полотно.
     * track — объект из race_init (12.1) либо Track.fromServer: девять
     * массивов и count. Числа те же, что на сервере: сервер держит
     * _f32(round(v, 3)), клиент кладёт round(v, 3) в Float32Array —
     * это одни и те же биты, и на них обе физики обязаны совпасть.
     */
    buildTrack(track, wallMargin, wallHeight, friction) {
        const fromTrack = meshParams(track);
        if (wallMargin === undefined) wallMargin = fromTrack[0];
        if (wallHeight === undefined) wallHeight = fromTrack[1];
        if (friction === undefined) friction = fromTrack[2];
        const names = track.x !== undefined ? COLUMNS : COLUMNS_TRACK;
        const first = track[names[0]];
        if (first === undefined) {
            throw new HostError('в записи трассы нет осевой линии: ни x, ни cx');
        }
        const n = track.count || first.length;
        if (n < 3) throw new HostError('осевая линия короче трёх точек');
        const offset = this.x.rp_track_alloc_centerline(n);
        if (offset === 0) throw new HostError('модуль не дал буфер под осевую линию');
        // Аллокация могла сдвинуть память: вид строим ПОСЛЕ неё.
        this._sync(true);
        const flat = new Float32Array(this.mem.buffer, offset, n * names.length);
        for (let c = 0; c < names.length; c++) {
            const column = track[names[c]];
            const base = c * n;
            for (let i = 0; i < n; i++) flat[base + i] = column[i];
        }
        const code = this.x.rp_track_build(wallMargin, wallHeight, friction);
        if (code !== 0) {
            throw new HostError('rp_track_build вернул ' + code +
                ' (1 — осевая не залита, 2 — Rapier не принял сетку)');
        }
        this._sync(true);
    }

    /**
     * Ровная площадка под испытания. Оба числа задаёт вызывающий: чисел
     * полотна у хозяина больше нет, они приезжают в записи трассы (§12.23).
     */
    addGround(halfSize, friction) {
        this.x.rp_add_ground(halfSize, friction);
        this._sync(true);
    }

    freeTrackMesh() {
        this.x.rp_track_free_mesh();
        this._sync(true);
    }

    /**
     * Коробка в мире. mass <= 0 — неподвижный коллайдер, тела не заводит
     * и MAX_PROPS не расходует (§12.30).
     */
    addBox(hx, hy, hz, x, y, z, yaw, pitch, friction, mass) {
        this.x.rp_add_box(hx, hy, hz, x, y, z, yaw, pitch, friction, mass || 0);
    }

    /**
     * Трамплины трассы неподвижными коробками (§12.30).
     *
     * Числа здесь НЕ считаются: они выведены один раз на сервере
     * (game/track.py, Track._ramp_boxes) и приехали в race_init готовыми,
     * вместе с остальной геометрией трамплина. Считать их у себя значило бы
     * завести вторую геометрию, обязанную совпасть с первой до бита, — то
     * есть ровно ту болезнь, от которой полотно лечится постройкой сетки
     * ВНУТРИ модуля (native/src/trackmesh.rs). Угол коробки берётся через
     * atan2, а он у CPython и у V8 имеет право разойтись в последнем разряде.
     *
     * friction — трение полотна, уже домноженное на погоду.
     */
    addRamps(track, friction) {
        const rows = (track && track.ramps) || [];
        const stride = RAMP_BOX_FLOATS;
        let count = 0;
        for (let k = 0; k < rows.length; k++) {
            const box = rows[k].boxes;
            if (!box || !box.length) continue;
            if (box.length % stride) {
                throw new HostError('коробки трамплина: ' + box.length +
                    ' чисел, не кратно ' + stride);
            }
            for (let i = 0; i < box.length; i += stride) {
                this.x.rp_add_box(box[i], box[i + 1], box[i + 2], box[i + 3],
                                  box[i + 4], box[i + 5], box[i + 6],
                                  box[i + 7], friction, 0);
                count++;
            }
        }
        // Вставка коллайдеров могла подвинуть память: виды после неё
        // недействительны. Один раз на все коробки, а не на каждую.
        if (count) this._sync(true);
        return count;
    }

    propCount() { return this.x.rp_prop_count(); }

    spawnCar(x, y, z, yaw) {
        const idx = this.x.rp_car_spawn(x, y, z, yaw);
        this._sync(true);
        return idx;
    }

    carCount() { return this.x.rp_car_count(); }

    tuningPreset(preset) {
        this.x.rp_tuning_preset(preset | 0);
        this._sync();
    }

    /** Имя поля CarTuning -> индекс во f32. ВЫВОДИТСЯ из раскладки. */
    tuningIndex() {
        if (this._tuningIndex) return this._tuningIndex;
        const out = Object.create(null);
        const src = this.abi.CarTuning;
        for (const name of Object.keys(src)) {
            if (name === 'SIZE' || name === 'FLOATS') continue;
            if (name !== name.toUpperCase()) continue;
            out[name.toLowerCase()] = src[name];
        }
        this._tuningIndex = out;
        return out;
    }

    /**
     * Наложить настройки машины на шаблон: {имя поля: число}. Зеркало
     * RapierHost.set_tuning из game/rapier_host.py — правки ложатся ПОВЕРХ
     * загруженного пресета, следующая spawnCar читает шаблон как есть.
     */
    setTuning(values) {
        if (!values) return;
        this._sync();
        const index = this.tuningIndex();
        for (const name of Object.keys(values)) {
            const at = index[name];
            if (at === undefined) {
                throw new HostError('в CarTuning нет поля ' + name);
            }
            this.tuning[at] = values[name];
        }
    }

    /** Шаблон целиком, именами полей. */
    tuningValues() {
        this._sync();
        const index = this.tuningIndex();
        const out = Object.create(null);
        for (const name of Object.keys(index)) out[name] = this.tuning[index[name]];
        return out;
    }

    // --- шаг ------------------------------------------------------------

    setInput(idx, throttle, brake, steer, handbrake) {
        const a = this.abi.CarInput;
        const base = idx * a.FLOATS;
        const inputs = this.inputs;
        inputs[base + a.THROTTLE] = throttle;
        inputs[base + a.BRAKE] = brake;
        inputs[base + a.STEER] = steer;
        inputs[base + a.HANDBRAKE] = handbrake;
    }

    /** Шаг(и) по 1/60 с. Один вызов на тик — это весь стык с модулем. */
    step(ticks = 1) {
        this._sync();
        return this.x.rp_step(ticks | 0);
    }

    // --- чтение ---------------------------------------------------------

    /** 52 числа состояния машины в переданный приёмник (без аллокаций в кадре). */
    carOut(idx, into) {
        const a = this.abi.CarOut;
        const base = idx * a.FLOATS;
        const dst = into || new Float32Array(a.FLOATS);
        for (let i = 0; i < a.FLOATS; i++) dst[i] = this.outputs[base + i];
        return dst;
    }

    carPose(idx, into) {
        const a = this.abi.CarOut;
        const base = idx * a.FLOATS;
        const out = this.outputs;
        const dst = into || new Float32Array(5);
        dst[0] = out[base + a.PX];
        dst[1] = out[base + a.PY];
        dst[2] = out[base + a.PZ];
        dst[3] = out[base + a.YAW];
        dst[4] = out[base + a.SPEED];
        return dst;
    }

    carSave(idx) { return this.x.rp_car_save(idx | 0); }
    carRestore(idx) { return this.x.rp_car_restore(idx | 0); }

    // --- хэши -----------------------------------------------------------

    // Половинками по u32, а не i64: BigInt не нужен, и строка выходит
    // ровно та же, что печатает game/rapier_host.py.
    _hash(name) {
        return hex32(this.x[name + '_hi']()) + hex32(this.x[name + '_lo']());
    }

    worldHash() { return this._hash('rp_world_hash'); }
    stateHash() { return this._hash('rp_state_hash'); }
    trackHashes() {
        return [this._hash('rp_track_verts_hash'), this._hash('rp_track_tris_hash')];
    }
    meshSize() { return [this.x.rp_track_vert_count(), this.x.rp_track_tri_count()]; }
}

// ===========================================================================
// Гонку считает Rapier: предсказание и реконсиляция своей машины (§12.24)
// ===========================================================================
//
// Зеркало RapierRace из game/rapier_host.py, но с одной принципиальной
// разницей: у клиента в мире ОДНА машина — своя. Чужих тел у него нет и
// быть не может (снапшот везёт позы, а не состояние солвера), поэтому
// столкновения с соседями клиент не предсказывает — ровно как и в классике
// (§6.2: resolve_collisions считает только сервер). Толчок от соседа
// приезжает следующим снапшотом и гасится обычной реконсиляцией.

// Числа награды за занос (6.3) ИМПОРТИРУЮТСЯ из physics.js, а не повторяются
// здесь: модуль игровых констант не знает (12.21), пресет их не выдумывает,
// и второго места, где они записаны, быть не должно. Сервер берёт ту же
// шестёрку из game/physics.py — это их единственная пара объявлений.
import {
    DRIFT_CHARGE_L1, DRIFT_CHARGE_L2, DRIFT_CHARGE_L3,
    DRIFT_BOOST_L1, DRIFT_BOOST_L2, DRIFT_BOOST_L3, BOOST_ACCEL,
    SPIN_RATE, SLOW_FACTOR, DT,
} from './physics.js';

// Дозы воздействий на один шаг (§12.25) — считаются из тех же констант
// раздела 6, что у классики. Сервер берёт эту же пару из game/physics.py.
export const SPEED_DROP_SLOW = 1.0 - SLOW_FACTOR;

// Те же числа, что на сервере. Расходиться им нельзя: с ними считается
// первый тик гонки и высота над полотном.
export const SETTLE_TICKS = 24;
export const DRIFT_MIN_SPEED = 4.0;
export const DRIFT_MIN_SLIP = 0.12;

// Зеркало game/rapier_host.PRESET_NAMES и server/config.PHYSICS_MODES.
export const PRESET_NAMES = ['arcade', 'sim'];

// Поля CarTuning, которые режет гандикап. Тот же список, что
// server/config.HANDICAP_STATS и что перечисляет applyHandicap() в main.js
// для предсказания классики; tools/test_sim.py сверяет копии одной проверкой.
export const HANDICAP_STATS = ['engine_force', 'max_speed', 'boost_speed'];

export function presetIndex(name) {
    const at = PRESET_NAMES.indexOf(String(name || '').toLowerCase());
    return at < 0 ? 0 : at;
}

/**
 * Правки CarTuning для машины каталога в выбранном режиме (§12.23).
 * Блок body общий для обоих режимов: кузов у машины один.
 */
export function carTuning(spec, mode, handicap, handicapFields) {
    // Награда за занос идёт ПЕРВОЙ и одинакова у всех машин: это правила
    // 6.3, а не свойство кузова. Зеркало car_tuning() из game/rapier_host.py.
    const stats = (spec && spec.stats) || null;
    const out = {
        drift_charge_l1: DRIFT_CHARGE_L1,
        drift_charge_l2: DRIFT_CHARGE_L2,
        drift_charge_l3: DRIFT_CHARGE_L3,
        drift_boost_l1: DRIFT_BOOST_L1,
        drift_boost_l2: DRIFT_BOOST_L2,
        drift_boost_l3: DRIFT_BOOST_L3,
        boost_accel: BOOST_ACCEL,
        boost_speed: stats ? (stats.boost_speed || 0) : 0,
    };
    const tuning = spec && spec.tuning;
    if (!tuning) return out;
    Object.assign(out, tuning.body || null);
    Object.assign(out, tuning[mode] || null);
    handicapTuning(out, handicap, handicapFields || HANDICAP_STATS);
    return out;
}

/**
 * Домножить поля гандикапа в ГОТОВОМ наборе настроек (§12.30).
 * Зеркало _handicap_tuning() из game/rapier_host.py, правило то же:
 * carTuning получает КАТАЛОЖНЫЕ числа и множитель и режет названные поля
 * сама. Заранее замедленные характеристики сюда не подают — boost_speed
 * (единственное поле не из блока tuning) порезался бы дважды.
 *
 * Резать надо именно тут: тяга и потолок скорости у модуля СВОИ, из блока
 * tuning, а stats.engine_force он не читает вовсе.
 */
export function handicapTuning(values, handicap, fields) {
    const k = +handicap;
    if (!(k > 0) || k === 1) return values;
    for (let i = 0; i < fields.length; i++) {
        const name = fields[i];
        if (name in values) values[name] = values[name] * k;
    }
    return values;
}

function shortAngle(from, to) {
    let d = to - from;
    const TAU = Math.PI * 2;
    while (d > Math.PI) d -= TAU;
    while (d < -Math.PI) d += TAU;
    return d;
}

/** Курс тела по кватерниону — ровно так же, как считает сам модуль. */
function yawOfQuat(qx, qy, qz, qw) {
    const fx = 2.0 * (qx * qz + qw * qy);
    const fz = 1.0 - 2.0 * (qx * qx + qy * qy);
    return Math.atan2(fx, fz);
}

export class RapierLocal {
    /** Загрузить модуль. Мир строится позже, в beginRace. */
    static async load(opts = {}) {
        return new RapierLocal(await RapierHost.load(opts));
    }

    constructor(host) {
        this.host = host;
        this.abi = host.abi;
        this.ready = false;       // мир под эту гонку построен
        this.idx = 0;
        this.restY = 0;           // высота центра кузова, когда колёса на полотне
        this.invSteerMax = 1;
        this.ring = null;         // кольцо состояний тела, параллельное кольцу net.js
        this.ringLen = 0;
        this.grid = null;         // куда поставлена машина на решётке
        this.level = 0;           // уровень заноса, выплаченный на прошлом шаге
        this.rampBoxes = 0;       // сколько коробок трамплинов стоит в мире
        // Гандикап своей машины (§12.30). Приезжает в race_init
        // players[].handicap и ложится ДО создания тела: перенастроить живое
        // тело модуль не умеет, CarTuning читается один раз, в spawn_car.
        this.handicap = 1;
        this.handicapStats = HANDICAP_STATS;
        this._begin = null;       // аргументы beginRace для пересборки
    }

    /**
     * Наложить гандикап и пересобрать мир (§12.30).
     *
     * Зовётся из main.js сразу после applyHandicap(): net.js строит мир по
     * race_init раньше, чем главный экран разберёт множитель, а ставить тело
     * дважды дешевле, чем городить порядок между двумя модулями. Пересборка
     * стоит одну сборку сетки полотна и случается только у победителя
     * прошлой гонки.
     */
    applyHandicap(factor) {
        const value = +factor;
        if (!(value > 0) || value === 1 || value === this.handicap) return false;
        this.handicap = value;
        const a = this._begin;
        if (!a) return false;
        this.beginRace(a[0], a[1], a[2], a[3], a[4], a[5]);
        return true;
    }

    /**
     * Построить мир под эту гонку: полотно, своя машина, осадка подвески.
     *
     * @param track     Track.fromServer — те же числа, что у сервера
     * @param spec      CarSpec своей машины (нужен только его tuning)
     * @param mode      'arcade' | 'sim' — настройка комнаты physics
     * @param gripMul   множитель сцепления по погоде: уезжает в трение сетки
     * @param ringLen   длина кольца предсказания net.js (INPUT_RING)
     * @param grid      {x, z, yaw} — место на решётке
     */
    beginRace(track, spec, mode, gripMul, ringLen, grid) {
        this._begin = [track, spec, mode, gripMul, ringLen, grid];
        const preset = presetIndex(mode);
        const host = this.host;
        this.ready = false;
        host.reset(preset);
        const p = meshParams(track);
        // Погода — единственное, чем правится покрытие: её множитель уезжает
        // в ТРЕНИЕ СЕТКИ. Сервер делает ровно то же в RapierRace.__init__,
        // и оба числа обязаны совпасть до бита, иначе предсказание поедет.
        const friction = p[2] * (gripMul || 1);
        host.buildTrack(track, p[0], p[1], friction);
        host.freeTrackMesh();
        // Трамплины — настоящая геометрия, неподвижные коробки (§12.30).
        // Числа приехали с сервера в race_init.track.ramps[].boxes, поэтому
        // у браузера и у сервера они одни и те же по построению.
        this.rampBoxes = host.addRamps(track, friction);
        host.tuningPreset(preset);
        host.setTuning(carTuning(spec, mode, this.handicap, this.handicapStats));
        const t = host.tuningValues();
        this.restY = t.wheel_radius + t.suspension_rest + t.half_height * 0.2;
        this.invSteerMax = 1 / (t.steer_max || 1);
        const surf = track.surface(grid.x, grid.z, 0);
        this.idx = host.spawnCar(grid.x, surf.y + this.restY, grid.z, grid.yaw);
        // §8.4 разведки: широкая фаза обновляется только внутри step, поэтому
        // на первом шаге лучи подвески не находят полотна. Холостые шаги
        // делает и сервер — ровно столько же и с тем же нулевым вводом.
        host.setInput(this.idx, 0, 0, 0, 0);
        host.step(SETTLE_TICKS);
        const floats = this.abi.CarSave.FLOATS;
        if (!this.ring || this.ringLen !== ringLen) {
            this.ring = new Float32Array(ringLen * floats);
            this.ringLen = ringLen;
        }
        this.grid = grid;
        this.ready = true;
    }

    // --- шаг ------------------------------------------------------------

    /**
     * Один шаг по битовой маске кнопок протокола.
     *
     * ``state`` — CarState своей машины: из него берётся флаг вне полотна и
     * таймеры бонусов. Таймеры тикают ЗДЕСЬ, а не в модуле, потому что при
     * откате их надо восстанавливать, а кольцо net.js их уже хранит
     * (SF_SPIN, SF_SLOW) — то же решение, что на сервере (§12.25).
     *
     * ``road`` — доза от происшествий на дороге (net.roadDose) либо null.
     * Считает её net.js: происшествия приезжают снапшотом и живут там же.
     */
    step(buttons, btn, state, road = null) {
        const host = this.host;
        host._sync();
        const a = this.abi.CarInput;
        const base = this.idx * a.FLOATS;
        const inputs = host.inputs;
        // Третий барьер 12.15 модулю снаружи: полотна он не знает. Флаг с
        // прошлого шага — сервер считает его той же surface() по той же позе.
        inputs[base + a.OFFTRACK] = state.offtrack ? 1 : 0;
        inputs[base + a.THROTTLE] = (buttons & btn.THROTTLE) ? 1 : 0;
        inputs[base + a.BRAKE] = (buttons & btn.BRAKE) ? 1 : 0;
        let steer = 0;
        if (buttons & btn.LEFT) steer += 1;
        if (buttons & btn.RIGHT) steer -= 1;
        inputs[base + a.STEER] = steer;
        inputs[base + a.HANDBRAKE] = (buttons & btn.DRIFT) ? 1 : 0;
        this._writeEffect(state, road);
        host.x.rp_step(1);
    }

    /**
     * Таймеры бонусов -> доза воздействия на этот шаг. Зеркало
     * RapierRace._write_effect из game/rapier_host.py, строка в строку:
     * всё, что влияет на движение, обязано считаться на обеих сторонах
     * одинаково, а раскрутка и «Гроза» движение меняют.
     *
     * Буста здесь нет: его остаток держит модуль, а снапшот приносит
     * только флаг. Продление по флагу делает applyAuthoritative — там же,
     * где классика делает свой HOLDOVER.
     */
    _writeEffect(state, road = null) {
        const host = this.host;
        const E = this.abi.CarEffect;
        const e = this.idx * E.FLOATS;
        const eff = host.effects;

        let spin = state.spinTime;
        if (spin > 0) {
            eff[e + E.SPIN_RATE] = SPIN_RATE;
            eff[e + E.STUN] = 1;
            eff[e + E.DRIFT_RESET] = 1;
            spin -= DT;
            state.spinTime = spin > 0 ? spin : 0;
        }
        let slow = state.slowTime;
        if (slow > 0) {
            eff[e + E.SPEED_DROP] = SPEED_DROP_SLOW;
            slow -= DT;
            state.slowTime = slow > 0 ? slow : 0;
        }
        let shield = state.shieldTime;
        if (shield > 0) {
            shield -= DT;
            state.shieldTime = shield > 0 ? shield : 0;
        }

        // Дорога (§12.28): [grip_drop, shift_x, shift_z, push_x, push_z,
        // speed_drop] — те же шесть чисел, что выписывает серверный
        // RapierRace._write_effect, и посчитаны они тем же кодом в net.js.
        if (road === null) return;
        if (road[0] > 0) eff[e + E.GRIP_DROP] = road[0];
        if (road[1] !== 0 || road[2] !== 0) {
            eff[e + E.SHIFT_X] = road[1];
            eff[e + E.SHIFT_Z] = road[2];
        }
        if (road[3] !== 0 || road[4] !== 0) {
            eff[e + E.PUSH_X] = road[3];
            eff[e + E.PUSH_Z] = road[4];
        }
        // Одно поле на «Грозу» и на обломки: берём БОЛЬШУЮ дозу, как сервер.
        if (road[5] > eff[e + E.SPEED_DROP]) eff[e + E.SPEED_DROP] = road[5];
    }

    /**
     * Поза из модуля -> CarState, затем шаг 16 из track.js.
     * Ровно то же, что делает RapierRace._read_all на сервере.
     */
    readInto(state, track) {
        const host = this.host;
        host._sync();
        const o = this.abi.CarOut;
        const out = host.outputs;
        const base = this.idx * o.FLOATS;
        const x = out[base + o.PX];
        const y = out[base + o.PY];
        const z = out[base + o.PZ];
        state.x = x;
        state.z = z;
        state.yaw = out[base + o.YAW];
        state.vx = out[base + o.VX];
        state.vz = out[base + o.VZ];
        state.vVert = out[base + o.VY];
        state.steer = out[base + o.WHEELS + this.abi.WheelOut.STEERING] * this.invSteerMax;
        state.driftCharge = out[base + o.DRIFT_CHARGE];
        state.driftDir = out[base + o.DRIFT_DIR] | 0;
        state.boostTime = out[base + o.BOOST_TIME];
        // Уровень, выплаченный НА ЭТОМ шаге; держится один шаг. Событие
        // drift_boost шлёт сервер — клиенту он нужен для звука и искр.
        this.level = out[base + o.DRIFT_LEVEL] | 0;
        const grounded = out[base + o.WHEELS_ON_GROUND];
        state.airborne = grounded === 0;
        const slip = Math.abs(out[base + o.SLIP_ANGLE]);
        state.driftActive = slip > DRIFT_MIN_SLIP
            && out[base + o.SPEED] > DRIFT_MIN_SPEED && grounded > 0;
        // Шаг 14 остался модулю: стены стоят в сетке полотна. Хозяину нужен
        // только флаг «вне полотна» — его читает HUD и звук.
        const surf = track.surface(x, z, state.sampleIdx);
        state.sampleIdx = surf.index;
        state.offtrack = surf.lateral > surf.halfWidth || surf.lateral < -surf.halfWidth;
        const h = y - surf.y - this.restY;
        state.height = h > 0 ? h : 0;
        // Шаг 16 — слово в слово прежний, в f64 и в track.js (§8.5 разведки).
        track.advanceProgress(state, surf.index);
    }

    // --- кольцо отката ----------------------------------------------------
    //
    // Состояние тела — 96 байт: поза, скорости, руль и заряд заноса
    // (§8.3 разведки). Контроллер колёс своего состояния не хранит, подвеска
    // целиком пересчитывается лучом на каждом шаге, поэтому клонировать мир
    // не надо. Откат при этом НЕ бит в бит: кэши разогрева узкой фазы не
    // восстанавливаются, и переигранная траектория отличается на ~1e-4 м.

    saveRing(slot) {
        const host = this.host;
        host.carSave(this.idx);
        host._sync();
        const floats = this.abi.CarSave.FLOATS;
        const src = this.idx * floats;
        const dst = slot * floats;
        for (let k = 0; k < floats; k++) this.ring[dst + k] = host.saves[src + k];
    }

    restoreRing(slot) {
        const host = this.host;
        host._sync();
        const floats = this.abi.CarSave.FLOATS;
        const src = slot * floats;
        const dst = this.idx * floats;
        for (let k = 0; k < floats; k++) host.saves[dst + k] = this.ring[src + k];
        host.carRestore(this.idx);
    }

    /**
     * Наложить авторитетную позу на восстановленное из кольца тело.
     *
     * Снапшот везёт x, z, yaw, vx, vz — и всё (раздел 5.3). Крена, тангажа,
     * вертикальной скорости и угловой скорости в нём нет, и выдумывать их
     * нельзя: подменить кватернион чистым поворотом вокруг Y значит
     * поставить машину плашмя посреди виража. Поэтому курс ДОВОРАЧИВАЕТСЯ
     * на разницу, а остальные оси остаются своими.
     *
     * Зовётся ПОСЛЕ restoreRing(slot): в saves уже лежит наше состояние на
     * этот тик, правится только то, что сервер действительно прислал.
     */
    applyAuthoritative(state) {
        const host = this.host;
        host._sync();
        const a = this.abi.CarSave;
        const saves = host.saves;
        const base = this.idx * a.FLOATS;
        const qx = saves[base + a.QX], qy = saves[base + a.QY];
        const qz = saves[base + a.QZ], qw = saves[base + a.QW];
        const d = shortAngle(yawOfQuat(qx, qy, qz, qw), state.yaw) * 0.5;
        const sn = Math.sin(d), cs = Math.cos(d);
        // q' = rotY(d) * q — доворот вокруг мировой вертикали.
        saves[base + a.QX] = cs * qx + sn * qz;
        saves[base + a.QY] = cs * qy + sn * qw;
        saves[base + a.QZ] = cs * qz - sn * qx;
        saves[base + a.QW] = cs * qw - sn * qy;
        saves[base + a.PX] = state.x;
        saves[base + a.PZ] = state.z;
        saves[base + a.VX] = state.vx;
        saves[base + a.VZ] = state.vz;
        // Остаток ускорения держит МОДУЛЬ, а снапшот везёт только флаг
        // (раздел 5.3). net.js уже подтянул state.boostTime по своему
        // HOLDOVER — тем же правилом, что у классики; здесь это число
        // кладётся туда, где буст на самом деле живёт. Без этой строки
        // подтяжка попадала бы в поле, которое readInto перезапишет из
        // модуля на следующем же шаге, то есть не делала бы ничего.
        saves[base + a.BOOST_TIME] = state.boostTime;
        host.carRestore(this.idx);
    }

    /**
     * Полная пересинхронизация: кольца нет, класть в тело нечего, кроме
     * того, что прислал сервер. Высота берётся от полотна — та самая, на
     * которой машина стоит колёсами.
     */
    teleport(state, track) {
        const host = this.host;
        host._sync();
        const a = this.abi.CarSave;
        const saves = host.saves;
        const base = this.idx * a.FLOATS;
        for (let k = 0; k < a.FLOATS; k++) saves[base + k] = 0;
        const surf = track.surface(state.x, state.z, state.sampleIdx);
        saves[base + a.PX] = state.x;
        saves[base + a.PY] = surf.y + this.restY;
        saves[base + a.PZ] = state.z;
        saves[base + a.QY] = Math.sin(state.yaw * 0.5);
        saves[base + a.QW] = Math.cos(state.yaw * 0.5);
        saves[base + a.VX] = state.vx;
        saves[base + a.VZ] = state.vz;
        host.carRestore(this.idx);
    }
}

export default RapierHost;
