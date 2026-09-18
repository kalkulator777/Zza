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
import math
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


# Прогон по трамплину (§12.30). Отдельный сценарий, а не правка старого:
# хэши §12.27 сняты СО СТАРОГО, и менять его значило бы обесценить
# записанные числа. Машины ставятся перед въездом и разложены поперёк
# трамплина — по скосам, по сердцевине и мимо, — поэтому любое расхождение
# коробок между хозяевами видно в первом же прыжке.
RAMP_TICKS = 900
RAMP_MARKS = (1, 60, 120, 200, 300, 450, 600, 900)
RAMP_RUN_UP = 90.0          # м от въезда назад, где ставятся машины


def _straightest_ramp(track):
    """Трамплин с самым прямым разгоном перед ним.

    Руля в программе нет: машины едут газом в пол по прямой, и въезд надо
    выбрать такой, до которого они доедут. На кривом разгоне они упираются
    в стену за 60 м до трамплина, и сверка движков становится сверкой двух
    одинаково разбившихся машин — то есть проверкой, которая не может
    покраснеть (§12.30: ровно это и случилось при первой попытке).
    """
    best, best_turn = 0, None
    n = track._n
    for k, ramp in enumerate(track.ramps):
        i = int(((ramp['s0'] - RAMP_RUN_UP) % track.length) * track._inv_step) % n
        stop = int((ramp['s0'] % track.length) * track._inv_step) % n
        turn = 0.0
        while i != stop:
            j = (i + 1) % n
            d = (math.atan2(track._ctx[j], track._ctz[j])
                 - math.atan2(track._ctx[i], track._ctz[i]))
            while d > math.pi:
                d -= 2.0 * math.pi
            while d < -math.pi:
                d += 2.0 * math.pi
            turn += abs(d)
            i = j
        if best_turn is None or turn < best_turn:
            best, best_turn = k, turn
    return track.ramps[best]


def _ramp_spawn(track, index, count):
    """(x, z, yaw) i-й машины перед трамплином с прямым разгоном."""
    ramp = _straightest_ramp(track)
    s = ramp['s0'] - RAMP_RUN_UP
    n = track._n
    f = (s % track.length) * track._inv_step
    i = int(f) % n
    t = f - int(f)
    j = (i + 1) % n
    hw = ramp['half_width']
    # Поперёк: от внешнего скоса до внешнего скоса, с запасом в полметра.
    lateral = ramp['offset'] + (2.0 * index / (count - 1.0) - 1.0) * (hw + 0.5)
    x = (track._cx[i] + (track._cx[j] - track._cx[i]) * t
         + track._cnx[i] * lateral)
    z = (track._cz[i] + (track._cz[j] - track._cz[i]) * t
         + track._cnz[i] * lateral)
    return x, z, math.atan2(track._ctx[i], track._ctz[i]), i


def build_scenario(track_id: str = TRACK_ID) -> dict:
    track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks', track_id + '.json'))
    catalog = load_cars(os.path.join(BASE_DIR, 'content', 'cars.json'))
    margin, height, friction = rh.mesh_params(track)
    ramps = bool(getattr(track, 'ramps', ()))
    ticks = RAMP_TICKS if ramps else TICKS
    marks = RAMP_MARKS if ramps else MARKS
    cars = []
    for i, car_id in enumerate(CAR_IDS):
        spec = catalog.get(car_id)
        tuning = rh.car_tuning(spec, 'arcade')
        rest = (tuning.get('wheel_radius', 0.34)
                + tuning.get('suspension_rest', 0.30)
                + tuning.get('half_height', 0.42) * 0.2)
        if ramps:
            x, z, yaw, hint = _ramp_spawn(track, i, len(CAR_IDS))
        else:
            grid = track.start_grid[i]
            x, z, yaw, hint = grid['x'], grid['z'], grid['yaw'], 0
        ground = track.surface(x, z, hint)[3]
        cars.append({'x': x, 'y': ground + rest, 'z': z,
                     'yaw': yaw, 'tuning': tuning})
    if ramps:
        # Газ в пол и без руля: задача — доехать до кромки и улететь, а не
        # накатать разнообразие. Разнообразие даёт поперечная раскладка.
        program = ('1' * len(CAR_IDS)) * ticks
    else:
        program = build_program(len(CAR_IDS), ticks)
    return {
        'track': track.to_client(),
        'mesh': [margin, height, friction],
        'preset': 0,
        'settle': rh.SETTLE_TICKS,
        'ticks': ticks,
        'marks': list(marks),
        'cars': cars,
        'program': program,
    }


# --- движки ------------------------------------------------------------------

def run_wasmtime(scenario: dict) -> dict:
    host = rh.RapierHost(preset=scenario['preset'])
    host.build_track(scenario['track'], *scenario['mesh'])
    verts, tris = host.track_hashes()
    mesh = {'verts': verts, 'tris': tris, 'size': list(host.mesh_size())}
    host.free_track_mesh()
    # Трамплины: коробки из той же записи трассы (§12.30). На трассе без
    # трамплинов вызов не делает ничего и старый сценарий не трогает.
    mesh['ramps'] = host.add_ramps(scenario['track'], scenario['mesh'][2])
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
    print('сетка полотна: вершины %s, треугольники %s, размер %s, '
          'коробок трамплинов %s'
          % (ref['mesh']['verts'], ref['mesh']['tris'], tuple(ref['mesh']['size']),
             ref['mesh'].get('ramps', 0)))
    bad = 0
    for name in names[1:]:
        other = results[name]
        if (other['mesh']['verts'] != ref['mesh']['verts']
                or other['mesh']['tris'] != ref['mesh']['tris']):
            print('  РАСХОЖДЕНИЕ сетки у %s: %s / %s'
                  % (name, other['mesh']['verts'], other['mesh']['tris']))
            bad += 1
        if other['mesh'].get('ramps', 0) != ref['mesh'].get('ramps', 0):
            print('  РАСХОЖДЕНИЕ числа коробок трамплинов у %s: %s против %s'
                  % (name, other['mesh'].get('ramps'), ref['mesh'].get('ramps')))
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
    parser.add_argument('--track', default=TRACK_ID,
                        help='трасса сценария; с трамплинами (industrial, '
                             'serpentine, ridge) машины ставятся перед '
                             'въездом и летят (§12.30)')
    args = parser.parse_args(argv)
    wanted = [name.strip() for name in args.engines.split(',') if name.strip()]

    scenario = build_scenario(args.track)
    print('стенд сверки движков: трасса %s, %d машин, %d тиков, '
          '%d контрольных точек'
          % (args.track, len(scenario['cars']), scenario['ticks'],
             len(scenario['marks'])))
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
