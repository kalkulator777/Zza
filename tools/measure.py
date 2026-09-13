#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Замеры отклика и производительности ZZA ARENA.

Три подкоманды:

  latency  сквозная задержка ввода: от keydown до первого кадра, в котором
           нарисованный боец сдвинулся. Раскладывается на слагаемые.
  frame    цена горячих функций клиента в мс/вызов + реальный fps.
  wsrate   ровность выдачи снапшотов сервером, голым WS-клиентом без браузера.

Как запускать:

    python3 play.py --port 8951 --no-browser --no-discovery     # в одном терминале
    ZZA_PROBE=1 python3 play.py --port 8951 ...                 # для latency нужен probe
    python3 tools/measure.py latency --trials 40
    python3 tools/measure.py frame --throttle 4
    python3 tools/measure.py wsrate

Для latency и frame нужен playwright и хромиум:
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tools/measure.py ...

Замедление процессора — через CDP Emulation.setCPUThrottlingRate, это грубая,
но воспроизводимая замена слабой машины.
"""

import argparse
import json
import random
import statistics as st
import time

PORT = 8951
CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

# ---------------------------------------------------------------- инструментовка

# Обёртки вокруг клиента: логика не меняется, только пишутся времена.
PROBE_JS = r"""
() => {
  if (window.__P) return 'already';
  const P = window.__P = {
    st: 0, bit: 2, t0: 0, tsend: 0, tsnap: 0, tframe: 0,
    base: 0, dq: null, ds: null,
    sends: [], arrivals: [], lastSend: 0, lastArr: 0,
    jitter: 0, loss: 0,
  };
  addEventListener('keydown', e => {
    if (P.st === 1 && e.code === 'KeyD') { P.t0 = e.timeStamp; P.st = 2; }
  }, true);

  const origSend = NET.send.bind(NET);
  NET.send = function (m) {
    if (m && m.t === 'i') {
      const now = performance.now();
      if (P.lastSend) P.sends.push(now - P.lastSend);
      P.lastSend = now;
      if (P.st === 2 && (m.k & P.bit) && !P.tsend) P.tsend = now;
    }
    return origSend(m);
  };

  const origPush = Render.push.bind(Render);
  const deliver = s => {
    origPush(s);
    const now = s._ct;
    if (P.lastArr) P.arrivals.push(now - P.lastArr);
    P.lastArr = now;
    if (P.st === 2 && !P.tsnap) {
      const me = s.p.find(p => p.i === MYPID);
      if (me && Math.abs(me.x - P.base) > 0.05) { P.tsnap = now; P.dq = s._dq; P.ds = s._ds; }
    }
  };
  // эмуляция плохой сети живёт здесь же: сервер ничего об этом не знает
  Render.push = function (s) {
    if (P.loss && Math.random() < P.loss) return;
    if (P.jitter) setTimeout(() => deliver(s), Math.random() * P.jitter);
    else deliver(s);
  };

  const origFrame = Render.frame.bind(Render);
  Render.frame = function (dt) {
    origFrame(dt);
    if (P.st === 2 && !P.tframe) {
      const v = Render._view;
      if (v) {
        const me = v.p.find(p => p.i === MYPID);
        if (me && Math.abs(me.x - P.base) > 0.3) P.tframe = performance.now();
      }
    }
  };
  return 'ok';
}
"""

COST_JS = r"""
() => {
  if (window.__C) { window.__C.reset(); return 'already'; }
  const C = window.__C = { d: {}, reset() { for (const k in this.d) this.d[k] = []; } };
  const rec = (k, ms) => { (C.d[k] = C.d[k] || []).push(ms); };
  const wrap = (obj, name, key) => {
    const o = obj[name].bind(obj);
    obj[name] = function (...a) {
      const t = performance.now();
      const r = o(...a);
      rec(key, performance.now() - t);
      return r;
    };
  };
  ['frame', '_interp', 'drawHUD', 'drawParts', 'drawFighter',
   'drawZones', 'drawStructs', 'drawProjectiles'].forEach(n => wrap(Render, n, n));
  const om = NET.ws.onmessage;
  NET.ws.onmessage = function (ev) {
    const t = performance.now();
    const r = om.call(this, ev);
    rec('onmessage', performance.now() - t);
    rec('bytes', ev.data.length);
    return r;
  };
  return 'ok';
}
"""

WINDOW_JS = r"""(secs) => new Promise(res => {
  if (window.__C) window.__C.reset();
  const ts = []; const t0 = performance.now();
  function f(t) {
    ts.push(t);
    if (t - t0 < secs * 1000) requestAnimationFrame(f);
    else {
      const d = [];
      for (let i = 1; i < ts.length; i++) d.push(ts[i] - ts[i-1]);
      d.sort((a, b) => a - b);
      res({fps: (ts.length - 1) / ((ts[ts.length-1] - ts[0]) / 1000),
           med: d[d.length >> 1], p90: d[Math.floor(d.length * 0.9)], max: d[d.length-1],
           cost: window.__C ? window.__C.d : {}, inmatch: INMATCH,
           level: Render.level, delay: Render.delay !== undefined ? Render.delay : Render.DELAY});
    }
  }
  requestAnimationFrame(f);
})"""

SETTLE_JS = r"""() => {
  const v = Render._view; if (!v) return null;
  const me = v.p.find(p => p.i === MYPID); if (!me) return null;
  const s = Render.buf[Render.buf.length - 1];
  const sm = s && s.p.find(p => p.i === MYPID); if (!sm) return null;
  return {x: me.x, vx: sm.vx, vy: sm.vy, g: sm.g, al: sm.al, hs: sm.hs, st: s.st};
}"""


# ---------------------------------------------------------------- общий вход
def launch(pw):
    return pw.chromium.launch(
        executable_path=CHROME, headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--use-gl=swiftshader", "--enable-unsafe-swiftshader"])


def url():
    return "http://127.0.0.1:%d/" % PORT


def open_menu(page):
    page.goto(url())
    page.wait_for_function("typeof MYPID !== 'undefined' && MYPID !== null && CAT.heroes.length")


def host_match(page, mode, quality=None, stocks=5, bots=0):
    """Создаёт комнату и стартует матч. bots=0 — второго игрока приводит вызывающий."""
    if quality:
        page.evaluate("q => Render.setQuality(q)", quality)
    page.select_option("#m-mode", mode)
    page.select_option("#m-stocks", str(stocks))
    page.click("#btn-create")
    page.wait_for_function("ROOM !== null")
    for _ in range(bots):
        page.click("#btn-bot")
        time.sleep(0.15)
    return page.evaluate("() => ROOM.code")


def begin(page):
    page.click("#btn-ready")
    time.sleep(0.3)
    page.click("#btn-start")
    page.wait_for_function("INMATCH === true", timeout=15000)
    page.wait_for_function(
        "Render.buf.length && Render.buf[Render.buf.length-1].st === 'play'", timeout=25000)
    page.evaluate("() => { Render.qmode = 'fixed'; }")  # авто-подбор не должен ехать в замере


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))]


# ---------------------------------------------------------------- latency
def cmd_latency(args):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        br = launch(pw)
        ctx = br.new_context(viewport={"width": 1280, "height": 760})
        page = ctx.new_page()
        open_menu(page)
        code = host_match(page, args.mode, quality=args.quality)
        # второй игрок — живой, но неподвижный: бот дёргал бы нашего бойца
        for _ in range(1 if args.mode == "1v1" else 3):
            p2 = ctx.new_page()
            open_menu(p2)
            p2.evaluate("c => NET.send({t:'join', code:c})", code)
            p2.wait_for_function("ROOM !== null")
            p2.evaluate("() => NET.send({t:'ready', v:true})")
        page.bring_to_front()
        begin(page)
        page.evaluate(PROBE_JS)
        if args.jitter or args.loss:
            page.evaluate("o => { window.__P.jitter = o.j; window.__P.loss = o.l; }",
                          {"j": args.jitter, "l": args.loss})
        cdp = ctx.new_cdp_session(page)
        if args.throttle > 1:
            cdp.send("Emulation.setCPUThrottlingRate", {"rate": args.throttle})
        time.sleep(1.5)

        rows = []
        for _ in range(args.trials):
            if not wait_still(page):
                continue
            r = one_trial(page)
            if r:
                rows.append(r)
            time.sleep(0.25)
        meta = page.evaluate("""() => ({
            sends: window.__P.sends.slice(-500), arrivals: window.__P.arrivals.slice(-500),
            level: Render.level, delay: Render.delay !== undefined ? Render.delay : Render.DELAY})""")
        br.close()

    report_latency(args, rows, meta)


def wait_still(page, timeout=8.0):
    t_end = time.time() + timeout
    hist = []
    while time.time() < t_end:
        s = page.evaluate(SETTLE_JS)
        if (s and s["st"] == "play" and s["al"] and s["g"] and s["hs"] == 0
                and abs(s["vx"]) < 0.05 and abs(s["vy"]) < 0.05):
            hist.append(s["x"])
            if len(hist) >= 6 and max(hist[-6:]) - min(hist[-6:]) < 0.05:
                return True
        else:
            hist = []
        time.sleep(0.03)
    return False


def one_trial(page, timeout=1.5):
    page.evaluate("""() => {
      const P = window.__P;
      P.t0 = P.tsend = P.tsnap = P.tframe = 0; P.dq = P.ds = null;
      P.base = Render._view.p.find(p => p.i === MYPID).x; P.st = 1;
    }""")
    # Момент нажатия нужно расфазировать с сеткой тиков сервера: без этого
    # ожидание готовности само подстраивается под приход снапшотов, нажатие
    # каждый раз попадает в одну и ту же фазу тика, и слагаемое dq выходит
    # завышенным. Случайная пауза возвращает равномерное попадание в тик.
    time.sleep(random.uniform(0.0, 0.0334))
    page.keyboard.down("d")
    t_end = time.time() + timeout
    res = None
    while time.time() < t_end:
        res = page.evaluate("() => { const P = window.__P; return P.tframe ? "
                            "{t0:P.t0, tsend:P.tsend, tsnap:P.tsnap, tframe:P.tframe,"
                            " dq:P.dq, ds:P.ds} : null; }")
        if res:
            break
        time.sleep(0.005)
    page.keyboard.up("d")
    page.evaluate("() => { window.__P.st = 0; }")
    if not res:
        return None
    return {"A": res["tsend"] - res["t0"], "B": res["tsnap"] - res["tsend"],
            "C": res["tframe"] - res["tsnap"], "T": res["tframe"] - res["t0"],
            "dq": res["dq"], "ds": res["ds"]}


NAMES = {
    "A": "A  ввод ждал отправки",
    "B": "B  сеть + тик + снапшот",
    "C": "C  буфер интерполяции + кадр",
    "T": "T  ИТОГО keydown -> пиксель*",
    "dq": "     dq  ввод лежал до тика",
    "ds": "     ds  тик лежал до снапшота",
}


def report_latency(args, rows, meta):
    if not rows:
        print("нет успешных попыток")
        return
    print("== %s == попыток %d, throttle x%d, %s, буфер %s мс, джиттер %d мс, потери %.0f%%"
          % (args.label, len(rows), args.throttle, args.mode,
             round(meta["delay"]), args.jitter, args.loss * 100))
    print("%-30s %8s %8s %8s %8s %8s" % ("слагаемое", "среднее", "медиана", "p90", "мин", "макс"))
    for k in ("A", "B", "C", "T", "dq", "ds"):
        v = [r[k] for r in rows if r.get(k) is not None]
        if not v:
            continue
        print("%-30s %8.1f %8.1f %8.1f %8.1f %8.1f"
              % (NAMES[k], st.mean(v), st.median(v), pct(v, 0.9), min(v), max(v)))
    print("* не входит путь клавиши до браузера и vsync/scanout (+8..16 мс, одинаково до и после)")
    if meta["sends"]:
        s = meta["sends"]
        print("интервал отправки ввода: медиана %.1f мс, p90 %.1f, макс %.1f"
              % (st.median(s), pct(s, 0.9), max(s)))
    if meta["arrivals"]:
        a = meta["arrivals"]
        print("интервал прихода снапшотов: медиана %.1f мс, p90 %.1f, p99 %.1f, макс %.1f, sd %.2f"
              % (st.median(a), pct(a, 0.9), pct(a, 0.99), max(a), st.pstdev(a)))


# ---------------------------------------------------------------- frame
def cmd_frame(args):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        br = launch(pw)
        ctx = br.new_context(viewport={"width": 1280, "height": 760})
        page = ctx.new_page()
        open_menu(page)
        host_match(page, args.mode, quality=args.quality, bots=3 if args.mode == "2v2" else 1)
        begin(page)
        page.evaluate(COST_JS)
        cdp = ctx.new_cdp_session(page)
        if args.throttle > 1:
            cdp.send("Emulation.setCPUThrottlingRate", {"rate": args.throttle})
        time.sleep(2.0)

        runs = []
        for _ in range(args.reps):
            if not page.evaluate("() => INMATCH"):
                cdp.send("Emulation.setCPUThrottlingRate", {"rate": 1})
                open_menu(page)
                host_match(page, args.mode, quality=args.quality,
                           bots=3 if args.mode == "2v2" else 1)
                begin(page)
                page.evaluate(COST_JS)
                if args.throttle > 1:
                    cdp.send("Emulation.setCPUThrottlingRate", {"rate": args.throttle})
                time.sleep(2.0)
            runs.append(page.evaluate(WINDOW_JS, args.secs))
        br.close()

    print("== %s == throttle x%d, %s, качество %s, окон %d по %.0f c"
          % (args.label, args.throttle, args.mode, args.quality, len(runs), args.secs))
    print("fps: %s -> медиана %.1f"
          % (", ".join("%.1f" % r["fps"] for r in runs), st.median([r["fps"] for r in runs])))
    print("кадр, мс: медиана %.1f  p90 %.1f  макс %.1f"
          % (st.median([r["med"] for r in runs]), st.median([r["p90"] for r in runs]),
             max(r["max"] for r in runs)))
    print("%-16s %7s %9s %9s %9s" % ("функция", "вызовов", "ср. мс", "p90 мс", "мс/с"))
    for k in ("frame", "_interp", "drawHUD", "drawParts", "drawFighter",
              "drawZones", "drawStructs", "drawProjectiles", "onmessage"):
        agg = []
        for r in runs:
            agg += (r["cost"].get(k) or [])
        if not agg:
            continue
        print("%-16s %7d %9.3f %9.3f %9.2f"
              % (k, len(agg), st.mean(agg), pct(agg, 0.9), sum(agg) / (args.secs * len(runs))))
    by = []
    for r in runs:
        by += (r["cost"].get("bytes") or [])
    if by:
        print("с сервера: %d сообщений, %.1f КБ/с, среднее %.0f Б"
              % (len(by), sum(by) / 1024 / (args.secs * len(runs)), st.mean(by)))


# ---------------------------------------------------------------- wsrate
def cmd_wsrate(args):
    import tornado.ioloop
    from tornado.websocket import websocket_connect

    async def run():
        ws = await websocket_connect("ws://127.0.0.1:%d/ws" % PORT)
        times, sizes, frames = [], [], []
        code = None
        t_end = None
        ws.write_message(json.dumps({"t": "hello", "name": "probe"}))
        ws.write_message(json.dumps({"t": "create", "rname": "probe", "mode": args.mode,
                                     "arena": "plato", "stocks": 5}))
        while True:
            raw = await ws.read_message()
            if raw is None:
                break
            now = time.perf_counter()
            m = json.loads(raw)
            if m.get("t") == "room" and code is None:
                code = m["code"]
                for _ in range(1 if args.mode == "1v1" else 3):
                    ws.write_message(json.dumps({"t": "addbot", "level": 2}))
                ws.write_message(json.dumps({"t": "ready", "v": True}))
                ws.write_message(json.dumps({"t": "start"}))
            elif m.get("t") == "s":
                times.append(now)
                sizes.append(len(raw))
                frames.append(m["f"])
                if t_end is None:
                    t_end = now + args.secs
                elif now > t_end:
                    break
        ws.close()
        times, sizes, frames = times[20:], sizes[20:], frames[20:]   # выбросим отсчёт
        d = [(times[i + 1] - times[i]) * 1000 for i in range(len(times) - 1)]
        df = sorted(set(frames[i + 1] - frames[i] for i in range(len(frames) - 1)))
        span = times[-1] - times[0]
        print("== %s == %s" % (args.label, args.mode))
        print("снапшотов %d за %.1f c -> %.1f Гц" % (len(times), span, len(times) / span))
        print("интервал, мс: медиана %.2f среднее %.2f sd %.2f p90 %.2f p99 %.2f мин %.2f макс %.2f"
              % (st.median(d), st.mean(d), st.pstdev(d), pct(d, .9), pct(d, .99), min(d), max(d)))
        print("шаг кадра мира между снапшотами: %s" % df)
        print("снапшот: медиана %d Б, макс %d Б, трафик %.1f КБ/с на клиента"
              % (st.median(sizes), max(sizes), sum(sizes) / span / 1024))

    tornado.ioloop.IOLoop.current().run_sync(run)


# ---------------------------------------------------------------- main
def main():
    global PORT
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=PORT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("latency")
    a.add_argument("--trials", type=int, default=40)
    a.add_argument("--throttle", type=int, default=1)
    a.add_argument("--mode", default="1v1")
    a.add_argument("--quality", default="high")
    a.add_argument("--jitter", type=float, default=0.0, help="случайная задержка снапшота, мс")
    a.add_argument("--loss", type=float, default=0.0, help="доля потерянных снапшотов, 0..1")
    a.add_argument("--label", default="замер")
    a.set_defaults(fn=cmd_latency)

    b = sub.add_parser("frame")
    b.add_argument("--throttle", type=int, default=4)
    b.add_argument("--secs", type=float, default=8.0)
    b.add_argument("--reps", type=int, default=3)
    b.add_argument("--mode", default="2v2")
    b.add_argument("--quality", default="high")
    b.add_argument("--label", default="замер")
    b.set_defaults(fn=cmd_frame)

    c = sub.add_parser("wsrate")
    c.add_argument("--secs", type=float, default=12.0)
    c.add_argument("--mode", default="1v1")
    c.add_argument("--label", default="замер")
    c.set_defaults(fn=cmd_wsrate)

    args = ap.parse_args()
    PORT = args.port
    args.fn(args)


if __name__ == "__main__":
    main()
