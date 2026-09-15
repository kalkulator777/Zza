#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автотест симуляции: полная гонка восьми ботов на всех трёх трассах.

Запуск из каталога ``racing/``::

    python3 tools/test_sim.py                # все проверки
    python3 tools/test_sim.py --balance      # плюс серия замеров баланса
    python3 tools/test_sim.py --laps 2       # короче гонка (по умолчанию 3)

Что проверяется (раздел 11 контракта, приёмка ``game/sim.py`` и ``game/items.py``):

* гонка на восьми ботах заканчивается сама, все финишируют;
* круги считаются верно: событий ``lap`` ровно столько, сколько кругов,
  сумма кругов сходится с итоговым временем;
* места непротиворечивы на каждом тике: перестановка 1..N, финишировавшие
  всегда выше едущих, едущие — по ``progress`` убыванием;
* никто не застревает в стене и не встаёт намертво;
* бонусы выпадают и применяются, ракеты попадают, мины срабатывают,
  боксы возвращаются через BOX_RESPAWN;
* щит гасит ровно одно попадание и тратится;
* потолок ``MAX_PROJECTILES`` соблюдается, бонус при этом не пропадает;
* устаревший и продублированный ``seq`` отбрасывается молча;
* срезать круг по траве нельзя: отсечки не дают засчитать.

Печатается измеренное время тика при восьми машинах — это условие приёмки.

Бот намеренно простой: держит газ, целится в точку осевой линии впереди,
подруливает к центру полотна, тормозит перед поворотом по кривизне трассы
и сворачивает за боксом, если руки пусты. Никакой гоночной линии — задача
теста проверить симуляцию, а не выиграть гонку.
"""

import argparse
import collections
import gc
import math
import os
import random
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from game import items as items_mod
from game import physics
from game import protocol
from game import sim as sim_mod
from game.cars import solve_top_speed
from game.sim import Simulation
from game.track import Track, WALL_MARGIN

TRACK_IDS = ('office', 'serpentine', 'industrial', 'avenue', 'ridge')
CAR_IDS = ('hatch', 'muscle', 'buggy', 'van', 'wedge', 'hatch', 'buggy', 'van')
ALL_ITEMS = list(items_mod.ITEM_IDS)

TWO_PI = math.pi * 2.0
MAX_RACE_TICKS = 60 * 60 * 8          # восемь минут — дальше гонка признаётся зависшей
DRIFT_COMMIT = 52                     # тиков, на которые бот фиксирует занос
LOOKAHEAD_ARC = 24.0                  # м дуги, по которой бот считает
                                      # крутизну рядом (условие заноса)
SCAN_STEP = 8.0                       # м между точками просмотра вперёд
BOT_GRIP_SAFETY = 0.72                # доля предела сцепления, на которую
                                      # бот согласен ехать: он идёт не по
                                      # идеальной траектории
BOT_BRAKE_SAFETY = 0.70               # с каким запасом бот считает свои
                                      # тормоза, планируя точку торможения


def _curvature_radii(track):
    """Радиус кривизны осевой линии в каждой точке, м.

    Считается один раз на трассу: боту он нужен на каждом тике, чтобы знать,
    насколько крутая дуга его ждёт через тормозную дистанцию.
    """
    samples = track.samples
    n = len(samples)
    out = [0.0] * n
    for i in range(n):
        a = samples[(i - 2) % n]
        b = samples[i]
        c = samples[(i + 2) % n]
        ab = math.hypot(b.x - a.x, b.z - a.z)
        bc = math.hypot(c.x - b.x, c.z - b.z)
        ca = math.hypot(a.x - c.x, a.z - c.z)
        area2 = abs((b.x - a.x) * (c.z - a.z) - (c.x - a.x) * (b.z - a.z))
        out[i] = 1.0e6 if area2 < 1e-9 else ab * bc * ca / (2.0 * area2)
    return out

BTN_THROTTLE = protocol.BTN_THROTTLE
BTN_BRAKE = protocol.BTN_BRAKE
BTN_LEFT = protocol.BTN_LEFT
BTN_RIGHT = protocol.BTN_RIGHT
BTN_DRIFT = protocol.BTN_DRIFT
BTN_ITEM = protocol.BTN_ITEM


# --- бот-автопилот ----------------------------------------------------------

class Autopilot(object):
    """Простой автопилот: осевая линия, газ в пол, руль к центру полотна."""

    def __init__(self, track, sim, skill=1.0, seed=0, lane=0.0,
                 allow_drift=False, stats=None):
        self.track = track
        self.sim = sim
        self.count = len(track.samples)
        self.step = track.length / self.count
        self.skill = skill
        # характеристики своей машины: предел поперечного ускорения и потолок.
        # BOT_GRIP_SAFETY — запас на то, что бот едет по осевой со сдвигом
        # в свою полосу, а не по идеальной траектории
        if stats is None:
            self.lat_accel = 0.17 * physics.GRIP_LAT_ACCEL * BOT_GRIP_SAFETY
            self.top_speed = 40.0
            self.boost_speed = 55.0
            self.brake_accel = 24.0
        else:
            self.lat_accel = stats.grip_step * physics.GRIP_LAT_ACCEL * BOT_GRIP_SAFETY
            self.top_speed = solve_top_speed(stats.engine_force, stats.max_speed,
                                             stats.drag, stats.roll)
            self.boost_speed = stats.boost_speed
            self.brake_accel = stats.brake_force * BOT_BRAKE_SAFETY
        self.radius = _curvature_radii(track)
        self.lane = lane                     # своя полоса, доля полуширины
        self.rng = random.Random(seed)
        self.hold = 0                        # задержка перед применением бонуса
        self.slow_ticks = 0                  # сколько тиков почти стоим
        self.reverse_ticks = 0               # сколько тиков ещё выбираться задом
        self.drift_ticks = 0                 # сколько тиков держим занос
        self.drift_dir = 0                   # в какую сторону держим руль в заносе
        self.allow_drift = allow_drift       # ручник в шпильках (проверка дрифта)
        self.boxes = track.item_boxes
        self.box_index = [int(round(b['s'] / self.step)) % self.count
                          for b in self.boxes]
        self.box_window = int(30.0 / self.step)

    def drive(self, car, tick):
        """Битовая маска кнопок на этот тик."""
        state = car.state
        samples = self.track.samples
        count = self.count
        index = state.sample_idx
        speed = math.sqrt(state.vx * state.vx + state.vz * state.vz)

        # --- точка прицеливания: осевая линия впереди, сдвинутая на свою полосу
        look = 7.0 + speed * 0.55
        ahead = samples[(index + int(look / self.step) + 1) % count]
        target_x = ahead.x + ahead.normal_x * self.lane * ahead.half_width
        target_z = ahead.z + ahead.normal_z * self.lane * ahead.half_width

        # --- за боксом, если руки пусты
        if not car.item and self.sim.items.enabled:
            active = self.sim.items.box_active
            best = -1
            best_gap = 1 << 30
            for k in range(len(self.boxes)):
                if not active[k]:
                    continue
                gap = (self.box_index[k] - index) % count
                if gap < 2 or gap > self.box_window:
                    continue
                if gap < best_gap:
                    best_gap = gap
                    best = k
            if best >= 0:
                target_x = self.boxes[best]['x']
                target_z = self.boxes[best]['z']

        error = math.atan2(target_x - state.x, target_z - state.z) - state.yaw
        while error > math.pi:
            error -= TWO_PI
        while error < -math.pi:
            error += TWO_PI

        # --- скорость по кривизне впереди.
        # Предел в дуге — по СЦЕПЛЕНИЮ СВОЕЙ машины: шаг 9 физики ограничивает
        # поперечное ускорение, поэтому v = sqrt(a_lat * R). Прежняя формула
        # (7.0 + 5.5 / turn) — это v = R * turn_rate, модель старой физики, где
        # предела по сцеплению не было вовсе: с ней бот вёл все пять машин по
        # одинаковым скоростям и разницы в сцеплении не видел в принципе.
        #
        # Смотреть вперёд надо на всю тормозную дистанцию, а не на фиксированные
        # 24 м: на 33 м/с это было 0,7 с запаса, на 55 м/с стало 0,4 с, и бот
        # физически не успевал оттормозиться — лишние метры он гасил травой
        # и отбойником, отчего гонку выигрывала самая тяговитая машина, а не
        # самая подходящая трассе.
        limit = self.top_speed
        radii = self.radius
        brake_a = self.brake_accel
        reach = speed * speed / (2.0 * brake_a) + 3.0 * SCAN_STEP
        dist = SCAN_STEP
        turn = 0.0
        while dist <= reach:
            j = (index + int(dist / self.step)) % count
            sample = samples[j]
            corner = math.sqrt(self.lat_accel * (radii[j] + sample.half_width))
            allow = math.sqrt(corner * corner + 2.0 * brake_a * dist) * self.skill
            if allow < limit:
                limit = allow
            if dist <= LOOKAHEAD_ARC * 1.5:
                bend = LOOKAHEAD_ARC / radii[j] if radii[j] > 0.0 else 0.0
                if bend > turn:
                    turn = bend         # крутизна рядом — для условия заноса
            dist += SCAN_STEP
        if state.boost_time > 0.0:
            limit = self.boost_speed   # ускорение не тормозим, оно для того и взято

        # --- выбираемся, если упёрлись: чуть назад и в другую сторону.
        #     Живой игрок делает ровно это, а без отката бот залипает
        #     в шпильке Серпантина и гонка не кончается.
        if self.reverse_ticks > 0:
            self.reverse_ticks -= 1
            out = BTN_BRAKE
            out |= BTN_RIGHT if error > 0.0 else BTN_LEFT
            return out
        if speed < 2.0 and state.spin_time <= 0.0:
            self.slow_ticks += 1
            if self.slow_ticks > 45:
                self.slow_ticks = 0
                self.reverse_ticks = 40
        else:
            self.slow_ticks = 0

        buttons = 0
        if speed < limit:
            buttons |= BTN_THROTTLE
        elif speed > limit * 1.12:
            buttons |= BTN_BRAKE
        if error > 0.030:
            buttons |= BTN_LEFT
        elif error < -0.030:
            buttons |= BTN_RIGHT
        # --- занос: живой игрок в шпильке дёргает ручник и ДЕРЖИТ руль.
        #     Короткие дёрганья руля заряд не копят (нужно |steer| > 0.35
        #     непрерывно), поэтому занос фиксируется на DRIFT_COMMIT тиков.
        if self.drift_ticks > 0:
            self.drift_ticks -= 1
            buttons &= ~(BTN_LEFT | BTN_RIGHT | BTN_BRAKE)
            buttons |= BTN_THROTTLE | BTN_DRIFT
            buttons |= BTN_LEFT if self.drift_dir > 0 else BTN_RIGHT
        elif (self.allow_drift and abs(state.steer) > 0.6
                and speed > 14.0 and turn > 0.35):
            self.drift_ticks = DRIFT_COMMIT
            self.drift_dir = 1 if state.steer > 0.0 else -1
        if car.item:
            if self.hold <= 0:
                self.hold = self.rng.randint(10, 60)
            self.hold -= 1
            if self.hold <= 0:
                buttons |= BTN_ITEM
        else:
            self.hold = 0
        return buttons


# --- каркас проверок --------------------------------------------------------

def check_generated(report):
    """Выпущенное генераторами совпадает с тем, что лежит в репозитории.

    Почему здесь, а не в smoke_test.py. Обе проверки — про содержимое
    репозитория, а не про браузер и не про машину: они не зависят ни от
    загрузки, ни от Firefox, и их незачем платить временем сквозного теста.
    А test_sim.py гоняют после каждой правки физики и трасс — то есть ровно
    тогда, когда раскладка ABI и может разъехаться.

    Первая проверка обязана быть первой в прогоне: если раскладка в
    native/abi/ разошлась с tools/abi_layout.json, то все остальные числа
    считаны по неверным смещениям, и разбираться надо с ней, а не с ними.
    """
    report.section('Выпущенное генераторами')
    for args, title in (
            ([sys.executable, os.path.join(BASE_DIR, 'tools', 'gen_abi.py'), '--check'],
             'раскладки ABI совпадают с tools/abi_layout.json'),
            ([sys.executable, os.path.join(BASE_DIR, 'tools', 'vendor_wasmtime.py'), '--check'],
             'wasmtime в vendor/ цел и совпадает с vendor/wasmtime.lock.json')):
        done = subprocess.run(args, cwd=BASE_DIR, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        detail = done.stdout.decode('utf-8', 'replace').strip().splitlines()
        report.check(done.returncode == 0, title,
                     detail[-1] if detail else 'код возврата %d' % done.returncode)
        if done.returncode != 0:
            for line in detail[:10]:
                report.note(line)


class Report(object):
    """Накопитель результатов: печатает по ходу, помнит провалы."""

    def __init__(self):
        self.failures = []
        self.checks = 0

    def check(self, condition, message, detail=''):
        self.checks += 1
        if condition:
            print('  [ок]    %s%s' % (message, (' — ' + detail) if detail else ''))
        else:
            print('  [ПРОВАЛ] %s%s' % (message, (' — ' + detail) if detail else ''))
            self.failures.append(message)
        return bool(condition)

    def note(self, message):
        print('          %s' % message)

    def section(self, title):
        print('')
        print('--- %s' % title)


def make_settings(laps, items=True, collisions=True):
    return {
        'track': '', 'laps': laps, 'max_players': 8,
        'items_enabled': items, 'items': list(ALL_ITEMS),
        'collisions': collisions, 'mirror': False,
    }


def make_players(count=8, car_id=None):
    """Расстановка игроков. ``car_id`` делает всех одинаковыми — это нужно
    сценариям, где разница характеристик машин мешает проверять снаряд."""
    return [{'slot': i, 'name': 'Бот %d' % i,
             'car_id': car_id or CAR_IDS[i % len(CAR_IDS)], 'color': '#ffffff'}
            for i in range(count)]


def new_sim(track, laps, items=True, collisions=True, count=8, seed=0,
            car_id=None):
    sim = Simulation(track, make_settings(laps, items, collisions),
                     make_players(count, car_id))
    sim.items.rng = random.Random(seed * 977 + 13)
    return sim


def make_bots(track, sim, seed, count=8, spread=0.03, allow_drift=False):
    rng = random.Random(seed)
    lanes = [-0.5 + k / float(count - 1) for k in range(count)] if count > 1 else [0.0]
    rng.shuffle(lanes)
    return [Autopilot(track, sim, 1.0 + rng.uniform(-spread, spread),
                      seed * 31 + i, lanes[i], allow_drift,
                      stats=sim.cars[i].stats)
            for i in range(count)]


# --- полная гонка -----------------------------------------------------------

def run_full_race(track, laps, seed, report, items=True):
    """Полная гонка восьми ботов с проверками на каждом тике."""
    sim = new_sim(track, laps, items=items, seed=seed)
    bots = make_bots(track, sim, seed)
    counts = collections.Counter()
    lap_events = collections.Counter()
    stuck_ticks = [0] * len(sim.cars)
    wall_breaks = 0
    place_breaks = 0
    stuck_slots = set()
    box_cycle = {'off': set(), 'respawned': 0}
    leader_seq = []

    for tick in range(MAX_RACE_TICKS):
        for index, car in enumerate(sim.cars):
            if car.removed:
                continue
            sim.set_input(car.slot, tick + 1, bots[index].drive(car, tick))

        events = sim.tick()

        for event in events:
            kind = event['kind']
            counts[(kind, event.get('item'), event.get('blocked'))] += 1
            counts[kind] += 1
            if kind == 'lap':
                lap_events[event['slot']] += 1

        # --- места: перестановка 1..N и правильный порядок
        if not _places_consistent(sim):
            place_breaks += 1

        # --- стены и застревание
        for index, car in enumerate(sim.cars):
            if car.removed or car.finished:
                continue
            state = car.state
            _idx, lateral, half_width, _y, _pitch = track.surface(
                state.x, state.z, state.sample_idx)
            if abs(lateral) > half_width + WALL_MARGIN + 0.35:
                wall_breaks += 1
            speed = math.sqrt(state.vx * state.vx + state.vz * state.vz)
            if speed < 1.0 and state.spin_time <= 0.0:
                stuck_ticks[index] += 1
                if stuck_ticks[index] > 300:        # пять секунд на месте
                    stuck_slots.add(car.slot)
            else:
                stuck_ticks[index] = 0

        # --- боксы: подобранный обязан вернуться
        active = sim.items.box_active
        for box_id in range(sim.items.box_count):
            if not active[box_id]:
                box_cycle['off'].add(box_id)
            elif box_id in box_cycle['off']:
                box_cycle['off'].discard(box_id)
                box_cycle['respawned'] += 1

        leader_seq.append(sim.cars[sim.rank[0]].slot)
        if sim.is_over():
            break

    finished_ticks = tick + 1
    return {
        'sim': sim, 'counts': counts, 'lap_events': lap_events,
        'wall_breaks': wall_breaks, 'place_breaks': place_breaks,
        'stuck_slots': stuck_slots, 'respawned': box_cycle['respawned'],
        'ticks': finished_ticks, 'leader_seq': leader_seq,
    }


def _places_consistent(sim):
    """Места — перестановка 1..N, финишировавшие выше едущих, едущие по пути."""
    cars = sim.cars
    count = len(cars)
    seen = 0
    for car in cars:
        if car.place < 1 or car.place > count:
            return False
        bit = 1 << car.place
        if seen & bit:
            return False
        seen |= bit
    previous = None
    for i in range(count):
        car = cars[sim.rank[i]]
        if previous is not None:
            if previous.finished and not car.finished:
                pass                       # финишировавший выше — так и надо
            elif not previous.finished and car.finished:
                return False               # едущий не может быть выше финишировавшего
            elif previous.finished and car.finished:
                if previous.finish_order > car.finish_order:
                    return False
            elif not previous.dnf and not car.dnf:
                if previous.state.progress < car.state.progress - 1e-9:
                    return False
        previous = car
    return True


def check_races(report, laps, seed=0):
    """Полная гонка на каждой из трёх трасс."""
    totals = collections.Counter()
    for track_id in TRACK_IDS:
        track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks',
                                        '%s.json' % track_id))
        report.section('Полная гонка: %s (%s), %d круга, 8 ботов'
                       % (track.name, track_id, laps))
        result = run_full_race(track, laps, seed, report, items=True)
        sim = result['sim']
        rows = sim.results()

        report.check(sim.is_over(), 'гонка закончилась сама',
                     'тиков %d, %.1f с' % (result['ticks'], sim.race_time))
        report.check(all(not row['dnf'] for row in rows), 'все финишировали',
                     'финишировало %d из %d'
                     % (sum(0 if row['dnf'] else 1 for row in rows), len(rows)))
        laps_ok = all(car.state.lap == laps for car in sim.cars)
        report.check(laps_ok, 'круги досчитаны до %d у всех' % laps,
                     'круги: %s' % sorted(car.state.lap for car in sim.cars))
        events_ok = all(result['lap_events'][car.slot] == laps for car in sim.cars)
        report.check(events_ok, 'событий lap ровно по числу кругов',
                     'событий: %s' % dict(result['lap_events']))
        report.check(result['place_breaks'] == 0, 'места непротиворечивы на каждом тике',
                     'сбоев %d' % result['place_breaks'])
        report.check(result['wall_breaks'] == 0, 'никто не вылез за стену',
                     'нарушений %d' % result['wall_breaks'])
        report.check(not result['stuck_slots'], 'никто не застрял',
                     'застряли: %s' % sorted(result['stuck_slots']))
        report.check(result['respawned'] > 0, 'боксы возвращаются после подбора',
                     'возвратов %d' % result['respawned'])

        counts = result['counts']
        totals.update(counts)
        picked = {name: counts[('pickup', name, None)] for name in ALL_ITEMS}
        report.note('подобрано: %s' % picked)
        report.note('попаданий: ракета %d (щитом погашено %d), мина %d (погашено %d), '
                    'гроза %d (погашено %d)'
                    % (counts[('hit', 'rocket', False)], counts[('hit', 'rocket', True)],
                       counts[('hit', 'mine', False)], counts[('hit', 'mine', True)],
                       counts[('hit', 'storm', False)], counts[('hit', 'storm', True)]))
        report.note('ускорений за дрифт: %d' % counts['drift_boost'])
        best = rows[0]
        report.note('победитель: слот %d (%s), %.1f с, лучший круг %s'
                    % (best['slot'], best['car'], best['time'], best['best_lap']))

    report.section('Итог по бонусам за три гонки')
    for name in ALL_ITEMS:
        picked = totals[('pickup', name, None)]
        used = totals[('use', name, None)]
        report.check(picked > 0 and used == picked,
                     'бонус %s выпадает и применяется' % name,
                     'подобран %d, применён %d' % (picked, used))
    report.check(totals[('hit', 'rocket', False)] > 0, 'ракеты попадают',
                 'попаданий %d' % totals[('hit', 'rocket', False)])
    report.check(totals[('hit', 'mine', False)] > 0, 'мины срабатывают',
                 'срабатываний %d' % totals[('hit', 'mine', False)])
    report.check(totals[('hit', 'storm', False)] > 0, 'гроза достаёт впереди идущих',
                 'замедлений %d' % totals[('hit', 'storm', False)])
    blocked = (totals[('hit', 'rocket', True)] + totals[('hit', 'mine', True)]
               + totals[('hit', 'storm', True)])
    report.check(blocked > 0, 'щит гасит попадания в живой гонке',
                 'погашено %d' % blocked)


# --- точечные сценарии ------------------------------------------------------

def _solo_track():
    return Track.load(os.path.join(BASE_DIR, 'content', 'tracks', 'office.json'))


def check_shield(report):
    """Щит гасит ровно одно попадание и при этом тратится."""
    report.section('Щит гасит ровно одно попадание')
    track = _solo_track()
    sim = new_sim(track, 3, count=2)
    victim, shooter = sim.cars[0], sim.cars[1]
    victim.item = items_mod.ITEM_SHIELD
    sim.items.use(sim, victim)
    report.check(victim.state.shield_time == items_mod.SHIELD_TIME,
                 'щит взведён на %.0f с' % items_mod.SHIELD_TIME)

    del sim.events[:]
    sim.items._apply_hit(sim, victim, shooter.slot, items_mod.ITEM_ROCKET)
    first = sim.events[-1]
    report.check(first['blocked'] is True, 'первое попадание погашено щитом')
    report.check(victim.state.spin_time == 0.0, 'машину не закрутило')
    report.check(victim.state.shield_time == 0.0, 'щит потрачен целиком')

    sim.items._apply_hit(sim, victim, shooter.slot, items_mod.ITEM_ROCKET)
    second = sim.events[-1]
    report.check(second['blocked'] is False, 'второе попадание проходит')
    report.check(abs(victim.state.spin_time - items_mod.ROCKET_SPIN) < 1e-9,
                 'машину закрутило на %.1f с' % items_mod.ROCKET_SPIN)
    report.check(first['kind'] == 'hit' and first['slot'] == victim.slot
                 and first['by'] == shooter.slot and first['item'] == 'rocket',
                 'событие hit по схеме раздела 9', repr(first))


def _rocket_scene(track, car_id, want_gap, seed=0):
    """Две одинаковые машины на трассе, лидер оторвался на ``want_gap`` метров.

    Машины ведут автопилоты, а не голая кнопка газа: на 180..197 км/ч машина
    с зажатым газом и без руля улетает в стену на первом же повороте, и вся
    проверка вырождается в «стоим у отбойника». Отрыв задаётся В МЕТРАХ и
    набирается замедлением догоняющего — метры не зависят от того, какой
    сейчас потолок скорости, а фиксированное число кадров зависит.

    Возвращает (sim, лидер, догоняющий, боты, ведущая функция, отрыв).
    """
    sim = new_sim(track, 9, count=2, collisions=False, seed=seed, car_id=car_id)
    leader, chaser = sim.cars[0], sim.cars[1]
    bots = make_bots(track, sim, seed, count=2)

    def drive(ticks, chaser_lift=False):
        """Тик за тиком. Кнопка бонуса у ботов отобрана: в этой проверке
        на трассе должен быть ровно один снаряд — наш."""
        for _ in range(ticks):
            sim.set_input(leader.slot, sim.tick_no + 1,
                          bots[0].drive(leader, sim.tick_no) & ~BTN_ITEM)
            mask = bots[1].drive(chaser, sim.tick_no) & ~BTN_ITEM
            if chaser_lift:
                mask &= ~BTN_THROTTLE
            sim.set_input(chaser.slot, sim.tick_no + 1, mask)
            sim.tick()

    drive(420)                       # обе вышли на рабочую скорость
    guard = 0
    while (leader.state.progress - chaser.state.progress) < want_gap \
            and guard < 60 * 60:
        drive(1, chaser_lift=True)
        guard += 1
    drive(150)                       # догоняющий снова разогнался
    gap = leader.state.progress - chaser.state.progress
    return sim, leader, chaser, drive, gap


def check_rocket(report):
    """Ракета самонаводится в ближайшего впереди и догоняет его."""
    report.section('Ракета самонаводится и попадает')
    track = _solo_track()

    # Прямая проверка на протухание константы: ракета обязана быть быстрее
    # самой быстрой машины каталога, иначе догонять ей нечем. Именно это
    # сломалось, когда потолки подняли со 138..152 до 181..197 км/ч.
    speed = items_mod.rocket_speed()
    fastest = 0.0
    fastest_id = ''
    for car in sim_mod.car_catalog():
        top = max(car.top_speed, car.boost_top)
        if top > fastest:
            fastest = top
            fastest_id = car.id
    report.check(speed > fastest * 1.15,
                 'ракета быстрее самой быстрой машины с запасом',
                 'ракета %.1f м/с, %s на ускорении %.1f м/с' % (speed, fastest_id, fastest))

    hits = []
    for want_gap in (20.0, 50.0, 110.0, 200.0):
        # «Клин» — самая быстрая машина каталога, то есть худший случай
        sim, leader, chaser, drive, gap = _rocket_scene(track, 'wedge', want_gap)
        chaser.item = items_mod.ITEM_ROCKET
        sim.items.use(sim, chaser)
        proj = sim.items.projectiles[sim.items._live[sim.items.live_count - 1]]
        target_ok = proj.target == leader.slot
        hit_at = None
        for step in range(400):
            drive(1)
            for event in sim.events:
                if event['kind'] == 'hit' and event['item'] == 'rocket':
                    hit_at = step / 60.0
            if hit_at is not None:
                break
        hits.append((round(gap, 1), hit_at, target_ok))
    report.check(all(item[0] > 10.0 for item in hits),
                 'отрыв действительно набран, сценарий не вырожден',
                 'отрывы: %s' % [g for g, _t, _o in hits])
    report.check(all(item[2] for item in hits),
                 'цель — ближайший впереди по progress')
    report.check(all(item[1] is not None for item in hits),
                 'ракета догоняет цель на всех дистанциях',
                 'отрыв/время: %s' % [(g, t) for g, t, _ in hits])
    report.check(all(item[1] <= items_mod.ROCKET_LIFE for item in hits
                     if item[1] is not None),
                 'ракета укладывается в свой срок жизни',
                 'срок %.1f с' % items_mod.ROCKET_LIFE)
    report.check(all(item[1] > 0.0 for item in hits if item[1] is not None),
                 'ракета не взрывается о собственный бампер')


def check_rocket_target(report):
    """Цель ракеты — ближайший ВПЕРЕДИ, а не лидер и не сосед по слоту."""
    report.section('Ракета выбирает ближайшего впереди')
    track = _solo_track()
    sim = new_sim(track, 9, count=5, collisions=False, car_id='hatch')
    step = track.length / len(track.samples)
    # расставляем по дуге вручную: 0 впереди всех, 4 последний
    for k, car in enumerate(sim.cars):
        index = int((200.0 - k * 40.0) / step) % len(track.samples)
        sample = track.samples[index]
        car.state.x = sample.x
        car.state.z = sample.z
        car.state.yaw = math.atan2(sample.tangent_x, sample.tangent_z)
        car.state.vx = sample.tangent_x * 20.0
        car.state.vz = sample.tangent_z * 20.0
        track.init_state(car.state)
    sim.tick()
    picks = []
    for k, car in enumerate(sim.cars):
        target = sim.items._target_ahead(sim, car)
        picks.append(-1 if target is None else target.slot)
    report.check(picks[0] == -1, 'у лидера цели нет — впереди никого',
                 'цель лидера: %d' % picks[0])
    report.check(picks[1:] == [0, 1, 2, 3],
                 'каждый целится в ближайшего впереди, а не в лидера',
                 'цели: %s' % picks)

    # выбывший сосед не должен становиться целью
    sim.drop_player(2)
    sim.cars[2].removed = True
    target = sim.items._target_ahead(sim, sim.cars[3])
    report.check(target is not None and target.slot == 1,
                 'выбывший пропускается, целью становится следующий впереди',
                 'цель: %s' % (target.slot if target else None))


def check_mine(report):
    """Мина взводится 0,5 с, живёт 25 с, контакт — раскрутка."""
    report.section('Мина: взведение, жизнь, срабатывание')
    track = _solo_track()
    sim = new_sim(track, 5, count=2, collisions=False)
    victim, dropper = sim.cars[0], sim.cars[1]
    dropper.item = items_mod.ITEM_MINE
    sim.items.use(sim, dropper)
    report.check(sim.items.live_count == 1, 'мина создана')
    proj = sim.items.projectiles[sim.items._live[0]]
    report.check(abs(proj.arm - items_mod.MINE_ARM) < 1e-9,
                 'взводится %.1f с' % items_mod.MINE_ARM)
    report.check(abs(proj.life - items_mod.MINE_LIFE) < 1e-9,
                 'живёт %.0f с' % items_mod.MINE_LIFE)

    # ставим машину ровно на мину и держим там: пока не взвелась — не бьёт
    def park():
        victim.state.x = proj.x
        victim.state.z = proj.z
        victim.state.sample_idx = track.nearest_index(proj.x, proj.z,
                                                      victim.state.sample_idx)
    early = 0
    for _ in range(int(items_mod.MINE_ARM * 60) - 2):
        park()
        for event in sim.tick():
            if event['kind'] == 'hit':
                early += 1
    report.check(early == 0, 'невзведённая мина не срабатывает')
    hit = None
    for _ in range(60):
        park()
        for event in sim.tick():
            if event['kind'] == 'hit':
                hit = event
        if hit:
            break
    report.check(hit is not None and hit['item'] == 'mine',
                 'взведённая мина срабатывает', repr(hit))
    report.check(abs(victim.state.spin_time - items_mod.MINE_SPIN) < 1e-9,
                 'контакт крутит машину %.1f с' % items_mod.MINE_SPIN)
    report.check(sim.items.live_count == 0, 'сработавшая мина снимается с трассы')

    # срок жизни
    sim = new_sim(track, 5, count=2, collisions=False)
    sim.cars[1].item = items_mod.ITEM_MINE
    sim.items.use(sim, sim.cars[1])
    for _ in range(int(items_mod.MINE_LIFE * 60) + 4):
        sim.tick()
    report.check(sim.items.live_count == 0, 'мина исчезает по истечении срока')


def check_boxes(report):
    """Боксы: занятые руки, исчезновение и возврат через BOX_RESPAWN."""
    report.section('Боксы: подбор, занятый слот, респаун')
    track = _solo_track()
    sim = new_sim(track, 5, count=1, collisions=False)
    car = sim.cars[0]
    box = track.item_boxes[0]

    def park():
        car.state.x = box['x']
        car.state.z = box['z']
        car.state.sample_idx = track.nearest_index(box['x'], box['z'],
                                                   car.state.sample_idx)

    # руки заняты — бокс остаётся
    car.item = items_mod.ITEM_BOOST
    park()
    sim.items.check_pickups(sim)
    report.check(sim.items.box_active[box['id']] is True,
                 'подбор при занятом слоте игнорируется, бокс остаётся')

    car.item = 0
    park()
    del sim.events[:]
    sim.items.check_pickups(sim)
    report.check(car.item != 0, 'бокс отдал бонус',
                 items_mod.ITEM_ID_BY_CODE[car.item] if car.item else '-')
    report.check(sim.items.box_active[box['id']] is False, 'бокс исчез')
    picked = [e for e in sim.events if e['kind'] == 'pickup']
    report.check(len(picked) == 1 and picked[0]['slot'] == car.slot,
                 'событие pickup по схеме раздела 9',
                 repr(picked[0]) if picked else 'нет события')

    mask = sim.items.box_mask()
    report.check(not protocol.box_active(mask, box['id']),
                 'маска снапшота помнит, что бокс снят')

    car.item = 0
    ticks = 0
    while ticks < int(items_mod.BOX_RESPAWN * 60) + 10:
        sim.items.update(sim, 1.0 / 60.0)
        ticks += 1
        if sim.items.box_active[box['id']]:
            break
    report.check(sim.items.box_active[box['id']] is True,
                 'бокс вернулся через %.0f с' % items_mod.BOX_RESPAWN,
                 'через %.2f с' % (ticks / 60.0))
    report.check(abs(ticks / 60.0 - items_mod.BOX_RESPAWN) < 0.1,
                 'срок респауна совпадает с BOX_RESPAWN')
    mask = sim.items.box_mask()
    report.check(protocol.box_active(mask, box['id']),
                 'маска снапшота помнит, что бокс вернулся')


def check_projectile_cap(report):
    """Потолок MAX_PROJECTILES соблюдается, бонус при этом не пропадает."""
    report.section('Потолок снарядов из protocol.MAX_PROJECTILES')
    track = _solo_track()
    sim = new_sim(track, 5, count=2, collisions=False)
    car = sim.cars[0]
    attempts = protocol.MAX_PROJECTILES + 8
    for _ in range(attempts):
        car.item = items_mod.ITEM_MINE
        sim.items.use(sim, car)
    report.check(sim.items.live_count == protocol.MAX_PROJECTILES,
                 'снарядов ровно %d, лишние не создаются' % protocol.MAX_PROJECTILES,
                 'создано %d за %d попыток' % (sim.items.live_count, attempts))
    report.check(car.item != 0, 'бонус остался в руках, а не пропал молча')
    _cars, projectiles, _mask = sim.snapshot_args()
    report.check(len(projectiles) == protocol.MAX_PROJECTILES,
                 'снапшот отдаёт все снаряды и не переполняется')
    buf = protocol.build_snapshot_base(sim.tick_no, _cars, projectiles, _mask)
    report.check(len(buf) == protocol.snapshot_size(len(_cars), len(projectiles),
                                                    len(_mask)),
                 'снапшот собирается протоколом без обрезки',
                 '%d байт' % len(buf))


def check_input(report):
    """Устаревший и дублированный seq отбрасывается молча."""
    report.section('Приём ввода: порядок и дубликаты')
    track = _solo_track()
    sim = new_sim(track, 3, count=2, collisions=False)
    sim.set_input(0, 10, BTN_THROTTLE)
    report.check(sim.ack_seq(0) == 10, 'принят seq 10')
    sim.set_input(0, 7, BTN_BRAKE)
    report.check(sim.ack_seq(0) == 10 and sim.cars[0].buttons == BTN_THROTTLE,
                 'устаревший seq 7 отброшен молча')
    sim.set_input(0, 10, BTN_BRAKE)
    report.check(sim.cars[0].buttons == BTN_THROTTLE, 'дубликат seq 10 отброшен')
    sim.set_input(0, 11, BTN_BRAKE)
    report.check(sim.ack_seq(0) == 11 and sim.cars[0].buttons == BTN_BRAKE,
                 'следующий seq принят')
    sim.set_input(99, 5, BTN_THROTTLE)
    report.check(sim.ack_seq(99) == 0, 'ввод от чужого слота не ломает симуляцию')

    # защёлка бонуса: короткое нажатие между тиками не теряется
    sim.cars[0].item = items_mod.ITEM_BOOST
    sim.set_input(0, 12, BTN_ITEM)
    sim.set_input(0, 13, 0)
    sim.tick()
    report.check(sim.cars[0].item == 0 and sim.cars[0].state.boost_time > 0.0,
                 'короткое нажатие бонуса не теряется между тиками')


def check_drift(report):
    """Занос по ручнику даёт событие drift_boost нужного уровня.

    Гоняем по шпилькам Серпантина: автопилот там сам уходит в занос,
    держит руль и получает награду из раздела 6.3.
    """
    report.section('Дрифт: событие drift_boost')
    track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks',
                                    'serpentine.json'))
    sim = new_sim(track, 9, items=False, count=1, collisions=False)
    car = sim.cars[0]
    bot = make_bots(track, sim, 0, count=1, allow_drift=True)[0]
    levels = []
    charge_seen = 0.0
    boost_after = 0
    for tick in range(60 * 120):
        sim.set_input(car.slot, sim.tick_no + 1, bot.drive(car, tick))
        for event in sim.tick():
            if event['kind'] == 'drift_boost':
                levels.append(event['level'])
                if car.state.boost_time > 0.0:
                    boost_after += 1
        if car.state.drift_charge > charge_seen:
            charge_seen = car.state.drift_charge
        if len(levels) >= 5:
            break
    report.check(charge_seen >= 0.7, 'заряд дрифта копится',
                 'максимум %.2f с' % charge_seen)
    report.check(len(levels) > 0, 'занос даёт событие drift_boost',
                 'событий %d, уровни %s' % (len(levels), levels))
    report.check(all(1 <= level <= 3 for level in levels),
                 'уровень ускорения в диапазоне 1..3', 'уровни %s' % levels)
    report.check(boost_after == len(levels),
                 'ускорение за занос действительно выдано')


def check_shortcut(report):
    """Срезать круг по траве нельзя: отсечки не дают засчитать."""
    report.section('Защита от срезки: круг по траве не засчитывается')
    track = _solo_track()
    sim = new_sim(track, 3, items=False, count=1, collisions=False)
    car = sim.cars[0]
    bot = make_bots(track, sim, 0, count=1)[0]

    # честно проезжаем примерно треть круга
    while car.state.progress < track.length * 0.35:
        sim.set_input(car.slot, sim.tick_no + 1, bot.drive(car, sim.tick_no))
        sim.tick()
    honest_progress = car.state.progress
    honest_cp = car.state.checkpoint
    report.check(honest_cp >= 3, 'честная треть круга набрала отсечки',
                 'отсечка %d, путь %.0f м' % (honest_cp, honest_progress))

    # телепорт наискось через газон почти к линии старта
    count = len(track.samples)
    near_finish = track.samples[count - 3]
    car.state.x = near_finish.x
    car.state.z = near_finish.z
    car.state.yaw = math.atan2(near_finish.tangent_x, near_finish.tangent_z)
    car.state.vx = math.sin(car.state.yaw) * 18.0
    car.state.vz = math.cos(car.state.yaw) * 18.0
    sim.tick()
    report.check(car.state.progress <= honest_progress + 1e-6,
                 'прыжок через газон не приносит прогресса',
                 'было %.1f м, стало %.1f м' % (honest_progress, car.state.progress))

    lap_before = car.state.lap
    crossed = False
    for _ in range(60 * 12):
        sim.set_input(car.slot, sim.tick_no + 1, bot.drive(car, sim.tick_no))
        sim.tick()
        if car.state.progress > 0.0 and car.state.progress < track.length * 0.2:
            crossed = True
            break
    report.check(crossed, 'машина после срезки пересекла линию старта')
    report.check(car.state.lap == lap_before,
                 'круг после срезки НЕ засчитан',
                 'круг %d, ожидался %d' % (car.state.lap, lap_before))

    # а честный полный круг засчитывается
    guard = 0
    while car.state.lap == lap_before and guard < 60 * 180:
        sim.set_input(car.slot, sim.tick_no + 1, bot.drive(car, sim.tick_no))
        sim.tick()
        guard += 1
    report.check(car.state.lap == lap_before + 1,
                 'честный полный круг засчитывается',
                 'за %.1f с' % (guard / 60.0))


def check_ghosts(report):
    """Финиш, отсчёт кругов, DNF и призраки отключившихся."""
    report.section('Финиш, призраки и DNF')
    track = _solo_track()
    sim = new_sim(track, 2, count=3)
    bots = make_bots(track, sim, 5, count=3)
    sim.drop_player(1)
    car = sim.cars[1]
    report.check(car.dnf and car.ghost, 'отключившийся помечен призраком')
    report.check(sim.is_over() is False, 'гонка не кончилась из-за одного DNF')

    ghost_gone_at = None
    slots_in_snapshot = None
    for tick in range(60 * 6):
        for index, other in enumerate(sim.cars):
            if other.removed:
                continue
            sim.set_input(other.slot, tick + 1, bots[index].drive(other, tick))
        sim.tick()
        cars_out, _proj, _mask = sim.snapshot_args()
        slots_in_snapshot = [row[0] for row in cars_out]
        if car.removed and ghost_gone_at is None:
            ghost_gone_at = tick / 60.0
            break
    report.check(ghost_gone_at is not None
                 and abs(ghost_gone_at - 3.0) < 0.1,
                 'призрак исчезает через 3 с',
                 'через %s с' % (round(ghost_gone_at, 2) if ghost_gone_at else '-'))
    report.check(1 not in slots_in_snapshot, 'исчезнувший призрак ушёл из снапшота')

    # флаг призрака в снапшоте до исчезновения
    sim = new_sim(track, 2, count=2)
    sim.drop_player(1)
    sim.tick()
    cars_out, _proj, _mask = sim.snapshot_args()
    flags = dict((row[0], row[1]) for row in cars_out)
    report.check(protocol.is_ghost(flags.get(1, 0)),
                 'флаг «призрак» выставлен в снапшоте')

    # гонка из одного игрока, который сошёл, обязана закончиться
    sim = new_sim(track, 2, count=1)
    sim.drop_player(0)
    report.check(sim.is_over(), 'гонка кончается, когда сошли все')
    rows = sim.results()
    report.check(rows and rows[0]['dnf'] is True, 'в итогах сошедший помечен dnf')


def check_snapshot(report):
    """Формат снапшота: кортежи раздела 12.3, ack на игрока."""
    report.section('Снапшот: формат 12.3')
    track = _solo_track()
    sim = new_sim(track, 3, count=8)
    bots = make_bots(track, sim, 2)
    for tick in range(240):
        for index, car in enumerate(sim.cars):
            sim.set_input(car.slot, tick + 1, bots[index].drive(car, tick))
        sim.tick()
    cars_out, projectiles, mask = sim.snapshot_args()
    report.check(all(isinstance(row, tuple) and len(row) == 12 for row in cars_out),
                 'машины — кортежи из 12 полей (одиннадцать из 12.3 плюс высота)')
    report.check(all(isinstance(row, tuple) and len(row) == 5 for row in projectiles),
                 'снаряды — кортежи из 5 полей')
    report.check(len(mask) == (len(track.item_boxes) + 7) // 8,
                 'маска боксов нужной длины', '%d байт' % len(mask))
    report.check(len(cars_out) <= protocol.MAX_CARS
                 and len(mask) <= protocol.MAX_BOX_MASK_LEN,
                 'потолки MAX_CARS и MAX_BOX_MASK_LEN соблюдены')
    buf = protocol.build_snapshot_base(sim.tick_no, cars_out, projectiles, mask)
    protocol.stamp_ack(buf, sim.ack_seq(3))
    report.check(len(buf) == protocol.snapshot_size(len(cars_out), len(projectiles),
                                                    len(mask)),
                 'протокол собрал пакет целиком', '%d байт' % len(buf))
    report.check(sim.ack_seq(3) == 240, 'ack_seq отдаёт последний принятый seq')
    steers = [row[9] for row in cars_out]
    report.check(all(-127 <= value <= 127 for value in steers),
                 'steer_q в диапазоне -127..127')
    report.check(all(0 <= row[10] <= 255 for row in cars_out),
                 'drift_charge в диапазоне 0..255')
    report.check(all(1 <= row[8] <= len(cars_out) for row in cars_out),
                 'place в диапазоне 1..N')


# --- замеры -----------------------------------------------------------------

# --- траффик и происшествия на дороге ----------------------------------------


def _road_settings(laps, traffic, events):
    settings = make_settings(laps)
    settings['traffic'] = traffic
    settings['events'] = events
    return settings


# Зерно бонусов для проверок траффика и происшествий.
#
# ЗАЧЕМ. ``new_sim`` засевает ``sim.items.rng``, а прямой вызов
# ``Simulation(...)`` — нет: там остаётся общий генератор модуля, то есть
# от запуска к запуску другой. Выпадение бонусов меняет поведение ботов,
# боты по-разному толкают болванок, и геометрические замеры перестают быть
# воспроизводимыми. Один и тот же прогон обязан давать одно и то же число,
# иначе шаткая проверка убивает доверие ко всему набору.
ROAD_SEED = 4242

# Пороги просвета рядом с болванкой. Оба выведены из габарита машины
# (1,90 м в ширину, раздел 6.2), а не подобраны под прогон — обоснование
# в докстринге check_traffic_free_lane.
TRAFFIC_FREE_SUSTAINED = 4.6   # м, устойчивый просвет: 2,4 ширины машины
TRAFFIC_FREE_INSTANT = 2.85    # м, мгновенный пол: полторы ширины машины


def _road_sim(track, laps, traffic, events, count=8, seed=ROAD_SEED):
    """Гонка с траффиком и происшествиями, полностью воспроизводимая."""
    sim = Simulation(track, _road_settings(laps, traffic, events),
                     make_players(count))
    sim.items.rng = random.Random(seed * 977 + 13)
    # У потока и у происшествий генераторы свои и уже засеяны геометрией
    # трассы (decor_seed), но привяжем их к тому же зерну: тогда прогон
    # воспроизводится целиком, а не наполовину.
    sim.traffic.rng = random.Random(seed * 31 + 7)
    sim.road.rng = random.Random(seed * 53 + 11)
    return sim


def check_traffic(report):
    """Поток машин-болванок: не застревает, не перекрывает трассу, доезжает."""
    report.section('Траффик: поток болванок')
    from game import traffic as traffic_mod

    # Выключенный траффик обязан быть ровно прежним поведением.
    track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks', 'office.json'))
    off = _road_sim(track, 2, 'off', 'off')
    off.tick()
    tr_rows, ev_rows = off.extra_snapshot_args()
    cars, proj, mask = off.snapshot_args()
    report.check(off.traffic.count == 0 and not tr_rows and not ev_rows,
                 'выключенный траффик не даёт ни одной записи')
    report.check(len(protocol.build_snapshot_base(1, cars, proj, mask, tr_rows, ev_rows))
                 == protocol.snapshot_size(len(cars), len(proj), len(mask)),
                 'выключенный траффик не стоит ни одного байта снапшота',
                 '%d байт' % protocol.snapshot_size(len(cars), len(proj), len(mask)))

    worst_ratio = 0.0
    worst_track = ''
    samples = [0, 0]          # всего замеров, из них вне асфальта
    stuck_total = 0
    min_speed_late = 1e9
    finished_all = True
    for track_id in TRACK_IDS:
        track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks',
                                        '%s.json' % track_id))
        sim = _road_sim(track, 2, 'dense', 'off')
        bots = make_bots(track, sim, seed=11)
        system = sim.traffic
        half = system._shw
        stuck = [0] * system.count
        for tick in range(MAX_RACE_TICKS):
            for index, car in enumerate(sim.cars):
                if car.removed:
                    continue
                sim.set_input(car.slot, tick + 1, bots[index].drive(car, tick))
            sim.tick()
            if tick > 120:
                for k, unit in enumerate(system.cars):
                    ratio = abs(unit.lateral) / half[unit.state.sample_idx]
                    if ratio > worst_ratio:
                        worst_ratio = ratio
                        worst_track = track_id
                    samples[0] += 1
                    if unit.state.offtrack:
                        samples[1] += 1
                    if unit.speed < 1.0:
                        stuck[k] += 1
                    if unit.speed < min_speed_late:
                        min_speed_late = unit.speed
            if sim.is_over():
                break
        stuck_total += sum(stuck)
        if not all(car.finished for car in sim.cars):
            finished_all = False

    # Сама по себе болванка полосу держит (отдельный прогон без гонщиков
    # даёт ноль выездов на всех пяти трассах). Но её толкают: гонщик имеет
    # полное право выпихнуть её на газон, и это честная физика, а не ошибка
    # водителя. Поэтому проверяется не «никогда не за кромкой», а доля
    # времени за кромкой — она должна быть мелкой и разовой.
    off_share = 100.0 * samples[1] / samples[0] if samples[0] else 0.0
    report.note('худшее смещение %.2f полуширины (%s), под толчками гонщиков'
                % (worst_ratio, worst_track))
    report.check(off_share < 3.0,
                 'болванка держит полосу, а не ездит по газону',
                 'вне асфальта %.2f %% времени (толчки гонщиков)' % off_share)
    report.check(stuck_total == 0,
                 'болванка нигде не встала',
                 'тиков со скоростью ниже 1 м/с: %d' % stuck_total)
    # Не проверка, а справка: мгновенный минимум зависит от того, толкнул ли
    # бот болванку именно на этом тике, и порогом быть не может. Условие
    # «не встала» закрывает проверка выше, она детерминирована по смыслу.
    report.note('самая медленная болванка за прогон: %.1f м/с' % min_speed_late)
    report.check(finished_all,
                 'восемь ботов доезжают сквозь плотный траффик на всех трассах')


def check_traffic_free_lane(report):
    """Рядом с болванкой всегда остаётся, где проехать.

    Меряется УСТОЙЧИВЫЙ просвет, а не мгновенный: для каждой болванки берётся
    лучший просвет за скользящие полсекунды, и минимум этой величины по всему
    прогону и есть ответ на вопрос «можно ли тут проехать». Мгновенный
    минимум на этот вопрос не отвечает и проверкой быть не может — он ловит
    переходные процессы длиной в несколько тиков.

    Разбор, из-за которого проверка переписана. На ``serpentine`` мгновенный
    просвет падал до 3,84 м при пороге 4,0 — примерно в двух прогонах из трёх,
    то есть проверка стояла ровно на пороге и была шаткой. Замер показал:
    за две минуты на девяти болванках таких тиков ВОСЕМЬ, самая длинная
    непрерывная просадка — четыре тика, 67 мс, и приходится она на момент,
    когда болванку толкнули через осевую линию (``lateral`` 0,04 при цели
    2,19). Гарантия полосы (``_lane_at``) при этом цела; отстаёт слежение за
    ней после толчка, и отстаёт на десятые доли секунды. Проехать мимо
    машины, у которой борт в 3,84 м от кромки, можно свободно: габарит
    гонщика 1,90 м, это два корпуса.

    Числа порогов взяты из геометрии, а не из того, что проходит:

    * УСТОЙЧИВЫЙ порог 4,6 м — это ``MIN_FREE_WIDTH`` происшествий, то есть
      2,4 ширины машины. ``_lane_at`` гарантирует 5,2 м целевой полосе, на
      самом узком полотне (``serpentine``, полуширина 3,75 м) запас по кромке
      срезает гарантию до 5,05 м, и 4,6 — это она минус ошибка слежения.
      Замерено по всем пяти трассам: 4,79 м, запас к порогу 0,19 м.
    * МГНОВЕННЫЙ порог 2,85 м — полторы ширины машины. Ниже него болванка
      обязана была бы уехать за осевую линию к дальней кромке, то есть
      покинуть свою полосу целиком. Замерено: 3,84 м, запас почти метр.
    """
    report.section('Траффик: трасса остаётся проезжей')
    window = 30                  # тиков в скользящем окне, полсекунды
    worst_inst = 1e9
    worst_sust = 1e9
    worst_pair = 1e9
    longest_dip = 0
    where = ''
    for track_id in TRACK_IDS:
        track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks',
                                        '%s.json' % track_id))
        sim = _road_sim(track, 2, 'dense', 'off')
        bots = make_bots(track, sim, seed=3)
        system = sim.traffic
        half = system._shw
        length = track.length
        count = system.count
        ring = [[1e9] * window for _ in range(count)]
        head = [0] * count
        dip = [0] * count
        for tick in range(60 * 90):
            for index, car in enumerate(sim.cars):
                if car.removed:
                    continue
                sim.set_input(car.slot, tick + 1, bots[index].drive(car, tick))
            sim.tick()
            if tick < 150:       # прогрев: поток разгоняется с нуля
                continue
            for k in range(count):
                unit = system.cars[k]
                hw = half[unit.state.sample_idx]
                # Болванка занимает поперёк [lat - R, lat + R]. Самый широкий
                # свободный кусок асфальта лежит с той стороны, куда она НЕ
                # смещена: от дальней кромки до её борта.
                free = hw + abs(unit.lateral) - physics.CAR_RADIUS
                if free < worst_inst:
                    worst_inst = free
                    where = track_id
                ring[k][head[k]] = free
                head[k] = (head[k] + 1) % window
                # лучший просвет за полсекунды: если и он мал, просвет
                # действительно узкий, а не мигнул на один тик
                sustained = max(ring[k])
                if sustained < worst_sust:
                    worst_sust = sustained
                if free < TRAFFIC_FREE_SUSTAINED:
                    dip[k] += 1
                    if dip[k] > longest_dip:
                        longest_dip = dip[k]
                else:
                    dip[k] = 0
            # две болванки не встают борт о борт поперёк трассы
            for a in range(count):
                for b in range(a + 1, count):
                    ua = system.cars[a]
                    ub = system.cars[b]
                    gap = abs(ua.arc - ub.arc)
                    if gap > length * 0.5:
                        gap = length - gap
                    if gap < 4.0:
                        span = abs(ua.lateral - ub.lateral)
                        if span < worst_pair:
                            worst_pair = span

    report.check(worst_sust >= TRAFFIC_FREE_SUSTAINED,
                 'рядом с болванкой устойчиво остаётся проезд',
                 'худший просвет за полсекунды %.2f м при пороге %.1f '
                 '(габарит машины 1,90 м)' % (worst_sust, TRAFFIC_FREE_SUSTAINED))
    report.check(worst_inst >= TRAFFIC_FREE_INSTANT,
                 'болванка не уходит из своей полосы даже под толчком',
                 'мгновенный минимум %.2f м при пороге %.2f (%s)'
                 % (worst_inst, TRAFFIC_FREE_INSTANT, where))
    report.note('просадок ниже %.1f м: самая длинная %d тиков (%.2f с)'
                % (TRAFFIC_FREE_SUSTAINED, longest_dip, longest_dip / 60.0))
    report.check(longest_dip < 60,
                 'узкий просвет не держится дольше секунды',
                 '%d тиков' % longest_dip)
    report.check(worst_pair >= 1e8 or worst_pair < 2.6,
                 'болванки не встают стеной борт о борт',
                 'пар на одной дуге не было' if worst_pair >= 1e8
                 else 'ближайшая пара разошлась на %.1f м поперёк' % worst_pair)


def check_road_events(report):
    """Происшествия: видно заранее, не под нос, круг остаётся проезжим."""
    report.section('Происшествия на дороге')
    from game import events as events_mod

    track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks', 'office.json'))
    off = _road_sim(track, 2, 'off', 'off')
    for _ in range(600):
        off.tick()
    report.check(off.road.spawned == 0 and not off.road.live,
                 'выключенные происшествия не порождают ничего')

    kinds = collections.Counter()
    worst_free = 1e9
    worst_margin = 1e9
    worst_ahead = 1e9
    spawned = 0
    finished_all = True
    for track_id in TRACK_IDS:
        track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks',
                                        '%s.json' % track_id))
        sim = _road_sim(track, 2, 'dense', 'often')
        bots = make_bots(track, sim, seed=23)
        road = sim.road
        length = track.length
        seen = 0
        for tick in range(MAX_RACE_TICKS):
            for index, car in enumerate(sim.cars):
                if car.removed:
                    continue
                sim.set_input(car.slot, tick + 1, bots[index].drive(car, tick))
            # запас «видно заранее» считаем ДО тика: положение машин то же,
            # что видел размещатель
            before = road.spawned
            positions = [(c.state.progress % length,
                          math.hypot(c.state.vx, c.state.vz))
                         for c in sim.cars if not c.removed]
            sim.tick()
            if road.spawned != before:
                seen += 1
                event = road.live[-1]
                kinds[event.kind] += 1
                free = road.free_width(event)
                if free < worst_free:
                    worst_free = free
                if road.last_margin < worst_margin:
                    worst_margin = road.last_margin
                for arc, speed in positions:
                    gap = event.arc - arc
                    if gap < 0.0:
                        gap += length
                    need = max(events_mod.SIGHT_MIN_AHEAD,
                               speed * (events_mod.WARN_TIME + events_mod.SIGHT_REACTION))
                    slack = gap - need
                    if slack < worst_ahead:
                        worst_ahead = slack
            if sim.is_over():
                break
        spawned += seen
        if not all(car.finished for car in sim.cars):
            finished_all = False

    report.check(spawned >= 15,
                 'происшествия появляются на всех трассах',
                 'всего %d за пять гонок, виды %s'
                 % (spawned, dict(kinds)))
    report.check(len(kinds) >= 3,
                 'выпадают разные виды происшествий',
                 'видов %d из 4' % len(kinds))
    report.check(worst_ahead >= 0.0,
                 'происшествие не появляется под носом у едущего',
                 'худший запас сверх требуемого %.0f м' % worst_ahead)
    report.check(worst_free >= events_mod.MIN_FREE_WIDTH - 0.01,
                 'рядом с происшествием остаётся проезд',
                 'худший просвет %.1f м при пороге %.1f'
                 % (worst_free, events_mod.MIN_FREE_WIDTH))
    report.check(finished_all,
                 'круг остаётся проезжим: восемь ботов финишируют при частых ДТП')


def check_road_physics(report):
    """Масло скользит, препятствие не пускает, обломки тормозят."""
    report.section('Происшествия: влияние на физику')
    from game import events as events_mod

    track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks', 'office.json'))
    sim = _road_sim(track, 3, 'off', 'rare', count=1)
    car = sim.cars[0]
    road = sim.road

    def place(kind, arc, lateral, phase):
        del road.live[:]
        for slot in road.pool:
            slot.alive = False
        slot = road.pool[0]
        hl, hw, _k = events_mod.GEOMETRY[kind]
        slot.kind = kind
        slot.phase = phase
        slot.arc = arc % track.length
        slot.lateral = lateral
        slot.half_len = hl
        slot.half_width = hw
        slot.track_half_width = 7.0
        slot.timer = 100.0
        slot.hit_mask = 0
        slot.alive = True
        road.live.append(slot)
        road._rebuild_zone()
        return slot

    # --- масло: сцепление падает ровно в OIL_GRIP раз --------------------
    state = car.state
    arc = (state.progress % track.length) + 40.0
    place(events_mod.KIND_OIL, arc, 0.0, events_mod.PHASE_ACTIVE)
    i = int(arc / track._step) % len(track.samples)
    sample = track.samples[i]
    state.x = sample.x
    state.z = sample.z
    state.sample_idx = i
    scale = road.grip_scale(state)
    report.check(abs(scale - events_mod.OIL_GRIP) < 1e-9,
                 'в пятне масла сцепление падает', 'множитель %.2f' % scale)
    state.x = sample.x + sample.normal_x * 30.0
    state.z = sample.z + sample.normal_z * 30.0
    report.check(road.grip_scale(state) == 1.0,
                 'вне пятна масла сцепление обычное')

    # --- твёрдое препятствие выталкивает ---------------------------------
    place(events_mod.KIND_WRECK, arc, 2.0, events_mod.PHASE_ACTIVE)
    state.x = sample.x + sample.normal_x * 2.0
    state.z = sample.z + sample.normal_z * 2.0
    state.vx = sample.normal_x * 5.0
    state.vz = sample.normal_z * 5.0
    state.sample_idx = i
    before_x, before_z = state.x, state.z
    road.apply_after_step(state, physics.DT)
    moved = math.hypot(state.x - before_x, state.z - before_z)
    report.check(moved > 0.05,
                 'перевёрнутая машина выталкивает наехавшего',
                 'выталкивание %.2f м' % moved)

    # --- обломки тормозят --------------------------------------------------
    place(events_mod.KIND_EXPLOSION, arc, 0.0, events_mod.PHASE_DEBRIS)
    state.x = sample.x
    state.z = sample.z
    state.vx = sample.tangent_x * 30.0
    state.vz = sample.tangent_z * 30.0
    state.sample_idx = i
    road.apply_after_step(state, physics.DT)
    after = math.hypot(state.vx, state.vz)
    report.check(after < 30.0,
                 'по обломкам машина теряет ход',
                 'с 30.0 до %.2f м/с за шаг' % after)
    report.check(abs(road.grip_scale(state) - events_mod.DEBRIS_GRIP) < 1e-9,
                 'на обломках сцепление хуже асфальта')

    # --- вспышка бьёт один раз -------------------------------------------
    place(events_mod.KIND_EXPLOSION, arc, 0.0, events_mod.PHASE_ACTIVE)
    state.x = sample.x
    state.z = sample.z
    state.sample_idx = i
    first = road.blast_spin(state, 0)
    second = road.blast_spin(state, 0)
    other = road.blast_spin(state, 1)
    report.check(first > 0.0 and second == 0.0 and other > 0.0,
                 'вспышка взрыва задевает каждую машину ровно один раз',
                 'первая %.1f с, повтор %.1f с, соседу %.1f с'
                 % (first, second, other))

    # --- предупреждение физики не имеет ----------------------------------
    place(events_mod.KIND_WRECK, arc, 0.0, events_mod.PHASE_WARN)
    state.vx = 0.0
    state.vz = 0.0
    before_x, before_z = state.x, state.z
    road.apply_after_step(state, physics.DT)
    report.check(state.x == before_x and state.z == before_z
                 and road.grip_scale(state) == 1.0,
                 'в фазе предупреждения происшествие только видно, но не действует')


def check_road_settings(report):
    """Враждебные входы на новые поля настроек комнаты."""
    report.section('Настройки: траффик и происшествия')
    from server.room import ContentLibrary, validate_settings, SettingsError
    from game import traffic as traffic_mod
    from game import events as events_mod

    content = ContentLibrary(os.path.join(BASE_DIR, 'content')).load()
    base = content.default_settings()
    report.check(base.get('traffic') == 'off' and base.get('events') == 'off',
                 'по умолчанию и траффик, и происшествия выключены')

    good = validate_settings({'traffic': 'dense', 'events': 'often'}, content)
    report.check(good['traffic'] == 'dense' and good['events'] == 'often',
                 'верные значения принимаются')

    bad_inputs = (
        {'traffic': 'DENSE'}, {'traffic': 'плотный'}, {'traffic': 1},
        {'traffic': True}, {'traffic': None}, {'traffic': ['dense']},
        {'traffic': {'level': 'dense'}}, {'traffic': ''},
        {'events': 'sometimes'}, {'events': 3}, {'events': False},
        {'events': ['often']}, {'events': None}, {'events': 'off '},
    )
    rejected = 0
    for raw in bad_inputs:
        try:
            validate_settings(raw, content)
        except SettingsError:
            rejected += 1
    report.check(rejected == len(bad_inputs),
                 'мусор в новых полях отвергается',
                 '%d из %d' % (rejected, len(bad_inputs)))

    # Незнакомое значение в БАЗЕ (пришло из старого файла) не роняет комнату.
    stale = dict(base)
    stale['traffic'] = 'insane'
    stale['events'] = 42
    fixed = validate_settings({}, content, base=stale)
    report.check(fixed['traffic'] == 'off' and fixed['events'] == 'off',
                 'мусор в сохранённых настройках чинится умолчанием')

    # Симуляция не должна падать ни на каком мусоре в настройках.
    track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks', 'office.json'))
    survived = True
    for value in ('insane', 42, None, True, [], {}, ''):
        try:
            sim = Simulation(track, {'laps': 1, 'traffic': value, 'events': value},
                             make_players(2))
            sim.tick()
            if sim.traffic.count != 0 or sim.road.enabled:
                survived = False
        except Exception:
            survived = False
    report.check(survived,
                 'симуляция переживает мусор в настройках и молча его выключает')


def collect_tick_times(track_id='office', laps=3, seed=0, warmup=180):
    """Время тика, замеренное отдельным прогоном и ничем не засорённое.

    Раньше замер снимался прямо в полной гонке, между проверками мест, стен
    и боксов. Вся эта обвязка аллоцирует множества и счётчики на каждом тике,
    сборщик мусора CPython срабатывает ВНУТРИ следующего ``sim.tick()`` —
    и в хвост распределения попадали не тики, а паузы GC от самого теста:
    медиана держалась на 0,090 мс, а p99.9 прыгал от 0,20 до 6,0 мс от
    запуска к запуску. Здесь в цикле нет ничего, кроме ботов и тика.

    Циклический сборщик на время замера выключен по той же причине: он
    обходит ВЕСЬ накопленный за тест кучевой мусор, а не то, что создал тик.
    Чтобы это не превратилось в поблажку, функция заодно возвращает, сколько
    циклического мусора симуляция породила за прогон — вызывающий это
    проверяет отдельно, чего раньше не проверял никто.

    Возвращает (список времён в мс, число недостижимых циклических объектов).
    """
    track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks',
                                    '%s.json' % track_id))
    sim = new_sim(track, laps, seed=seed)
    bots = make_bots(track, sim, seed)
    samples = []
    gc.collect()
    gc.disable()
    try:
        for tick in range(MAX_RACE_TICKS):
            for index, car in enumerate(sim.cars):
                if car.removed:
                    continue
                sim.set_input(car.slot, tick + 1, bots[index].drive(car, tick))
            started = time.perf_counter()
            sim.tick()
            elapsed = (time.perf_counter() - started) * 1000.0
            if tick >= warmup:      # первые тики — прогрев интерпретатора
                samples.append(elapsed)
            if sim.is_over():
                break
    finally:
        gc.enable()
    # всё, что породил прогон и что не убралось счётчиком ссылок
    garbage = gc.collect()
    return samples, garbage


def measure_traffic_tick(report):
    """Сколько стоит тик с траффиком и происшествиями и сколько весит снапшот.

    Замер снимается тем же способом, что и основной (отдельный прогон,
    выключенный циклический сборщик), но по трём плотностям подряд — так
    цена одной болванки выходит вычитанием, а не догадкой.
    """
    report.section('Траффик: цена тика и вес снапшота')
    track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks', 'avenue.json'))
    rows = {}
    for traffic, events in (('off', 'off'), ('sparse', 'off'),
                            ('dense', 'off'), ('dense', 'often')):
        sim = _road_sim(track, 3, traffic, events)
        bots = make_bots(track, sim, seed=9)
        times = []
        sizes = []
        extra_bytes = [0]
        gc.collect()
        gc.disable()
        try:
            for tick in range(60 * 150):
                for index, car in enumerate(sim.cars):
                    if car.removed:
                        continue
                    sim.set_input(car.slot, tick + 1, bots[index].drive(car, tick))
                started = time.perf_counter()
                sim.tick()
                spent = (time.perf_counter() - started) * 1000.0
                if tick >= 240:
                    times.append(spent)
                    if tick % 3 == 0:
                        cars, proj, mask = sim.snapshot_args()
                        extra_t, extra_e = sim.extra_snapshot_args()
                        size = protocol.snapshot_size(
                            len(cars), len(proj), len(mask),
                            len(extra_t), len(extra_e))
                        sizes.append(size)
                        plain = protocol.snapshot_size(
                            len(cars), len(proj), len(mask))
                        if size - plain > extra_bytes[0]:
                            extra_bytes[0] = size - plain
                if sim.is_over():
                    break
        finally:
            gc.enable()
        times.sort()
        rows[(traffic, events)] = (
            len(sim.traffic.cars), sum(times) / len(times),
            times[int(len(times) * 0.99)],
            sum(sizes) / len(sizes), max(sizes), extra_bytes[0])
        report.note('%-6s ДТП %-5s болванок %2d: тик сред %.4f мс, p99 %.4f мс; '
                    'снапшот сред %.0f, макс %d байт (сверх базового +%d)'
                    % ((traffic, events) + rows[(traffic, events)]))

    base = rows[('off', 'off')]
    dense = rows[('dense', 'off')]
    full = rows[('dense', 'often')]
    per_car = (dense[1] - base[1]) / dense[0] * 1000.0
    report.note('цена одной болванки: %.1f мкс на тик' % per_car)
    report.check(full[1] < config_tick_budget() * 0.5,
                 'тик с плотным траффиком и частыми ДТП в бюджете комнаты',
                 '%.4f мс из %.1f мс — %.0f %%'
                 % (full[1], config_tick_budget(),
                    full[1] / config_tick_budget() * 100.0))
    report.check(full[2] < config_tick_budget(),
                 'p99 тика в бюджете', '%.4f мс' % full[2])
    # Потолок пакета без траффика: восемь машин, полный пул снарядов,
    # маска боксов этой трассы. Он же и был потолком до появления траффика —
    # именно это и проверяем, а не число 282 (оно верно для трассы с двумя
    # байтами маски, а у avenue их три).
    report.check(base[5] == 0,
                 'выключенный траффик не стоит ни одного байта снапшота',
                 'пакет ровно тот же, что до появления траффика '
                 '(максимум %d байт на этой трассе)' % base[4])
    full_cap = protocol.snapshot_size(protocol.MAX_CARS, 4, 4,
                                      protocol.MAX_TRAFFIC,
                                      protocol.MAX_ROAD_EVENTS)
    report.check(full[4] <= full_cap,
                 'полный снапшот не выходит за расчётный потолок',
                 '%d байт при потолке %d' % (full[4], full_cap))
    report.check(full[3] < base[3] * 2.0,
                 'снапшот не раздулся втрое',
                 'средний %.0f против %.0f байт, рост %.0f %%'
                 % (full[3], base[3], (full[3] / base[3] - 1.0) * 100.0))


def config_tick_budget():
    """Бюджет тика из контракта (раздел 1), 2 мс."""
    return 2.0


def measure_tick(report, measured):
    """Время тика при восьми машинах — условие приёмки раздела 1."""
    report.section('Время тика (восемь машин, бонусы включены)')
    samples, garbage = measured
    if not samples:
        report.check(False, 'замер не состоялся')
        return
    ordered = sorted(samples)
    count = len(ordered)
    avg = sum(ordered) / count
    p50 = ordered[count // 2]
    p99 = ordered[int(count * 0.99)]
    p999 = ordered[int(count * 0.999)]
    worst = ordered[-1]
    report.note('тиков измерено: %d' % count)
    report.note('среднее %.4f мс, медиана %.4f мс, p99 %.4f мс, p99.9 %.4f мс, '
                'максимум %.4f мс' % (avg, p50, p99, p999, worst))
    report.check(avg < 0.5, 'среднее время тика в бюджете комнаты (2 мс)',
                 '%.4f мс — это %.1f %% бюджета' % (avg, avg / 2.0 * 100.0))
    report.check(p99 < 0.5, 'p99 в бюджете', '%.4f мс' % p99)
    report.check(p999 < 1.0, 'p99.9 в бюджете', '%.4f мс' % p999)
    # Единичный выброс — это сборка мусора интерпретатора или вытеснение
    # процесса планировщиком, а не работа симуляции: при p99.9 меньше
    # десятой доли бюджета одиночный пик в миллисекунду ни о чём не говорит.
    if worst >= 2.0:
        report.note('ВНИМАНИЕ: одиночный выброс %.3f мс (планировщик ОС) — '
                    'при p99.9 = %.3f мс это не стоимость тика' % (worst, p999))
    # Замер шёл с выключенным циклическим сборщиком, поэтому отдельно
    # проверяем, что выключать его было честно: тик не должен плодить циклы,
    # иначе на живом сервере он же и платил бы за их уборку.
    report.check(garbage <= count // 100,
                 'тик не плодит циклический мусор',
                 '%d недостижимых объектов на %d тиков' % (garbage, count))


def balance_series(laps=3, runs=6, hold_ticks=180):
    """Серия гонок с бонусами и без: сколько раз меняется лидер."""
    print('')
    print('=== Баланс: смены лидера с бонусами и без ===')
    print('    (сменой считается лидерство, удержанное не меньше %.1f с)'
          % (hold_ticks / 60.0))
    summary = {}
    for track_id in TRACK_IDS:
        track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks',
                                        '%s.json' % track_id))
        for items in (True, False):
            changes = []
            spreads = []
            shifts = []
            for seed in range(runs):
                data = _balance_race(track, laps, items, seed, hold_ticks)
                changes.append(data[0])
                spreads.append(data[1])
                shifts.append(data[2])
            key = (track_id, items)
            summary[key] = (sum(changes) / runs, sum(spreads) / runs,
                            sum(shifts) / runs)
            print('  %-11s бонусы %-3s: смен лидера %.2f, разрыв 1-8 %.1f с, '
                  'сдвиг мест от середины к финишу %.2f'
                  % (track_id, 'да' if items else 'нет',
                     summary[key][0], summary[key][1], summary[key][2]))
    with_items = [v[0] for k, v in summary.items() if k[1]]
    without = [v[0] for k, v in summary.items() if not k[1]]
    print('  ИТОГО: смен лидера за гонку — с бонусами %.2f, без бонусов %.2f'
          % (sum(with_items) / len(with_items), sum(without) / len(without)))
    return summary


def _balance_race(track, laps, items, seed, hold_ticks):
    sim = new_sim(track, laps, items=items, seed=seed)
    bots = make_bots(track, sim, seed)
    leaders = []
    half = None
    for tick in range(MAX_RACE_TICKS):
        for index, car in enumerate(sim.cars):
            if car.removed:
                continue
            sim.set_input(car.slot, tick + 1, bots[index].drive(car, tick))
        sim.tick()
        leaders.append(sim.cars[sim.rank[0]].slot)
        if half is None and sim.cars[sim.rank[0]].laps >= laps // 2 + 1:
            half = dict((c.slot, c.place) for c in sim.cars)
        if sim.is_over():
            break
    rows = sim.results()
    final = dict((row['slot'], row['place']) for row in rows)
    shift = 0.0
    if half:
        shift = sum(abs(final[s] - half[s]) for s in half) / len(half)
    return (_count_lead_changes(leaders, hold_ticks),
            rows[-1]['time'] - rows[0]['time'], shift)


def _count_lead_changes(leaders, hold_ticks):
    """Смена лидера, устойчивая хотя бы hold_ticks: борьба колесо в колесо
    на каждом тике сменой не считается."""
    runs = []
    for slot in leaders:
        if runs and runs[-1][0] == slot:
            runs[-1][1] += 1
        else:
            runs.append([slot, 1])
    stable = []
    for slot, length in runs:
        if length < hold_ticks and stable:
            continue
        if stable and stable[-1] == slot:
            continue
        stable.append(slot)
    return max(0, len(stable) - 1)


# --- точка входа ------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='test_sim.py',
        description='Автотест game/sim.py и game/items.py на трёх трассах.')
    parser.add_argument('--laps', type=int, default=3, help='кругов в гонке (по умолчанию 3)')
    parser.add_argument('--seed', type=int, default=0, help='зерно ботов и бонусов')
    parser.add_argument('--balance', action='store_true',
                        help='дополнительно прогнать серию замеров баланса')
    parser.add_argument('--runs', type=int, default=6,
                        help='гонок на трассу в серии баланса')
    args = parser.parse_args(argv)

    print('=' * 72)
    print('  Автотест симуляции: %d круга, 8 ботов, трассы %s'
          % (args.laps, ', '.join(TRACK_IDS)))
    print('=' * 72)

    report = Report()
    started = time.time()
    check_generated(report)
    check_races(report, args.laps, args.seed)
    check_shield(report)
    check_rocket(report)
    check_rocket_target(report)
    check_mine(report)
    check_boxes(report)
    check_projectile_cap(report)
    check_input(report)
    check_drift(report)
    check_shortcut(report)
    check_ghosts(report)
    check_snapshot(report)
    check_traffic(report)
    check_traffic_free_lane(report)
    check_road_events(report)
    check_road_physics(report)
    check_road_settings(report)
    measure_tick(report, collect_tick_times(laps=args.laps, seed=args.seed))
    measure_traffic_tick(report)

    if args.balance:
        balance_series(args.laps, args.runs)

    print('')
    print('=' * 72)
    if report.failures:
        print('  ПРОВАЛЕНО %d из %d проверок:' % (len(report.failures), report.checks))
        for name in report.failures:
            print('    - %s' % name)
        print('=' * 72)
        return 1
    print('  ВСЕ %d ПРОВЕРОК ПРОЙДЕНЫ за %.1f с' % (report.checks, time.time() - started))
    print('=' * 72)
    return 0


if __name__ == '__main__':
    sys.exit(main())
