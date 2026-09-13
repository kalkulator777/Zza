/* Клавиатура + мышь -> битовая маска ввода. */
const BIT = { LEFT: 1, RIGHT: 2, JUMP: 4, DOWN: 8, BASIC: 16, Q: 32, E: 64, R: 128, DASH: 256 };

const Input = {
  held: 0, pressed: 0, sentHeld: -1, aim: { x: 1, y: 0 },
  mouse: { x: 0, y: 0 }, enabled: false, canvas: null,

  bind(canvas) {
    this.canvas = canvas;
    const map = e => {
      switch (e.code) {
        case 'KeyA': case 'ArrowLeft': return BIT.LEFT;
        case 'KeyD': case 'ArrowRight': return BIT.RIGHT;
        case 'KeyW': case 'ArrowUp': case 'Space': return BIT.JUMP;
        case 'KeyS': case 'ArrowDown': return BIT.DOWN;
        case 'KeyQ': return BIT.Q;
        case 'KeyE': return BIT.E;
        case 'KeyR': return BIT.R;
        case 'ShiftLeft': case 'ShiftRight': return BIT.DASH;
        case 'KeyF': return BIT.BASIC;
        default: return 0;
      }
    };
    addEventListener('keydown', e => {
      if (!this.enabled) return;
      const b = map(e);
      if (!b) return;
      e.preventDefault();
      if (!(this.held & b)) this.pressed |= b;
      this.held |= b;
    });
    addEventListener('keyup', e => {
      const b = map(e); if (!b) return;
      e.preventDefault(); this.held &= ~b;
    });
    addEventListener('blur', () => { this.held = 0; });
    canvas.addEventListener('contextmenu', e => e.preventDefault());
    canvas.addEventListener('mousedown', e => {
      if (!this.enabled) return;
      e.preventDefault();
      const b = e.button === 0 ? BIT.BASIC : (e.button === 2 ? BIT.Q : 0);
      if (!b) return;
      if (!(this.held & b)) this.pressed |= b;
      this.held |= b;
    });
    addEventListener('mouseup', e => {
      const b = e.button === 0 ? BIT.BASIC : (e.button === 2 ? BIT.Q : 0);
      if (b) this.held &= ~b;
    });
    canvas.addEventListener('mousemove', e => {
      const r = canvas.getBoundingClientRect();
      this.mouse.x = (e.clientX - r.left) / r.width * Render.CW;
      this.mouse.y = (e.clientY - r.top) / r.height * Render.CH;
    });
  },

  /* Прицел: вектор от бойца к курсору, длина 0..1 (1 = дальше 400px). */
  updateAim(px, py) {
    const w = Render.screenToWorld(this.mouse.x, this.mouse.y);
    const dx = w.x - px, dy = w.y - py;
    const d = Math.hypot(dx, dy) || 1;
    const k = Math.min(1, d / 400);
    this.aim.x = dx / d * k;
    this.aim.y = dy / d * k;
  },

  flush(net) {
    if (!this.enabled) return;
    const p = this.pressed; this.pressed = 0;
    net.send({ t: 'i', k: this.held, p, ax: +this.aim.x.toFixed(3), ay: +this.aim.y.toFixed(3) });
  }
};
