/* Склейка: сеть, экраны, игровой цикл. */
const NET = new Net();
let CAT = { heroes: [], arenas: [], modes: [] };
let ROOM = null, MYPID = null, MYTEAM = 0, INMATCH = false;
/* Код комнаты, в которую надо попасть: из ссылки ?join= или из прошлой сессии
   (после обрыва связи возвращаемся туда же сами). */
let PENDING_JOIN = new URLSearchParams(location.search).get('join')
  || sessionStorage.getItem('zza.room') || null;
let EVER_CONNECTED = false, FAILS = 0;
const cv = document.getElementById('cv');

function nick() { return (document.getElementById('nick').value || '').trim() || 'Игрок'; }

/* ---------------- инициализация ---------------- */
document.getElementById('nick').value = localStorage.getItem('zza.nick') || '';
document.getElementById('nick').addEventListener('change', () => {
  localStorage.setItem('zza.nick', nick());
  NET.send({ t: 'hello', name: nick() });
});

Input.bind(cv);

NET.on('welcome', m => {
  MYPID = m.pid;
  CAT = { heroes: m.heroes, arenas: m.arenas, modes: m.modes };
  Render.init(cv, CAT.heroes);
  ['m-arena', 'l-arena'].forEach(id => {
    const s = document.getElementById(id);
    s.innerHTML = '';
    CAT.arenas.forEach(a => {
      const o = document.createElement('option');
      o.value = a.id; o.textContent = a.name + ' — ' + a.hint;
      s.appendChild(o);
    });
  });
  NET.send({ t: 'hello', name: nick() });
  if (PENDING_JOIN && !ROOM) NET.send({ t: 'join', code: PENDING_JOIN });
});

NET.on('_open', () => { EVER_CONNECTED = true; FAILS = 0; });

NET.on('_close', () => {
  FAILS++;
  if (EVER_CONNECTED) {
    if (FAILS === 1) UI.toast('Связь потеряна, переподключаюсь…', true);
  } else if (FAILS === 2) {
    UI.toast(`Не получается подключиться к ${location.host}. Проверь, что игра там запущена, `
      + 'и что порт не закрыт файрволом.', true);
  }
});
NET.on('error', m => {
  UI.toast(m.m, true);
  if (/не найдена/i.test(m.m)) { PENDING_JOIN = null; sessionStorage.removeItem('zza.room'); }
});
NET.on('kicked', () => { ROOM = null; PENDING_JOIN = null; sessionStorage.removeItem('zza.room'); UI.show('menu'); UI.toast('Тебя убрали из комнаты', true); });
NET.on('room_closed', () => { ROOM = null; PENDING_JOIN = null; sessionStorage.removeItem('zza.room'); INMATCH = false; UI.show('menu'); UI.toast('Комната закрыта'); });
NET.on('left', () => { ROOM = null; PENDING_JOIN = null; sessionStorage.removeItem('zza.room'); UI.show('menu'); });

NET.on('room', m => {
  ROOM = m;
  PENDING_JOIN = null;
  sessionStorage.setItem('zza.room', m.code);
  if (location.search) history.replaceState({}, '', location.pathname);
  const me = m.players.find(p => p.pid === MYPID);
  if (me) MYTEAM = me.team;
  if (!INMATCH) UI.show('lobby');
  UI.renderLobby(m, MYPID, CAT);
});

NET.on('chat', m => UI.chatLine(m.n, m.m));

NET.on('match_start', m => {
  INMATCH = true;
  Render.setMatch(m, MYPID);
  UI.closeModal();
  UI.show('game');
  document.getElementById('killfeed').innerHTML = '';
  centerMsg('', 0);
});

NET.on('s', snap => {
  Render.push(snap);
  handleAnnounce(snap);
});

NET.on('match_end', m => {
  INMATCH = false;
  UI.show('lobby');
  UI.results(m.result, CAT, MYTEAM);
});
NET.on('match_abort', () => { INMATCH = false; UI.show('lobby'); UI.toast('Матч прерван'); });

/* ---------------- меню ---------------- */
document.getElementById('btn-create').onclick = () => {
  NET.send({ t: 'hello', name: nick() });
  NET.send({
    t: 'create', rname: 'Игра ' + nick(),
    mode: document.getElementById('m-mode').value,
    arena: document.getElementById('m-arena').value,
    stocks: +document.getElementById('m-stocks').value
  });
};
document.getElementById('btn-join').onclick = doJoin;
document.getElementById('join-code').addEventListener('keydown', e => { if (e.key === 'Enter') doJoin(); });

function doJoin() {
  const v = document.getElementById('join-code').value.trim();
  if (!v) return;
  if (v.includes('.') || v.includes(':')) {
    let url = v.startsWith('http') ? v : 'http://' + v;
    if (!/:\d+/.test(url)) url += ':8777';
    location.href = url;
    return;
  }
  NET.send({ t: 'hello', name: nick() });
  NET.send({ t: 'join', code: v.toUpperCase() });
}
document.getElementById('btn-heroes').onclick = () => UI.heroModal(CAT);
document.querySelector('.modal-x').onclick = () => UI.closeModal();
document.getElementById('modal').addEventListener('click', e => {
  if (e.target.id === 'modal') UI.closeModal();
});

/* опрос списка игр */
async function pollGames() {
  if (UI.cur !== 'menu') return;
  try {
    const r = await fetch('/api/games', { cache: 'no-store' });
    UI.renderGames(await r.json(), (code, spec) => {
      NET.send({ t: 'hello', name: nick() });
      NET.send({ t: 'join', code, spec });
    });
  } catch (e) { /* сервер перезапускается */ }
}
setInterval(pollGames, 2000);
pollGames();

/* ---------------- лобби ---------------- */
document.getElementById('btn-leave').onclick = () => NET.send({ t: 'leave' });
document.getElementById('btn-ready').onclick = () => {
  const me = ROOM && ROOM.players.find(p => p.pid === MYPID);
  NET.send({ t: 'ready', v: !(me && me.ready) });
};
document.getElementById('btn-start').onclick = () => NET.send({ t: 'start' });
document.getElementById('btn-bot').onclick = () =>
  NET.send({ t: 'addbot', level: +document.getElementById('l-botlvl').value });
document.getElementById('btn-spec').onclick = () => {
  const me = ROOM && ROOM.players.find(p => p.pid === MYPID);
  NET.send({ t: 'spectate', v: !(me && me.spec) });
};
document.querySelectorAll('.join-team').forEach(b => {
  b.onclick = () => NET.send({ t: 'team', team: +b.dataset.team });
});
['l-mode-sel', 'l-arena', 'l-stocks'].forEach(id => {
  document.getElementById(id).onchange = () => NET.send({
    t: 'config',
    mode: document.getElementById('l-mode-sel').value,
    arena: document.getElementById('l-arena').value,
    stocks: +document.getElementById('l-stocks').value
  });
});
document.getElementById('btn-copy').onclick = () => {
  const url = `${location.origin}/?join=${ROOM ? ROOM.code : ''}`;
  navigator.clipboard ? navigator.clipboard.writeText(url).then(() => UI.toast('Ссылка скопирована'))
    : prompt('Ссылка:', url);
};
document.getElementById('chat-in').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  const v = e.target.value.trim(); if (!v) return;
  NET.send({ t: 'chat', m: v }); e.target.value = '';
});
document.getElementById('btn-quit').onclick = () => {
  if (ROOM && ROOM.host === MYPID) NET.send({ t: 'abort' });
  else { INMATCH = false; UI.show('lobby'); }
};

/* ---------------- сообщения по центру / киллфид ---------------- */
let lastCt = -1, lastState = '';
function centerMsg(txt, ms) {
  const el = document.getElementById('center-msg');
  el.textContent = txt;
  el.classList.toggle('show', !!txt);
  if (txt && ms) setTimeout(() => el.classList.remove('show'), ms);
}
function handleAnnounce(s) {
  if (s.st === 'countdown') {
    const n = Math.ceil(s.ct);
    if (n !== lastCt) { lastCt = n; centerMsg(n > 0 ? String(n) : '', 0); }
  } else if (lastState === 'countdown' && s.st === 'play') {
    centerMsg('БОЙ!', 900);
  }
  lastState = s.st;
  (s.e || []).forEach(e => {
    if (e.k === 'ko') {
      const a = Render.players[e.by], b = Render.players[e.pid];
      const el = document.createElement('div');
      el.className = 'kf';
      el.innerHTML = `<b style="color:${a ? TEAM_COL[a.team] : '#888'}">${esc(a ? a.name : 'арена')}</b>
        ⚔ <b style="color:${b ? TEAM_COL[b.team] : '#888'}">${esc(b ? b.name : '')}</b>`;
      const kf = document.getElementById('killfeed');
      kf.appendChild(el);
      while (kf.children.length > 5) kf.firstChild.remove();
      setTimeout(() => el.remove(), 5000);
    } else if (e.k === 'end') {
      const w = s.w;
      centerMsg(w === -1 ? 'НИЧЬЯ' : (w === MYTEAM ? 'ПОБЕДА' : 'ПОРАЖЕНИЕ'), 6000);
    } else if (e.k === 'ult') {
      const p = Render.players[e.pid];
      if (!p) return;
      const el = document.createElement('div');
      el.className = 'kf';
      el.innerHTML = `<b style="color:${TEAM_COL[p.team]}">${esc(p.name)}</b> — ультимейт`;
      const kf = document.getElementById('killfeed');
      kf.appendChild(el);
      while (kf.children.length > 5) kf.firstChild.remove();
      setTimeout(() => el.remove(), 3500);
    }
  });
}

/* ---------------- цикл ---------------- */
let last = performance.now(), fps = 60, acc = 0;
function loop(now) {
  const dt = Math.min(0.05, (now - last) / 1000); last = now;
  fps += ((1 / Math.max(0.001, dt)) - fps) * 0.05;
  if (UI.cur === 'game') {
    const v = Render.view();
    if (v) {
      const me = v.p.find(p => p.i === MYPID);
      if (me) Input.updateAim(me.x, me.y);
    }
    acc += dt;
    if (acc >= 1 / 60) { acc = 0; Input.flush(NET); }
    Render.frame(dt);
    document.getElementById('net').textContent =
      `${NET.ping} ms · ${Math.round(fps)} fps`;
  }
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);
NET.connect();
