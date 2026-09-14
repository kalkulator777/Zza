# -*- coding: utf-8 -*-
"""Комната, конечный автомат гонки и менеджер комнат.

Автомат состояний — ровно по §9 контракта:

    LOBBY -> COUNTDOWN -> RACING -> RESULTS -> LOBBY

Комната ничего не знает про физику, трассу и бонусы: вся игровая логика живёт
за интерфейсом ``Simulation`` из §12.4. Комната отвечает за темп (фиксированный
шаг 1/60 с с накопителем), за рассылку снапшотов каждый третий тик, за
таймауты и за то, чтобы ни одно поле, пришедшее от клиента, не было принято
на веру.
"""

import json
import os
import random
import time

from tornado.ioloop import IOLoop, PeriodicCallback

from game import protocol

from . import config
from .player import sanitize_text


# --- подключение симуляции ---------------------------------------------------

_SIM_CLASS = None
_SIM_IS_STUB = False


def resolve_simulation(log=None):
    """Класс симуляции: настоящий ``game.sim.Simulation`` или временная заглушка.

    Заглушка подставляется ровно в одном случае — модуль ``game.sim`` ещё не
    написан и не импортируется. Любая другая ошибка импорта (синтаксис,
    опечатка в самом sim.py) наружу не глушится: такую поломку надо видеть,
    а не заметать под пустую гонку.
    """
    global _SIM_CLASS, _SIM_IS_STUB
    if _SIM_CLASS is not None:
        return _SIM_CLASS
    try:
        from game.sim import Simulation
        _SIM_CLASS = Simulation
        _SIM_IS_STUB = False
    except ImportError as exc:
        from ._stub_sim import StubSimulation
        _SIM_CLASS = StubSimulation
        _SIM_IS_STUB = True
        if log is not None:
            log('game.sim недоступен (%s) — работает временная заглушка '
                'симуляции: тик идёт, машины не едут' % exc)
    return _SIM_CLASS


def simulation_is_stub():
    """Работает ли сейчас заглушка вместо настоящей симуляции."""
    return _SIM_IS_STUB


# --- ошибки настроек ---------------------------------------------------------

class SettingsError(Exception):
    """Настройки от клиента не прошли проверку: код и текст уходят в error."""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


# --- каталог контента --------------------------------------------------------

class ContentLibrary(object):
    """Трассы и машины: читаются с диска один раз при старте и кэшируются.

    В горячем пути к диску не обращается никто: геометрия всех трасс (включая
    зеркальные варианты) построена заранее, каталог для welcome собран заранее.
    """

    def __init__(self, root, log=None):
        self.root = root
        self.tracks_dir = os.path.join(root, 'tracks')
        self.cars_path = os.path.join(root, 'cars.json')
        self._log = log or (lambda message: None)
        self.tracks = []             # элементы welcome.content.tracks (§9)
        self.track_ids = ()
        self.cars = []               # элементы welcome.content.cars (§9)
        self.car_ids = ()
        self.colors = list(config.COLORS)
        self.issues = []             # что не загрузилось — печатается при старте
        self._geometry = {}          # (track_id, mirror) -> Track
        self.welcome_content = {'tracks': [], 'cars': [], 'colors': self.colors}

    def load(self):
        """Прочитать контент с диска. Отсутствие файлов не мешает серверу жить."""
        self._load_tracks()
        self._load_cars()
        self.welcome_content = {
            'tracks': self.tracks,
            'cars': self.cars,
            'colors': self.colors,
        }
        return self

    def _load_tracks(self):
        try:
            from game.track import Track
        except ImportError as exc:
            self.issues.append('game.track недоступен (%s): трасс не будет' % exc)
            return
        if not os.path.isdir(self.tracks_dir):
            self.issues.append('нет каталога трасс %s' % self.tracks_dir)
            return
        catalog = []
        ids = []
        for name in sorted(os.listdir(self.tracks_dir)):
            if not name.endswith('.json'):
                continue
            path = os.path.join(self.tracks_dir, name)
            try:
                track = Track.load(path)
                mirrored = Track.load(path, mirror=True)
            except Exception as exc:
                self.issues.append('трасса %s не загрузилась: %s' % (name, exc))
                continue
            if track.id in self._geometry:
                self.issues.append('трасса %s: повтор id %r, пропущена' % (name, track.id))
                continue
            self._geometry[(track.id, False)] = track
            self._geometry[(track.id, True)] = mirrored
            ids.append(track.id)
            catalog.append({
                'id': track.id,
                'name': track.name,
                'desc': track.desc,
                'difficulty': track.difficulty,
                'theme': track.theme,
                'preview': track.preview_path(),
            })
        catalog.sort(key=lambda item: (item['difficulty'], item['id']))
        self.tracks = catalog
        self.track_ids = tuple(item['id'] for item in catalog)
        if not catalog:
            self.issues.append('в %s нет ни одной трассы' % self.tracks_dir)

    def _load_cars(self):
        if not os.path.isfile(self.cars_path):
            self.issues.append('нет файла машин %s' % self.cars_path)
            return
        try:
            with open(self.cars_path, 'r', encoding='utf-8') as fp:
                data = json.load(fp)
        except Exception as exc:
            self.issues.append('машины не загрузились: %s' % exc)
            return
        if isinstance(data, dict):
            data = data.get('cars', [])
        if not isinstance(data, list):
            self.issues.append('cars.json: ожидался список машин')
            return
        cars = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                self.issues.append('cars.json: запись без поля id пропущена')
                continue
            bars = item.get('bars')
            cars.append({
                'id': item['id'],
                'name': item.get('name', item['id']),
                'desc': item.get('desc', ''),
                'bars': bars if isinstance(bars, dict) else {},
            })
        self.cars = cars
        self.car_ids = tuple(car['id'] for car in cars)
        if not cars:
            self.issues.append('cars.json: ни одной пригодной машины')

    # --- доступ -------------------------------------------------------------

    def track(self, track_id, mirror=False):
        """Готовая геометрия трассы из кэша или None, если такой трассы нет."""
        return self._geometry.get((track_id, bool(mirror)))

    def default_track_id(self):
        return self.track_ids[0] if self.track_ids else ''

    def default_car_id(self):
        return self.car_ids[0] if self.car_ids else None

    def default_settings(self):
        """Настройки комнаты по умолчанию с подставленной первой трассой."""
        settings = dict(config.DEFAULT_SETTINGS)
        settings['items'] = list(config.DEFAULT_SETTINGS['items'])
        settings['track'] = self.default_track_id()
        return settings


# --- проверка настроек комнаты ----------------------------------------------

def validate_settings(raw, content, base=None, min_players=1):
    """Проверить настройки комнаты (§9). Клиенту не верим ни в одном поле.

    ``base`` — от чего отталкиваться (текущие настройки комнаты или умолчания);
    отсутствующие в ``raw`` поля остаются прежними. Лишние поля игнорируются.
    ``min_players`` — сколько игроков уже в комнате: ниже этого ``max_players``
    опустить нельзя.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SettingsError('bad_settings', 'настройки должны быть объектом')
    out = dict(base) if base else content.default_settings()
    out['items'] = list(out.get('items', config.ITEM_IDS))

    if 'track' in raw:
        track_id = raw['track']
        if not isinstance(track_id, str) or track_id not in content.track_ids:
            raise SettingsError('bad_track', 'нет такой трассы')
        out['track'] = track_id
    if not out.get('track'):
        raise SettingsError('bad_track', 'на сервере нет ни одной трассы')
    if out['track'] not in content.track_ids:
        raise SettingsError('bad_track', 'нет такой трассы')

    if 'laps' in raw:
        laps = raw['laps']
        if not isinstance(laps, int) or isinstance(laps, bool):
            raise SettingsError('bad_laps', 'кругов: нужно целое число')
        if not config.LAPS_MIN <= laps <= config.LAPS_MAX:
            raise SettingsError('bad_laps', 'кругов: от %d до %d'
                                % (config.LAPS_MIN, config.LAPS_MAX))
        out['laps'] = laps

    if 'max_players' in raw:
        limit = raw['max_players']
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise SettingsError('bad_max_players', 'игроков: нужно целое число')
        if not config.PLAYERS_MIN <= limit <= config.PLAYERS_MAX:
            raise SettingsError('bad_max_players', 'игроков: от %d до %d'
                                % (config.PLAYERS_MIN, config.PLAYERS_MAX))
        if limit < min_players:
            raise SettingsError('bad_max_players',
                                'в комнате уже %d игроков' % min_players)
        out['max_players'] = limit

    for flag in ('items_enabled', 'collisions', 'mirror'):
        if flag in raw:
            value = raw[flag]
            if not isinstance(value, bool):
                raise SettingsError('bad_flag', 'поле %s: нужно true или false' % flag)
            out[flag] = value

    if 'items' in raw:
        items = raw['items']
        if not isinstance(items, list) or len(items) > len(config.ITEM_IDS):
            raise SettingsError('bad_items', 'бонусы: нужен список')
        chosen = set()
        for item in items:
            if not isinstance(item, str) or item not in config.ITEM_IDS:
                raise SettingsError('bad_items', 'неизвестный бонус')
            chosen.add(item)
        # Канонический порядок из контракта, а не порядок от клиента.
        out['items'] = [item for item in config.ITEM_IDS if item in chosen]

    return out


# --- комната -----------------------------------------------------------------

class Room(object):
    """Одна комната: лобби, гонка, итоги."""

    def __init__(self, manager, room_id, name, settings, log=None):
        self.manager = manager
        self.id = room_id
        self.name = name
        self.settings = settings
        self.state = config.STATE_LOBBY
        self.owner_slot = -1
        self.players = {}            # slot -> Player
        self.order = []              # игроки в порядке входа: рассылка и передача владения
        self.chat = []               # последние сообщения (§9)
        self._log = log or (lambda message: None)
        self._sim = None
        self._race_slots = ()        # слоты участников текущей гонки
        self._reserved = set()       # слоты, занятые гонкой (в т.ч. отвалившимися)
        self._timer = None           # отсчёт / итоги
        self._loop = None            # PeriodicCallback игрового цикла
        self._accum = 0.0            # накопитель фиксированного шага
        self._last_frame = 0.0
        self._ticks = 0              # тиков с начала гонки
        self._grace_deadline = 0     # тик, на котором истекает FINISH_GRACE
        self._timeout_tick = 0       # тик, на котором истекает RACE_TIMEOUT
        self._countdown_value = 0
        self._dirty_ping = False     # ping кого-то изменился — лобби обновить
        # Измерения для приёмки: время тика и провалы темпа.
        self.tick_ms_max = 0.0
        self.tick_ms_sum = 0.0
        self.tick_samples = 0
        self.skipped_ticks = 0

    # --- общие сведения -----------------------------------------------------

    @property
    def population(self):
        return len(self.players)

    def is_empty(self):
        return not self.players

    def summary(self):
        """Элемент rooms[] события rooms (§9)."""
        return {
            'id': self.id,
            'name': self.name,
            'track': self.settings['track'],
            'laps': self.settings['laps'],
            'players': len(self.players),
            'max_players': self.settings['max_players'],
            'state': self.state,
        }

    def state_payload(self):
        """Событие room (§9): полное состояние комнаты."""
        return {
            't': 'room',
            'id': self.id,
            'name': self.name,
            'owner_slot': self.owner_slot,
            'state': self.state,
            'settings': dict(self.settings),
            'players': [player.lobby_info() for player in self.order],
            'chat': list(self.chat),
        }

    # --- рассылка -----------------------------------------------------------

    def broadcast(self, event):
        """Разослать JSON-событие всем в комнате: сериализуем один раз."""
        payload = json.dumps(event, ensure_ascii=False, separators=(',', ':'))
        for player in self.order:
            player.send_text(payload)

    def broadcast_state(self):
        self.broadcast(self.state_payload())

    # --- вход и выход -------------------------------------------------------

    def free_slot(self):
        """Наименьший свободный слот с учётом занятых гонкой, или -1.

        Слоты отвалившихся во время гонки заняты до её конца: там ещё доживает
        машина-призрак, и отдавать их новичкам нельзя.
        """
        limit = self.settings['max_players']
        for slot in range(limit):
            if slot in self.players or slot in self._reserved:
                continue
            return slot
        return -1

    def add_player(self, player):
        """Посадить игрока в свободный слот. Возвращает True при успехе."""
        slot = self.free_slot()
        if slot < 0:
            return False
        player.room = self
        player.slot = slot
        player.ready = False
        player.ack_seq = 0
        # Вошедший во время гонки ждёт в наблюдателях до следующей (§9).
        player.spectator = self.state != config.STATE_LOBBY
        if player.car is None:
            player.car = self.manager.content.default_car_id()
        self.players[slot] = player
        self.order.append(player)
        if self.owner_slot < 0:
            self.owner_slot = slot
        return True

    def remove_player(self, player):
        """Убрать игрока из комнаты: передать владение, погасить пустую комнату."""
        slot = player.slot
        if self.players.get(slot) is not player:
            return
        del self.players[slot]
        try:
            self.order.remove(player)
        except ValueError:
            pass
        player.room = None
        player.slot = -1
        player.ready = False
        player.spectator = False
        if slot in self._race_slots and self._sim is not None:
            # Машина остаётся призраком и исчезнет по правилам симуляции (§12.4).
            try:
                self._sim.drop_player(slot)
            except Exception as exc:
                self._log('sim.drop_player(%d) упал: %s' % (slot, exc))
        if self.owner_slot == slot:
            self.transfer_owner()
        if not self.players:
            self.close()
            self.manager.drop_room(self)
            return
        if self.state in (config.STATE_COUNTDOWN, config.STATE_RACING):
            if not self._active_racers():
                # Гонщиков не осталось — доигрывать нечего.
                self.finish_race('empty')
                return
        self.broadcast_state()

    def _active_racers(self):
        """Сколько участников гонки ещё на связи."""
        count = 0
        for slot in self._race_slots:
            player = self.players.get(slot)
            if player is not None and player.connected:
                count += 1
        return count

    def transfer_owner(self):
        """Передача владения комнатой первому из оставшихся по времени входа."""
        self.owner_slot = self.order[0].slot if self.order else -1

    # --- лобби --------------------------------------------------------------

    def set_car(self, player, car_id, color):
        """Выбор машины и цвета. Во время гонки менять может только зритель."""
        content = self.manager.content
        if self.state != config.STATE_LOBBY and not player.spectator:
            player.send_error('busy', 'машину меняют в лобби')
            return
        if not content.car_ids:
            player.send_error('no_cars', 'машины ещё не загружены на сервере')
            return
        if not isinstance(car_id, str) or car_id not in content.car_ids:
            player.send_error('bad_car', 'нет такой машины')
            return
        if not isinstance(color, str) or color not in config.COLORS:
            player.send_error('bad_color', 'нет такого цвета')
            return
        player.car = car_id
        player.color = color
        self.broadcast_state()

    def set_ready(self, player, ready):
        if not isinstance(ready, bool):
            player.send_error('bad_ready', 'готовность: нужно true или false')
            return
        if self.state != config.STATE_LOBBY:
            player.send_error('busy', 'готовность меняют в лобби')
            return
        if player.ready == ready:
            return
        player.ready = ready
        self.broadcast_state()

    def update_settings(self, player, raw):
        """Смена настроек: только владелец, только в лобби."""
        if player.slot != self.owner_slot:
            player.send_error('not_owner', 'настройки меняет владелец комнаты')
            return
        if self.state != config.STATE_LOBBY:
            player.send_error('busy', 'настройки меняют в лобби')
            return
        try:
            settings = validate_settings(raw, self.manager.content,
                                         base=self.settings,
                                         min_players=len(self.players))
        except SettingsError as exc:
            player.send_error(exc.code, exc.message)
            return
        self.settings = settings
        self.broadcast_state()
        self.manager.rooms_changed()

    def add_chat(self, player, text):
        """Сообщение в чат комнаты: длина и частота ограничены."""
        text = sanitize_text(text, config.CHAT_MAX_LEN)
        if not text:
            return
        if not player.chat_allowed(time.monotonic()):
            player.send_error('chat_rate', 'слишком часто')
            return
        entry = {
            'slot': player.slot,
            'name': player.name,
            'text': text,
            'ts': round(time.time(), 3),
        }
        self.chat.append(entry)
        if len(self.chat) > config.CHAT_HISTORY:
            del self.chat[:len(self.chat) - config.CHAT_HISTORY]
        # Чат — часть состояния комнаты (§9), отдельного события для него нет.
        self.broadcast_state()

    def note_ping_change(self):
        """Пометить, что ping игроков обновился: лобби обновим по таймеру."""
        self._dirty_ping = True

    def refresh_lobby_pings(self):
        """Периодическое обновление лобби ради свежих ping (только в LOBBY)."""
        if self._dirty_ping and self.state == config.STATE_LOBBY:
            self._dirty_ping = False
            self.broadcast_state()

    # --- старт гонки --------------------------------------------------------

    def start_race(self, player):
        """Запуск гонки владельцем комнаты: проверки, race_init, отсчёт."""
        if player.slot != self.owner_slot:
            player.send_error('not_owner', 'гонку запускает владелец комнаты')
            return
        if self.state != config.STATE_LOBBY:
            player.send_error('busy', 'гонка уже идёт')
            return
        racers = [p for p in self.order if p.connected and not p.spectator]
        racers.sort(key=lambda p: p.slot)
        if len(racers) < config.START_PLAYERS_MIN:
            player.send_error('not_enough', 'некому ехать')
            return
        if any(not p.ready for p in racers):
            player.send_error('not_ready', 'не все готовы')
            return
        content = self.manager.content
        track = content.track(self.settings['track'], self.settings['mirror'])
        if track is None:
            player.send_error('bad_track', 'трасса не загружена')
            return
        default_car = content.default_car_id()
        for racer in racers:
            if racer.car is None:
                racer.car = default_car

        sim_class = self.manager.simulation_class
        try:
            sim = sim_class(track, dict(self.settings), [p.sim_info() for p in racers])
        except Exception as exc:
            self._log('комната %s: симуляция не создалась: %s' % (self.id, exc))
            self.broadcast({'t': 'error', 'code': 'sim_failed',
                            'message': 'гонка не стартовала: %s' % exc})
            return

        self._sim = sim
        self._race_slots = tuple(p.slot for p in racers)
        self._reserved = set(self._race_slots)
        self._ticks = 0
        self._grace_deadline = 0
        self._timeout_tick = int(config.RACE_TIMEOUT / config.TICK_DT)
        self.tick_ms_max = 0.0
        self.tick_ms_sum = 0.0
        self.tick_samples = 0
        self.skipped_ticks = 0
        self.state = config.STATE_COUNTDOWN

        grid = getattr(track, 'start_grid', None) or []
        players_payload = []
        for index, racer in enumerate(racers):
            place = grid[index] if index < len(grid) else {'x': 0.0, 'z': 0.0, 'yaw': 0.0}
            players_payload.append(racer.race_info(place))
        self.broadcast({
            't': 'race_init',
            'track': track.to_client(),
            'laps': self.settings['laps'],
            'settings': dict(self.settings),
            'players': players_payload,
        })
        self.broadcast_state()
        self.manager.rooms_changed()
        # Один снапшот до отсчёта: клиент сразу видит машины на решётке.
        self.broadcast_snapshot()
        self._countdown_value = config.COUNTDOWN_SECONDS
        self._countdown_step()

    def _countdown_step(self):
        """Шаги отсчёта 3, 2, 1, 0 — по секунде (§9)."""
        self.broadcast({'t': 'countdown', 'value': self._countdown_value})
        if self._countdown_value <= 0:
            self._timer = None
            self._begin_racing()
            return
        self._countdown_value -= 1
        self._timer = IOLoop.current().call_later(1.0, self._countdown_step)

    def _begin_racing(self):
        """Ввод разблокирован, запускается игровой цикл."""
        self.state = config.STATE_RACING
        self.broadcast_state()
        self._accum = 0.0
        self._last_frame = time.monotonic()
        self._loop = PeriodicCallback(self._frame, config.TICK_MS)
        self._loop.start()

    # --- игровой цикл -------------------------------------------------------

    def _frame(self):
        """Кадр таймера: догоняем фиксированный шаг накопителем (§12.4)."""
        now = time.monotonic()
        elapsed = now - self._last_frame
        self._last_frame = now
        if elapsed < 0.0:
            elapsed = 0.0
        self._accum += elapsed
        dt = config.TICK_DT
        steps = int(self._accum / dt)
        if steps > config.MAX_CATCHUP_TICKS:
            # Отстали сильно — пропускаем отставание, а не нагоняем (§12.4):
            # лучше рывок, чем спираль смерти.
            dropped = steps - config.MAX_CATCHUP_TICKS
            self._accum -= dropped * dt
            self.skipped_ticks += dropped
            steps = config.MAX_CATCHUP_TICKS
        while steps > 0:
            steps -= 1
            self._accum -= dt
            if not self._tick_once():
                return

    def _tick_once(self):
        """Один шаг симуляции. False — гонка на этом шаге закончилась."""
        sim = self._sim
        if sim is None:
            return False
        started = time.perf_counter()
        try:
            events = sim.tick()
        except Exception as exc:
            self._log('комната %s: симуляция упала на тике %d: %s'
                      % (self.id, self._ticks, exc))
            self.finish_race('sim_error')
            return False
        self._ticks += 1
        if events:
            self._emit_events(events)
        if self._ticks % config.SNAPSHOT_EVERY == 0:
            self.broadcast_snapshot()
        spent = (time.perf_counter() - started) * 1000.0
        self.tick_ms_sum += spent
        self.tick_samples += 1
        if spent > self.tick_ms_max:
            self.tick_ms_max = spent
        return self._check_race_end()

    def _emit_events(self, events):
        """Рассылка race_event от симуляции и отметка финиша лидера."""
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get('t') is None:
                event['t'] = 'race_event'
            if event.get('kind') == 'finish' and not self._grace_deadline:
                # После финиша лидера остальным даётся FINISH_GRACE (§9).
                self._grace_deadline = self._ticks + int(config.FINISH_GRACE / config.TICK_DT)
            self.broadcast(event)

    def _check_race_end(self):
        """Условия окончания гонки. True — продолжаем."""
        sim = self._sim
        try:
            over = bool(sim.is_over())
        except Exception as exc:
            self._log('комната %s: is_over() упал: %s' % (self.id, exc))
            over = True
        if over:
            self.finish_race('over')
            return False
        if self._grace_deadline and self._ticks >= self._grace_deadline:
            self.finish_race('grace')
            return False
        if self._ticks >= self._timeout_tick:
            self.finish_race('timeout')
            return False
        return True

    def broadcast_snapshot(self):
        """Снапшот собирается один раз на комнату, на клиента правится ack_seq."""
        sim = self._sim
        if sim is None:
            return
        try:
            cars, projectiles, box_mask = sim.snapshot_args()
        except Exception as exc:
            self._log('комната %s: snapshot_args() упал: %s' % (self.id, exc))
            return
        buf = protocol.build_snapshot_base(sim.tick_no, cars, projectiles, box_mask)
        race_slots = self._race_slots
        stamp = protocol.stamp_ack
        for player in self.order:
            if not player.connected:
                continue
            slot = player.slot
            if slot in race_slots:
                ack = sim.ack_seq(slot)
                player.ack_seq = ack
            else:
                ack = 0
            stamp(buf, ack)
            # Tornado принимает только bytes: копия 282 байт на клиента дешевле
            # пересборки всего пакета (§12.3).
            player.send_binary(bytes(buf))

    def set_input(self, player, seq, buttons):
        """Бинарный ввод от игрока: только участнику идущей гонки."""
        if self.state != config.STATE_RACING or self._sim is None:
            return
        slot = player.slot
        if slot not in self._race_slots:
            return
        self._sim.set_input(slot, seq, buttons)

    # --- финиш и итоги ------------------------------------------------------

    def finish_race(self, reason):
        """Завершение гонки: итоги на RESULTS_SECONDS и назад в лобби."""
        if self.state not in (config.STATE_COUNTDOWN, config.STATE_RACING):
            return
        self._stop_loop()
        sim = self._sim
        rows = []
        if sim is not None:
            try:
                rows = sim.results()
            except Exception as exc:
                self._log('комната %s: results() упал: %s' % (self.id, exc))
                rows = []
        self.state = config.STATE_RESULTS
        self.broadcast({'t': 'results', 'rows': rows})
        self.broadcast_state()
        self.manager.rooms_changed()
        self.log_tick_stats(reason)
        self._timer = IOLoop.current().call_later(config.RESULTS_SECONDS,
                                                  self._back_to_lobby)

    def log_tick_stats(self, reason):
        """Напечатать измеренную стоимость тика: это условие приёмки (§1)."""
        if not self.tick_samples:
            return
        self._log('комната %s: гонка окончена (%s), тиков %d, '
                  'тик avg %.3f мс, max %.3f мс, пропущено %d'
                  % (self.id, reason, self.tick_samples,
                     self.tick_ms_sum / self.tick_samples,
                     self.tick_ms_max, self.skipped_ticks))
        self.tick_samples = 0
        self.tick_ms_sum = 0.0

    def _back_to_lobby(self):
        """RESULTS -> LOBBY: зрители становятся игроками, готовность снимается."""
        self._timer = None
        self._sim = None
        self._race_slots = ()
        self._reserved = set()
        self.state = config.STATE_LOBBY
        for player in self.order:
            player.reset_for_lobby()
        self.broadcast_state()
        self.manager.rooms_changed()

    def _stop_loop(self):
        if self._loop is not None:
            self._loop.stop()
            self._loop = None
        if self._timer is not None:
            IOLoop.current().remove_timeout(self._timer)
            self._timer = None

    def close(self):
        """Погасить все таймеры комнаты (выход последнего игрока, остановка сервера)."""
        self._stop_loop()
        self.log_tick_stats('комната закрыта')
        self._sim = None
        self.state = config.STATE_LOBBY

    def __repr__(self):
        return '<Room %s %r %s %d/%d>' % (self.id, self.name, self.state,
                                          len(self.players),
                                          self.settings['max_players'])


# --- менеджер комнат ---------------------------------------------------------

class RoomManager(object):
    """Все комнаты сервера и все подключённые игроки."""

    def __init__(self, content, servers_provider=None, log=None):
        self.content = content
        self.rooms = {}
        self.players = []                    # все игроки, прошедшие hello
        self._servers = servers_provider or (lambda: [])
        self._log = log or (lambda message: None)
        self.simulation_class = resolve_simulation(self._log)
        self._ping_timer = None
        self._lobby_timer = None
        self._rng = random.SystemRandom()

    # --- жизненный цикл ------------------------------------------------------

    def start(self):
        """Запустить служебные таймеры: замер ping и обновление лобби."""
        self._ping_timer = PeriodicCallback(self._ping_round,
                                            config.PING_INTERVAL * 1000.0)
        self._ping_timer.start()
        self._lobby_timer = PeriodicCallback(self._lobby_round,
                                             config.LOBBY_REFRESH_INTERVAL * 1000.0)
        self._lobby_timer.start()

    def stop(self):
        for timer in (self._ping_timer, self._lobby_timer):
            if timer is not None:
                timer.stop()
        self._ping_timer = None
        self._lobby_timer = None
        for room in list(self.rooms.values()):
            room.close()
        self.rooms.clear()

    def _ping_round(self):
        now = time.monotonic()
        for player in self.players:
            if player.connected:
                player.start_ping(now)

    def _lobby_round(self):
        for room in self.rooms.values():
            room.refresh_lobby_pings()

    # --- игроки --------------------------------------------------------------

    def attach(self, player):
        self.players.append(player)

    def detach(self, player):
        """Соединение закрылось: выйти из комнаты и забыть игрока."""
        player.connected = False
        room = player.room
        if room is not None:
            room.remove_player(player)
            self.rooms_changed()
        try:
            self.players.remove(player)
        except ValueError:
            pass

    def stats(self):
        """(комнат, игроков) для UDP-маяка."""
        return len(self.rooms), len(self.players)

    # --- комнаты -------------------------------------------------------------

    def new_room_id(self):
        while True:
            room_id = '%0*x' % (config.ROOM_ID_BYTES * 2,
                                self._rng.getrandbits(config.ROOM_ID_BYTES * 8))
            if room_id not in self.rooms:
                return room_id

    def create_room(self, player, name, raw_settings):
        """Создать комнату и посадить в неё создателя. Возвращает Room или None."""
        if len(self.rooms) >= config.MAX_ROOMS:
            player.send_error('too_many_rooms', 'на сервере слишком много комнат')
            return None
        try:
            settings = validate_settings(raw_settings, self.content, base=None,
                                         min_players=1)
        except SettingsError as exc:
            player.send_error(exc.code, exc.message)
            return None
        room_name = sanitize_text(name, config.ROOM_NAME_MAX_LEN)
        if not room_name:
            room_name = 'Комната %s' % (player.name or 'без имени')
        if player.room is not None:
            player.room.remove_player(player)
        room = Room(self, self.new_room_id(), room_name, settings, log=self._log)
        self.rooms[room.id] = room
        if not room.add_player(player):
            del self.rooms[room.id]
            player.send_error('room_full', 'в комнате нет мест')
            return None
        room.broadcast_state()
        self.rooms_changed()
        return room

    def join_room(self, player, room_id):
        """Вход в комнату по идентификатору."""
        if not isinstance(room_id, str):
            player.send_error('bad_room', 'нет такой комнаты')
            return None
        room = self.rooms.get(room_id)
        if room is None:
            player.send_error('no_room', 'комната не найдена')
            return None
        if player.room is room:
            room.broadcast_state()
            return room
        if player.room is not None:
            player.room.remove_player(player)
            self.rooms_changed()
        if not room.add_player(player):
            player.send_error('room_full', 'в комнате нет мест')
            return None
        room.broadcast_state()
        self.rooms_changed()
        return room

    def leave_room(self, player):
        room = player.room
        if room is None:
            return
        room.remove_player(player)
        self.rooms_changed()
        player.send_event(self.rooms_event())

    def drop_room(self, room):
        """Удалить опустевшую комнату."""
        if self.rooms.pop(room.id, None) is not None:
            self.rooms_changed()

    # --- список комнат -------------------------------------------------------

    def rooms_event(self):
        """Событие rooms (§9): комнаты этого сервера и соседи по сети."""
        try:
            servers = self._servers()
        except Exception:
            servers = []
        return {
            't': 'rooms',
            'rooms': [room.summary() for room in self.rooms.values()],
            'servers': servers,
        }

    def rooms_changed(self):
        """Разослать обновлённый список тем, кто сейчас в меню."""
        payload = None
        for player in self.players:
            if player.room is None and player.connected:
                if payload is None:
                    payload = json.dumps(self.rooms_event(), ensure_ascii=False,
                                         separators=(',', ':'))
                player.send_text(payload)
