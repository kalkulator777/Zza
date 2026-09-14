/**
 * geomutil.js — низкоуровневые помощники построения геометрии для модуля [render].
 *
 * ЭТОГО ФАЙЛА НЕТ В ДЕРЕВЕ ФАЙЛОВ DESIGN.md (раздел 3). Он согласован отдельно
 * как ВНУТРЕННИЙ помощник рендера: его импортируют только trackmesh.js,
 * carmesh.js и scenery.js. Точкой стыка с другими исполнителями он не является,
 * никакой модуль вне js/render/ на него ссылаться не должен.
 *
 * Зачем он нужен:
 *  - BufferGeometryUtils вендорить запрещено (10.3), а слияние геометрий нужно
 *    всем трём файлам рендера — здесь лежит собственная реализация ровно под
 *    наш случай: неиндексированные геометрии, атрибуты position/normal/color;
 *  - построение полос (ribbon), сеток поверхностей и экструзии профиля вдоль
 *    пути — общий код полотна трассы, бордюров, разметки и рельефа;
 *  - детерминированный ГПСЧ (mulberry32) — декор обязан строиться одинаково
 *    на всех клиентах при одном и том же decor_seed (7.1).
 *
 * Всё здесь вызывается один раз при загрузке гонки, поэтому аллокации внутри
 * допустимы. В кадровом цикле ничего из этого файла вызываться не должно.
 */

import * as THREE from 'three';

// ============================================================================
// Детерминированная случайность
// ============================================================================

/**
 * mulberry32 — быстрый ГПСЧ с 32-битным состоянием.
 * Одно и то же целое зерно даёт одну и ту же последовательность в любом
 * браузере: все операции целочисленные, float появляется только в конце.
 */
export function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
        a = (a + 0x6d2b79f5) >>> 0;
        let t = a;
        t = Math.imul(t ^ (t >>> 15), t | 1);
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

/** Обёртка над mulberry32 с удобными методами. */
export class Rng {
    constructor(seed) {
        this.seed = seed >>> 0;
        this._next = mulberry32(this.seed);
    }
    /** [0, 1) */
    next() {
        return this._next();
    }
    /** [a, b) */
    range(a, b) {
        return a + (b - a) * this._next();
    }
    /** целое из [a, b] включительно */
    int(a, b) {
        return a + Math.floor(this._next() * (b - a + 1));
    }
    /** случайный элемент массива */
    pick(arr) {
        return arr[Math.floor(this._next() * arr.length) % arr.length];
    }
    /** true с вероятностью p */
    chance(p) {
        return this._next() < p;
    }
    /** -1 или +1 */
    sign() {
        return this._next() < 0.5 ? -1 : 1;
    }
    /** симметричный разброс [-d, +d] */
    spread(d) {
        return (this._next() * 2 - 1) * d;
    }
}

/**
 * Периодический шум на отрезке [0, 1): сумма гармоник со случайными фазами.
 * Периодичность гарантирует, что замкнутая трасса сходится без шва.
 * harmonics — целые номера гармоник, amps — их амплитуды.
 */
export function loopNoise(rng, harmonics, amps) {
    const n = harmonics.length;
    const k = new Float64Array(n);
    const a = new Float64Array(n);
    const ph = new Float64Array(n);
    for (let i = 0; i < n; i++) {
        k[i] = harmonics[i] | 0;
        a[i] = amps[i];
        ph[i] = rng.next() * Math.PI * 2;
    }
    return function (t) {
        let s = 0;
        for (let i = 0; i < n; i++) s += a[i] * Math.sin(Math.PI * 2 * k[i] * t + ph[i]);
        return s;
    };
}

// ============================================================================
// Цвета
// ============================================================================

/**
 * Приводит что угодно (строка, число, THREE.Color) к THREE.Color.
 * Важно: THREE.Color сам переводит sRGB в рабочее (линейное) пространство,
 * а вершинные цвета шейдер ожидает уже в рабочем — поэтому везде работаем
 * через THREE.Color и никогда не кладём hex в атрибут напрямую.
 */
export function toColor(value, out) {
    const c = out || new THREE.Color();
    if (value instanceof THREE.Color) return c.copy(value);
    if (typeof value === 'number') return c.setHex(value);
    if (typeof value === 'string') return c.set(value);
    return c.setRGB(1, 1, 1);
}

/** out = base * k, с ограничением сверху. Дешёвая вариация оттенка. */
export function shade(out, base, k) {
    out.setRGB(Math.min(1, base.r * k), Math.min(1, base.g * k), Math.min(1, base.b * k));
    return out;
}

/** Линейная смесь двух цветов. */
export function mixColor(out, a, b, t) {
    out.setRGB(a.r + (b.r - a.r) * t, a.g + (b.g - a.g) * t, a.b + (b.b - a.b) * t);
    return out;
}

// ============================================================================
// MeshBuilder — накопитель треугольников
// ============================================================================

/**
 * Накопитель неиндексированной геометрии: позиции, нормали, вершинные цвета.
 * Плюс необязательный «флаг» на вершину (this.flag) — им carmesh.js помечает
 * перекрашиваемые панели кузова, чтобы потом менять цвет игрока, не
 * пересобирая геометрию.
 */
export class MeshBuilder {
    constructor() {
        this.pos = [];
        this.nrm = [];
        this.col = [];
        this.flags = [];
        this.flag = 0;
    }

    get vertexCount() {
        return this.pos.length / 3;
    }
    get triangleCount() {
        return this.pos.length / 9;
    }

    _push(x, y, z, nx, ny, nz, r, g, b) {
        this.pos.push(x, y, z);
        this.nrm.push(nx, ny, nz);
        this.col.push(r, g, b);
        this.flags.push(this.flag);
    }

    /** Треугольник по числам, нормаль считается по обходу (против часовой — лицевая). */
    triRaw(ax, ay, az, bx, by, bz, cx, cy, cz, r, g, b) {
        const ux = bx - ax,
            uy = by - ay,
            uz = bz - az;
        const vx = cx - ax,
            vy = cy - ay,
            vz = cz - az;
        let nx = uy * vz - uz * vy;
        let ny = uz * vx - ux * vz;
        let nz = ux * vy - uy * vx;
        const l = Math.sqrt(nx * nx + ny * ny + nz * nz) || 1;
        nx /= l;
        ny /= l;
        nz /= l;
        this._push(ax, ay, az, nx, ny, nz, r, g, b);
        this._push(bx, by, bz, nx, ny, nz, r, g, b);
        this._push(cx, cy, cz, nx, ny, nz, r, g, b);
    }

    /** Треугольник по трём точкам-массивам [x,y,z] и цвету THREE.Color. */
    tri(a, b, c, color) {
        this.triRaw(a[0], a[1], a[2], b[0], b[1], b[2], c[0], c[1], c[2], color.r, color.g, color.b);
    }

    /** Четырёхугольник a-b-c-d (обход против часовой со стороны лицевой грани). */
    quad(a, b, c, d, color) {
        this.tri(a, b, c, color);
        this.tri(a, c, d, color);
    }

    /** Тот же квад, но числами — для горячих мест построения полос. */
    quadRaw(ax, ay, az, bx, by, bz, cx, cy, cz, dx, dy, dz, r, g, b) {
        this.triRaw(ax, ay, az, bx, by, bz, cx, cy, cz, r, g, b);
        this.triRaw(ax, ay, az, cx, cy, cz, dx, dy, dz, r, g, b);
    }

    /**
     * Треугольник с ОТДЕЛЬНЫМ цветом на каждую вершину.
     *
     * flatShading влияет только на нормали: вершинные цвета шейдер
     * интерполирует по грани в любом случае. Это и есть носитель запечённого
     * затенения — плавный тёмный контакт получается без единого лишнего
     * треугольника.
     */
    triVC(a, b, c, ca, cb, cc) {
        const ux = b[0] - a[0],
            uy = b[1] - a[1],
            uz = b[2] - a[2];
        const vx = c[0] - a[0],
            vy = c[1] - a[1],
            vz = c[2] - a[2];
        let nx = uy * vz - uz * vy;
        let ny = uz * vx - ux * vz;
        let nz = ux * vy - uy * vx;
        const l = Math.sqrt(nx * nx + ny * ny + nz * nz) || 1;
        nx /= l;
        ny /= l;
        nz /= l;
        this._push(a[0], a[1], a[2], nx, ny, nz, ca.r, ca.g, ca.b);
        this._push(b[0], b[1], b[2], nx, ny, nz, cb.r, cb.g, cb.b);
        this._push(c[0], c[1], c[2], nx, ny, nz, cc.r, cc.g, cc.b);
    }

    /** Четырёхугольник с отдельным цветом на каждую вершину. */
    quadVC(a, b, c, d, ca, cb, cc, cd) {
        this.triVC(a, b, c, ca, cb, cc);
        this.triVC(a, c, d, ca, cc, cd);
    }

    /** Выпуклый многоугольник веером. reverse — обойти в обратную сторону. */
    polygon(points, color, reverse) {
        const n = points.length;
        if (n < 3) return;
        for (let i = 1; i < n - 1; i++) {
            if (reverse) this.tri(points[0], points[i + 1], points[i], color);
            else this.tri(points[0], points[i], points[i + 1], color);
        }
    }

    /**
     * Параллелепипед по центру и размерам. colors — либо один цвет,
     * либо объект {top, bottom, side} для разной покраски граней.
     */
    box(cx, cy, cz, sx, sy, sz, color, colorTop) {
        const x0 = cx - sx * 0.5,
            x1 = cx + sx * 0.5;
        const y0 = cy - sy * 0.5,
            y1 = cy + sy * 0.5;
        const z0 = cz - sz * 0.5,
            z1 = cz + sz * 0.5;
        const top = colorTop || color;
        // верх
        this.quadRaw(x0, y1, z0, x0, y1, z1, x1, y1, z1, x1, y1, z0, top.r, top.g, top.b);
        // низ
        this.quadRaw(x0, y0, z0, x1, y0, z0, x1, y0, z1, x0, y0, z1, color.r, color.g, color.b);
        // перед (+z)
        this.quadRaw(x0, y0, z1, x1, y0, z1, x1, y1, z1, x0, y1, z1, color.r, color.g, color.b);
        // зад (-z)
        this.quadRaw(x1, y0, z0, x0, y0, z0, x0, y1, z0, x1, y1, z0, color.r, color.g, color.b);
        // право (+x)
        this.quadRaw(x1, y0, z1, x1, y0, z0, x1, y1, z0, x1, y1, z1, color.r, color.g, color.b);
        // лево (-x)
        this.quadRaw(x0, y0, z0, x0, y0, z1, x0, y1, z1, x0, y1, z0, color.r, color.g, color.b);
    }

    /** Собирает BufferGeometry. Геометрия неиндексированная — так требует flatShading. */
    build() {
        const g = new THREE.BufferGeometry();
        g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(this.pos), 3));
        g.setAttribute('normal', new THREE.BufferAttribute(new Float32Array(this.nrm), 3));
        g.setAttribute('color', new THREE.BufferAttribute(new Float32Array(this.col), 3));
        g.computeBoundingSphere();
        g.computeBoundingBox();
        return g;
    }

    /** Массив флагов вершин (см. this.flag). */
    buildFlags() {
        return new Uint8Array(this.flags);
    }
}

// ============================================================================
// Слияние геометрий (замена BufferGeometryUtils.mergeGeometries)
// ============================================================================

const _mat3 = new THREE.Matrix3();

/**
 * Сливает список BufferGeometry в одну неиндексированную с атрибутами
 * position / normal / color.
 *  - индексированные входы разворачиваются;
 *  - отсутствующие нормали считаются по граням;
 *  - отсутствующие цвета заполняются белым;
 *  - лишние атрибуты (uv и прочее) отбрасываются;
 *  - matrices[i] (если задан) применяется к позициям и нормалям i-й геометрии.
 * Входные геометрии не изменяются и остаются на совести вызывающего.
 */
export function mergeGeometries(geometries, matrices) {
    const parts = [];
    let total = 0;
    for (let i = 0; i < geometries.length; i++) {
        const src = geometries[i];
        if (!src) continue;
        const g = src.index ? src.toNonIndexed() : src;
        const p = g.attributes.position;
        if (!p || p.count === 0) continue;
        parts.push({ geom: g, temp: g !== src, matrix: matrices ? matrices[i] : null });
        total += p.count;
    }

    const pos = new Float32Array(total * 3);
    const nrm = new Float32Array(total * 3);
    const col = new Float32Array(total * 3);
    let off = 0;

    for (let pi = 0; pi < parts.length; pi++) {
        const { geom, matrix } = parts[pi];
        const p = geom.attributes.position;
        const n = geom.attributes.normal;
        const c = geom.attributes.color;
        const count = p.count;
        const base = off * 3;

        pos.set(p.array.subarray ? p.array.subarray(0, count * 3) : p.array, base);

        if (n) {
            nrm.set(n.array.subarray ? n.array.subarray(0, count * 3) : n.array, base);
        } else {
            // считаем нормали по граням прямо на месте
            for (let v = 0; v < count; v += 3) {
                const i0 = base + v * 3;
                const ax = pos[i0],
                    ay = pos[i0 + 1],
                    az = pos[i0 + 2];
                const bx = pos[i0 + 3],
                    by = pos[i0 + 4],
                    bz = pos[i0 + 5];
                const cx = pos[i0 + 6],
                    cy = pos[i0 + 7],
                    cz = pos[i0 + 8];
                let nx = (by - ay) * (cz - az) - (bz - az) * (cy - ay);
                let ny = (bz - az) * (cx - ax) - (bx - ax) * (cz - az);
                let nz = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
                const l = Math.sqrt(nx * nx + ny * ny + nz * nz) || 1;
                nx /= l;
                ny /= l;
                nz /= l;
                for (let k = 0; k < 3; k++) {
                    nrm[i0 + k * 3] = nx;
                    nrm[i0 + k * 3 + 1] = ny;
                    nrm[i0 + k * 3 + 2] = nz;
                }
            }
        }

        if (c) {
            col.set(c.array.subarray ? c.array.subarray(0, count * 3) : c.array, base);
        } else {
            col.fill(1, base, base + count * 3);
        }

        if (matrix) {
            const e = matrix.elements;
            _mat3.getNormalMatrix(matrix);
            const ne = _mat3.elements;
            for (let v = 0; v < count; v++) {
                const i0 = base + v * 3;
                const x = pos[i0],
                    y = pos[i0 + 1],
                    z = pos[i0 + 2];
                pos[i0] = e[0] * x + e[4] * y + e[8] * z + e[12];
                pos[i0 + 1] = e[1] * x + e[5] * y + e[9] * z + e[13];
                pos[i0 + 2] = e[2] * x + e[6] * y + e[10] * z + e[14];
                const nx = nrm[i0],
                    ny = nrm[i0 + 1],
                    nz = nrm[i0 + 2];
                let rx = ne[0] * nx + ne[3] * ny + ne[6] * nz;
                let ry = ne[1] * nx + ne[4] * ny + ne[7] * nz;
                let rz = ne[2] * nx + ne[5] * ny + ne[8] * nz;
                const l = Math.sqrt(rx * rx + ry * ry + rz * rz) || 1;
                nrm[i0] = rx / l;
                nrm[i0 + 1] = ry / l;
                nrm[i0 + 2] = rz / l;
            }
        }

        off += count;
    }

    const out = new THREE.BufferGeometry();
    out.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    out.setAttribute('normal', new THREE.BufferAttribute(nrm, 3));
    out.setAttribute('color', new THREE.BufferAttribute(col, 3));
    out.computeBoundingSphere();
    out.computeBoundingBox();
    return out;
}

// ============================================================================
// Примитивы с вершинными цветами
// ============================================================================

/**
 * Приводит любую геометрию three.js к нашему виду: неиндексированная,
 * только position/normal/color, покрашена одним цветом или функцией
 * paint(x, y, z, index, outColor).
 */
export function solidify(geometry, color, paint) {
    const g = geometry.index ? geometry.toNonIndexed() : geometry.clone();
    for (const name of Object.keys(g.attributes)) {
        if (name !== 'position' && name !== 'normal') g.deleteAttribute(name);
    }
    if (!g.attributes.normal) g.computeVertexNormals();
    const p = g.attributes.position;
    const n = p.count;
    const col = new Float32Array(n * 3);
    const tmp = new THREE.Color();
    for (let i = 0; i < n; i++) {
        if (paint) {
            tmp.copy(color);
            paint(p.getX(i), p.getY(i), p.getZ(i), i, tmp);
        } else {
            tmp.copy(color);
        }
        col[i * 3] = tmp.r;
        col[i * 3 + 1] = tmp.g;
        col[i * 3 + 2] = tmp.b;
    }
    g.setAttribute('color', new THREE.BufferAttribute(col, 3));
    return g;
}

/** Матрица переноса/поворота/масштаба — для сборки составных примитивов. */
export function trs(x, y, z, ry, sx, sy, sz) {
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), ry || 0);
    m.compose(
        new THREE.Vector3(x, y, z),
        q,
        new THREE.Vector3(sx === undefined ? 1 : sx, sy === undefined ? 1 : sy, sz === undefined ? 1 : sz)
    );
    return m;
}

/** Коробка как отдельная геометрия (для сборки составных инстансов). */
export function primBox(sx, sy, sz, color, x, y, z, ry) {
    const g = solidify(new THREE.BoxGeometry(sx, sy, sz), color);
    if (x || y || z || ry) g.applyMatrix4(trs(x || 0, y || 0, z || 0, ry || 0));
    return g;
}

/** Цилиндр/конус (rTop = 0 даёт конус), ось Y. */
export function primCyl(rTop, rBot, h, seg, color, x, y, z, ry) {
    const g = solidify(new THREE.CylinderGeometry(rTop, rBot, h, seg, 1, false), color);
    if (x || y || z || ry) g.applyMatrix4(trs(x || 0, y || 0, z || 0, ry || 0));
    return g;
}

/** Гранёный «камень»/сфера. */
export function primPoly(radius, detail, color, x, y, z) {
    const g = solidify(new THREE.IcosahedronGeometry(radius, detail || 0), color);
    if (x || y || z) g.applyMatrix4(trs(x || 0, y || 0, z || 0, 0));
    return g;
}

// ============================================================================
// Полосы, сетки поверхностей, экструзия профиля
// ============================================================================

/**
 * Сетка поверхности: rows точек вдоль пути на cols точек поперёк.
 * point(i, j, out3) заполняет out3 координатами узла,
 * color(i, j, outColor) задаёт цвет квада [i..i+1] x [j..j+1].
 * vertexColor(i, j, outColor) — цвет УЗЛА (i, j); если задан, он главнее
 * color и даёт плавный градиент поперёк и вдоль полосы: именно так в полотно
 * и в рельеф запекается затенение у кромок.
 * closed — замкнуть последнюю строку с первой, flip — вывернуть лицевую сторону.
 *
 * Обход квада (i,j) -> (i+1,j) -> (i+1,j+1) -> (i,j+1) даёт нормаль вверх,
 * если i растёт «вперёд», а j — «вправо».
 */
export function surfaceGrid(builder, opts) {
    const rows = opts.rows;
    const cols = opts.cols;
    const closed = !!opts.closed;
    const flip = !!opts.flip;
    const point = opts.point;
    const vertexColor = opts.vertexColor;
    const color = opts.color || function (i, j, c) { c.setRGB(1, 1, 1); };
    const tmp = new THREE.Color(1, 1, 1);
    const out = [0, 0, 0];
    // четыре угла квада в вершинных цветах: объекты переиспользуются
    const vc00 = new THREE.Color(1, 1, 1);
    const vc10 = new THREE.Color(1, 1, 1);
    const vc11 = new THREE.Color(1, 1, 1);
    const vc01 = new THREE.Color(1, 1, 1);
    const pa = [0, 0, 0],
        pb = [0, 0, 0],
        pc = [0, 0, 0],
        pd = [0, 0, 0];

    let cur = new Float32Array(cols * 3);
    let nxt = new Float32Array(cols * 3);

    for (let j = 0; j < cols; j++) {
        point(0, j, out);
        cur[j * 3] = out[0];
        cur[j * 3 + 1] = out[1];
        cur[j * 3 + 2] = out[2];
    }

    const last = closed ? rows : rows - 1;
    for (let i = 0; i < last; i++) {
        const i2 = closed && i === rows - 1 ? 0 : i + 1;
        for (let j = 0; j < cols; j++) {
            point(i2, j, out);
            nxt[j * 3] = out[0];
            nxt[j * 3 + 1] = out[1];
            nxt[j * 3 + 2] = out[2];
        }
        for (let j = 0; j < cols - 1; j++) {
            const a = j * 3,
                b = a + 3;
            if (vertexColor) {
                // (i, j) -> vc00, (i+1, j) -> vc10, (i+1, j+1) -> vc11, (i, j+1) -> vc01
                vertexColor(i, j, vc00);
                vertexColor(i2, j, vc10);
                vertexColor(i2, j + 1, vc11);
                vertexColor(i, j + 1, vc01);
                pa[0] = cur[a]; pa[1] = cur[a + 1]; pa[2] = cur[a + 2];
                pb[0] = nxt[a]; pb[1] = nxt[a + 1]; pb[2] = nxt[a + 2];
                pc[0] = nxt[b]; pc[1] = nxt[b + 1]; pc[2] = nxt[b + 2];
                pd[0] = cur[b]; pd[1] = cur[b + 1]; pd[2] = cur[b + 2];
                if (flip) builder.quadVC(pa, pd, pc, pb, vc00, vc01, vc11, vc10);
                else builder.quadVC(pa, pb, pc, pd, vc00, vc10, vc11, vc01);
                continue;
            }
            color(i, j, tmp);
            if (flip) {
                builder.quadRaw(
                    cur[a], cur[a + 1], cur[a + 2],
                    cur[b], cur[b + 1], cur[b + 2],
                    nxt[b], nxt[b + 1], nxt[b + 2],
                    nxt[a], nxt[a + 1], nxt[a + 2],
                    tmp.r, tmp.g, tmp.b
                );
            } else {
                builder.quadRaw(
                    cur[a], cur[a + 1], cur[a + 2],
                    nxt[a], nxt[a + 1], nxt[a + 2],
                    nxt[b], nxt[b + 1], nxt[b + 2],
                    cur[b], cur[b + 1], cur[b + 2],
                    tmp.r, tmp.g, tmp.b
                );
            }
        }
        const swap = cur;
        cur = nxt;
        nxt = swap;
    }
}

/**
 * Полоса (ribbon) по двум наборам точек одинаковой длины.
 * left/right — массивы [x,y,z] либо плоские Float32Array по три числа.
 * color(i, outColor) красит i-й квад.
 */
export function ribbonStrip(builder, left, right, opts) {
    const o = opts || {};
    const flat = !Array.isArray(left[0]);
    const n = flat ? left.length / 3 : left.length;
    const get = flat
        ? function (arr, i, out) {
              out[0] = arr[i * 3];
              out[1] = arr[i * 3 + 1];
              out[2] = arr[i * 3 + 2];
          }
        : function (arr, i, out) {
              const p = arr[i];
              out[0] = p[0];
              out[1] = p[1];
              out[2] = p[2];
          };
    const l = [0, 0, 0],
        r = [0, 0, 0];
    surfaceGrid(builder, {
        rows: n,
        cols: 2,
        closed: !!o.closed,
        flip: !!o.flip,
        point: function (i, j, out) {
            if (j === 0) {
                get(left, i, l);
                out[0] = l[0];
                out[1] = l[1];
                out[2] = l[2];
            } else {
                get(right, i, r);
                out[0] = r[0];
                out[1] = r[1];
                out[2] = r[2];
            }
        },
        color: o.color,
        vertexColor: o.vertexColor
    });
}

/**
 * Экструзия плоского профиля вдоль пути.
 * frame(i, out9) заполняет [ox,oy,oz, rx,ry,rz, ux,uy,uz] — начало координат
 * сечения, орт «вправо» и орт «вверх».
 * profile — массив пар [u, v] в этих ортах.
 * color(i, k, outColor) красит k-ю грань между профильными точками k и k+1.
 */
export function extrudeProfile(builder, opts) {
    const count = opts.count;
    const profile = opts.profile;
    const frame = opts.frame;
    const f = new Float64Array(9);
    surfaceGrid(builder, {
        rows: count,
        cols: profile.length,
        closed: !!opts.closed,
        flip: !!opts.flip,
        point: function (i, j, out) {
            frame(i, f);
            const u = profile[j][0],
                v = profile[j][1];
            out[0] = f[0] + f[3] * u + f[6] * v;
            out[1] = f[1] + f[4] * u + f[7] * v;
            out[2] = f[2] + f[5] * u + f[8] * v;
        },
        color: opts.color,
        vertexColor: opts.vertexColor
    });
}

/**
 * Лофт замкнутых сечений вдоль оси Z (для кузовов машин).
 * sections — массив сечений, каждое: { z, pts: [[x,y], ...] } одинаковой длины,
 * точки обходятся по часовой при взгляде спереди (из +Z) — тогда нормали наружу.
 * color(i, k, outColor) красит грань между сечениями i, i+1 на ребре k.
 * capStart / capEnd — закрыть торцы.
 */
export function loft(builder, sections, opts) {
    const o = opts || {};
    const color = o.color;
    const tmp = new THREE.Color(1, 1, 1);
    const m = sections[0].pts.length;
    const a = [0, 0, 0],
        b = [0, 0, 0],
        c = [0, 0, 0],
        d = [0, 0, 0];

    for (let i = 0; i < sections.length - 1; i++) {
        const s0 = sections[i],
            s1 = sections[i + 1];
        for (let k = 0; k < m; k++) {
            const k2 = (k + 1) % m;
            a[0] = s0.pts[k][0];
            a[1] = s0.pts[k][1];
            a[2] = s0.z;
            b[0] = s1.pts[k][0];
            b[1] = s1.pts[k][1];
            b[2] = s1.z;
            c[0] = s1.pts[k2][0];
            c[1] = s1.pts[k2][1];
            c[2] = s1.z;
            d[0] = s0.pts[k2][0];
            d[1] = s0.pts[k2][1];
            d[2] = s0.z;
            color(i, k, tmp);
            builder.quad(a, b, c, d, tmp);
        }
    }

    if (o.capStart) {
        const s = sections[0];
        const pts = [];
        for (let k = 0; k < m; k++) pts.push([s.pts[k][0], s.pts[k][1], s.z]);
        color(-1, -1, tmp);
        builder.polygon(pts, tmp, false);
    }
    if (o.capEnd) {
        const s = sections[sections.length - 1];
        const pts = [];
        for (let k = 0; k < m; k++) pts.push([s.pts[k][0], s.pts[k][1], s.z]);
        color(sections.length, -1, tmp);
        builder.polygon(pts, tmp, true);
    }
}

// ============================================================================
// Покраска и уборка
// ============================================================================

/**
 * Перекрашивает существующую геометрию: paint(x, y, z, i, outColor).
 * Если атрибута color нет — он создаётся.
 */
export function paintVertices(geometry, paint) {
    const p = geometry.attributes.position;
    let c = geometry.attributes.color;
    if (!c) {
        c = new THREE.BufferAttribute(new Float32Array(p.count * 3), 3);
        geometry.setAttribute('color', c);
    }
    const tmp = new THREE.Color(1, 1, 1);
    for (let i = 0; i < p.count; i++) {
        paint(p.getX(i), p.getY(i), p.getZ(i), i, tmp);
        c.setXYZ(i, tmp.r, tmp.g, tmp.b);
    }
    c.needsUpdate = true;
    return geometry;
}

// ============================================================================
// Запечённое затенение (ambient occlusion) в вершинных цветах
// ============================================================================

/**
 * Приближение ambient occlusion, запекаемое в вершинные цвета ОДИН РАЗ при
 * построении сцены. В кадре это стоит ноль: цвет уже лежит в буфере.
 *
 * Честная трассировка по полусфере при генерации слишком дорога (сцена
 * собирается на старте гонки), поэтому берём два дешёвых приближения, которые
 * вместе дают почти тот же результат на глаз:
 *
 *  1. ВЫСОТА НАД ОПОРОЙ. Чем ближе вершина к земле (или к поверхности, на
 *     которой предмет стоит), тем больше её закрывает сама земля и соседние
 *     предметы. Коэффициент растёт от floor у основания до единицы на высоте
 *     height. Это и есть тёмный контакт у оснований столбов, отбойников,
 *     зданий, деревьев и трибун.
 *  2. НАКЛОН НОРМАЛИ. Грань, смотрящая вверх, видит всё небо; смотрящая вниз —
 *     не видит ничего. Множитель (1 + ny) / 2, смягчённый параметром sky.
 *
 * Работает по месту: атрибут color домножается, а не переписывается, поэтому
 * запекание можно применять к уже покрашенной геометрии и складывать несколько
 * проходов подряд.
 *
 * @param {THREE.BufferGeometry} geometry неиндексированная, с атрибутами
 *                                        position, normal и color
 * @param {object} opts
 *   base    y опорной поверхности (по умолчанию — низ ограничивающего бокса)
 *   height  на какой высоте затенение сходит на нет, м
 *   floor   множитель у самой опоры, 0..1 (0.55 — заметно, но не черно)
 *   power   кривизна спада: >1 прижимает тень к земле
 *   sky     вклад наклона нормали, 0..1
 *   down    дополнительный множитель для граней, смотрящих строго вниз
 */
export function bakeContactAO(geometry, opts) {
    const o = opts || {};
    const pos = geometry.attributes.position;
    const nrm = geometry.attributes.normal;
    let col = geometry.attributes.color;
    if (!pos) return geometry;
    if (!col) {
        col = new THREE.BufferAttribute(new Float32Array(pos.count * 3).fill(1), 3);
        geometry.setAttribute('color', col);
    }
    let base = o.base;
    if (base === undefined) {
        if (!geometry.boundingBox) geometry.computeBoundingBox();
        base = geometry.boundingBox.min.y;
    }
    const height = o.height === undefined ? 1.6 : o.height;
    const floor = o.floor === undefined ? 0.55 : o.floor;
    const power = o.power === undefined ? 0.85 : o.power;
    const sky = o.sky === undefined ? 0.22 : o.sky;
    const down = o.down === undefined ? 0.82 : o.down;
    const invH = height > 1e-6 ? 1 / height : 0;
    const pa = pos.array;
    const na = nrm ? nrm.array : null;
    const ca = col.array;

    for (let i = 0, n = pos.count; i < n; i++) {
        const i3 = i * 3;
        let t = (pa[i3 + 1] - base) * invH;
        if (t < 0) t = 0;
        else if (t > 1) t = 1;
        if (power !== 1) t = Math.pow(t, power);
        let k = floor + (1 - floor) * t;
        if (na) {
            const ny = na[i3 + 1];
            // полусферический вклад: грань вверх видит всё небо (множитель 1),
            // грань вниз не видит ничего (множитель 1 - sky)
            k *= 1 - sky + sky * (ny * 0.5 + 0.5);
            if (ny < -0.5) k *= down;
        }
        if (k > 1) k = 1;
        ca[i3] *= k;
        ca[i3 + 1] *= k;
        ca[i3 + 2] *= k;
    }
    col.needsUpdate = true;
    return geometry;
}

/**
 * Затенение вокруг заданных точек в системе координат геометрии: вершина
 * тем темнее, чем ближе она к точке-окклюдеру. Этим темнеют колёсные арки и
 * низ кузова у машины и ниши под ступенями трибун.
 *
 * @param {THREE.BufferGeometry} geometry
 * @param {Float32Array|Array} points плоский список x, y, z, radius по четыре
 * @param {number} strength насколько темнеет вершина в самом центре, 0..1
 */
export function bakeProximityAO(geometry, points, strength) {
    const pos = geometry.attributes.position;
    let col = geometry.attributes.color;
    if (!pos || !points || !points.length) return geometry;
    if (!col) {
        col = new THREE.BufferAttribute(new Float32Array(pos.count * 3).fill(1), 3);
        geometry.setAttribute('color', col);
    }
    const s = strength === undefined ? 0.35 : strength;
    const pa = pos.array;
    const ca = col.array;
    const m = (points.length / 4) | 0;
    for (let i = 0, n = pos.count; i < n; i++) {
        const i3 = i * 3;
        const x = pa[i3],
            y = pa[i3 + 1],
            z = pa[i3 + 2];
        let dark = 0;
        for (let p = 0; p < m; p++) {
            const p4 = p * 4;
            const dx = x - points[p4];
            const dy = y - points[p4 + 1];
            const dz = z - points[p4 + 2];
            const r = points[p4 + 3];
            const d2 = dx * dx + dy * dy + dz * dz;
            const r2 = r * r;
            if (d2 >= r2) continue;
            // мягкий спад: 1 в центре, 0 на радиусе
            const t = 1 - Math.sqrt(d2) / r;
            const v = t * t * (3 - 2 * t);
            if (v > dark) dark = v;
        }
        if (dark <= 0) continue;
        const k = 1 - s * dark;
        ca[i3] *= k;
        ca[i3 + 1] *= k;
        ca[i3 + 2] *= k;
    }
    col.needsUpdate = true;
    return geometry;
}

/**
 * Освобождает геометрии и материалы поддерева. Материалы и геометрии,
 * встреченные повторно, освобождаются один раз.
 * Текстуры материалов освобождаются вместе с ними.
 */
export function disposeObject(root) {
    const geoms = new Set();
    const mats = new Set();
    root.traverse(function (obj) {
        if (obj.geometry) geoms.add(obj.geometry);
        const m = obj.material;
        if (m) {
            if (Array.isArray(m)) for (let i = 0; i < m.length; i++) mats.add(m[i]);
            else mats.add(m);
        }
    });
    geoms.forEach(function (g) {
        g.dispose();
    });
    mats.forEach(function (m) {
        if (m.map) m.map.dispose();
        if (m.emissiveMap) m.emissiveMap.dispose();
        if (m.alphaMap) m.alphaMap.dispose();
        m.dispose();
    });
    if (root.parent) root.parent.remove(root);
}

/** Сумма треугольников поддерева с учётом инстансов — для отчётов и оверлея F3. */
export function countTriangles(root) {
    let total = 0;
    root.traverse(function (obj) {
        const g = obj.geometry;
        if (!g || !g.attributes || !g.attributes.position) return;
        const verts = g.index ? g.index.count : g.attributes.position.count;
        const inst = obj.isInstancedMesh ? obj.count : 1;
        total += (verts / 3) * inst;
    });
    return Math.round(total);
}

/** Сумма draw call поддерева (по одному на видимый меш). */
export function countDrawCalls(root) {
    let total = 0;
    root.traverse(function (obj) {
        if (obj.isMesh || obj.isInstancedMesh || obj.isLine || obj.isPoints) total++;
    });
    return total;
}

// ============================================================================
// Отсечение по пирамиде видимости
// ============================================================================
//
// Это единственная часть geomutil.js, которая работает В КАДРОВОМ ЦИКЛЕ,
// поэтому здесь особенно строго: ни одной аллокации. Матрица, пирамида и
// вспомогательные объекты — модульные константы, заполняются на месте.
//
// Проверка ведётся по ОГРАНИЧИВАЮЩИМ СФЕРАМ. Сфера — самая дешёвая форма:
// шесть скалярных произведений и одно сравнение, никаких ветвлений по осям.
// Консервативность сферы здесь в плюс: объект скорее останется в кадре
// лишний раз, чем пропадёт на краю экрана.

const _cullMat = new THREE.Matrix4();

/**
 * Пирамида видимости камеры плюс отсечение по дальности.
 *
 * Дальность — не отсебятина, а следствие тумана: линейный `scene.fog`
 * полностью заменяет цвет объекта цветом тумана на расстоянии `far`, и за
 * этой чертой любой объект неотличим от фона. Поэтому отсечение по
 * `maxDist = fog.far + запас` картинку не меняет, а работу снимает.
 *
 * Объекты, которые ЗАВЕДОМО дальше тумана и всё равно должны быть видны
 * (горные пики на горизонте, облака), проверяются через inFrustum() —
 * без дальности.
 */
export class ViewCuller {
    constructor() {
        this.frustum = new THREE.Frustum();
        this.camX = 0;
        this.camY = 0;
        this.camZ = 0;
        this.fwdX = 0;
        this.fwdY = 0;
        this.fwdZ = -1;
        this.maxDist = 1e9;
        this.enabled = true;
    }

    /**
     * Перечитать камеру. Зовётся один раз в кадре, ПОСЛЕ updateMatrixWorld.
     * @param {THREE.Camera} camera
     * @param {number} maxDist дальность отсечения, м (0 или меньше — без предела)
     */
    update(camera, maxDist) {
        _cullMat.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse);
        this.frustum.setFromProjectionMatrix(_cullMat);
        const e = camera.matrixWorld.elements;
        this.camX = e[12];
        this.camY = e[13];
        this.camZ = e[14];
        // Взгляд камеры: третий столбец матрицы — её локальная +Z, а смотрит
        // камера в -Z. Нужен именно он, а не радиус (см. visible()).
        const l = Math.sqrt(e[8] * e[8] + e[9] * e[9] + e[10] * e[10]) || 1;
        this.fwdX = -e[8] / l;
        this.fwdY = -e[9] / l;
        this.fwdZ = -e[10] / l;
        this.maxDist = maxDist > 0 ? maxDist : 1e9;
    }

    /** Отключить отсечение целиком (отладка и замеры «как было»). */
    setEnabled(on) {
        this.enabled = on !== false;
    }

    /** Сфера в пирамиде видимости, без учёта дальности. */
    inFrustum(x, y, z, r) {
        if (!this.enabled) return true;
        const planes = this.frustum.planes;
        for (let i = 0; i < 6; i++) {
            const p = planes[i];
            const n = p.normal;
            if (n.x * x + n.y * y + n.z * z + p.constant < -r) return false;
        }
        return true;
    }

    /**
     * Сфера видна: и в пирамиде, и ближе предела дальности.
     *
     * Дальность меряется ВДОЛЬ ВЗГЛЯДА камеры, а не по радиусу, и это не
     * придирка. `THREE.Fog` линейный по `vFogDepth`, то есть по глубине в
     * системе камеры (`-mvPosition.z`), а не по расстоянию до камеры. У
     * объекта на краю кадра радиус больше глубины в полтора раза: отсекая
     * по радиусу, мы срезали бы то, что туман ещё не успел скрыть, — и на
     * боку кадра у горизонта появлялась бы заметная разница. Проверено
     * сравнением кадров: по радиусу — до 0,04 % пикселей расходятся и
     * доходит до 139 из 255 по яркости, по глубине — ноль.
     */
    visible(x, y, z, r) {
        if (!this.enabled) return true;
        const dx = x - this.camX;
        const dy = y - this.camY;
        const dz = z - this.camZ;
        const depth = dx * this.fwdX + dy * this.fwdY + dz * this.fwdZ;
        if (depth - r > this.maxDist) return false;
        return this.inFrustum(x, y, z, r);
    }
}

/**
 * Накопитель ограничивающей сферы: собирает объединение сфер и отдаёт
 * центр и радиус. Работает через габаритный ящик — так объединение считается
 * за одно сравнение на ось и остаётся консервативным.
 * Используется ТОЛЬКО при сборке сцены.
 */
export class SphereAccum {
    constructor() {
        this.reset();
    }

    reset() {
        this.n = 0;
        this.minX = Infinity; this.minY = Infinity; this.minZ = Infinity;
        this.maxX = -Infinity; this.maxY = -Infinity; this.maxZ = -Infinity;
    }

    add(x, y, z, r) {
        this.n++;
        if (x - r < this.minX) this.minX = x - r;
        if (y - r < this.minY) this.minY = y - r;
        if (z - r < this.minZ) this.minZ = z - r;
        if (x + r > this.maxX) this.maxX = x + r;
        if (y + r > this.maxY) this.maxY = y + r;
        if (z + r > this.maxZ) this.maxZ = z + r;
    }

    /** Записать (cx, cy, cz, radius) в out по смещению off. Пустой набор — радиус -1. */
    writeTo(out, off) {
        if (this.n === 0) {
            out[off] = 0; out[off + 1] = 0; out[off + 2] = 0; out[off + 3] = -1;
            return;
        }
        const cx = (this.minX + this.maxX) * 0.5;
        const cy = (this.minY + this.maxY) * 0.5;
        const cz = (this.minZ + this.maxZ) * 0.5;
        const hx = this.maxX - cx;
        const hy = this.maxY - cy;
        const hz = this.maxZ - cz;
        out[off] = cx;
        out[off + 1] = cy;
        out[off + 2] = cz;
        out[off + 3] = Math.sqrt(hx * hx + hy * hy + hz * hz);
    }
}

/**
 * Габаритная сфера диапазона вершин неиндексированной геометрии.
 * Нужна нарезке полотна и рельефа на сегменты: у каждого сегмента своя сфера.
 */
export function rangeSphere(positions, start, count, out, off) {
    let minX = Infinity, minY = Infinity, minZ = Infinity;
    let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    const end = (start + count) * 3;
    for (let i = start * 3; i < end; i += 3) {
        const x = positions[i], y = positions[i + 1], z = positions[i + 2];
        if (x < minX) minX = x;
        if (y < minY) minY = y;
        if (z < minZ) minZ = z;
        if (x > maxX) maxX = x;
        if (y > maxY) maxY = y;
        if (z > maxZ) maxZ = z;
    }
    if (count <= 0) {
        out[off] = 0; out[off + 1] = 0; out[off + 2] = 0; out[off + 3] = -1;
        return;
    }
    const cx = (minX + maxX) * 0.5;
    const cy = (minY + maxY) * 0.5;
    const cz = (minZ + maxZ) * 0.5;
    const hx = maxX - cx, hy = maxY - cy, hz = maxZ - cz;
    out[off] = cx;
    out[off + 1] = cy;
    out[off + 2] = cz;
    out[off + 3] = Math.sqrt(hx * hx + hy * hy + hz * hz);
}

/**
 * Аддитивный материал в тумане обязан гаснуть В ЧЁРНОЕ, а не в цвет тумана.
 *
 * Штатный `fog_fragment` подмешивает к цвету фрагмента цвет тумана. Для
 * обычной поверхности это верно, а для аддитивного смешивания — нет: далёкий
 * ореол вместо того, чтобы исчезнуть, начинает ПРИБАВЛЯТЬ к фону цвет тумана.
 * Десяток далёких фонарей так поднимают небо у горизонта на несколько
 * ступеней яркости. Побочный эффект важнее косметики: пока далёкий ореол
 * что-то добавляет к кадру, отсечение по дальности перестаёт быть
 * бесплатным — убрать его становится видно.
 *
 * Здесь та же строка смешивания уводит цвет в ноль: свет по дороге
 * рассеивается, а не подкрашивает воздух.
 */
export function fadeAdditiveFog(material, key) {
    // onBeforeCompile получает ИСХОДНИК ДО раскрытия #include: подменять надо
    // сам include, а не строку из чанка — её в этот момент ещё нет.
    const body = [
        '#ifdef USE_FOG',
        '  #ifdef FOG_EXP2',
        '    float fogFactor = 1.0 - exp( - fogDensity * fogDensity * vFogDepth * vFogDepth );',
        '  #else',
        '    float fogFactor = smoothstep( fogNear, fogFar, vFogDepth );',
        '  #endif',
        '  gl_FragColor.rgb = mix( gl_FragColor.rgb, vec3( 0.0 ), fogFactor );',
        '#endif'
    ].join('\n');
    material.onBeforeCompile = function (shader) {
        shader.fragmentShader = shader.fragmentShader.replace('#include <fog_fragment>', body);
    };
    // без своего ключа three.js переиспользует программу обычного материала
    material.customProgramCacheKey = function () {
        return 'addFog:' + key;
    };
    return material;
}
