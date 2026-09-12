/* DOM: экраны, меню, лобби, модалки. */
const UI = {
  cur: 'menu',
  show(id) {
    document.querySelectorAll('.screen').forEach(s => s.classList.toggle('active', s.id === id));
    this.cur = id;
    Input.enabled = (id === 'game');
  },
  toast(msg, bad) {
    const d = document.createElement('div');
    d.className = 'toast' + (bad ? ' bad' : ''); d.textContent = msg;
    document.getElementById('toast').appendChild(d);
    setTimeout(() => d.remove(), 3200);
  },
  modal(html) {
    document.getElementById('modal-content').innerHTML = html;
    document.getElementById('modal').classList.add('show');
  },
  closeModal() { document.getElementById('modal').classList.remove('show'); },

  glyphCanvas(heroId, col, size) {
    const cv = document.createElement('canvas');
    cv.width = cv.height = size * 2; cv.style.width = cv.style.height = size + 'px';
    cv.className = 'glyph';
    const c = cv.getContext('2d'); c.scale(2, 2);
    if (GLYPH[heroId]) GLYPH[heroId](c, size / 2, size / 2, size * 0.9, col);
    return cv;
  },

  /* ---------------- список игр ---------------- */
  renderGames(data, onJoinLocal) {
    const box = document.getElementById('games');
    const st = document.getElementById('disco-state');
    if (data.discovery) {
      st.textContent = data.peers > 0 ? `в сети: ${data.peers + 1} ПК` : 'поиск…';
      st.className = 'tag ok';
    } else {
      st.textContent = 'броадкаст недоступен'; st.className = 'tag bad';
    }
    const all = data.local.concat(data.lan);
    if (!all.length) {
      box.innerHTML = '<div class="empty">Пока ничего не найдено.<br>Создай игру — её увидят остальные.</div>';
      return;
    }
    box.innerHTML = '';
    all.forEach(g => {
      const d = document.createElement('div');
      d.className = 'game' + (g.players >= g.cap ? ' full' : '');
      const modeTxt = g.mode === '2v2' ? '2 × 2' : '1 × 1';
      const stateTxt = g.state === 'match' ? 'идёт бой' : 'лобби';
      d.innerHTML = `<div class="gi"><div class="gn"></div>
        <div class="gm">${modeTxt} · ${stateTxt} · ${g.host} · код ${g.code}</div></div>
        <span class="cnt">${g.players}/${g.cap}</span>`;
      d.querySelector('.gn').textContent = g.name;
      const b = document.createElement('button');
      b.className = 'primary mini';
      b.textContent = g.players >= g.cap || g.state === 'match' ? 'смотреть' : 'войти';
      b.onclick = () => {
        if (g.remote) location.href = g.url;
        else onJoinLocal(g.code, g.players >= g.cap || g.state === 'match');
      };
      d.appendChild(b);
      box.appendChild(d);
    });
  },

  /* ---------------- лобби ---------------- */
  renderLobby(room, myPid, cat) {
    document.getElementById('l-name').textContent = room.name;
    document.getElementById('l-code').textContent = room.code;
    document.getElementById('l-mode').textContent =
      (room.mode === '2v2' ? '2 на 2' : '1 на 1') + ' · ' + room.stocks + ' жизни';
    const isHost = room.host === myPid;
    document.getElementById('host-bar').classList.toggle('hidden', !isHost);
    document.getElementById('l-mode-sel').value = room.mode;
    document.getElementById('l-arena').value = room.arena;
    document.getElementById('l-stocks').value = String(room.stocks);
    document.getElementById('btn-start').disabled = !isHost;

    const me = room.players.find(p => p.pid === myPid);
    const rb = document.getElementById('btn-ready');
    rb.classList.toggle('on', !!(me && me.ready));
    rb.textContent = me && me.ready ? 'Готов ✓' : 'Готов';
    rb.disabled = !!(me && me.spec);
    document.getElementById('btn-spec').textContent = me && me.spec ? 'Вернуться в бой' : 'Стать зрителем';

    [0, 1].forEach(t => {
      const box = document.getElementById('team' + t);
      box.innerHTML = '';
      room.players.filter(p => !p.spec && p.team === t).forEach(p => {
        const hero = cat.heroes.find(h => h.id === p.hero) || {};
        const d = document.createElement('div');
        d.className = 'slot' + (p.ready ? ' ready' : '');
        d.innerHTML = `<span class="dot"></span>`;
        d.appendChild(this.glyphCanvas(p.hero, hero.color || '#fff', 20));
        const nm = document.createElement('span');
        nm.className = 'nm';
        nm.textContent = p.name + (p.bot ? ' · бот' : '');
        d.appendChild(nm);
        const hs = document.createElement('span');
        hs.className = 'hs'; hs.textContent = hero.name || '';
        d.appendChild(hs);
        if (p.pid === myPid) { const m = document.createElement('span'); m.className = 'me'; m.textContent = 'ты'; d.appendChild(m); }
        if (isHost && p.pid !== myPid) {
          const k = document.createElement('button');
          k.className = 'mini kick'; k.textContent = '×'; k.title = 'убрать';
          k.onclick = () => NET.send({ t: 'kick', pid: p.pid });
          d.appendChild(k);
        }
        box.appendChild(d);
      });
      const spec = room.players.filter(p => p.spec);
      if (t === 1 && spec.length) {
        const s = document.createElement('div');
        s.className = 'hs'; s.style.marginTop = '6px';
        s.textContent = 'Зрители: ' + spec.map(p => p.name).join(', ');
        box.appendChild(s);
      }
    });

    const taken = {};
    room.players.forEach(p => { if (!p.spec && p.pid !== myPid) taken[p.hero] = p.name; });
    this.renderHeroGrid(document.getElementById('heroes'), cat, me ? me.hero : null, taken,
      id => NET.send({ t: 'hero', hero: id }));

    const log = document.getElementById('chat-log');
    log.innerHTML = '';
    (room.chat || []).forEach(m => this.chatLine(m.n, m.m, true));
    log.scrollTop = log.scrollHeight;
  },

  chatLine(n, m, silent) {
    const log = document.getElementById('chat-log');
    const d = document.createElement('div');
    if (n) { d.className = 'cm'; d.innerHTML = '<b></b>: <span></span>'; d.querySelector('b').textContent = n; d.querySelector('span').textContent = m; }
    else { d.className = 'sys'; d.textContent = m; }
    log.appendChild(d);
    if (!silent) log.scrollTop = log.scrollHeight;
  },

  renderHeroGrid(box, cat, sel, taken, onPick) {
    box.innerHTML = '';
    cat.heroes.forEach(h => {
      const d = document.createElement('div');
      const isTaken = !!taken[h.id];
      d.className = 'hero' + (sel === h.id ? ' sel' : '') + (isTaken ? ' taken' : '');
      const head = document.createElement('div'); head.className = 'hh';
      head.appendChild(this.glyphCanvas(h.id, h.color, 34));
      const nm = document.createElement('div');
      nm.innerHTML = `<div class="hn"></div><div class="hr"></div>`;
      nm.querySelector('.hn').textContent = h.name;
      nm.querySelector('.hn').style.color = h.color;
      nm.querySelector('.hr').textContent = isTaken ? 'занят: ' + taken[h.id] : h.role;
      head.appendChild(nm);
      d.appendChild(head);
      const st = document.createElement('div'); st.className = 'stats';
      st.textContent = `${h.hp} HP · вес ${h.weight} · скор ${h.speed} · слож ${'★'.repeat(h.difficulty)}`;
      d.appendChild(st);
      const tl = document.createElement('div'); tl.className = 'tl'; tl.textContent = h.tagline;
      d.appendChild(tl);
      const ab = document.createElement('div'); ab.className = 'abilities';
      h.abilities.forEach(a => {
        const r = document.createElement('div'); r.className = 'ab';
        r.innerHTML = `<span class="k"></span><span><span class="an"></span> <span class="ad"></span></span>`;
        r.querySelector('.k').textContent = KEYLABEL[a.key] === 'ЛКМ' ? '•' : KEYLABEL[a.key];
        r.querySelector('.an').textContent = a.name;
        r.querySelector('.ad').textContent = a.desc;
        ab.appendChild(r);
      });
      const pr = document.createElement('div'); pr.className = 'ab';
      pr.innerHTML = `<span class="k">П</span><span><span class="an"></span> <span class="ad"></span></span>`;
      pr.querySelector('.an').textContent = h.passive.name;
      pr.querySelector('.ad').textContent = h.passive.desc;
      ab.appendChild(pr);
      d.appendChild(ab);
      if (!isTaken) d.onclick = () => onPick(h.id);
      box.appendChild(d);
    });
  },

  heroModal(cat) {
    this.modal('<h2 style="margin-bottom:10px">Герои</h2><div id="hm" class="heroes"></div>');
    this.renderHeroGrid(document.getElementById('hm'), cat, null, {}, () => {});
  },

  results(res, cat, myTeam) {
    const win = res.winner;
    const title = win === -1 ? 'НИЧЬЯ' : (win === myTeam ? 'ПОБЕДА' : 'ПОРАЖЕНИЕ');
    const col = win === -1 ? '#8b93a8' : (win === myTeam ? '#6ee7a0' : '#ff6b6b');
    let rows = '';
    res.board.slice().sort((a, b) => b.kills - a.kills || b.dmg - a.dmg).forEach(p => {
      const h = cat.heroes.find(x => x.id === p.hero) || {};
      rows += `<tr class="t${p.team}"><td>${esc(p.name)}</td><td>${esc(h.name || '')}</td>
        <td>${p.kills}</td><td>${p.deaths}</td><td>${p.dmg}</td><td>${p.stocks}</td></tr>`;
    });
    this.modal(`<div class="win-title" style="color:${col}">${title}</div>
      <div style="color:var(--dim);margin-top:4px">${esc(res.arena)} · ${res.time} с</div>
      <table class="board"><tr><th>Игрок</th><th>Герой</th><th>Убийств</th><th>Смертей</th><th>Урон</th><th>Жизни</th></tr>${rows}</table>
      <p style="color:var(--dim);margin-top:14px">Закрой окно — вы вернулись в лобби, можно менять героев и играть снова.</p>`);
  }
};

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
