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
};

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
    onSnap(m) { app.world.applySnap(m); },
    onEvent(m) { /* 5.2: события этапа 0c ещё не рисуются */ },
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
    '   задержка ' + (n.rtt ? n.rtt.toFixed(1) : '—') + ' мс' +
    '   сущностей ' + s.ents + (s.hidden ? ' (в темноте ' + s.hidden + ')' : '') +
    '   тик ' + Math.floor(view.tick) + '/' + app.world.latestTick +
    '   кадр ' + s.ms.toFixed(2) + ' мс' +
    '   drawCalls ' + s.drawCalls +
    '   снапшотов ' + app.world.snaps +
    (view.interp ? '' : '   [ИНТЕРПОЛЯЦИЯ ВЫКЛЮЧЕНА]');
}

// ---------------------------------------------------------------- старт

export function boot() {
  app.world = new WorldState();
  app.render = createRenderer();
  const canvas = $('c');
  app.render.init(canvas, { quality: 'high', tilePx: 48 });
  app.input = new Input(canvas, (sx, sy) => app.render.screenToWorld(sx, sy)).attach();
  app.net = new Net(makeHandlers());

  $('createBtn').onclick = () => doJoin('');
  $('joinBtn').onclick = () => doJoin(($('code').value || '').trim().toUpperCase());
  $('refreshBtn').onclick = () => app.net.rooms();
  $('readyBtn').onclick = doReady;
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
    setInterp: (v) => { app.world.interp = !!v; return app.world.interp; },
    getInterp: () => app.world.interp,
    record: (n) => { app.rec = { on: true, want: n, samples: [] }; },
    recording: () => app.rec.on,
    samples: () => app.rec.samples,
    status: () => $('status').textContent,

    // --- туман (tests/client_fog.py) ---------------------------------
    // Выключатель «клиент игнорирует vis» — ровно тот клиент, что был до
    // тумана. Нужен, чтобы проверка тумана умела покраснеть.
    setFog: (v) => { app.world.fogOn = !!v; return app.world.fogOn; },
    getFog: () => app.world.fogOn,
    fog: () => {
      const L = app.world.level, f = app.world.fog;
      if (!L || !f) return null;
      return { w: L.w, h: L.h, cells: Array.from(f), msgs: app.world.visMsgs };
    },
    worldToScreen: (x, y) => app.render.worldToScreen(x, y),
    canvasSize: () => ({ w: $('c').width, h: $('c').height }),

    // Пиксели НАСТОЯЩЕГО кадра: «видно» — это то, что попало на канву, а
    // не то, что лежит в модели мира. dark считает пиксели, у которых
    // максимальный канал не выше thresh (чернота), maxR — самый яркий
    // красный в коробке (по нему видно врага: #d16a6a это r=209, а самый
    // красный кусок подземелья — стена #666f88, r=102).
    pixels: (x, y, w, h, thresh) => {
      const c = $('c');
      const g = c.getContext('2d');
      x = Math.max(0, Math.min(c.width - 1, Math.round(x || 0)));
      y = Math.max(0, Math.min(c.height - 1, Math.round(y || 0)));
      w = Math.max(1, Math.min(c.width - x, Math.round(w || c.width)));
      h = Math.max(1, Math.min(c.height - y, Math.round(h || c.height)));
      const d = g.getImageData(x, y, w, h).data;
      const t = thresh === undefined ? 10 : thresh;
      let dark = 0, maxCh = 0, maxR = 0, sum = 0;
      for (let i = 0; i < d.length; i += 4) {
        const r = d[i], gg = d[i + 1], b = d[i + 2];
        const m = r > gg ? (r > b ? r : b) : (gg > b ? gg : b);
        if (m <= t) dark++;
        if (m > maxCh) maxCh = m;
        if (r > maxR) maxR = r;
        sum += m;
      }
      const n = d.length / 4;
      return { n: n, dark: dark, frac: dark / n, maxCh: maxCh, maxR: maxR,
               mean: sum / n, box: [x, y, w, h] };
    },

    // Подать сообщение тем же путём, каким его подаёт провод: net._onMessage
    // — это ровно то, что вызывает ws.onmessage. Никаких обходов разбора.
    wire: (s) => { app.net._onMessage({ data: s }); return app.world.ents.size; },

    // Стоимость кадра: каждое значение — один настоящий кадр, а не выборка
    // опросом из питона.
    msRecord: (on) => { app.msRec = !!on; app.msRing = []; return app.msRec; },
    msSamples: () => app.msRing.slice(),
  };
}

boot();
