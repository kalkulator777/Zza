# -*- coding: utf-8 -*-
"""Комнаты: код из 4 букв, фазы, вход, выход, отключение, переподключение.

Комната живёт по фазам:

    LOBBY  -> PLAYING -> FINISHED

В LOBBY мира НЕТ вовсе: люди собираются, выбирают настройки (RoomSettings),
и ничего не считается — лобби процессор не ест, тик его не трогает. Мир
рождается ровно в одном месте — Room.start(), из настроек. FINISHED пока
заглушка: конца забега на этапе 0a ещё нет.

Переход LOBBY -> PLAYING делает только start(). Кто решает, что пора, —
это политика, и она отдельно (try_start): старт взводит ХОЗЯИН лобби
(8.7), а выстрел происходит, когда готовы все, кто в сети. start() про эту
политику не знает ничего и знать не должен.

Параметры игры живут в RoomSettings, и там же — единственная дверь для
выбора человека (apply). 8.7 требует, чтобы источником истины по
параметрам был сервер: всё присланное сверяется со списком допустимых, и
не сошедшееся просто не применяется.

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
from . import items
from . import proto
from . import world as world_mod

CODE_ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ"   # без I, O, Q — их путают вслух
CODE_LEN = 4
MAX_PLAYERS = 8            # ЖЁСТКИЙ потолок сервера; выбираемый — settings.max_players
RECONNECT_SEC = 120.0      # столько ждём отключившегося, держа его сущность
EMPTY_ROOM_SEC = 300.0     # столько пустая комната живёт перед сносом

# фазы комнаты
LOBBY = "lobby"
PLAYING = "playing"
FINISHED = "finished"

# --- допустимые значения параметров игры (8.7) -----------------------------
# ЭТО И ЕСТЬ «источник истины — сервер». Всё, что пришло с провода, ищется
# в этих списках; не нашлось — параметр не меняется, а комната живёт дальше.

MODES = ("descent", "siege")          # 8.6
# Осада (8.6) делается ВТОРОЙ, после того как Спуск играется, и её кода нет.
# Поэтому она в MODES (чтобы клиент показал её серой), но не в принимаемых:
# принять режим, которого нет, значит выдать пустую комнату за игру.
MODES_READY = ("descent",)

# Сложность — три ступени (8.7). Единственная ручка, которая целиком в этом
# файле: запас здоровья, с которым игрок ВЫХОДИТ в подземелье. Бестиарий
# (ai.py) и урон (combat.py) сложности пока не знают — доложено отдельно.
# Числа выведены от базовых 100 hp (world.spawn_player) и от удара рубаки
# 20 hp (4.2): 140 = семь ударов вместо пяти, 70 = три с половиной.
DIFFS = (0, 1, 2)
DIFF_NAMES = ("Прогулка", "Обычная", "Мясорубка")
DIFF_HP = (140, 100, 70)
DIFF_DEFAULT = 1

PLAYERS_MIN = 2            # 8.7: максимум игроков выбирается в 2..6
PLAYERS_MAX = 6
PLAYERS_DEFAULT = 6

SEED_MIN = 1
SEED_MAX = (1 << 30) - 1

TOKEN_HEX = 16             # 8 байт: 2^64 вариантов, угадать нечего

# --- пинги на карте (8.8) --------------------------------------------------
#
# Пинг — это СОБЫТИЕ (ev, 5.2), а не поле сущности: он ничего не меняет в
# мире и живёт секунды. Но 5.2 прямо говорит, что событие может потеряться,
# а пинг, которого не увидели, — это ровно та молчаливая темнота, ради
# которой 8.8 и написан. Поэтому комната держит короткий СПИСОК живых пингов
# и повторяет их: потеря стоит полсекунды, а не весь пинг.
#
# ЧИСЛА И ИХ ВЫВОД.
#
# PING_LIFE — 120 тиков = 4.0 с. Это время, за которое игрок (7.5 кл/с, 4.2)
#   пересекает кадр шириной 30 клеток (4.1). Пинг «иду сюда», который гаснет
#   раньше, чем до него можно дойти с другого края экрана, бесполезен ровно
#   в своём главном случае.
# PING_GAP — 15 тиков = 0.5 с между ПРИНЯТЫМИ пингами одного игрока. Человек
#   осмысленно пингует реже двух раз в секунду; всё, что чаще, — не речь, а
#   шум. Клиент, жмущий пинг каждый тик (30 Гц), получает 2 принятых и 28
#   отвергнутых в секунду.
# PING_PER_PLAYER — 2 живых пинга на игрока. Своим третьим пингом игрок
#   гасит свой же самый старый: сказать одновременно больше двух вещей
#   нельзя, а экран, засеянный своими же метками, не читается.
# PING_MAX — 8 живых на комнату, старшие вытесняются. Отсюда и трафик:
#   8 пингов x 2 повтора/с x ~100 байт = 1.6 КБ/с на игрока при бюджете
#   200 КБ/с (2.3), то есть 0.8%.
# PING_REPEAT — 15 тиков = 0.5 с. Потерянный повтор стоит 0.5 с из 4.0 с
#   жизни пинга (12%), и за жизнь пинга повторов восемь.
PING_LIFE = 120
PING_GAP = 15
PING_PER_PLAYER = 2
PING_MAX = 8
PING_REPEAT = 15


def _rand_seed():
    return random.randrange(SEED_MIN, SEED_MAX + 1)


class RoomSettings(object):
    """Параметры игры, выбираемые в лобби (8.7). Из них рождается мир.

    ЗДЕСЬ ЖИВЁТ «ИСТОЧНИК ИСТИНЫ — СЕРВЕР». apply() — единственная дверь, в
    которую входит выбор человека, и она сверяет КАЖДОЕ значение со списком
    допустимых. Что не сошлось — не применяется, и комната остаётся с тем,
    что у неё было; ни исключения, ни падения, ни «ну ладно, примем».

    Про seed отдельно. Он всегда КОНКРЕТНОЕ число: генератору нужно число, а
    не «пусто». «Пусто = случайный» (8.7) держится флагом seed_auto: при
    seed_auto=True сид перебрасывается в момент старта, при False — стоит
    ровно тот, который назвали, и забег повторяется дословно.
    """

    __slots__ = ("seed", "seed_auto", "mode", "floor", "map_w", "map_h",
                 "theme", "diff", "ff", "max_players", "join_running")

    def __init__(self, seed=None, mode="descent", floor=1,
                 map_w=gen.ROOM_W, map_h=gen.ROOM_H, theme=None,
                 diff=DIFF_DEFAULT, ff=False, max_players=PLAYERS_DEFAULT,
                 join_running=True):
        self.seed_auto = seed is None
        self.seed = seed if seed is not None else _rand_seed()
        self.mode = mode if mode in MODES_READY else MODES_READY[0]
        self.floor = floor
        self.map_w = map_w
        self.map_h = map_h
        # тема генератора (8.6). Источник истины по параметрам — сервер
        # (8.7): присланное значение проверяется по списку допустимых, а не
        # принимается на веру.
        self.theme = theme if theme in gen.THEMES else gen.DEFAULT_THEME
        self.diff = diff if diff in DIFFS else DIFF_DEFAULT
        self.ff = bool(ff)                     # дружественный огонь, 8.7
        self.max_players = min(PLAYERS_MAX, max(PLAYERS_MIN, int(max_players)))
        self.join_running = bool(join_running)  # вход в идущую игру, 8.5

    # --- проверка присланного (8.7) ---------------------------------------

    def apply(self, opts, players_now=0):
        """Применить то, что прислал хозяин. Возвращает список отвергнутого.

        players_now — сколько человек уже в комнате: опустить потолок ниже
        собравшихся нельзя, иначе комната объявляет часть своих лишними.
        """
        bad = []

        def take(key, fn):
            if key not in opts:
                return
            try:
                ok = fn(opts[key])
            except Exception:
                ok = False
            if not ok:
                bad.append(key)

        def set_mode(v):
            if v in MODES_READY:
                self.mode = v
                return True
            return False

        def set_theme(v):
            if v in gen.THEMES:
                self.theme = v
                return True
            return False

        def set_seed(v):
            # «пусто» — это None или пустая строка, и только они.
            if v is None or (isinstance(v, str) and v == ""):
                self.seed_auto = True
                self.seed = _rand_seed()
                return True
            # bool — подкласс int в питоне; True сидом не бывает.
            if isinstance(v, bool) or not isinstance(v, int):
                return False
            if not (SEED_MIN <= v <= SEED_MAX):
                return False
            self.seed_auto = False
            self.seed = int(v)
            return True

        def set_diff(v):
            if isinstance(v, bool) or not isinstance(v, int) or v not in DIFFS:
                return False
            self.diff = int(v)
            return True

        def set_ff(v):
            if not isinstance(v, bool):
                return False
            self.ff = v
            return True

        def set_max(v):
            if isinstance(v, bool) or not isinstance(v, int):
                return False
            if not (PLAYERS_MIN <= v <= PLAYERS_MAX) or v < players_now:
                return False
            self.max_players = int(v)
            return True

        def set_join(v):
            if not isinstance(v, bool):
                return False
            self.join_running = v
            return True

        take("mode", set_mode)
        take("theme", set_theme)
        take("seed", set_seed)
        take("diff", set_diff)
        take("ff", set_ff)
        take("max_players", set_max)
        take("join_running", set_join)
        return bad

    def player_hp(self):
        """Запас здоровья игрока по сложности (8.7)."""
        return DIFF_HP[self.diff if self.diff in DIFFS else DIFF_DEFAULT]

    def roll_seed(self):
        """Сид на этот забег: при «пусто» — новый случайный (8.7)."""
        if self.seed_auto:
            self.seed = _rand_seed()
        return self.seed

    def describe(self):
        """Плоский вид параметров — он же то, что уходит на провод (8.7)."""
        return {"seed": self.seed, "seed_auto": self.seed_auto,
                "mode": self.mode, "theme": self.theme,
                "diff": self.diff, "ff": self.ff,
                "max_players": self.max_players,
                "join_running": self.join_running,
                "w": self.map_w, "h": self.map_h}


def limits():
    """Что клиенту разрешено предлагать (8.7). Список составляет СЕРВЕР."""
    return {"modes": list(MODES), "modes_ready": list(MODES_READY),
            "themes": gen.theme_names(),
            "diffs": list(DIFFS), "diff_names": list(DIFF_NAMES),
            "players": [PLAYERS_MIN, PLAYERS_MAX],
            "seed": [SEED_MIN, SEED_MAX],
            "ups": list(items.NAMES)}



def new_token():
    """Секрет игрока (5.1). Отдаётся в welcome, возвращается в join."""
    return "%0*x" % (TOKEN_HEX, random.getrandbits(TOKEN_HEX * 4))


class Player(object):
    __slots__ = ("pid", "name", "conn", "ent_id", "room", "ready",
                 "ack", "pending", "needs_full", "gone_at", "bytes_out",
                 "msgs_out", "token", "joined_at", "ping_tick", "pings_dropped")

    def __init__(self, pid, name, conn, token=""):
        self.pid = pid
        self.name = name or ("Игрок%d" % pid)
        self.conn = conn
        # 5.1: опознание по token, а не по имени. Имя — не секрет, его
        # слышно через стол, и двух тёзок оно путает насмерть.
        self.token = token or new_token()
        self.joined_at = time.monotonic()   # по нему переходит право хозяина
        self.ent_id = 0
        self.room = None
        self.ready = False
        self.ack = 0            # последний применённый seq (5.1)
        self.pending = None     # последний пришедший input, применяется в тик
        self.needs_full = True
        self.gone_at = 0.0
        self.bytes_out = 0
        self.msgs_out = 0
        # 8.8: тик последнего ПРИНЯТОГО пинга и счётчик отвергнутых. Живут у
        # игрока, а не у комнаты: спамит один, а платить за это остальные
        # не должны.
        self.ping_tick = -10 ** 9
        self.pings_dropped = 0

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
        # 8.7: хозяин — создатель лобби; при его выходе право переходит
        # следующему по времени входа. Здесь только pid: сам игрок живёт в
        # self.players и может уйти.
        self.host_pid = 0
        # Взведённый старт хозяина. Не «игра идёт», а «хозяин сказал начать»:
        # игра пойдёт, когда к этому сложится готовность всех (see try_start).
        self.armed = False
        # 11.7: последний разосланный набор апгрейдов, id сущности -> кортеж.
        # Нужен ровно затем, чтобы build уходил ПРИ ИЗМЕНЕНИИ, а не 30 раз в
        # секунду: набор меняется десяток раз за забег.
        self._builds = {}
        # 8.8: живые пинги комнаты. Список словарей, не сущности: в мир они
        # не попадают вовсе, и world.py про них не знает ничего.
        self._pings = []
        self.pings_made = 0        # счётчики для приёмки (tests/ping_*.py)
        self.pings_dropped = 0

    @property
    def floor(self):
        return self.settings.floor

    # --- фазы --------------------------------------------------------------

    def start(self):
        """LOBBY -> PLAYING. Единственное место, где рождается мир."""
        if self.phase != LOBBY:
            return False
        s = self.settings
        s.roll_seed()            # 8.7: «пусто» — значит новый сид на забег
        fl = gen.generate(s.seed, s.floor, s.map_w, s.map_h, s.theme)
        self.world = world_mod.World(fl.grid, s.seed, s.floor,
                                     fl.spawns, fl.stairs)
        # 8.1: враги по комнатам, кроме стартовой, числом от глубины этажа
        ai.populate(self.world, fl)
        # 8.4: алтарь этажа — три предмета, берётся один. Стоит рядом с
        # расстановкой врагов и по той же причине: и то и другое — наполнение
        # ЭТАЖА, а не мира, и повторяется при каждом спуске (descend).
        items.populate(self.world, fl)
        hp = s.player_hp()          # 8.7: сложность — запас здоровья игрока
        for i, p in enumerate(self.players.values()):
            e = self.world.spawn_player(p.name, i)
            e.hp_max = hp
            e.hp = hp
            p.ent_id = e.id
            p.needs_full = True
            if not p.online:
                e.flags |= world_mod.F_OFFLINE
        self.phase = PLAYING
        self.armed = False
        self.started_at = time.monotonic()
        self._builds.clear()
        for p in self.players.values():
            if p.online:
                self.send_level(p)
        self.announce_joined()      # фаза сменилась — лобби обязано это знать
        return True

    # --- хозяин лобби (8.7) ------------------------------------------------

    def host(self):
        return self.players.get(self.host_pid)

    def is_host(self, player):
        return player is not None and player.pid == self.host_pid

    def pass_host(self):
        """Право хозяина — следующему по времени входа (8.7).

        Кого считать ушедшим. Разрыв связи держит сущность 120 с
        (RECONNECT_SEC), но лобби на две минуты замирать не должно: без
        хозяина никто не сменит параметр и не нажмёт старт. Поэтому право
        уходит, как только хозяин не в сети, а есть кто-то в сети; вернётся
        он уже обычным игроком. Если в сети нет никого — хозяин остаётся
        прежним, и вернувшись, получает своё обратно.
        """
        h = self.players.get(self.host_pid)
        if h is not None and h.online:
            return self.host_pid
        online = sorted((p for p in self.players.values() if p.online),
                        key=lambda p: p.joined_at)
        if not online:
            return self.host_pid
        self.host_pid = online[0].pid
        self.armed = False      # взвёл старт один, а отвечает за него другой
        return self.host_pid

    # --- политика запуска (8.7) --------------------------------------------

    def set_opts(self, player, opts):
        """Параметры меняет ТОЛЬКО хозяин. Возвращает (можно, отвергнутое)."""
        if not self.is_host(player):
            return False, []
        if self.phase != LOBBY:
            return False, []
        bad = self.settings.apply(opts, players_now=len(self.players))
        self.announce_joined()
        return True, bad

    def set_armed(self, player, v):
        """Кнопка «начать» — только у хозяина (8.7)."""
        if not self.is_host(player) or self.phase != LOBBY:
            return False
        self.armed = bool(v)
        if self.armed:
            player.ready = True    # нажал «начать» — значит готов
        self.announce_joined()
        self.try_start()
        return True

    def try_start(self):
        """Старт по кнопке хозяина, когда готовы все (8.7).

        ПОЧЕМУ КНОПКА ВЗВОДИТСЯ, А НЕ СТРЕЛЯЕТ. Буквальное «хозяин жмёт
        после того, как все отметились» означает лишний круг «ну жми уже»:
        последний готовый ждёт хозяина, хозяин ждал последнего. Здесь кнопка
        ХОЗЯИНА взводит старт, а выстрел происходит в тот момент, когда
        последний отметил готовность. Начать комнату по-прежнему не может
        никто, кроме хозяина: без armed try_start не делает ничего.
        """
        if self.phase != LOBBY or not self.armed:
            return False
        online = [p for p in self.players.values() if p.online]
        if not online or not all(p.ready for p in online):
            return False
        return self.start()

    # Прежнее имя политики. app.py звал её по каждому ready, и смысл тот же:
    # «может, уже пора». Оставлено, чтобы не переучивать вызывающих.
    maybe_start = try_start

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
        """[pid, имя, готов, хозяин] — форма для proto.joined.

        Первые два поля стоят там же, где стояли: на них держится state.js
        и чужие проверки, читающие имена из списка.
        """
        return [[p.pid, p.name, bool(p.ready), p.pid == self.host_pid]
                for p in self.players.values()]

    def find_by_token(self, token):
        """Вернувшийся игрок по token (5.1). Единственное честное опознание."""
        if not token:
            return None
        for p in self.players.values():
            if p.token == token:
                return p
        return None

    def find_offline_by_name(self, name):
        """ЗАПАСНОЙ путь для клиента без token — и дыра 5.1 целиком.

        Имя не секрет и не уникально: два тёзки здесь перепутаются, а чужое
        имя можно назвать. Оставлено только ради клиентов, которые token не
        присылают (свои же питоновские стенды). Как только token пришёл,
        сюда не заходят вовсе — см. add().
        """
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

    def add(self, player, token=""):
        """Вход или переподключение. Возвращает принятого игрока или причину.

        При отказе возвращает строку-код («full», «closed»), а не None: тому,
        кто стучится, надо сказать, ПОЧЕМУ его не взяли.

        Опознание вернувшегося (5.1): сперва token, и только если его не
        прислали вовсе — имя. Клиент, у которого token есть, по имени не
        ищется никогда: именно там жили два тёзки и угаданное чужое имя.
        """
        old = self.find_by_token(token) if token else None
        if old is None and not token:
            old = self.find_offline_by_name(player.name)
        if old is not None:
            if old.online and old is not player:
                # тот же token с двух вкладок: старую связь рвём, иначе две
                # вкладки будут рулить одной сущностью вперемешку
                try:
                    old.conn.close()
                except Exception:
                    pass
            # переподключение: тот же игрок, та же сущность
            old.conn = player.conn
            old.pending = None
            old.needs_full = True
            old.gone_at = 0.0
            if player.name:
                old.name = player.name
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
            self.pass_host()
            self.announce_joined()
            return old
        # 8.5 + 8.7: вход в идущую игру — выбираемый параметр, и выключенный
        # он обязан не пускать, а не пускать молча в пустоту.
        if self.phase == PLAYING and not self.settings.join_running:
            return "closed"
        if self.phase == FINISHED:
            return "closed"
        # 8.7: максимум игроков выбирается в лобби; MAX_PLAYERS — жёсткий
        # потолок сервера, ниже которого выбор опуститься может, а выше нет.
        cap = min(MAX_PLAYERS, self.settings.max_players)
        if len(self.players) >= cap:
            return "full"
        player.room = self
        player.needs_full = True
        self.spawn_for(player)       # в LOBBY вернёт None, и это правильно
        if self.world is not None:
            ent = self.world.entities.get(player.ent_id)
            if ent is not None:
                hp = self.settings.player_hp()
                ent.hp_max = hp
                ent.hp = hp
        self.players[player.pid] = player
        self.empty_since = 0.0
        if not self.host_pid or self.host_pid not in self.players:
            self.host_pid = player.pid      # создатель лобби — хозяин (8.7)
        self.pass_host()
        self.announce_joined()
        return player

    def set_ready(self, player, v):
        """Готовность игрока. Её видят все сразу, и она может начать игру.

        ГОТОВНОСТЬ ХОЗЯИНА — ЭТО И ЕСТЬ ЕГО СОГЛАСИЕ НАЧАТЬ. Иначе в лобби
        появляется состояние «все готовы, включая хозяина, а игра стоит»,
        из которого выход один — объяснять хозяину, что он нажал не ту
        кнопку. Отдельная кнопка «Начать игру» (set_armed) остаётся: она
        взводит старт, не отмечая готовность вручную, и умеет его снять.
        """
        player.ready = bool(v)
        if self.is_host(player):
            self.armed = player.ready
        self.announce_joined()
        self.try_start()
        return player.ready

    def leave(self, player):
        """Уйти в меню по своей воле, а не по обрыву связи."""
        self.drop(player)
        player.room = None
        player.ready = False
        self.pass_host()
        self.announce_joined()

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
        self.pass_host()             # 8.7: хозяин ушёл — право следующему
        self.announce_joined()

    def drop(self, player):
        """Насовсем: убрать игрока и его сущность."""
        self.players.pop(player.pid, None)
        if self.world is not None:
            self.world.remove(player.ent_id)
        if self.count_online() == 0:
            self.empty_since = time.monotonic()

    def expire(self, now):
        gone = False
        for p in list(self.players.values()):
            if not p.online and p.gone_at and now - p.gone_at > RECONNECT_SEC:
                self.drop(p)
                gone = True
        if gone:
            if self.host_pid not in self.players:
                self.host_pid = 0
            self.pass_host()
            self.announce_joined()

    # --- сообщения ---------------------------------------------------------

    def broadcast(self, text):
        for p in self.players.values():
            p.send(text)

    def announce_joined(self):
        """Состояние лобби всем, кто в сети (8.7).

        Шлётся при каждом изменении: вход, выход, готовность, смена
        параметра, смена хозяина, старт. Целиком, а не дельтой: сообщение
        весит две сотни байт и случается по нажатию человека, а не 30 раз в
        секунду. Клиент от этого перестаёт хранить состояние лобби у себя —
        он рисует ровно то, что сказал сервер (8.7: источник истины).
        """
        lst = self.player_list()
        opts = self.settings.describe()
        lim = limits()
        for p in self.players.values():
            if p.online:
                p.send(proto.joined(self.code, p.pid, lst, host=self.host_pid,
                                    opts=opts, limits=lim, armed=self.armed,
                                    phase=self.phase))

    # --- набор апгрейдов (11.7) --------------------------------------------
    #
    # Набор не влезает в десять полей сущности (4.3), а держать его на
    # событии pick клиенту запрещено (5.2): событие теряется под нагрузкой,
    # и HUD «что я собрал» начал бы врать ровно в тот момент, когда на него
    # смотрят. Поэтому — отдельное сообщение build: рядом с полным снапшотом
    # и при каждом изменении набора.

    def _ups_of(self, ent):
        ups = getattr(ent, "ups", None)
        return tuple(ups) if ups else ()

    def builds_for(self, player):
        """Наборы всех игроков — тому, кто получает полный снапшот."""
        w = self.world
        if w is None:
            return 0
        n = 0
        for q in self.players.values():
            ent = w.entities.get(q.ent_id)
            if ent is None:
                continue
            ups = self._ups_of(ent)
            player.send(proto.build(ent.id, ups or [0] * items.N_UP))
            n += 1
        return n

    def broadcast_builds(self):
        """Разослать наборы, которые изменились на этом тике."""
        w = self.world
        if w is None:
            return 0
        n = 0
        for q in self.players.values():
            ent = w.entities.get(q.ent_id)
            if ent is None:
                continue
            ups = self._ups_of(ent)
            if self._builds.get(ent.id) == ups:
                continue
            self._builds[ent.id] = ups
            self.broadcast(proto.build(ent.id, ups or [0] * items.N_UP))
            n += 1
        return n

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
            # 8.8: биты пинга снимаются ЗДЕСЬ и в сущность не попадают.
            # Мир (world/combat) про пинг не знает и знать не должен: пинг
            # ничего не делает в мире, он только сообщение людям. Снятие
            # именно здесь, а не в proto.py, — потому что proto разбирает
            # ФОРМУ сообщения, а «какие биты доходят до тела» это игровое
            # правило и живёт в комнате.
            btn = m["btn"]
            if btn & proto.BTN_PING_ANY:
                self.place_ping(p, e, m["aim"],
                                proto.PING_DANGER
                                if (btn & proto.BTN_PING_DANGER)
                                else proto.PING_HERE)
            e.btn = btn & ~proto.BTN_PING_ANY
            e.aim = m["aim"]

    def tick(self):
        if self.phase != PLAYING:
            return False         # лобби не считается вообще
        self.apply_inputs()
        self.world.step()
        if self.stairs_ready():
            self.descend()       # 8.1: спуск, когда на лестнице ВСЕ живые
        self.broadcast_events()
        self.repeat_pings()      # 8.8: живой пинг повторяется, потеря не фатальна
        self.broadcast_builds()  # 11.7: набор апгрейдов — при изменении
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
        items.populate(self.world, fl)   # 8.4: новый этаж — новый алтарь
        # 8.8: пинги — это координаты ЭТОГО этажа. На новом они указывали бы
        # в случайное место, и «опасность» стояла бы там, где её нет.
        del self._pings[:]
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

    # --- пинги на карте (8.8) ----------------------------------------------
    #
    # ПОЧЕМУ ЭТО СОБЫТИЕ, А НЕ ПОЛЕ СУЩНОСТИ. Пинг ничего не делает в мире:
    # он не двигается, ни с кем не сталкивается, никого не бьёт. Заведи его
    # сущностью — и он поедет в каждом снапшоте десятью полями (4.3) тридцать
    # раз в секунду вместо двух сообщений в секунду, а туман (5.2) обязан был
    # бы его прятать, то есть ровно то, чего пинг не должен делать.
    #
    # ПОЧЕМУ ПИНГ ВИДЕН В ТЕМНОТЕ И В НЕРАЗВЕДАННОМ. 5.2 запрещает клиенту
    # рисовать СУЩНОСТИ на неосвещённых клетках, и запрет этот про одно:
    # сервер знает позиции врагов, а группа их не видит. С пингом всё
    # наоборот — его поставил ЧЕЛОВЕК, руками, из того, что он сам видит.
    # Он не выдаёт того, чего группа не знает; он и есть способ рассказать.
    # Спрятать пинг в темноте значит выключить 8.8 ровно там, где он нужен:
    # «не ходи в тот чёрный коридор» — это сообщение ИМЕННО про темноту.
    #
    # ПОЧЕМУ ПИНГУЕТ И ДУХ. Мёртвый игрок (8.5) не может больше ничего —
    # ни бить, ни светить туман (4.4). Сказать «там босс» он может, и это
    # единственное, что у него осталось.

    def place_ping(self, player, ent, aim, kind):
        """Пинг от игрока. Возвращает True, если он принят.

        Отвергается молча: отказ — это НЕ ошибка протокола, а нормальная
        работа ограничителя, и слать на него err значило бы заваливать
        спамера ответами на его же спам.
        """
        w = self.world
        if w is None:
            return False
        tick = w.tick
        # Ограничитель: не чаще одного пинга в PING_GAP тиков на игрока.
        if tick - player.ping_tick < PING_GAP:
            player.pings_dropped += 1
            self.pings_dropped += 1
            return False
        player.ping_tick = tick
        # Клиенту не верят (5.1): точка обязана лежать на карте. proto режет
        # aim по +-4096, но 4096 — это не «на карте», а «не бесконечность».
        x = min(max(float(aim[0]), 0.0), float(w.grid.w))
        y = min(max(float(aim[1]), 0.0), float(w.grid.h))
        rec = {"tick": tick, "x": proto.r3(x), "y": proto.r3(y),
               "u": int(kind), "a": ent.id, "nm": player.name,
               "sent": tick}
        # Свой третий пинг гасит свой же самый старый, и только потом общий
        # потолок гасит самый старый в комнате. Порядок важен: иначе один
        # болтун вытеснял бы пинги всех остальных.
        mine = [r for r in self._pings if r["a"] == ent.id]
        while len(mine) >= PING_PER_PLAYER:
            self._pings.remove(mine.pop(0))
        while len(self._pings) >= PING_MAX:
            del self._pings[0]
        self._pings.append(rec)
        self.pings_made += 1
        self.broadcast(self._ping_msg(rec))
        return True

    @staticmethod
    def _ping_msg(rec):
        # tick в сообщении — тик ПОСТАНОВКИ, и он же едет в каждом повторе.
        # Отсюда клиент берёт две вещи разом: ключ «это тот же самый пинг»
        # (автор + тик) и сколько пингу осталось жить, — то есть догнавший
        # игру не получит чужой пинг на полные 4 секунды заново.
        return proto.ev(rec["tick"], "ping", a=rec["a"], u=rec["u"],
                        x=rec["x"], y=rec["y"], nm=rec["nm"])

    def expire_pings(self):
        """Убрать отжившие. Возвращает, сколько осталось."""
        w = self.world
        if w is None:
            del self._pings[:]
            return 0
        tick = w.tick
        keep = [r for r in self._pings if tick - r["tick"] < PING_LIFE]
        if len(keep) != len(self._pings):
            self._pings[:] = keep
        return len(self._pings)

    def repeat_pings(self):
        """Повторить живые пинги (5.2: событие может потеряться).

        Повтор идёт РЕДКО и только пока пинг жив, поэтому дельты и снапшота
        он не касается вовсе. Возвращает, сколько сообщений ушло.
        """
        if not self._pings:
            return 0
        self.expire_pings()
        tick = self.world.tick
        n = 0
        for rec in self._pings:
            if tick - rec["sent"] < PING_REPEAT:
                continue
            rec["sent"] = tick
            self.broadcast(self._ping_msg(rec))
            n += 1
        return n

    def pings_for(self, player):
        """Живые пинги — тому, кто получает полный снапшот (вход, спуск).

        Без этого вошедший в идущую партию (5.1) не видит того, что группа
        обсуждает прямо сейчас, до следующего повтора.
        """
        self.expire_pings()
        for rec in self._pings:
            player.send(self._ping_msg(rec))
        return len(self._pings)

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
                self.builds_for(p)      # 11.7: набор рядом с полным снапшотом
                self.pings_for(p)       # 8.8: живые пинги — вошедшему сразу
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
        # партии посередине — а это ровно то, зачем список нужен. 8.7 просит
        # видеть из списка ещё режим и число игроков — отсюда mode и max;
        # join говорит, пустят ли в идущую партию, чтобы человек не стучался
        # в закрытую дверь.
        return [{"id": r.code, "players": r.count_online(), "floor": r.floor,
                 "phase": r.phase, "mode": r.settings.mode,
                 "max": r.settings.max_players,
                 "join": bool(r.settings.join_running)}
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
