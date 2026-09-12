"""Константы симуляции. Всё в пикселях логической арены и секундах."""

TICK = 60
DT = 1.0 / TICK
SNAP_EVERY = 2  # снапшот каждые N тиков -> 30 Гц

# --- физика ---
GRAVITY = 2100.0
MAX_FALL = 1500.0
FASTFALL_MUL = 1.8

RUN_SPEED = 330.0
GROUND_ACCEL = 3800.0
AIR_ACCEL = 1700.0
GROUND_FRICTION = 4400.0
AIR_FRICTION = 420.0
JUMP_VEL = 800.0
DJUMP_VEL = 730.0

FW = 44.0  # ширина бойца
FH = 72.0  # высота бойца

DASH_SPEED = 960.0
DASH_TIME = 0.16
DASH_CD = 2.2
DASH_IFRAMES = 0.14

# --- бой ---
KB_RAGE = 0.9          # множитель отброса при 0 HP
HITSTUN_PER_KB = 0.00058
HITSTUN_MIN = 0.06
HITSTUN_MAX = 0.85
HIT_IFRAMES = 0.05     # короткая неуязвимость после удара, чтобы не было мультихитов

ULT_MAX = 100.0
ULT_DEALT = 0.55
ULT_TAKEN = 0.40
ULT_PER_SEC = 1.6

# --- матч ---
COUNTDOWN = 3.0
RESPAWN_TIME = 2.6
SPAWN_INVULN = 1.6
POST_MATCH = 8.0
DEFAULT_STOCKS = 3
MATCH_TIME_LIMIT = 300.0

# состояния матча
ST_COUNTDOWN = "countdown"
ST_PLAY = "play"
ST_OVER = "over"

# биты ввода
IN_LEFT = 1
IN_RIGHT = 2
IN_JUMP = 4
IN_DOWN = 8
IN_BASIC = 16
IN_Q = 32
IN_E = 64
IN_R = 128
IN_DASH = 256
