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
import math
import os
import random
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from game import items as items_mod
from game import protocol
from game.sim import Simulation
from game.track import Track, WALL_MARGIN

TRACK_IDS = ('office', 'serpentine', 'industrial')
CAR_IDS = ('hatch', 'muscle', 'buggy', 'van', 'wedge', 'hatch', 'buggy', 'van')
ALL_ITEMS = list(items_mod.ITEM_IDS)

TWO_PI = math.pi * 2.0
MAX_RACE_TICKS = 60 * 60 * 8          # восемь минут — дальше гонка признаётся зависшей
DRIFT_COMMIT = 52                     # тиков, на которые бот фиксирует занос

BTN_THROTTLE = protocol.BTN_THROTTLE
BTN_BRAKE = protocol.BTN_BRAKE
BTN_LEFT = protocol.BTN_LEFT
BTN_RIGHT = protocol.BTN_RIGHT
BTN_DRIFT = protocol.BTN_DRIFT
BTN_ITEM = protocol.BTN_ITEM


# --- бот-автопилот ----------------------------------------------------------

class Autopilot(object):
    """Простой автопилот: осевая линия, газ в пол, руль к центру полотна."""

    def __init__(self, track, sim, skill=1.0, seed=0, lane=0.0):
        self.track = track
        self.sim = sim
        self.count = len(track.samples)
        self.step = track.length / self.count
        self.skill = skill
        self.lane = lane                     # своя полоса, доля полуширины
        self.rng = random.Random(seed)
        self.hold = 0                        # задержка перед применением бонуса
        self.slow_ticks = 0                  # сколько тиков почти стоим
        self.reverse_ticks = 0               # сколько тиков ещё выбираться задом
        self.drift_ticks = 0                 # сколько тиков держим занос
        self.drift_dir = 0                   # в какую сторону держим руль в заносе
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

        # --- скорость по кривизне впереди
        near = samples[(index + int(10.0 / self.step)) % count]
        far = samples[(index + int(34.0 / self.step)) % count]
        dot = near.tangent_x * far.tangent_x + near.tangent_z * far.tangent_z
        if dot > 1.0:
            dot = 1.0
        elif dot < -1.0:
            dot = -1.0
        turn = math.acos(dot)
        limit = 33.0 * self.skill
        if turn > 0.15:
            corner = (7.0 + 5.5 / turn) * self.skill
            if corner < limit:
                limit = corner
        if state.boost_time > 0.0:
            limit = 70.0            # ускорение не тормозим, оно для того и взято

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
        elif abs(state.steer) > 0.6 and speed > 14.0 and turn > 0.35:
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


def make_players(count=8):
    return [{'slot': i, 'name': 'Бот %d' % i,
             'car_id': CAR_IDS[i % len(CAR_IDS)], 'color': '#ffffff'}
            for i in range(count)]


def new_sim(track, laps, items=True, collisions=True, count=8, seed=0):
    sim = Simulation(track, make_settings(laps, items, collisions),
                     make_players(count))
    sim.items.rng = random.Random(seed * 977 + 13)
    return sim


def make_bots(track, sim, seed, count=8, spread=0.03):
    rng = random.Random(seed)
    lanes = [-0.5 + k / float(count - 1) for k in range(count)] if count > 1 else [0.0]
    rng.shuffle(lanes)
    return [Autopilot(track, sim, 1.0 + rng.uniform(-spread, spread),
                      seed * 31 + i, lanes[i]) for i in range(count)]


# --- полная гонка -----------------------------------------------------------

def run_full_race(track, laps, seed, report, items=True, measure=False):
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
    tick_times = []
    leader_seq = []

    for tick in range(MAX_RACE_TICKS):
        for index, car in enumerate(sim.cars):
            if car.removed:
                continue
            sim.set_input(car.slot, tick + 1, bots[index].drive(car, tick))

        started = time.perf_counter()
        events = sim.tick()
        if measure:
            tick_times.append((time.perf_counter() - started) * 1000.0)

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
        'ticks': finished_ticks, 'tick_times': tick_times,
        'leader_seq': leader_seq,
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
    measured = None
    for track_id in TRACK_IDS:
        track = Track.load(os.path.join(BASE_DIR, 'content', 'tracks',
                                        '%s.json' % track_id))
        report.section('Полная гонка: %s (%s), %d круга, 8 ботов'
                       % (track.name, track_id, laps))
        result = run_full_race(track, laps, seed, report, items=True,
                               measure=(track_id == 'office'))
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
        if result['tick_times']:
            measured = result['tick_times']

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
    return measured


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


def check_rocket(report):
    """Ракета догоняет цель впереди на разных дистанциях."""
    report.section('Ракета самонаводится и попадает')
    track = _solo_track()
    hits = []
    for head_start in (0, 30, 120, 240):
        sim = new_sim(track, 5, count=2, collisions=False)
        leader, chaser = sim.cars[0], sim.cars[1]
        for _ in range(head_start):
            sim.set_input(leader.slot, sim.tick_no + 1, BTN_THROTTLE)
            sim.tick()
        for _ in range(240):
            sim.set_input(leader.slot, sim.tick_no + 1, BTN_THROTTLE)
            sim.set_input(chaser.slot, sim.tick_no + 1, BTN_THROTTLE)
            sim.tick()
        gap = leader.state.progress - chaser.state.progress
        chaser.item = items_mod.ITEM_ROCKET
        sim.items.use(sim, chaser)
        proj = sim.items.projectiles[sim.items._live[0]]
        target_ok = proj.target == leader.slot
        hit_at = None
        for step in range(400):
            sim.set_input(leader.slot, sim.tick_no + 1, BTN_THROTTLE)
            sim.set_input(chaser.slot, sim.tick_no + 1, BTN_THROTTLE)
            for event in sim.tick():
                if event['kind'] == 'hit' and event['item'] == 'rocket':
                    hit_at = step / 60.0
            if hit_at is not None:
                break
        hits.append((round(gap, 1), hit_at, target_ok))
    report.check(all(item[2] for item in hits),
                 'цель — ближайший впереди по progress')
    report.check(all(item[1] is not None for item in hits),
                 'ракета догоняет цель на всех дистанциях',
                 'отрыв/время: %s' % [(g, t) for g, t, _ in hits])
    report.check(all(item[1] > 0.0 for item in hits if item[1] is not None),
                 'ракета не взрывается о собственный бампер')


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
    bot = make_bots(track, sim, 0, count=1)[0]
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
    report.check(all(isinstance(row, tuple) and len(row) == 11 for row in cars_out),
                 'машины — кортежи из 11 полей')
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

def measure_tick(report, samples):
    """Время тика при восьми машинах — условие приёмки раздела 1."""
    report.section('Время тика (восемь машин, бонусы включены)')
    if not samples:
        report.check(False, 'замер не состоялся')
        return
    ordered = sorted(samples)
    count = len(ordered)
    avg = sum(ordered) / count
    p50 = ordered[count // 2]
    p99 = ordered[int(count * 0.99)]
    worst = ordered[-1]
    report.note('тиков измерено: %d' % count)
    report.note('среднее %.4f мс, медиана %.4f мс, p99 %.4f мс, максимум %.4f мс'
                % (avg, p50, p99, worst))
    report.check(avg < 0.5, 'среднее время тика в бюджете комнаты (2 мс)',
                 '%.4f мс — это %.1f %% бюджета' % (avg, avg / 2.0 * 100.0))
    report.check(worst < 2.0, 'даже худший тик уложился в 2 мс',
                 '%.4f мс' % worst)


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
    samples = check_races(report, args.laps, args.seed)
    check_shield(report)
    check_rocket(report)
    check_mine(report)
    check_boxes(report)
    check_projectile_cap(report)
    check_input(report)
    check_drift(report)
    check_shortcut(report)
    check_ghosts(report)
    check_snapshot(report)
    measure_tick(report, samples)

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
