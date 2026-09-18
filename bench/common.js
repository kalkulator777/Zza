/*
 * Zza — общая логика стендов рендера (bench2d.html / bench3d.html).
 *
 * Классический скрипт (НЕ ES-модуль) — специально, чтобы bench2d.html
 * открывался двойным щелчком (file://) без сервера: Chromium запрещает
 * `<script type="module">` из file:// по CORS, а обычный <script src>
 * из file:// работает нормально. bench3d.html вынужден быть модулем
 * (three.js — ESM), поэтому common.js подключается туда ДО модуля
 * обычным <script src>, а модуль читает готовый window.ZBench.
 *
 * Здесь нет кода рендера — только мир (карта, сущности), счётчики
 * кадров и генератор текстового отчёта. Рендер — в самих HTML-файлах.
 */
(function (global) {
  'use strict';

  var MAP_W = 80;
  var MAP_H = 60;

  var TILE = { FLOOR: 0, FLOOR_ALT: 1, WALL: 2, PILLAR: 3 };

  // 4 типа сущностей: разный размер, разная скорость, разный цвет —
  // чтобы у обоих бэкендов было что рисовать по-разному, а не N клонов.
  var KINDS = [
    { name: 'scout', size: 0.32, height: 0.6, speed: 3.0, color: 0x6fd3ff, css: '#6fd3ff' },
    { name: 'grunt', size: 0.45, height: 0.9, speed: 1.7, color: 0xd6a75c, css: '#d6a75c' },
    { name: 'brute', size: 0.65, height: 1.2, speed: 1.05, color: 0xd65c5c, css: '#e2635a' },
    { name: 'boss', size: 0.95, height: 1.6, speed: 0.6, color: 0x9b5cd6, css: '#b06bf2' },
  ];

  function mulberry32(seed) {
    var a = seed >>> 0;
    return function () {
      a |= 0;
      a = (a + 0x6d2b79f5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function parseQuery(defaults) {
    var p = new URLSearchParams(global.location.search);
    var n = parseInt(p.get('n'), 10);
    if (!(n >= 0)) n = defaults.n;
    var q = p.get('q') === 'high' ? 'high' : 'low';
    var tile = parseInt(p.get('tile'), 10);
    if (!(tile > 0)) tile = defaults.tile;
    var resStr = p.get('res') || defaults.res;
    var m = /^(\d+)x(\d+)$/.exec(resStr);
    var width = m ? parseInt(m[1], 10) : defaults.width;
    var height = m ? parseInt(m[2], 10) : defaults.height;
    var seed = parseInt(p.get('seed'), 10);
    if (!(seed >= 0)) seed = defaults.seed;
    var auto = p.has('auto');
    var autoMs = parseInt(p.get('auto'), 10);
    if (!(autoMs > 0)) autoMs = 0;
    return { n: n, q: q, tile: tile, width: width, height: height, seed: seed, auto: auto, autoMs: autoMs };
  }

  // Карта 80×60: кайма стен, несколько «комнат»-заглушек (стены/колонны)
  // и пятна альт-пола. Полностью детерминирована seed'ом — 2D и 3D стенд
  // с одинаковым seed рисуют один и тот же уровень.
  function generateMap(w, h, rng) {
    var tiles = new Uint8Array(w * h);
    var x, y;
    for (y = 0; y < h; y++) {
      for (x = 0; x < w; x++) {
        tiles[y * w + x] = (x === 0 || y === 0 || x === w - 1 || y === h - 1) ? TILE.WALL : TILE.FLOOR;
      }
    }
    var blobs = Math.floor((w * h) / 220);
    var i, cx, cy, r, kind, dx, dy;
    for (i = 0; i < blobs; i++) {
      cx = 2 + Math.floor(rng() * (w - 4));
      cy = 2 + Math.floor(rng() * (h - 4));
      kind = rng() < 0.5 ? TILE.WALL : TILE.PILLAR;
      r = 1 + Math.floor(rng() * 2);
      for (dy = -r; dy <= r; dy++) {
        for (dx = -r; dx <= r; dx++) {
          x = cx + dx; y = cy + dy;
          if (x < 1 || y < 1 || x >= w - 1 || y >= h - 1) continue;
          if (dx * dx + dy * dy <= r * r) tiles[y * w + x] = kind;
        }
      }
    }
    var patches = Math.floor((w * h) / 90);
    for (i = 0; i < patches; i++) {
      cx = 1 + Math.floor(rng() * (w - 2));
      cy = 1 + Math.floor(rng() * (h - 2));
      r = 1 + Math.floor(rng() * 3);
      for (dy = -r; dy <= r; dy++) {
        for (dx = -r; dx <= r; dx++) {
          x = cx + dx; y = cy + dy;
          if (x < 1 || y < 1 || x >= w - 1 || y >= h - 1) continue;
          if (dx * dx + dy * dy <= r * r && tiles[y * w + x] === TILE.FLOOR) tiles[y * w + x] = TILE.FLOOR_ALT;
        }
      }
    }
    return { w: w, h: h, tiles: tiles };
  }

  function isWalkable(map, x, y) {
    var xi = Math.floor(x), yi = Math.floor(y);
    if (xi < 0 || yi < 0 || xi >= map.w || yi >= map.h) return false;
    var t = map.tiles[yi * map.w + xi];
    return t === TILE.FLOOR || t === TILE.FLOOR_ALT;
  }

  function createEntities(n, map, rng) {
    var list = [];
    for (var i = 0; i < n; i++) {
      var kind = i % KINDS.length;
      var x = map.w / 2, y = map.h / 2, tries = 0;
      do {
        x = 1 + rng() * (map.w - 2);
        y = 1 + rng() * (map.h - 2);
        tries++;
      } while (!isWalkable(map, x, y) && tries < 30);
      var ang = rng() * Math.PI * 2;
      var spd = KINDS[kind].speed * (0.6 + rng() * 0.8);
      list.push({
        id: i,
        kind: kind,
        x: x, y: y,
        vx: Math.cos(ang) * spd, vy: Math.sin(ang) * spd,
        facing: ang,
        phase: rng() * Math.PI * 2,
      });
    }
    return list;
  }

  // Простая блуждающая физика: отскок от стен + редкая случайная смена
  // курса. Стоит по CPU O(n) за кадр — часть нагрузки, которую должен
  // почувствовать стенд при росте n, наравне с отрисовкой.
  function stepEntities(list, map, dt, rng) {
    for (var k = 0; k < list.length; k++) {
      var e = list[k];
      var nx = e.x + e.vx * dt;
      var ny = e.y + e.vy * dt;
      if (!isWalkable(map, nx, e.y)) { e.vx = -e.vx; nx = e.x; }
      if (!isWalkable(map, e.x, ny)) { e.vy = -e.vy; ny = e.y; }
      if (rng() < dt * 0.4) {
        var ang = rng() * Math.PI * 2;
        var spd = Math.hypot(e.vx, e.vy) || 1;
        e.vx = Math.cos(ang) * spd;
        e.vy = Math.sin(ang) * spd;
      }
      e.x = nx; e.y = ny;
      if (e.vx || e.vy) e.facing = Math.atan2(e.vy, e.vx);
    }
  }

  // Детерминированный путь «игрока» — фигура Лиссажу, покрывающая всю
  // карту вплоть до краёв. Гарантирует, что камера реально скроллится и
  // упирается в границы карты (проверяет клэмп камеры/статик-слоя), а не
  // стоит на месте, что сделало бы бенч бессмысленным.
  function playerPath(t, map) {
    var cx = map.w / 2, cy = map.h / 2;
    var rx = map.w / 2 - 3, ry = map.h / 2 - 3;
    return {
      x: cx + Math.sin(t * 0.17) * rx,
      y: cy + Math.sin(t * 0.23 + 1.3) * ry,
    };
  }

  // Скользящее окно последних кадров — для живого HUD (медиана/p99 за
  // последние ~2 с), независимо от 20-секундного прогона.
  function FrameStats(windowSize) {
    this.windowSize = windowSize || 180;
    this.buf = [];
  }
  FrameStats.prototype.push = function (dtMs) {
    this.buf.push(dtMs);
    if (this.buf.length > this.windowSize) this.buf.shift();
  };
  FrameStats.prototype._pct = function (p) {
    if (!this.buf.length) return 0;
    var s = this.buf.slice().sort(function (a, b) { return a - b; });
    return s[Math.min(s.length - 1, Math.floor(p * s.length))];
  };
  FrameStats.prototype.median = function () { return this._pct(0.5); };
  FrameStats.prototype.p99 = function () { return this._pct(0.99); };
  FrameStats.prototype.fps = function () {
    var m = this.median();
    return m > 0 ? 1000 / m : 0;
  };

  // Полный прогон фиксированной длительности (по умолчанию 20 с) —
  // копит КАЖДЫЙ кадр, а не только окно, и на выходе даёт отчёт.
  function RunRecorder(durationMs) {
    this.durationMs = durationMs || 20000;
    this.samples = [];
    this.running = false;
    this.startTime = 0;
  }
  RunRecorder.prototype.start = function () {
    this.samples = [];
    this.running = true;
    this.startTime = performance.now();
  };
  // Возвращает true ровно в кадре, где прогон завершился.
  RunRecorder.prototype.push = function (dtMs) {
    if (!this.running) return false;
    this.samples.push(dtMs);
    if (performance.now() - this.startTime >= this.durationMs) {
      this.running = false;
      return true;
    }
    return false;
  };
  RunRecorder.prototype.stats = function () {
    var s = this.samples.slice().sort(function (a, b) { return a - b; });
    var pct = function (p) { return s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : 0; };
    var sum = 0;
    for (var i = 0; i < s.length; i++) sum += s[i];
    return {
      count: s.length,
      median: pct(0.5),
      p99: pct(0.99),
      min: s.length ? s[0] : 0,
      max: s.length ? s[s.length - 1] : 0,
      avg: s.length ? sum / s.length : 0,
    };
  };

  function fmt(n, d) { return (isFinite(n) ? n : 0).toFixed(d == null ? 2 : d); }

  // Текстовый отчёт «под копирование». Цели по 2.1a/2.1b печатаются
  // обе — отчёт не сводит результат к одному порогу «100 или провал»,
  // решение по числу принимает человек, читающий README.
  function buildReport(o) {
    var fpsMed = o.stats.median > 0 ? 1000 / o.stats.median : 0;
    var fpsP99 = o.stats.p99 > 0 ? 1000 / o.stats.p99 : 0;
    var target = o.quality === 'high'
      ? 'цель для q=high: ≥ 60 fps (контракт DESIGN.md 2.1b, картинка важнее кадров)'
      : 'цель для q=low: ≥ 100 fps (контракт DESIGN.md 2.1a, соревновательный режим)';
    var lines = [
      '=== Zza bench report ===',
      'backend: ' + o.backend,
      'quality: ' + o.quality,
      target,
      'провал = даже q=low не даёт 60 fps',
      'entities (n): ' + o.n,
      'tile px: ' + o.tile,
      'canvas (внутреннее разрешение): ' + o.width + 'x' + o.height,
      'devicePixelRatio: ' + (global.devicePixelRatio || 1) + ' (рендер принудительно на 1x, см. README)',
      'draw calls (последний кадр): ' + o.drawCalls,
      'frames измерено: ' + o.stats.count,
      'frame time median: ' + fmt(o.stats.median) + ' ms  →  fps ' + fmt(fpsMed, 1),
      'frame time p99:    ' + fmt(o.stats.p99) + ' ms  →  fps ' + fmt(fpsP99, 1),
      'frame time min/avg/max: ' + fmt(o.stats.min) + ' / ' + fmt(o.stats.avg) + ' / ' + fmt(o.stats.max) + ' ms',
      'работа/кадр (без ожидания vsync), медиана: ' + fmt(o.workMedian != null ? o.workMedian : 0) + ' ms' +
        (o.workP99 != null ? '  p99: ' + fmt(o.workP99) + ' ms' : ''),
      'GPU (WEBGL_debug_renderer_info): ' + (o.gpu || 'н/д (2D-бэкенд, без WebGL)'),
      'screen: ' + (global.screen ? global.screen.width + 'x' + global.screen.height : 'н/д'),
      'окно браузера: ' + global.innerWidth + 'x' + global.innerHeight,
      'userAgent: ' + navigator.userAgent,
      'timestamp: ' + new Date().toISOString(),
    ];
    return lines.join('\n');
  }

  function getGpuInfo(gl) {
    try {
      var dbg = gl.getExtension('WEBGL_debug_renderer_info');
      if (dbg) return String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL));
      return String(gl.getParameter(gl.RENDERER));
    } catch (e) {
      return 'н/д: ' + e.message;
    }
  }

  // ---- HUD: строится через innerHTML, обновляется через textContent.
  // Никакого fillText/canvas для текста — см. правило 7.2 про кэш текста.
  var HUD_CSS = ''
    + '#zb-hud{position:fixed;top:10px;left:10px;z-index:10;font:12px/1.4 monospace;'
    + 'color:#e8f0ff;background:rgba(10,14,22,.82);border:1px solid #3a4a66;border-radius:8px;'
    + 'padding:10px 12px;min-width:230px;box-shadow:0 4px 18px rgba(0,0,0,.4)}'
    + '#zb-hud .zb-big{font-size:26px;font-weight:700;color:#7fe0a0}'
    + '#zb-hud .zb-label{margin-left:6px;color:#9db0cf}'
    + '#zb-hud .zb-grid{display:grid;grid-template-columns:1fr 1fr;gap:2px 10px;margin:6px 0}'
    + '#zb-hud .zb-warn{color:#ffb454;margin:4px 0}'
    + '#zb-hud button{font:12px monospace;background:#22304a;color:#e8f0ff;border:1px solid #3a4a66;'
    + 'border-radius:5px;padding:4px 8px;cursor:pointer;margin-right:6px}'
    + '#zb-hud button:disabled{opacity:.5;cursor:default}'
    + '#zb-hud textarea{width:100%;height:150px;margin-top:6px;font:11px monospace;background:#0b1220;'
    + 'color:#cfe0ff;border:1px solid #3a4a66;border-radius:5px;box-sizing:border-box;resize:vertical}'
    + '#zb-hud .zb-hint{color:#7488a8;margin-top:6px}'
    + '#zb-hud .zb-progress{margin-left:6px;color:#9db0cf}';

  function createHud(container, opts) {
    opts = opts || {};
    var style = document.createElement('style');
    style.textContent = HUD_CSS;
    document.head.appendChild(style);
    container.innerHTML =
      '<div class="zb-row"><span class="zb-big" id="zb-fps">0</span><span class="zb-label">fps (медиана, окно ~2 с)</span></div>' +
      '<div class="zb-grid">' +
      '<div>кадр мед: <b id="zb-med">0</b> мс</div>' +
      '<div>кадр p99: <b id="zb-p99">0</b> мс</div>' +
      '<div>сущности: <b id="zb-ents">' + (opts.n || 0) + '</b></div>' +
      '<div>draw calls: <b id="zb-draws">0</b></div>' +
      '<div>канва: <b id="zb-res">0x0</b></div>' +
      '<div>режим: <b id="zb-q">' + (opts.q || 'low') + '</b></div>' +
      '<div>работа/кадр: <b id="zb-work">0</b> мс</div>' +
      '<div class="zb-hint" style="grid-column:1/3">(без ожидания vsync — см. README)</div>' +
      '</div>' +
      '<div id="zb-fit-warn" class="zb-warn" style="display:none"></div>' +
      '<div class="zb-row"><button id="zb-run">Прогон 20 секунд</button>' +
      '<button id="zb-fs">Во весь экран</button>' +
      '<span class="zb-progress" id="zb-progress"></span></div>' +
      '<textarea id="zb-report" readonly placeholder="Отчёт появится здесь после прогона — скопируйте и пришлите"></textarea>' +
      '<div class="zb-row"><button id="zb-copy">Копировать отчёт</button></div>' +
      '<div class="zb-hint">backend: ' + (opts.backend || '') + ' · n/q/tile/res задаются через ?n=&q=&tile=&res= в адресной строке</div>';

    var $ = function (id) { return document.getElementById(id); };
    var hud = {
      fps: $('zb-fps'), med: $('zb-med'), p99: $('zb-p99'),
      ents: $('zb-ents'), draws: $('zb-draws'), res: $('zb-res'), q: $('zb-q'),
      work: $('zb-work'),
      fitWarn: $('zb-fit-warn'),
      runBtn: $('zb-run'), fsBtn: $('zb-fs'), progress: $('zb-progress'),
      report: $('zb-report'), copyBtn: $('zb-copy'),
    };

    hud.fsBtn.addEventListener('click', function () {
      var el = document.documentElement;
      if (el.requestFullscreen) el.requestFullscreen().catch(function () {});
    });
    hud.copyBtn.addEventListener('click', function () {
      var text = hud.report.value;
      if (!text) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          hud.copyBtn.textContent = 'Скопировано';
          setTimeout(function () { hud.copyBtn.textContent = 'Копировать отчёт'; }, 1500);
        }, function () { fallbackCopy(hud); });
      } else {
        fallbackCopy(hud);
      }
    });

    return hud;
  }

  function fallbackCopy(hud) {
    hud.report.focus();
    hud.report.select();
    try { document.execCommand('copy'); hud.copyBtn.textContent = 'Скопировано'; }
    catch (e) { hud.copyBtn.textContent = 'Скопируйте вручную (Ctrl+C)'; }
    setTimeout(function () { hud.copyBtn.textContent = 'Копировать отчёт'; }, 1500);
  }

  function updateHudLive(hud, data) {
    hud.fps.textContent = fmt(data.fps, 1);
    hud.med.textContent = fmt(data.median);
    hud.p99.textContent = fmt(data.p99);
    hud.ents.textContent = String(data.ents);
    hud.draws.textContent = String(data.drawCalls);
    hud.res.textContent = data.width + 'x' + data.height;
    hud.q.textContent = data.quality;
    if (hud.work) hud.work.textContent = fmt(data.workMs != null ? data.workMs : 0);
    if (data.fitMismatch) {
      hud.fitWarn.style.display = '';
      hud.fitWarn.textContent = 'окно ' + global.innerWidth + 'x' + global.innerHeight +
        ' ≠ канва ' + data.width + 'x' + data.height + ' — число будет не про целевое разрешение, разверните на весь экран на 1920×1080';
    } else {
      hud.fitWarn.style.display = 'none';
    }
  }

  // Обёртка над 20-секундным прогоном: копит кадры в RunRecorder, водит
  // прогресс-бар в HUD и по завершении зовёт коллбек со статистикой.
  function createRunController(hud, durationMs) {
    var rec = new RunRecorder(durationMs || 20000);
    var doneCb = null;
    hud.runBtn.addEventListener('click', function () {
      if (rec.running) return;
      hud.report.value = '';
      hud.runBtn.disabled = true;
      rec.start();
    });
    return {
      push: function (dtMs) {
        if (!rec.running) return;
        var elapsed = (performance.now() - rec.startTime) / 1000;
        hud.progress.textContent = fmt(Math.min(elapsed, rec.durationMs / 1000), 0) + ' / ' + (rec.durationMs / 1000) + ' с';
        var done = rec.push(dtMs);
        if (done) {
          hud.runBtn.disabled = false;
          hud.progress.textContent = 'готово';
          if (doneCb) doneCb(rec.stats());
        }
      },
      onDone: function (fn) { doneCb = fn; },
    };
  }

  global.ZBench = {
    MAP_W: MAP_W, MAP_H: MAP_H, TILE: TILE, KINDS: KINDS,
    mulberry32: mulberry32,
    parseQuery: parseQuery,
    generateMap: generateMap,
    isWalkable: isWalkable,
    createEntities: createEntities,
    stepEntities: stepEntities,
    playerPath: playerPath,
    FrameStats: FrameStats,
    RunRecorder: RunRecorder,
    buildReport: buildReport,
    getGpuInfo: getGpuInfo,
    createHud: createHud,
    updateHudLive: updateHudLive,
    createRunController: createRunController,
  };
})(window);
