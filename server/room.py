# -*- coding: utf-8 -*-
"""Комнаты: код из 4 букв, фазы, вход, выход, отключение, переподключение.

Комната живёт по фазам:

    LOBBY  -> PLAYING -> FINISHED

В LOBBY мира НЕТ вовсе: люди собираются, выбирают настройки (RoomSettings),
и ничего не считается — лобби процессор не ест, тик его не трогает. Мир
рождается ровно в одном месте — Room.start(), из настроек. FINISHED пока
заглушка: конца забега на этапе 0a ещё нет.

Переход LOBBY -> PLAYING делает только start(). Кто решает, что пора, —
это политика, она отдельно (maybe_start), и на этапе 5 её заменит кнопка
хозяина лобби, не трогая start().

Игроки одной комнаты живут в одном мире и получают ОДИН И ТОТ ЖЕ
сериализованный снапшот (DESIGN.md 4.4, 11.2). Личное у игрока — только
короткая голова сообщения с его ack (см. proto.snap_with_ack), сущности
сериализуются один раз на комнату.
"""

import asyncio
import random
import time

from . import ai
from . import gen
from . import proto
from . import world as world_mod

CODE_ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ"   # без I, O, Q — их путают вслух
CODE_LEN = 4
MAX_PLAYERS = 8
RECONNECT_SEC = 120.0      # столько ждём отключившегося, держа его сущность
EMPTY_ROOM_SEC = 300.0     # столько пустая комната живёт перед сносом

# фазы комнаты
LOBBY = "lobby"
PLAYING = "playing"
FINISHED = "finished"


class RoomSettings(object):
    """Параметры игры, выбираемые в лобби. Из них рождается мир.

    На этапе 0a здесь ровно то, что уже нужно генератору. Место для
    остального (режим, набор карт, сложность, число этажей, правила
    смерти) — здесь же: этап 5 добавляет поле сюда и читает его в
    Room.start(), больше нигде трогать не надо.
    """

    __slots__ = ("seed", "mode", "floor", "map_w", "map_h", "theme")

    def __init__(self, seed=None, mode="coop", floor=1,
                 map_w=gen.ROOM_W, map_h=gen.ROOM_H, theme=None):
        self.seed = seed if seed is not None else random.randrange(1, 1 << 30)
        self.mode = mode        # этап 5: кооп / испытание / ...
        self.floor = floor
        self.map_w = map_w
        self.map_h = map_h
        # тема генератора (8.6). Источник истины по параметрам — сервер
        # (8.7): присланное значение проверяется по списку допустимых, а не
        # принимается на веру.
        self.theme = theme if theme in gen.THEMES else gen.DEFAULT_THEME

    def describe(self):
        """Плоский вид для будущего протокола лобби (этап 5)."""
        return {"seed": self.seed, "mode": self.mode, "theme": self.theme,
                "w": self.map_w, "h": self.map_h}



class Player(object):
    __slots__ = ("pid", "name", "conn", "ent_id", "room", "ready",
                 "ack", "pending", "needs_full", "gone_at", "bytes_out",
                 "msgs_out")

    def __init__(self, pid, name, conn):
        self.pid = pid
        self.name = name or ("Игрок%d" % pid)
        self.conn = conn
        self.ent_id = 0
        self.room = None
        self.ready = False
        self.ack = 0            # последний применённый seq (5.1)
        self.pending = None     # последний пришедший input, применяется в тик
        self.needs_full = True
        self.gone_at = 0.0
        self.bytes_out = 0
        self.msgs_out = 0

    @property
    def online(self):
        return self.conn is not None

    def send(self, text):
        if self.conn is None:
            return
        # снапшот — только цифры, то есть ASCII: длина строки и есть байты.
        # encode() здесь гонялся бы вторым проходом поверх того, что сделает
        # tornado, а это 6 x 10 КБ x 30 Гц впустую.
        self.bytes_out += len(text) if text.isascii() else len(text.encode("utf-8"))
        self.msgs_out += 1
        self.conn.send_str(text)


# --- голова полного снапшота: ack И you свои у каждого игрока -------------
# DESIGN.md 5.2 требует "you" (id своей сущности) в каждом снапшоте с
# full:true. 4.4 требует сериализовать снапшот один раз на комнату — та же
# коллизия, что уже решена для ack (proto.snap_with_ack): тяжёлый хвост
# (tick, e, vis) готовится один раз в w.snapshot_full(), а лёгкая голова
# клеится строкой отдельно на каждого игрока. you — лишь ещё одно поле в
# этой голове, поэтому за пределы приёма proto.py не выходим: голова и так
# уже собирается здесь, в room.py, вызовом proto.snap_with_ack. На дельтах
# you не шлём — контракт требует его только при full:true, а хвост дельты и
# так общий на комнату и не должен меняться ради поля, которое ему не нужно.
def _snap_full_with_you(tail, ack, you):
    return '{"t":"snap","ack":%d,"you":%d,%s' % (ack, you, tail)


class Room(object):
    def __init__(self, code, settings=None, seed=None):
        self.code = code
        self.settings = settings if settings is not None else RoomSettings(seed=seed)
        self.phase = LOBBY
        self.world = None            # мира нет до start(): это лобби
        self.players = {}
        self.created = time.monotonic()
        self.empty_since = time.monotonic()
        self.started_at = 0.0

    @property
    def floor(self):
        return self.settings.floor

    # --- фазы --------------------------------------------------------------

    def start(self):
        """LOBBY -> PLAYING. Единственное место, где рождается мир."""
        if self.phase != LOBBY:
            return False
        s = self.settings
        fl = gen.generate(s.seed, s.floor, s.map_w, s.map_h, s.theme)
        self.world = world_mod.World(fl.grid, s.seed, s.floor,
                                     fl.spawns, fl.stairs)
        # 8.1: враги по комнатам, кроме стартовой, числом от глубины этажа
        ai.populate(self.world, fl)
        for i, p in enumerate(self.players.values()):
            e = self.world.spawn_player(p.name, i)
            p.ent_id = e.id
            p.needs_full = True
            if not p.online:
                e.flags |= world_mod.F_OFFLINE
        self.phase = PLAYING
        self.started_at = time.monotonic()
        for p in self.players.values():
            if p.online:
                self.send_level(p)
        return True

    def maybe_start(self):
        """Политика запуска, а не механика. На 0a: все, кто в сети, готовы.

        Этап 5 заменит это решением хозяина лобби — start() не изменится.
        """
        if self.phase != LOBBY:
            return False
        online = [p for p in self.players.values() if p.online]
        if not online or not all(p.ready for p in online):
            return False
        return self.start()

    def finish(self):
        """PLAYING -> FINISHED. Заглушка: конца забега на 0a ещё нет."""
        if self.phase != PLAYING:
            return False
        self.phase = FINISHED
        return True

    # --- вход/выход --------------------------------------------------------

    def count_online(self):
        return sum(1 for p in self.players.values() if p.online)

    def player_list(self):
        return [[p.pid, p.name] for p in self.players.values()]

    def find_offline_by_name(self, name):
        if not name:
            return None
        for p in self.players.values():
            if not p.online and p.name == name:
                return p
        return None

    def spawn_for(self, player):
        """Завести сущность игроку. В лобби сущностей нет вовсе."""
        if self.world is None:
            return None
        e = self.world.spawn_player(player.name, len(self.players))
        player.ent_id = e.id
        return e

    def add(self, player):
        """Вход или переподключение. Возвращает принятого игрока или None."""
        old = self.find_offline_by_name(player.name)
        if old is not None:
            # переподключение: тот же игрок, та же сущность
            old.conn = player.conn
            old.pending = None
            old.needs_full = True
            old.gone_at = 0.0
            if self.world is not None:
                ent = self.world.entities.get(old.ent_id)
                if ent is not None:
                    ent.flags &= ~world_mod.F_OFFLINE
                    ent.mv = (0.0, 0.0)
                else:
                    self.spawn_for(old)
            player.conn.player = old
            old.room = self
            self.empty_since = 0.0
            self.announce_joined()
            return old
        if self.count_online() >= MAX_PLAYERS:
            return None
        player.room = self
        player.needs_full = True
        self.spawn_for(player)       # в LOBBY вернёт None, и это правильно
        self.players[player.pid] = player
        self.empty_since = 0.0
        self.announce_joined()
        return player

    def disconnect(self, player):
        """Связь пропала. Сущность держим RECONNECT_SEC, помечая OFFLINE."""
        player.conn = None
        player.gone_at = time.monotonic()
        player.pending = None
        player.ready = False
        if self.world is not None:
            ent = self.world.entities.get(player.ent_id)
            if ent is not None:
                ent.flags |= world_mod.F_OFFLINE
                ent.mv = (0.0, 0.0)
                ent.btn = 0          # иначе зажатая атака молотит без хозяина
                ent.vx = ent.vy = 0.0
        if self.count_online() == 0:
            self.empty_since = time.monotonic()
        self.announce_joined()

    def drop(self, player):
        """Насовсем: убрать игрока и его сущность."""
        self.players.pop(player.pid, None)
        if self.world is not None:
            self.world.remove(player.ent_id)
        if self.count_online() == 0:
            self.empty_since = time.monotonic()

    def expire(self, now):
        for p in list(self.players.values()):
            if not p.online and p.gone_at and now - p.gone_at > RECONNECT_SEC:
                self.drop(p)

    # --- сообщения ---------------------------------------------------------

    def broadcast(self, text):
        for p in self.players.values():
            p.send(text)

    def announce_joined(self):
        lst = self.player_list()
        for p in self.players.values():
            if p.online:
                p.send(proto.joined(self.code, p.pid, lst))

    def send_level(self, player):
        if self.world is None:
            return False         # в лобби показывать нечего
        w = self.world
        player.send(proto.level(self.floor, self.settings.seed,
                                w.grid.w, w.grid.h, bytes(w.grid.tiles)))
        return True

    # --- тик ---------------------------------------------------------------

    def apply_inputs(self):
        ents = self.world.entities
        for p in self.players.values():
            m = p.pending
            if m is None:
                continue
            p.pending = None
            p.ack = m["seq"]
            e = ents.get(p.ent_id)
            if e is None:
                continue
            e.mv = m["mv"]
            e.btn = m["btn"]
            e.aim = m["aim"]

    def tick(self):
        if self.phase != PLAYING:
            return False         # лобби не считается вообще
        self.apply_inputs()
        self.world.step()
        if self.stairs_ready():
            self.descend()       # 8.1: спуск, когда на лестнице ВСЕ живые
        self.broadcast_events()
        self.broadcast_snapshot()
        return True

    # --- спуск на следующий этаж (8.1) -------------------------------------

    def stairs_ready(self):
        """Все живые стоят на лестнице. Один группу не утаскивает.

        Считается по ЖИВЫМ (world.alive_players): мёртвый ходит духом (8.5) и
        воскреснет этажом ниже, ждать его негде — на лестницу дух встать может,
        но требовать этого значило бы запирать группу до конца забега.
        """
        w = self.world
        if w is None or w.stairs is None:
            return False
        alive = w.alive_players()
        if not alive:
            return False         # все мертвы — это конец забега (8.1), не спуск
        for e in alive:
            if not w.on_stairs(e):
                return False
        return True

    def descend(self):
        """Этаж +1: тот же сид, новая карта, туман с нуля, здоровье с собой."""
        s = self.settings
        s.floor += 1
        fl = gen.generate(s.seed, s.floor, s.map_w, s.map_h, s.theme)
        self.world.enter_floor(s.floor, fl.grid, fl.spawns, fl.stairs)
        ai.populate(self.world, fl)      # 8.1: этаж глубже — врагов больше
        for p in self.players.values():
            # 5.2: при смене этажа клиент получает level и полный снапшот.
            # Порядок обязателен: level чистит у клиента сущности и туман,
            # полный снапшот заводит их заново.
            p.needs_full = True
            if p.online:
                self.send_level(p)
        return s.floor

    def broadcast_events(self):
        """ev (5.2): попадание, смерть, выстрел. Копится в world, шлём тут.

        Мир про сеть не знает — поэтому события он складывает, а рассылает их
        комната. Потеря ev допустима по 5.2, состояния на них нет: замах и
        рывок видны битами flags, здоровье и смерть — полями снапшота.
        """
        evs = self.world.events
        if not evs:
            return 0
        n = len(evs)
        for tick, kind, kw in evs:
            self.broadcast(proto.ev(tick, kind, **kw))
        del evs[:]
        return n

    def broadcast_snapshot(self):
        w = self.world
        tail = w.snapshot_delta()           # один раз на комнату (4.4)
        full_tail = None
        for p in self.players.values():
            if not p.online:
                continue
            if p.needs_full:
                if full_tail is None:
                    full_tail = w.snapshot_full(remember=False)
                p.needs_full = False
                p.send(_snap_full_with_you(full_tail, p.ack, p.ent_id))
            else:
                p.send(proto.snap_with_ack(tail, p.ack))


class Rooms(object):
    """Реестр комнат и общий тик 30 Гц на asyncio."""

    def __init__(self):
        self.rooms = {}
        self._next_pid = 1
        self._task = None
        self.running = False
        self.tick_ms = []          # последние замеры длительности тика

    def new_pid(self):
        p = self._next_pid
        self._next_pid += 1
        return p

    def new_code(self):
        for _ in range(200):
            c = "".join(random.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
            if c not in self.rooms:
                return c
        raise RuntimeError("не удалось подобрать код комнаты")

    def create(self, seed=None, settings=None):
        """Новая комната. Рождается в LOBBY и без мира."""
        code = self.new_code()
        r = Room(code, settings=settings, seed=seed)
        self.rooms[code] = r
        return r

    def get(self, code):
        return self.rooms.get(code)

    def get_or_create(self, code):
        if code:
            r = self.rooms.get(code)
            if r is not None:
                return r
        return self.create()

    def listing(self):
        # форма ровно как в 5.2: без phase нельзя отличить лобби от чужой
        # партии посередине — а это ровно то, зачем список нужен.
        return [{"id": r.code, "players": r.count_online(), "floor": r.floor,
                 "phase": r.phase}
                for r in self.rooms.values()]

    def housekeep(self, now):
        for code, r in list(self.rooms.items()):
            r.expire(now)
            if not r.players and r.empty_since and now - r.empty_since > EMPTY_ROOM_SEC:
                del self.rooms[code]

    def tick_all(self):
        # тикают только играющие комнаты: лобби процессор не ест
        for r in list(self.rooms.values()):
            if r.phase == PLAYING:
                r.tick()

    async def run(self):
        """Тик 30 Гц без накопления сдвига."""
        self.running = True
        dt = world_mod.DT
        next_t = time.monotonic()
        last_keep = next_t
        loop = asyncio.get_running_loop()
        while self.running:
            next_t += dt
            delay = next_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.5:
                next_t = time.monotonic()     # отстали сильно — не догоняем
            t0 = loop.time()
            try:
                self.tick_all()
            except Exception:
                import traceback
                traceback.print_exc()
            dur = (loop.time() - t0) * 1000.0
            self.tick_ms.append(dur)
            if len(self.tick_ms) > 900:
                del self.tick_ms[:300]
            now = time.monotonic()
            if now - last_keep > 5.0:
                last_keep = now
                self.housekeep(now)

    def stop(self):
        self.running = False
