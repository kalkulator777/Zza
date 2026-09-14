// Геометрия трассы на клиенте — зеркало game/track.py.
//
// Владелец модуля: [track]. Ни одной зависимости, three.js здесь не нужен.
//
// nearestIndex / surface / clampToTrack / advanceProgress повторяют питоновские
// построчно: тот же порядок сравнений, те же окна поиска, те же константы.
// Сервер присылает числа, уже округлённые до 3 знаков, и они ложатся в
// Float32Array — ровно в те же значения, что держит у себя питон (он приводит
// свои массивы к float32 при сборке). Считают обе стороны в float64, поэтому
// результаты совпадают бит в бит, и реконсиляции не из-за чего срабатывать.
//
// Имена полей состояния — camelCase, как в static/js/physics.js:
// sampleIdx, offtrack, progress, lap, checkpoint.
//
// Аллокаций в горячих методах ноль: surface() заполняет заранее выделенный
// объект-приёмник и возвращает его же.

export const CHECKPOINT_COUNT = 12;

export const NARROW_WINDOW = 4;      // быстрое окно вокруг подсказки, точек
export const LOCAL_WINDOW = 16;      // широкое окно, точек (±32 м)
export const COARSE_STRIDE = 8;      // шаг грубого прохода при потере подсказки
export const LOST_DISTANCE = 24.0;   // дальше подсказка недействительна, м
const LOST_D2 = LOST_DISTANCE * LOST_DISTANCE;

export const WALL_MARGIN = 2.5;      // зона вылета за кромкой асфальта, м
export const WALL_BOUNCE = 0.35;     // отражение нормальной скорости (6.4)

const LAP_EPS = 1e-9;                // запас при floor(progress / length)

export class Track {
    /** Собрать трассу из объекта race_init.track (формат 12.1). */
    static fromServer(data) {
        return new Track(data);
    }

    constructor(data) {
        this.id = data.id;
        this.name = data.name;
        this.theme = data.theme;
        this.decorSeed = data.decor_seed;
        this.length = data.length;
        this.step = data.sample_step;
        this.invStep = 1.0 / this.step;
        this.count = data.count;
        this.halfLength = this.length * 0.5;
        this.quarterLength = this.length * 0.25;

        const n = this.count;
        this.cx = new Float32Array(data.x);
        this.cy = new Float32Array(data.y);
        this.cz = new Float32Array(data.z);
        this.ctx = new Float32Array(data.tx);
        this.ctz = new Float32Array(data.tz);
        this.cnx = new Float32Array(data.nx);
        this.cnz = new Float32Array(data.nz);
        this.chw = new Float32Array(data.hw);
        this.cs = new Float32Array(data.s);

        // Продольный уклон: на клиенте считается по присланным высотам той же
        // формулой, что на сервере, — отдельным массивом его не гоняют.
        this.cpitch = new Float64Array(n);
        const inv2 = 1.0 / (2.0 * this.step);
        for (let i = 0; i < n; i++) {
            const a = i > 0 ? i - 1 : n - 1;
            const b = i + 1 < n ? i + 1 : 0;
            this.cpitch[i] = Math.atan((this.cy[b] - this.cy[a]) * inv2);
        }

        this.checkpoints = Int32Array.from(data.checkpoints);
        this.cpS = new Float64Array(CHECKPOINT_COUNT);
        for (let k = 0; k < CHECKPOINT_COUNT; k++) {
            this.cpS[k] = this.cs[this.checkpoints[k]];
        }

        this.itemBoxes = data.item_boxes;
        this.startGrid = data.start_grid;

        // приёмник для surface(): один на трассу, новых объектов в кадре нет
        this.surf = { index: 0, lateral: 0.0, halfWidth: 0.0, y: 0.0, pitch: 0.0 };
    }

    /**
     * Локальный поиск ближайшей точки осевой линии вокруг hint.
     * Узкое окно -> широкое окно -> грубый проход по всей трассе. Последний
     * включается только при потерянной подсказке: респаун, телепорт, старт.
     */
    nearestIndex(x, z, hint) {
        const n = this.count;
        if (hint < 0 || hint >= n) {
            return this.globalIndex(x, z);
        }
        const cx = this.cx;
        const cz = this.cz;

        let best = hint;
        let bestD2 = 1e30;
        let bestOff = 0;
        for (let off = -NARROW_WINDOW; off <= NARROW_WINDOW; off++) {
            let j = hint + off;
            if (j < 0) { j += n; } else if (j >= n) { j -= n; }
            const dx = x - cx[j];
            const dz = z - cz[j];
            const d2 = dx * dx + dz * dz;
            if (d2 < bestD2) { bestD2 = d2; best = j; bestOff = off; }
        }
        if (bestOff > -NARROW_WINDOW && bestOff < NARROW_WINDOW && bestD2 <= LOST_D2) {
            return best;
        }

        bestD2 = 1e30;
        for (let off = -LOCAL_WINDOW; off <= LOCAL_WINDOW; off++) {
            let j = hint + off;
            if (j < 0) { j += n; } else if (j >= n) { j -= n; }
            const dx = x - cx[j];
            const dz = z - cz[j];
            const d2 = dx * dx + dz * dz;
            if (d2 < bestD2) { bestD2 = d2; best = j; bestOff = off; }
        }
        if (bestOff <= -LOCAL_WINDOW || bestOff >= LOCAL_WINDOW || bestD2 > LOST_D2) {
            return this.globalIndex(x, z);
        }
        return best;
    }

    /** Грубый проход по всей трассе плюс уточнение. Подсказка потеряна. */
    globalIndex(x, z) {
        const n = this.count;
        const cx = this.cx;
        const cz = this.cz;
        let best = 0;
        let bestD2 = 1e30;
        for (let j = 0; j < n; j += COARSE_STRIDE) {
            const dx = x - cx[j];
            const dz = z - cz[j];
            const d2 = dx * dx + dz * dz;
            if (d2 < bestD2) { bestD2 = d2; best = j; }
        }
        const coarse = best;
        for (let off = -COARSE_STRIDE; off <= COARSE_STRIDE; off++) {
            let j = coarse + off;
            if (j < 0) { j += n; } else if (j >= n) { j -= n; }
            const dx = x - cx[j];
            const dz = z - cz[j];
            const d2 = dx * dx + dz * dz;
            if (d2 < bestD2) { bestD2 = d2; best = j; }
        }
        return best;
    }

    /**
     * Заполняет и возвращает this.surf: index, lateral, halfWidth, y, pitch.
     * lateral — знаковое смещение от оси, положительное вправо.
     * Новых объектов не создаётся, приёмник переиспользуется.
     */
    surface(x, z, hint) {
        const i = this.nearestIndex(x, z, hint);
        const dx = x - this.cx[i];
        const dz = z - this.cz[i];
        const lateral = dx * this.cnx[i] + dz * this.cnz[i];
        const along = dx * this.ctx[i] + dz * this.ctz[i];
        const n = this.count;
        let j;
        let f;
        if (along >= 0.0) {
            j = i + 1 < n ? i + 1 : 0;
            f = along * this.invStep;
        } else {
            j = i > 0 ? i - 1 : n - 1;
            f = -along * this.invStep;
        }
        if (f > 1.0) { f = 1.0; }
        const out = this.surf;
        out.index = i;
        out.lateral = lateral;
        out.y = this.cy[i] + (this.cy[j] - this.cy[i]) * f;
        out.halfWidth = this.chw[i] + (this.chw[j] - this.chw[i]) * f;
        out.pitch = this.cpitch[i] + (this.cpitch[j] - this.cpitch[i]) * f;
        return out;
    }

    /**
     * Шаг 14 физики: выталкивание из стены, гашение скорости.
     * Полотно halfWidth — асфальт, дальше зона вылета шириной WALL_MARGIN
     * (там уже стоит offtrack, его читает шаг 11), за ней — жёсткая стена.
     * Обновляет: x, z, vx, vz, sampleIdx, offtrack.
     */
    clampToTrack(state, hint) {
        const s = this.surface(state.x, state.z, hint);
        const i = s.index;
        const lateral = s.lateral;
        const halfWidth = s.halfWidth;
        state.sampleIdx = i;
        state.offtrack = lateral > halfWidth || lateral < -halfWidth;
        const limit = halfWidth + WALL_MARGIN;
        let over = 0.0;
        if (lateral > limit) {
            over = lateral - limit;
        } else if (lateral < -limit) {
            over = lateral + limit;
        }
        if (over === 0.0) {
            return;
        }
        const nx = this.cnx[i];
        const nz = this.cnz[i];
        state.x -= nx * over;
        state.z -= nz * over;
        const vn = state.vx * nx + state.vz * nz;
        if ((over > 0.0 && vn > 0.0) || (over < 0.0 && vn < 0.0)) {
            const k = vn * (1.0 + WALL_BOUNCE);
            state.vx -= nx * k;
            state.vz -= nz * k;
        }
    }

    /**
     * Шаг 16 физики: накопление пути, отсечки, круги (раздел 7.4).
     * Обновляет: sampleIdx, progress, checkpoint, lap. Прошлое положение по
     * дуге отдельным полем не хранится: progress всегда равен s + круги*length,
     * значит остаток по модулю длины круга и есть прошлое s.
     */
    advanceProgress(state, hint) {
        const i = this.nearestIndex(state.x, state.z, hint);
        state.sampleIdx = i;
        const dx = state.x - this.cx[i];
        const dz = state.z - this.cz[i];
        let along = dx * this.ctx[i] + dz * this.ctz[i];
        const step = this.step;
        if (along > step) { along = step; } else if (along < -step) { along = -step; }
        const length = this.length;
        let sNew = this.cs[i] + along;
        if (sNew >= length) { sNew -= length; } else if (sNew < 0.0) { sNew += length; }

        let progress = state.progress;
        const lapsDone = Math.floor(progress / length);
        let last = progress - lapsDone * length;

        let ds = sNew - last;
        if (ds > this.halfLength) {
            ds -= length;
        } else if (ds < -this.halfLength) {
            ds += length;
        }

        if (ds > this.quarterLength || ds < -this.quarterLength) {
            // Телепорт: путь не засчитываем, точку отсчёта подтягиваем назад,
            // но никогда вперёд — прыжок через газон не должен быть выгоден.
            let target = sNew + lapsDone * length;
            if (target > progress) { target -= length; }
            state.progress = target;
            return;
        }

        progress += ds;
        state.progress = progress;
        const cpS = this.cpS;
        let cp = state.checkpoint;

        if (ds > 0.0) {
            let remaining = ds;
            for (let k = 0; k < CHECKPOINT_COUNT; k++) {
                const target = cpS[cp];
                let gap = target - last;
                if (gap < 0.0) { gap += length; }
                if (gap > remaining) { break; }
                last = target;
                remaining -= gap;
                if (cp === 0) {
                    // линия старта пройдена после всех одиннадцати отсечек
                    state.lap = Math.floor(progress / length + LAP_EPS);
                }
                cp = cp + 1 < CHECKPOINT_COUNT ? cp + 1 : 0;
            }
        } else if (ds < 0.0) {
            let remaining = -ds;
            for (let k = 0; k < CHECKPOINT_COUNT; k++) {
                const prevCp = cp > 0 ? cp - 1 : CHECKPOINT_COUNT - 1;
                const target = cpS[prevCp];
                let gap = last - target;
                if (gap < 0.0) { gap += length; }
                if (gap > remaining) { break; }
                last = target;
                remaining -= gap;
                cp = prevCp;
                if (prevCp === 0 && state.lap > 0) {
                    state.lap -= 1;      // откат через линию задним ходом
                }
            }
        }

        state.checkpoint = cp;
    }

    /**
     * Первичная привязка машины к трассе: стартовая решётка, начало гонки.
     * Решётка стоит позади линии, поэтому progress отрицателен — пересечение
     * линии даёт ровно ноль. Для респауна по ходу гонки звать НЕ надо.
     */
    initState(state) {
        const i = this.globalIndex(state.x, state.z);
        const dx = state.x - this.cx[i];
        const dz = state.z - this.cz[i];
        let along = dx * this.ctx[i] + dz * this.ctz[i];
        const step = this.step;
        if (along > step) { along = step; } else if (along < -step) { along = -step; }
        let sPos = this.cs[i] + along;
        if (sPos >= this.length) {
            sPos -= this.length;
        } else if (sPos < 0.0) {
            sPos += this.length;
        }
        state.sampleIdx = i;
        state.progress = sPos > this.halfLength ? sPos - this.length : sPos;
        state.checkpoint = 0;
        state.lap = 0;
        state.offtrack = false;
    }
}

export default Track;
