# -*- coding: utf-8 -*-
"""Состояние мира: тик, реестр сущностей, снапшоты (DESIGN.md 4, 5.2).

Мир не знает ни про сеть, ни про рендер. Он умеет: шагнуть на тик,
завести/убрать сущность и отдать снапшот — полный или дельту.

Дельта считается сравнением с тем, что уже отправлено, причём сравниваются
УЖЕ ОКРУГЛЁННЫЕ массивы (proto.encode_entity). Поэтому дрожание ниже
точности округления (5.2) трафика не создаёт.
"""

import math

from . import nav
from . import physics
from . import proto
from . import vis as vis_mod

TICK_HZ = 30
DT = 1.0 / TICK_HZ

# kind
K_PLAYER = 1
K_ENEMY = 2           # рубака: прёт в ближний бой (4.2)
K_PROP = 3
K_SHOT = 4
# 4.2: виды врага различаются ЗНАЧЕНИЕМ kind, а не битом в flags. Рубака и
# стрелок ведут себя противоположно (один прёт в упор, другой держит полосу
# 5-9 клеток и пятится), и игрок обязан видеть разницу до того, как получит
# урон. Бит в flags смешал бы «кто это» с «что он сейчас делает»: flags
# меняются каждый замах, вид — никогда. Цена на проводе нулевая: kind в
# снапшоте уже есть (4.3), это тот же один символ.
K_ENEMY_RANGED = 5    # стрелок: держит дистанцию и стреляет (4.2)

# Все вражеские виды одним местом: команда, расталкивание и ИИ спрашивают
# «враг ли это», а не перечисляют номера.
ENEMY_KINDS = (K_ENEMY, K_ENEMY_RANGED)

# flags (4.3). DEAD и OFFLINE — из контракта; WINDUP и DASH добавлены на
# этапе 2a и вынесены в отчёт как правка 4.3. Почему битом, а не событием:
# 5.2 запрещает держать состояние на ev, а замах игрок обязан ВИДЕТЬ все
# 0.25 с подряд — потерянный пакет превратил бы его в удар из ниоткуда.
# Цена нулевая: поле flags уже в снапшоте, значения 0..15 — тот же один-два
# символа.
F_DEAD = 1
F_OFFLINE = 2
F_WINDUP = 4        # идёт замах ближней атаки (4.2)
F_DASH = 8          # идёт рывок, урон не проходит (4.2)

# 4.2: скорости в клетках в секунду
SPEED_RUN = 5.0
SPEED_SHOT = 12.0

R_PLAYER = 0.35   # радиус игрока: проходит в проём в одну клетку

# --- громкость действий (8.2), в КЛЕТКАХ ПУТИ, а не по прямой -------------
# Шум считается волной по полу (nav.noise_wave), поэтому «18» — это 18
# клеток обхода стен, а не круг радиуса 18.
#
# ОТКУДА ЧИСЛА. Комната генератора — 4..11 клеток стороной (gen.Theme), её
# диагональ не больше 15; коридор между центрами соседних комнат — десятки
# клеток. Отсюда три уровня, а не три вкуса:
#   * выстрел 18 — заведомо больше комнаты: слышно ЗА дверью, в соседней;
#     это и есть «дальний бой громкий» (8.2), и это цена стрельбы;
#   * ближняя атака 6 — заведомо меньше комнаты: слышно тех, кто и так
#     рядом, за дверь не уходит;
#   * рывок 2 — соседние клетки, «почти беззвучно» (8.2).
# 18 — ещё и то число, на котором 8.3 замерил цену волны: 0.046 мс.
NOISE_SHOT = 18
NOISE_MELEE = 6
NOISE_DASH = 2


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


# Импорт боя стоит НИЖЕ констант намеренно. combat.py читает отсюда TICK_HZ и
# SPEED_SHOT (числа 4.1/4.2 живут в одном месте, а не в двух), а world.py
# вызывает combat в тике — импорт взаимный. В Python 3.11 он работает в обе
# стороны ровно при одном условии: к моменту, когда исполняется тело
# combat.py, нужные ему имена в world уже определены. Отсюда и место строки.
# Обратный порядок (import combat первым) тоже проверен: тогда world
# выполняется целиком, а combat получает уже готовый модуль.
from . import combat        # noqa: E402
# items.py читает отсюда kind/flags и ничего больше; combat.py читает items.
# Порядок тот же и по той же причине: к моменту исполнения тела items.py
# константы world уже определены.
from . import items          # noqa: E402
# ai.py читает combat и world; стоит ниже combat по той же причине,
# что и combat: к этому моменту оба нужных ему модуля уже готовы.
from . import ai            # noqa: E402


class Entity(object):
    # Первые десять полей — контрактные (4.3), они и уходят в снапшот.
    # Остальное серверное: в снапшот не попадает никогда.
    __slots__ = ("id", "kind", "x", "y", "vx", "vy", "hp", "hp_max",
                 "flags", "facing", "r", "speed", "ctl", "mv", "btn",
                 "aim", "name", "ttl", "team",
                 # бой (combat.py): все времена — АБСОЛЮТНЫЕ номера тиков,
                 # а не счётчики. Счётчик надо уменьшать ровно один раз за
                 # тик и ровно в одном месте, иначе неуязвимость рывка
                 # съезжает на тик в ту или другую сторону; сравнение с
                 # world.tick съехать не может.
                 "atk_hit", "atk_ready", "dash_end", "dash_ready",
                 "dash_dx", "dash_dy", "inv_end", "shot_ready", "shot_q",
                 "owner", "dmg",
                 # ИИ (ai.py). На проводе их нет: 4.3 разрешает ровно десять
                 # полей, и вид врага в них не влезает.
                 "ai", "ai_alert", "ai_wind",
                 # предметы и апгрейды (items.py, 8.4). Тоже серверные: набор
                 # апгрейдов в десять полей 4.3 не влезает, см. items.py.
                 "ups", "alt", "bounce", "dash_hit")

    def __init__(self, eid, kind, x, y):
        self.id = eid
        self.kind = kind
        self.x = float(x)
        self.y = float(y)
        self.vx = 0.0
        self.vy = 0.0
        self.hp = 100
        self.hp_max = 100
        self.flags = 0
        self.facing = 0.0
        self.r = 0.3
        self.speed = 0.0
        self.ctl = False          # управляется вводом игрока
        self.mv = (0.0, 0.0)
        self.btn = 0
        self.aim = (0.0, 0.0)
        self.name = ""
        self.ttl = 0
        # команда: 1 игроки, 2 враги, 0 нейтрал (реквизит). Бьют друг друга
        # только разные команды, поэтому дружественного огня нет по
        # построению, а не по проверке "если это игрок".
        self.team = 1 if kind == K_PLAYER else (2 if kind in ENEMY_KINDS
                                                else 0)
        self.atk_hit = 0        # тик, на котором прилетит удар (0 — нет замаха)
        self.atk_ready = 0      # тик, с которого можно бить снова
        self.dash_end = 0       # последний тик рывка (0 — не в рывке)
        self.dash_ready = 0     # тик, с которого можно рвануть снова
        self.dash_dx = 0.0
        self.dash_dy = 0.0
        self.inv_end = 0        # последний тик неуязвимости
        self.shot_ready = 0
        self.shot_q = 0         # выстрел заказан, родится в combat.resolve
        self.owner = 0          # для снаряда: чей
        self.dmg = 0            # для снаряда: сколько снимает
        self.ai = 0             # вид поведения (ai.AI_*), 0 — ИИ нет
        self.ai_alert = 0       # тик, до которого враг гонится без видимости
        self.ai_wind = 0        # тик выстрела врага (0 — замаха нет)
        self.ups = None         # апгрейды владельца: None или [N_UP] счётчиков
        self.alt = 0            # для предмета: номер алтаря (8.4)
        self.bounce = 0         # для снаряда: сколько отскоков осталось
        self.dash_hit = None    # кого уже задел ЭТОТ рывок (Таран)


class World(object):
    def __init__(self, grid, seed=1, floor=1, spawns=None, stairs=None):
        self.grid = grid
        self.seed = seed
        self.floor = floor
        self.spawns = spawns or [(2.5, 2.5)]
        self.stairs = stairs          # (tx,ty) лестницы вниз (8.1)
        # Туман — ОДИН на комнату (4.4): считается здесь, в снапшот уходит
        # одной строкой на всех, а не на каждого игрока (11.2).
        self.fog = vis_mod.Fog(grid)
        self.tick = 0
        self.entities = {}
        self._next_id = 1
        self._sent = {}        # id -> последний отправленный массив
        self._removed = []     # id, удалённые с прошлой дельты
        self.events = []       # (tick, kind, kw) -> proto.ev, вынимает комната
        # Карта расстояний до живых игроков (8.3). ОДНА на этаж и на всех
        # врагов: пересчёт раз в nav.NAV_PERIOD тиков, см. server/nav.py.
        self.nav = nav.Field(grid)
        self.noises = []       # (tx, ty, громкость, команда) — шум этого тика
        self.noise_waves = 0   # сколько волн шума посчитано за жизнь мира
        # очередь подборов предмета (8.4): кнопку читает combat.begin ИЗ
        # цикла по сущностям, а подбор меняет состав мира — поэтому очередь,
        # как у снарядов (shot_q) и у шума.
        self.pickups = []
        self.altar_seq = 0     # номер алтаря: растёт, не переиспользуется

    # --- реестр ------------------------------------------------------------

    def new_id(self):
        i = self._next_id
        self._next_id += 1     # id не переиспользуются (4.3)
        return i

    def spawn(self, kind, x, y, **kw):
        e = Entity(self.new_id(), kind, x, y)
        for k, v in kw.items():
            setattr(e, k, v)
        self.entities[e.id] = e
        return e

    def spawn_player(self, name="", idx=0):
        sx, sy = self.spawns[idx % len(self.spawns)]
        sx, sy = physics.free_spot(self.grid, sx, sy, R_PLAYER)
        return self.spawn(K_PLAYER, sx, sy, r=R_PLAYER, speed=SPEED_RUN,
                          ctl=True, name=name, hp=100, hp_max=100)

    def remove(self, eid):
        if self.entities.pop(eid, None) is not None:
            if self._sent.pop(eid, None) is not None:
                self._removed.append(eid)

    # --- тик ---------------------------------------------------------------

    def _crowd_push(self, dt):
        """Смещения расталкивания на этот тик: id -> (px, py), или None.

        Считается ДО прохода по сущностям и по их положению на начало тика:
        иначе тело, которое шагнуло первым, толкало бы соседа сильнее, чем
        сосед его, и толпа поехала бы в сторону порядка обхода словаря.

        Расталкиваются ВРАГИ МЕЖДУ СОБОЙ. Игроки — нет, и это осознанно:
        толкать игрока телом врага значит менять бой (из-под замаха
        выталкивало бы само), а слипание, которое лечится, — про толпу
        врагов. Снаряды — нет: у них та же команда 2, но прямая линия
        входит в их смысл (4.2a), поэтому отбор идёт по kind, а не по team.
        """
        xs = []
        ys = []
        rs = []
        steps = []
        ids = []
        for e in self.entities.values():
            if e.kind not in ENEMY_KINDS or (e.flags & F_DEAD):
                continue
            xs.append(e.x)
            ys.append(e.y)
            rs.append(e.r)
            steps.append(e.speed * dt)
            ids.append(e.id)
        if len(xs) < 2:
            return None
        res = physics.separate(xs, ys, rs, steps)
        if not res:
            return None
        return dict((ids[i], v) for i, v in res.items())

    def step(self, dt=DT):
        self.tick += 1
        grid = self.grid
        # ИИ врагов — ДО движения: он выставляет vx/vy, которые физика ниже
        # и отрабатывает (вместе с подталкиванием на углах 4.2a, без которого
        # враг встаёт в первом же проёме — 8.3). Состав мира ai.step не
        # меняет, поэтому словарь под ним не шевелится.
        ai.step(self, dt)
        # тела друг для друга больше не проницаемы (physics.separate)
        pushes = self._crowd_push(dt)
        gone = None
        for e in self.entities.values():
            # кнопки и боевые таймеры — до движения: рывок выставляет скорость
            if e.ctl or e.atk_hit or e.dash_end:
                combat.begin(self, e)
            if e.ctl:
                if e.dash_end:
                    pass                      # скорость рывка выставил combat
                else:
                    e.vx = e.mv[0] * e.speed
                    e.vy = e.mv[1] * e.speed
                if not e.atk_hit:
                    # на замахе направление заморожено: дуга ударит ровно там,
                    # где клиент её рисовал все 8 тиков (4.2)
                    ax, ay = e.aim
                    if ax or ay:
                        e.facing = math.atan2(ay - e.y, ax - e.x)
                    elif e.vx or e.vy:
                        e.facing = math.atan2(e.vy, e.vx)
            if e.ttl:
                e.ttl -= 1
                if e.ttl <= 0:
                    # убирать ВНУТРИ цикла нельзя: словарь меняется во время
                    # итерации. До снарядов ttl никто не ставил, и это не
                    # стреляло; со снарядами стрельнуло бы в первую же минуту.
                    if gone is None:
                        gone = []
                    gone.append(e.id)
                    continue
            pmv = pushes.get(e.id) if pushes else None
            if e.vx or e.vy or pmv:
                if e.ctl and (e.flags & F_DEAD):
                    # дух летает (8.5): стены ему не преграда, зато он не
                    # светит туман (viewers) и не трогает бой
                    e.x = _clamp(e.x + e.vx * dt, 0.5, grid.w - 0.5)
                    e.y = _clamp(e.y + e.vy * dt, 0.5, grid.h - 0.5)
                    continue
                # 4.2a: снаряду подталкивание на углах ВЫКЛЮЧЕНО —
                # прямая линия входит в смысл снаряда
                nx, ny, hit = physics.move_circle(grid, e.x, e.y,
                                                  e.vx * dt, e.vy * dt, e.r,
                                                  e.kind != K_SHOT, pmv)
                e.x = nx
                e.y = ny
                if hit:
                    if e.kind == K_SHOT:
                        if e.bounce > 0:
                            # Рикошет (8.4): снаряд не гибнет о стену, а
                            # отражается по той оси, которую стена и
                            # заблокировала (маска hit: 1 по X, 2 по Y —
                            # physics.move_circle). Дальность от этого не
                            # растёт: ttl не трогаем, живёт снаряд те же
                            # 14 клеток пути, просто ломаными.
                            e.bounce -= 1
                            if hit & 1:
                                e.vx = -e.vx
                            if hit & 2:
                                e.vy = -e.vy
                            # facing нужен клиенту, чтобы рисовать снаряд по
                            # направлению полёта, а оно только что сменилось
                            e.facing = math.atan2(e.vy, e.vx)
                            continue
                        if gone is None:      # снаряд гибнет о стену (4.2a)
                            gone = []
                        gone.append(e.id)
                        # точка гибели уходит событием: клиенту — искра о
                        # стену, проверке — число, которое иначе не увидеть
                        # (сущности к концу тика уже нет)
                        self.event("boom", a=e.owner, b=e.id,
                                   x=proto.r3(e.x), y=proto.r3(e.y))
                        continue
                    if hit & 1:
                        e.vx = 0.0
                    if hit & 2:
                        e.vy = 0.0
        if gone:
            for eid in gone:
                self.remove(eid)
        combat.resolve(self, dt)
        self.fog.update(self.viewers())
        return self.tick

    # --- события (5.2, сообщение ev) ---------------------------------------

    MAX_EVENTS = 64

    def event(self, kind, **kw):
        """Сложить событие; вынимает и рассылает комната, раз в тик.

        ОТКУДА ПОТОЛОК. Список вычерпывается каждый тик, значит потолок — это
        "сколько ev уходит клиенту за тик". Одно событие на проводе ~70 байт,
        64 события = 4.5 КБ в тик = 134 КБ/с сырых поверх снапшота — при
        бюджете 2.3 в 200 КБ/с это влезает только со сжатием, и это уже
        потолок, а не рабочий режим. Реальный худший случай считается так:
        6 игроков бьют раз в 14 тиков плюс 200 врагов этапа 2b с замахом
        0.5 с — это 13 событий в тик, запас к потолку впятеро.
        Вторая работа потолка — предохранитель: мир можно шагать и без
        комнаты (стенд, проверка), тогда вынимать события некому, и без
        потолка список рос бы весь прогон. Терять ev контракт разрешает
        прямым текстом (5.2), состояния на них нет.
        """
        if len(self.events) < self.MAX_EVENTS:
            self.events.append((self.tick, kind, kw))

    # --- шум (8.2) ---------------------------------------------------------

    MAX_NOISES = 32

    def make_noise(self, x, y, loud, team=0):
        """Сложить шум; разбирает ai.step в начале СЛЕДУЮЩЕГО тика.

        Отложенность намеренная и стоит ровно один тик (33 мс): шум рождается
        внутри прохода по сущностям (combat.begin) и внутри combat.resolve,
        а волну там считать нельзя — она должна быть ОДНА на все источники
        одной громкости, а не по волне на каждый выстрел (8.3).

        Потолок — предохранитель, как у событий: мир можно шагать и без ИИ
        (стенд, проверка), тогда список никто не вычерпывает.
        """
        if len(self.noises) < self.MAX_NOISES:
            self.noises.append((int(x), int(y), int(loud), int(team)))

    # --- смена этажа (8.1) -------------------------------------------------

    def enter_floor(self, floor, grid, spawns, stairs):
        """Новый этаж в том же мире: id не переиспользуются (4.3).

        Мир не пересоздаётся именно поэтому: новый World начал бы нумерацию
        сущностей с единицы, и id стал бы уникален в пределах ЭТАЖА, а не
        забега, как требует 4.3.
        """
        self.floor = floor
        self.grid = grid
        self.spawns = spawns or [(2.5, 2.5)]
        self.stairs = stairs
        self.nav.drop()          # карта старого этажа больше ничего не значит
        del self.noises[:]
        # предметы старого этажа убираются ниже вместе с врагами, поэтому и
        # очередь подбора к ним больше не относится
        del self.pickups[:]
        if grid.w * grid.h == self.fog.n:
            self.fog.grid = grid
            self.fog.forget_all()     # 4.4: память группы забывается целиком
        else:
            self.fog = vis_mod.Fog(grid)
        i = 0
        for e in list(self.entities.values()):
            if e.kind != K_PLAYER:
                self.remove(e.id)     # враги и снаряды остались этажом выше
                continue
            sx, sy = self.spawns[i % len(self.spawns)]
            i += 1
            e.x, e.y = physics.free_spot(grid, sx, sy, e.r)
            e.vx = e.vy = 0.0
            e.mv = (0.0, 0.0)
            e.btn = 0
            if e.flags & F_DEAD:
                # 8.5: воскресает на следующем этаже бесплатно
                e.flags &= ~F_DEAD
                e.hp = e.hp_max
            # e.ups здесь НЕ трогается, и это не забывчивость: 8.4 держит
            # апгрейды внутри забега, а забег — это все этажи. Здоровье
            # переносится по 8.5, накопленное — по той же причине. Смерть
            # набор тоже не отнимает: дух воскресает со своими апгрейдами.
            combat.reset(e)
        # всем уйдёт полный снапшот, дельте не от чего отсчитываться
        self._sent = {}
        self._removed = []
        return self.floor

    def viewers(self):
        """Кто светит в туман: живые игроки (4.4). Мёртвый не разведчик."""
        out = []
        for e in self.entities.values():
            if e.kind == K_PLAYER and not (e.flags & F_DEAD):
                out.append((e.id, e.x, e.y))
        return out

    # --- снапшоты ----------------------------------------------------------

    def snapshot_full(self, remember=True):
        """Полный снапшот: хвост сообщения (без головы с ack)."""
        enc = proto.encode_entity
        rows = []
        sent = {} if remember else None
        for e in self.entities.values():
            a = enc(e)
            rows.append(a)
            if remember:
                sent[e.id] = a
        if remember:
            self._sent = sent
            self._removed = []
        # входящему нужен весь туман целиком, а не изменения
        return proto.snap_tail(self.tick, True, rows, None,
                               self.fog.full_encoded())

    def snapshot_delta(self):
        """Дельта: изменившиеся сущности + список удалённых rm (5.2)."""
        enc = proto.encode_entity
        sent = self._sent
        rows = []
        for eid, e in self.entities.items():
            a = enc(e)
            if sent.get(eid) != a:
                rows.append(a)
                sent[eid] = a
        rm = self._removed
        self._removed = []
        # дельта тумана — одна на комнату; None, если туман не менялся
        return proto.snap_tail(self.tick, False, rows, rm,
                               self.fog.delta_encoded())

    def alive_players(self):
        """Живые игроки, которых ждут на лестнице (8.1).

        Отвалившийся не в счёт: 8.5 превращает его в камень, а камень не
        ходит. Иначе один упавший интернет держал бы группу 120 с.
        """
        out = []
        for e in self.entities.values():
            if e.kind != K_PLAYER:
                continue
            if e.flags & (F_DEAD | F_OFFLINE):
                continue
            out.append(e)
        return out

    def on_stairs(self, e):
        """Стоит ли сущность на клетке лестницы (тайл 2, 4.1)."""
        st = self.stairs
        if st is None:
            return False
        return int(e.x) == st[0] and int(e.y) == st[1]

    def stats(self):
        return {"tick": self.tick, "entities": len(self.entities)}
