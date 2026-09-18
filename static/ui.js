// -*- coding: utf-8 -*-
// Меню, лобби, оверлей отладки и главный цикл (DESIGN.md 5, 6.1, 7.1).
//
// Красоты здесь нет намеренно: интерфейс — этап 5. Здесь ровно то, без
// чего в игру не войти: имя, список комнат, вход по коду, кнопка «готов»,
// и отладочный оверлей с fps / задержкой / числом сущностей / тиком.

import { Net } from './net.js';
import { WorldState, TICK_HZ } from './state.js';
import { Input } from './input.js';
import { createRenderer } from './render2d.js';

const $ = (id) => document.getElementById(id);

const app = {
  net: null, world: null, input: null, render: null,
  // Какой бэкенд рендера сейчас работает и на какой канве. ДВУМЕРНЫЙ ПО
  // УМОЛЧАНИЮ — он принят, на нём стоят чужие проверки, и менять это
  // молча нельзя (DESIGN.md 7.1: бэкенда два, выбор по замеру 2.1).
  backend: '2d', canvas: null, backendBusy: false,
  screen: 'menu',
  name: '',
  room: '',
  ready: false,
  players: [],
  rooms: [],
  lastErr: '',
  // кадры
  frames: 0, fps: 0, _fpsT0: 0, _fpsN: 0,
  // запись позиций для проверки плавности (tests/client_interp.py)
  rec: { on: false, want: 0, samples: [] },
  // запись стоимости кадра (tests/client_fog.py). Выключена по умолчанию:
  // stats() создаёт объект, и в обычной игре этот мусор ни к чему.
  msRec: false, msRing: [],
  // запись КАДРОВ и СНАПШОТОВ для tests/client_combat.py. Оба выключены:
  // первый читает пиксели канвы каждый кадр, второй создаёт объект на
  // каждый снапшот — в игре ни то, ни другое не нужно.
  watch: { on: false, x: 0, y: 0, w: 0, h: 0, out: [], max: 0 },
  trk: { on: false, out: [], max: 0 },
};

// Хэш куска кадра. Нужен ровно одному вопросу: «изменился ли кадр», и
// ответ должен быть ДА/НЕТ, а не «средняя яркость подросла на 0.3». FNV-1a
// по каждому четвёртому пикселю: 4 пикселя подряд одинаковыми не бывают
// (тело игрока 34 px в поперечнике, дуга замаха 115 px), а работы вчетверо
// меньше — эта функция зовётся каждый кадр, пока идёт запись.
function frameHash(data) {
  let h = 2166136261;
  for (let i = 0; i < data.length; i += 16) {
    h ^= data[i]; h = Math.imul(h, 16777619);
    h ^= data[i + 1]; h = Math.imul(h, 16777619);
    h ^= data[i + 2]; h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

// Пиксели текущего кадра. У канвы ровно ОДИН контекст на всю жизнь: если
// на ней работает WebGL, getImageData с неё не получить в принципе.
// Поэтому пиксели спрашиваются у бэкенда, а путь двумерного остаётся
// побитно прежним — у него readPixels нет, и ветка та же, что была.
function readPix(x, y, w, h) {
  if (app.render && app.render.readPixels) return app.render.readPixels(x, y, w, h);
  return $('c').getContext('2d').getImageData(x, y, w, h).data;
}

function activeCanvas() {
  return app.canvas || $('c');
}

// ---------------------------------------------------------------- экраны

function show(name) {
  app.screen = name;
  $('menu').style.display = name === 'menu' ? '' : 'none';
  $('lobby').style.display = name === 'lobby' ? '' : 'none';
  $('game').style.display = name === 'game' ? '' : 'none';
  if (app.input) app.input.enabled = (name === 'game');
  if (name !== 'game' && app.input) app.input.clear();
}

function setStatus(text, bad) {
  const el = $('status');
  el.textContent = text || '';
  el.className = bad ? 'bad' : '';
  if (bad) app.lastErr = text;
}

function renderRoomList() {
  const box = $('roomlist');
  box.textContent = '';
  if (!app.rooms.length) {
    const p = document.createElement('div');
    p.className = 'dim';
    p.textContent = 'Комнат нет. Создай свою — код продиктуешь коллеге.';
    box.appendChild(p);
    return;
  }
  for (const r of app.rooms) {
    const row = document.createElement('div');
    row.className = 'room';
    const info = document.createElement('span');
    // phase сервер пока не присылает (см. отчёт этапа 0c) — не врём,
    // а просто не показываем то, чего нет.
    info.textContent = r.id + '  ·  игроков ' + r.players + '  ·  этаж ' + r.floor +
                       (r.phase ? ('  ·  ' + r.phase) : '');
    const b = document.createElement('button');
    b.textContent = 'Войти';
    b.onclick = () => doJoin(r.id);
    row.appendChild(info);
    row.appendChild(b);
    box.appendChild(row);
  }
}

function renderPlayers() {
  const box = $('players');
  box.textContent = '';
  for (const p of app.players) {
    const d = document.createElement('div');
    d.textContent = (p[0] === app.net.pid ? '► ' : '   ') + p[1] + '  (pid ' + p[0] + ')';
    box.appendChild(d);
  }
}

// ---------------------------------------------------------------- сеть

function doJoin(code) {
  app.name = ($('name').value || '').trim();
  app.ready = false;
  setStatus('вход…');
  app.net.hello(app.name);
  app.net.join(code || '', app.name);
}

function doReady() {
  app.ready = !app.ready;
  app.net.ready(app.ready);
  $('readyBtn').textContent = app.ready ? 'Не готов' : 'Готов';
  setStatus(app.ready ? 'готов — ждём остальных' : 'в лобби');
}

function makeHandlers() {
  return {
    onOpen() {
      setStatus('соединение установлено');
      app.net.hello(($('name').value || '').trim());
      app.net.rooms();
      // Ввод шлём только в игре: в меню и лобби сущности нет, и 30 Гц
      // пустых сообщений — это трафик и работа сервера ни за что.
      app.net.startPump(() => (app.screen === 'game' ? app.input.sample() : null));
    },
    onClose(wanted) {
      if (wanted) return;
      setStatus('связь с сервером потеряна — перезагрузи страницу', true);
      show('menu');
    },
    onWelcome(m) {
      if (m.proto !== 1) {
        // 5.3: старый клиент получает внятный экран, а не тихую поломку.
        setStatus('обнови папку игры: сервер говорит на proto ' + m.proto +
                  ', клиент на 1', true);
      }
    },
    onRoomList(rooms) { app.rooms = rooms; renderRoomList(); },
    onJoined(m) {
      app.room = m.room;
      app.players = m.players || [];
      app.world.applyJoined(m);
      $('roomCode').textContent = m.room;
      renderPlayers();
      if (app.screen === 'menu') {
        show('lobby');
        setStatus('в лобби комнаты ' + m.room + ' — нажми «Готов»');
      }
    },
    onLevel(m, tiles) {
      if (!tiles) { setStatus('битая карта с сервера', true); return; }
      app.world.applyLevel(m, tiles);
      show('game');
      setStatus('');
      resize();
    },
    onSnap(m) {
      app.world.applySnap(m);
      // Запись flags/hp по СНАПШОТАМ, а не по кадрам: замах живёт в flags
      // (4.3) и меняется ровно на тике сервера. Кадр показывает его с
      // задержкой интерполяции (6.1), и мерить по кадру номер тика — значит
      // мерить задержку, а не замах.
      const tr = app.trk;
      if (tr.on) {
        const e = app.world.self();
        tr.out.push({ tick: m.tick, flags: e ? e.flags : -1,
                      hp: e ? e.hp : -1, hpMax: e ? e.hpMax : -1,
                      now: performance.now() });
        if (tr.out.length >= tr.max) tr.on = false;
      }
    },
    // 5.2: событие — это ВСПЫШКА и ничего больше. Ни здоровье, ни замах, ни
    // смерть отсюда не берутся: событие может потеряться, и клиент, который
    // держал бы на нём состояние, врал бы ровно при просадке сети.
    onEvent(m) { app.world.pushEvent(m); },
    onError(m) { setStatus('сервер: ' + (m.msg || m.code), true); },
    onInputSent(seq, mv) { app.world.noteInput(seq, mv); },
  };
}

// ---------------------------------------------------------------- кадр

function resize() {
  const w = window.innerWidth, h = window.innerHeight;
  app.render.resize(w, h);
}

function frame(now) {
  requestAnimationFrame(frame);
  app.frames++;
  if (!app._fpsT0) app._fpsT0 = now;
  app._fpsN++;
  if (now - app._fpsT0 >= 500) {
    app.fps = app._fpsN * 1000 / (now - app._fpsT0);
    app._fpsT0 = now; app._fpsN = 0;
  }
  if (app.screen !== 'game') return;

  const view = app.world.buildView(now);
  app.render.draw(view, view.alpha);

  if (app.msRec) {
    app.msRing.push(app.render.stats().ms);
    if (app.msRing.length > 600) app.msRing.shift();
  }

  // Запись кадров: хэш куска канвы + тик, состояние которого в этом куске
  // нарисовано. Только для проверок; в игре выключено.
  const wt = app.watch;
  if (wt.on) {
    const d = readPix(wt.x, wt.y, wt.w, wt.h);
    wt.out.push({ now: now, rt: view.tick, lt: app.world.latestTick,
                  h: frameHash(d) });
    if (wt.out.length >= wt.max) wt.on = false;
  }

  // Запись позиций своего персонажа — для проверки плавности.
  const r = app.rec;
  if (r.on) {
    const me = view.self;
    if (me) {
      r.samples.push([now, me.dx, me.dy]);
      if (r.samples.length >= r.want) r.on = false;
    }
  }
  updateHud(view);
}

let hudT0 = 0;
function updateHud(view) {
  const now = performance.now();
  if (now - hudT0 < 200) return;         // текст в DOM — не каждый кадр
  hudT0 = now;
  const s = app.render.stats();
  const n = app.net;
  $('hud').textContent =
    'fps ' + app.fps.toFixed(0) +
    (view.self ? ('   hp ' + view.self.hp + '/' + view.self.hpMax +
                  (view.self.flags & 1 ? ' (дух)' : '')) : '') +
    '   задержка ' + (n.rtt ? n.rtt.toFixed(1) : '—') + ' мс' +
    '   сущностей ' + s.ents + (s.hidden ? ' (в темноте ' + s.hidden + ')' : '') +
    '   тик ' + Math.floor(view.tick) + '/' + app.world.latestTick +
    '   кадр ' + s.ms.toFixed(2) + ' мс' +
    '   drawCalls ' + s.drawCalls +
    '   снапшотов ' + app.world.snaps +
    (view.interp ? '' : '   [ИНТЕРПОЛЯЦИЯ ВЫКЛЮЧЕНА]');
}

// ------------------------------------------------------- бэкенд рендера
//
// DESIGN.md 7.1: бэкендов два, оба поверх одного состояния, выбор делается
// по замеру 2.1 на целевой машине. Здесь — сам переключатель.
//
// ТРИ ВЕЩИ, КОТОРЫЕ ЗДЕСЬ НЕ СЛУЧАЙНЫ.
//
// 1. Трёхмерный бэкенд грузится ДИНАМИЧЕСКИМ import, а не обычным. three.js
//    это 700 КБ двумя файлами; статический import тащил бы их на каждый
//    старт игры, включая тот, где никто 3D не включит, — а 2.6 даёт на весь
//    старт 5 секунд. Пока кнопку не нажали, эти байты не запрашиваются.
// 2. Канва меняется вместе с бэкендом. У канвы ровно один контекст на всю
//    жизнь элемента: getContext('2d') после getContext('webgl2') вернёт
//    null, и наоборот. Один элемент на два бэкенда — это молча чёрный экран.
// 3. Ввод перевешивается на новую канву. input.js берёт прицел из
//    getBoundingClientRect() своей канвы и отбрасывает нажатия не по ней
//    (e.target !== canvas); оставить его на спрятанной канве значит
//    получить игру, в которой мышь не работает, а клавиши работают.

async function setBackend(name) {
  if (name !== '2d' && name !== '3d') return app.backend;
  if (name === app.backend || app.backendBusy) return app.backend;
  app.backendBusy = true;
  try {
    let make;
    if (name === '3d') {
      const mod = await import('./render3d.js');
      make = mod.createRenderer;
    } else {
      make = createRenderer;
    }
    const opts = { quality: app.render.quality || 'high',
                   tilePx: app.render.tilePx || 48 };
    const oldCanvas = app.canvas;
    const canvas = name === '3d' ? $('c3') : $('c');

    app.render.dispose();
    canvas.style.display = 'block';
    if (oldCanvas !== canvas) oldCanvas.style.display = 'none';

    app.render = make();
    app.canvas = canvas;
    app.backend = name;
    app.render.init(canvas, opts);
    app.render.resize(window.innerWidth, window.innerHeight);

    app.input.detach();
    app.input.canvas = canvas;
    app.input.attach();
    app.input.enabled = (app.screen === 'game');

    $('backendBtn').textContent = name === '3d' ? '3D' : '2D';
  } catch (e) {
    setStatus('бэкенд ' + name + ' не поднялся: ' + e, true);
    throw e;
  } finally {
    app.backendBusy = false;
  }
  return app.backend;
}

// ---------------------------------------------------------------- старт

export function boot() {
  app.world = new WorldState();
  app.render = createRenderer();
  const canvas = $('c');
  app.canvas = canvas;
  app.render.init(canvas, { quality: 'high', tilePx: 48 });
  app.input = new Input(canvas, (sx, sy) => app.render.screenToWorld(sx, sy)).attach();
  app.net = new Net(makeHandlers());

  $('createBtn').onclick = () => doJoin('');
  $('joinBtn').onclick = () => doJoin(($('code').value || '').trim().toUpperCase());
  $('refreshBtn').onclick = () => app.net.rooms();
  $('readyBtn').onclick = doReady;
  $('backendBtn').onclick = () => setBackend(app.backend === '2d' ? '3d' : '2d');
  // Клавиша B свободна: input.js разбирает WASD/стрелки, F, R, E, Q,
  // Shift и пробел — B среди них нет.
  window.addEventListener('keydown', (e) => {
    if (e.code === 'KeyB' && app.screen === 'game' && !e.repeat) {
      setBackend(app.backend === '2d' ? '3d' : '2d');
    }
  });
  $('code').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') $('joinBtn').click();
  });

  window.addEventListener('resize', resize);
  resize();
  show('menu');
  setStatus('подключаемся…');
  app.net.connect();
  requestAnimationFrame(frame);

  // Крючки для автоматических проверок (tests/client_*.py). Только чтение
  // и переключатель интерполяции — игровую логику мимо них не пускаем.
  window.__zza = {
    app: app,
    screen: () => app.screen,
    room: () => app.room,
    pid: () => app.net.pid,
    selfId: () => app.world.selfId,
    self: () => {
      const e = app.world.self();
      return e ? { id: e.id, x: e.dx, y: e.dy, sx: e.x, sy: e.y, flags: e.flags } : null;
    },
    others: () => {
      const out = [];
      for (const e of app.world.ents.values()) {
        if (e.id === app.world.selfId) continue;
        out.push({ id: e.id, kind: e.kind, x: e.dx, y: e.dy, flags: e.flags });
      }
      return out;
    },
    ents: () => app.world.ents.size,
    level: () => {
      const L = app.world.level;
      return L ? { floor: L.floor, seed: L.seed, w: L.w, h: L.h,
                   tiles: Array.from(L.tiles) } : null;
    },
    tick: () => app.world.latestTick,
    snaps: () => app.world.snaps,
    rtt: () => app.net.rtt,
    fps: () => app.fps,
    stats: () => app.render.stats(),
    // --- бэкенд рендера (tests/client_3d.py) --------------------------
    setBackend: (v) => setBackend(v),
    getBackend: () => app.backend,
    backendName: () => app.render.name,

    setInterp: (v) => { app.world.interp = !!v; return app.world.interp; },
    getInterp: () => app.world.interp,
    record: (n) => { app.rec = { on: true, want: n, samples: [] }; },
    recording: () => app.rec.on,
    samples: () => app.rec.samples,
    status: () => $('status').textContent,

    // --- бой (tests/client_combat.py) --------------------------------
    // Выключатели существуют затем же, зачем setFog: чтобы проверка умела
    // покраснеть, и чтобы «стоимость кадра до и после» мерилась на ОДНОЙ
    // сцене спина к спине, а не на двух разных прогонах.
    //   setCombat(false) — рендер ровно такой, каким он был до этой работы;
    //   setWindup(false) — пропадает ТОЛЬКО замах;
    //   setFx(false)     — пропадают только вспышки от событий.
    setCombat: (v) => { app.render.combat = !!v; return app.render.combat; },
    getCombat: () => app.render.combat,
    setWindup: (v) => { app.render.windup = !!v; return app.render.windup; },
    getWindup: () => app.render.windup,
    setFx: (v) => { app.world.fxOn = !!v; return app.world.fxOn; },
    getFx: () => app.world.fxOn,

    // Геометрия полоски своего здоровья — из рендера, а не списана в питон.
    hpBar: () => app.render.hpBarRect(),

    // Своё здоровье и флаги — то, что клиент ОБЯЗАН нарисовать. Числа из
    // снапшота, не из событий.
    me: () => {
      const e = app.world.self();
      return e ? { id: e.id, hp: e.hp, hpMax: e.hpMax, flags: e.flags,
                   x: e.dx, y: e.dy, facing: e.dfacing } : null;
    },
    // Журнал событий ev (5.2): вид, тик сервера, кто кого, сколько урона.
    evLog: () => app.world.evLog(),
    evs: () => app.world.evs,

    // Запись СНАПШОТОВ: тик сервера и flags/hp своей сущности на нём.
    track: (n) => { app.trk = { on: true, out: [], max: n }; return n; },
    tracking: () => app.trk.on,
    tracks: () => app.trk.out,

    // Запись КАДРОВ: хэш куска канвы и тик, состояние которого нарисовано.
    // Именно так отвечают на вопрос «на каком тике кадр изменился»: питон
    // опросом из-за границы процесса такого разрешения не даёт вовсе.
    watch: (x, y, w, h, n) => {
      const c = activeCanvas();
      x = Math.max(0, Math.min(c.width - 1, Math.round(x)));
      y = Math.max(0, Math.min(c.height - 1, Math.round(y)));
      w = Math.max(1, Math.min(c.width - x, Math.round(w)));
      h = Math.max(1, Math.min(c.height - y, Math.round(h)));
      app.watch = { on: true, x: x, y: y, w: w, h: h, out: [], max: n };
      return { x: x, y: y, w: w, h: h };
    },
    watching: () => app.watch.on,
    watched: () => app.watch.out,

    // --- туман (tests/client_fog.py) ---------------------------------
    // Выключатель «клиент игнорирует vis» — ровно тот клиент, что был до
    // тумана. Нужен, чтобы проверка тумана умела покраснеть.
    setFog: (v) => { app.world.fogOn = !!v; return app.world.fogOn; },
    getFog: () => app.world.fogOn,

    // --- свет факела (tests/client_light.py) --------------------------
    // Свет живёт в маске тумана, а маска перерисовывается только по дельте
    // (7.2) — поэтому выключатель обязан сбросить её ключ, иначе кадр
    // останется старым до первого шага игрока.
    setTorch: (v) => {
      app.render.torch = !!v;
      app.render.fogKey = '';
      return app.render.torch;
    },
    getTorch: () => app.render.torch,
    fog: () => {
      const L = app.world.level, f = app.world.fog;
      if (!L || !f) return null;
      return { w: L.w, h: L.h, cells: Array.from(f), msgs: app.world.visMsgs };
    },
    worldToScreen: (x, y) => app.render.worldToScreen(x, y),
    canvasSize: () => ({ w: activeCanvas().width, h: activeCanvas().height }),

    // Пиксели НАСТОЯЩЕГО кадра: «видно» — это то, что попало на канву, а
    // не то, что лежит в модели мира. dark считает пиксели, у которых
    // максимальный канал не выше thresh (чернота), maxR — самый яркий
    // красный в коробке (по нему видно врага: #d16a6a это r=209, а самый
    // красный кусок подземелья — стена #666f88, r=102).
    pixels: (x, y, w, h, thresh) => {
      const c = activeCanvas();
      x = Math.max(0, Math.min(c.width - 1, Math.round(x || 0)));
      y = Math.max(0, Math.min(c.height - 1, Math.round(y || 0)));
      w = Math.max(1, Math.min(c.width - x, Math.round(w || c.width)));
      h = Math.max(1, Math.min(c.height - y, Math.round(h || c.height)));
      const d = readPix(x, y, w, h);
      const t = thresh === undefined ? 10 : thresh;
      let dark = 0, maxCh = 0, maxR = 0, sum = 0, lum = 0, bright = 0;
      for (let i = 0; i < d.length; i += 4) {
        const r = d[i], gg = d[i + 1], b = d[i + 2];
        const m = r > gg ? (r > b ? r : b) : (gg > b ? gg : b);
        if (m <= t) dark++;
        if (m > t) bright++;
        if (m > maxCh) maxCh = m;
        if (r > maxR) maxR = r;
        sum += m;
        // Rec.709: настоящая ЯРКОСТЬ. mean выше — среднее по МАКСИМАЛЬНОМУ
        // каналу, и это детектор черноты, а не яркость: у холодного пола
        // максимальный канал синий, и тёплый свет его почти не двигает.
        lum += 0.2126 * r + 0.7152 * gg + 0.0722 * b;
      }
      const n = d.length / 4;
      return { n: n, dark: dark, frac: dark / n, bright: bright,
               maxCh: maxCh, maxR: maxR,
               mean: sum / n, lum: lum / n, box: [x, y, w, h],
               hash: frameHash(d) };
    },

    // Пачка коробок за ОДИН заход в браузер. Замер света читает пиксели
    // сотен клеток; по одному вызову на клетку это сотни переходов границы
    // процесса и десятки секунд на ровном месте.
    boxesMean: (list, thresh) => {
      const c = activeCanvas();
      const t = thresh === undefined ? 10 : thresh;
      const out = [];
      for (let k = 0; k < list.length; k++) {
        const q = list[k];
        const x = Math.max(0, Math.min(c.width - 1, Math.round(q[0])));
        const y = Math.max(0, Math.min(c.height - 1, Math.round(q[1])));
        const w = Math.max(1, Math.min(c.width - x, Math.round(q[2])));
        const h = Math.max(1, Math.min(c.height - y, Math.round(q[3])));
        const d = readPix(x, y, w, h);
        let sum = 0, lum = 0, dark = 0, bright = 0, maxCh = 0;
        for (let i = 0; i < d.length; i += 4) {
          const r = d[i], gg = d[i + 1], b = d[i + 2];
          const m = r > gg ? (r > b ? r : b) : (gg > b ? gg : b);
          if (m <= t) dark++; else bright++;
          if (m > maxCh) maxCh = m;
          sum += m;
          lum += 0.2126 * r + 0.7152 * gg + 0.0722 * b;
        }
        const n = d.length / 4;
        out.push({ n: n, mean: sum / n, lum: lum / n, dark: dark,
                   bright: bright, maxCh: maxCh });
      }
      return out;
    },

    // Подать сообщение тем же путём, каким его подаёт провод: net._onMessage
    // — это ровно то, что вызывает ws.onmessage. Никаких обходов разбора.
    wire: (s) => { app.net._onMessage({ data: s }); return app.world.ents.size; },

    // Стоимость кадра: каждое значение — один настоящий кадр, а не выборка
    // опросом из питона.
    msRecord: (on) => { app.msRec = !!on; app.msRing = []; return app.msRec; },
    msSamples: () => app.msRing.slice(),

    // Стоимость кадра с разрешением лучше кванта таймера. performance.now()
    // в браузере огрублён до 0.1 мс, а весь кадр стоит меньше этого кванта:
    // на одиночных кадрах любое отношение вырождается в 0.1/0.0. Поэтому
    // draw() гоняется n раз подряд по ОДНОМУ И ТОМУ ЖЕ виду, и квант
    // размазывается по n. Повторять draw() безопасно: 7.1 запрещает ему
    // менять мир, он только рисует то, что дали.
    benchDraw: (n) => {
      const view = app.world.buildView(performance.now());
      app.render.draw(view, view.alpha);     // прогрев: кэш карты и маска тумана
      const t0 = performance.now();
      for (let i = 0; i < n; i++) app.render.draw(view, view.alpha);
      return (performance.now() - t0) / n;
    },

    // То же, но с ПРИНУДИТЕЛЬНЫМ сливом кадра. Канва в браузере отложенная:
    // draw() только складывает команды, а растеризация случается потом и в
    // benchDraw не попадает — оттуда и неправдоподобные 0.01 мс на кадр
    // 1920x1080. Чтение одного пикселя заставляет браузер дорисовать всё
    // накопленное, и в замер входит настоящая закраска. Само чтение стоит
    // одинаково в обоих сравниваемых замерах и потому из отношения уходит.
    benchDrawFlush: (n) => {
      const view = app.world.buildView(performance.now());
      // Слив у каждого бэкенда свой, а смысл один: заставить браузер
      // ДОРИСОВАТЬ накопленное. У канвы это чтение одного пикселя, у WebGL
      // — readPixels одного пикселя (он тоже синхронный и тоже упирается в
      // конец конвейера). Без слива меряется подача команд, а не закраска.
      let flush;
      if (app.render.flush) {
        flush = () => app.render.flush();
      } else {
        const g = $('c').getContext('2d');
        flush = () => g.getImageData(0, 0, 1, 1);
      }
      app.render.draw(view, view.alpha);
      flush();
      const t0 = performance.now();
      for (let i = 0; i < n; i++) {
        app.render.draw(view, view.alpha);
        flush();
      }
      return (performance.now() - t0) / n;
    },

    // Во что обходится ПЕРЕСЧЁТ маски тумана — то, что случается не каждый
    // кадр, а только когда пришла дельта vis. Разница двух пачек: в первой
    // маска перерисовывается перед каждым draw, во второй нет.
    benchFog: (n) => {
      const view = app.world.buildView(performance.now());
      const r = app.render;
      r.draw(view, view.alpha);
      const t0 = performance.now();
      for (let i = 0; i < n; i++) { r.fogKey = ''; r.draw(view, view.alpha); }
      const a = performance.now() - t0;
      const t1 = performance.now();
      for (let i = 0; i < n; i++) r.draw(view, view.alpha);
      const b = performance.now() - t1;
      return { repaint: (a - b) / n, frame: b / n, n: n };
    },
  };
}

boot();
