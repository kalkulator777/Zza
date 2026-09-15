// Площадка для катания на физике Rapier.
//
// Здесь НЕТ ничего из игры: ни сети, ни трасс, ни бонусов. Задача одна —
// дать заказчику руль в руки и услышать «ощущается так себе» или «годится».
// Рендер нарочно минимальный: коробки, цилиндры, сетка на земле.
//
// Вся связь с физикой — через плоский ABI модуля racing_physics.wasm:
// один вызов rp_step на тик, всё остальное читается и пишется прямо
// в общей линейной памяти.

import * as THREE from 'three';

const DT = 1 / 60;
const MAX_SUBSTEPS = 5;           // не даём догонять больше пяти тиков за кадр

// ---------------------------------------------------------------------------
// 1. Загрузка модуля и раскладка общей памяти
// ---------------------------------------------------------------------------

const wasmBytes = await loadWasm();
const inst = await WebAssembly.instantiate(await WebAssembly.compile(wasmBytes), {});
const X = inst.exports;
if (X.rp_abi_version() !== 1) throw new Error('версия ABI не та');

async function loadWasm() {
  // В однофайловой сборке байты уже лежат в странице.
  if (globalThis.__RP_WASM_B64) {
    const raw = atob(globalThis.__RP_WASM_B64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  return new Uint8Array(await (await fetch('./racing_physics.wasm')).arrayBuffer());
}

// Память может «переехать» при росте — после любой аллокации внутри модуля
// старые типизированные массивы отваливаются. Поэтому виды пересоздаются
// централизованно и только тогда, когда буфер действительно сменился.
const mem = X.memory;
let buf = null;
let vIn, vOut, vDesc, vProp, vTune;
const F_OUT = 52, F_DESC = 8, F_PROP = 8, F_IN = 4;

function refresh() {
  if (buf === mem.buffer) return;
  buf = mem.buffer;
  vIn = new Float32Array(buf, X.rp_inputs_ptr(), F_IN * 16);
  vOut = new Float32Array(buf, X.rp_outputs_ptr(), F_OUT * 16);
  vDesc = new Float32Array(buf, X.rp_descs_ptr(), F_DESC * 16);
  vProp = new Float32Array(buf, X.rp_props_ptr(), F_PROP * 64);
  vTune = new Float32Array(buf, X.rp_tuning_ptr(), X.rp_tuning_floats());
}

// ---------------------------------------------------------------------------
// 2. Площадка: ровный асфальт, конусы, трамплин, бордюр
// ---------------------------------------------------------------------------

const CONES = [];
for (let k = 0; k < 8; k++) CONES.push([(k % 2 ? 4.5 : -4.5), 22 + k * 9]);
for (let k = 0; k < 4; k++) CONES.push([-16 + k * 1.5, 52 + k * 3]);

const SPAWN = [0, 0.9, -30, 0];

function buildWorld(preset) {
  X.rp_world_reset(preset);
  refresh();
  X.rp_add_ground(300, 1.1);
  // Трамплин: короткая аппарель и площадка схода.
  X.rp_add_box(5, 0.25, 7, 0, 1.05, 95, 0, -0.28, 0.95, 0);
  X.rp_add_box(5, 0.25, 2.2, 0, 2.95, 104.5, 0, 0, 0.95, 0);
  // Бордюр вдоль правого края разгонной прямой.
  X.rp_add_box(0.4, 0.13, 26, -9, 0.13, 40, 0, 0, 1.2, 0);
  // Невысокая стенка в конце площадки — чтобы было обо что стукнуться.
  X.rp_add_box(26, 1.2, 0.5, 0, 1.2, 150, 0, 0, 1.0, 0);
  X.rp_add_box(26, 1.2, 0.5, 0, 1.2, -60, 0, 0, 1.0, 0);
  for (const [x, z] of CONES) X.rp_add_box(0.28, 0.4, 0.28, x, 0.4, z, 0, 0, 0.6, 9);
  X.rp_car_spawn(SPAWN[0], SPAWN[1], SPAWN[2], SPAWN[3]);
  refresh();
}

let preset = 0;
buildWorld(preset);

const desc = {
  halfWidth: vDesc[0], halfHeight: vDesc[1], halfLength: vDesc[2],
  wheelRadius: vDesc[3], axleX: vDesc[4], axleZ: vDesc[5],
  connY: vDesc[6], suspRest: vDesc[7],
};

// ---------------------------------------------------------------------------
// 3. Рендер. Всё создаётся один раз, в кадровом цикле — только присвоения.
// ---------------------------------------------------------------------------

const canvas = document.getElementById('view');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: false });
renderer.setPixelRatio(Math.min(devicePixelRatio, 1));
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x8fb2dd);
scene.fog = new THREE.Fog(0x8fb2dd, 150, 520);
const camera = new THREE.PerspectiveCamera(62, 1, 0.3, 700);

scene.add(new THREE.HemisphereLight(0xbfd8ff, 0x4a4438, 2.0));
scene.add(new THREE.AmbientLight(0xffffff, 0.35));
const sun = new THREE.DirectionalLight(0xfff0d8, 2.0);
sun.position.set(40, 70, 20);
scene.add(sun);

const ground = new THREE.Mesh(new THREE.PlaneGeometry(900, 900),
  new THREE.MeshLambertMaterial({ color: 0xa9b1bc }));
ground.rotation.x = -Math.PI / 2;
scene.add(ground);
const grid = new THREE.GridHelper(600, 60, 0x5d6672, 0x7b838e);
grid.position.y = 0.012;
scene.add(grid);

// Разметка вдоль разгонной прямой: без неё скорость на глаз не читается,
// а именно ощущение скорости заказчик и должен оценить.
{
  const stripe = new THREE.BoxGeometry(0.35, 0.02, 3.2);
  const mt = new THREE.MeshLambertMaterial({ color: 0xf2f4f7 });
  const dash = new THREE.InstancedMesh(stripe, mt, 44 * 2);
  const m4 = new THREE.Matrix4();
  let n = 0;
  for (let i = 0; i < 44; i++) {
    const z = -55 + i * 5;
    m4.makeTranslation(3.6, 0.02, z); dash.setMatrixAt(n++, m4);
    m4.makeTranslation(-3.6, 0.02, z); dash.setMatrixAt(n++, m4);
  }
  dash.count = n;
  scene.add(dash);
  // поперечные отметки каждые 25 м — видно, сколько пролетаешь за секунду
  const tick = new THREE.BoxGeometry(9.0, 0.02, 0.25);
  const mt2 = new THREE.MeshLambertMaterial({ color: 0xffd34d });
  const marks = new THREE.InstancedMesh(tick, mt2, 10);
  let k = 0;
  for (let z = -50; z <= 150; z += 25) {
    m4.makeTranslation(0, 0.02, z); marks.setMatrixAt(k++, m4);
  }
  marks.count = k;
  scene.add(marks);
}

function addBox(hx, hy, hz, x, y, z, rotX, color) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(hx * 2, hy * 2, hz * 2),
    new THREE.MeshLambertMaterial({ color }));
  m.position.set(x, y, z);
  if (rotX) m.rotation.x = rotX;
  scene.add(m);
  return m;
}
addBox(5, 0.25, 7, 0, 1.05, 95, -0.28, 0x8b95a6);
addBox(5, 0.25, 2.2, 0, 2.95, 104.5, 0, 0x8b95a6);
addBox(0.4, 0.13, 26, -9, 0.13, 40, 0, 0xe8c44a);
addBox(26, 1.2, 0.5, 0, 1.2, 150, 0, 0x99a3b1);
addBox(26, 1.2, 0.5, 0, 1.2, -60, 0, 0x99a3b1);

// Конусы — подвижные тела, их позы приезжают из модуля каждый кадр.
const coneMeshes = [];
{
  const g = new THREE.ConeGeometry(0.32, 0.9, 10);
  const mt = new THREE.MeshLambertMaterial({ color: 0xff7a33 });
  for (let i = 0; i < CONES.length; i++) {
    const m = new THREE.Mesh(g, mt);
    scene.add(m);
    coneMeshes.push(m);
  }
}

// Машина: кузов, крыша и четыре колеса.
const car = new THREE.Group();
const bodyMesh = new THREE.Mesh(
  new THREE.BoxGeometry(desc.halfWidth * 2, desc.halfHeight * 2, desc.halfLength * 2),
  new THREE.MeshLambertMaterial({ color: 0x3f8ae0 }));
car.add(bodyMesh);
const roof = new THREE.Mesh(
  new THREE.BoxGeometry(desc.halfWidth * 1.7, 0.42, desc.halfLength * 0.9),
  new THREE.MeshLambertMaterial({ color: 0x2b6cb8 }));
roof.position.set(0, desc.halfHeight + 0.2, -0.15);
car.add(roof);
const nose = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.16, 0.5),
  new THREE.MeshLambertMaterial({ color: 0xffe9a8 }));
nose.position.set(0, 0, desc.halfLength + 0.1);
car.add(nose);
scene.add(car);

const wheels = [];
{
  const g = new THREE.CylinderGeometry(desc.wheelRadius, desc.wheelRadius, 0.26, 16);
  g.rotateZ(Math.PI / 2);          // ось цилиндра вдоль X, как ось колеса
  const mt = new THREE.MeshLambertMaterial({ color: 0x33383f });
  // спица: по ней на глаз видно, крутится колесо или юзом идёт
  const gs = new THREE.BoxGeometry(0.28, desc.wheelRadius * 1.7, 0.07);
  const mts = new THREE.MeshLambertMaterial({ color: 0xe8e2d4 });
  for (let i = 0; i < 4; i++) {
    const pivot = new THREE.Group();     // поворот руля
    const spin = new THREE.Mesh(g, mt);  // проворот колеса
    spin.add(new THREE.Mesh(gs, mts));
    pivot.add(spin);
    scene.add(pivot);
    wheels.push({ pivot, spin });
  }
}

const HARD = [
  [+desc.axleX, desc.connY, +desc.axleZ],
  [-desc.axleX, desc.connY, +desc.axleZ],
  [+desc.axleX, desc.connY, -desc.axleZ],
  [-desc.axleX, desc.connY, -desc.axleZ],
];

// Заранее выделенные векторы: в кадровом цикле не аллоцируем (раздел 1 контракта).
const tmpQ = new THREE.Quaternion();
const tmpV = new THREE.Vector3();
const tmpV2 = new THREE.Vector3();
const camPos = new THREE.Vector3();
const camAim = new THREE.Vector3();

// ---------------------------------------------------------------------------
// 4. Клавиатура — ровно как в игре
// ---------------------------------------------------------------------------

const keys = Object.create(null);
let camMode = 0;
addEventListener('keydown', ev => {
  const k = ev.code;
  keys[k] = true;
  if (k === 'KeyR') respawn();
  if (k === 'KeyC') camMode = (camMode + 1) % 3;
  if (k === 'Digit1') setPreset(0);
  if (k === 'Digit2') setPreset(1);
  if (['KeyW', 'KeyS', 'KeyA', 'KeyD', 'Space'].includes(k)) ev.preventDefault();
});
addEventListener('keyup', ev => { keys[ev.code] = false; });
addEventListener('blur', () => { for (const k in keys) keys[k] = false; });

function respawn() {
  // Дешевле пересобрать мир, чем аккуратно ставить тело на место.
  buildWorld(preset);
}

function setPreset(p) {
  if (p === preset) return;
  preset = p;
  buildWorld(p);
  document.getElementById('bArcade').classList.toggle('on', p === 0);
  document.getElementById('bSim').classList.toggle('on', p === 1);
  document.getElementById('modeNote').textContent = p === 0
    ? 'жёстко, цепко, помощь в руле'
    : 'честно: мягче подвеска, живое сцепление, без помощи';
}
document.getElementById('bArcade').onclick = () => setPreset(0);
document.getElementById('bSim').onclick = () => setPreset(1);

// ---------------------------------------------------------------------------
// 5. Строки телеметрии
// ---------------------------------------------------------------------------

const WHEEL_NAMES = ['перед лев', 'перед прав', 'зад лев', 'зад прав'];
const rows = [];
{
  const tb = document.getElementById('wheels');
  for (let i = 0; i < 4; i++) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td class="k">' + WHEEL_NAMES[i] + '</td>' +
      '<td class="v"><span class="bar"><i></i></span> <span class="bar"><i class="load"></i></span></td>';
    tb.appendChild(tr);
    const bars = tr.querySelectorAll('i');
    rows.push({ tr, travel: bars[0], load: bars[1], name: tr.firstChild });
  }
}
const elKmh = document.getElementById('kmh');
const elSlip = document.getElementById('slip');
const elRpm = document.getElementById('rpm');
const elCharge = document.getElementById('charge');
const elGround = document.getElementById('ground');
const elSteer = document.getElementById('steer');
const elCost = document.getElementById('cost');
const elFrame = document.getElementById('frame');

function num(v, d) { return v.toFixed(d).replace('.', ','); }

// ---------------------------------------------------------------------------
// 6. Кадровый цикл
// ---------------------------------------------------------------------------

let acc = 0;
let last = performance.now();
let costAvg = 0, frameAvg = 0, hudT = 0;

function resize() {
  const w = innerWidth, h = innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
addEventListener('resize', resize);
resize();

function frame(now) {
  requestAnimationFrame(frame);

// Отладочный крючок: нужен только проверочному прогону, в кадре не участвует.
globalThis.__dbg = () => ({
  cam: camera.position.toArray().map(v => +v.toFixed(2)),
  car: car.position.toArray().map(v => +v.toFixed(2)),
  wheel0: wheels[0].pivot.position.toArray().map(v => +v.toFixed(2)),
  groundVisible: ground.visible,
  childCount: scene.children.length,
  renderInfo: { calls: renderer.info.render.calls, tris: renderer.info.render.triangles },
});
  const dtReal = Math.min((now - last) / 1000, 0.25);
  last = now;
  acc += dtReal;

  refresh();
  vIn[0] = keys.KeyW ? 1 : 0;
  vIn[1] = keys.KeyS ? 1 : 0;
  vIn[2] = (keys.KeyA ? 1 : 0) - (keys.KeyD ? 1 : 0);   // + налево, раздел 4
  vIn[3] = keys.Space ? 1 : 0;

  let steps = 0;
  while (acc >= DT && steps < MAX_SUBSTEPS) { acc -= DT; steps++; }
  if (steps) {
    const t0 = performance.now();
    X.rp_step(steps);
    const us = (performance.now() - t0) * 1000 / steps;
    costAvg += (us - costAvg) * 0.05;
    refresh();
  }
  frameAvg += (dtReal * 1000 - frameAvg) * 0.05;

  // --- поза кузова ---
  const o = 0;
  tmpQ.set(vOut[o + 4], vOut[o + 5], vOut[o + 6], vOut[o + 7]);
  car.position.set(vOut[o + 0], vOut[o + 1], vOut[o + 2]);
  car.quaternion.copy(tmpQ);

  // --- колёса: точка крепления + ход подвески вниз ---
  for (let i = 0; i < 4; i++) {
    const w = o + 20 + i * 8;
    const h = HARD[i];
    tmpV.set(h[0], h[1] - vOut[w + 0], h[2]).applyQuaternion(tmpQ).add(car.position);
    const p = wheels[i].pivot;
    p.position.copy(tmpV);
    p.quaternion.copy(tmpQ);
    p.rotateY(vOut[w + 3]);
    wheels[i].spin.rotation.x = vOut[w + 4];
  }

  // --- конусы ---
  const np = X.rp_prop_count();
  for (let i = 0; i < coneMeshes.length && i < np; i++) {
    const q = i * F_PROP;
    coneMeshes[i].position.set(vProp[q + 0], vProp[q + 1], vProp[q + 2]);
    coneMeshes[i].quaternion.set(vProp[q + 4], vProp[q + 5], vProp[q + 6], vProp[q + 7]);
  }

  // --- камера ---
  const yaw = vOut[o + 3];
  const back = camMode === 2 ? 15 : 8.6;
  const high = camMode === 2 ? 6.5 : 3.1;
  if (camMode === 1) {
    // вид из кабины
    tmpV2.set(0, desc.halfHeight + 0.45, 0.35).applyQuaternion(tmpQ).add(car.position);
    camPos.copy(tmpV2);
    tmpV2.set(0, desc.halfHeight + 0.35, 12).applyQuaternion(tmpQ).add(car.position);
    camAim.copy(tmpV2);
  } else {
    camPos.set(car.position.x - Math.sin(yaw) * back,
      car.position.y + high,
      car.position.z - Math.cos(yaw) * back);
    camAim.set(car.position.x, car.position.y + 0.9, car.position.z);
  }
  camera.position.lerp(camPos, camMode === 1 ? 1 : 0.22);
  camera.lookAt(camAim);

  renderer.render(scene, camera);

  // --- телеметрия, раз в 100 мс ---
  hudT += dtReal;
  if (hudT > 0.1) {
    hudT = 0;
    elKmh.textContent = Math.round(vOut[o + 11] * 3.6);
    elSlip.textContent = num(vOut[o + 15] * 180 / Math.PI, 1) + '°';
    elRpm.textContent = Math.round(vOut[o + 16]);
    elCharge.textContent = num(vOut[o + 18], 2) + ' с';
    elGround.textContent = vOut[o + 17].toFixed(0);
    elSteer.textContent = num(vOut[o + 23], 2);
    for (let i = 0; i < 4; i++) {
      const w = o + 20 + i * 8;
      const travel = vOut[w + 1];            // + сжатие, - отбой
      const maxTravel = 0.22;
      const t = Math.max(0, Math.min(1, (travel + maxTravel) / (2 * maxTravel)));
      rows[i].travel.style.width = (t * 100).toFixed(0) + '%';
      const load = Math.max(0, Math.min(1, vOut[w + 2] / 14000));
      rows[i].load.style.width = (load * 100).toFixed(0) + '%';
      rows[i].tr.classList.toggle('air', vOut[w + 7] < 0.5);
    }
    elCost.textContent = num(costAvg, 1);
    elFrame.textContent = num(frameAvg, 1);
  }
}
requestAnimationFrame(frame);
