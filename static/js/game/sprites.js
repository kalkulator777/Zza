// Всё, что можно нарисовать один раз, рисуется один раз в offscreen-канвас,
// а в кадре остаётся drawImage. Текст особенно: fillText в игровом цикле
// стоит заметных миллисекунд, а подпись под машиной меняется раз в заезд.

function mk(w, h) {
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.ceil(w));
  c.height = Math.max(1, Math.ceil(h));
  return c;
}

const SS = 2;   // спрайты в двойном разрешении: на мониторах побольше не мылит

export function makeCarSprite(color, len = 34, wid = 18) {
  const pad = 6;
  const c = mk((len + pad * 2) * SS, (wid + pad * 2) * SS);
  const g = c.getContext('2d');
  g.scale(SS, SS);
  g.translate(pad, pad);

  const r = 5;
  g.beginPath();
  g.roundRect(0, 0, len, wid, r);
  g.fillStyle = color;
  g.fill();

  // Тёмный низ даёт объём без единой тени в кадре
  g.beginPath();
  g.roundRect(0, wid * 0.58, len, wid * 0.42, [0, 0, r, r]);
  g.fillStyle = 'rgba(0,0,0,.22)';
  g.fill();

  // Стекло — сразу видно, где перёд
  g.beginPath();
  g.roundRect(len * 0.46, wid * 0.16, len * 0.26, wid * 0.68, 2);
  g.fillStyle = 'rgba(12,18,28,.78)';
  g.fill();

  // Колёса
  g.fillStyle = '#15181f';
  for (const [x, y] of [[len * 0.13, -1.5], [len * 0.13, wid - 2.5],
                        [len * 0.72, -1.5], [len * 0.72, wid - 2.5]]) {
    g.beginPath();
    g.roundRect(x, y, len * 0.19, 4, 1.5);
    g.fill();
  }

  g.strokeStyle = 'rgba(255,255,255,.22)';
  g.lineWidth = 1;
  g.beginPath();
  g.roundRect(.5, .5, len - 1, wid - 1, r);
  g.stroke();

  return { canvas: c, ox: pad, oy: pad + wid / 2, w: len + pad * 2, h: wid + pad * 2,
           cx: pad + len / 2, cy: pad + wid / 2 };
}

const DECOR_SIZE = { tree: 34, bush: 22, rock: 26, tire: 16, cone: 12,
                     sign: 20, building: 60 };

export function makeDecorSprite(type, theme = 'day') {
  const s = DECOR_SIZE[type] || 20;
  const c = mk(s * SS, s * SS);
  const g = c.getContext('2d');
  g.scale(SS, SS);
  const h = s / 2;

  const leaf = theme === 'snow' ? '#cfe3ea' : theme === 'desert' ? '#8a9b5a' : '#2e6b38';
  const leaf2 = theme === 'snow' ? '#eaf4f8' : theme === 'desert' ? '#a3b56d' : '#3d8a48';

  switch (type) {
    case 'tree':
      g.fillStyle = 'rgba(0,0,0,.28)';
      g.beginPath(); g.ellipse(h + 2, h + 3, h * 0.8, h * 0.7, 0, 0, 7); g.fill();
      g.fillStyle = leaf;
      g.beginPath(); g.arc(h, h, h * 0.86, 0, 7); g.fill();
      g.fillStyle = leaf2;
      g.beginPath(); g.arc(h - h * 0.22, h - h * 0.24, h * 0.52, 0, 7); g.fill();
      break;
    case 'bush':
      g.fillStyle = leaf;
      for (const [dx, dy, rr] of [[0, 0, .55], [-.3, .1, .4], [.3, .12, .42]]) {
        g.beginPath(); g.arc(h + dx * s, h + dy * s, h * rr, 0, 7); g.fill();
      }
      break;
    case 'rock':
      g.fillStyle = theme === 'desert' ? '#9a8a70' : '#6c7180';
      g.beginPath();
      g.moveTo(h * 0.3, s * 0.8); g.lineTo(h * 0.55, h * 0.4);
      g.lineTo(h * 1.3, h * 0.25); g.lineTo(s * 0.88, s * 0.72);
      g.closePath(); g.fill();
      g.fillStyle = 'rgba(255,255,255,.14)';
      g.beginPath();
      g.moveTo(h * 0.55, h * 0.4); g.lineTo(h * 1.3, h * 0.25);
      g.lineTo(h * 1.05, h * 0.75); g.closePath(); g.fill();
      break;
    case 'tire':
      g.fillStyle = '#1b1e25';
      g.beginPath(); g.arc(h, h, h * 0.9, 0, 7); g.fill();
      g.fillStyle = '#31363f';
      g.beginPath(); g.arc(h, h, h * 0.45, 0, 7); g.fill();
      break;
    case 'cone':
      g.fillStyle = '#ff7a29';
      g.beginPath(); g.moveTo(h, h * 0.2); g.lineTo(s * 0.86, s * 0.88);
      g.lineTo(s * 0.14, s * 0.88); g.closePath(); g.fill();
      g.fillStyle = '#fff';
      g.fillRect(s * 0.26, s * 0.52, s * 0.48, s * 0.14);
      break;
    case 'sign':
      g.fillStyle = '#b9c2d4';
      g.fillRect(h - 1.5, h, 3, h);
      g.fillStyle = '#ffd23f';
      g.beginPath(); g.moveTo(h, 1); g.lineTo(s - 1, h); g.lineTo(h, s * 0.72);
      g.lineTo(1, h); g.closePath(); g.fill();
      break;
    case 'building':
    default:
      g.fillStyle = theme === 'night' ? '#2a3040' : '#5a6070';
      g.fillRect(s * 0.1, s * 0.1, s * 0.8, s * 0.8);
      g.fillStyle = theme === 'night' ? '#ffd98a' : 'rgba(255,255,255,.18)';
      for (let y = 0; y < 3; y++) {
        for (let x = 0; x < 3; x++) {
          if ((x + y) % 2) continue;
          g.fillRect(s * (0.2 + x * 0.23), s * (0.2 + y * 0.23), s * 0.13, s * 0.13);
        }
      }
      break;
  }
  return { canvas: c, size: s };
}

export function makeLabel(text, color) {
  const font = '600 13px system-ui, sans-serif';
  const probe = mk(10, 10).getContext('2d');
  probe.font = font;
  const w = Math.ceil(probe.measureText(text).width) + 12;
  const h = 19;
  const c = mk(w * SS, h * SS);
  const g = c.getContext('2d');
  g.scale(SS, SS);
  g.font = font;
  g.textBaseline = 'middle';
  g.fillStyle = 'rgba(8,10,15,.62)';
  g.beginPath(); g.roundRect(0, 0, w, h, 5); g.fill();
  g.fillStyle = color;
  g.fillText(text, 6, h / 2 + .5);
  return { canvas: c, w, h };
}

export function makeGrassTile(theme) {
  const s = 128;
  const c = mk(s, s);
  const g = c.getContext('2d');
  const base = { day: '#1f3b23', night: '#131d2a', desert: '#6b5a36', snow: '#dfe8ee' }[theme] || '#1f3b23';
  const spec = { day: '#26472a', night: '#182534', desert: '#7a663d', snow: '#eef4f8' }[theme] || '#26472a';
  g.fillStyle = base;
  g.fillRect(0, 0, s, s);
  g.fillStyle = spec;
  // Детерминированный «шум»: одинаковый узор у всех, без случайности
  let seed = 12345;
  for (let i = 0; i < 260; i++) {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    const x = (seed >> 7) % s, y = (seed >> 15) % s;
    g.fillRect(x, y, 2 + (seed & 1), 1 + ((seed >> 3) & 1));
  }
  return c;
}
