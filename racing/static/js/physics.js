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
// driftActive и driftCharge имён не меняли, хотя кнопка теперь зовётся
// ручником: оба поля едут в снапшот и их читают рендер, HUD и звук.
//
// Четыре правки по итогам живого плейтеста (столкновения капсулой, ручник
// вместо дрифта, предел поперечного ускорения в шаге 9, поднятый потолок
// скорости), подкрученные константы и места, где контракт пришлось
// дотолковать, подробно расписаны в докстринге game/physics.py.
// Значения обоих файлов совпадают до последнего разряда.

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

// --- предел по сцеплению в шаге 9 ------------------------------------------
// Поперечное ускорение в повороте есть |vFwd * turn|. Потолок этой величины
// задаётся сцеплением машины: aLatMax = gripStep * GRIP_LAT_ACCEL. Отсюда
// скорость в повороте радиуса R равна sqrt(aLatMax * R), а не R * turnRate,
// то есть gripStep наконец что-то решает. Множитель подобран замером: при
// разбросе gripStep 0.158..0.190 он даёт 25.3..30.4 м/с² поперёк, то есть
// 20.1..22.1 м/с в связке радиусом 16 м и 31.8..34.9 м/с в дуге 40 м. Меньше —
// и сцепление начинает решать всё, машины перестают балансироваться; больше —
// и предела фактически нет, как было до правки.
export const GRIP_LAT_ACCEL = 160.0;       // м/с² поперёк на единицу gripStep
export const LAT_CAP_MIN_SPEED = 6.0;      // м/с: ниже предел не сужается

// --- ручной тормоз (в 6.3 и 6.4 он назван «дрифтом») -----------------------
// Занос — следствие ручника, а не название кнопки. Ручник: (а) доворачивает
// корму сверх того, что позволяет сцепление, (б) снимает боковое сцепление до
// driftGripStep, (в) умеренно тормозит. Усиление 1.55 из 6.4 на здешних
// поворотах (радиус 13..17,5 м) разворачивало машину вокруг оси; 1.18 плюс
// поднятый на HANDBRAKE_LAT_GAIN предел по сцеплению дают поворот в 1.45 раза
// круче, чем на сцеплении, ценой четверти скорости — занос, который держишь
// рулём, а не разворот.
export const HANDBRAKE_TURN_GAIN = 1.18;   // множитель поворота на ручнике (было 1.55)
export const HANDBRAKE_LAT_GAIN = 1.25;    // во сколько ручник поднимает предел
export const HANDBRAKE_DECEL = 9.0;        // м/с² продольного замедления
export const HANDBRAKE_MIN_SPEED = 7.0;    // м/с, ниже занос не начинается (было 8.0)
export const HANDBRAKE_MAX_SLIP = 0.36;    // потолок |vLat| / |vFwd|, ~20° (было 0.5)
export const HANDBRAKE_SLIDE_RECOVER = 0.7;// доля срезанного заноса обратно в vFwd
export const HANDBRAKE_CHARGE_SLIP = 0.12; // ниже этого скольжения заряд не копится

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

// --- габарит столкновений: капсула вдоль продольной оси ---------------------
// Кузова в content/cars.json: длина 3.7..4.95 м, ширина 1.80..2.02 м. Капсула
// одна на всех (характеристик формы в статистиках машины нет), взята по
// среднему кузову: отрезок 2*1.05 м с радиусом 0.95 м даёт габарит 4.00 x 1.90 м.
export const CAR_RADIUS = 0.95;            // м, радиус капсулы = полуширина кузова
export const CAR_AXIS_HALF = 1.05;         // м, полуотрезок капсулы вдоль оси

// Биты ввода. Значения обязаны совпадать с BTN_* из static/js/protocol.js;
// продублированы здесь, чтобы физика не тянула за собой сетевой модуль.
export const BTN_THROTTLE = 1 << 0;
export const BTN_BRAKE = 1 << 1;
export const BTN_LEFT = 1 << 2;
export const BTN_RIGHT = 1 << 3;
export const BTN_HANDBRAKE = 1 << 4;
export const BTN_DRIFT = BTN_HANDBRAKE;    // имя из раздела 5.2 и protocol.js

// Тот же набор одним объектом — для HUD, настроек и отладочного оверлея.
export const CAR_CONSTANTS = Object.freeze({
    DT,
    STEER_RATE,
    STEER_RETURN,
    STEER_MAX,
    TURN_FULL_SPEED,
    TURN_FALLOFF,
    GRIP_LAT_ACCEL,
    LAT_CAP_MIN_SPEED,
    HANDBRAKE_TURN_GAIN,
    HANDBRAKE_LAT_GAIN,
    HANDBRAKE_DECEL,
    HANDBRAKE_MIN_SPEED,
    HANDBRAKE_MAX_SLIP,
    HANDBRAKE_SLIDE_RECOVER,
    HANDBRAKE_CHARGE_SLIP,
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
    CAR_AXIS_HALF,
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
        driftCharge: 0.0,         // накопленный заряд заноса, с
        driftActive: false,       // идёт ли занос (ручник держит машину боком)
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
    const sliding = state.driftActive;   // занос, поднятый ручником на прошлом шаге
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
    let btnHandbrake = buttons & BTN_HANDBRAKE;

    // шаг 1: раскрутка после урона глушит ввод и крутит машину
    let spinning;
    if (spinTime > 0.0) {
        btnGas = 0;
        btnBrake = 0;
        btnLeft = 0;
        btnRight = 0;
        btnHandbrake = 0;
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
    // шаг 6, расширение: ручник тормозит. Без этого «ручной тормоз» только
    // снимал боковое сцепление и названию не соответствовал. Замедление
    // одинаково на переднем и заднем ходу и никогда не переворачивает знак.
    if (btnHandbrake) {
        const handStep = HANDBRAKE_DECEL * dt;
        if (vFwd > handStep) {
            vFwd -= handStep;
        } else if (vFwd < -handStep) {
            vFwd += handStep;
        } else {
            vFwd = 0.0;
        }
    }

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
    if (sliding) {
        turn *= HANDBRAKE_TURN_GAIN;
    }
    // шаг 9, расширение: предел по сцеплению. Поперечное ускорение в повороте
    // есть |vFwd * turn|; выше aLatMax машина просто не поворачивает. Отсюда
    // скорость в дуге радиуса R равна sqrt(aLatMax * R) — именно это делает
    // gripStep характеристикой, а не украшением карточки машины. На ручнике
    // потолок поднят: занос и нужен, чтобы повернуть круче, чем позволяет
    // сцепление.
    let latLimit = carStats.gripStep * GRIP_LAT_ACCEL;
    if (sliding) {
        latLimit *= HANDBRAKE_LAT_GAIN;
    }
    let turnCap;
    if (absFwd > LAT_CAP_MIN_SPEED) {
        turnCap = latLimit / absFwd;
    } else {
        turnCap = latLimit / LAT_CAP_MIN_SPEED;
    }
    if (turn > turnCap) {
        turn = turnCap;
    } else if (turn < -turnCap) {
        turn = -turnCap;
    }
    yaw += turn * dt;

    // шаг 10: боковое сцепление (на ручнике оно резко ниже)
    if (sliding) {
        vLat *= 1.0 - carStats.driftGripStep;
        // потолок угла скольжения: без него курс убегает от вектора скорости,
        // занос вырождается в раскрутку на месте и срывается сам (см. докстринг
        // game/physics.py). Срезанное не выбрасывается, а частью возвращается
        // в продольную скорость — так занос остаётся быстрым способом
        // пройти поворот
        const maxLat = HANDBRAKE_MAX_SLIP * (vFwd < 0.0 ? -vFwd : vFwd);
        if (vLat > maxLat) {
            vFwd += (vLat - maxLat) * HANDBRAKE_SLIDE_RECOVER;
            vLat = maxLat;
        } else if (vLat < -maxLat) {
            vFwd += (-vLat - maxLat) * HANDBRAKE_SLIDE_RECOVER;
            vLat = -maxLat;
        }
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

    // шаг 15: заряд заноса и награда за него (раздел 6.3)
    let level = 0;
    if (spinning) {
        // раскрутило — занос сбит, заряд сгорает без награды
        if (sliding) {
            state.driftActive = false;
        }
        if (driftCharge !== 0.0) {
            state.driftCharge = 0.0;
        }
    } else if (btnHandbrake && vFwd > HANDBRAKE_MIN_SPEED) {
        // требования «|steer| > 0.35» больше нет: ручник срабатывает от одного
        // пробела, руль нужен, чтобы заносом управлять, а не чтобы его начать
        if (!sliding) {
            state.driftActive = true;
        }
        // ...но заряд копится только за НАСТОЯЩИЙ занос. Иначе зажатый на
        // прямой пробел давал бы ускорение ни за что
        const absLat = vLat < 0.0 ? -vLat : vLat;
        if (absLat > HANDBRAKE_CHARGE_SLIP * vFwd) {
            state.driftCharge = driftCharge + dt;
        }
    } else if (sliding) {
        // ручник отпущен или скорость потеряна
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

// Продольные оси машин на текущий тик. Буфер модульного уровня: считать
// sin/cos на каждую ПАРУ было бы 2*C(8,2) = 56 вызовов вместо восьми, а
// заводить массив внутри функции нельзя — resolveCollisions зовётся 60 раз
// в секунду и обязана быть без аллокаций. Растёт один раз до нужной длины.
const AXIS_X = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0];
const AXIS_Z = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0];

/**
 * Столкновения машина-машина: капсула против капсулы.
 *
 * На клиенте НЕ вызывается (раздел 6.2: столкновения считает только сервер) и
 * намеренно не является частью step. Экспортируется, чтобы зеркало было полным
 * и чтобы одиночные офлайн-прогоны в браузере вели себя как сервер.
 *
 * Машина — капсула вдоль продольной оси: отрезок от -CAR_AXIS_HALF до
 * +CAR_AXIS_HALF вдоль (sin yaw, cos yaw), обмотанный радиусом CAR_RADIUS.
 * Габарит 4,00 x 1,90 м против прежнего круга 2,2 м — именно из-за круга
 * казалось, что столкновений нет: машины успевали въехать друг в друга
 * на два метра, прежде чем что-то происходило.
 *
 * cars — массив состояний, stats — параллельный ему массив характеристик
 * (нужна mass), count — сколько первых элементов участвует.
 */
export function resolveCollisions(cars, stats, count) {
    while (AXIS_X.length < count) {
        AXIS_X.push(0.0);
        AXIS_Z.push(0.0);
    }
    for (let i = 0; i < count; i++) {
        const yaw = cars[i].yaw;
        AXIS_X[i] = Math.sin(yaw);
        AXIS_Z[i] = Math.cos(yaw);
    }

    const half = CAR_AXIS_HALF;
    const contact = CAR_RADIUS + CAR_RADIUS;
    const contactSq = contact * contact;
    // предпроверка по центрам: дальше этого капсулы не достанут никак
    const reach = half + half + contact;
    const reachSq = reach * reach;

    const last = count - 1;
    for (let i = 0; i < last; i++) {
        const a = cars[i];
        let ax = a.x;
        let az = a.z;
        const ux = AXIS_X[i];
        const uz = AXIS_Z[i];
        const massA = stats[i].mass;
        for (let j = i + 1; j < count; j++) {
            const b = cars[j];
            const cx = ax - b.x;
            const cz = az - b.z;
            if (cx * cx + cz * cz >= reachSq) {
                continue;
            }
            const wx = AXIS_X[j];
            const wz = AXIS_Z[j];
            // ближайшая пара точек на двух отрезках:
            // минимум |D + s*u - t*w|² по s, t из [-half, half]
            const dotUw = ux * wx + uz * wz;
            const dU = cx * ux + cz * uz;
            const dW = cx * wx + cz * wz;
            const denom = 1.0 - dotUw * dotUw;
            let s;
            if (denom > 1e-9) {
                s = (dotUw * dW - dU) / denom;
            } else {
                // оси параллельны: минимум вырожден в отрезок, берём
                // проекцию центра b на ось a
                s = -dU;
            }
            if (s > half) {
                s = half;
            } else if (s < -half) {
                s = -half;
            }
            let t = dW + s * dotUw;
            if (t > half || t < -half) {
                t = t > half ? half : -half;
                s = t * dotUw - dU;
                if (s > half) {
                    s = half;
                } else if (s < -half) {
                    s = -half;
                }
            }
            const dx = (b.x + wx * t) - (ax + ux * s);
            const dz = (b.z + wz * t) - (az + uz * s);
            const distSq = dx * dx + dz * dz;
            if (distSq >= contactSq) {
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
                overlap = contact - dist;
            } else {
                // точки контакта совпали — расталкиваем по линии центров,
                // а если совпали и центры, то по оси X: направление зависит
                // только от индексов, значит результат детерминирован
                const centerSq = cx * cx + cz * cz;
                if (centerSq > 1e-9) {
                    const inv = 1.0 / Math.sqrt(centerSq);
                    nx = -cx * inv;
                    nz = -cz * inv;
                } else {
                    nx = 1.0;
                    nz = 0.0;
                }
                overlap = contact;
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
            ax = a.x;
            az = a.z;

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
