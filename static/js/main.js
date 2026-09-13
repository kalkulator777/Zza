// Точка сборки клиента: экраны, обработка сообщений сервера, игровой цикл.

import { Net } from './net.js';
import { Input } from './input.js';
import { World } from './game/world.js';
import { TrackGeom } from './game/trackgeom.js';
import { Renderer } from './game/render.js';
import { SURFACE_ORDER } from './game/carstep.js';
import { Diag } from './diag.js';
import { Tuner } from './tuner.js';
import { sfx, resumeAudio } from './sound.js';
import {
  $, el, fmtTime, fmtClock, renderLobbyList, renderPlayers,
  renderResults, renderRecords, renderStandings,
} from './ui.js';

const app = {
  net: null, input: null, world: null, renderer: null, diag: null, tuner: null,
  phys: null, surfaces: null, tracks: [], records: {}, items: new Map(),
  me: { token: localStorage.getItem('zza.token') || null, name: '' },
  lobby: null, raceInfo: null, results: null,
  screen: 'connect', hz: 60, paused: false,
};
window.__zza = app;   // для отладки из консоли браузера

// ---------------------------------------------------------------- экраны

const SCREENS = ['connect', 'menu', 'lobby', 'race', 'results'];

function show(name) {
  app.screen = name;
  for (const s of SCREENS) {
    $('screen-' + s).classList.toggle('active', s === name);
  }
  app.input.enabled = (name === 'race' && !app.paused);
  if (name === 'race') resumeAudio();
}

let toastTimer = null;
function toast(text, ms = 1800) {
  const n = $('toast');
  n.textContent = text;
  n.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => n.classList.add('hidden'), ms);
}
app.toast = toast;

// ----------------------------------------------------------------- связь

function onStatus(state, detail) {
  const msg = $('connect-msg'), det = $('connect-detail'), retry = $('connect-retry');
  if (state === 'open') {
    hello();
    return;
  }
  if (app.screen !== 'connect' && state === 'closed') {
    toast('Связь с сервером потеряна, восстанавливаю…', 3000);
  }
  if (app.screen === 'connect') {
    msg.textContent = state === 'connecting'
      ? 'Подключаюсь к серверу…'
      : 'Сервер не отвечает, пробую снова…';
    if (detail) {
      det.textContent = `${detail}\n\nАдрес: ${location.host}\n\n` +
        'Если это чужой компьютер — проверьте, что игра там запущена\n' +
        'и что адрес набран верно.';
      det.classList.remove('hidden');
    }
    retry.classList.toggle('hidden', state === 'connecting');
  }
}

function hello() {
  app.net.send({ t: 'hello', token: app.me.token, name: nameValue() });
}

function nameValue() {
  const v = ($('name-input').value || '').trim();
  return v || localStorage.getItem('zza.name') || 'Гонщик';
}

// ------------------------------------------------------- приём сообщений

const HANDLERS = {
  hello_ok(m) {
    app.me.token = m.token;
    app.me.name = m.name;
    app.hz = m.hz;
    localStorage.setItem('zza.token', m.token);
    localStorage.setItem('zza.name', m.name);
    $('name-input').value = m.name;
    app.phys = m.physics;
    app.tracks = m.tracks;
    app.records = m.records || {};
    // Описания бонусов приходят с сервера, а не зашиты в клиент: новый бонус
    // должен добавляться строчкой в powerups.json и больше нигде.
    app.items = new Map((m.powerups || []).map((p) => [p.id, p]));
    app.surfaces = SURFACE_ORDER.map((n) => {
      const s = m.physics.surfaces[n];
      return [s.grip, s.drag, s.accel, s.maxSpeed];
    });
    if (!app.renderer) app.renderer = new Renderer($('cv'), app.phys);
    $('server-hint').textContent = `сервер ${location.host} · ${m.tracks.length} трасс`;
    if (app.screen === 'connect') show('menu');
  },

  lobbies(m) {
    app.lobbyList = m.list;
    if (app.screen === 'menu') renderLobbyList($('lobby-list'), m.list, joinLobby);
  },

  lobby(m) {
    app.lobby = m.lobby;
    renderLobby();
    if (app.screen === 'menu' || app.screen === 'connect') show('lobby');
    if (app.screen === 'results' && m.lobby.state === 'waiting') show('lobby');
  },

  chat(m) {
    if (!app.lobby) return;
    app.lobby.chat = (app.lobby.chat || []).concat([m]).slice(-20);
    renderChat();
  },

  peer(m) {
    if (!app.world) return;
    for (const car of app.world.cars.values()) {
      if (car.token === m.token) car.online = m.online;
    }
  },

  race_init(m) { startRace(m); },

  s(m) { onSnapshot(m); },

  results(m) {
    app.results = m;
    renderResultsScreen();
    show('results');
    sfx.finish();
  },

  tuned(m) {
    if (app.tuner) app.tuner.applyRemote(m.section, m.key, m.value);
  },

  err(m) {
    toast(m.msg || 'Ошибка');
    if (m.code === 'kicked' || m.code === 'nolobby') {
      app.lobby = null;
      show('menu');
    }
  },
};

function onMessage(m) {
  const fn = HANDLERS[m.t];
  if (fn) fn(m);
}

// ------------------------------------------------------------------ меню

function joinLobby(lb) {
  let pass = '';
  if (lb.locked) {
    pass = prompt(`Лобби «${lb.name}» под паролем:`) || '';
    if (!pass) return;
  }
  app.net.send({ t: 'lobby_join', id: lb.id, password: pass });
}

function trackById(id) { return app.tracks.find((t) => t.id === id); }

function updateDialogInfo() {
  const t = trackById($('opt-track').value);
  if (!t) return;
  $('opt-track-info').textContent =
    `${t.difficulty || 'трасса'} · круг ≈ ${Math.round(t.length / 330)} с · ${t.author || ''}`;
  const laps = Math.max(1, +$('opt-laps').value || t.laps);
  const mins = laps * (t.length / 330) / 60;
  $('opt-duration').textContent = `заезд примерно ${mins.toFixed(1)} мин`;
}

function openCreateDialog(editing) {
  const dlg = $('dlg-create');
  const sel = $('opt-track');
  sel.textContent = '';
  for (const t of app.tracks) {
    const o = document.createElement('option');
    o.value = t.id;
    o.textContent = `${t.name} (${t.difficulty || '—'})`;
    sel.appendChild(o);
  }
  const s = editing && app.lobby ? app.lobby.settings : null;
  $('dlg-title').textContent = editing ? 'Настройки лобби' : 'Создать лобби';
  $('dlg-ok').textContent = editing ? 'Сохранить' : 'Создать';
  $('opt-name').value = editing && app.lobby ? app.lobby.name : `Заезд ${app.me.name}`;
  $('opt-name').parentElement.classList.toggle('hidden', !!editing);
  sel.value = s ? s.track : (app.tracks[0] && app.tracks[0].id);
  $('opt-laps').value = s ? s.laps : (trackById(sel.value) || {}).laps || 3;
  $('opt-max').value = s ? s.max : 6;
  $('opt-powerups').checked = s ? s.powerups : true;
  $('opt-collisions').checked = s ? s.collisions : true;
  $('opt-pass').value = '';
  $('opt-pass').parentElement.classList.toggle('hidden', !!editing);
  updateDialogInfo();

  dlg.returnValue = '';
  dlg.showModal();
  dlg.onclose = () => {
    if (dlg.returnValue !== 'ok') return;
    const settings = {
      track: sel.value,
      laps: +$('opt-laps').value,
      max: +$('opt-max').value,
      powerups: $('opt-powerups').checked,
      collisions: $('opt-collisions').checked,
    };
    if (editing) app.net.send({ t: 'lobby_settings', settings });
    else app.net.send({ t: 'lobby_create', name: $('opt-name').value,
                        settings, password: $('opt-pass').value });
  };
}

// ----------------------------------------------------------------- лобби

function renderLobby() {
  const lb = app.lobby;
  if (!lb) return;
  $('lobby-name').textContent = lb.name;
  $('lobby-code').textContent = lb.id;
  $('lobby-count').textContent = `${lb.players.length}/${lb.settings.max}`;
  renderPlayers($('player-list'), lb, app.me.token,
                (tok) => app.net.send({ t: 'kick', token: tok }));

  const t = trackById(lb.settings.track);
  const bits = [
    t ? t.name : lb.settings.track,
    `${lb.settings.laps} кругов`,
    lb.settings.powerups ? 'с бонусами' : 'без бонусов',
    lb.settings.collisions ? 'со столкновениями' : 'без столкновений',
  ];
  const box = $('lobby-settings');
  box.textContent = '';
  for (const b of bits) box.appendChild(el('span', 'badge', b));
  if (t) {
    const mins = lb.settings.laps * (t.length / 330) / 60;
    box.appendChild(el('span', 'badge', `≈ ${mins.toFixed(1)} мин`));
  }

  const isHost = lb.host === app.me.token;
  $('btn-settings').classList.toggle('hidden', !isHost);
  $('btn-start').classList.toggle('hidden', !isHost);
  $('btn-ready').classList.toggle('hidden', isHost);
  const me = lb.players.find((p) => p.token === app.me.token);
  $('btn-ready').textContent = me && me.ready ? 'Не готов' : 'Готов';

  const others = lb.players.filter((p) => p.token !== lb.host);
  const waiting = others.filter((p) => !p.ready).length;
  $('start-hint').textContent = isHost
    ? (lb.players.length < 2
        ? 'Можно начать и одному — остальные зайдут на следующий заезд.'
        : waiting ? `Не готовы: ${waiting}. Начать можно в любой момент.` : 'Все готовы.')
    : 'Заезд начинает хозяин лобби.';
  renderChat();
}

function renderChat() {
  const log = $('chat-log');
  log.textContent = '';
  for (const c of (app.lobby && app.lobby.chat) || []) {
    const row = el('div', 'item-row');
    row.appendChild(el('b', null, c.name));
    row.appendChild(el('div', 'grow', c.text));
    log.appendChild(row);
  }
  log.scrollTop = log.scrollHeight;
}

// ----------------------------------------------------------------- заезд

function startRace(m) {
  app.raceInfo = m;
  app.hz = m.hz;
  const track = new TrackGeom(m.track);
  app.world = new World(track, app.phys, app.surfaces, m.hz, m.you);
  app.world.boxes = m.boxes || [];
  app.world.items = app.items;
  app.world.entities = [];
  for (const c of m.cars) app.world.addCar(c);
  app.world.tick = m.tick;
  app.world.serverTick = m.tick;

  // Стартовая расстановка до первого снапшота: без неё первый кадр был бы
  // с машинами в нулевой точке карты.
  for (const car of app.world.cars.values()) {
    car.rx = track.px[0]; car.ry = track.py[0]; car.ra = 0;
  }

  app.renderer.setTrack(track);
  app.diag.reset();
  app.paused = false;
  $('race-menu').classList.add('hidden');
  $('spectate').classList.toggle('hidden', m.you >= 0);
  $('hud-laps').textContent = '/' + m.laps;
  $('countdown').classList.remove('hidden');
  app.input.clear();
  show('race');
  // Только теперь у холста появился настоящий размер: до show() экран заезда
  // скрыт и clientWidth равен нулю.
  app.renderer.resize();
}

let lastCountShown = -1;

function onSnapshot(m) {
  if (!app.world) return;
  const events = app.world.onSnapshot(m);
  for (const ev of events) handleEvent(ev);

  if (m.st === 'countdown') {
    const left = Math.ceil((app.raceInfo.startTick - m.k) / app.hz);
    const node = $('countdown');
    node.classList.remove('hidden');
    node.textContent = left > 0 ? String(left) : 'ЕДЕМ!';
    if (left !== lastCountShown) {
      lastCountShown = left;
      if (left > 0) sfx.count();
    }
  } else {
    const node = $('countdown');
    if (!node.classList.contains('hidden')) {
      node.textContent = 'ЕДЕМ!';
      sfx.go();
      setTimeout(() => node.classList.add('hidden'), 600);
    }
  }
}

function handleEvent(ev) {
  const w = app.world, r = app.renderer;
  const mine = ev.slot === w.you;
  switch (ev.t) {
    case 'lap': {
      const car = w.cars.get(ev.slot);
      if (mine) { sfx.lap(); toast(`Круг ${ev.lap} — ${fmtTime(ev.ms)}`); }
      if (car) car.lap = ev.lap;
      break;
    }
    case 'pickup':
      if (mine) { sfx.pickup(); }
      break;
    case 'use':
      if (mine) sfx.use();
      break;
    case 'hit': {
      const car = w.cars.get(ev.slot);
      if (car) r.burst(car.rx, car.ry, 'rgba(255,120,60,', 14);
      if (mine) { sfx.hit(); toast('Попадание!'); }
      break;
    }
    case 'shield': {
      const car = w.cars.get(ev.slot);
      if (car) r.burst(car.rx, car.ry, 'rgba(79,195,247,', 12);
      if (mine) sfx.shield();
      break;
    }
    case 'bump':
      r.burst(ev.x, ev.y, 'rgba(255,255,255,', 6);
      if (ev.a === w.you || ev.b === w.you) sfx.bump();
      break;
    case 'boom':
      r.burst(ev.x, ev.y, 'rgba(255,180,60,', 18);
      break;
    case 'respawn':
      if (mine) { sfx.respawn(); toast('Возврат на трассу'); }
      break;
    case 'finish':
      if (mine) toast(`Финиш! Место ${ev.place}`, 3000);
      break;
    default:
      break;
  }
}

// --------------------------------------------------------------- цикл

let lastFrame = performance.now();
let acc = 0;
let hudSkip = 0;

function loop(now) {
  requestAnimationFrame(loop);
  const dtFrame = Math.min(0.25, (now - lastFrame) / 1000);
  const frameMs = now - lastFrame;
  lastFrame = now;
  app.diag.tickFrame(frameMs);

  if (app.screen !== 'race' || !app.world) {
    app.diag.render(app);
    return;
  }

  const w = app.world;
  const dt = 1 / app.hz;
  // Небольшое растяжение времени держит опережение сервера в нужном окне,
  // не разрывая историю скачком номера тика.
  acc += dtFrame * w.paceFactor();
  let steps = 0;
  while (acc >= dt && steps < 8) {
    acc -= dt;
    steps++;
    const mask = app.paused ? 0 : app.input.read();
    w.simTick(mask);
    // Шлём каждый тик, а не только при изменении: сервер измеряет по
    // свежести ввода, насколько клиент опережает его, и редкие посылки
    // сделали бы эту оценку бессмысленной.
    app.net.sendInput(w.tick, mask);
  }

  w.decayOffset(dtFrame);
  w.interpolate();

  const own = w.cars.get(w.you);
  if (own && w.state === 'racing') app.renderer.skid(own);

  app.renderer.frame(w, dtFrame);

  if (++hudSkip >= 4) { hudSkip = 0; updateHud(); }
  app.diag.render(app);
}

function updateHud() {
  const w = app.world;
  if (!w) return;
  const own = w.cars.get(w.you);
  const order = w.rank || [];
  const place = order.indexOf(w.you);
  $('hud-place').textContent = place >= 0 ? place + 1 : '—';
  $('hud-place-of').textContent = '/' + w.cars.size;
  $('hud-lap').textContent = own ? Math.min(own.lap + 1, app.raceInfo.laps) : 1;
  $('hud-time').textContent = fmtClock(w.elapsed || 0);
  $('hud-speed').textContent = own
    ? Math.round(Math.hypot(own.vx, own.vy) / 3) : 0;

  const item = $('hud-item');
  const icon = $('hud-item-icon');
  const has = own && own.item;
  const def = has ? app.items.get(own.item) : null;
  item.classList.toggle('empty', !has);
  item.classList.toggle('ready', !!has);
  icon.textContent = def ? def.short : '';
  item.style.borderColor = def ? def.color : '';
  item.title = def ? def.name : '';
  $('hud-item-name').textContent = def ? def.name : '';

  renderStandings($('standings'), w, app.raceInfo.cars);
}

// ------------------------------------------------------------ результаты

function renderResultsScreen() {
  const r = app.results;
  if (!r) return;
  $('res-title').textContent = r.mode === 'timetrial'
    ? `Заезд на время — ${r.trackName}` : `Результаты — ${r.trackName}`;
  renderResults($('res-table'), r.rows, app.me.token,
                app.raceInfo ? app.raceInfo.cars : []);
  const board = (r.boards && (r.boards.timetrial || r.boards.race)) || [];
  renderRecords($('res-records'), board);
  const isHost = app.lobby && app.lobby.host === app.me.token;
  $('btn-rematch').classList.toggle('hidden', !isHost);
  $('btn-to-lobby').classList.toggle('hidden', r.mode === 'timetrial');
}

// ----------------------------------------------------------------- старт

function bindUi() {
  $('connect-retry').onclick = () => app.net.connect();

  $('name-input').value = localStorage.getItem('zza.name') || '';
  $('name-input').onchange = () => {
    const v = nameValue();
    localStorage.setItem('zza.name', v);
    app.me.name = v;
    hello();
  };

  $('btn-create').onclick = () => openCreateDialog(false);
  $('btn-solo').onclick = () => {
    const t = app.tracks[0];
    if (!t) return;
    openSoloDialog();
  };
  $('btn-refresh').onclick = () => app.net.send({ t: 'lobby_list' });
  $('btn-join-code').onclick = () => {
    const code = ($('join-code').value || '').trim().toUpperCase();
    if (code) app.net.send({ t: 'lobby_join', id: code });
  };
  $('join-code').onkeydown = (e) => { if (e.key === 'Enter') $('btn-join-code').click(); };

  $('opt-track').onchange = () => {
    const t = trackById($('opt-track').value);
    if (t) $('opt-laps').value = t.laps;
    updateDialogInfo();
  };
  $('opt-laps').oninput = updateDialogInfo;

  $('btn-leave').onclick = () => { app.lobby = null; app.net.send({ t: 'lobby_leave' }); show('menu'); };
  $('btn-ready').onclick = () => {
    const me = app.lobby.players.find((p) => p.token === app.me.token);
    app.net.send({ t: 'ready', v: !(me && me.ready) });
  };
  $('btn-start').onclick = () => app.net.send({ t: 'start' });
  $('btn-settings').onclick = () => openCreateDialog(true);
  $('chat-form').onsubmit = (e) => {
    e.preventDefault();
    const v = $('chat-input').value.trim();
    if (v) app.net.send({ t: 'chat', text: v });
    $('chat-input').value = '';
  };

  $('btn-resume').onclick = () => setPaused(false);
  $('btn-quit').onclick = () => {
    app.net.send({ t: 'leave_race' });
    app.world = null;
    app.lobby = null;
    show('menu');
  };
  $('btn-rematch').onclick = () => app.net.send({ t: 'rematch' });
  $('btn-to-lobby').onclick = () => { if (app.lobby) show('lobby'); };
  $('btn-to-menu').onclick = () => {
    app.net.send({ t: 'lobby_leave' });
    app.lobby = null;
    show('menu');
  };

  $('cv').ondblclick = () => {
    if (!document.fullscreenElement) document.documentElement.requestFullscreen?.();
    else document.exitFullscreen?.();
  };
}

function openSoloDialog() {
  const dlg = $('dlg-create');
  const sel = $('opt-track');
  sel.textContent = '';
  for (const t of app.tracks) {
    const o = document.createElement('option');
    o.value = t.id;
    o.textContent = `${t.name} (${t.difficulty || '—'})`;
    sel.appendChild(o);
  }
  $('dlg-title').textContent = 'Заезд на время';
  $('dlg-ok').textContent = 'Поехали';
  $('opt-name').parentElement.classList.add('hidden');
  $('opt-pass').parentElement.classList.add('hidden');
  $('opt-max').parentElement.classList.add('hidden');
  $('opt-powerups').parentElement.classList.add('hidden');
  $('opt-collisions').parentElement.classList.add('hidden');
  sel.value = app.tracks[0].id;
  $('opt-laps').value = 3;
  updateDialogInfo();
  dlg.returnValue = '';
  dlg.showModal();
  dlg.onclose = () => {
    $('opt-max').parentElement.classList.remove('hidden');
    $('opt-powerups').parentElement.classList.remove('hidden');
    $('opt-collisions').parentElement.classList.remove('hidden');
    if (dlg.returnValue !== 'ok') return;
    app.net.send({ t: 'solo', settings: { track: sel.value, laps: +$('opt-laps').value } });
  };
}

function setPaused(v) {
  app.paused = v;
  $('race-menu').classList.toggle('hidden', !v);
  app.input.enabled = !v && app.screen === 'race';
  if (v) app.input.clear();
}

function bindKeys() {
  // Единственное место, где ловятся служебные клавиши. Input вызывает onKey
  // для всех клавиш, которых нет в раскладке управления, независимо от того,
  // включён ли игровой ввод — поэтому F3 работает и в меню, и в лобби.
  // Второй обработчик рядом был бы не подстраховкой, а двойным нажатием.
  app.input.onKey = (e) => {
    if (e.code === 'F3') { e.preventDefault(); app.diag.toggle(); }
    else if (e.code === 'F4') { e.preventDefault(); app.tuner.toggle(); }
    else if (e.code === 'Escape' && app.screen === 'race') setPaused(!app.paused);
    else if (e.code === 'KeyR' && app.screen === 'race' && !app.paused) {
      app.net.send({ t: 'respawn' });
    }
  };
}

function boot() {
  app.input = new Input();
  app.diag = new Diag();
  app.tuner = new Tuner(app);
  bindUi();
  bindKeys();
  app.net = new Net(onMessage, onStatus);
  app.net.connect();
  requestAnimationFrame(loop);
}

boot();
