/* Минималистичные глифы героев — рисуются и в лобби, и в бою. */
const GLYPH = {
  rezak(c, x, y, s, col) {
    c.save(); c.translate(x, y); c.strokeStyle = col; c.lineWidth = s * 0.11;
    c.lineJoin = 'round'; c.fillStyle = col;
    c.beginPath();
    c.moveTo(0, -s * 0.5); c.lineTo(s * 0.28, 0); c.lineTo(0, s * 0.5); c.lineTo(-s * 0.28, 0);
    c.closePath(); c.fill();
    c.globalAlpha = 0.55; c.beginPath();
    c.moveTo(-s * 0.46, -s * 0.3); c.lineTo(-s * 0.2, s * 0.36);
    c.moveTo(s * 0.46, -s * 0.3); c.lineTo(s * 0.2, s * 0.36);
    c.stroke(); c.restore();
  },
  bunker(c, x, y, s, col) {
    c.save(); c.translate(x, y); c.fillStyle = col; c.strokeStyle = col;
    c.lineWidth = s * 0.1; c.lineJoin = 'round';
    c.beginPath();
    for (let i = 0; i < 6; i++) {
      const a = Math.PI / 6 + i * Math.PI / 3;
      const px = Math.cos(a) * s * 0.46, py = Math.sin(a) * s * 0.46;
      i ? c.lineTo(px, py) : c.moveTo(px, py);
    }
    c.closePath(); c.globalAlpha = 0.35; c.fill(); c.globalAlpha = 1; c.stroke();
    c.beginPath(); c.moveTo(0, -s * 0.24); c.lineTo(0, s * 0.24); c.stroke();
    c.restore();
  },
  igla(c, x, y, s, col) {
    c.save(); c.translate(x, y); c.fillStyle = col; c.strokeStyle = col; c.lineWidth = s * 0.09;
    c.beginPath(); c.moveTo(s * 0.5, 0); c.lineTo(-s * 0.3, -s * 0.3);
    c.lineTo(-s * 0.12, 0); c.lineTo(-s * 0.3, s * 0.3); c.closePath(); c.fill();
    c.globalAlpha = 0.5; c.beginPath(); c.arc(0, 0, s * 0.46, -0.7, 0.7); c.stroke();
    c.restore();
  },
  vyuga(c, x, y, s, col) {
    c.save(); c.translate(x, y); c.strokeStyle = col; c.lineWidth = s * 0.1; c.lineCap = 'round';
    for (let i = 0; i < 3; i++) {
      c.rotate(Math.PI / 3);
      c.beginPath(); c.moveTo(-s * 0.46, 0); c.lineTo(s * 0.46, 0); c.stroke();
      c.beginPath();
      c.moveTo(s * 0.26, 0); c.lineTo(s * 0.36, -s * 0.12);
      c.moveTo(s * 0.26, 0); c.lineTo(s * 0.36, s * 0.12);
      c.moveTo(-s * 0.26, 0); c.lineTo(-s * 0.36, -s * 0.12);
      c.moveTo(-s * 0.26, 0); c.lineTo(-s * 0.36, s * 0.12);
      c.stroke();
    }
    c.restore();
  },
  gayka(c, x, y, s, col) {
    c.save(); c.translate(x, y); c.fillStyle = col; c.strokeStyle = col; c.lineWidth = s * 0.09;
    for (let i = 0; i < 8; i++) {
      const a = i * Math.PI / 4;
      c.beginPath();
      c.moveTo(Math.cos(a) * s * 0.3, Math.sin(a) * s * 0.3);
      c.lineTo(Math.cos(a) * s * 0.5, Math.sin(a) * s * 0.5);
      c.stroke();
    }
    c.beginPath(); c.arc(0, 0, s * 0.3, 0, 6.284); c.globalAlpha = 0.35; c.fill();
    c.globalAlpha = 1; c.stroke();
    c.beginPath(); c.arc(0, 0, s * 0.12, 0, 6.284); c.stroke();
    c.restore();
  },
  gorn(c, x, y, s, col) {                      // язык пламени
    c.save(); c.translate(x, y); c.fillStyle = col; c.strokeStyle = col;
    c.lineWidth = s * 0.08; c.lineJoin = 'round';
    c.beginPath();
    c.moveTo(0, -s * 0.5);
    c.quadraticCurveTo(s * 0.34, -s * 0.06, s * 0.22, s * 0.2);
    c.quadraticCurveTo(s * 0.14, s * 0.46, -s * 0.06, s * 0.48);
    c.quadraticCurveTo(-s * 0.34, s * 0.42, -s * 0.28, s * 0.08);
    c.quadraticCurveTo(-s * 0.24, -s * 0.16, -s * 0.04, -s * 0.24);
    c.quadraticCurveTo(-s * 0.14, -s * 0.02, 0, -s * 0.5);
    c.closePath(); c.fill();
    c.globalAlpha = 0.5;
    c.beginPath();
    c.moveTo(0, s * 0.44);
    c.quadraticCurveTo(-s * 0.16, s * 0.12, 0, -s * 0.1);
    c.quadraticCurveTo(s * 0.16, s * 0.14, 0, s * 0.44);
    c.closePath();
    c.fillStyle = '#fff'; c.fill();
    c.restore();
  },
  zerkalo(c, x, y, s, col) {                   // клинок и его отражение
    c.save(); c.translate(x, y); c.fillStyle = col; c.strokeStyle = col;
    c.lineWidth = s * 0.09; c.lineJoin = 'round';
    c.beginPath();
    c.moveTo(0, -s * 0.5); c.lineTo(s * 0.3, s * 0.02); c.lineTo(-s * 0.3, s * 0.02);
    c.closePath(); c.fill();
    c.globalAlpha = 0.45;
    c.beginPath();
    c.moveTo(0, s * 0.5); c.lineTo(s * 0.3, s * 0.06); c.lineTo(-s * 0.3, s * 0.06);
    c.closePath(); c.stroke();
    c.globalAlpha = 1;
    c.lineWidth = s * 0.07;
    c.beginPath(); c.moveTo(-s * 0.44, s * 0.04); c.lineTo(s * 0.44, s * 0.04); c.stroke();
    c.restore();
  },
  yakor(c, x, y, s, col) {                     // якорь
    c.save(); c.translate(x, y); c.strokeStyle = col; c.fillStyle = col;
    c.lineWidth = s * 0.11; c.lineCap = 'round'; c.lineJoin = 'round';
    c.beginPath(); c.arc(0, -s * 0.36, s * 0.13, 0, 6.284); c.stroke();
    c.beginPath();
    c.moveTo(0, -s * 0.23); c.lineTo(0, s * 0.34);
    c.moveTo(-s * 0.3, -s * 0.1); c.lineTo(s * 0.3, -s * 0.1);
    c.stroke();
    c.beginPath(); c.arc(0, s * 0.12, s * 0.36, 0.35, Math.PI - 0.35); c.stroke();
    c.restore();
  },
  puls(c, x, y, s, col) {
    c.save(); c.translate(x, y); c.strokeStyle = col; c.fillStyle = col; c.lineWidth = s * 0.14;
    c.lineCap = 'round';
    c.beginPath(); c.moveTo(0, -s * 0.4); c.lineTo(0, s * 0.4);
    c.moveTo(-s * 0.4, 0); c.lineTo(s * 0.4, 0); c.stroke();
    c.globalAlpha = 0.4; c.beginPath(); c.arc(0, 0, s * 0.48, 0, 6.284); c.lineWidth = s * 0.07;
    c.stroke(); c.restore();
  }
};

const TEAM_COL = ['#4ea8ff', '#ff6b6b'];
const KEYLABEL = { basic: 'ЛКМ', q: 'Q', e: 'E', r: 'R' };

function hexA(hex, a) {
  const h = hex.replace('#', '');
  const n = parseInt(h.length === 3 ? h.split('').map(x => x + x).join('') : h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/* Иконки слотов способностей — одинаковые для всех героев, красятся в цвет героя. */
const ABICON = {
  basic(c, x, y, s, col) {           // удар
    c.save(); c.translate(x, y); c.strokeStyle = col; c.lineWidth = s * 0.12; c.lineCap = 'round';
    c.beginPath(); c.moveTo(-s * 0.34, s * 0.3); c.lineTo(s * 0.3, -s * 0.32); c.stroke();
    c.fillStyle = col; c.beginPath();
    c.moveTo(s * 0.42, -s * 0.44); c.lineTo(s * 0.16, -s * 0.36); c.lineTo(s * 0.34, -s * 0.12);
    c.closePath(); c.fill(); c.restore();
  },
  q(c, x, y, s, col) {               // кольцо
    c.save(); c.translate(x, y); c.strokeStyle = col; c.lineWidth = s * 0.13;
    c.beginPath(); c.arc(0, 0, s * 0.34, 0.6, 5.5); c.stroke();
    c.fillStyle = col; c.beginPath(); c.arc(s * 0.3, -s * 0.2, s * 0.1, 0, 6.284); c.fill();
    c.restore();
  },
  e(c, x, y, s, col) {               // треугольник-волна
    c.save(); c.translate(x, y); c.strokeStyle = col; c.lineWidth = s * 0.12; c.lineJoin = 'round';
    c.beginPath(); c.moveTo(-s * 0.36, s * 0.28); c.lineTo(0, -s * 0.36); c.lineTo(s * 0.36, s * 0.28);
    c.closePath(); c.stroke();
    c.globalAlpha = .4; c.fillStyle = col; c.fill(); c.restore();
  },
  r(c, x, y, s, col) {               // звезда-ульта
    c.save(); c.translate(x, y); c.fillStyle = col;
    c.beginPath();
    for (let i = 0; i < 10; i++) {
      const r = i % 2 ? s * 0.16 : s * 0.42, a = -Math.PI / 2 + i * Math.PI / 5;
      const px = Math.cos(a) * r, py = Math.sin(a) * r;
      i ? c.lineTo(px, py) : c.moveTo(px, py);
    }
    c.closePath(); c.fill(); c.restore();
  }
};

/* Новый герой, которому ещё не нарисовали свой значок: собираем узнаваемую
   фигуру из его id, чтобы хотя бы не путать героев между собой. */
const _fallbackCache = {};
function glyphFallback(id) {
  if (_fallbackCache[id]) return _fallbackCache[id];
  let h = 0;
  for (let i = 0; i < (id || '').length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  const sides = 3 + (Math.abs(h) % 5);          // 3..7 углов
  const rot = (Math.abs(h >> 3) % 12) / 12 * Math.PI;
  const inner = (Math.abs(h >> 7) % 2) === 1;
  const fn = (c, x, y, s, col) => {
    c.save(); c.translate(x, y); c.rotate(rot);
    c.strokeStyle = col; c.fillStyle = col; c.lineWidth = s * 0.1; c.lineJoin = 'round';
    c.beginPath();
    for (let i = 0; i < sides; i++) {
      const a = -Math.PI / 2 + i * 2 * Math.PI / sides;
      const px = Math.cos(a) * s * 0.46, py = Math.sin(a) * s * 0.46;
      i ? c.lineTo(px, py) : c.moveTo(px, py);
    }
    c.closePath();
    c.globalAlpha = 0.35; c.fill(); c.globalAlpha = 1; c.stroke();
    if (inner) { c.beginPath(); c.arc(0, 0, s * 0.16, 0, 6.284); c.fill(); }
    c.restore();
  };
  _fallbackCache[id] = fn;
  return fn;
}
