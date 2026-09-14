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
//   off 9   u8    car_count
//   далее car_count записей по 26 байт:
//     u8 slot, u8 flags, f32 x, f32 z, f32 yaw, f32 vx, f32 vz,
//     u8 lap, u8 place, i8 steer_q (-127..127 <-> -1..1), u8 drift_charge (charge*100)
//   u8 proj_count
//   далее proj_count записей по 15 байт:
//     u16 id, u8 kind, f32 x, f32 z, f32 yaw
//   u8 box_mask_len
//   box_mask_len байт маски активных боксов:
//     бокс i -> байт i >> 3, бит i & 7 (младший бит — первый бокс)
//
// buttons:   0 газ, 1 тормоз/задний ход, 2 влево, 3 вправо,
//            4 дрифт (ручник), 5 применить бонус, 6 взгляд назад, 7 резерв
// car flags: 0 вне трассы, 1 дрифтует, 2 ускорение, 3 крутит (урон),
//            4 щит, 5 финишировал, 6 призрак (отключился), 7 тормозит
//
// Размер снапшота = 10 + 26*car_count + 1 + 15*proj_count + 1 + box_mask_len.
// Для 8 машин, 4 снарядов и маски в 2 байта это 282 байта (5,6 КБ/с на клиента
// при 20 Гц). В DESIGN.md §5.3 в итоговой сумме стоит 265 — та сумма не сходится
// с собственным списком полей: в ней снаряд посчитан как 11 байт (потерян f32 yaw)
// и не учтён байт box_mask_len. Здесь реализован список полей, он первичен.
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
export const ACK_OFFSET = 5;

export const MAX_CARS = 8;            // мест в гонке
export const MAX_PROJECTILES = 32;    // потолок буфера снарядов
export const MAX_BOX_MASK_LEN = 32;   // 32 байта маски = до 256 боксов

const STEER_SCALE = 1 / 127;          // -127..127 -> -1..1
const DRIFT_CHARGE_SCALE = 1 / 100;   // байт -> секунды заряда дрифта

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
export function snapshotSize(carCount, projCount, boxMaskLen) {
    return SNAPSHOT_HEADER_SIZE
        + carCount * SNAPSHOT_CAR_SIZE
        + 1 + projCount * SNAPSHOT_PROJ_SIZE
        + 1 + boxMaskLen;
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

    const carCount = view.getUint8(9);
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

    out.tick = view.getUint32(1, true);
    out.ackSeq = view.getUint32(ACK_OFFSET, true);
    out.carCount = carCount;
    out.projCount = projCount;
    out.boxMaskLen = maskLen;

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

    out.valid = true;
    return true;
}
