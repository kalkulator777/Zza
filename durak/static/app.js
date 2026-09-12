/* Дурак — клиент. Ванильный JS, без сборки и внешних библиотек. */
'use strict';

const SUITS = { S: '♠', H: '♥', D: '♦', C: '♣' };
const RED = { H: true, D: true };
const RANK_RU = { J: 'В', Q: 'Д', K: 'К', A: 'Т' };
const DECK_LABEL = { 24: '24 (9…Т)', 32: '32 (7…Т)', 36: '36 (6…Т)', 52: '52 (2…Т)' };

function plural(n, one, few, many) {
  const a = Math.abs(n);
  if (a % 10 === 1 && a % 100 !== 11) return n + ' ' + one;
  if (a % 10 >= 2 && a % 10 <= 4 && (a % 100 < 12 || a % 100 > 14)) return n + ' ' + few;
  return n + ' ' + many;
}

const SLOT = new URLSearchParams(location.search).get('p');
const TOKEN_KEY = 'durak_token' + (SLOT ? '_' + SLOT : '');
const NAME_KEY = 'durak_name' + (SLOT ? '_' + SLOT : '');

const state = {
  token: localStorage.getItem(TOKEN_KEY) || null,
  name: localStorage.getItem(NAME_KEY) || '',
  rooms: [],
  room: null,
  selected: null,       // выбранная в руке карта
  resultHidden: false,
};

let ws = null;
let retryDelay = 500;
let toastTimer = null;

const $ = (id) => document.getElementById(id);

/* ---------- связь ---------- */

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function setConn(cls, text) {
  const el = $('conn');
  el.className = 'conn ' + cls;
  el.textContent = text;
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws');

  ws.onopen = () => {
    retryDelay = 500;
    setConn('ok', 'на связи');
    if (state.name) send({ t: 'hello', token: state.token, name: state.name });
    else showScreen('screen-login');
  };
  ws.onclose = () => {
    setConn('bad', 'нет связи, переподключаюсь…');
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 5000);
  };
  ws.onerror = () => setConn('bad', 'ошибка соединения');
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    handle(msg);
  };
}

function handle(msg) {
  switch (msg.t) {
    case 'hello':
      state.token = msg.token;
      state.name = msg.name;
      localStorage.setItem(TOKEN_KEY, msg.token);
      localStorage.setItem(NAME_KEY, msg.name);
      $('me-box').classList.remove('hidden');
      $('me-name').textContent = msg.name;
      break;
    case 'rooms':
      state.rooms = msg.rooms;
      state.room = null;
      renderRooms();
      showScreen('screen-lobby');
      break;
    case 'room': {
      const prev = state.room;
      state.room = msg.room;
      state.selected = null;
      const wasFinished = prev && prev.game && prev.game.finished;
      const nowFinished = msg.room.game && msg.room.game.finished;
      if (!nowFinished || !wasFinished) state.resultHidden = false;
      renderRoom();
      break;
    }
    case 'err':
      toast(msg.msg);
      break;
  }
}

function toast(text) {
  const el = $('toast');
  el.textContent = text;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 4000);
}

/* ---------- экраны ---------- */

function showScreen(id) {
  for (const s of document.querySelectorAll('.screen')) {
    s.classList.toggle('active', s.id === id);
  }
}

/* ---------- лобби ---------- */

function rulesText(s) {
  return [
    'колода ' + (DECK_LABEL[s.deck_size] || s.deck_size),
    'до ' + s.max_players + ' игроков',
    s.transfer ? 'переводной' : 'обычный',
    'подкидывают ' + (s.throw_in === 'neighbors' ? 'соседи' : 'все'),
    'в кону до ' + s.max_attack_cards,
  ].join(' · ');
}

function renderRooms() {
  const box = $('rooms-list');
  box.textContent = '';
  if (!state.rooms.length) {
    const p = document.createElement('div');
    p.className = 'empty';
    p.textContent = 'Пока пусто — создайте комнату справа.';
    box.appendChild(p);
    return;
  }
  for (const r of state.rooms) {
    const row = document.createElement('div');
    row.className = 'room-row';

    const code = document.createElement('span');
    code.className = 'code';
    code.textContent = r.id;

    const mid = document.createElement('div');
    const title = document.createElement('div');
    title.textContent = r.title;
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = r.players + '/' + r.max + ' · ' + rulesText(r.settings) +
      (r.in_game ? ' · идёт партия' : '');
    mid.appendChild(title);
    mid.appendChild(meta);

    const btn = document.createElement('button');
    btn.textContent = r.in_game ? 'Идёт партия' : 'Войти';
    btn.disabled = r.in_game || r.players >= r.max;
    btn.onclick = () => send({ t: 'join', room: r.id });

    row.appendChild(code);
    row.appendChild(mid);
    row.appendChild(btn);
    box.appendChild(row);
  }
}

function readSettings(prefix) {
  return {
    deck_size: Number($(prefix + 'deck_size').value),
    max_players: Number($(prefix + 'max_players').value),
    throw_in: $(prefix + 'throw_in').value,
    max_attack_cards: Number($(prefix + 'max_attack_cards').value),
    transfer: $(prefix + 'transfer').checked,
  };
}

function writeSettings(prefix, s) {
  $(prefix + 'deck_size').value = String(s.deck_size);
  $(prefix + 'max_players').value = String(s.max_players);
  $(prefix + 'throw_in').value = s.throw_in;
  $(prefix + 'max_attack_cards').value = String(s.max_attack_cards);
  $(prefix + 'transfer').checked = !!s.transfer;
}

function checkCreateLimits() {
  const s = readSettings('c_');
  const fit = Math.floor(s.deck_size / 6);
  const warn = $('create-warn');
  if (s.max_players > fit) {
    warn.textContent = 'В колоде из ' + s.deck_size + ' карт помещается максимум ' +
      fit + ' игроков по 6 карт.';
    warn.classList.remove('hidden');
    return false;
  }
  warn.classList.add('hidden');
  return true;
}

/* ---------- комната ---------- */

function renderRoom() {
  const room = state.room;
  if (!room) return;
  if (room.phase === 'game') {
    renderGame(room);
    showScreen('screen-game');
    renderChat($('game-chat'), room.chat);
    return;
  }
  showScreen('screen-room');

  $('room-title').textContent = room.title;
  $('room-code').textContent = room.id;

  const box = $('room-players');
  box.textContent = '';
  for (const p of room.players) {
    const row = document.createElement('div');
    row.className = 'player-row';

    const dot = document.createElement('span');
    dot.className = 'dot' + (p.online ? '' : ' off');
    const nm = document.createElement('span');
    nm.textContent = p.name + (p.id === room.you ? ' (вы)' : '');
    const tag = document.createElement('span');
    tag.className = 'tag';
    const tags = [];
    if (p.host) tags.push('ведущий');
    if (!p.online) tags.push('не в сети');
    if (p.losses) tags.push('дурак ×' + p.losses);
    tag.textContent = tags.join(', ');

    row.appendChild(dot);
    row.appendChild(nm);
    row.appendChild(tag);

    if (room.is_host && p.id !== room.you) {
      const kick = document.createElement('button');
      kick.className = 'link';
      kick.textContent = 'выгнать';
      kick.style.marginLeft = 'auto';
      kick.onclick = () => send({ t: 'kick', pid: p.id });
      row.appendChild(kick);
    }
    box.appendChild(row);
  }

  $('btn-start').disabled = !room.can_start;
  $('room-hint').textContent = room.is_host
    ? (room.players.length < 2 ? 'Нужен хотя бы один соперник. Код комнаты: ' + room.id
      : 'Все на месте — можно начинать. Код комнаты: ' + room.id)
    : 'Ждём, пока ведущий начнёт партию. Код комнаты: ' + room.id;

  $('room-settings-view').textContent = rulesText(room.settings);
  $('room-settings-view').classList.toggle('hidden', room.is_host);
  $('room-settings-edit').classList.toggle('hidden', !room.is_host);
  if (room.is_host) writeSettings('r_', room.settings);

  renderChat($('room-chat'), room.chat);
}

function renderChat(box, messages) {
  box.textContent = '';
  for (const m of messages || []) {
    const div = document.createElement('div');
    div.className = 'msg';
    if (m.name === null) {
      div.classList.add('sys');
      div.textContent = m.text;
    } else {
      const who = document.createElement('span');
      who.className = 'who';
      who.textContent = m.name + ': ';
      div.appendChild(who);
      div.appendChild(document.createTextNode(m.text));
    }
    box.appendChild(div);
  }
  box.scrollTop = box.scrollHeight;
}

/* ---------- карты ---------- */

function cardEl(cid, trump) {
  const suit = cid.slice(-1);
  const rank = cid.slice(0, -1);
  const el = document.createElement('div');
  el.className = 'card' + (RED[suit] ? ' red' : '') + (suit === trump ? ' trump-suit' : '');
  el.dataset.cid = cid;

  const r = document.createElement('span');
  r.className = 'r';
  r.textContent = RANK_RU[rank] || rank;
  const s = document.createElement('span');
  s.className = 's';
  s.textContent = SUITS[suit];
  const big = document.createElement('span');
  big.className = 'big';
  big.textContent = SUITS[suit];

  el.appendChild(r);
  el.appendChild(s);
  el.appendChild(big);
  return el;
}

function cardActions(game, cid) {
  const a = game.actions;
  const acts = [];
  if (a.attack.indexOf(cid) >= 0) acts.push({ type: 'attack' });
  if (a.transfer.indexOf(cid) >= 0) acts.push({ type: 'transfer' });
  for (const i of (a.defend[cid] || [])) acts.push({ type: 'defend', target: i });
  return acts;
}

function doAction(act, cid) {
  send({ t: 'move', action: act.type, card: cid, target: act.target });
  state.selected = null;
}

/* ---------- игровой стол ---------- */

function renderGame(room) {
  const g = room.game;
  const trump = g.trump;
  const mySeat = g.seat;

  /* соперники — по кругу, начиная со следующего за мной */
  const opp = $('opponents');
  opp.textContent = '';
  const n = g.players.length;
  const order = [];
  for (let i = 1; i <= n; i++) {
    const p = g.players[mySeat === null ? i - 1 : (mySeat + i) % n];
    if (p && p.seat !== mySeat) order.push(p);
  }
  for (const p of order) {
    const div = document.createElement('div');
    div.className = 'opp' + (p.role ? ' ' + p.role : '') + (p.out ? ' out' : '');

    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = p.name;
    div.appendChild(nm);

    const role = document.createElement('span');
    role.className = 'role' + (p.role === 'defender' ? ' def' : '');
    role.textContent = p.role === 'attacker' ? '⚔ ходит'
      : p.role === 'defender' ? '🛡 отбивается' : '';
    div.appendChild(role);

    const fan = document.createElement('div');
    fan.className = 'mini-fan';
    for (let i = 0; i < Math.min(p.cards, 8); i++) {
      const b = document.createElement('div');
      b.className = 'mini-back';
      fan.appendChild(b);
    }
    div.appendChild(fan);

    const st = document.createElement('span');
    st.className = 'state';
    const bits = [plural(p.cards, 'карта', 'карты', 'карт')];
    if (p.out) bits.push('вышел');
    if (p.passed && !p.out) bits.push('пас');
    if (!p.online) bits.push('не в сети');
    st.textContent = bits.join(' · ');
    div.appendChild(st);

    opp.appendChild(div);
  }

  /* колода и козырь */
  const deck = $('deck-box');
  deck.textContent = '';
  if (g.deck > 0) {
    const stack = document.createElement('div');
    stack.className = 'deck-stack';
    if (g.trump_card) {
      const tc = cardEl(g.trump_card, trump);
      tc.classList.add('trump-card');
      stack.appendChild(tc);
    }
    const back = document.createElement('div');
    back.className = 'back';
    stack.appendChild(back);
    deck.appendChild(stack);
    deck.appendChild(document.createTextNode(
      'в колоде ' + plural(g.deck, 'карта', 'карты', 'карт')));
  } else {
    const lbl = document.createElement('div');
    lbl.textContent = 'колода пуста';
    deck.appendChild(lbl);
    const t = document.createElement('div');
    t.className = 'trump-alone';
    t.style.fontSize = '20px';
    t.style.color = RED[trump] ? '#ff7070' : '#dfe6ef';
    t.textContent = 'козырь ' + SUITS[trump];
    deck.appendChild(t);
  }

  const disc = $('discard-box');
  disc.textContent = g.discard ? 'в отбое\n' + plural(g.discard, 'карта', 'карты', 'карт') : '';

  /* стол */
  const tbl = $('table-cards');
  tbl.textContent = '';
  const sel = state.selected;
  const targets = sel && g.actions.defend[sel] ? g.actions.defend[sel] : [];
  if (!g.table.length) {
    const hint = document.createElement('span');
    hint.className = 'table-hint';
    hint.textContent = 'стол пуст';
    tbl.appendChild(hint);
  }
  g.table.forEach((pair, i) => {
    const box = document.createElement('div');
    box.className = 'pair' + (targets.indexOf(i) >= 0 ? ' target' : '');
    const att = cardEl(pair.a, trump);
    att.classList.add('att');
    box.appendChild(att);
    if (pair.d) {
      const def = cardEl(pair.d, trump);
      def.classList.add('def');
      box.appendChild(def);
    }
    if (targets.indexOf(i) >= 0) {
      box.onclick = () => doAction({ type: 'defend', target: i }, sel);
    }
    tbl.appendChild(box);
  });

  /* строка состояния */
  const me = mySeat === null ? null : g.players[mySeat];
  const attName = g.players[g.attacker] ? g.players[g.attacker].name : '';
  const defName = g.players[g.defender] ? g.players[g.defender].name : '';
  const status = $('game-status');
  status.textContent = '';
  if (g.finished) {
    status.textContent = 'Партия закончена';
  } else if (mySeat === null) {
    status.textContent = 'Вы наблюдаете · ходит ' + attName + ' · отбивается ' + defName;
  } else if (mySeat === g.defender) {
    status.textContent = g.taking
      ? 'Вы забираете карты · ждём, подкинут ли ещё'
      : g.table.length ? 'Вы отбиваетесь · ходит ' + attName
        : 'Ходит ' + attName + ' · отбиваетесь вы';
  } else if (mySeat === g.attacker && !g.table.length) {
    status.textContent = 'Ваш ход · отбивается ' + defName;
  } else if (g.actions.attack.length) {
    status.textContent = 'Можно подкинуть · отбивается ' + defName;
  } else {
    status.textContent = 'Ходит ' + attName + ' · отбивается ' + defName;
  }
  if (sel && targets.length) {
    status.appendChild(document.createTextNode(' · '));
    const b = document.createElement('b');
    b.textContent = 'выберите карту, которую бьёте';
    status.appendChild(b);
  }

  /* кнопки */
  const acts = $('game-actions');
  acts.textContent = '';
  const addBtn = (text, cls, fn, disabled) => {
    const b = document.createElement('button');
    b.textContent = text;
    if (cls) b.className = cls;
    b.disabled = !!disabled;
    b.onclick = fn;
    acts.appendChild(b);
    return b;
  };

  if (!g.finished && mySeat !== null) {
    const a = g.actions;
    if (a.can_take) addBtn('Беру', 'danger', () => send({ t: 'move', action: 'take' }));
    if (a.can_pass) {
      const allBeaten = g.table.every((p) => p.d);
      addBtn(g.taking ? 'Хватит' : (allBeaten ? 'Бито' : 'Пас'), 'primary',
        () => send({ t: 'move', action: 'pass' }));
    }
    if (sel) {
      const selActs = cardActions(g, sel);
      const cardName = (RANK_RU[sel.slice(0, -1)] || sel.slice(0, -1)) + SUITS[sel.slice(-1)];
      for (const act of selActs) {
        if (act.type === 'attack') {
          addBtn((g.table.length ? 'Подкинуть ' : 'Ходить ') + cardName, '',
            () => doAction(act, sel));
        } else if (act.type === 'transfer') {
          addBtn('Перевести ' + cardName, '', () => doAction(act, sel));
        }
      }
      if (selActs.length > 1 || targets.length > 1) {
        addBtn('Отмена', '', () => { state.selected = null; renderRoom(); });
      }
    }
  }
  if (room.is_host && !g.finished) {
    addBtn('Прервать партию', 'link', () => {
      if (confirm('Прервать партию и вернуться в комнату?')) send({ t: 'abort' });
    });
  }
  if (room.is_host && g.finished) {
    addBtn('Новая партия', 'primary', () => send({ t: 'again' }));
  }

  /* моя рука */
  const hand = $('my-hand');
  hand.textContent = '';
  for (const cid of g.hand) {
    const el = cardEl(cid, trump);
    const acts2 = g.finished ? [] : cardActions(g, cid);
    if (acts2.length) {
      el.classList.add('playable');
      el.onclick = () => {
        if (acts2.length === 1) doAction(acts2[0], cid);
        else state.selected = (state.selected === cid ? null : cid);
        renderRoom();
      };
    } else {
      el.classList.add('dim');
    }
    if (cid === sel) el.classList.add('selected');
    hand.appendChild(el);
  }
  if (!g.hand.length) {
    const p = document.createElement('span');
    p.className = 'table-hint';
    p.textContent = mySeat === null ? 'вы не за столом' : 'карт нет';
    hand.appendChild(p);
  }

  /* журнал */
  const log = $('game-log');
  log.textContent = '';
  for (const line of g.log) {
    const d = document.createElement('div');
    d.textContent = line;
    log.appendChild(d);
  }
  log.scrollTop = log.scrollHeight;

  /* итог */
  const res = $('result');
  if (g.finished && !state.resultHidden) {
    const iAmDurak = me && g.durak === me.id;
    const durakName = g.players.find((p) => p.id === g.durak);
    $('result-title').textContent = g.draw ? 'Ничья'
      : iAmDurak ? 'Вы дурак :)' : 'Дурак — ' + (durakName ? durakName.name : '?');
    $('result-text').textContent = room.is_host
      ? 'Можно сразу начать новую партию.'
      : 'Ждём, пока ведущий начнёт новую партию.';
    $('btn-again').classList.toggle('hidden', !room.is_host);
    res.classList.remove('hidden');
  } else {
    res.classList.add('hidden');
  }
}

/* ---------- обработчики ---------- */

function login() {
  const name = $('login-name').value.trim();
  if (!name) return toast('Введите имя');
  state.name = name;
  localStorage.setItem(NAME_KEY, name);
  send({ t: 'hello', token: state.token, name: name });
}

function onEnter(id, fn) {
  $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') fn(); });
}

function init() {
  $('login-name').value = state.name;
  $('btn-login').onclick = login;
  onEnter('login-name', login);

  $('btn-rename').onclick = () => {
    const name = prompt('Новое имя', state.name);
    if (name && name.trim()) send({ t: 'name', name: name.trim() });
  };

  $('btn-create').onclick = () => {
    if (!checkCreateLimits()) return;
    send({ t: 'create', title: $('c_title').value, settings: readSettings('c_') });
  };
  for (const id of ['c_deck_size', 'c_max_players']) $(id).onchange = checkCreateLimits;

  const joinByCode = () => {
    const code = $('join-code').value.trim().toUpperCase();
    if (code) send({ t: 'join', room: code });
  };
  $('btn-join-code').onclick = joinByCode;
  onEnter('join-code', joinByCode);

  $('btn-start').onclick = () => send({ t: 'start' });
  $('btn-leave').onclick = () => send({ t: 'leave' });
  $('btn-game-leave').onclick = () => send({ t: 'leave' });
  $('btn-save-settings').onclick = () =>
    send({ t: 'settings', settings: readSettings('r_') });

  $('btn-again').onclick = () => send({ t: 'again' });
  $('btn-result-close').onclick = () => {
    state.resultHidden = true;
    $('result').classList.add('hidden');
  };

  const chatSend = (inputId) => {
    const el = $(inputId);
    const text = el.value.trim();
    if (!text) return;
    send({ t: 'chat', text: text });
    el.value = '';
  };
  $('btn-room-chat').onclick = () => chatSend('room-chat-input');
  onEnter('room-chat-input', () => chatSend('room-chat-input'));
  $('btn-game-chat').onclick = () => chatSend('game-chat-input');
  onEnter('game-chat-input', () => chatSend('game-chat-input'));

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && state.selected) { state.selected = null; renderRoom(); }
  });

  showScreen(state.name ? 'screen-lobby' : 'screen-login');
  connect();
}

init();
