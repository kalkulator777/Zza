// Прогон стенда сверки в движке JavaScript: Node (V8) и браузер.
//
// Сценарий готовит tools/test_wasm_parity.py и кладёт рядом файлом: трасса
// (те же девять массивов 12.1, что у сервера), расстановка, настройки машин
// и программа ввода. Здесь НЕТ ни одного числа игры — только прогон.
//
// Один и тот же файл грузится и Node, и страницей в браузере: обмен через
// строку JSON, ответ — строка JSON с хэшами на контрольных точках.

const B32 = '0123456789abcdefghijklmnopqrstuv';

/** Разжать программу ввода: по одному символу base32 на машину на тик. */
function mask(program, tick, car, cars) {
    return B32.indexOf(program[tick * cars + car]);
}

/**
 * Прогнать сценарий. host — уже созданный RapierHost, scenario — разобранный
 * JSON. Возвращает {mesh, points:[{tick, world, state}]}.
 */
export function runScenario(host, RapierHostClass, scenario) {
    const a = host.abi;
    host.reset(scenario.preset | 0);
    host.buildTrack(scenario.track, scenario.mesh[0], scenario.mesh[1], scenario.mesh[2]);
    const mesh = { verts: host.trackHashes()[0], tris: host.trackHashes()[1],
                   size: host.meshSize() };
    host.freeTrackMesh();
    for (const car of scenario.cars) {
        host.tuningPreset(scenario.preset | 0);
        host.setTuning(car.tuning);
        host.spawnCar(car.x, car.y, car.z, car.yaw);
    }
    const n = scenario.cars.length;
    const ci = a.CarInput;
    // Осадка подвески — те же холостые шаги, что делают оба хозяина.
    host._sync();
    for (let k = 0; k < host.inputs.length; k++) host.inputs[k] = 0;
    host.step(scenario.settle | 0);

    const points = [];
    const marks = new Set(scenario.marks);
    const program = scenario.program;
    for (let t = 0; t < scenario.ticks; t++) {
        host._sync();
        const inputs = host.inputs;
        for (let c = 0; c < n; c++) {
            const m = mask(program, t, c, n);
            const base = c * ci.FLOATS;
            inputs[base + ci.THROTTLE] = (m & 1) ? 1 : 0;
            inputs[base + ci.BRAKE] = (m & 2) ? 1 : 0;
            inputs[base + ci.STEER] = ((m & 4) ? 1 : 0) - ((m & 8) ? 1 : 0);
            inputs[base + ci.HANDBRAKE] = (m & 16) ? 1 : 0;
            // Флаг «вне полотна» в стенде всегда снят: полотно считает
            // хозяин, а стенду нужна одинаковость движков, а не игра.
            inputs[base + ci.OFFTRACK] = 0;
        }
        host.step(1);
        if (marks.has(t + 1)) {
            points.push({ tick: t + 1, world: host.worldHash(), state: host.stateHash() });
        }
    }
    return { mesh, points };
}

/** Точка входа Node: node tools/wasm_parity.mjs <сценарий.json> <модуль.wasm> */
async function mainNode() {
    const fs = await import('node:fs');
    const url = await import('node:url');
    const [scenarioPath, wasmPath] = process.argv.slice(2);
    const here = url.fileURLToPath(new URL('.', import.meta.url));
    const abi = await import(url.pathToFileURL(here + '../native/abi/abi_layout.js').href);
    const mod = await import(url.pathToFileURL(here + '../static/js/rapier_host.js').href);
    const bytes = new Uint8Array(fs.readFileSync(wasmPath));
    const host = await mod.RapierHost.load({ wasm: bytes, abi: abi });
    const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));
    process.stdout.write(JSON.stringify(runScenario(host, mod.RapierHost, scenario)));
}

if (typeof process !== 'undefined' && process.argv && process.argv.length > 3) {
    mainNode().catch((err) => {
        process.stderr.write(String(err && err.stack || err) + '\n');
        process.exit(1);
    });
}
