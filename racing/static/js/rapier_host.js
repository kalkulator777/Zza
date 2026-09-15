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

// Порядок столбцов залитой осевой линии — 12.1.
const COLUMNS = ['x', 'y', 'z', 'tx', 'tz', 'nx', 'nz', 'hw', 's'];

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
        const n = track.count || track.x.length;
        if (n < 3) throw new HostError('осевая линия короче трёх точек');
        const offset = this.x.rp_track_alloc_centerline(n);
        if (offset === 0) throw new HostError('модуль не дал буфер под осевую линию');
        // Аллокация могла сдвинуть память: вид строим ПОСЛЕ неё.
        this._sync(true);
        const flat = new Float32Array(this.mem.buffer, offset, n * COLUMNS.length);
        for (let c = 0; c < COLUMNS.length; c++) {
            const column = track[COLUMNS[c]];
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

export default RapierHost;
