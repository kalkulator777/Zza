/**
 * trackmesh.js — [render] построение статической геометрии трассы.
 *
 * Экспортирует buildTrackMeshes(track, theme, quality) из контракта (раздел 3).
 * Вход — данные формата 12.1 (плоские массивы x, y, z, tx, tz, nx, nz, hw, s),
 * либо экземпляр клиентского Track из js/track.js (те же массивы под именами
 * cx, cy, cz, ctx, ctz, cnx, cnz, chw, cs), либо список samples из 7.2.
 *
 * Бюджет (раздел 1): четыре меша на всю трассу.
 *
 *   surface   полотно + бордюры + лента рельефа (общий материал)
 *   markings  осевая прерывистая, кромочные, стартовая клетка
 *   ground    общий грунт до горизонта (два треугольника)
 *   arch      стартовая арка
 *
 * ОТСЕЧЕНИЕ. Первые два меша нарезаны на сегменты по дуге (~180 м) и в кадре
 * рисуются только видимыми кусками: 1–3 вызова на меш вместо одного, но
 * вместо всего кольца — только то, что попадает в пирамиду видимости и ближе
 * тумана. На длинной трассе это кратное падение треугольников.
 * Полотно, бордюры и рельеф живут в одном меше именно поэтому: нарезка
 * умножает число вызовов, и держать три меша там, где хватает одного, стало
 * дорого. Подробности — у SegmentedSurface ниже.
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
    bakeContactAO,
    bakeVertexLight,
    disposeObject,
    countTriangles,
    countDrawCalls,
    rangeSphere
} from './geomutil.js';

// ---------------------------------------------------------------------------
// Пресеты качества: реально меняют плотность сетки
// ---------------------------------------------------------------------------

const QUALITY = {
    low: { roadCols: 4, terrainRings: 4, terrainStride: 3, kerbSide: false, edgeLines: true, archDetail: 0 },
    medium: { roadCols: 6, terrainRings: 6, terrainStride: 2, kerbSide: true, edgeLines: true, archDetail: 1 },
    high: { roadCols: 8, terrainRings: 8, terrainStride: 1, kerbSide: true, edgeLines: true, archDetail: 2 }
};

// ---------------------------------------------------------------------------
// Запечённое затенение (задача «AO в вершины»)
// ---------------------------------------------------------------------------
//
// Считается один раз при построении и живёт в атрибуте color. В кадре стоит
// ноль. Полосу вдоль кромки полотна даёт вершинный градиент: узлы сетки у
// бордюра темнее, к середине дороги затенение сходит на нет.

const AO_EDGE_DEPTH = 0.46;   // насколько темнеет асфальт у самого бордюра
const AO_EDGE_WIDTH = 2.2;    // м, ширина тёмной полосы вдоль кромки
const AO_SHOULDER = 0.50;     // насколько темнеет обочина у кромки
const AO_SHOULDER_W = 5.0;    // м, на каком удалении затенение обочины сходит
const AO_DITCH = 0.22;        // добавка за провал рельефа ниже полотна

/** Гладкая ступенька smoothstep(0, 1, t). */
function smooth01(t) {
    if (t <= 0) return 0;
    if (t >= 1) return 1;
    return t * t * (3 - 2 * t);
}

/** Множитель затенения асфальта: dist — расстояние от кромки полотна, м. */
function roadEdgeAO(dist) {
    return 1 - AO_EDGE_DEPTH * smooth01(1 - dist / AO_EDGE_WIDTH);
}

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

// ---------------------------------------------------------------------------
// Время суток на полотне
// ---------------------------------------------------------------------------
//
// Само освещение задаёт scenery.js. Здесь правится только то, что запечено
// в вершины: асфальт и трава к ночи уходят в холодную темноту, а разметка,
// наоборот, светлеет и получает слабый эмиссив — это световозвращающая
// краска, без неё в свете фар полотно читается как чёрная яма.

const TOD_SURFACE = {
    day: { k: 1.0, tint: [1.0, 1.0, 1.0], lineK: 1.0, lineEmissive: null },
    dusk: { k: 0.9, tint: [1.04, 0.96, 0.92], lineK: 1.05, lineEmissive: '#1a1712' },
    night: { k: 0.72, tint: [0.84, 0.9, 1.06], lineK: 1.25, lineEmissive: '#31353d' }
};

/** Приглушить и подкрасить цвет под время суток (на месте). */
function todShade(color, tod) {
    const t = tod.tint;
    color.setRGB(
        Math.min(1, color.r * tod.k * t[0]),
        Math.min(1, color.g * tod.k * t[1]),
        Math.min(1, color.b * tod.k * t[2])
    );
    return color;
}

// ---------------------------------------------------------------------------
// Погода на полотне
// ---------------------------------------------------------------------------
//
// Погода приходит тем же каналом, что и время суток (`env.weather`, см.
// scenery.js), и ложится ПОВЕРХ него: сначала палитра темы приглушается под
// время суток, потом — под погоду. Сухая погода не меняет ничего вовсе,
// это проверяется попиксельно.
//
// Мокрый асфальт складывается из двух половин:
//  1. ЗАТЕМНЕНИЕ. Мокрая поверхность отражает больше и рассеивает меньше,
//     поэтому диффузная составляющая падает, а цвет холоднеет. Это запекается
//     в вершинные цвета полотна — в кадре ноль вызовов, ноль треугольников,
//     ноль фрагментов.
//  2. БЛИК. Френелевский подмес цвета неба плюс вытянутый зеркальный отблик
//     солнца, посчитанные В ТЕХ ЖЕ пикселях, которые полотно и так рисует
//     (см. surfaceShader ниже). Ни второго прохода, ни экранных целей.
//
// Плоского зеркала здесь нет намеренно: это второй полный обход сцены
// (+52 вызова, +44 тыс. треугольников), и снижение его разрешения ни вызовов,
// ни треугольников не убирает.

const WEATHER_SURFACE = {
    clear: { roadK: 1.0, roadTint: [1.0, 1.0, 1.0], kerbK: 1.0, grassK: 1.0, gloss: 0.0 },
    // множители подобраны так, чтобы асфальт читался как мокрый, но не как
    // ночной: полотно темнеет заметно, обочина — вдвое слабее (трава мокнет,
    // но не блестит), бордюры почти не трогаются, иначе теряется шашка
    wet: { roadK: 0.56, roadTint: [0.94, 0.98, 1.10], kerbK: 0.86, grassK: 0.82, gloss: 1.0 }
};

const KERB_WIDTH = 0.62; // ширина бордюра, м
const MARK_LIFT = 0.015; // подъём разметки над полотном, м

// ---------------------------------------------------------------------------
// Трамплины
// ---------------------------------------------------------------------------
//
// Геометрия строится по полю `ramps` формата 12.1 — по тем же числам, по
// которым считает физика (game/track.py, RAMP_*), поэтому картинка и шаг 14б
// не могут разъехаться: профиль высоты здесь буквально тот же, что в
// Track.ramp_height.
//
// ГЛАВНОЕ ТРЕБОВАНИЕ К ВИДУ — трамплин обязан читаться ЗАРАНЕЕ. Улететь
// случайно и потом выяснить, что это было, игрок не простит. Поэтому въезд
// не просто наклонная плоскость:
//
//   * от него на RAMP_APPROACH метров назад тянется полосатый коридор —
//     он виден задолго до самого въезда и сразу показывает ширину трамплина
//     и то, с какой стороны его можно объехать;
//   * коридор и въезд обрамлены поднятыми бордюрными рейками в цвете
//     поребриков темы: это объём, он читается на дистанции, где плоская
//     разметка уже сливается;
//   * само полотно въезда размечено поперечными предупреждающими полосами,
//     так что уклон виден как уклон, а не как пятно на асфальте;
//   * за кромкой вылета стоит тёмная вертикальная стенка — силуэт классического
//     трамплина, а не «дорога вдруг оборвалась».
//
// Всё это ОДИН меш на все трамплины трассы: он маленький (сотни треугольников),
// живёт на общем материале полотна и стоит один вызов отрисовки.
const RAMP_EDGE_W = 1.0;      // м бокового скоса — зеркало RAMP_EDGE из game/track.py
const RAMP_APPROACH = 16.0;   // м полосатого коридора перед въездом
const RAMP_APRON_LIFT = 0.03; // м подъёма коридора над полотном (без z-fighting)
const RAMP_BAND = 1.6;        // м, шаг предупреждающих полос
const RAMP_RAIL_W = 0.42;     // м, ширина бордюрной рейки по краю трамплина
const RAMP_RAIL_H = 0.26;     // м, высота рейки над полотном
const RAMP_RAIL_BAND = 1.1;   // м, шаг чередования цветов рейки
const RAMP_DECK_STEP = 0.7;   // м, шаг разбиения въезда вдоль дуги
const RAMP_APRON_STEP = 1.6;  // м, шаг разбиения коридора вдоль дуги

// ---------------------------------------------------------------------------
// Карта следов в параметризации ленты
// ---------------------------------------------------------------------------
//
// Следы шин, юз, пыль на обочине и копоть от взрывов — ОДНА текстура,
// натянутая на полотно по его собственной параметризации: одна ось — путь
// по кругу, вторая — смещение поперёк. В кадре это стоит ноль вызовов,
// ноль треугольников и ноль фрагментов: выборка идёт в тех же пикселях,
// которые полотно и так рисует.
//
// Почему не геометрия и не декали. За гонку набегает около 50 000 звеньев
// следа; лентой квадов это 96 000 треугольников — весь бюджет кадра. Резать
// кольцевым буфером до 8 000 нельзя: следы начнут пропадать через сорок
// секунд, а требование ровно обратное — они копятся всю гонку.
//
// Почему не карта по мировым XZ. На габарит трассы 513 м даже 2048² даёт
// 0,25 м на тексель при ширине следа 0,22 м — след уже тексела и не читается.
// Нужное разрешение стоило бы 36 МБ.
//
// РАСКЛАДКА ОСЕЙ. Путь по кругу идёт по ВЫСОТЕ текстуры, смещение поперёк —
// по ширине. Так участок трассы — это несколько подряд идущих СТРОК, то есть
// непрерывный кусок массива: и дописывание отпечатков, и выцветание льются
// в GPU прямоугольниками через copyTextureToTexture, без перекладывания
// данных и без сырого WebGL.
//
// ПОПЕРЕЧНЫЙ ОХВАТ шире полотна (MARK_EXTENT полуширин в каждую сторону):
// внутри него живут и дорога, и бордюры, и ближняя обочина, поэтому следы
// на траве получаются той же картой и той же выборкой — даром. Дальше охвата
// параметр зажимается в крайний столбец, который НИКОГДА не штампуется:
// это и есть «заведомо пустая кайма» вместо проверки в шейдере.

const MARK_EXTENT = 1.6;      // полуширин полотна в каждую сторону
const MARK_METERS = 0.35;     // целевая длина тексела вдоль трассы, м
const MARK_COLS = { low: 128, medium: 192, high: 256 };
const MARK_ROWS_MIN = 2048;
const MARK_ROWS_MAX = 8192;
const MARK_EDGE = 2;          // столбцов пустой каймы с каждого края
const MARK_CAP = 232;         // потолок насыщения: трасса не станет чёрной
const MARK_HALFLIFE = 75.0;   // с, за сколько след бледнеет вдвое
const MARK_SLICE = 96;        // строк выцветания за кадр
const MARK_MAX_RECTS = 5;     // прямоугольников догрузки за кадр
// Не чаще этого шлём накопленное в видеопамять, с.
//
// Цена догрузки измерена, и это цена ВЫЗОВА, а не строк: при одинаковых
// 24 КБ данных один copyTextureToTexture стоит 0,49 мс в Firefox, 4,2 мс
// в Chromium с llvmpipe и 9,5–10,6 мс в Chromium со swiftshader, а вклад
// строк в регрессии равен нулю. В заносе грязный прямоугольник появляется
// каждый кадр, то есть шестьдесят вызовов в секунду на ровном месте.
//
// Порог выведен из картинки, а не подобран. Задержка догрузки — это разрыв
// между колесом и началом следа длиной v*T. Требование: разрыв короче
// половины машины (2 м) на той скорости, где ещё скользят (40 м/с), то есть
// T <= 2/40 = 0,05 с. При 60 кадрах это каждый третий кадр, при 20 — каждый:
// прореживание идёт по времени, поэтому на просадке кадра отставание следа
// не растёт, а исчезает.
const MARK_UPLOAD_PERIOD = 0.05;
const MARK_BRUSHES = 24;      // кистей с памятью прошлой точки (8 машин x 2 + запас)

/**
 * Число строк карты под длину круга. Степень двойки не нужна: WebGL2 снял
 * ограничение на размеры текстур, а округление вверх до степени двойки стоило
 * бы лишний мегабайт на трёх трассах из пяти (serpentine 4423 строки округлились
 * бы до 8192). Кратность 64 оставлена ради выравнивания строк при догрузке.
 */
function markRows(length) {
    let rows = Math.ceil(length / MARK_METERS / 64) * 64;
    if (rows < MARK_ROWS_MIN) rows = MARK_ROWS_MIN;
    else if (rows > MARK_ROWS_MAX) rows = MARK_ROWS_MAX;
    return rows;
}

/**
 * Карта следов: пиксели в оперативной памяти плюс её двойник в видеопамяти.
 *
 * Две THREE.DataTexture делят ОДИН массив байт. Одна (`texture`) висит
 * в материале полотна, вторая (`src`) не участвует в отрисовке и служит
 * источником для частичной догрузки: three.js в этом случае уходит в ветку
 * texSubImage2D и заливает ровно указанный прямоугольник, а не всю текстуру.
 * Полная заливка мегабайта каждый кадр была бы и трафиком, и перевалидацией
 * текстуры в драйвере — ровно тем, чего на Intel хочется избежать.
 */
class TrackMarkMap {
    constructor(cols, rows, length) {
        this.cols = cols;
        this.rows = rows;
        this.length = length > 1 ? length : 1;
        this.data = new Uint8Array(cols * rows);

        this.texture = new THREE.DataTexture(this.data, cols, rows, THREE.RedFormat, THREE.UnsignedByteType);
        this.texture.name = 'trackMarks';
        this.texture.wrapS = THREE.ClampToEdgeWrapping;
        // вдоль круга карта замкнута: последний метр соседствует с первым
        this.texture.wrapT = THREE.RepeatWrapping;
        this.texture.minFilter = THREE.LinearFilter;
        this.texture.magFilter = THREE.LinearFilter;
        this.texture.generateMipmaps = false;
        this.texture.unpackAlignment = 1;
        this.texture.needsUpdate = true;

        this.src = new THREE.DataTexture(this.data, cols, rows, THREE.RedFormat, THREE.UnsignedByteType);
        this.src.unpackAlignment = 1;
        this.src.generateMipmaps = false;

        // память кистей: прошлая точка каждого колеса, чтобы след был линией,
        // а не пунктиром из отдельных отпечатков
        this.brushRow = new Int32Array(MARK_BRUSHES).fill(-1);
        this.brushCol = new Int32Array(MARK_BRUSHES);

        // грязные прямоугольники кадра: (row0, row1) — столбцы всегда все
        this.rectN = 0;
        this.rectA = new Int32Array(MARK_MAX_RECTS + 2);
        this.rectB = new Int32Array(MARK_MAX_RECTS + 2);

        this.fadeRow = 0;
        this.fadeLut = new Uint8Array(256);
        this.fadeK = -1;
        this._rebuildFadeLut(1);

        this._box = new THREE.Box2(new THREE.Vector2(), new THREE.Vector2());
        this._dst = new THREE.Vector3();
        this.uploads = 0;
        this.uploadWait = 0;
    }

    /**
     * Таблица выцветания на 256 значений: в кадре это выборка по индексу,
     * а не Math.pow на каждый тексель. k — множитель за ОДИН заход полосы
     * на данную строку.
     */
    _rebuildFadeLut(k) {
        if (Math.abs(k - this.fadeK) < 1e-5) return;   // кадр в кадр дважды не считаем
        this.fadeK = k;
        const lut = this.fadeLut;
        for (let i = 1; i < 256; i++) {
            const v = i * k;
            // хвост дотягиваем линейно, иначе умножение навсегда застревает
            // на единицах и карта никогда не очищается до нуля
            lut[i] = v > i - 1 ? i - 1 : v | 0;
        }
        lut[0] = 0;
    }

    /** Строка карты по пути вдоль круга (заворачивается). */
    rowAt(s) {
        let t = s / this.length;
        t -= Math.floor(t);
        let r = (t * this.rows) | 0;
        if (r < 0) r = 0;
        else if (r >= this.rows) r = this.rows - 1;
        return r;
    }

    /** Столбец карты по смещению поперёк и полуширине полотна. */
    colAt(lateral, halfWidth) {
        const hw = halfWidth > 0.1 ? halfWidth : 0.1;
        const u = 0.5 + (0.5 * lateral) / (hw * MARK_EXTENT);
        let c = (u * this.cols) | 0;
        if (c < MARK_EDGE) c = -1;
        else if (c >= this.cols - MARK_EDGE) c = -1;
        return c;
    }

    _mark(row, col, radius, strength) {
        const cols = this.cols;
        const rows = this.rows;
        const data = this.data;
        const r = radius < 1 ? 1 : radius | 0;
        const inv = 1 / (r + 0.5);
        for (let dy = -r; dy <= r; dy++) {
            let y = row + dy;
            y -= Math.floor(y / rows) * rows;
            const base = y * cols;
            for (let dx = -r; dx <= r; dx++) {
                const x = col + dx;
                if (x < MARK_EDGE || x >= cols - MARK_EDGE) continue;
                const d = Math.sqrt(dx * dx + dy * dy) * inv;
                if (d >= 1) continue;
                const w = (1 - d) * (1 - d) * strength * 255;
                const at = base + x;
                let v = data[at] + w;
                if (v > MARK_CAP) v = MARK_CAP;
                data[at] = v;
            }
        }
        this._dirty(row - r, row + r);
    }

    /**
     * Мазок кистью brush: соединяется с прошлой точкой той же кисти, поэтому
     * на скорости след остаётся сплошной линией, а не цепочкой пятен.
     */
    stroke(brush, s, lateral, halfWidth, strength, radius) {
        const col = this.colAt(lateral, halfWidth);
        if (col < 0) {
            this.brushRow[brush] = -1;
            return;
        }
        const row = this.rowAt(s);
        const prevRow = this.brushRow[brush];
        if (prevRow >= 0) {
            let dr = row - prevRow;
            // короткий путь по кольцу
            if (dr > this.rows * 0.5) dr -= this.rows;
            else if (dr < -this.rows * 0.5) dr += this.rows;
            const dc = col - this.brushCol[brush];
            let steps = Math.max(Math.abs(dr), Math.abs(dc));
            if (steps > 16) steps = 16;   // телепорт (респаун) линией не тянем
            for (let k = 1; k < steps; k++) {
                const t = k / steps;
                this._mark(prevRow + Math.round(dr * t), this.brushCol[brush] + Math.round(dc * t),
                    radius, strength);
            }
        }
        this._mark(row, col, radius, strength);
        this.brushRow[brush] = row;
        this.brushCol[brush] = col;
    }

    /** Разорвать линию кисти: машина перестала скользить. */
    release(brush) {
        this.brushRow[brush] = -1;
    }

    /** Круглое пятно: копоть от взрыва, резина на решётке. */
    blob(s, lateral, halfWidth, strength, radiusM) {
        const col = this.colAt(lateral, halfWidth);
        if (col < 0) return;
        const rr = Math.max(1, Math.round(radiusM / (this.length / this.rows)));
        this._mark(this.rowAt(s), col, rr, strength);
    }

    /**
     * Пометить строки к догрузке. Отпечаток у самого шва круга заворачивается
     * (_mark пишет по модулю), а прямоугольник — нет: завернувшийся хвост
     * приедет в видеопамять со следующим заходом полосы выцветания, то есть
     * меньше чем через секунду. Это дешевле, чем второй прямоугольник.
     */
    _dirty(a, b) {
        let lo = a < 0 ? 0 : a;
        const hi = b >= this.rows ? this.rows - 1 : b;
        if (hi < lo) return;
        // слить с уже накопленными, если пересекаются или примыкают
        for (let i = 0; i < this.rectN; i++) {
            if (lo <= this.rectB[i] + 1 && hi >= this.rectA[i] - 1) {
                if (lo < this.rectA[i]) this.rectA[i] = lo;
                if (hi > this.rectB[i]) this.rectB[i] = hi;
                return;
            }
        }
        if (this.rectN < MARK_MAX_RECTS) {
            this.rectA[this.rectN] = lo;
            this.rectB[this.rectN] = hi;
            this.rectN++;
            return;
        }
        // мест нет: склеиваем с ближайшим — лишние строки дешевле лишнего вызова
        let best = 0;
        let bestGap = 0x7fffffff;
        for (let i = 0; i < this.rectN; i++) {
            const gap = lo > this.rectB[i] ? lo - this.rectB[i] : this.rectA[i] - hi;
            if (gap < bestGap) {
                bestGap = gap;
                best = i;
            }
        }
        if (lo < this.rectA[best]) this.rectA[best] = lo;
        if (hi > this.rectB[best]) this.rectB[best] = hi;
    }

    /**
     * Выцветание. Карта конечная, а гонка длинная: без этого за сессию всё
     * полотно почернеет. Проходим её полосами, MARK_SLICE строк за кадр, —
     * полный круг занимает пару секунд, а стоит несколько тысяч байт.
     */
    fade(dt) {
        if (dt <= 0) return;
        const rows = this.rows;
        // строку навещают раз в rows / MARK_SLICE кадров, то есть раз
        // в dt * rows / MARK_SLICE секунд — из этого и считается множитель
        const visit = (dt * rows) / MARK_SLICE;
        this._rebuildFadeLut(Math.pow(0.5, visit / MARK_HALFLIFE));
        const cols = this.cols;
        const data = this.data;
        const lut = this.fadeLut;
        const row0 = this.fadeRow;
        let row1 = row0 + MARK_SLICE;
        if (row1 > rows) row1 = rows;
        const from = row0 * cols;
        const to = row1 * cols;
        let any = false;
        for (let i = from; i < to; i++) {
            const v = data[i];
            if (v === 0) continue;
            data[i] = lut[v];
            any = true;
        }
        if (any) this._dirty(row0, row1 - 1);
        this.fadeRow = row1 >= rows ? 0 : row1;
    }

    /**
     * Догрузить накопленные прямоугольники в видеопамять.
     * copyTextureToTexture с источником-DataTexture, которого нет в отрисовке,
     * уходит в texSubImage2D: заливается ровно прямоугольник.
     *
     * Строки дёшевы, а вызов дорог, поэтому вызовы прореживаются по времени —
     * см. MARK_UPLOAD_PERIOD. Аргумент dt необязателен: без него догрузка идёт
     * сразу, как и раньше.
     */
    upload(renderer, dt) {
        // Прореживание вызовов (MARK_UPLOAD_PERIOD). Таймер идёт всегда, а не
        // только пока есть грязь: иначе одиночный след, появившийся в тишине,
        // ждал бы полный период просто потому, что до него ничего не грузили.
        // Худшее ожидание всё равно равно периоду — из него порог и выведен.
        if (dt > 0) {
            this.uploadWait -= dt;
            if (this.uploadWait > 0) return;
            this.uploadWait = MARK_UPLOAD_PERIOD;
        }
        if (!this.rectN) return;
        const box = this._box;
        const dst = this._dst;
        for (let i = 0; i < this.rectN; i++) {
            const a = this.rectA[i];
            const b = this.rectB[i];
            box.min.set(0, a);
            box.max.set(this.cols, b + 1);
            dst.set(0, a, 0);
            renderer.copyTextureToTexture(this.src, this.texture, box, dst);
            this.uploads++;
        }
        this.rectN = 0;
    }

    /** Стереть всё (новая гонка на той же карте). */
    clear() {
        this.data.fill(0);
        this.brushRow.fill(-1);
        this.texture.needsUpdate = true;
        this.rectN = 0;
        this.uploadWait = 0;
    }

    dispose() {
        this.texture.dispose();
        this.src.dispose();
    }
}

// ---------------------------------------------------------------------------
// Шейдер полотна: карта следов и мокрый блик
// ---------------------------------------------------------------------------
//
// Обе добавки живут в ОДНОЙ вставке и в одной шейдерной программе: полотно
// и разметка делят её (программа у three ключуется параметрами материала,
// а polygonOffset в шейдер не входит). Это +1 программа на сцену, а не +2.
//
// Подводные камни, из-за которых код выглядит именно так:
//  - `attribute vec2 uv` объявлять НЕЛЬЗЯ: three объявляет его в префиксе
//    всегда, повторное объявление роняет линковку, и видно это только
//    в консоли;
//  - `#include <color_vertex>` идёт ДО `<begin_vertex>`, поэтому вставка,
//    которой нужен `transformed`, вешается на `<fog_vertex>` — он последний;
//  - onBeforeCompile получает исходник ДО раскрытия #include, подменять надо
//    сам include;
//  - без customProgramCacheKey three переиспользовал бы программу обычного
//    материала и вставка просто не попала бы в сцену.

function surfaceShader(material, opts) {
    const marks = !!opts.marks;
    const wet = !!opts.wet;
    if (!marks && !wet) return;

    const decl = [];
    const vert = ['#include <fog_vertex>', 'vTrackUV = uv;'];
    const frag = [];
    decl.push('varying vec2 vTrackUV;');
    if (wet) {
        decl.push('varying vec3 vTrackPos;');
        vert.push('vTrackPos = ( modelMatrix * vec4( transformed, 1.0 ) ).xyz;');
    }

    // Порядок важен: резина гасит и блик тоже. Если сначала затемнить
    // полотно следом, а потом прибавить отражение неба, след смоется этим
    // отражением — на мокрой дороге он пропадал почти целиком (проверено
    // на скриншоте). Поэтому след умножает ИТОГ, включая блик.
    if (marks) {
        frag.push('float trackMark = 1.0 - uMarkDepth * texture2D( uMarkMap, vTrackUV ).r;');
    }
    if (wet) {
        frag.push(
            // нормаль полотна — вверх: поперёк дороги сечение строго
            // горизонтальное, продольный уклон даёт единицы процентов,
            // и ради них не стоит ни атрибута нормалей, ни производных
            'vec3 wetV = normalize( cameraPosition - vTrackPos );',
            'float wetLat = abs( vTrackUV.x - 0.5 ) * ' + (2 * MARK_EXTENT).toFixed(3) + ';',
            'float wetMask = 1.0 - smoothstep( 0.90, 1.04, wetLat );',
            'float wetFr = 0.04 + 0.96 * pow( 1.0 - clamp( wetV.y, 0.0, 1.0 ), 5.0 );',
            'vec3 wetRefl = reflect( -wetV, vec3( 0.0, 1.0, 0.0 ) );',
            'float wetSun = pow( max( dot( wetRefl, uWetSunDir ), 0.0 ), 68.0 );',
            'outgoingLight += ( uWetSky * wetFr * uWetGloss',
            '    + uWetSunColor * wetSun * uWetGloss * 1.6 ) * wetMask;'
        );
    }
    if (marks) frag.push('outgoingLight *= trackMark;');

    const uniforms = opts.uniforms;
    material.onBeforeCompile = function (shader) {
        for (const key in uniforms) shader.uniforms[key] = uniforms[key];
        const head = [];
        if (marks) head.push('uniform sampler2D uMarkMap;', 'uniform float uMarkDepth;');
        if (wet) head.push('uniform vec3 uWetSky;', 'uniform vec3 uWetSunColor;',
            'uniform vec3 uWetSunDir;', 'uniform float uWetGloss;');
        shader.vertexShader = decl.join('\n') + '\n' + shader.vertexShader
            .replace('#include <fog_vertex>', vert.join('\n'));
        shader.fragmentShader = head.join('\n') + '\n' + decl.join('\n') + '\n'
            + shader.fragmentShader.replace('#include <opaque_fragment>',
                frag.join('\n') + '\n#include <opaque_fragment>');
    };
    material.customProgramCacheKey = function () {
        return 'trackSurface:' + (marks ? 'm' : '') + (wet ? 'w' : '');
    };
}

// ---------------------------------------------------------------------------
// Нарезка полотна и рельефа на сегменты вдоль дуги
// ---------------------------------------------------------------------------
//
// Прежде вся лента шла в кадр целиком: цена росла прямо пропорционально длине
// круга, и длинные трассы приходилось лечить прореживанием выборки
// (TERRAIN_REF_LENGTH — снят вместе с этим текстом). Теперь полотно, бордюры,
// разметка и рельеф режутся на сегменты по дуге, у каждого сегмента своя
// ограничивающая сфера, и в кадр уходят только видимые.
//
// ШВОВ НЕТ ПО ПОСТРОЕНИЮ: соседние сегменты делят общий ряд выборок. Ряд
// row[k+1] строится и как последний ряд сегмента k, и как первый ряд
// сегмента k+1, из одних и тех же чисел, — вершины совпадают бит в бит,
// щели между сегментами появиться неоткуда.
//
// Как это попадает в GPU одним мешем. Сегменты лежат в буфере подряд, в
// порядке дуги. `geometry.groups` — это список диапазонов, и three.js
// рисует по одному вызову на группу, НО только если материал меша задан
// массивом (проверено по вендоренному r180: `Array.isArray(material)` —
// единственная ветка, где groups вообще читаются). Поэтому материал у
// сегментного меша — массив из MAX_RUNS одинаковых ссылок, а в кадре
// переписывается только длина списка групп и границы диапазонов.
// Ни новых объектов, ни новых материалов при этом не возникает.

const SEGMENT_LENGTH = 180.0; // целевая длина сегмента по дуге, м

/**
 * Потолок числа прогонов на один меш.
 *
 * Видимые сегменты почти всегда идут подряд (камера смотрит вперёд вдоль
 * дуги), но на кольце бывает виден и противоположный виток — это второй
 * прогон. Каждый прогон стоит один draw call, поэтому их число ограничено:
 * если видимых кусков больше, самые близкие друг к другу склеиваются вместе
 * с промежутком. Хуже картинке от этого не будет — только чуть больше
 * треугольников.
 */
const MAX_RUNS = 3;

/** Границы сегментов в индексах выборок осевой линии. */
function planSegments(T) {
    let n = Math.round(T.length / SEGMENT_LENGTH);
    if (n < 3) n = 3;
    if (n > 48) n = 48;
    if (n > T.count >> 2) n = Math.max(1, T.count >> 2);
    const row = new Int32Array(n + 1);
    for (let k = 0; k <= n; k++) row[k] = Math.round((k * T.count) / n);
    row[0] = 0;
    row[n] = T.count;
    // строгая монотонность: на совсем короткой трассе округление может слипнуться
    for (let k = 1; k <= n; k++) if (row[k] <= row[k - 1]) row[k] = row[k - 1] + 1;
    return { count: n, row: row };
}

/**
 * Меш, нарезанный на сегменты вдоль дуги, с покадровым отсечением.
 *
 * starts/counts — вершинные диапазоны сегментов, spheres — по четыре числа
 * (cx, cy, cz, r) на сегмент. Всё рабочее выделено здесь, в кадре только
 * счёт и запись чисел.
 */
class SegmentedSurface {
    constructor(name, geometry, material, starts, counts, spheres) {
        this.n = starts.length;
        this.starts = starts;
        this.counts = counts;
        this.spheres = spheres;
        this.vis = new Uint8Array(this.n);
        this.runStart = new Int32Array(this.n + 1);
        this.runEnd = new Int32Array(this.n + 1);

        // Материал-массив: групп у геометрии без него three.js не читает.
        const mats = [];
        for (let i = 0; i < MAX_RUNS; i++) mats.push(material);
        const mesh = new THREE.Mesh(geometry, mats);
        mesh.name = name;
        mesh.matrixAutoUpdate = false;
        mesh.updateMatrix();
        // Отсечение целого меша бессмысленно — оно и есть наша задача,
        // только по сегментам.
        mesh.frustumCulled = false;
        this.mesh = mesh;

        // Пул объектов групп: в кадре переписываются их поля, а не создаются
        // новые. geometry.groups — живой массив, ему меняется только длина.
        this.pool = [];
        for (let i = 0; i < MAX_RUNS; i++) {
            this.pool.push({ start: 0, count: 0, materialIndex: i });
        }
        this.groups = geometry.groups;
        this.showAll();
    }

    /** Показать всё кольцо (отсечение выключено). */
    showAll() {
        const g = this.pool[0];
        g.start = 0;
        g.count = this.starts.length ? this.starts[this.n - 1] + this.counts[this.n - 1] : 0;
        this.groups[0] = g;
        this.groups.length = 1;
        this.visibleSegments = this.n;
    }

    /** Пересчитать видимые сегменты и собрать из них прогоны. */
    cull(culler) {
        if (!culler || !culler.enabled) {
            this.showAll();
            return;
        }
        const n = this.n;
        const sph = this.spheres;
        const vis = this.vis;
        let shown = 0;
        for (let i = 0; i < n; i++) {
            const o = i * 4;
            const v = sph[o + 3] >= 0 && culler.visible(sph[o], sph[o + 1], sph[o + 2], sph[o + 3]) ? 1 : 0;
            vis[i] = v;
            shown += v;
        }
        this.visibleSegments = shown;
        if (shown === 0) {
            this.groups.length = 0;
            return;
        }

        // прогоны подряд идущих видимых сегментов
        const rs = this.runStart;
        const re = this.runEnd;
        let runN = 0;
        let i = 0;
        while (i < n) {
            if (!vis[i]) { i++; continue; }
            let j = i;
            while (j + 1 < n && vis[j + 1]) j++;
            rs[runN] = i;
            re[runN] = j;
            runN++;
            i = j + 1;
        }

        // склейка лишних прогонов по самому узкому промежутку
        while (runN > MAX_RUNS) {
            let best = 0;
            let bestGap = 0x7fffffff;
            for (let k = 0; k < runN - 1; k++) {
                const gap = rs[k + 1] - re[k] - 1;
                if (gap < bestGap) { bestGap = gap; best = k; }
            }
            re[best] = re[best + 1];
            for (let k = best + 1; k < runN - 1; k++) {
                rs[k] = rs[k + 1];
                re[k] = re[k + 1];
            }
            runN--;
        }

        for (let k = 0; k < runN; k++) {
            const a = rs[k];
            const b = re[k];
            const g = this.pool[k];
            g.start = this.starts[a];
            g.count = this.starts[b] + this.counts[b] - g.start;
            this.groups[k] = g;
        }
        this.groups.length = runN;
    }
}

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
            startGrid: track.start_grid || track.startGrid || null,
            ramps: track.ramps || []
        };
    }

    if (track.cx && track.cz && track.chw) {
        // экземпляр клиентского Track из js/track.js: те же данные, но поля
        // названы иначе (контракт 12.1 фиксирует формат JSON, а не имена полей
        // класса), поэтому принимаем и такой вид
        const count = track.count || track.cx.length;
        return {
            count: count,
            length: track.length || count * (track.step || 2.0),
            step: track.step || 2.0,
            theme: track.theme || 'city',
            seed: track.decorSeed === undefined ? 12345 : track.decorSeed,
            x: track.cx,
            y: track.cy,
            z: track.cz,
            tx: track.ctx,
            tz: track.ctz,
            nx: track.cnx,
            nz: track.cnz,
            hw: track.chw,
            s: track.cs,
            startGrid: track.startGrid || null,
            ramps: track.ramps || []
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
            startGrid: track.start_grid || null,
            ramps: track.ramps || []
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
 * @param {object} [opts]  необязательные параметры, добавленные после контракта:
 *   ao         запекать ли затенение в вершинные цвета;
 *   timeOfDay  day | dusk | night (приходит из настроек комнаты);
 *   weather    clear | wet (тот же канал окружения, см. scenery.js);
 *   wet        включён ли ВИЗУАЛ мокрого асфальта (отдельная галочка);
 *   marks      строить ли карту следов шин (отдельная галочка);
 *   light      {ambient, directional} из buildScenery — если задан, свет
 *              запекается в вершины, материалы становятся MeshBasicMaterial,
 *              а атрибут normal выбрасывается;
 *   sky        THREE.Color неба у горизонта — для френелевского подмеса.
 */
export function buildTrackMeshes(track, theme, quality, opts) {
    const T = readTrack(track);
    const P = QUALITY[quality] || QUALITY.medium;
    const o = opts || {};
    const ao = o.ao !== false;
    const todName = TOD_SURFACE[o.timeOfDay] ? o.timeOfDay : 'day';
    const tod = TOD_SURFACE[todName];
    const weatherName = WEATHER_SURFACE[o.weather] ? o.weather : 'clear';
    // галочка выключает ВЕСЬ визуал мокрого, включая запечённое затемнение:
    // при `clear` или снятой галочке картинка обязана совпадать с прежней
    const wetOn = weatherName !== 'clear' && o.wet !== false;
    const wx = wetOn ? WEATHER_SURFACE[weatherName] : WEATHER_SURFACE.clear;
    const themeName = theme || T.theme || 'city';
    const pal = THEMES[themeName] || THEMES.city;
    const light = o.light || null;

    const group = new THREE.Group();
    group.name = 'track';

    // Карта следов шин. Вдоль круга — по высоте, поперёк ленты — по ширине
    // (см. TrackMarkMap). Строится только по галочке: без неё нет ни текстуры,
    // ни атрибута uv, ни своей шейдерной программы.
    const marks = o.marks
        ? new TrackMarkMap(MARK_COLS[quality] || MARK_COLS.medium, markRows(T.length), T.length)
        : null;

    // Запечённый свет убирает из шейдера полотна всё освещение: Ламберт
    // становится Basic, атрибут нормалей исчезает (минус треть памяти
    // геометрии и минус 12 байт выборки на вершину в каждом кадре).
    const MatClass = light ? THREE.MeshBasicMaterial : THREE.MeshLambertMaterial;
    // flatShading у MeshBasicMaterial нет и быть не может: это свойство про
    // нормали, а нормалей после запекания нет вовсе. Даже сам КЛЮЧ туда
    // передавать нельзя — three ругается в консоль на неизвестное свойство.
    const surfaceParams = { vertexColors: true };
    const markParams = {
        vertexColors: true,
        polygonOffset: true,
        polygonOffsetFactor: -2,
        polygonOffsetUnits: -4
    };
    if (!light) {
        surfaceParams.flatShading = true;
        markParams.flatShading = true;
    }

    // общий материал статики: один объект — меньше переключений состояния
    const surfaceMat = new MatClass(surfaceParams);
    surfaceMat.name = 'trackSurface';

    // разметка: тот же материал плюс полигональное смещение к камере
    const markMat = new MatClass(markParams);
    markMat.name = 'trackMarkings';
    // световозвращающая краска: ночью линии видно и без прямого света.
    // При запечённом свете эмиссив складывается прямо в вершинный цвет —
    // у MeshBasicMaterial его просто некуда положить, и разметка бы погасла.
    const lineEmissive = tod.lineEmissive ? toColor(tod.lineEmissive) : null;
    if (lineEmissive && !light) {
        markMat.emissive = lineEmissive;
        markMat.emissiveIntensity = 1.0;
    }

    // Уникформы вставки в шейдер полотна. Объекты общие для обоих материалов:
    // и текстура следов, и параметры блика живут в одном экземпляре.
    const shaderUniforms = {};
    if (marks) {
        shaderUniforms.uMarkMap = { value: marks.texture };
        shaderUniforms.uMarkDepth = { value: 0.62 };
    }
    if (wetOn) {
        const sky = o.sky ? toColor(o.sky) : toColor('#8fa6c0');
        const sunCol = light ? light.directional.color : toColor('#fff4e2');
        const sunDir = light ? light.directional.direction : [0.45, 0.82, 0.35];
        const dl = Math.sqrt(sunDir[0] * sunDir[0] + sunDir[1] * sunDir[1] + sunDir[2] * sunDir[2]) || 1;
        shaderUniforms.uWetSky = { value: new THREE.Vector3(sky.r, sky.g, sky.b) };
        shaderUniforms.uWetSunColor = {
            value: new THREE.Vector3(sunCol.r, sunCol.g, sunCol.b)
                .multiplyScalar(light ? light.directional.intensity : 1)
        };
        shaderUniforms.uWetSunDir = {
            value: new THREE.Vector3(sunDir[0] / dl, sunDir[1] / dl, sunDir[2] / dl)
        };
        shaderUniforms.uWetGloss = { value: 0.30 * wx.gloss };
    }
    const shaderOpts = { marks: !!marks, wet: wetOn, uniforms: shaderUniforms };
    surfaceShader(surfaceMat, shaderOpts);
    surfaceShader(markMat, shaderOpts);

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

    // время суток: всё, кроме разметки, приглушается и холоднеет
    for (const key in colors) {
        if (key === 'line' || key === 'white') {
            const c = colors[key];
            c.setRGB(Math.min(1, c.r * tod.lineK), Math.min(1, c.g * tod.lineK), Math.min(1, c.b * tod.lineK));
        } else {
            todShade(colors[key], tod);
        }
    }

    // нижняя отметка мира: сюда садится и внешнее кольцо рельефа, и плоскость
    // грунта — стык получается без шва
    let minY = Infinity;
    let maxY = -Infinity;
    for (let i = 0; i < T.count; i++) {
        if (T.y[i] < minY) minY = T.y[i];
        if (T.y[i] > maxY) maxY = T.y[i];
    }
    const groundY = minY - pal.terrainDrop - 2.0;

    const plan = planSegments(T);
    const uvOn = !!marks || wetOn;   // параметризация ленты нужна обеим темам
    // Полотно, бордюры и лента рельефа делят ОДИН материал, поэтому и меш у
    // них один: иначе нарезка на сегменты утроила бы число вызовов там, где
    // раньше был один. Разметке нужен свой материал (polygonOffset), она
    // остаётся отдельным сегментным мешем.
    const surface = buildSurface(T, P, colors, pal, groundY, surfaceMat, ao, plan, uvOn, wx);
    const markings = buildMarkings(T, P, colors, markMat, ao, plan, uvOn);
    const ground = buildGround(T, colors, groundY, surfaceMat, uvOn);
    const arch = buildStartArch(T, P, colors, surfaceMat, ao, uvOn);
    // Трамплины: один меш на все, и только если они на трассе есть. На карте
    // без трамплинов не появляется ни вызова отрисовки, ни треугольника.
    const ramps = buildRamps(T, colors, pal, surfaceMat, ao, uvOn, wx);

    if (light) {
        // Порядок важен: затенение уже лежит в цветах, свет домножается сверху,
        // и только после этого нормали становятся не нужны.
        bakeVertexLight(surface.mesh.geometry, light);
        bakeVertexLight(markings.mesh.geometry, light, { emissive: lineEmissive });
        bakeVertexLight(ground.geometry, light);
        bakeVertexLight(arch.geometry, light);
        if (ramps) bakeVertexLight(ramps.geometry, light);
    }

    // Сегментные поверхности: отсекаются посегментно каждый кадр.
    const segmented = [surface, markings];
    group.add(ground, surface.mesh, markings.mesh, arch);
    if (ramps) group.add(ramps);

    const api = {
        group: group,
        surface: surface.mesh,
        road: surface.mesh,
        kerbs: surface.mesh,
        markings: markings.mesh,
        terrain: surface.mesh,
        ground: ground,
        arch: arch,
        /** Меш трамплинов или null, если их на трассе нет. */
        ramps: ramps,
        segments: plan.count,
        materials: { surface: surfaceMat, markings: markMat },
        bounds: { minY: minY, maxY: maxY, groundY: groundY },
        timeOfDay: todName,
        weather: weatherName,
        wet: wetOn,
        baked: !!light,
        /** Карта следов (или null). Штампует в неё renderer.js. */
        marks: marks,

        /**
         * Отсечение сегментов по пирамиде видимости. Зовётся раз в кадр из
         * renderer.js, ПОСЛЕ обновления матриц камеры. Ноль аллокаций.
         */
        cull: function (culler) {
            let shown = 0;
            for (let i = 0; i < segmented.length; i++) {
                segmented[i].cull(culler);
                shown += segmented[i].visibleSegments;
            }
            api.stats.visibleSegments = shown;
        },

        stats: {
            drawCalls: countDrawCalls(group),
            triangles: countTriangles(group),
            visibleSegments: plan.count * segmented.length
        },
        dispose: function () {
            disposeObject(group);
            // 12.11: текстуры обязаны возвращаться в ноль, а карту следов
            // disposeObject не видит — она живёт в uniform, а не в material.map
            if (marks) marks.dispose();
        }
    };
    return api;
}

// ---------------------------------------------------------------------------
// Полотно
// ---------------------------------------------------------------------------

/**
 * Полотно, бордюры и лента рельефа в ОДНОМ сегментном меше.
 *
 * Три поверхности делят материал, поэтому их выгодно резать вместе: один
 * сегмент — один непрерывный диапазон вершин, в котором лежит и асфальт, и
 * бордюры, и трава этого куска трассы.
 */
function buildSurface(T, P, colors, pal, groundY, material, ao, plan, uvOn, wx) {
    const b = new MeshBuilder();
    if (uvOn) b.enableUV();
    const road = roadWriter(T, P, colors, ao, uvOn, wx);
    const kerbs = kerbWriter(T, P, colors, ao, uvOn, wx);
    const terrain = terrainWriter(T, P, colors, pal, groundY, ao, plan, uvOn, wx);

    const starts = new Int32Array(plan.count);
    const counts = new Int32Array(plan.count);
    for (let k = 0; k < plan.count; k++) {
        starts[k] = b.vertexCount;
        road(b, plan.row[k], plan.row[k + 1]);
        kerbs(b, plan.row[k], plan.row[k + 1]);
        terrain(b, k);
        counts[k] = b.vertexCount - starts[k];
    }

    const geom = b.build();
    const spheres = segmentSpheres(geom, starts, counts);
    return new SegmentedSurface('trackSurface', geom, material, starts, counts, spheres);
}

// ---------------------------------------------------------------------------
// Полотно
// ---------------------------------------------------------------------------

/** Замыкание, пишущее ряды полотна [i0, i1] в переданный построитель. */
function roadWriter(T, P, colors, ao, uvOn, wx) {
    const rng = new Rng(T.seed ^ 0x51ed2701);
    const cols = P.roadCols + 1;
    // заранее посчитанный шум на УЗЕЛ сетки: цвет полотна не должен «мигать»
    // при повторной сборке, поэтому берём его из детерминированного ГПСЧ
    const jitter = new Float32Array(T.count * cols);
    for (let i = 0; i < jitter.length; i++) jitter[i] = 1 + (rng.next() * 2 - 1) * 0.05;

    const patch = new THREE.Color();
    let base = 0;

    function point(ii, j, out) {
        const i = (base + ii) % T.count;
        const u = (-1 + (2 * j) / P.roadCols) * T.hw[i];
        out[0] = T.x[i] + T.nx[i] * u;
        out[1] = T.y[i];
        out[2] = T.z[i] + T.nz[i] * u;
    }

    // Цвет считается на УЗЕЛ, а не на квад: только так тёмная полоса у
    // бордюра получается плавной, а не ступенькой в одну колонку.
    function vertexColor(ii, j, c) {
        const i = (base + ii) % T.count;
        const edge = Math.abs(-1 + (2 * j) / P.roadCols); // 0 в центре, 1 у кромки
        // траектория по центру чуть темнее (резина), кромки светлее (пыль)
        mixColor(patch, colors.asphaltDark, colors.asphalt, 0.25 + 0.75 * edge);
        let k = jitter[i * cols + j];
        if (ao) {
            // запечённый контакт с бордюром и обочиной
            k *= roadEdgeAO(T.hw[i] * (1 - edge));
        }
        // мокрое полотно темнее и холоднее: диффузного рассеяния меньше,
        // остальное уходит в зеркальный блик (его считает шейдер)
        k *= wx.roadK;
        const t = wx.roadTint;
        c.setRGB(
            Math.min(1, patch.r * k * t[0]),
            Math.min(1, patch.g * k * t[1]),
            Math.min(1, patch.b * k * t[2])
        );
    }

    // Параметризация ленты: v — путь по кругу, u — смещение поперёк.
    // v берётся из НЕЗАВЁРНУТОГО индекса, иначе замыкающий ряд последнего
    // сегмента получил бы v = 0 и вся карта размазалась бы по одному квaду.
    function uv(ii, j, out) {
        const lateral = (-1 + (2 * j) / P.roadCols) * T.hw[(base + ii) % T.count];
        out[0] = 0.5 + (0.5 * lateral) / (T.hw[(base + ii) % T.count] * MARK_EXTENT);
        out[1] = (base + ii) / T.count;
    }

    return function (b, i0, i1) {
        base = i0;
        surfaceGrid(b, {
            rows: i1 - i0 + 1, // +1: замыкающий ряд общий с соседним сегментом
            cols: cols,
            closed: false,
            point: point,
            vertexColor: vertexColor,
            uv: uvOn ? uv : null
        });
    };
}

// ---------------------------------------------------------------------------
// Трамплины
// ---------------------------------------------------------------------------

/**
 * Точка осевой линии на произвольной дуге s: индекс дробный, соседние выборки
 * смешиваются. Без этого въезд длиной 6 м лёг бы на три узла сетки и получил
 * бы ступеньки там, где физика считает гладкий уклон.
 * Заполняет out = [x, y, z, nx, nz] и возвращает его.
 */
function rampFrame(T, s, out) {
    const n = T.count;
    const total = T.length;
    let ss = s % total;
    if (ss < 0) ss += total;
    const f = ss / T.step;
    let i0 = Math.floor(f);
    const t = f - i0;
    i0 = ((i0 % n) + n) % n;
    const i1 = i0 + 1 < n ? i0 + 1 : 0;
    const nx = T.nx[i0] + (T.nx[i1] - T.nx[i0]) * t;
    const nz = T.nz[i0] + (T.nz[i1] - T.nz[i0]) * t;
    const len = Math.sqrt(nx * nx + nz * nz) || 1;
    out[0] = T.x[i0] + (T.x[i1] - T.x[i0]) * t;
    out[1] = T.y[i0] + (T.y[i1] - T.y[i0]) * t;
    out[2] = T.z[i0] + (T.z[i1] - T.z[i0]) * t;
    out[3] = nx / len;
    out[4] = nz / len;
    out[5] = T.hw[i0] + (T.hw[i1] - T.hw[i0]) * t;
    return out;
}

/** Высота полотна трамплина: зеркало Track.ramp_height, но по дуге и смещению. */
function rampDeckHeight(ramp, ds, u) {
    if (ds <= 0 || ds > ramp.length) return 0;
    let du = u - ramp.offset;
    if (du < 0) du = -du;
    const edge = ramp.half_width - du;
    if (edge <= 0) return 0;
    let h = ramp.slope * ds;
    if (edge < RAMP_EDGE_W) h *= edge / RAMP_EDGE_W;
    return h;
}

/**
 * Один меш на все трамплины трассы: полосатый коридор подхода, полотно
 * въезда, бордюрные рейки по краям и тёмная стенка за кромкой вылета.
 * Возвращает null, если трамплинов на трассе нет, — тогда и лишнего вызова
 * отрисовки не появляется.
 */
function buildRamps(T, colors, pal, material, ao, uvOn, wx) {
    const ramps = T.ramps;
    if (!ramps || !ramps.length) return null;

    const b = new MeshBuilder();
    if (uvOn) b.enableUV();

    const warnA = toColor(pal.archTrim);      // предупреждающая полоса
    const warnB = toColor(pal.asphaltDark);   // тёмная полоса между ними
    todShadeLike(warnA, colors);
    const railA = colors.kerbA;
    const railB = colors.kerbB;
    const railSide = colors.kerbSide;
    const lipFace = colors.dark;

    const fa = [0, 0, 0, 0, 0, 0];
    const fb = [0, 0, 0, 0, 0, 0];
    const pa = [0, 0, 0];
    const pb = [0, 0, 0];
    const pc = [0, 0, 0];
    const pd = [0, 0, 0];
    const uva = [0, 0];
    const uvb = [0, 0];
    const uvc = [0, 0];
    const uvd = [0, 0];

    // uv ленты — та же параметризация, что у полотна: карта следов шин
    // продолжается по трамплину, иначе колея обрывалась бы на въезде
    function setUV(out, s, u, hw) {
        out[0] = 0.5 + (0.5 * u) / (hw * MARK_EXTENT);
        out[1] = (s / T.length) % 1;
    }

    function put(sA, sB, uA, uB, hA0, hA1, hB0, hB1, lift, color) {
        rampFrame(T, sA, fa);
        rampFrame(T, sB, fb);
        pa[0] = fa[0] + fa[3] * uA; pa[1] = fa[1] + hA0 + lift; pa[2] = fa[2] + fa[4] * uA;
        pb[0] = fb[0] + fb[3] * uA; pb[1] = fb[1] + hB0 + lift; pb[2] = fb[2] + fb[4] * uA;
        pc[0] = fb[0] + fb[3] * uB; pc[1] = fb[1] + hB1 + lift; pc[2] = fb[2] + fb[4] * uB;
        pd[0] = fa[0] + fa[3] * uB; pd[1] = fa[1] + hA1 + lift; pd[2] = fa[2] + fa[4] * uB;
        if (uvOn) {
            setUV(uva, sA, uA, fa[5]);
            setUV(uvb, sB, uA, fb[5]);
            setUV(uvc, sB, uB, fb[5]);
            setUV(uvd, sA, uB, fa[5]);
            b.quadVC(pa, pb, pc, pd, color, color, color, color, uva, uvb, uvc, uvd);
        } else {
            b.quad(pa, pb, pc, pd, color);
        }
    }

    // вертикальная грань поперёк трассы, лицом ВПЕРЁД по ходу движения
    function wall(s, uA, uB, y0, y1, color) {
        rampFrame(T, s, fa);
        pa[0] = fa[0] + fa[3] * uB; pa[1] = fa[1] + y0; pa[2] = fa[2] + fa[4] * uB;
        pb[0] = fa[0] + fa[3] * uB; pb[1] = fa[1] + y1; pb[2] = fa[2] + fa[4] * uB;
        pc[0] = fa[0] + fa[3] * uA; pc[1] = fa[1] + y1; pc[2] = fa[2] + fa[4] * uA;
        pd[0] = fa[0] + fa[3] * uA; pd[1] = fa[1] + y0; pd[2] = fa[2] + fa[4] * uA;
        if (uvOn) {
            setUV(uva, s, uB, fa[5]);
            setUV(uvb, s, uB, fa[5]);
            setUV(uvc, s, uA, fa[5]);
            setUV(uvd, s, uA, fa[5]);
            b.quadVC(pa, pb, pc, pd, color, color, color, color, uva, uvb, uvc, uvd);
        } else {
            b.quad(pa, pb, pc, pd, color);
        }
    }

    const tint = new THREE.Color();

    for (let k = 0; k < ramps.length; k++) {
        const r = ramps[k];
        const s0 = r.s0;
        const sLip = r.s0 + r.length;
        const uL = r.offset - r.half_width;
        const uR = r.offset + r.half_width;

        // --- 1. коридор подхода: полосы поперёк, видны издалека
        const aSteps = Math.max(2, Math.round(RAMP_APPROACH / RAMP_APRON_STEP));
        for (let i = 0; i < aSteps; i++) {
            const sA = s0 - RAMP_APPROACH + (RAMP_APPROACH * i) / aSteps;
            const sB = s0 - RAMP_APPROACH + (RAMP_APPROACH * (i + 1)) / aSteps;
            // чем ближе к въезду, тем плотнее тёмные полосы — «внимание»
            const near = (i + 1) / aSteps;
            mixColor(tint, warnB, warnA, i % 2 ? 0.85 : 0.1 + 0.25 * near);
            put(sA, sB, uL, uR, 0, 0, 0, 0, RAMP_APRON_LIFT, tint);
        }

        // --- 2. полотно въезда. Высота берётся тем же профилем, что в физике,
        //        включая боковой скос RAMP_EDGE_W: колесо и картинка живут
        //        на одной поверхности.
        const dSteps = Math.max(3, Math.round(r.length / RAMP_DECK_STEP));
        const cols = 6;
        for (let i = 0; i < dSteps; i++) {
            const sA = s0 + (r.length * i) / dSteps;
            const sB = s0 + (r.length * (i + 1)) / dSteps;
            const dsA = sA - s0;
            const dsB = sB - s0;
            const band = Math.floor(dsA / (RAMP_BAND * 0.35)) % 2;
            for (let j = 0; j < cols; j++) {
                const uA = uL + ((uR - uL) * j) / cols;
                const uB = uL + ((uR - uL) * (j + 1)) / cols;
                mixColor(tint, warnB, warnA, band ? 0.9 : 0.12);
                put(sA, sB, uA, uB,
                    rampDeckHeight(r, dsA, uA), rampDeckHeight(r, dsA, uB),
                    rampDeckHeight(r, dsB, uA), rampDeckHeight(r, dsB, uB),
                    0, tint);
            }
        }

        // --- 3. стенка за кромкой вылета: силуэт трамплина, а не обрыв дороги
        wall(sLip, uL, uR, 0, r.rise, lipFace);

        // --- 4. бордюрные рейки по обеим кромкам, от начала коридора до
        //        кромки вылета. Это объём: он читается там, где плоская
        //        разметка уже сливается с асфальтом.
        const railSteps = Math.max(
            4, Math.round((RAMP_APPROACH + r.length) / RAMP_RAIL_BAND));
        for (let side = 0; side < 2; side++) {
            const inner = side ? uR : uL;
            const outer = side ? uR + RAMP_RAIL_W : uL - RAMP_RAIL_W;
            for (let i = 0; i < railSteps; i++) {
                const sA = s0 - RAMP_APPROACH
                    + ((RAMP_APPROACH + r.length) * i) / railSteps;
                const sB = s0 - RAMP_APPROACH
                    + ((RAMP_APPROACH + r.length) * (i + 1)) / railSteps;
                const baseA = Math.max(0, rampDeckHeight(r, sA - s0, inner));
                const baseB = Math.max(0, rampDeckHeight(r, sB - s0, inner));
                const col = i % 2 ? railA : railB;
                // верх рейки
                put(sA, sB, inner, outer,
                    baseA + RAMP_RAIL_H, baseA + RAMP_RAIL_H,
                    baseB + RAMP_RAIL_H, baseB + RAMP_RAIL_H, 0, col);
                // внутренняя и внешняя щёки: без них рейка «просвечивает»
                railSide2(b, T, sA, sB, inner, baseA, baseB, RAMP_RAIL_H,
                          railSide, side === 0);
                railSide2(b, T, sA, sB, outer, baseA, baseB, RAMP_RAIL_H,
                          railSide, side === 1);
            }
        }
    }

    const geom = b.build();
    const mesh = new THREE.Mesh(geom, material);
    mesh.name = 'trackRamps';
    mesh.frustumCulled = true;
    return mesh;
}

/** Вертикальная щека рейки вдоль трассы на смещении u. */
function railSide2(b, T, sA, sB, u, hA, hB, height, color, flip) {
    const fa = rampFrame(T, sA, [0, 0, 0, 0, 0, 0]);
    const ax = fa[0] + fa[3] * u, ay = fa[1], az = fa[2] + fa[4] * u;
    const fb = rampFrame(T, sB, [0, 0, 0, 0, 0, 0]);
    const bx = fb[0] + fb[3] * u, by = fb[1], bz = fb[2] + fb[4] * u;
    const p0 = [ax, ay + hA, az];
    const p1 = [ax, ay + hA + height, az];
    const p2 = [bx, by + hB + height, bz];
    const p3 = [bx, by + hB, bz];
    if (flip) b.quad(p0, p1, p2, p3, color);
    else b.quad(p3, p2, p1, p0, color);
}

/** Приглушить цвет под уже посчитанную палитру (время суток уже в colors). */
function todShadeLike(color, colors) {
    // colors.asphalt уже прошёл time-of-day и погоду; берём из него общий
    // коэффициент яркости и применяем к предупреждающему цвету, чтобы
    // трамплин ночью не светился неоном посреди тёмной трассы
    const k = (colors.asphalt.r + colors.asphalt.g + colors.asphalt.b) / 0.72;
    const m = Math.min(1.0, Math.max(0.35, k));
    color.setRGB(color.r * m, color.g * m, color.b * m);
}

function segmentSpheres(geom, starts, counts) {
    const pos = geom.attributes.position.array;
    const out = new Float32Array(starts.length * 4);
    for (let k = 0; k < starts.length; k++) {
        rangeSphere(pos, starts[k], counts[k], out, k * 4);
    }
    return out;
}

// ---------------------------------------------------------------------------
// Бордюры
// ---------------------------------------------------------------------------

/** Замыкание, пишущее бордюры обеих кромок для рядов [i0, i1]. */
function kerbWriter(T, P, colors, ao, uvOn, wx) {
    const tmp = new THREE.Color();

    // профиль в ортах (u — наружу от кромки, v — вверх от полотна)
    const profileOut = P.kerbSide
        ? [[0.0, 0.005], [KERB_WIDTH * 0.85, 0.075], [KERB_WIDTH, -0.35]]
        : [[0.0, 0.005], [KERB_WIDTH, 0.06]];

    let base = 0;
    let sgn = 1;

    function frame(ii, f) {
        const i = (base + ii) % T.count;
        f[0] = T.x[i] + T.nx[i] * T.hw[i] * sgn;
        f[1] = T.y[i];
        f[2] = T.z[i] + T.nz[i] * T.hw[i] * sgn;
        f[3] = T.nx[i] * sgn;
        f[4] = 0;
        f[5] = T.nz[i] * sgn;
        f[6] = 0;
        f[7] = 1;
        f[8] = 0;
    }

    function color(ii, k, c) {
        const i = (base + ii) % T.count;
        if (k === 1) {
            // наружная стенка смотрит вниз и в грунт — там темнее всего
            shade(c, colors.kerbSide, (ao ? 0.52 : 1) * wx.kerbK);
        } else {
            // чередование посегментно: шаг выборки 2 м, полоса = 2 м
            // Цвет на квад, а не на узел: иначе красно-белая шашка
            // расплылась бы в градиент и перестала читаться.
            tmp.copy((i & 1) === 0 ? colors.kerbA : colors.kerbB);
            shade(c, tmp, (ao ? 0.88 : 1) * wx.kerbK);
        }
    }

    // бордюр лежит сразу за кромкой полотна: u чуть больше единичной отметки
    function uv(ii, j, out) {
        const i = (base + ii) % T.count;
        const lateral = (T.hw[i] + profileOut[j][0]) * sgn;
        out[0] = 0.5 + (0.5 * lateral) / (T.hw[i] * MARK_EXTENT);
        out[1] = (base + ii) / T.count;
    }

    return function (b, i0, i1) {
        for (let side = 0; side < 2; side++) {
            sgn = side === 0 ? 1 : -1;
            base = i0;
            // для левой стороны профиль отражается, поэтому выворачиваем обход
            extrudeProfile(b, {
                count: i1 - i0 + 1,
                closed: false,
                flip: sgn < 0,
                profile: profileOut,
                frame: frame,
                color: color,
                uv: uvOn ? uv : null
            });
        }
    };
}

// ---------------------------------------------------------------------------
// Разметка
// ---------------------------------------------------------------------------

function buildMarkings(T, P, colors, material, ao, plan, uvOn) {
    const b = new MeshBuilder();
    if (uvOn) b.enableUV();
    const lift = MARK_LIFT;
    const lineC = new THREE.Color();
    // кромочная линия лежит внутри тёмной полосы у бордюра: если оставить её
    // белой, запечённый контакт разрежется пополам яркой чертой
    const edgeLineK = ao ? roadEdgeAO(0.36) : 1;
    shade(lineC, colors.line, edgeLineK);

    let base = 0;
    let sgn = 1;

    // кромочная полоса строится сеткой 2 колонки: внутренняя и внешняя кромка
    function edgePoint(ii, j, out) {
        const i = (base + ii) % T.count;
        const u = (T.hw[i] - (j === 0 ? 0.55 : 0.18)) * sgn;
        out[0] = T.x[i] + T.nx[i] * u;
        out[1] = T.y[i] + lift;
        out[2] = T.z[i] + T.nz[i] * u;
    }

    function edgeColor(ii, j, c) {
        c.copy(lineC);
    }

    // разметка лежит на полотне и обязана пачкаться вместе с ним: та же
    // параметризация, тот же материал по программе — значит и следы те же
    function edgeUV(ii, j, out) {
        const i = (base + ii) % T.count;
        const lateral = (T.hw[i] - (j === 0 ? 0.55 : 0.18)) * sgn;
        out[0] = 0.5 + (0.5 * lateral) / (T.hw[i] * MARK_EXTENT);
        out[1] = (base + ii) / T.count;
    }

    // осевая прерывистая: три отрезка через три пропуска (6 м штрих / 6 м пусто)
    const dashOn = 3;
    const dashPeriod = 6;
    const half = 0.14;

    const starts = new Int32Array(plan.count);
    const counts = new Int32Array(plan.count);
    for (let k = 0; k < plan.count; k++) {
        starts[k] = b.vertexCount;
        const i0 = plan.row[k];
        const i1 = plan.row[k + 1];

        // кромочные линии по обеим сторонам, сплошные
        if (P.edgeLines) {
            for (let side = 0; side < 2; side++) {
                sgn = side === 0 ? 1 : -1;
                base = i0;
                surfaceGrid(b, {
                    rows: i1 - i0 + 1,
                    cols: 2,
                    closed: false,
                    flip: sgn < 0,
                    point: edgePoint,
                    color: edgeColor,
                    uv: uvOn ? edgeUV : null
                });
            }
        }

        for (let i = i0; i < i1; i++) {
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
            if (uvOn) b.setUV(0.5, i / T.count);
            b.quadRaw(
                ax, y0, az,
                dx, y1, dz,
                cx, y1, cz,
                bx, y0, bz,
                colors.line.r, colors.line.g, colors.line.b
            );
        }

        // стартовая клетка живёт в выборке 0, то есть в самом первом сегменте
        if (k === 0) {
            if (uvOn) b.setUV(0.5, 0);
            buildStartLine(b, T, colors, lift);
        }

        counts[k] = b.vertexCount - starts[k];
    }

    const geom = b.build();
    const spheres = segmentSpheres(geom, starts, counts);
    const surf = new SegmentedSurface('trackMarkings', geom, material, starts, counts, spheres);
    surf.mesh.renderOrder = 1;
    return surf;
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
 * off — расстояние наружу от кромки полотна в метрах, side — 0 (сторона положительной нормали, то есть левый борт по разделу 4) или 1.
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

/** Замыкание, пишущее ленту рельефа сегмента k (обе стороны). */
function terrainWriter(T, P, colors, pal, groundY, ao, plan, uvOn, wx) {
    const tp = terrainParams(T, P, pal, groundY);
    const rings = P.terrainRings;
    // Прореживание по длине круга снято: лента отсекается посегментно,
    // и её цена больше не зависит от длины трассы.
    const stride = P.terrainStride;
    const rowsAll = Math.max(3, Math.floor(T.count / stride));

    const jitRng = new Rng((T.seed ^ 0x77aa3311) >>> 0);
    const patch = new THREE.Color();
    const grassJit = new Float32Array(rowsAll * rings);
    for (let i = 0; i < grassJit.length; i++) grassJit[i] = 1 + (jitRng.next() * 2 - 1) * 0.09;

    // затенение кольца: у самой кромки полотна темно, дальше светлеет
    const ringAO = new Float32Array(rings);
    for (let j = 0; j < rings; j++) {
        ringAO[j] = ao ? 1 - AO_SHOULDER * smooth01(1 - tp.offs[j] / AO_SHOULDER_W) : 1;
    }

    // Границы сегментов в рядах ленты: те же места дуги, что и у полотна.
    // Сегментов ровно столько же — лента живёт в общем меше с полотном.
    const segCount = plan.count;
    const tRow = new Int32Array(segCount + 1);
    tRow[0] = 0;
    for (let k = 1; k < segCount; k++) {
        let v = Math.round(plan.row[k] / stride);
        if (v < tRow[k - 1] + 1) v = tRow[k - 1] + 1;
        if (v > rowsAll - (segCount - k)) v = rowsAll - (segCount - k);
        tRow[k] = v;
    }
    tRow[segCount] = rowsAll;

    let base = 0;
    let side = 0;
    let sgn = 1;

    function point(ii, j, out) {
        const row = (base + ii) % rowsAll;
        const i = (row * stride) % T.count;
        const u = (T.hw[i] + tp.offs[j]) * sgn;
        out[0] = T.x[i] + T.nx[i] * u;
        out[1] = ringHeight(T, tp, i, j, side);
        out[2] = T.z[i] + T.nz[i] * u;
    }

    // цвет на узел: тёмная кайма у обочины переходит в траву плавно
    function vertexColor(ii, j, c) {
        const row = (base + ii) % rowsAll;
        const i = (row * stride) % T.count;
        let k = grassJit[row * rings + j] * ringAO[j];
        if (ao) {
            // канава ниже полотна дополнительно затенена
            const drop = T.y[i] - ringHeight(T, tp, i, j, side);
            if (drop > 0) k *= 1 - AO_DITCH * smooth01(drop / 6);
        }
        // мокрая трава темнеет слабее асфальта и не блестит вовсе
        k *= wx.grassK;
        if (j === 0) {
            shade(c, colors.shoulder, k);
            return;
        }
        const t = j / (rings - 1);
        mixColor(patch, colors.groundNear, colors.groundFar, t);
        shade(c, patch, k);
    }

    // Ближняя обочина попадает в ту же карту следов, что и полотно, — это
    // и есть «следы на траве» из исследования: та же текстура, та же выборка,
    // ноль дополнительной цены. Дальние кольца уходят за охват карты, их u
    // зажимается в пустую кайму.
    function uv(ii, j, out) {
        const row = (base + ii) % rowsAll;
        const i = (row * stride) % T.count;
        const lateral = (T.hw[i] + tp.offs[j]) * sgn;
        out[0] = 0.5 + (0.5 * lateral) / (T.hw[i] * MARK_EXTENT);
        out[1] = ((base + ii) * stride) / T.count;
    }

    return function (b, k) {
        for (side = 0; side < 2; side++) {
            sgn = side === 0 ? 1 : -1;
            base = tRow[k];
            surfaceGrid(b, {
                rows: tRow[k + 1] - base + 1, // замыкающий ряд общий с соседом
                cols: rings,
                closed: false,
                flip: sgn < 0,
                point: point,
                vertexColor: vertexColor,
                uv: uvOn ? uv : null
            });
        }
    };
}

// ---------------------------------------------------------------------------
// Общий грунт до горизонта
// ---------------------------------------------------------------------------

function buildGround(T, colors, groundY, material, uvOn) {
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
    // uv (0, 0) — крайний столбец карты следов, он всегда пустой, и маска
    // мокрого блика там тоже ноль: грунт и арка остаются как были
    if (uvOn) b.enableUV();
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
    mesh.frustumCulled = false; // два треугольника под всей картой
    mesh.matrixAutoUpdate = false;
    mesh.updateMatrix();
    return mesh;
}

// ---------------------------------------------------------------------------
// Стартовая арка
// ---------------------------------------------------------------------------

function buildStartArch(T, P, colors, material, ao, uvOn) {
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
    if (uvOn) b.enableUV();
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

    const archGeom = b.build();
    if (ao) {
        // основания опор тонут в тени, верх перекладины видит всё небо
        bakeContactAO(archGeom, { base: py, height: 3.2, floor: 0.5, power: 0.7, sky: 0.3 });
    }
    const mesh = new THREE.Mesh(archGeom, material);
    mesh.name = 'trackArch';
    mesh.matrixAutoUpdate = false;
    mesh.updateMatrix();
    // Арка — один компактный объект у линии старта: её отсекает сам three.js
    // по ограничивающей сфере геометрии, своей нарезки тут не нужно.
    mesh.frustumCulled = true;
    return mesh;
}

export default buildTrackMeshes;
