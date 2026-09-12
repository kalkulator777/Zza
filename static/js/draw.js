// Холст. Рисунок — это список операций, а не картинка: так он летит по сети
// сотнями байт, отматывается назад и перерисовывается в любом масштабе.
export const W = 1000;
export const H = 750;
const FLUSH_MS = 80;      // как часто куски штриха уходят на сервер
const TOLERANCE = 40;     // допуск заливки по цвету

export class Board {
  constructor(canvas, { onOps } = {}) {
    this.canvas = canvas;
    canvas.width = W;
    canvas.height = H;
    this.ctx = canvas.getContext("2d", { willReadFrequently: true });
    this.onOps = onOps || (() => {});
    this.ops = [];
    this.pending = [];
    this.tool = "pen";
    this.color = "#111111";
    this.size = 6;
    this.enabled = false;
    this.stroke = null;
    this.chunk = [];
    this.strokeId = 0;
    this.reset();

    canvas.addEventListener("pointerdown", (e) => this.onDown(e));
    canvas.addEventListener("pointermove", (e) => this.onMove(e));
    window.addEventListener("pointerup", () => this.onUp());
    canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    setInterval(() => this.flush(), FLUSH_MS);
  }

  // --- состояние ------------------------------------------------------

  reset() {
    this.ops = [];
    this.stroke = null;
    this.chunk = [];
    this.clearSurface();
  }

  clearSurface() {
    this.ctx.fillStyle = "#ffffff";
    this.ctx.fillRect(0, 0, W, H);
  }

  setOps(ops) {
    this.ops = Array.isArray(ops) ? ops.slice() : [];
    this.redraw();
  }

  applyOps(ops) {
    for (const op of ops) {
      this.ops.push(op);
      this.paint(op);
    }
  }

  redraw() {
    this.clearSurface();
    for (const op of this.ops) this.paint(op);
  }

  undo() {
    if (!this.ops.length) return;
    const last = this.ops[this.ops.length - 1];
    if (last.s === undefined) {
      this.ops.pop();
    } else {
      while (this.ops.length && this.ops[this.ops.length - 1].s === last.s) this.ops.pop();
    }
    this.redraw();
  }

  clear() {
    this.ops = [];
    this.clearSurface();
  }

  // --- ввод -----------------------------------------------------------

  pointAt(event) {
    const rect = this.canvas.getBoundingClientRect();
    const x = (event.clientX - rect.left) * (W / rect.width);
    const y = (event.clientY - rect.top) * (H / rect.height);
    return [
      Math.round(Math.max(0, Math.min(W, x)) * 10) / 10,
      Math.round(Math.max(0, Math.min(H, y)) * 10) / 10,
    ];
  }

  onDown(event) {
    if (!this.enabled || event.button !== 0) return;
    this.canvas.setPointerCapture?.(event.pointerId);
    const [x, y] = this.pointAt(event);

    if (this.tool === "fill") {
      this.push({ t: "f", c: this.color, x, y });
      return;
    }
    this.strokeId += 1;
    this.stroke = {
      t: "p",
      c: this.tool === "eraser" ? "#ffffff" : this.color,
      w: this.tool === "eraser" ? this.size * 2.2 : this.size,
      s: this.strokeId,
    };
    this.chunk = [x, y];
    this.dot(x, y, this.stroke.c, this.stroke.w);
  }

  onMove(event) {
    if (!this.stroke) return;
    const [x, y] = this.pointAt(event);
    const n = this.chunk.length;
    if (n >= 2 && Math.abs(this.chunk[n - 2] - x) < 0.6 && Math.abs(this.chunk[n - 1] - y) < 0.6) return;
    this.segment(this.chunk[n - 2], this.chunk[n - 1], x, y, this.stroke.c, this.stroke.w);
    this.chunk.push(x, y);
  }

  onUp() {
    if (!this.stroke) return;
    this.flush();
    this.stroke = null;
    this.chunk = [];
  }

  /** Кусок текущего штриха уходит на сервер, не дожидаясь конца линии. */
  flush() {
    if (!this.stroke || this.chunk.length < 2) {
      this.drain();
      return;
    }
    if (this.chunk.length >= 4 || this.ops[this.ops.length - 1]?.s !== this.stroke.s) {
      const op = { ...this.stroke, pts: this.chunk.slice() };
      this.ops.push(op);
      this.pending.push(op);
      this.chunk = this.chunk.slice(-2);
    }
    this.drain();
  }

  push(op) {
    this.ops.push(op);
    this.pending.push(op);
    this.paint(op);
  }

  drain() {
    if (!this.pending.length) return;
    const ops = this.pending;
    this.pending = [];
    this.onOps(ops);
  }

  // --- рисование ------------------------------------------------------

  paint(op) {
    if (op.t === "p") this.paintStroke(op);
    else if (op.t === "f") this.fill(op.x, op.y, op.c);
  }

  paintStroke(op) {
    const pts = op.pts || [];
    if (pts.length < 2) return;
    if (pts.length === 2) {
      this.dot(pts[0], pts[1], op.c, op.w);
      return;
    }
    const ctx = this.ctx;
    ctx.strokeStyle = op.c;
    ctx.lineWidth = op.w;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(pts[0], pts[1]);
    for (let i = 2; i < pts.length; i += 2) ctx.lineTo(pts[i], pts[i + 1]);
    ctx.stroke();
  }

  segment(x1, y1, x2, y2, color, width) {
    const ctx = this.ctx;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }

  dot(x, y, color, width) {
    const ctx = this.ctx;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x, y, width / 2, 0, Math.PI * 2);
    ctx.fill();
  }

  /** Заливка по образцу: у всех одинаковый браузер и размер холста,
      поэтому воспроизводится одинаково. */
  fill(x, y, color) {
    const ctx = this.ctx;
    const image = ctx.getImageData(0, 0, W, H);
    const data = image.data;
    const start = (Math.round(y) * W + Math.round(x)) * 4;
    if (start < 0 || start >= data.length) return;

    const target = [data[start], data[start + 1], data[start + 2]];
    const paint = hexToRgb(color);
    if (!paint) return;
    if (Math.abs(target[0] - paint[0]) + Math.abs(target[1] - paint[1]) +
        Math.abs(target[2] - paint[2]) < 8) return;

    const stack = [Math.round(y) * W + Math.round(x)];
    const seen = new Uint8Array(W * H);
    while (stack.length) {
      const index = stack.pop();
      if (seen[index]) continue;
      seen[index] = 1;
      const offset = index * 4;
      if (Math.abs(data[offset] - target[0]) + Math.abs(data[offset + 1] - target[1]) +
          Math.abs(data[offset + 2] - target[2]) > TOLERANCE) continue;
      data[offset] = paint[0];
      data[offset + 1] = paint[1];
      data[offset + 2] = paint[2];
      data[offset + 3] = 255;
      const px = index % W;
      if (px > 0) stack.push(index - 1);
      if (px < W - 1) stack.push(index + 1);
      if (index >= W) stack.push(index - W);
      if (index < W * H - W) stack.push(index + W);
    }
    ctx.putImageData(image, 0, 0);
  }
}

function hexToRgb(hex) {
  const match = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
  if (!match) return null;
  return [parseInt(match[1], 16), parseInt(match[2], 16), parseInt(match[3], 16)];
}
