# -*- coding: utf-8 -*-
"""Кодирование/декодирование сообщений протокола (DESIGN.md раздел 5).

Единственное место в сервере, где знают о форме сообщений на проводе.
Никакой другой модуль не должен делать json.dumps/json.loads над сетевым
трафиком.

Два правила, которые здесь держатся:

* сущность в снапшоте — массив в порядке полей DESIGN.md 4.3 (ENT_FIELDS);
* клиенту не верят никогда: всё входящее проходит через parse_client(),
  которое возвращает либо чистый словарь с уже нормированными числами,
  либо None. Исключений наружу не летит.
"""

import base64
import json
import math

PROTO = 1
TICK_HZ = 30

# --- порядок полей сущности в снапшоте -------------------------------------
# DESIGN.md 4.3 перечисляет 10 полей, пример в 5.2 показывает 9 (без hp_max).
# Здесь взяты 10 по 4.3: без hp_max клиент не нарисует полоску здоровья.
# Расхождение контракта вынесено в отчёт этапа 0a.
ENT_FIELDS = ("id", "kind", "x", "y", "vx", "vy", "hp", "hp_max", "flags", "facing")

POS_DIGITS = 3   # 5.2: координаты до 3 знаков (0.001 клетки = 0.05 px)
VEL_DIGITS = 2   # 5.2: скорости до 2 знаков

# битовая маска btn (5.1)
BTN_ATTACK = 1
BTN_DASH = 2
BTN_USE = 4
BTN_SHOOT = 8            # дальняя атака
BTN_ITEM = 16            # предмет; бит 16 прежняя маска срезала целиком
# --- пинги на карте (8.8) --------------------------------------------------
# Пинг — это КНОПКА, нажатая в точке прицела, ровно как дальняя атака: точка
# уже едет в том же сообщении полем aim, а частота input (30 Гц) сама по себе
# равна тику. Отдельного сообщения клиента поэтому нет, и это не экономия
# байтов: своё сообщение пришлось бы разбирать вне общего потока ввода, то
# есть вне порядка «последний ввод на тик» (5.1), и спам пингами обходил бы
# ровно тот механизм, который от спама ввода уже защищает.
#
# Битов два, а не один: 8.8 просит различать «иду сюда» и «опасность», и это
# единственная пара, которая требует ПРОТИВОПОЛОЖНЫХ действий (иди туда /
# не ходи туда). Остальные виды из 8.8 («смотри», «помоги») от первого
# отличаются только словом, а не действием группы, — см. отчёт этапа 8.8.
# Третий вид, если понадобится, — это ПОЛЕ, а не третий бит: бит смешал бы
# «что нажато» с «что сказано».
BTN_PING = 32            # «внимание, сюда»
BTN_PING_DANGER = 64     # «опасность»
BTN_PING_ANY = BTN_PING | BTN_PING_DANGER
BTN_MASK = (BTN_ATTACK | BTN_DASH | BTN_USE | BTN_SHOOT | BTN_ITEM |
            BTN_PING | BTN_PING_DANGER)

# Виды пинга на проводе (поле u в ev, 5.2). Числа, а не строки: сравнение в
# клиенте числовое, как у FX_* в state.js.
PING_HERE = 0
PING_DANGER = 1

MAX_NAME = 24
MAX_AIM = 4096.0  # прицел за пределами любой разумной карты — мусор

_SEP = (",", ":")


# --- мелочи ----------------------------------------------------------------

def _r(v, digits):
    """Округление для провода. -0.0 превращается в 0.0: лишний символ."""
    r = round(v, digits)
    return r if r else 0.0


def r3(v):
    return _r(v, POS_DIGITS)


def r2(v):
    return _r(v, VEL_DIGITS)


def _bad_const(name):
    # json.loads по умолчанию радостно принимает NaN/Infinity.
    # NaN в координате отравляет физику навсегда, поэтому — отказ.
    raise ValueError("non-finite constant: %s" % name)


def _num(v, lo=None, hi=None, default=0.0):
    """Число с провода -> конечный float. Всё остальное -> default."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    f = float(v)
    if not math.isfinite(f):          # 1e999 парсится в inf мимо parse_constant
        return default
    if lo is not None and f < lo:
        f = lo
    if hi is not None and f > hi:
        f = hi
    return f


def _int(v, lo=None, hi=None, default=0):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    if isinstance(v, float):
        if not math.isfinite(v):
            return default
        v = int(v)
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return int(v)


def clean_name(v):
    if not isinstance(v, str):
        return ""
    v = "".join(ch for ch in v if ch.isprintable())
    return v.strip()[:MAX_NAME]


def clean_room(v):
    """Код комнаты: ровно 4 латинские буквы. Иначе — пусто (= создать новую)."""
    if not isinstance(v, str):
        return ""
    v = v.strip().upper()
    if len(v) == 4 and all("A" <= c <= "Z" for c in v):
        return v
    return ""


# --- лобби (8.7) -----------------------------------------------------------
# Ключи параметров игры. Список ЗАКРЫТЫЙ: всё, чего здесь нет, с провода не
# проходит вовсе. Допустимые ЗНАЧЕНИЯ проверяет room.RoomSettings.apply —
# это игровое правило, а не форма сообщения, и знать его proto.py нечего.
OPT_KEYS = ("mode", "theme", "seed", "diff", "ff", "max_players", "join_running")

MAX_TOKEN = 32
MAX_OPT_STR = 32


def clean_token(v):
    """token из welcome (5.1). Только [0-9a-f], иначе пусто."""
    if not isinstance(v, str):
        return ""
    v = v.strip().lower()[:MAX_TOKEN]
    if v and all(c in "0123456789abcdef" for c in v):
        return v
    return ""


def clean_opts(v):
    """Параметры лобби с провода -> словарь известных ключей.

    Здесь режется только ФОРМА: незнакомый ключ выбрасывается, строка
    подрезается, нечисловой NaN/inf превращается в None (то есть в «мусор»,
    который дальше будет отвергнут по значению). Никакого «а допустим ли
    режим siege» здесь нет и быть не должно: источник истины по значениям —
    room.py (8.7), и держать этот список в двух местах значит однажды
    разрешить в одном то, что запрещено в другом.
    """
    if not isinstance(v, dict):
        return {}
    out = {}
    for k in OPT_KEYS:
        if k not in v:
            continue
        x = v[k]
        if isinstance(x, str):
            x = x.strip()[:MAX_OPT_STR]
        elif isinstance(x, float) and not math.isfinite(x):
            x = None                      # 1e999/NaN — мусор, а не число
        elif isinstance(x, (list, tuple, dict)):
            x = None
        out[k] = x
    return out


def norm_mv(v):
    """mv с провода -> (dx, dy), длина <= 1. Сервер нормирует сам (5.1)."""
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        return (0.0, 0.0)
    dx = _num(v[0], -1.0, 1.0)
    dy = _num(v[1], -1.0, 1.0)
    d2 = dx * dx + dy * dy
    if d2 > 1.0:
        d = math.sqrt(d2)
        dx /= d
        dy /= d
    return (dx, dy)


# --- входящие (5.1) --------------------------------------------------------

def _v_hello(m):
    return {"t": "hello", "name": clean_name(m.get("name")),
            "ver": _int(m.get("ver"), 0, 1 << 20, 0)}


def _v_rooms(m):
    return {"t": "rooms"}


def _v_join(m):
    # token (5.1): по нему отвалившийся забирает свою сущность. Имя тоже
    # разбирается — это запасной путь для клиента без token; см. room.add.
    return {"t": "join", "room": clean_room(m.get("room")),
            "name": clean_name(m.get("name")),
            "token": clean_token(m.get("token"))}


def _v_ready(m):
    return {"t": "ready", "v": bool(m.get("v"))}


def _v_opts(m):
    return {"t": "opts", "opts": clean_opts(m.get("opts"))}


def _v_start(m):
    # v отсутствует = true: «жми старт», а не «сними старт».
    return {"t": "start", "v": True if m.get("v") is None else bool(m.get("v"))}


def _v_leave(m):
    return {"t": "leave"}


def _v_input(m):
    aim = m.get("aim")
    if isinstance(aim, (list, tuple)) and len(aim) == 2:
        ax = _num(aim[0], -MAX_AIM, MAX_AIM)
        ay = _num(aim[1], -MAX_AIM, MAX_AIM)
    else:
        ax = ay = 0.0
    return {"t": "input",
            "seq": _int(m.get("seq"), 0, 1 << 31, 0),
            "mv": norm_mv(m.get("mv")),
            "aim": (ax, ay),
            "btn": _int(m.get("btn"), 0, 255, 0) & BTN_MASK}


def _v_ping(m):
    return {"t": "ping", "id": _int(m.get("id"), 0, 1 << 31, 0),
            "ct": _num(m.get("ct"), -1e12, 1e12, 0.0)}


_VALIDATORS = {
    "hello": _v_hello, "rooms": _v_rooms, "join": _v_join,
    "ready": _v_ready, "input": _v_input, "ping": _v_ping,
    "opts": _v_opts, "start": _v_start, "leave": _v_leave,
}


def parse_client(raw):
    """Разбор сообщения клиента. Мусор -> None, наружу ничего не летит."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        msg = json.loads(raw, parse_constant=_bad_const)
        if not isinstance(msg, dict):
            return None
        fn = _VALIDATORS.get(msg.get("t"))
        if fn is None:
            return None
        return fn(msg)
    except Exception:
        return None


# --- исходящие (5.2) -------------------------------------------------------

def encode(msg):
    """dict -> строка на провод."""
    return json.dumps(msg, separators=_SEP, ensure_ascii=False)


def welcome(pid, token=""):
    m = {"t": "welcome", "pid": pid, "tick_hz": TICK_HZ, "proto": PROTO}
    if token:
        m["token"] = token
    return encode(m)


def roomlist(rooms):
    return encode({"t": "roomlist", "rooms": rooms})


def joined(room, pid, players, host=0, opts=None, limits=None, armed=False,
           phase=""):
    """Состояние лобби целиком (8.7): кто внутри, кто хозяин, какие параметры.

    Шлётся при КАЖДОМ изменении лобби, а не только при входе: параметры
    меняет хозяин, а видят их сразу все, и догонять это дельтами нечего —
    сообщение весит две сотни байт и случается по нажатию человека.

    players — [pid, имя, готов, хозяин]. Первые два поля на прежних местах:
    на них стоят state.js и чужие проверки.
    """
    m = {"t": "joined", "room": room, "pid": pid, "players": players}
    if host:
        m["host"] = host
    if opts is not None:
        m["opts"] = opts
    if limits is not None:
        m["limits"] = limits
    if armed:
        m["armed"] = True
    if phase:
        m["phase"] = phase
    return encode(m)


def build(eid, ups):
    """Набор апгрейдов сущности (11.7).

    Отдельным сообщением, потому что в десять полей 4.3 набор не влезает, а
    держать его на событии pick клиенту запрещено 5.2: событие теряется.
    ups — позиционный список счётчиков по видам апгрейда (items.N_UP).
    """
    return encode({"t": "build", "id": eid, "ups": list(ups)})


def level(floor, seed, w, h, tiles):
    return encode({"t": "level", "floor": floor, "seed": seed,
                   "w": w, "h": h, "tiles": rle_encode(tiles)})


def ev(tick, kind, **kw):
    m = {"t": "ev", "tick": tick, "k": kind}
    m.update(kw)
    return encode(m)


def pong(pid_msg_id, ct, st):
    return encode({"t": "pong", "id": pid_msg_id, "ct": ct, "st": st})


def error(code, text):
    return encode({"t": "err", "code": code, "msg": text})


def encode_entity(e):
    """Сущность -> массив в порядке ENT_FIELDS."""
    return [e.id, e.kind, r3(e.x), r3(e.y), r2(e.vx), r2(e.vy),
            e.hp, e.hp_max, e.flags, r2(e.facing)]


# --- снапшот: один на комнату, ack — на игрока -----------------------------
# 4.4 требует сериализовать снапшот один раз на комнату, а 5.2 кладёт в него
# поле ack, которое у каждого игрока своё. Противоречие снимается так: тяжёлую
# часть (сущности) сериализуем один раз в "хвост", а лёгкую голову с ack
# приклеиваем строкой на каждого игрока. Форма сообщения на проводе — ровно
# как в 5.2, сериализация — одна на комнату.

def snap_tail(tick, full, entities, rm=None, vis=None):
    body = {"tick": tick, "full": bool(full), "e": entities}
    if rm:
        body["rm"] = rm
    if vis is not None:
        body["vis"] = vis
    return encode(body)[1:]   # без открывающей '{'


def snap_with_ack(tail, ack):
    return '{"t":"snap","ack":%d,%s' % (ack, tail)


def snap(tick, ack, full, entities, rm=None, vis=None):
    """Полное сообщение снапшота одним куском (для тестов и одиночных отправок)."""
    return snap_with_ack(snap_tail(tick, full, entities, rm, vis), ack)


# --- RLE тайлов ------------------------------------------------------------

def rle_encode(tiles):
    """bytes -> base64 от пар (значение, длина<=255)."""
    out = bytearray()
    i = 0
    n = len(tiles)
    while i < n:
        v = tiles[i]
        j = i + 1
        while j < n and tiles[j] == v and (j - i) < 255:
            j += 1
        out.append(v)
        out.append(j - i)
        i = j
    return base64.b64encode(bytes(out)).decode("ascii")


def rle_decode(s):
    raw = base64.b64decode(s)
    out = bytearray()
    for i in range(0, len(raw) - 1, 2):
        out.extend(bytes([raw[i]]) * raw[i + 1])
    return bytes(out)
