// -*- coding: utf-8 -*-
// Меню, лобби, оверлей отладки и главный цикл (DESIGN.md 5, 6.1, 7.1, 8.7).
//
// ГЛАВНОЕ ПРАВИЛО ЭТОГО ФАЙЛА (8.7): состояния лобби здесь НЕТ. Ни списка
// игроков, ни выбранных параметров, ни «я хозяин» клиент сам не выводит —
// всё это приходит сообщением joined и перерисовывается целиком. Нажатие
// на кнопку — это просьба к серверу, а не изменение картинки. Иначе у двух
// вкладок мгновенно расходятся мнения о том, какой стоит сид.

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
  // --- лобби (8.7). Всё присланное сервером, ничего своего -------------
  host: 0,            // pid хозяина
  armed: false,       // хозяин нажал «начать»
  phase: '',          // lobby / playing / finished
  opts: {},           // выбранные параметры
  limits: null,       // что вообще разрешено выбирать — список от сервера
  optsBuilt: false,   // выпадающие списки уже наполнены значениями сервера
  roomsTimer: 0,      // список лобби обновляется сам, пока человек в меню
  // --- набор апгрейдов (11.7) ------------------------------------------
  // id сущности -> массив счётчиков. Приходит сообщением build; из событий
  // pick НЕ собирается никогда (5.2: событие теряется).
  builds: new Map(),
  dropEv: false,      // проверка 11.7: «событие потерялось» (см. __zza)
  // --- пинги на карте (8.8) --------------------------------------------
  // Живут ЗДЕСЬ, а не в state.js: мир — это то, что прислал сервер в
  // снапшоте, а пинг сущностью не является и ни одного поля мира не
  // трогает. Ключ записи — автор и тик постановки: сервер повторяет пинг,
  // пока тот жив (5.2, событие может потеряться), и повтор обязан обновить
  // запись, а не завести вторую.
  pings: [],
  pingsSeen: 0,       // сколько пингов ПРИШЛО за забег (приёмка)
  pingsSent: 0,       // сколько пингов УШЛО на сервер (приёмка)
  lastPingSent: null,
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

const MODE_RU = { descent: 'Спуск', siege: 'Осада' };
const PHASE_RU = { lobby: 'набор', playing: 'идёт игра', finished: 'закончено' };

// 8.7: из списка видно код, режим, число игроков и идёт ли уже игра.
function renderRoomList() {
  const box = $('roomlist');
  box.textContent = '';
  if (!app.rooms.length) {
    const p = document.createElement('div');
    p.className = 'dim';
    p.textContent = 'Лобби нет. Создай своё — код продиктуешь коллеге.';
    box.appendChild(p);
    return;
  }
  for (const r of app.rooms) {
    const row = document.createElement('div');
    const running = r.phase === 'playing';
    row.className = 'room' + (running ? ' busy' : '');
    const info = document.createElement('span');
    info.textContent = r.id +
      '  ·  ' + (MODE_RU[r.mode] || r.mode || '—') +
      '  ·  игроков ' + r.players + (r.max ? ('/' + r.max) : '') +
      '  ·  ' + (PHASE_RU[r.phase] || r.phase || '') +
      (running ? ('  ·  этаж ' + r.floor) : '');
    const b = document.createElement('button');
    // Закрытую партию показываем, но не предлагаем: постучаться в дверь,
    // которая не откроется, человек должен не глазами, а никак.
    const closed = running && r.join === false;
    b.textContent = closed ? 'закрыто' : 'Войти';
    b.disabled = closed;
    b.onclick = () => doJoin(r.id);
    row.appendChild(info);
    row.appendChild(b);
    box.appendChild(row);
  }
}

// 8.7: список собравшихся с готовностью каждого и отметкой хозяина.
// players — [pid, имя, готов, хозяин] из joined; своего мнения тут нет.
function renderPlayers() {
  const box = $('players');
  box.textContent = '';
  for (const p of app.players) {
    const d = document.createElement('div');
    d.className = 'pl';
    const rd = document.createElement('span');
    const ready = !!p[2];
    rd.className = 'rd ' + (ready ? 'ok' : 'no');
    rd.textContent = ready ? '✓ готов' : '· не готов';
    const nm = document.createElement('span');
    nm.textContent = (p[0] === app.net.pid ? '► ' : '   ') + p[1] +
                     '  (pid ' + p[0] + ')';
    d.appendChild(rd);
    d.appendChild(nm);
    if (p[3]) {
      const c = document.createElement('span');
      c.className = 'crown';
      c.textContent = '★ хозяин';
      d.appendChild(c);
    }
    box.appendChild(d);
  }
}

// ---------------------------------------------------------------- лобби
//
// Выпадающие списки наполняются ЗНАЧЕНИЯМИ СЕРВЕРА (joined.limits), а не
// зашитыми здесь константами. Это 8.7 буквально: список допустимых значений
// один, и он на сервере. Зашей их тут — и однажды клиент предложит режим,
// которого в сборке нет, а человек будет гадать, почему кнопка не работает.

function fillSelect(el, items) {
  el.textContent = '';
  for (const it of items) {
    const o = document.createElement('option');
    o.value = String(it.v);
    o.textContent = it.t;
    if (it.off) o.disabled = true;
    el.appendChild(o);
  }
}

function buildOptControls() {
  const L = app.limits;
  if (!L || app.optsBuilt) return;
  app.optsBuilt = true;
  const ready = L.modes_ready || L.modes || [];
  fillSelect($('optMode'), (L.modes || []).map((m) => ({
    v: m, t: (MODE_RU[m] || m) + (ready.indexOf(m) < 0 ? ' (не сделана)' : ''),
    off: ready.indexOf(m) < 0 })));
  fillSelect($('optTheme'), (L.themes || []).map((t) => ({ v: t, t: t })));
  const dn = L.diff_names || [];
  fillSelect($('optDiff'), (L.diffs || []).map((d, i) => ({ v: d, t: dn[i] || d })));
  const pr = L.players || [2, 6];
  const nums = [];
  for (let n = pr[0]; n <= pr[1]; n++) nums.push({ v: n, t: String(n) });
  fillSelect($('optMax'), nums);
}

function renderLobby() {
  const amHost = app.host === app.net.pid;
  const o = app.opts || {};
  buildOptControls();
  $('roomCode').textContent = app.room;
  renderPlayers();
  if (o.mode !== undefined) $('optMode').value = o.mode;
  if (o.theme !== undefined) $('optTheme').value = o.theme;
  if (o.diff !== undefined) $('optDiff').value = String(o.diff);
  if (o.max_players !== undefined) $('optMax').value = String(o.max_players);
  $('optFF').checked = !!o.ff;
  $('optJoin').checked = o.join_running !== false;
  // Поле сида не перебиваем, пока в нём печатают: иначе ответ сервера
  // съедает недонабранную цифру. Своё же значение показываем всегда.
  if (document.activeElement !== $('optSeed')) {
    $('optSeed').value = o.seed_auto ? '' : String(o.seed === undefined ? '' : o.seed);
  }
  $('optSeed').placeholder = o.seed_auto
    ? ('пусто = случайный (сейчас ' + o.seed + ')') : 'пусто = случайный';

  for (const id of ['optMode', 'optTheme', 'optSeed', 'optSeedBtn', 'optDiff',
                    'optMax', 'optFF', 'optJoin']) {
    $(id).disabled = !amHost;
  }
  $('readyBtn').textContent = app.ready ? 'Не готов' : 'Готов';
  $('startBtn').style.display = amHost ? '' : 'none';
  $('startBtn').textContent = app.armed ? 'Не начинать' : 'Начать игру';

  const waiting = app.players.filter((p) => !p[2]).map((p) => p[1]);
  $('hostNote').textContent = amHost
    ? ('Вы хозяин: параметры и старт — ваши. ' +
       (waiting.length ? ('Ждём: ' + waiting.join(', ')) : 'Все готовы.'))
    : ('Параметры меняет хозяин. ' +
       (app.armed ? 'Старт нажат — игра пойдёт, как только отметятся все.'
                  : 'Игру начинает хозяин.'));
}

function sendOpts(o) {
  app.net.opts(o);
}

// ---------------------------------------------------------------- сеть

function doJoin(code) {
  app.name = ($('name').value || '').trim();
  app.ready = false;
  app.optsBuilt = false;        // лимиты придут заново вместе с joined
  setStatus('вход…');
  app.net.hello(app.name);
  app.net.join(code || '', app.name);
}

function doReady() {
  // Своё «готов» здесь только для надписи на кнопке; правду о готовности
  // всё равно пришлёт сервер следующим joined.
  app.ready = !app.ready;
  app.net.ready(app.ready);
  $('readyBtn').textContent = app.ready ? 'Не готов' : 'Готов';
  setStatus(app.ready ? 'готов — ждём остальных' : 'в лобби');
}

function doStart() {
  app.net.start(!app.armed);
}

function doLeave() {
  app.net.leave();
  app.room = '';
  app.ready = false;
  app.players = [];
  show('menu');
  setStatus('');
  app.net.rooms();
}

function makeHandlers() {
  return {
    onOpen() {
      setStatus('соединение установлено');
      app.net.hello(($('name').value || '').trim());
      app.net.rooms();
      // Ввод шлём только в игре: в меню и лобби сущности нет, и 30 Гц
      // пустых сообщений — это трафик и работа сервера ни за что.
      // Опрос ввода 30 Гц (5.1). По дороге считаем УШЕДШИЕ пинги: без
      // этого числа «пинга не видно» неразличимо с «пинг не отправляли», а
      // это две разные поломки в двух разных половинах игры.
      app.net.startPump(() => {
        if (app.screen !== 'game') return null;
        const st = app.input.sample();
        if (st.btn & 96) { app.pingsSent++; app.lastPingSent = st.aim.slice(); }
        return st;
      });
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
      // 8.7: joined — это ВЕСЬ снимок лобби. Ничего не досочиняем.
      app.room = m.room;
      app.players = m.players || [];
      app.host = m.host || 0;
      app.armed = !!m.armed;
      app.phase = m.phase || '';
      if (m.opts) app.opts = m.opts;
      if (m.limits) { app.limits = m.limits; app.optsBuilt = false; }
      const me = app.players.find((p) => p[0] === m.pid);
      app.ready = !!(me && me[2]);
      app.world.applyJoined(m);
      renderLobby();
      if (app.screen === 'menu') {
        show('lobby');
        setStatus('лобби ' + m.room + ' — отметь готовность');
      }
    },
    // 11.7: набор апгрейдов. Единственный источник HUD «что я собрал».
    onBuild(m) {
      app.builds.set(m.id, m.ups || []);
    },
    onLevel(m, tiles) {
      if (!tiles) { setStatus('битая карта с сервера', true); return; }
      // 8.8: пинг — это координаты ЭТОГО этажа. Сервер их тоже чистит при
      // спуске (room.descend); клиент чистит сам, чтобы метка не пережила
      // смену карты даже на один кадр.
      app.pings.length = 0;
      app.world.applyLevel(m, tiles);
      show('game');
      setStatus('');
      resize();
      updateBuildHud();
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
    onEvent(m) {
      // dropEv — выключатель для проверки 11.7: он изображает ПОТЕРЮ
      // события. Набор апгрейдов обязан остаться верным и без ev.
      if (app.dropEv) return;
      // 8.8: пинг — это ev, но не вспышка. Вспышки (state.js) живут 0.3 с и
      // гаснут в темноте; пинг живёт секунды и в темноте виден нарочно.
      if (m.k === 'ping') { addPing(m); return; }
      app.world.pushEvent(m);
    },
    onError(m) { setStatus('сервер: ' + (m.msg || m.code), true); },
    onInputSent(seq, mv) { app.world.noteInput(seq, mv); },
  };
}

// ------------------------------------------------------- пинги (8.8)
//
// СРОК ЖИЗНИ СЧИТАЕТСЯ ОТ ТИКА СЕРВЕРА, А НЕ ОТ МОМЕНТА ПОЛУЧЕНИЯ. Сервер
// повторяет живой пинг (5.2: событие может потеряться) и в каждом повторе
// шлёт тик ПОСТАНОВКИ. Иначе пинг, поставленный три секунды назад, у
// вошедшего в идущую партию прожил бы ещё четыре — то есть «опасность»
// висела бы у одного дольше, чем у остальных, и группа видела бы разное.
const PING_LIFE_TICKS = 120;        // 4.0 с при 30 Гц; вывод — в room.py
const PING_FADE_MS = 400;           // последние 0.4 с пинг гаснет плавно
// Потолки ТЕ ЖЕ, что на сервере (room.PING_PER_PLAYER / PING_MAX), и стоят
// они здесь не для красоты. Сервер вытесняет третий пинг автора из СВОЕГО
// списка — то есть перестаёт его повторять, — но уже разосланный пинг
// живёт у клиента свои 4 секунды. Без потолка на клиенте спамер рисует
// всем 4.0/0.5 = 8 меток вместо двух: замерено, tests/client_ping.py
// показывала 5 живых там, где сервер держал 2. Потолок здесь закрывает это
// так же, как «клиенту не верят» на сервере: не верить надо в обе стороны.
const PING_PER_AUTHOR = 2;
const PING_MAX = 8;

function addPing(m) {
  const key = (m.a | 0) + ':' + (m.tick | 0);
  const now = performance.now();
  // Сколько пингу осталось, по тикам сервера. Свежий пинг обгоняет
  // latestTick на доли тика — отрицательный возраст это норма, не ошибка.
  const age = Math.max(0, (app.world.latestTick | 0) - (m.tick | 0));
  const left = (PING_LIFE_TICKS - age) * (1000 / TICK_HZ);
  if (left <= 0) return;
  for (let i = 0; i < app.pings.length; i++) {
    if (app.pings[i].key === key) { app.pings[i].dies = now + left; return; }
  }
  app.pings.push({ key: key, a: m.a | 0, u: m.u | 0,
                   x: +m.x, y: +m.y, nm: m.nm || '',
                   tick: m.tick | 0, born: now, dies: now + left });
  app.pingsSeen++;
  dropExtra(app.pings, m.a | 0);
}

// Лишние метки гасятся по ТИКУ ПОСТАНОВКИ, а не по времени прихода: тик
// приходит с сервера и одинаков у всех, а время прихода у каждого своё, и
// по нему четыре вкладки погасили бы разные пинги.
function dropExtra(ps, author) {
  for (;;) {
    let n = 0, old = -1, oldTick = Infinity;
    for (let i = 0; i < ps.length; i++) {
      if (ps[i].a !== author) continue;
      n++;
      if (ps[i].tick < oldTick) { oldTick = ps[i].tick; old = i; }
    }
    if (n <= PING_PER_AUTHOR || old < 0) break;
    ps.splice(old, 1);
  }
  while (ps.length > PING_MAX) {
    let old = 0;
    for (let i = 1; i < ps.length; i++) if (ps[i].tick < ps[old].tick) old = i;
    ps.splice(old, 1);
  }
}

function livePings(now) {
  const ps = app.pings;
  let k = 0;
  for (let i = 0; i < ps.length; i++) {
    if (ps[i].dies > now) ps[k++] = ps[i];
  }
  ps.length = k;
  return ps;
}

// Вид для рендера. 7.1 отдаёт бэкенду «состояние на 6.1»; пинги добавляются
// ЗДЕСЬ, одним местом на все пути рисования, — иначе стенд стоимости кадра
// (benchDrawFlush) мерил бы кадр без пингов и называл бы его кадром с ними.
function buildFrameView(now) {
  const view = app.world.buildView(now);
  view.pings = livePings(now);
  view.pingFade = PING_FADE_MS;
  view.now = now;
  return view;
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

  const view = buildFrameView(now);
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
  updateBuildHud();
}

// 11.7: «что я собрал». Берётся ИСКЛЮЧИТЕЛЬНО из сообщений build. Ни одно
// событие pick сюда не заглядывает: 5.2 прямо говорит, что событие может
// потеряться, а HUD, который врёт под нагрузкой, хуже отсутствующего.
function buildText(ups) {
  const names = (app.limits && app.limits.ups) || [];
  const parts = [];
  for (let i = 0; i < ups.length; i++) {
    if (!ups[i]) continue;
    parts.push((names[i] || ('апгрейд ' + i)) + (ups[i] > 1 ? (' x' + ups[i]) : ''));
  }
  return parts;
}

let buildKey = '';
function updateBuildHud() {
  const ups = app.builds.get(app.world.selfId) || [];
  const parts = buildText(ups);
  const text = parts.length ? ('набор: ' + parts.join(' · ')) : '';
  if (text === buildKey) return;      // текст в DOM — только когда он новый
  buildKey = text;
  $('buildHud').textContent = text;
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
                   tilePx: app.render.tilePx || 64 };
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
  // ЗАЧЕМ ЗДЕСЬ АДРЕСНАЯ СТРОКА. Планок частоты кадров в 2.1 две — q=low
  // (>=100 fps) и q=high (>=60), — и меряются они на ЦЕЛЕВОЙ машине, куда
  // ни playwright, ни стендов не завезти. Без этих трёх строк владелец
  // может запустить только q=high и только двумерный бэкенд, то есть
  // половину того, что 2.1 требует замерить. Значений по умолчанию это не
  // трогает: без параметров всё ровно как было — 2D и quality 'high'.
  //   ?backend=3d   поднять трёхмерный сразу
  //   ?q=low        минимальные настройки (сглаживание выключено)
  //   ?tile=48      другой масштаб (4.1: зум — ручка клиента)
  const qs = new URLSearchParams(location.search);
  const quality = qs.get('q') === 'low' ? 'low' : 'high';
  const tilePx = Math.max(8, Math.min(192, parseInt(qs.get('tile'), 10) || 64));
  app.render = createRenderer();
  const canvas = $('c');
  app.canvas = canvas;
  app.render.init(canvas, { quality: quality, tilePx: tilePx });
  app.input = new Input(canvas, (sx, sy) => app.render.screenToWorld(sx, sy)).attach();
  app.net = new Net(makeHandlers());

  $('createBtn').onclick = () => doJoin('');
  $('joinBtn').onclick = () => doJoin(($('code').value || '').trim().toUpperCase());
  $('refreshBtn').onclick = () => app.net.rooms();
  $('readyBtn').onclick = doReady;
  $('startBtn').onclick = doStart;
  $('leaveBtn').onclick = doLeave;

  // Параметры лобби (8.7). Каждый обработчик делает ровно одно: ПРОСИТ
  // сервер поставить значение. Ни один из них не меняет app.opts — картинку
  // перерисует ответ сервера, и только он.
  $('optMode').onchange = () => sendOpts({ mode: $('optMode').value });
  $('optTheme').onchange = () => sendOpts({ theme: $('optTheme').value });
  $('optDiff').onchange = () => sendOpts({ diff: parseInt($('optDiff').value, 10) });
  $('optMax').onchange = () => sendOpts({ max_players: parseInt($('optMax').value, 10) });
  $('optFF').onchange = () => sendOpts({ ff: $('optFF').checked });
  $('optJoin').onchange = () => sendOpts({ join_running: $('optJoin').checked });
  const sendSeed = () => {
    const v = ($('optSeed').value || '').trim();
    // Пусто = случайный (8.7). Непустое отправляем ЧИСЛОМ, если оно число;
    // если человек набрал буквы — пусть сервер и откажет, ему решать.
    sendOpts({ seed: v === '' ? null : (/^\d+$/.test(v) ? parseInt(v, 10) : v) });
  };
  $('optSeedBtn').onclick = sendSeed;
  $('optSeed').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendSeed();
  });
  $('backendBtn').onclick = () => setBackend(app.backend === '2d' ? '3d' : '2d');
  // Клавиша B свободна: input.js разбирает WASD/стрелки, F, R, E, Q,
  // Shift, пробел и (с 8.8) V, C — B среди них нет.
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
  // Список лобби обновляется сам, пока человек в меню (8.7): коллега создал
  // лобби минуту назад, и жать «обновить» ради этого никто не обязан. В
  // игре и в лобби таймер молчит — там список не виден вовсе.
  app.roomsTimer = setInterval(() => {
    if (app.screen === 'menu' && app.net.open) app.net.rooms();
  }, 2000);
  if (qs.get('backend') === '3d') setBackend('3d');
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

    // --- лобби (tests/client_lobby_*.py, 8.7) -------------------------
    // Только ЧТЕНИЕ показаний и те же действия, что делают кнопки. Своего
    // пути в обход интерфейса тут нет: проверка нажимает кнопки.
    lobby: () => ({ room: app.room, host: app.host, armed: app.armed,
                    phase: app.phase, ready: app.ready,
                    players: app.players, opts: app.opts,
                    limits: app.limits }),
    isHost: () => app.host === app.net.pid,
    rooms: () => app.rooms,
    token: () => app.net.token,
    // Сырой путь для проверки «клиенту нельзя верить»: послать параметр
    // мимо интерфейса, как это сделал бы кривой или злой клиент.
    sendOpts: (o) => { app.net.opts(o); return true; },
    sendStart: (v) => { app.net.start(v); return true; },

    // --- набор апгрейдов (11.7) ---------------------------------------
    myBuild: () => (app.builds.get(app.world.selfId) || []).slice(),
    builds: () => Array.from(app.builds.entries()),
    buildHud: () => $('buildHud').textContent,
    // «Событие потерялось»: ev в клиент не попадает вовсе. Набор обязан
    // остаться верным — он приходит сообщением build, а не из ev.
    setDropEv: (v) => { app.dropEv = !!v; return app.dropEv; },
    getDropEv: () => app.dropEv,

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

    // --- пинги на карте (tests/client_ping_*.py, 8.8) ------------------
    // Показания: что клиент СЕЙЧАС считает живым пингом. Рисование этого
    // не заменяет — приёмка смотрит пиксели, а не этот список.
    pings: () => app.pings.map((p) => ({ a: p.a, u: p.u, x: p.x, y: p.y,
                                         nm: p.nm, left: p.dies - performance.now() })),
    pingsSeen: () => app.pingsSeen,
    pingsSent: () => app.pingsSent,
    lastPingSent: () => app.lastPingSent,
    // Куда поставлен пинг — в ЭКРАННЫХ пикселях, из рендера. Проверке
    // нужно знать, где искать метку, и списывать эту арифметику в питон
    // значило бы завести вторую истину о камере.
    pingScreen: (i) => {
      const p = app.pings[i];
      if (!p) return null;
      const s2 = app.render.worldToScreen(p.x, p.y);
      return { x: s2[0], y: s2[1], u: p.u, nm: p.nm };
    },
    // Пинг в точку мира мимо мыши: за кадр мышью не ткнуть, а стрелку к
    // внешнему пингу проверить надо. Уезжает тем же опросом 30 Гц, что и
    // нажатие клавиши, — см. input.pingAtWorld. Обычный путь человека
    // (клавиша V/C, колесо мыши) приёмка тоже проходит.
    pingAt: (x, y, kind) => app.input.pingAtWorld(x, y, !!kind),
    // Число нажатий пинга, посчитанное самим input.js: по нему видно, что
    // зажатая клавиша даёт ОДИН пинг, а не тридцать.
    pingPresses: () => app.input.pings,
    // Выключатель рисования пингов — затем же, зачем setFog и setCombat:
    // чтобы стоимость кадра мерилась на ОДНОЙ сцене спина к спине.
    setPingDraw: (v) => { app.render.pings = !!v; return app.render.pings; },
    getPingDraw: () => app.render.pings,

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
      const view = buildFrameView(performance.now());
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
      const view = buildFrameView(performance.now());
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
      const view = buildFrameView(performance.now());
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
