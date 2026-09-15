// Бинарный сетевой протокол гонки: коды сообщений, упаковка ввода, разбор снапшота.
// Зеркало game/protocol.py. Шпаргалка по layout ниже продублирована в обоих
// файлах слово в слово: правишь здесь — правь и там.
//
// Использование на клиенте:
//
//   import { encodeInput, createSnapshotBuffer, decodeSnapshot } from './protocol.js';
//
//   const snap = createSnapshotBuffer();          // один раз при старте
//   ws.binaryType = 'arraybuffer';
//   ws.onmessage = (e) => {
//       if (typeof e.data === 'string') { handleJson(e.data); return; }
//       if (decodeSnapshot(e.data, snap)) applySnapshot(snap);
//   };
//   // каждый шаг предсказания, 60 Гц:
//   ws.send(encodeInput(seq, buttons));           // буфер переиспользуемый
//
// encodeInput отдаёт один и тот же ArrayBuffer при каждом вызове: WebSocket.send
// копирует данные синхронно, поэтому переиспользование безопасно. Не держите
// ссылку на этот буфер дольше вызова send.
//
// decodeSnapshot пишет в заранее выделенные типизированные массивы объекта out
// и не создаёт ни массивов, ни объектов. Единственный объект, который может
// возникнуть при разборе, — DataView на входящий ArrayBuffer (каждое сообщение
// WebSocket приходит новым буфером, обойти это нельзя); он кэшируется в out
// и пересоздаётся только при смене буфера, то есть 20 раз в секунду, вне
// кадрового цикла. В самом кадровом цикле чтение из out аллокаций не делает.

// ---------------------------------------------------------------------------
// ШПАРГАЛКА ПО ФОРМАТУ ПАКЕТОВ
// (идентична в game/protocol.py и static/js/protocol.js — правишь одно, правь второе)
//
// Всё little-endian, без выравнивания.
//
// Ввод, клиент -> сервер, 7 байт:
//   off 0   u8    type = 0x01 (MSG_INPUT)
//   off 1   u32   seq           номер шага клиента, монотонно растёт
//   off 5   u8    buttons       битовая маска кнопок
//   off 6   u8    reserved = 0
//
// Снапшот, сервер -> клиент, заголовок 10 байт:
//   off 0   u8    type = 0x10 (MSG_SNAPSHOT)
//   off 1   u32   tick          номер тика сервера
//   off 5   u32   ack_seq       ACK_OFFSET: единственное поле, зависящее от клиента
//   off 9   u8    car_count     младшие 7 бит — число машин (0..8),
//                               бит 7 (SNAPSHOT_FLAG_EXTRA) — есть ли за
//                               маской боксов секции траффика и происшествий
//   далее car_count записей по 26 байт:
//     u8 slot, u8 flags, f32 x, f32 z, f32 yaw, f32 vx, f32 vz,
//     u8 lap, u8 place, i8 steer_q (-127..127 <-> -1..1), u8 drift_charge (charge*100)
//   u8 proj_count
//   далее proj_count записей по 15 байт:
//     u16 id, u8 kind, f32 x, f32 z, f32 yaw
//   u8 box_mask_len
//   box_mask_len байт маски активных боксов:
//     бокс i -> байт i >> 3, бит i & 7 (младший бит — первый бокс)
//   --- дальше только если в car_count поднят бит 7 (FLAG_EXTRA) ---
//   u8 traffic_count
//   далее traffic_count записей по 6 байт (машины-болванки):
//     u8 ident   младшие 4 бита — id в пуле, старшие 4 — вид (силуэт и цвет)
//     u16 s_q    положение вдоль дуги трассы, s * 65535 / length
//     i8 lat_q   смещение от оси, 0.1 м на единицу (+-12.7 м)
//     i8 dyaw_q  курс МИНУС курс касательной трассы, pi/127 на единицу
//     u8 spd_q   модуль скорости, 0.25 м/с на единицу (0..63.75)
//   u8 event_count
//   далее event_count записей по 8 байт (происшествия на дороге):
//     u8 id, u8 kind, u8 phase,
//     u16 s_q    центр вдоль дуги, как у траффика
//     i8 lat_q   центр поперёк, 0.1 м
//     u8 hl_q    полудлина вдоль трассы, 0.25 м на единицу
//     u8 hw_q    полуширина поперёк трассы, 0.1 м на единицу
//
// buttons:   0 газ, 1 тормоз/задний ход, 2 влево, 3 вправо,
//            4 дрифт (ручник), 5 применить бонус, 6 взгляд назад, 7 резерв
// car flags: 0 вне трассы, 1 дрифтует, 2 ускорение, 3 крутит (урон),
//            4 щит, 5 финишировал, 6 призрак (отключился), 7 тормозит
//
// Размер снапшота = 10 + 26*car_count + 1 + 15*proj_count + 1 + box_mask_len,
// и ЕСЛИ есть траффик или происшествия, ещё + 1 + 6*traffic_count
//                                            + 1 + 8*event_count.
// Для 8 машин, 4 снарядов и маски в 2 байта без траффика и происшествий это
// ровно прежние 282 байта, байт в байт: выключенная настройка не стоит НИ
// ОДНОГО лишнего байта, за это и отвечает флаг в car_count. Плотный траффик
// (12 машин) добавляет 1 + 72 = 73 байта, шесть происшествий — ещё 1 + 48 = 49.
// Потолок наполнения: 404 байта, 8,1 КБ/с против 5,6 КБ/с у пустого.
// В DESIGN.md §5.3 в итоговой сумме стоит 265 — та сумма не сходится
// с собственным списком полей: в ней снаряд посчитан как 11 байт (потерян f32 yaw)
// и не учтён байт box_mask_len. Здесь реализован список полей, он первичен.
//
// ПОЧЕМУ ТРАФФИК НЕ ЕДЕТ ОБЫЧНОЙ ЗАПИСЬЮ МАШИНЫ (26 байт). Запись гонщика
// несёт то, чего у болванки нет и не будет: круг, место, заряд заноса, угол
// руля, вектор скорости в мировых координатах. Болванка же по построению
// держится трассы, поэтому её положение описывается дугой и смещением от оси
// точнее и вчетверо дешевле: 6 байт против 26. Двенадцать болванок стоят
// 72 байта — столько же, сколько ТРИ записи гонщиков. Потолок MAX_CARS = 8
// при этом не тронут: гонщики и траффик — разные массивы.
// ---------------------------------------------------------------------------

// --- коды сообщений --------------------------------------------------------

export const MSG_INPUT = 0x01;
export const MSG_SNAPSHOT = 0x10;

// --- маска кнопок (раздел 5.2) ---------------------------------------------

export const BTN_THROTTLE = 1 << 0;   // газ
export const BTN_BRAKE = 1 << 1;      // тормоз / задний ход
export const BTN_LEFT = 1 << 2;       // влево
export const BTN_RIGHT = 1 << 3;      // вправо
export const BTN_DRIFT = 1 << 4;      // дрифт (ручник)
export const BTN_ITEM = 1 << 5;       // применить бонус
export const BTN_LOOK_BACK = 1 << 6;  // взгляд назад
export const BTN_RESERVED = 1 << 7;   // резерв

// --- флаги машины (раздел 5.3) ---------------------------------------------

export const FLAG_OFFTRACK = 1 << 0;  // вне трассы
export const FLAG_DRIFTING = 1 << 1;  // дрифтует
export const FLAG_BOOST = 1 << 2;     // ускорение активно
export const FLAG_SPIN = 1 << 3;      // крутит (получил урон)
export const FLAG_SHIELD = 1 << 4;    // под щитом
export const FLAG_FINISHED = 1 << 5;  // финишировал
export const FLAG_GHOST = 1 << 6;     // отключился, машина-призрак
export const FLAG_BRAKING = 1 << 7;   // тормозит (стоп-сигналы)

// --- размеры и потолки -----------------------------------------------------

export const INPUT_SIZE = 7;
export const SNAPSHOT_HEADER_SIZE = 10;
export const SNAPSHOT_CAR_SIZE = 26;
export const SNAPSHOT_PROJ_SIZE = 15;
export const SNAPSHOT_TRAFFIC_SIZE = 6;
export const SNAPSHOT_EVENT_SIZE = 8;
export const ACK_OFFSET = 5;

export const MAX_CARS = 8;            // мест в гонке
export const MAX_PROJECTILES = 32;    // потолок буфера снарядов
export const MAX_BOX_MASK_LEN = 32;   // 32 байта маски = до 256 боксов
// Бит 7 поля car_count: за маской боксов идут секции траффика и происшествий.
// Нужен ради обратной совместимости по байтам — комната с выключенными
// траффиком и происшествиями шлёт ровно тот же пакет, что и раньше.
export const SNAPSHOT_FLAG_EXTRA = 0x80;
export const SNAPSHOT_CAR_COUNT_MASK = 0x7F;

export const MAX_TRAFFIC = 12;        // машин-болванок в снапшоте
export const TRAFFIC_LOOKS = 12;      // видов болванки (силуэт + цвет)
export const MAX_ROAD_EVENTS = 6;     // одновременных происшествий

// Виды происшествий и их фазы — зеркало game/events.py.
export const ROAD_EXPLOSION = 1;
export const ROAD_OIL = 2;
export const ROAD_WRECK = 3;
export const ROAD_BLOCKADE = 4;

export const ROAD_PHASE_WARN = 0;      // маяки стоят, физики нет
export const ROAD_PHASE_ACTIVE = 1;    // действует
export const ROAD_PHASE_CLEARING = 2;  // убирается, физики уже нет
export const ROAD_PHASE_DEBRIS = 3;    // обломки после взрыва

const STEER_SCALE = 1 / 127;          // -127..127 -> -1..1
const DRIFT_CHARGE_SCALE = 1 / 100;   // байт -> секунды заряда дрифта
const LATERAL_SCALE = 1 / 10;         // i8 -> метры смещения от оси
const ANGLE_SCALE = Math.PI / 127;    // i8 -> радианы
const SPEED_SCALE = 1 / 4;            // u8 -> м/с
const HALF_LENGTH_SCALE = 1 / 4;      // u8 -> метры полудлины
const HALF_WIDTH_SCALE = 1 / 10;      // u8 -> метры полуширины
export const ARC_SCALE = 1 / 65535;   // u16 -> доля круга (умножить на length)

// --- ввод: клиент -> сервер ------------------------------------------------

// Один буфер на всё время жизни страницы: ввод уходит 60 раз в секунду,
// аллокация ArrayBuffer на каждый кадр недопустима.
const inputBuffer = new ArrayBuffer(INPUT_SIZE);
const inputView = new DataView(inputBuffer);
inputView.setUint8(0, MSG_INPUT);
inputView.setUint8(6, 0);

/**
 * Упаковать ввод в 7 байт. Возвращает переиспользуемый ArrayBuffer —
 * отдавать его нужно сразу в ws.send(), копию он не делает.
 */
export function encodeInput(seq, buttons) {
    inputView.setUint32(1, seq >>> 0, true);
    inputView.setUint8(5, buttons & 0xFF);
    return inputBuffer;
}

// Хелперы кнопок — зеркало has_* из protocol.py.

export function hasThrottle(buttons) { return (buttons & BTN_THROTTLE) !== 0; }
export function hasBrake(buttons) { return (buttons & BTN_BRAKE) !== 0; }
export function hasLeft(buttons) { return (buttons & BTN_LEFT) !== 0; }
export function hasRight(buttons) { return (buttons & BTN_RIGHT) !== 0; }
export function hasDrift(buttons) { return (buttons & BTN_DRIFT) !== 0; }
export function hasItem(buttons) { return (buttons & BTN_ITEM) !== 0; }
export function hasLookBack(buttons) { return (buttons & BTN_LOOK_BACK) !== 0; }

// --- флаги машины ----------------------------------------------------------

export function isOffTrack(flags) { return (flags & FLAG_OFFTRACK) !== 0; }
export function isDrifting(flags) { return (flags & FLAG_DRIFTING) !== 0; }
export function isBoosting(flags) { return (flags & FLAG_BOOST) !== 0; }
export function isSpinning(flags) { return (flags & FLAG_SPIN) !== 0; }
export function hasShield(flags) { return (flags & FLAG_SHIELD) !== 0; }
export function hasFinished(flags) { return (flags & FLAG_FINISHED) !== 0; }
export function isGhost(flags) { return (flags & FLAG_GHOST) !== 0; }
export function isBraking(flags) { return (flags & FLAG_BRAKING) !== 0; }

/** Заряд дрифта из байта снапшота обратно в секунды (для выбора цвета искр). */
export function driftChargeSeconds(raw) { return raw * DRIFT_CHARGE_SCALE; }

/** Активен ли бокс с бонусом по разобранной маске. */
export function isBoxActive(out, boxId) {
    const byteIndex = boxId >> 3;
    if (byteIndex < 0 || byteIndex >= out.boxMaskLen) return false;
    return ((out.boxMask[byteIndex] >> (boxId & 7)) & 1) === 1;
}

/** Размер снапшота в байтах при заданном наполнении. */
export function snapshotSize(carCount, projCount, boxMaskLen,
                             trafficCount, eventCount) {
    let size = SNAPSHOT_HEADER_SIZE
        + carCount * SNAPSHOT_CAR_SIZE
        + 1 + projCount * SNAPSHOT_PROJ_SIZE
        + 1 + boxMaskLen;
    const traf = trafficCount || 0;
    const evt = eventCount || 0;
    if (traf || evt) {
        size += 1 + traf * SNAPSHOT_TRAFFIC_SIZE + 1 + evt * SNAPSHOT_EVENT_SIZE;
    }
    return size;
}

// --- снапшот: сервер -> клиент ---------------------------------------------

/**
 * Создать структуру-приёмник снапшота. Вызывается ОДИН раз при старте:
 * все массивы фиксированного размера, дальше decodeSnapshot только
 * перезаписывает их содержимое.
 *
 * Поля:
 *   tick, ackSeq            заголовок
 *   carCount                сколько машин в снапшоте (<= MAX_CARS)
 *   carSlot[i]              слот игрока 0..7
 *   carFlags[i]             битовые флаги, читать хелперами isDrifting и т.д.
 *   carX/carZ/carYaw        позиция и курс
 *   carVx/carVz             скорость в мировых координатах
 *   carLap[i]               пройдено полных кругов
 *   carPlace[i]             место 1..8
 *   carSteer[i]             угол руля -1..1 (уже разквантован)
 *   carDriftCharge[i]       заряд дрифта 0..255, в секунды — driftChargeSeconds
 *   indexBySlot[slot]       индекс машины в массивах или -1, если слота нет
 *   projCount               сколько снарядов (<= MAX_PROJECTILES)
 *   projId/projKind         идентификатор и вид снаряда (таблица бонусов)
 *   projX/projZ/projYaw     позиция и курс снаряда
 *   boxMaskLen, boxMask     маска активных боксов, читать через isBoxActive
 *   valid                   успешен ли последний разбор
 *
 * Траффик (машины-болванки). Положение приходит в координатах ТРАССЫ, а не
 * мира: дуга в долях круга (умножить на track.length), смещение от оси в
 * метрах и разница курса с касательной. Мировую точку восстанавливает net.js
 * по той же осевой линии, по которой её считал сервер.
 *   trafCount, trafId[i], trafLook[i], trafArc[i] (доля круга 0..1),
 *   trafLat[i], trafDyaw[i], trafSpeed[i]
 *
 * Происшествия на дороге — так же в координатах трассы:
 *   evtCount, evtId[i], evtKind[i], evtPhase[i], evtArc[i] (доля круга),
 *   evtLat[i], evtHalfLen[i], evtHalfWidth[i]
 *
 * ГОТОВЫЕ К ОТРИСОВКЕ значения траффика и происшествий. Их заполняет net.js
 * в interpolate() — уже в мировых координатах и уже на момент показа, — а
 * читает renderer.applySnapshot(). Буфер снапшота и так единственный объект,
 * который main.js передаёт из сети в рендер; заводить ради болванок вторую
 * такую дорогу значило бы править main.js, который принадлежит другому
 * исполнителю. Массивы выделены здесь же, поэтому аллокаций в кадре нет.
 *   trafViewCount, trafViewLook[i], trafViewX/Y/Z/Yaw[i]
 *   evtViewCount, evtViewId/Kind/Phase[i], evtViewX/Y/Z/Yaw[i],
 *   evtViewHalfLen/HalfWidth[i]
 */
export function createSnapshotBuffer() {
    return {
        tick: 0,
        ackSeq: 0,
        valid: false,

        carCount: 0,
        carSlot: new Uint8Array(MAX_CARS),
        carFlags: new Uint8Array(MAX_CARS),
        carX: new Float32Array(MAX_CARS),
        carZ: new Float32Array(MAX_CARS),
        carYaw: new Float32Array(MAX_CARS),
        carVx: new Float32Array(MAX_CARS),
        carVz: new Float32Array(MAX_CARS),
        carLap: new Uint8Array(MAX_CARS),
        carPlace: new Uint8Array(MAX_CARS),
        carSteer: new Float32Array(MAX_CARS),
        carDriftCharge: new Uint8Array(MAX_CARS),
        indexBySlot: new Int8Array(MAX_CARS).fill(-1),

        projCount: 0,
        projId: new Uint16Array(MAX_PROJECTILES),
        projKind: new Uint8Array(MAX_PROJECTILES),
        projX: new Float32Array(MAX_PROJECTILES),
        projZ: new Float32Array(MAX_PROJECTILES),
        projYaw: new Float32Array(MAX_PROJECTILES),

        boxMaskLen: 0,
        boxMask: new Uint8Array(MAX_BOX_MASK_LEN),

        trafCount: 0,
        trafId: new Uint8Array(MAX_TRAFFIC),
        trafLook: new Uint8Array(MAX_TRAFFIC),
        trafArc: new Float32Array(MAX_TRAFFIC),
        trafLat: new Float32Array(MAX_TRAFFIC),
        trafDyaw: new Float32Array(MAX_TRAFFIC),
        trafSpeed: new Float32Array(MAX_TRAFFIC),

        evtCount: 0,
        evtId: new Uint8Array(MAX_ROAD_EVENTS),
        evtKind: new Uint8Array(MAX_ROAD_EVENTS),
        evtPhase: new Uint8Array(MAX_ROAD_EVENTS),
        evtArc: new Float32Array(MAX_ROAD_EVENTS),
        evtLat: new Float32Array(MAX_ROAD_EVENTS),
        evtHalfLen: new Float32Array(MAX_ROAD_EVENTS),
        evtHalfWidth: new Float32Array(MAX_ROAD_EVENTS),

        // --- готовое к отрисовке, заполняет net.js -------------------------
        trafViewCount: 0,
        trafViewLook: new Uint8Array(MAX_TRAFFIC),
        trafViewX: new Float32Array(MAX_TRAFFIC),
        trafViewY: new Float32Array(MAX_TRAFFIC),
        trafViewZ: new Float32Array(MAX_TRAFFIC),
        trafViewYaw: new Float32Array(MAX_TRAFFIC),

        evtViewCount: 0,
        evtViewId: new Uint8Array(MAX_ROAD_EVENTS),
        evtViewKind: new Uint8Array(MAX_ROAD_EVENTS),
        evtViewPhase: new Uint8Array(MAX_ROAD_EVENTS),
        evtViewX: new Float32Array(MAX_ROAD_EVENTS),
        evtViewY: new Float32Array(MAX_ROAD_EVENTS),
        evtViewZ: new Float32Array(MAX_ROAD_EVENTS),
        evtViewYaw: new Float32Array(MAX_ROAD_EVENTS),
        evtViewHalfLen: new Float32Array(MAX_ROAD_EVENTS),
        evtViewHalfWidth: new Float32Array(MAX_ROAD_EVENTS),

        // Кэш DataView на последний разобранный буфер, см. комментарий вверху.
        _src: null,
        _view: null,
    };
}

/**
 * Разобрать бинарный снапшот в заранее выделенную структуру out.
 * Принимает ArrayBuffer (как приходит из WebSocket при binaryType
 * 'arraybuffer') или готовый DataView. Новых объектов и массивов не создаёт.
 *
 * Возвращает true при успехе. При любом несоответствии формату возвращает
 * false и ставит out.valid = false, оставляя предыдущие данные нетронутыми:
 * битый пакет не должен сбивать интерполяцию.
 */
export function decodeSnapshot(data, out) {
    let view;
    if (data instanceof DataView) {
        view = data;
    } else {
        if (data !== out._src) {
            out._src = data;
            out._view = new DataView(data);
        }
        view = out._view;
    }

    const total = view.byteLength;
    if (total < SNAPSHOT_HEADER_SIZE || view.getUint8(0) !== MSG_SNAPSHOT) {
        out.valid = false;
        return false;
    }

    const header = view.getUint8(9);
    const carCount = header & SNAPSHOT_CAR_COUNT_MASK;
    const hasExtra = (header & SNAPSHOT_FLAG_EXTRA) !== 0;
    if (carCount > MAX_CARS) { out.valid = false; return false; }

    let need = SNAPSHOT_HEADER_SIZE + carCount * SNAPSHOT_CAR_SIZE + 1;
    if (total < need) { out.valid = false; return false; }

    const projCount = view.getUint8(need - 1);
    if (projCount > MAX_PROJECTILES) { out.valid = false; return false; }

    need += projCount * SNAPSHOT_PROJ_SIZE + 1;
    if (total < need) { out.valid = false; return false; }

    const maskLen = view.getUint8(need - 1);
    if (maskLen > MAX_BOX_MASK_LEN) { out.valid = false; return false; }

    need += maskLen;
    if (total < need) { out.valid = false; return false; }

    // Секции траффика и происшествий есть только если поднят флаг: пакет
    // комнаты без них обязан разбираться ровно как прежде.
    let trafCount = 0;
    let evtCount = 0;
    if (hasExtra) {
        need += 1;
        if (total < need) { out.valid = false; return false; }
        trafCount = view.getUint8(need - 1);
        if (trafCount > MAX_TRAFFIC) { out.valid = false; return false; }

        need += trafCount * SNAPSHOT_TRAFFIC_SIZE + 1;
        if (total < need) { out.valid = false; return false; }

        evtCount = view.getUint8(need - 1);
        if (evtCount > MAX_ROAD_EVENTS) { out.valid = false; return false; }

        need += evtCount * SNAPSHOT_EVENT_SIZE;
        if (total < need) { out.valid = false; return false; }
    }

    out.tick = view.getUint32(1, true);
    out.ackSeq = view.getUint32(ACK_OFFSET, true);
    out.carCount = carCount;
    out.projCount = projCount;
    out.boxMaskLen = maskLen;
    out.trafCount = trafCount;
    out.evtCount = evtCount;

    const indexBySlot = out.indexBySlot;
    for (let i = 0; i < MAX_CARS; i++) indexBySlot[i] = -1;

    let offset = SNAPSHOT_HEADER_SIZE;
    for (let i = 0; i < carCount; i++) {
        const slot = view.getUint8(offset);
        out.carSlot[i] = slot;
        out.carFlags[i] = view.getUint8(offset + 1);
        out.carX[i] = view.getFloat32(offset + 2, true);
        out.carZ[i] = view.getFloat32(offset + 6, true);
        out.carYaw[i] = view.getFloat32(offset + 10, true);
        out.carVx[i] = view.getFloat32(offset + 14, true);
        out.carVz[i] = view.getFloat32(offset + 18, true);
        out.carLap[i] = view.getUint8(offset + 22);
        out.carPlace[i] = view.getUint8(offset + 23);
        out.carSteer[i] = view.getInt8(offset + 24) * STEER_SCALE;
        out.carDriftCharge[i] = view.getUint8(offset + 25);
        if (slot < MAX_CARS) indexBySlot[slot] = i;
        offset += SNAPSHOT_CAR_SIZE;
    }

    offset += 1; // байт proj_count уже прочитан
    for (let i = 0; i < projCount; i++) {
        out.projId[i] = view.getUint16(offset, true);
        out.projKind[i] = view.getUint8(offset + 2);
        out.projX[i] = view.getFloat32(offset + 3, true);
        out.projZ[i] = view.getFloat32(offset + 7, true);
        out.projYaw[i] = view.getFloat32(offset + 11, true);
        offset += SNAPSHOT_PROJ_SIZE;
    }

    offset += 1; // байт box_mask_len уже прочитан
    const boxMask = out.boxMask;
    for (let i = 0; i < maskLen; i++) boxMask[i] = view.getUint8(offset + i);
    for (let i = maskLen; i < MAX_BOX_MASK_LEN; i++) boxMask[i] = 0;
    offset += maskLen;
    if (!hasExtra) {
        out.valid = true;
        return true;
    }

    offset += 1; // байт traffic_count уже прочитан
    for (let i = 0; i < trafCount; i++) {
        const ident = view.getUint8(offset);
        out.trafId[i] = ident & 0x0F;
        out.trafLook[i] = ident >> 4;
        out.trafArc[i] = view.getUint16(offset + 1, true) * ARC_SCALE;
        out.trafLat[i] = view.getInt8(offset + 3) * LATERAL_SCALE;
        out.trafDyaw[i] = view.getInt8(offset + 4) * ANGLE_SCALE;
        out.trafSpeed[i] = view.getUint8(offset + 5) * SPEED_SCALE;
        offset += SNAPSHOT_TRAFFIC_SIZE;
    }

    offset += 1; // байт event_count уже прочитан
    for (let i = 0; i < evtCount; i++) {
        out.evtId[i] = view.getUint8(offset);
        out.evtKind[i] = view.getUint8(offset + 1);
        out.evtPhase[i] = view.getUint8(offset + 2);
        out.evtArc[i] = view.getUint16(offset + 3, true) * ARC_SCALE;
        out.evtLat[i] = view.getInt8(offset + 5) * LATERAL_SCALE;
        out.evtHalfLen[i] = view.getUint8(offset + 6) * HALF_LENGTH_SCALE;
        out.evtHalfWidth[i] = view.getUint8(offset + 7) * HALF_WIDTH_SCALE;
        offset += SNAPSHOT_EVENT_SIZE;
    }

    out.valid = true;
    return true;
}
