// Сборка DOM для меню, лобби и результатов. Здесь нет игровой логики —
// только превращение состояния в разметку.

export const $ = (id) => document.getElementById(id);

export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

export function fmtTime(ms) {
  if (ms == null) return '—';
  const s = ms / 1000;
  const m = Math.floor(s / 60);
  const r = s - m * 60;
  return m > 0 ? `${m}:${r.toFixed(2).padStart(5, '0')}` : r.toFixed(2);
}

export function fmtClock(sec) {
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, '0')}`;
}

export function renderLobbyList(container, list, onJoin) {
  container.textContent = '';
  if (!list.length) {
    container.appendChild(el('div', 'muted small',
      'Пока никто не создал лобби. Нажмите «Создать лобби» — остальные увидят его здесь.'));
    return;
  }
  for (const lb of list) {
    const row = el('div', 'item-row' + (lb.state !== 'waiting' ? ' racing' : ''));
    const info = el('div', 'grow');
    info.appendChild(el('div', 'name', lb.name));
    const bits = [`${lb.players}/${lb.max}`, lb.track, `${lb.laps} кр.`];
    if (!lb.powerups) bits.push('без бонусов');
    if (!lb.collisions) bits.push('без столкновений');
    info.appendChild(el('div', 'sub', bits.join(' · ')));
    row.appendChild(info);
    row.appendChild(el('span', 'badge', lb.id));
    if (lb.locked) row.appendChild(el('span', 'badge', 'пароль'));

    if (lb.state !== 'waiting') {
      row.appendChild(el('span', 'badge race', 'идёт заезд'));
    } else if (lb.players >= lb.max) {
      row.appendChild(el('span', 'badge', 'мест нет'));
    } else {
      const b = el('button', 'btn', 'Войти');
      b.onclick = () => onJoin(lb);
      row.appendChild(b);
    }
    container.appendChild(row);
  }
}

export function renderPlayers(container, lobby, myToken, onKick) {
  container.textContent = '';
  for (const p of lobby.players) {
    const row = el('div', 'item-row');
    const dot = el('div', 'dot');
    dot.style.background = p.color;
    row.appendChild(dot);
    const info = el('div', 'grow');
    info.appendChild(el('div', 'name', p.name + (p.token === myToken ? ' (вы)' : '')));
    row.appendChild(info);
    if (p.online === false) row.appendChild(el('span', 'badge race', 'нет связи'));
    if (p.host) row.appendChild(el('span', 'badge', 'хозяин'));
    else row.appendChild(el('span', 'badge' + (p.ready ? ' on' : ''),
                            p.ready ? 'готов' : 'ждёт'));
    if (lobby.host === myToken && p.token !== myToken) {
      const k = el('button', 'link', '✕');
      k.title = 'Исключить';
      k.onclick = () => onKick(p.token);
      row.appendChild(k);
    }
    container.appendChild(row);
  }
}

export function renderResults(container, rows, myToken, cars) {
  container.textContent = '';
  const tokenBySlot = new Map((cars || []).map((c) => [c.slot, c.token]));
  rows.forEach((r, i) => {
    const row = el('div', 'item-row');
    const dot = el('div', 'dot');
    dot.style.background = r.color;
    row.appendChild(dot);
    row.appendChild(el('b', null, `${i + 1}.`));
    const info = el('div', 'grow');
    info.appendChild(el('div', 'name', r.name +
      (tokenBySlot.get(r.slot) === myToken ? ' (вы)' : '')));
    const bits = [];
    bits.push(r.finished ? `итог ${fmtTime(r.ms)}` : `не доехал (${r.laps} кр.)`);
    if (r.best) bits.push(`лучший круг ${fmtTime(r.best)}`);
    if (r.respawns) bits.push(`респавнов ${r.respawns}`);
    info.appendChild(el('div', 'sub', bits.join(' · ')));
    row.appendChild(info);
    container.appendChild(row);
  });
}

export function renderRecords(container, board) {
  container.textContent = '';
  if (!board || !board.length) {
    container.appendChild(el('div', 'muted small', 'Рекордов пока нет — поставьте первый.'));
    return;
  }
  board.forEach((r, i) => {
    const row = el('div', 'item-row');
    row.appendChild(el('b', null, `${i + 1}.`));
    row.appendChild(el('div', 'grow', r.name));
    row.appendChild(el('span', 'badge', fmtTime(r.ms)));
    container.appendChild(row);
  });
}

export function renderStandings(container, world, cars) {
  const order = world.rank || [];
  container.textContent = '';
  order.forEach((slot, i) => {
    const car = world.cars.get(slot);
    if (!car) return;
    const row = el('div', 'stand-row' + (slot === world.you ? ' you' : ''));
    row.appendChild(el('b', null, String(i + 1)));
    const dot = el('div', 'dot');
    dot.style.background = car.color;
    row.appendChild(dot);
    row.appendChild(el('div', 'grow', car.name));
    if (car.online === false) row.appendChild(el('span', 'off', 'нет связи'));
    else row.appendChild(el('span', 'muted', `${car.lap + 1}`));
    container.appendChild(row);
  });
}
