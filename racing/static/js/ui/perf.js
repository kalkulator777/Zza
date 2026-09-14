/*
 * Оверлей производительности, клавиша F3 (раздел 10.4 контракта).
 *
 * Показывает всё, по чему сверяется бюджет раздела 1: fps и график времени
 * кадра, draw call, треугольники, ping, интервал между снапшотами и
 * использование памяти, если браузер его отдаёт.
 *
 * Цифры оверлей не добывает сам: рендер и сетевой код передают их одним
 * вызовом update(stats). Единственное, что считается здесь, — время кадра,
 * если его не передали.
 *
 * Пока оверлей скрыт, update() выходит на первой же строке и не стоит
 * ничего. В видимом состоянии текст переписывается пять раз в секунду
 * (числа, скачущие каждый кадр, всё равно нечитаемы), а на графике
 * появляется один новый столбик за кадр.
 *
 * Использование из main.js:
 *
 *   import { PerfOverlay } from './ui/perf.js';
 *
 *   const perf = new PerfOverlay(document.getElementById('overlay-perf'));
 *   // в кадровом цикле, после рендера:
 *   perf.update(perfStats);     // объект переиспользуется, не создаётся в кадре
 *
 * Поля stats (все необязательные):
 *   frameMs     длительность кадра, мс
 *   fps         если считает рендер; иначе усредняется из frameMs
 *   drawCalls   renderer.info.render.calls
 *   triangles   renderer.info.render.triangles
 *   ping        RTT до сервера, мс
 *   snapshotMs  интервал между двумя последними снапшотами, мс (ожидается 50)
 *   quality     имя пресета качества
 *   renderScale render scale, 0.5 / 0.75 / 1
 */

const HISTORY = 122;             // столбиков на графике
const TEXT_PERIOD = 200;         // мс между обновлениями текста
const FRAME_BUDGET = 16.67;      // мс на кадр при 60 fps
const GRAPH_MAX = 33.4;          // мс, верх графика (два кадровых бюджета)

function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
}

export class PerfOverlay {
    /**
     * @param {HTMLElement} root корень слоя (#overlay-perf)
     * @param {object} [options] {bindKeys: true} — самому слушать F3
     */
    constructor(root, options) {
        this.root = root;
        const opts = options || {};

        this.visible = false;
        this.history = new Float32Array(HISTORY);
        this.historyHead = 0;
        this.lastFrameStamp = 0;
        this.frameAccum = 0;
        this.frameCount = 0;
        this.nextTextUpdate = 0;

        this._build();
        if (opts.bindKeys !== false) this._bindKeys();
        this.hide();
    }

    show() {
        this.root.hidden = false;
        this.visible = true;
        this.lastFrameStamp = 0;
        this.nextTextUpdate = 0;
    }

    hide() {
        this.root.hidden = true;
        this.visible = false;
    }

    toggle() {
        if (this.visible) this.hide(); else this.show();
    }

    /**
     * Один вызов на кадр. Скрытый оверлей не делает ничего.
     * @param {object} stats см. шапку файла; можно звать и без аргумента
     */
    update(stats) {
        if (!this.visible) return;
        const now = performance.now();

        // Время кадра: берём переданное, иначе меряем сами.
        let frameMs = stats && stats.frameMs;
        if (!(frameMs > 0)) {
            frameMs = this.lastFrameStamp ? now - this.lastFrameStamp : FRAME_BUDGET;
        }
        this.lastFrameStamp = now;
        if (frameMs > 999) frameMs = 999;

        this.history[this.historyHead] = frameMs;
        this.historyHead = (this.historyHead + 1) % HISTORY;
        this.frameAccum += frameMs;
        this.frameCount++;

        this._drawGraph();

        if (now < this.nextTextUpdate) return;
        this.nextTextUpdate = now + TEXT_PERIOD;
        this._updateText(stats, frameMs);
    }

    _updateText(stats, frameMs) {
        const avg = this.frameCount ? this.frameAccum / this.frameCount : frameMs;
        this.frameAccum = 0;
        this.frameCount = 0;

        let fps = stats && stats.fps;
        if (!(fps > 0)) fps = avg > 0 ? 1000 / avg : 0;
        const fpsInt = Math.round(fps);

        this.fpsNode.textContent = String(fpsInt);
        // Пороги приёмки раздела 1: 60 целевое, 45 минимальное.
        this.fpsNode.className = 'perf-fps' + (fpsInt >= 58 ? '' : fpsInt >= 45 ? ' warn' : ' bad');

        this._setValue(this.frameNode, avg.toFixed(2) + ' мс',
            avg <= 17.6 ? 0 : avg <= 22.5 ? 1 : 2);

        // Пик за окно истории — именно он съедает плавность.
        let peak = 0;
        for (let i = 0; i < HISTORY; i++) if (this.history[i] > peak) peak = this.history[i];
        this._setValue(this.peakNode, peak.toFixed(1) + ' мс',
            peak <= 22 ? 0 : peak <= 33 ? 1 : 2);

        const calls = stats && stats.drawCalls;
        this._setValue(this.callsNode, calls === undefined ? '—' : String(calls | 0),
            calls === undefined ? 0 : (calls <= 60 ? 0 : calls <= 90 ? 1 : 2));

        const tris = stats && stats.triangles;
        this._setValue(this.trisNode, tris === undefined ? '—' : formatThousands(tris | 0),
            tris === undefined ? 0 : (tris <= 150000 ? 0 : tris <= 220000 ? 1 : 2));

        const ping = stats && stats.ping;
        this._setValue(this.pingNode, ping === undefined ? '—' : Math.round(ping) + ' мс',
            ping === undefined ? 0 : (ping <= 30 ? 0 : ping <= 90 ? 1 : 2));

        const snap = stats && stats.snapshotMs;
        this._setValue(this.snapNode, snap === undefined ? '—' : Math.round(snap) + ' мс',
            snap === undefined ? 0 : (snap <= 70 ? 0 : snap <= 140 ? 1 : 2));

        // Память — только в браузерах, которые её показывают.
        const mem = performance.memory;
        this._setValue(this.memNode,
            mem ? (mem.usedJSHeapSize / 1048576).toFixed(1) + ' МБ' : 'нет данных', 0);

        const quality = stats && stats.quality;
        const scale = stats && stats.renderScale;
        this.footNode.textContent = 'качество: ' + (quality || '—')
            + ' · render scale: ' + (scale ? Math.round(scale * 100) + '%' : '—')
            + ' · F3 — скрыть';
    }

    _setValue(node, text, level) {
        if (node.textContent !== text) node.textContent = text;
        const cls = level === 0 ? 'v' : level === 1 ? 'v warn' : 'v bad';
        if (node.className !== cls) node.className = cls;
    }

    _drawGraph() {
        const ctx = this.graphCtx;
        const w = this.graphWidth;
        const h = this.graphHeight;
        const dpr = this.graphDpr;

        ctx.clearRect(0, 0, w, h);

        // Линия бюджета 16,7 мс
        const budgetY = h - (FRAME_BUDGET / GRAPH_MAX) * h;
        ctx.strokeStyle = '#3ddc8455';
        ctx.lineWidth = 1 * dpr;
        ctx.beginPath();
        ctx.moveTo(0, budgetY);
        ctx.lineTo(w, budgetY);
        ctx.stroke();

        const barW = w / HISTORY;
        for (let i = 0; i < HISTORY; i++) {
            const value = this.history[(this.historyHead + i) % HISTORY];
            if (value <= 0) continue;
            let bar = (value / GRAPH_MAX) * h;
            if (bar > h) bar = h;
            // Зелёный — стабильные 60 fps (rAF даёт 16,6..17,5 мс), жёлтый —
            // просадка, красный — потерянный кадр.
            ctx.fillStyle = value <= 17.6 ? '#3ddc84'
                : value <= 22.5 ? '#ffc93c' : '#ff5a5f';
            ctx.fillRect(i * barW, h - bar, barW - 0.5 * dpr, bar);
        }
    }

    _bindKeys() {
        // По event.code, чтобы раскладка не влияла (раздел 10.4).
        this._onKeyDown = (e) => {
            if (e.code !== 'F3') return;
            e.preventDefault();
            this.toggle();
        };
        window.addEventListener('keydown', this._onKeyDown);
    }

    _build() {
        const root = this.root;
        root.innerHTML = '';
        const wrap = el('div', 'perf-root');
        root.appendChild(wrap);

        const head = el('div', 'perf-head');
        this.fpsNode = el('div', 'perf-fps', '0');
        head.appendChild(this.fpsNode);
        head.appendChild(el('div', 'perf-tag', 'fps · F3'));
        wrap.appendChild(head);

        const canvas = document.createElement('canvas');
        canvas.className = 'perf-graph';
        wrap.appendChild(canvas);
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        this.graphDpr = dpr;
        this.graphWidth = Math.round(244 * dpr);
        this.graphHeight = Math.round(52 * dpr);
        canvas.width = this.graphWidth;
        canvas.height = this.graphHeight;
        this.graphCtx = canvas.getContext('2d');

        const grid = el('div', 'perf-grid');
        this.frameNode = this._addRow(grid, 'кадр');
        this.peakNode = this._addRow(grid, 'пик');
        this.callsNode = this._addRow(grid, 'draw call');
        this.trisNode = this._addRow(grid, 'тр-ки');
        this.pingNode = this._addRow(grid, 'ping');
        this.snapNode = this._addRow(grid, 'снапшот');
        this.memNode = this._addRow(grid, 'память');
        this._addRow(grid, 'лимит').textContent = '60/150k';
        wrap.appendChild(grid);

        this.footNode = el('div', 'perf-foot', 'F3 — скрыть');
        wrap.appendChild(this.footNode);
    }

    _addRow(grid, key) {
        const row = el('div', 'perf-kv');
        row.appendChild(el('span', 'k', key));
        const value = el('span', 'v', '—');
        row.appendChild(value);
        grid.appendChild(row);
        return value;
    }
}

/** «150 000» — с неразрывным пробелом между разрядами. */
function formatThousands(n) {
    if (n < 1000) return String(n);
    const s = String(n);
    let out = '';
    for (let i = 0; i < s.length; i++) {
        if (i > 0 && (s.length - i) % 3 === 0) out += ' ';
        out += s[i];
    }
    return out;
}
