// Физика аркадной машины: один шаг симуляции, раздел 6 контракта.
//
// Это ЗЕРКАЛО game/physics.py. Порядок операций, имена констант и формулы
// совпадают построчно, у каждого блока проставлен номер шага из раздела 6.2 —
// файлы положены рядом и сравниваются глазами за минуту. Правишь здесь —
// правь и там, иначе предсказание разъедется с сервером и картинку задёргает.
//
// Ограничения раздела 6, соблюдены буквально:
//   * внутри шага нет Math.exp, Math.pow, Math.atan2 и оператора ** — все
//     затухания заданы сразу «на шаг» при фиксированном dt = 1/60;
//   * внутри шага ноль аллокаций: ни массивов, ни объектов, ни замыканий,
//     ни деструктуризации. Состояние — плоские поля объекта из createCarState;
//   * высота (y) на физику не влияет, всё считается в плоскости (x, z).
//
// Имена полей состояния здесь в camelCase (driftCharge, boostTime, sampleIdx),
// как и имена методов трассы в разделе 7.3 (nearestIndex, clampToTrack).
// Соответствие полям раздела 6.1 — один в один, см. createCarState.
//
// Подкрученные константы и места, где контракт пришлось дотолковать,
// перечислены в докстринге game/physics.py. Значения обоих файлов совпадают.

// ---------------------------------------------------------------------------
// КОНСТАНТЫ (раздел 6.4)
// Все затухания — «на шаг» при DT = 1/60. Менять DT нельзя, не пересчитав их.
// ---------------------------------------------------------------------------

export const DT = 1.0 / 60.0;              // фиксированный шаг симуляции, с

export const STEER_RATE = 4.5;             // набор угла руля, 1/с (было 3.2)
export const STEER_RETURN = 6.5;           // возврат руля в ноль, 1/с (было 5.0)
export const STEER_MAX = 1.0;              // предел |steer| (шаг 3)

export const TURN_FULL_SPEED = 9.0;        // м/с, полная поворотливость (было 12.0)
export const TURN_FALLOFF = 0.45;          // срез поворота на max_speed

export const DRIFT_TURN_GAIN = 1.7;        // множитель поворота в заносе (было 1.55)
export const DRIFT_MIN_SPEED = 7.0;        // м/с, ниже занос не начинается (было 8.0)
export const DRIFT_STEER_MIN = 0.35;       // порог |steer| для заноса (раздел 6.3)

export const DRIFT_CHARGE_L1 = 0.7;        // с заряда -> уровень 1, синие искры
export const DRIFT_CHARGE_L2 = 1.4;        // с заряда -> уровень 2, оранжевые
export const DRIFT_CHARGE_L3 = 2.4;        // с заряда -> уровень 3, фиолетовые
export const DRIFT_BOOST_L1 = 0.7;         // с ускорения за уровень 1
export const DRIFT_BOOST_L2 = 1.2;         // с ускорения за уровень 2
export const DRIFT_BOOST_L3 = 1.8;         // с ускорения за уровень 3

export const SPIN_RATE = 9.0;              // рад/с раскрутки после урона
export const BOOST_ACCEL = 22.0;           // м/с², подтягивание к boostSpeed
export const SLOW_FACTOR = 0.995;          // на шаг, пока slowTime > 0 (было 0.985)
export const OFFTRACK_FACTOR = 0.992;      // на шаг вне трассы (было 0.985)

export const BRAKE_REVERSE_SPEED = 0.5;    // м/с, ниже тормоз — это задний ход
export const REVERSE_MAX_SPEED = 9.0;      // м/с, потолок заднего хода

export const WALL_BOUNCE = 0.35;           // доля нормальной скорости после стены
export const COLLISION_PUSH = 0.6;         // доля перекрытия, снимаемая за шаг
export const COLLISION_RESTITUTION = 0.35; // упругость обмена импульсом
export const CAR_RADIUS = 1.1;             // м, круг столкновений

// Биты ввода. Значения обязаны совпадать с BTN_* из static/js/protocol.js;
// продублированы здесь, чтобы физика не тянула за собой сетевой модуль.
export const BTN_THROTTLE = 1 << 0;
export const BTN_BRAKE = 1 << 1;
export const BTN_LEFT = 1 << 2;
export const BTN_RIGHT = 1 << 3;
export const BTN_DRIFT = 1 << 4;

// Тот же набор одним объектом — для HUD, настроек и отладочного оверлея.
export const CAR_CONSTANTS = Object.freeze({
    DT,
    STEER_RATE,
    STEER_RETURN,
    STEER_MAX,
    TURN_FULL_SPEED,
    TURN_FALLOFF,
    DRIFT_TURN_GAIN,
    DRIFT_MIN_SPEED,
    DRIFT_STEER_MIN,
    DRIFT_CHARGE_L1,
    DRIFT_CHARGE_L2,
    DRIFT_CHARGE_L3,
    DRIFT_BOOST_L1,
    DRIFT_BOOST_L2,
    DRIFT_BOOST_L3,
    SPIN_RATE,
    BOOST_ACCEL,
    SLOW_FACTOR,
    OFFTRACK_FACTOR,
    BRAKE_REVERSE_SPEED,
    REVERSE_MAX_SPEED,
    WALL_BOUNCE,
    COLLISION_PUSH,
    COLLISION_RESTITUTION,
    CAR_RADIUS,
});

/**
 * Состояние машины, раздел 6.1. Фабрика, а не класс: объект создаётся один раз
 * на машину и дальше только переписывается по полям, в кадровом цикле
 * аллокаций нет. Поля offtrack, lap и checkpoint контракт не называет —
 * см. докстринг game/physics.py, пункты 4 и 5.
 */
export function createCarState(x, z, yaw) {
    return {
        x: x || 0.0,              // позиция, м
        z: z || 0.0,
        yaw: yaw || 0.0,          // курс, рад; 0 — нос в +Z
        vx: 0.0,                  // скорость в мировых координатах, м/с
        vz: 0.0,
        steer: 0.0,               // текущий угол руля, -1..1
        driftCharge: 0.0,         // накопленный заряд дрифта, с
        driftActive: false,       // идёт ли занос
        boostTime: 0.0,           // остаток ускорения, с
        spinTime: 0.0,            // остаток раскрутки после урона, с
        shieldTime: 0.0,          // остаток щита, с
        slowTime: 0.0,            // остаток замедления, с
        progress: 0.0,            // накопленный путь по трассе, м
        sampleIdx: 0,             // индекс ближайшей точки осевой линии
        offtrack: false,          // вне трассы (выставляет clampToTrack)
        lap: 0,                   // пройдено полных кругов (advanceProgress)
        checkpoint: 0,            // индекс следующей ожидаемой отсечки
    };
}

/** Поставить машину на решётку: позиция и курс, всё остальное в ноль. */
export function resetCarState(state, x, z, yaw) {
    state.x = x;
    state.z = z;
    state.yaw = yaw;
    state.vx = 0.0;
    state.vz = 0.0;
    state.steer = 0.0;
    state.driftCharge = 0.0;
    state.driftActive = false;
    state.boostTime = 0.0;
    state.spinTime = 0.0;
    state.shieldTime = 0.0;
    state.slowTime = 0.0;
    state.progress = 0.0;
    state.sampleIdx = 0;
    state.offtrack = false;
    state.lap = 0;
    state.checkpoint = 0;
}

/** Скопировать чужое состояние поверх своего (реконсиляция, раздел 10.2). */
export function copyCarState(dst, src) {
    dst.x = src.x;
    dst.z = src.z;
    dst.yaw = src.yaw;
    dst.vx = src.vx;
    dst.vz = src.vz;
    dst.steer = src.steer;
    dst.driftCharge = src.driftCharge;
    dst.driftActive = src.driftActive;
    dst.boostTime = src.boostTime;
    dst.spinTime = src.spinTime;
    dst.shieldTime = src.shieldTime;
    dst.slowTime = src.slowTime;
    dst.progress = src.progress;
    dst.sampleIdx = src.sampleIdx;
    dst.offtrack = src.offtrack;
    dst.lap = src.lap;
    dst.checkpoint = src.checkpoint;
}

/**
 * Характеристики машины из content/cars.json (раздел 6.5) в объект с теми же
 * именами полей, что читает step. Зовётся один раз при race_init.
 */
export function createCarStats(stats) {
    return {
        engineForce: stats.engine_force,
        maxSpeed: stats.max_speed,
        brakeForce: stats.brake_force,
        reverseForce: stats.reverse_force,
        turnRate: stats.turn_rate,
        gripStep: stats.grip_step,
        driftGripStep: stats.drift_grip_step,
        drag: stats.drag,
        roll: stats.roll,
        boostSpeed: stats.boost_speed,
        mass: stats.mass,
    };
}

/**
 * Один шаг физики одной машины. Порядок операций — раздел 6.2, дословно.
 *
 * buttons — битовая маска ввода (раздел 5.2), track — объект с интерфейсом
 * раздела 7.3, hint — индекс точки осевой линии, вокруг которого трасса ищет
 * ближайшую (обычно state.sampleIdx).
 *
 * Возвращает уровень ускорения за занос, полученного ИМЕННО на этом шаге:
 * 0 — ничего, 1..3 — уровень из раздела 6.3 (по нему рисуются искры и звук).
 * Аллокаций нет: ответ — маленькое целое.
 */
export function step(state, carStats, buttons, dt, track, hint) {
    // --- состояние и характеристики в локальные имена
    let yaw = state.yaw;
    let steer = state.steer;
    let spinTime = state.spinTime;
    let boostTime = state.boostTime;
    let slowTime = state.slowTime;
    let shieldTime = state.shieldTime;
    const driftActive = state.driftActive;
    const driftCharge = state.driftCharge;
    let vx = state.vx;
    let vz = state.vz;

    const maxSpeed = carStats.maxSpeed;
    const boostSpeed = carStats.boostSpeed;
    const turnRate = carStats.turnRate;

    let btnGas = buttons & BTN_THROTTLE;
    let btnBrake = buttons & BTN_BRAKE;
    let btnLeft = buttons & BTN_LEFT;
    let btnRight = buttons & BTN_RIGHT;
    let btnDrift = buttons & BTN_DRIFT;

    // шаг 1: раскрутка после урона глушит ввод и крутит машину
    let spinning;
    if (spinTime > 0.0) {
        btnGas = 0;
        btnBrake = 0;
        btnLeft = 0;
        btnRight = 0;
        btnDrift = 0;
        yaw += SPIN_RATE * dt;
        spinTime -= dt;
        if (spinTime < 0.0) {
            spinTime = 0.0;
        }
        spinning = true;
    } else {
        spinning = false;
    }
    // шаг 1, расширение: контракт нигде не уменьшает щит, делаем это здесь
    if (shieldTime > 0.0) {
        shieldTime -= dt;
        if (shieldTime < 0.0) {
            shieldTime = 0.0;
        }
    }

    // шаг 2: целевой угол руля
    let steerTarget = 0.0;
    if (btnLeft) {
        steerTarget += 1.0;
    }
    if (btnRight) {
        steerTarget -= 1.0;
    }

    // шаг 3: руль тянется к цели, без ввода — возвращается в ноль
    let steerStep;
    if (steerTarget !== 0.0) {
        steerStep = STEER_RATE * dt;
    } else {
        steerStep = STEER_RETURN * dt;
    }
    const steerDelta = steerTarget - steer;
    if (steerDelta > steerStep) {
        steer += steerStep;
    } else if (steerDelta < -steerStep) {
        steer -= steerStep;
    } else {
        steer = steerTarget;
    }
    if (steer > STEER_MAX) {
        steer = STEER_MAX;
    } else if (steer < -STEER_MAX) {
        steer = -STEER_MAX;
    }

    // шаг 4: базис машины (раздел 4: ноль yaw — нос в +Z)
    const fwdX = Math.sin(yaw);
    const fwdZ = Math.cos(yaw);
    const rightX = fwdZ;
    const rightZ = -fwdX;

    // шаг 5: разложение скорости на продольную и боковую
    let vFwd = vx * fwdX + vz * fwdZ;
    let vLat = vx * rightX + vz * rightZ;

    // шаг 6: продольная сила (газ / тормоз / задний ход)
    let accel;
    if (btnGas) {
        // maxSpeedEff: под ускорением потолок поднимается до boostSpeed
        if (boostTime > 0.0) {
            accel = carStats.engineForce * (1.0 - vFwd / boostSpeed);
        } else {
            accel = carStats.engineForce * (1.0 - vFwd / maxSpeed);
        }
        if (accel < 0.0) {
            accel = 0.0;
        }
    } else if (btnBrake) {
        if (vFwd > BRAKE_REVERSE_SPEED) {
            accel = -carStats.brakeForce;
        } else {
            // задний ход гаснет к REVERSE_MAX_SPEED тем же видом формулы,
            // что и газ: vFwd здесь отрицательный
            let reverseK = 1.0 + vFwd / REVERSE_MAX_SPEED;
            if (reverseK < 0.0) {
                reverseK = 0.0;
            }
            accel = -carStats.reverseForce * reverseK;
        }
    } else {
        accel = 0.0;
    }
    vFwd += accel * dt;

    // шаг 7: ускорение от бонуса (и от заноса — уровни 1..3)
    if (boostTime > 0.0) {
        const boostDelta = boostSpeed - vFwd;
        const boostStep = BOOST_ACCEL * dt;
        if (boostDelta > boostStep) {
            vFwd += boostStep;
        } else if (boostDelta < -boostStep) {
            vFwd -= boostStep;
        } else {
            vFwd = boostSpeed;
        }
        boostTime -= dt;
        if (boostTime < 0.0) {
            boostTime = 0.0;
        }
    }

    // шаг 8: замедление от «Грозы»
    if (slowTime > 0.0) {
        vFwd *= SLOW_FACTOR;
        slowTime -= dt;
        if (slowTime < 0.0) {
            slowTime = 0.0;
        }
    }

    // шаг 9: поворот
    let absFwd = vFwd < 0.0 ? -vFwd : vFwd;
    let speedFactor = absFwd / TURN_FULL_SPEED;
    if (speedFactor > 1.0) {
        speedFactor = 1.0;
    }
    const overSpeed = absFwd - TURN_FULL_SPEED;
    let falloff;
    if (overSpeed <= 0.0) {
        falloff = 1.0;
    } else {
        const overSpan = maxSpeed - TURN_FULL_SPEED;
        let overT;
        if (overSpan > 0.0) {
            overT = overSpeed / overSpan;
            if (overT > 1.0) {
                overT = 1.0;
            }
        } else {
            overT = 1.0;
        }
        falloff = 1.0 - TURN_FALLOFF * overT;
    }
    let turn = steer * turnRate * speedFactor * falloff;
    if (vFwd < 0.0) {
        turn = -turn;            // задним ходом руль работает наоборот
    }
    if (driftActive) {
        turn *= DRIFT_TURN_GAIN;
    }
    yaw += turn * dt;

    // шаг 10: боковое сцепление (в заносе оно резко ниже)
    if (driftActive) {
        vLat *= 1.0 - carStats.driftGripStep;
    } else {
        vLat *= 1.0 - carStats.gripStep;
    }

    // шаг 11: сопротивление; вне трассы дополнительно вязнем
    absFwd = vFwd < 0.0 ? -vFwd : vFwd;
    vFwd -= (carStats.drag * vFwd * absFwd + carStats.roll * vFwd) * dt;
    if (state.offtrack) {
        vFwd *= OFFTRACK_FACTOR;
    }

    // шаг 12: сборка скорости обратно в мировые координаты
    vx = fwdX * vFwd + rightX * vLat;
    vz = fwdZ * vFwd + rightZ * vLat;

    // шаг 13: интегрирование позиции
    const x = state.x + vx * dt;
    const z = state.z + vz * dt;

    // состояние в поля до обращения к трассе: шаги 14 и 16 правят его на месте
    state.x = x;
    state.z = z;
    state.vx = vx;
    state.vz = vz;
    state.yaw = yaw;
    state.steer = steer;
    state.spinTime = spinTime;
    state.shieldTime = shieldTime;
    state.slowTime = slowTime;
    state.boostTime = boostTime;

    // шаг 14: границы трассы, выталкивание и гашение по WALL_BOUNCE
    track.clampToTrack(state, hint);

    // (столкновения машина-машина считает только сервер, между шагами 14 и 15;
    //  клиент их не предсказывает, в шаг они не входят)

    // шаг 15: заряд дрифта и награда за занос (раздел 6.3)
    let level = 0;
    if (spinning) {
        // раскрутило — занос сбит, заряд сгорает без награды
        if (driftActive) {
            state.driftActive = false;
        }
        if (driftCharge !== 0.0) {
            state.driftCharge = 0.0;
        }
    } else if (btnDrift && vFwd > DRIFT_MIN_SPEED
               && (steer > DRIFT_STEER_MIN || steer < -DRIFT_STEER_MIN)) {
        if (!driftActive) {
            state.driftActive = true;
        }
        state.driftCharge = driftCharge + dt;
    } else if (driftActive) {
        // ручник отпущен, руль выпрямлен или скорость потеряна
        let reward;
        if (driftCharge >= DRIFT_CHARGE_L3) {
            level = 3;
            reward = DRIFT_BOOST_L3;
        } else if (driftCharge >= DRIFT_CHARGE_L2) {
            level = 2;
            reward = DRIFT_BOOST_L2;
        } else if (driftCharge >= DRIFT_CHARGE_L1) {
            level = 1;
            reward = DRIFT_BOOST_L1;
        } else {
            reward = 0.0;
        }
        // новый буст не укорачивает уже идущий
        if (reward > boostTime) {
            state.boostTime = reward;
        }
        state.driftActive = false;
        state.driftCharge = 0.0;
    }

    // шаг 16: progress, круги и отсечки
    track.advanceProgress(state, hint);

    return level;
}

/**
 * Столкновения машина-машина, круг-круг радиуса CAR_RADIUS.
 *
 * На клиенте НЕ вызывается (раздел 6.2: столкновения считает только сервер) и
 * намеренно не является частью step. Экспортируется, чтобы зеркало было полным
 * и чтобы одиночные офлайн-прогоны в браузере вели себя как сервер.
 *
 * cars — массив состояний, stats — параллельный ему массив характеристик
 * (нужна mass), count — сколько первых элементов участвует.
 */
export function resolveCollisions(cars, stats, count) {
    const diameter = CAR_RADIUS + CAR_RADIUS;
    const minDistSq = diameter * diameter;
    const last = count - 1;
    for (let i = 0; i < last; i++) {
        const a = cars[i];
        const massA = stats[i].mass;
        for (let j = i + 1; j < count; j++) {
            const b = cars[j];
            const dx = b.x - a.x;
            const dz = b.z - a.z;
            const distSq = dx * dx + dz * dz;
            if (distSq >= minDistSq) {
                continue;
            }
            const massB = stats[j].mass;
            let nx;
            let nz;
            let overlap;
            if (distSq > 1e-9) {
                const dist = Math.sqrt(distSq);
                const inv = 1.0 / dist;
                nx = dx * inv;
                nz = dz * inv;
                overlap = diameter - dist;
            } else {
                // машины строго в одной точке — расталкиваем по оси X,
                // направление зависит только от индексов, значит
                // результат детерминирован
                nx = 1.0;
                nz = 0.0;
                overlap = diameter;
            }

            // расталкивание, тяжёлую машину двигаем меньше
            const totalMass = massA + massB;
            const push = overlap * COLLISION_PUSH / totalMass;
            const pushA = push * massB;
            const pushB = push * massA;
            a.x -= nx * pushA;
            a.z -= nz * pushA;
            b.x += nx * pushB;
            b.z += nz * pushB;

            // обмен импульсом вдоль нормали, с затуханием
            const relN = (b.vx - a.vx) * nx + (b.vz - a.vz) * nz;
            if (relN < 0.0) {       // только если реально сближаются
                const invA = 1.0 / massA;
                const invB = 1.0 / massB;
                const impulse = -(1.0 + COLLISION_RESTITUTION) * relN / (invA + invB);
                a.vx -= nx * impulse * invA;
                a.vz -= nz * impulse * invA;
                b.vx += nx * impulse * invB;
                b.vz += nz * impulse * invB;
            }
        }
    }
}
