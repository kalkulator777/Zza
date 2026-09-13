// Звук синтезируется на месте: файлов нет, качать нечего, на машинах без
// интернета это важно. Если звук в системе не работает — просто молчит,
// на игру это не влияет.

let ctx = null;
let enabled = true;

function ac() {
  if (ctx) return ctx;
  try {
    ctx = new (window.AudioContext || window.webkitAudioContext)();
  } catch { enabled = false; }
  return ctx;
}

export function resumeAudio() {
  const c = ac();
  if (c && c.state === 'suspended') c.resume().catch(() => {});
}

export function setSound(on) { enabled = on; }
export function soundOn() { return enabled; }

function blip(freq, dur, type = 'square', gain = 0.06, slide = 0) {
  if (!enabled) return;
  const c = ac();
  if (!c || c.state !== 'running') return;
  try {
    const o = c.createOscillator();
    const g = c.createGain();
    o.type = type;
    o.frequency.setValueAtTime(freq, c.currentTime);
    if (slide) o.frequency.exponentialRampToValueAtTime(
      Math.max(40, freq + slide), c.currentTime + dur);
    g.gain.setValueAtTime(gain, c.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0001, c.currentTime + dur);
    o.connect(g); g.connect(c.destination);
    o.start(); o.stop(c.currentTime + dur + 0.02);
  } catch { /* звук не критичен */ }
}

export const sfx = {
  count: () => blip(440, 0.12, 'square', 0.05),
  go: () => blip(880, 0.3, 'square', 0.07, 220),
  lap: () => blip(660, 0.16, 'triangle', 0.06, 180),
  pickup: () => blip(520, 0.1, 'sine', 0.05, 260),
  use: () => blip(300, 0.14, 'sawtooth', 0.05, 200),
  hit: () => blip(150, 0.24, 'sawtooth', 0.07, -80),
  bump: () => blip(90, 0.1, 'square', 0.05, -30),
  shield: () => blip(720, 0.18, 'sine', 0.05, -200),
  finish: () => { blip(660, 0.14, 'square', 0.06); setTimeout(() => blip(880, 0.3, 'square', 0.07), 140); },
  respawn: () => blip(220, 0.2, 'triangle', 0.05, 160),
};
