// Характеристики машин на клиенте — зеркало game/cars.py.
//
// Владелец модуля: [track]. Зависимостей нет, сеть отсюда не дёргается:
// список машин приходит готовым в событии welcome (welcome.content.cars),
// модуль только раскладывает его и отдаёт по id.
//
// Поля stats остаются в snake_case, как в cars.json и в протоколе; перевести
// их в camelCase для шага физики умеет physics.createCarStats(spec.stats).

export const BAR_NAMES = ['speed', 'accel', 'grip', 'weight'];

const STAT_NAMES = [
    'engine_force', 'max_speed', 'brake_force', 'reverse_force', 'turn_rate',
    'grip_step', 'drift_grip_step', 'drag', 'roll', 'boost_speed', 'mass',
];

export class CarSpec {
    constructor(row) {
        this.id = row.id;
        this.name = row.name || row.id;
        this.desc = row.desc || '';
        this.bars = row.bars || { speed: 3, accel: 3, grip: 3, weight: 3 };
        this.stats = row.stats || null;
        this.shape = row.shape || null;
        // Фактический потолок скорости: корень уравнения
        // engine_force*(1 - v/max_speed) = drag*v^2 + roll*v.
        // max_speed — опорная скорость двигателя, а не достижимая; HUD и лобби
        // должны показывать именно это число, иначе врут игроку.
        this.topSpeed = this.stats ? solveTopSpeed(this.stats) : 0.0;
    }
}

/** Фактический потолок скорости, м/с (та же формула, что в game/cars.py). */
export function solveTopSpeed(stats) {
    const a = stats.drag;
    const b = stats.roll + stats.engine_force / stats.max_speed;
    const c = -stats.engine_force;
    return (-b + Math.sqrt(b * b - 4.0 * a * c)) / (2.0 * a);
}

export class CarCatalog {
    constructor(rows) {
        this.list = [];
        this.byId = new Map();
        for (let i = 0; i < rows.length; i++) {
            const spec = new CarSpec(rows[i]);
            if (!spec.id || this.byId.has(spec.id)) {
                continue;                     // дубли и записи без id молча мимо
            }
            if (!spec.stats) {
                console.warn('cars.js: у машины %s нет stats, предсказание для '
                             + 'неё работать не будет', spec.id);
            } else {
                for (let k = 0; k < STAT_NAMES.length; k++) {
                    if (typeof spec.stats[STAT_NAMES[k]] !== 'number') {
                        console.warn('cars.js: у машины %s нет характеристики %s',
                                     spec.id, STAT_NAMES[k]);
                    }
                }
            }
            this.byId.set(spec.id, spec);
            this.list.push(spec);
        }
        this.ids = this.list.map((spec) => spec.id);
        this.defaultId = this.ids.length ? this.ids[0] : null;
    }

    /**
     * Собрать каталог из события welcome. Принимает и само событие, и его
     * поле content, и голый массив машин — чтобы вызывающему не думать.
     */
    static fromWelcome(source) {
        let rows = source;
        if (rows && !Array.isArray(rows)) {
            rows = rows.content ? rows.content.cars : rows.cars;
        }
        return new CarCatalog(Array.isArray(rows) ? rows : []);
    }

    has(carId) {
        return this.byId.has(carId);
    }

    /** Машина по id или null. */
    get(carId) {
        return this.byId.get(carId) || null;
    }

    /** Машина по id, а для незнакомого id — машина по умолчанию. */
    resolve(carId) {
        return this.byId.get(carId) || this.byId.get(this.defaultId) || null;
    }

    get size() {
        return this.list.length;
    }
}

// Каталог на всю сессию: welcome приходит один раз, а нужен он и лобби,
// и HUD, и построению моделей. Прокидывать его через все модули незачем.
let current = new CarCatalog([]);

/** Запомнить каталог из welcome (зовёт net.js) и вернуть его. */
export function setCatalog(source) {
    current = CarCatalog.fromWelcome(source);
    return current;
}

/** Текущий каталог; до welcome он пустой, но валидный. */
export function getCatalog() {
    return current;
}

export default CarCatalog;
