# -*- coding: utf-8 -*-
"""Один `.wasm` — один результат во всех движках (§12.22, §12.24).

Что проверяется. Сервер и браузер гоняют ОДИН И ТОТ ЖЕ модуль физики, и вся
затея четвёртого этапа держится на том, что он считает одинаково везде.
Стенд прогоняет один сценарий — трасса, восемь машин, длинная программа
ввода — в трёх движках и сверяет хэш мира на семи контрольных точках:

* **wasmtime** из ``vendor/`` — им считает сервер;
* **Node (V8)** — тот же движок, что в Chromium, но без браузера;
* **Chromium** и **Firefox** через playwright — то, во что играют на самом деле.

Движок, которого на машине нет, стенд пропускает с внятной строкой, а не
валит прогон: отсутствие браузера — это про окружение, а не про игру.

Сценарий готовится здесь и уезжает движкам файлом: те же девять массивов
осевой линии формата 12.1, та же расстановка, те же настройки машин и та же
программа ввода, сжатая в строку base32 по пять бит на машину на тик.
Прогон в движке делает tools/wasm_parity.mjs — общий и для Node, и для
страницы в браузере.

    python3 tools/test_wasm_parity.py            # все доступные движки
    python3 tools/test_wasm_parity.py --engines wasmtime,node
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('RACING_PHYSICS', 'shadow')   # хозяин нужен нам живым

from game import rapier_host as rh          # noqa: E402
from game.track import Track                # noqa: E402
from game.cars import load_cars             # noqa: E402

TRACK_ID = 'avenue'
CAR_IDS = ('hatch', 'muscle', 'buggy', 'van', 'wedge', 'hatch', 'buggy', 'van')
TICKS = 10000
MARKS = (1, 10, 100, 1000, 2500, 5000, 7500, 10000)
B32 = '0123456789abcdefghijklmnopqrstuv'

# Где искать предустановленные браузеры: playwright install тут запускать
# нельзя (§1 — пакеты не ставим).
BROWSERS = {
    'chromium': ('/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
                 '/opt/pw-browsers/chromium/chrome-linux/chrome'),
    'firefox': ('/opt/pw-browsers/firefox-1495/firefox/firefox',
                '/opt/pw-browsers/firefox/firefox/firefox'),
}


def build_program(cars: int, ticks: int, seed: int = 20260915) -> str:
    """Программа ввода: по символу base32 на машину на тик.

    Биты те же, что у протокола: 1 газ, 2 тормоз, 4 налево, 8 направо,
    16 ручник. Каждая машина меняет команду на своём периоде — так за
    10 000 тиков набирается и разгон, и торможение, и занос, и стена.
    """
    rng = random.Random(seed)
    out = []
    held = [1] * cars
    period = [37 + 11 * i for i in range(cars)]
    for t in range(ticks):
        for c in range(cars):
            if t % period[c] == 0:
                r = rng.random()
                m = 1
                if r < 0.30:
                    m |= 4
                elif r < 0.60:
                    m |= 8
                if r > 0.90:
                    m |= 2
                if 0.62 < r < 0.74:
                    m |= 16
                held[c] = m
            out.append(B32[held[c]])
    return ''.join(out)


def build_scenario() -> dict:
    track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks', TRACK_ID + '.json'))
    catalog = load_cars(os.path.join(BASE_DIR, 'content', 'cars.json'))
    margin, height, friction = rh.mesh_params(track)
    cars = []
    for i, car_id in enumerate(CAR_IDS):
        spec = catalog.get(car_id)
        grid = track.start_grid[i]
        tuning = rh.car_tuning(spec, 'arcade')
        rest = (tuning.get('wheel_radius', 0.34)
                + tuning.get('suspension_rest', 0.30)
                + tuning.get('half_height', 0.42) * 0.2)
        ground = track.surface(grid['x'], grid['z'], 0)[3]
        cars.append({'x': grid['x'], 'y': ground + rest, 'z': grid['z'],
                     'yaw': grid['yaw'], 'tuning': tuning})
    return {
        'track': track.to_client(),
        'mesh': [margin, height, friction],
        'preset': 0,
        'settle': rh.SETTLE_TICKS,
        'ticks': TICKS,
        'marks': list(MARKS),
        'cars': cars,
        'program': build_program(len(cars), TICKS),
    }


# --- движки ------------------------------------------------------------------

def run_wasmtime(scenario: dict) -> dict:
    host = rh.RapierHost(preset=scenario['preset'])
    host.build_track(scenario['track'], *scenario['mesh'])
    verts, tris = host.track_hashes()
    mesh = {'verts': verts, 'tris': tris, 'size': list(host.mesh_size())}
    host.free_track_mesh()
    for car in scenario['cars']:
        host.tuning_preset(scenario['preset'])
        host.set_tuning(car['tuning'])
        host.spawn_car(car['x'], car['y'], car['z'], car['yaw'])
    abi = rh.abi
    n = len(scenario['cars'])
    host._sync()
    for k in range(len(host.inputs)):
        host.inputs[k] = 0.0
    host.step(scenario['settle'])

    program = scenario['program']
    marks = set(scenario['marks'])
    ci = abi.CarInput
    points = []
    for t in range(scenario['ticks']):
        host._sync()
        inputs = host.inputs
        row = t * n
        for c in range(n):
            m = B32.index(program[row + c])
            base = c * ci.FLOATS
            inputs[base + ci.THROTTLE] = 1.0 if m & 1 else 0.0
            inputs[base + ci.BRAKE] = 1.0 if m & 2 else 0.0
            inputs[base + ci.STEER] = (1.0 if m & 4 else 0.0) - (1.0 if m & 8 else 0.0)
            inputs[base + ci.HANDBRAKE] = 1.0 if m & 16 else 0.0
            inputs[base + ci.OFFTRACK] = 0.0
        host.step(1)
        if (t + 1) in marks:
            points.append({'tick': t + 1, 'world': host.world_hash(),
                           'state': host.state_hash()})
    return {'mesh': mesh, 'points': points}


def run_node(scenario_path: str) -> dict:
    wasm = rh.WASM_PATH
    done = subprocess.run(['node', os.path.join(BASE_DIR, 'tools', 'wasm_parity.mjs'),
                           scenario_path, wasm],
                          cwd=BASE_DIR, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError('node вернул %d: %s' % (done.returncode, done.stderr[-600:]))
    return json.loads(done.stdout)


def find_browser(kind: str):
    for path in BROWSERS.get(kind, ()):
        if os.path.isfile(path):
            return path
    return None


def run_browser(kind: str, scenario_path: str) -> dict:
    from playwright.sync_api import sync_playwright
    page_html = (
        '<!doctype html><meta charset="utf-8"><title>сверка</title>'
        '<script type="module">\n'
        'import { RapierHost } from "/static/js/rapier_host.js";\n'
        'import { runScenario } from "/tools/wasm_parity.mjs";\n'
        'import * as abi from "/native/abi/abi_layout.js";\n'
        'window.__run = async () => {\n'
        '  const bytes = new Uint8Array(await (await fetch("/wasm")).arrayBuffer());\n'
        '  const host = await RapierHost.load({ wasm: bytes, abi: abi });\n'
        '  const scenario = await (await fetch("/scenario")).json();\n'
        '  return runScenario(host, RapierHost, scenario);\n'
        '};\n'
        'window.__ready = true;\n'
        '</script>')
    executable = find_browser(kind)
    with sync_playwright() as pw:
        engine = getattr(pw, kind)
        args = (['--no-sandbox', '--disable-dev-shm-usage']
                if kind == 'chromium' else [])
        browser = engine.launch(headless=True, executable_path=executable, args=args)
        # no_viewport: связка playwright и Firefox на этой машине спотыкается
        # на Browser.setDefaultViewport (версии разъехались). Размера окна
        # стенду не надо — он считает физику, а не рисует.
        context = browser.new_context(no_viewport=True)
        page = context.new_page()

        def route(handler):
            url = handler.request.url
            if url.endswith('/wasm'):
                with open(rh.WASM_PATH, 'rb') as fh:
                    handler.fulfill(status=200, body=fh.read(),
                                    content_type='application/wasm')
                return
            if url.endswith('/scenario'):
                with open(scenario_path, 'rb') as fh:
                    handler.fulfill(status=200, body=fh.read(),
                                    content_type='application/json')
                return
            if url.endswith('/page'):
                handler.fulfill(status=200, body=page_html, content_type='text/html')
                return
            path = url.split('://', 1)[1].split('/', 1)[1]
            local = os.path.join(BASE_DIR, path)
            if os.path.isfile(local):
                with open(local, 'rb') as fh:
                    handler.fulfill(status=200, body=fh.read(),
                                    content_type='text/javascript')
                return
            handler.fulfill(status=404, body='нет такого файла: ' + path)

        page.route('**/*', route)
        page.goto('http://parity.local/page', wait_until='load')
        page.wait_for_function('() => window.__ready === true', timeout=30000)
        out = page.evaluate('() => window.__run()')
        browser.close()
    return out


# --- сверка -------------------------------------------------------------------

def compare(results: dict) -> int:
    names = list(results)
    base = names[0]
    ref = results[base]
    print('')
    print('сетка полотна: вершины %s, треугольники %s, размер %s'
          % (ref['mesh']['verts'], ref['mesh']['tris'], tuple(ref['mesh']['size'])))
    bad = 0
    for name in names[1:]:
        other = results[name]
        if (other['mesh']['verts'] != ref['mesh']['verts']
                or other['mesh']['tris'] != ref['mesh']['tris']):
            print('  РАСХОЖДЕНИЕ сетки у %s: %s / %s'
                  % (name, other['mesh']['verts'], other['mesh']['tris']))
            bad += 1
    print('')
    print('%-8s %-18s %-18s %s' % ('тик', 'хэш мира', 'хэш состояний', 'движки'))
    for i, point in enumerate(ref['points']):
        agree = []
        for name in names:
            p = results[name]['points'][i]
            if p['world'] == point['world'] and p['state'] == point['state']:
                agree.append(name)
            else:
                agree.append(name + ' РАЗОШЁЛСЯ')
                bad += 1
        print('%-8d %-18s %-18s %s'
              % (point['tick'], point['world'], point['state'], ', '.join(agree)))
    print('')
    if bad:
        print('РАСХОЖДЕНИЙ: %d' % bad)
    else:
        print('СОВПАЛО во всех движках (%s) на всех %d точках'
              % (', '.join(names), len(ref['points'])))
    return 1 if bad else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='test_wasm_parity.py',
        description='Один .wasm — один результат в wasmtime, Node и Chromium.')
    parser.add_argument('--engines', default='wasmtime,node,chromium,firefox',
                        help='какие движки гонять через запятую')
    parser.add_argument('--keep', metavar='FILE', default=None,
                        help='сохранить сценарий в файл (для ручного прогона)')
    args = parser.parse_args(argv)
    wanted = [name.strip() for name in args.engines.split(',') if name.strip()]

    print('стенд сверки движков: трасса %s, %d машин, %d тиков, %d контрольных точек'
          % (TRACK_ID, len(CAR_IDS), TICKS, len(MARKS)))
    scenario = build_scenario()
    tmp = args.keep or os.path.join(tempfile.mkdtemp(prefix='wasm-parity-'),
                                    'scenario.json')
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(scenario, fh)
    print('сценарий: %s (%.1f КБ)' % (tmp, os.path.getsize(tmp) / 1024.0))

    results = {}
    for name in wanted:
        try:
            if name == 'wasmtime':
                results[name] = run_wasmtime(scenario)
            elif name == 'node':
                results[name] = run_node(tmp)
            elif name in BROWSERS:
                results[name] = run_browser(name, tmp)
            else:
                print('  неизвестный движок %r — пропущен' % name)
                continue
            print('  %s: готово' % name)
        except Exception as exc:                 # движка нет — это не провал игры
            print('  %s ПРОПУЩЕН: %s' % (name, str(exc)[:300]))
    if len(results) < 2:
        print('сверять нечего: движков меньше двух')
        return 2
    return compare(results)


if __name__ == '__main__':
    sys.exit(main())
