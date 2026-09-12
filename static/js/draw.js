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
    this.onOps = onOps || null;
    this.ops = [];
    this.pending = [];
    this.tool = "pen";
    this.color = "#111111";
    this.size = 6;
    this.enabled = false;
    this.stroke = null;
    this.chunk = [];
    this.strokeId = 0;
    this.under = [];      // бледная калька под рисунком
    this.locked = 0;      // столько первых операций чужие — их не стереть
    this.band = null;     // своя полоса холста (Изысканный труп)
    this.reset();

    canvas.addEventListener("pointerdown", (e) => this.onDown(e));
    canvas.addEventListener("pointermove", (e) => this.onMove(e));
    window.addEventListener("pointerup", () => this.onUp());
    canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    // холст только для показа чужого рисунка ничего не отправляет
    if (this.onOps) setInterval(() => this.flush(), FLUSH_MS);
  }

  // --- состояние ------------------------------------------------------

  reset() {
    this.ops = [];
    this.under = [];
    this.locked = 0;
    this.band = null;
    this.stroke = null;
    this.chunk = [];
    this.clearSurface();
  }

  clearSurface() {
    this.ctx.fillStyle = "#ffffff";
    this.ctx.fillRect(0, 0, W, H);
  }

  setBand(band) {
    this.band = band && band.count ? band : null;
    this.redraw();
  }

  setOps(ops, locked = 0) {
    this.ops = Array.isArray(ops) ? ops.slice() : [];
    this.locked = locked;
    this.redraw();
  }

  applyOps(ops) {
    for (const op of ops) {
      this.ops.push(op);
      this.paint(op);
    }
  }

  redraw() {
    const ctx = this.ctx;
    this.clearSurface();
    if (this.under.length) {
      ctx.save();
      if (this.band) {
        // от соседней полосы видно только краешек на стыке
        ctx.beginPath();
        ctx.rect(0, Math.max(0, this.band.y0 - this.band.seam), W, this.band.seam);
        ctx.clip();
        ctx.globalAlpha = 0.6;
      } else {
        ctx.globalAlpha = 0.28;
      }
      for (const op of this.under) this.paint(op);
      ctx.restore();
    }
    for (const op of this.ops) this.paint(op);
    if (this.band) this.paintBand();
  }

  /** Затемняем чужие полосы и рисуем границы своей. */
  paintBand() {
    const ctx = this.ctx;
    const { y0, y1 } = this.band;
    ctx.fillStyle = "rgba(44, 18, 89, 0.12)";
    ctx.fillRect(0, 0, W, y0);
    ctx.fillRect(0, y1, W, H - y1);
    ctx.strokeStyle = "rgba(44, 18, 89, 0.45)";
    ctx.lineWidth = 2;
    ctx.setLineDash([12, 10]);
    ctx.beginPath();
    ctx.moveTo(0, y0); ctx.lineTo(W, y0);
    ctx.moveTo(0, y1); ctx.lineTo(W, y1);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  undo() {
    if (this.ops.length <= this.locked) return;
    const last = this.ops[this.ops.length - 1];
    if (last.s === undefined) {
      this.ops.pop();
    } else {
      while (this.ops.length > this.locked && this.ops[this.ops.length - 1].s === last.s) this.ops.pop();
    }
    this.redraw();
  }

  clear() {
    this.ops = this.ops.slice(0, this.locked);
    this.redraw();
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
    const raw = this.pointAt(event);
    if (this.band && (raw[1] < this.band.y0 || raw[1] > this.band.y1)) return;
    this.canvas.setPointerCapture?.(event.pointerId);
    const [x, y] = this.clamp(raw);

    if (this.tool === "fill") {
      this.push(this.mark({ t: "f", c: this.color, x, y }));
      return;
    }
    this.strokeId += 1;
    this.stroke = this.mark({
      t: "p",
      c: this.tool === "eraser" ? "#ffffff" : this.color,
      w: this.tool === "eraser" ? this.size * 2.2 : this.size,
      s: this.strokeId,
    });
    this.chunk = [x, y];
    this.dot(x, y, this.stroke.c, this.stroke.w);
  }

  onMove(event) {
    if (!this.stroke) return;
    const [x, y] = this.clamp(this.pointAt(event));
    const n = this.chunk.length;
    if (n >= 2 && Math.abs(this.chunk[n - 2] - x) < 0.6 && Math.abs(this.chunk[n - 1] - y) < 0.6) return;
    this.segment(this.chunk[n - 2], this.chunk[n - 1], x, y, this.stroke.c, this.stroke.w);
    this.chunk.push(x, y);
  }

  /** Полоса вшивается в саму операцию — иначе при перерисовке у соседей она разъедется. */
  mark(op) {
    if (this.band) op.b = [this.band.y0, this.band.y1];
    return op;
  }

  clamp([x, y]) {
    if (!this.band) return [x, y];
    return [x, Math.max(this.band.y0, Math.min(this.band.y1, y))];
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
    if (!this.onOps || !this.pending.length) return;
    const ops = this.pending;
    this.pending = [];
    this.onOps(ops);
  }

  // --- рисование ------------------------------------------------------

  paint(op) {
    if (op.b && op.t !== "f") {
      this.ctx.save();
      this.ctx.beginPath();
      this.ctx.rect(0, op.b[0], W, op.b[1] - op.b[0]);
      this.ctx.clip();
    }
    if (op.t === "p") this.paintStroke(op);
    else if (op.t === "f") this.fill(op.x, op.y, op.c, op.b);
    if (op.b && op.t !== "f") this.ctx.restore();
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
  fill(x, y, color, bounds) {
    const ctx = this.ctx;
    const top = bounds ? Math.max(0, Math.round(bounds[0])) : 0;
    const bottom = bounds ? Math.min(H, Math.round(bounds[1])) : H;
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
      const py = (index - px) / W;
      if (px > 0) stack.push(index - 1);
      if (px < W - 1) stack.push(index + 1);
      if (py > top) stack.push(index - W);
      if (py < bottom - 1) stack.push(index + W);
    }
    ctx.putImageData(image, 0, 0);
  }
}

function hexToRgb(hex) {
  const match = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
  if (!match) return null;
  return [parseInt(match[1], 16), parseInt(match[2], 16), parseInt(match[3], 16)];
}
