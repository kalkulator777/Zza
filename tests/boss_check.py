#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка босса (DESIGN.md 8.1) и числа алтарей по живым (8.4).

Что здесь проверяется и почему именно так.

  1. БОСС РОВНО НА КАЖДОМ ПЯТОМ ЭТАЖЕ И БОЛЬШЕ НИГДЕ. Прогон по 20 этажам
     на нескольких сидах, считается сущность с kind = K_BOSS в настоящем
     мире, а не флаг в генераторе. Рядом — где он стоит и кто ещё в арене.
  2. БОССА ВИДНО ДО УДАРА. То же, что для рядового врага в ai_check
     (раздел 6), но для ОБЕИХ атак: сколько тиков проходит между появлением
     бита F_WINDUP и уроном, и уходит ли игрок, нажавший рывок по биту.
  3. ДВЕ АТАКИ ТРЕБУЮТ РАЗНОГО ОТВЕТА. Таблица 2x2: «Обвал» и «Залп»
     против «отступить» и «уйти вбок». Красное здесь — это когда один и тот
     же ответ годится на обе атаки: тогда бой одномерный, и вторая атака
     ничего не добавляет. Меряется УРОН, а не намерения.
  4. БОСС НЕ ЛОМАЕТ ФИЗИКУ. Тело у него вдвое больше по площади (r 0.50
     против 0.35), поэтому проверяется прямо: проходит ли он проём в одну
     клетку (карта своя, маленькая — правило 10), не выталкивает ли игрока
     сквозь стену (смотрится клетка, в которую въезжает ЦЕНТР игрока), и
     доходит ли он до игрока на настоящих этажах с боссом.
  5. БОССА МОЖНО УБИТЬ. Скриптовый игрок, который умеет ровно то, что
     проверено в п.3, дерётся до конца; печатается длительность и остаток
     здоровья. Число тут — это ответ на «400 hp — это сколько секунд».
  6. ЛЕСТНИЦА ЗАПЕРТА, ПОКА БОСС ЖИВ (8.1: босс — вершина забега).
  7. АЛТАРЕЙ СТОЛЬКО, СКОЛЬКО ЖИВЫХ (8.4). Настоящая комната, настоящие
     игроки: один, двое, четверо.

Запуск:  python3 tests/boss_check.py
Выход: 0 — зелено, 1 — красно.
"""

import math
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import ai, boss, combat, gen, items, nav, physics, proto  # noqa: E402
from server import vis as vis_mod                                  # noqa: E402
from server import world as world_mod                              # noqa: E402
from server.room import Player, Room, RoomSettings                 # noqa: E402

W = world_mod
FAILS = []
CLOCK = time.perf_counter

BTN_DASH = proto.BTN_DASH
BTN_ATTACK = proto.BTN_ATTACK


def check(ok, title, detail=""):
    print(("  ЗЕЛЕНО  " if ok else "  КРАСНО  ") + title +
          (("  | " + detail) if detail else ""))
    if not ok:
        FAILS.append(title)
    return ok


def note(title, detail=""):
    print("  данные   " + title + (("  | " + detail) if detail else ""))


def uptime_line():
    try:
        return subprocess.run(["uptime"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:               # noqa: BLE001
        return "uptime недоступен"


# --- свои карты (правило 10) ----------------------------------------------

def hall(w, h):
    g = physics.Grid(w, h)
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            g.set(x, y, physics.TILE_FLOOR)
    return g


def split_hall(w=26, h=12, wx=13, door_y=5):
    g = hall(w, h)
    for y in range(1, h - 1):
        g.set(wx, y, physics.TILE_WALL)
    g.set(wx, door_y, physics.TILE_FLOOR)
    return g


def make_world(grid):
    return W.World(grid, seed=1, floor=1, spawns=[(2.5, 2.5)])


def add_player(w, x, y, hp=100):
    return w.spawn(W.K_PLAYER, x, y, r=W.R_PLAYER, speed=W.SPEED_RUN,
                   ctl=True, hp=hp, hp_max=hp)


def wake(e, w, ticks=10 ** 6):
    e.ai_alert = w.tick + ticks
    return e


class _Conn(object):
    def __init__(self):
        self.msgs = []

    def send_str(self, text):
        self.msgs.append(text)


# --- 1. ровно каждый пятый этаж -------------------------------------------

def t_every_fifth():
    print("\n--- 1. босс ровно на каждом пятом этаже (8.1) ---")
    note("правило", "boss.on_floor: этаж %% %d == 0" % boss.BOSS_EVERY)
    seeds = (11, 4242, 777, 90210)
    rows = []
    bad = []
    for seed in seeds:
        line = []
        for floor_n in range(1, 21):
            fl = gen.generate(seed, floor_n, 64, 48)
            w = W.World(fl.grid, seed=seed, floor=floor_n,
                        spawns=fl.spawns, stairs=fl.stairs)
            ai.populate(w, fl)
            n = sum(1 for e in w.entities.values() if e.kind == W.K_BOSS)
            line.append(n)
            want = 1 if floor_n % 5 == 0 else 0
            if n != want:
                bad.append((seed, floor_n, n, want))
        rows.append((seed, line))
    for seed, line in rows:
        note("сид %d, этажи 1..20" % seed,
             " ".join(("Б" if n else ".") for n in line)
             + "   (боссов всего %d)" % sum(line))
    check(not bad, "на этажах 5,10,15,20 — ровно один босс, на прочих ни одного",
          "расхождений %d по %d этажам (%d сидов x 20)%s"
          % (len(bad), 20 * len(seeds), len(seeds),
             "" if not bad else "; первое %s" % (bad[0],)))

    # арена: где он стоит и кто с ним
    sizes = []
    mobs_in_arena = 0
    pill = 0
    for seed in seeds:
        for floor_n in (5, 10, 15, 20):
            fl = gen.generate(seed, floor_n, 64, 48)
            w = W.World(fl.grid, seed=seed, floor=floor_n,
                        spawns=fl.spawns, stairs=fl.stairs)
            mobs = ai.populate(w, fl)
            r = fl.rooms[fl.boss_room]
            sizes.append(min(r[2] - r[0] + 1, r[3] - r[1] + 1))
            for e in mobs:
                if e.kind == W.K_BOSS:
                    continue
                if r[0] <= e.x <= r[2] + 1 and r[1] <= e.y <= r[3] + 1:
                    mobs_in_arena += 1
            for ty in range(r[1], r[3] + 1):
                for tx in range(r[0], r[2] + 1):
                    if fl.grid.tiles[ty * fl.grid.w + tx] == gen.TILE_WALL:
                        pill += 1
    check(fl.boss_room >= 0 and fl.boss_room == fl.stairs_room,
          "арена босса — это комната лестницы: мимо него вниз не пройти",
          "boss_room %d, stairs_room %d" % (fl.boss_room, fl.stairs_room))
    check(min(sizes) >= gen.ARENA_MIN_SIDE,
          "арена не теснее %d клеток по меньшей стороне" % gen.ARENA_MIN_SIDE,
          "минимум по 16 этажам %d, медиана %d"
          % (min(sizes), statistics.median(sizes)))
    check(mobs_in_arena == 0,
          "в арене нет рядовых: это бой с боссом, а не этаж с толстым врагом",
          "рядовых внутри арены на 16 этажах: %d" % mobs_in_arena)
    check(pill == 0, "в арене нет столбов (за столбом не достаёт ни одна атака)",
          "клеток стены внутри арены на 16 этажах: %d" % pill)
    note("рядовых на этаже", "без босса %d, с боссом %d (8.1 + ai.BOSS_FLOOR_SHARE): "
         "суммарное здоровье этажа %d против %d"
         % (ai.ENEMY_BASE + ai.ENEMY_PER_FLOOR * 4, ai.count_for(5),
            (ai.ENEMY_BASE + ai.ENEMY_PER_FLOOR * 4) * 35,
            ai.count_for(5) * 35 + boss.BOSS_HP))


# --- 2. босса видно до удара ----------------------------------------------

def _fake_wind(stacks):
    """Пустышка с набором «Ветер x stacks» для items.dash_cd.

    Список длины items.N_UP, а не литерал: апгрейдов стало пять (8.4), и
    короткий литерал молча промахнулся бы мимо индекса.
    """
    ups = [0] * items.N_UP
    ups[items.U_WIND] = stacks
    return type("E", (), {"ups": ups})()


def _duel(dodge_dir, dist, ticks=70, hp=1000):
    """Босс против игрока. dodge_dir — (dx,dy) рывка по биту, или None.

    Возвращает (тик появления бита, тик и величина первого урона, игрок,
    босс, дистанция на момент урона).
    """
    g = hall(40, 24)
    w = make_world(g)
    p = add_player(w, 10.5, 11.5, hp=hp)
    b = boss.make(w, 10.5 + dist, 11.5)
    wake(b, w)
    saw = 0
    pressed = 0
    hit_at = 0
    hit_dmg = 0
    hp0 = p.hp
    for _ in range(ticks):
        if saw and dodge_dir and not pressed:
            p.btn = BTN_DASH
            p.mv = dodge_dir
            pressed = w.tick + 1
        w.step()
        p.btn = 0
        if not saw and (b.flags & W.F_WINDUP):
            saw = w.tick
        if p.hp < hp0 and not hit_at:
            hit_at = w.tick
            hit_dmg = hp0 - p.hp
            break
    d = math.hypot(p.x - b.x, p.y - b.y)
    return saw, pressed, hit_at, hit_dmg, d, p, b


def t_readable():
    print("\n--- 2. босса видно до удара: бит WINDUP и рывок (4.2, 4.3) ---")
    note("числа боя", "«Обвал»: замах %d тиков, круг %.2f + радиус цели, урон %d; "
                      "«Залп»: замах %d тиков, %d снаряда по %d, веер %.0f град"
         % (boss.SLAM_WIND, boss.SLAM_R, boss.SLAM_DMG, boss.VOLLEY_WIND,
            boss.VOLLEY_N, boss.VOLLEY_DMG, math.degrees(boss.VOLLEY_SPREAD)))
    note("для сравнения", "замах рубаки %d тиков (4.2, вилка 12-18), "
                          "рывок %d тиков = %.2f клетки, неуязвимость %d тиков"
         % (ai.MELEE_WIND, combat.DASH_TICKS,
            combat.DASH_SPEED * combat.DASH_TICKS * W.DT, combat.DASH_INV_TICKS))

    # «Обвал»: игрок стоит на дистанции своего удара
    reach = combat.MELEE_REACH + boss.BOSS_R
    saw, _, hit_at, dmg, _, _, _ = _duel(None, reach)
    check(saw > 0 and hit_at > 0 and dmg == boss.SLAM_DMG,
          "«Обвал»: неподвижный игрок урон получает",
          "бит WINDUP с тика %d, урон %d на тике %d — это %d тиков форы"
          % (saw, dmg, hit_at, hit_at - saw))
    slam_lead = hit_at - saw
    check(slam_lead >= combat.DASH_TICKS,
          "между видимым замахом «Обвала» и уроном не меньше %d тиков (рывок)"
          % combat.DASH_TICKS,
          "фактически %d тиков = %.0f мс; рывок занимает %d тиков = %.0f мс"
          % (slam_lead, slam_lead / 30.0 * 1000, combat.DASH_TICKS,
             combat.DASH_TICKS / 30.0 * 1000))

    # босс стоит СПРАВА от игрока (x + dist), значит «прочь» это -x.
    # Окно 30 тиков: замах 18 плюс запас. Длиннее нельзя — босс успеет начать
    # ВТОРУЮ атаку, и «получил урон» перестанет значить «не ушёл от обвала».
    saw2, pressed2, hit2, dmg2, d2, _, _ = _duel((-1.0, 0.0), reach, ticks=30)
    check(dmg2 != boss.SLAM_DMG,
          "игрок, нажавший рывок ПРОЧЬ по биту WINDUP, «Обвал» не получает",
          "нажал на тике %d (бит с %d), к концу замаха отъехал на %.2f клетки "
          "при радиусе обвала %.2f; урона за окно %d"
          % (pressed2, saw2, d2, boss.SLAM_R + W.R_PLAYER, dmg2))

    # «Залп»: игрок на дистанции стрельбы
    saw3, _, hit3, dmg3, _, _, _ = _duel(None, 5.0)
    check(saw3 > 0 and hit3 > 0 and dmg3 == boss.VOLLEY_DMG,
          "«Залп»: неподвижный игрок урон получает",
          "бит WINDUP с тика %d, урон %d на тике %d — это %d тиков форы"
          % (saw3, dmg3, hit3, hit3 - saw3))
    check(hit3 - saw3 >= boss.VOLLEY_WIND,
          "между видимым замахом «Залпа» и уроном не меньше замаха (%d тиков)"
          % boss.VOLLEY_WIND,
          "фактически %d тиков: %d замаха плюс %d тиков полёта снаряда"
          % (hit3 - saw3, boss.VOLLEY_WIND, hit3 - saw3 - boss.VOLLEY_WIND))

    # окно 45 тиков: замах 18 плюс полёт снаряда; откат залпа 75, второго
    # залпа за окно быть не может
    saw4, pressed4, hit4, dmg4, d4, _, _ = _duel((0.0, 1.0), 5.0, ticks=45)
    check(dmg4 != boss.VOLLEY_DMG,
          "игрок, нажавший рывок ВБОК по биту WINDUP, «Залп» не получает",
          "нажал на тике %d (бит с %d), ушёл с линии; урона за окно %d"
          % (pressed4, saw4, dmg4))
    return slam_lead


# --- 3. две атаки — два разных ответа -------------------------------------

# ОКНО ЗАМЕРА — РОВНО ОДНА АТАКА, И СЧИТАЕТСЯ ОНО ОТ ЧИСЕЛ АТАКИ.
# Было 75 тиков на обе атаки, и на прежнем темпе это работало случайно: до
# второй атаки босс не успевал. С темпом 7.5 кл/с игрок добегает до стены
# зала за 9 клеток, упирается, босс (6.3) доходит и бьёт ВТОРОЙ раз внутри
# того же окна — в таблице появлялось 25 и 50 урона там, где верный ответ
# сработал. Это ложный красный на исправной игре, ровно тот случай, про
# который раздел 10 говорит «порог не может быть временем».
#
# Слам бьёт в конце замаха, поэтому ему хватает замаха плюс запас. Залпу
# нужна ещё ВСЯ жизнь снаряда: не догнал за combat.SHOT_TICKS (24 тика =
# дальность 14 клеток, 4.2) — не догонит никогда, снаряд растворился.
# Оба окна короче отката своей атаки (54 и 75), то есть второй атаке внутри
# окна места нет по построению.
SLAM_WINDOW = boss.SLAM_WIND + 6
VOLLEY_WINDOW = boss.VOLLEY_WIND + combat.SHOT_TICKS + 6


def _answer(attack, move, ticks=None):
    """Сколько урона снимет атака, если отвечать так. move: 'стоять',
    'отступить', 'вбок'. Ответ — БЕГ, а не рывок: рывок уводит от всего
    (3.50 клетки за 5 тиков), и на нём разница между атаками не видна."""
    if ticks is None:
        ticks = SLAM_WINDOW if attack == "обвал" else VOLLEY_WINDOW
    g = hall(40, 24)
    w = make_world(g)
    dist = combat.MELEE_REACH + boss.BOSS_R if attack == "обвал" else 5.0
    p = add_player(w, 10.5, 11.5, hp=1000)
    b = boss.make(w, 10.5 + dist, 11.5)
    wake(b, w)
    if attack == "обвал":
        b.shot_ready = 10 ** 6       # второй атаке в этом замере места нет
    else:
        b.atk_ready = 10 ** 6
    mv = {"стоять": (0.0, 0.0), "отступить": (-1.0, 0.0),
          "вбок": (0.0, -1.0)}[move]
    hp0 = p.hp
    started = 0
    for _ in range(ticks):
        if started:
            p.mv = mv
        w.step()
        if not started and (b.flags & W.F_WINDUP):
            started = w.tick
    return hp0 - p.hp


def t_two_answers():
    print("\n--- 3. две атаки требуют разного ответа (иначе бой одномерный) ---")
    moves = ("стоять", "отступить", "вбок")
    table = {}
    for atk in ("обвал", "залп"):
        for mv in moves:
            table[(atk, mv)] = _answer(atk, mv)
    print("             " + "".join("%12s" % m for m in moves))
    for atk in ("обвал", "залп"):
        print("    %-9s" % atk + "".join("%12d" % table[(atk, m)]
                                         for m in moves))
    note("как читать таблицу",
         "урон за ОДНУ атаку: окно %d тиков у «Обвала» и %d у «Залпа» "
         "(замах плюс запас, второй атаке места нет); бег %.1f кл/с даёт "
         "%.2f клетки за %d тиков замаха"
         % (SLAM_WINDOW, VOLLEY_WINDOW, W.SPEED_RUN,
            W.SPEED_RUN * boss.SLAM_WIND / 30.0, boss.SLAM_WIND))
    check(table[("обвал", "стоять")] > 0 and table[("залп", "стоять")] > 0,
          "стоять столбом наказывается обеими атаками",
          "обвал %d, залп %d" % (table[("обвал", "стоять")],
                                 table[("залп", "стоять")]))
    check(table[("обвал", "отступить")] == 0 and table[("обвал", "вбок")] > 0,
          "«Обвал» уходится ОТСТУПЛЕНИЕМ, и только им",
          "отступить %d, вбок %d — из круга вбок не выходят"
          % (table[("обвал", "отступить")], table[("обвал", "вбок")]))
    check(table[("залп", "вбок")] == 0 and table[("залп", "отступить")] > 0,
          "«Залп» уходится УХОДОМ ВБОК, и только им",
          "вбок %d, отступить %d — снаряд %.0f кл/с догоняет бегущего по прямой"
          % (table[("залп", "вбок")], table[("залп", "отступить")],
             combat.SHOT_SPEED))
    check(table[("обвал", "отступить")] != table[("залп", "отступить")]
          and table[("обвал", "вбок")] != table[("залп", "вбок")],
          "правильный ответ на одну атаку НЕВЕРЕН для другой",
          "диагональ таблицы нулевая, побочная — нет")


# --- 4. физика ------------------------------------------------------------

def t_physics():
    print("\n--- 4. босс не ломает физику (4.2a) ---")
    # (а) проём в одну клетку
    got = []
    for k in range(16):
        g = split_hall(w=26, h=12, wx=13, door_y=5)
        w = make_world(g)
        p = add_player(w, 20.5, 5.5, hp=10 ** 7)
        b = boss.make(w, 4.5, 1.5 + k * 0.5)
        wake(b, w)
        t_through = 0
        for t in range(400):
            w.step()
            if b.x > 13.0:
                t_through = t + 1
                break
        got.append(t_through)
    ok = sum(1 for t in got if t)
    check(ok == 16, "босс (r %.2f) проходит проём в одну клетку" % boss.BOSS_R,
          "прошло %d из 16, медиана %d тиков; полоса прохода для центра "
          "1 - 2r = %.2f, проводит подталкивание 4.2a"
          % (ok, statistics.median([t for t in got if t]) if ok else -1,
             1 - 2 * boss.BOSS_R))
    note("почему радиус именно %.2f" % boss.BOSS_R,
         "это потолок: при 0.55 полоса отрицательна и проходов 0 из 16 "
         "(замер тем же стендом)")

    # (б) босс не выталкивает игрока сквозь стену
    g = hall(20, 12)
    w = make_world(g)
    p = add_player(w, 1.5, 5.5, hp=10 ** 7)      # вплотную к левой стене
    b = boss.make(w, 4.0, 5.5)
    wake(b, w)
    worst_x = p.x
    in_wall = 0
    moved = 0.0
    for _ in range(240):
        p.mv = (-1.0, 0.0)                        # игрок сам жмёт в стену
        w.step()
        # правило 10: смотрим клетку, в которую въезжает ЦЕНТР тела
        if w.grid.solid(int(p.x), int(p.y)):
            in_wall += 1
        worst_x = min(worst_x, p.x)
        moved = max(moved, abs(p.y - 5.5))
    check(in_wall == 0 and worst_x >= 0.5 - 1e-9,
          "босс не выталкивает игрока сквозь стену",
          "240 тиков тарана в угол: центр игрока в стене %d тиков, "
          "минимальный x %.3f при радиусе %.2f (стена по x < %.2f)"
          % (in_wall, worst_x, W.R_PLAYER, 1.0))
    note("почему так по построению",
         "world._crowd_push расталкивает ВРАГОВ МЕЖДУ СОБОЙ; игрок в список "
         "не попадает вовсе, поэтому телом его сдвинуть нечем")

    # (в) доходит на настоящих этажах с боссом
    stuck = []
    ticks_to = []
    for seed in (11, 4242, 777, 90210, 5150):
        for floor_n in (5, 10):
            fl = gen.generate(seed, floor_n, 64, 48)
            w = W.World(fl.grid, seed=seed, floor=floor_n,
                        spawns=fl.spawns, stairs=fl.stairs)
            p = add_player(w, fl.entry[0] + 0.5, fl.entry[1] + 0.5, hp=10 ** 7)
            mobs = ai.populate(w, fl)
            b = [e for e in mobs if e.kind == W.K_BOSS][0]
            d0 = math.hypot(b.x - p.x, b.y - p.y)
            got_t = 0
            for t in range(1800):        # предохранитель: 60 с при пути < 130
                wake(b, w)
                w.step()
                if math.hypot(b.x - p.x, b.y - p.y) <= boss.SLAM_START:
                    got_t = t + 1
                    break
            if got_t:
                ticks_to.append(got_t)
            else:
                stuck.append((seed, floor_n, round(b.x, 1), round(b.y, 1), d0))
    check(not stuck, "босс доходит до игрока на каждом этаже с боссом",
          "10 этажей, дошли %d, медиана %d тиков; не дошли: %s"
          % (len(ticks_to),
             statistics.median(ticks_to) if ticks_to else -1, stuck or "нет"))


# --- 4b. чувства босса: те же, что у рядового ------------------------------

def t_senses():
    print("\n--- 4b. босс видит туманом (4.4) и слышит волной (8.2) ---")
    # ЗРЕНИЕ: клетка босса освещена -> он поднят, без единого луча
    g = hall(30, 12)
    w = make_world(g)
    p = add_player(w, 4.5, 5.5, hp=10 ** 7)
    b = boss.make(w, 9.5, 5.5)
    b.ai_alert = 0
    # ДВА тика, и это не подгонка: world.step зовёт ai.step ДО движения, а
    # туман обновляет ПОСЛЕ. На первом тике тумана ещё нет ни у кого.
    w.step()
    b.ai_alert = 0
    w.step()
    lit = w.fog.state[int(b.y) * g.w + int(b.x)]
    check(b.ai_alert > w.tick and lit == vis_mod.VIS_LIT,
          "босс поднят чтением байта тумана, как рядовой (4.4)",
          "клетка босса %d (VIS_LIT=%d), ai_alert %d при тике %d — луч не "
          "бросался ни один" % (lit, vis_mod.VIS_LIT, b.ai_alert, w.tick))

    # СЛУХ: за стеной, вне видимости. Выстрел слышно (18), ближний удар нет (6)
    for loud, name, hear in ((W.NOISE_SHOT, "выстрел", True),
                             (W.NOISE_MELEE, "ближняя атака", False)):
        g2 = split_hall(w=26, h=12, wx=13, door_y=1)
        w2 = make_world(g2)
        p2 = add_player(w2, 10.5, 5.5, hp=10 ** 7)
        b2 = boss.make(w2, 16.5, 5.5)
        # по прямой 6 клеток, по полу — через единственную дверь наверху
        path = nav.distance_map(g2, [(int(p2.x), int(p2.y))])[
            int(b2.y) * g2.w + int(b2.x)]
        b2.ai_alert = 0
        w2.step()
        b2.ai_alert = 0                   # если успел увидеть — сбросить
        w2.make_noise(p2.x, p2.y, loud, p2.team)
        w2.step()
        got = b2.ai_alert > w2.tick
        check(got == hear,
              "босс %s за стеной %s (громкость %d, по полу %d клеток, "
              "по прямой %.0f)"
              % ("слышит" if hear else "НЕ слышит", name, loud, path,
                 abs(b2.x - p2.x)),
              "ai_alert %d при тике %d; круг по радиусу услышал бы ОБА"
              % (b2.ai_alert, w2.tick))


# --- 5. босса можно убить -------------------------------------------------

def t_killable():
    print("\n--- 5. босса можно убить: скриптовый игрок против 400 hp ---")

    def fight(ups=()):
        g = hall(40, 24)
        w = make_world(g)
        p = add_player(w, 10.5, 11.5, hp=100)
        for u, n in ups:
            items.grant(p, u, n)
        b = boss.make(w, 22.0, 11.5)
        wake(b, w)
        reach = combat.MELEE_REACH + boss.BOSS_R
        for t in range(3600):        # предохранитель: 2 минуты на бой в 15 с
            dx = b.x - p.x
            dy = b.y - p.y
            d = math.hypot(dx, dy) or 1e-9
            ux, uy = dx / d, dy / d
            btn = 0
            mv = (0.0, 0.0)
            if b.flags & W.F_WINDUP:
                # ровно то, что проверено в п.3: круг — прочь, линия — вбок.
                # Граница — КРОМКА КРУГА, а не дистанция начала замаха:
                # внутри круга замах может быть только «Обвалом»
                # (boss.VOLLEY_NEAR стоит за кромкой именно ради этого).
                if d <= boss.SLAM_R + W.R_PLAYER:
                    mv = (-ux, -uy)
                else:
                    mv = (-uy, ux)
                if w.tick >= p.dash_ready:
                    btn |= BTN_DASH
            elif d > reach - 0.1:
                mv = (ux, uy)
            else:
                btn |= BTN_ATTACK
            p.mv = mv
            p.btn = btn
            p.aim = (b.x, b.y)
            w.step()
            p.btn = 0
            if b.hp <= 0 or b.id not in w.entities:
                return t + 1, p.hp
            if p.hp <= 0:
                return -(t + 1), 0
        return 0, p.hp

    t_bare, hp_bare = fight()
    check(t_bare > 0, "голый игрок босса убивает",
          "%d тиков = %.1f с, здоровья осталось %d из 100"
          % (t_bare, t_bare / 30.0, hp_bare) if t_bare > 0 else
          "погиб на тике %d" % -t_bare)
    t_up, hp_up = fight(((items.U_WIND, 1), (items.U_RAM, 1)))
    note("с набором (Ветер + Таран, 8.4)",
         "%d тиков = %.1f с, здоровья осталось %d"
         % (abs(t_up), abs(t_up) / 30.0, hp_up))
    wind_cd = items.dash_cd(_fake_wind(1), combat.DASH_CD_TICKS)
    check(t_up > 0 and hp_up >= hp_bare,
          "набор апгрейдов босса не ухудшает: с ним игрок выходит не хуже",
          "голый %d hp из 100, с набором %d hp; Ветер укорачивает откат рывка "
          "%d -> %d тиков (прямая мерка — items_check, раздел 4)"
          % (hp_bare, hp_up, combat.DASH_CD_TICKS, wind_cd))
    note("почему здесь Ветер ничего не прибавляет",
         "босс бьёт раз в %d тиков («Обвал») и раз в %d («Залп»), а рывок "
         "готов раз в %d даже БЕЗ Ветра — значит скриптовый боец встречает "
         "рывком каждый замах в обоих прогонах, и разнице взяться неоткуда "
         "(замерено: 10 замахов, 0 встречено без готового рывка). Ветер "
         "платит там, где замахи идут чаще отката: в толпе, и это меряется "
         "прямо — items_check, кольцо из восьми рубак"
         % (boss.SLAM_CD, boss.VOLLEY_CD, combat.DASH_CD_TICKS))
    note("а вот быстрее бой не становится",
         "голый %.1f с, с набором %.1f с. Это про СКРИПТОВОГО бойца: он "
         "рвётся только ПРОЧЬ, то есть Тараном (урон рывка) не пользуется "
         "вовсе, а лишние уходы уводят его дальше и возвращаться дольше. "
         "Числом бьёт Таран ниже, отдельно."
         % (t_bare / 30.0, t_up / 30.0))
    note("темп урона", "%d hp за %.1f с = %.1f hp/с при 20 за удар и откате "
         "%d тиков" % (boss.BOSS_HP, t_bare / 30.0,
                       boss.BOSS_HP / (t_bare / 30.0), combat.MELEE_CD_TICKS))

    # Таран (8.4) бьёт босса без единой оговорки про босса
    g = hall(40, 24)
    w = make_world(g)
    p = add_player(w, 10.5, 11.5, hp=10 ** 6)
    items.grant(p, items.U_RAM, 1)
    b = boss.make(w, 13.0, 11.5)
    b.ai = 0                                   # нужен неподвижный мешок
    hp0 = b.hp
    p.mv = (1.0, 0.0)
    p.btn = BTN_DASH
    w.step()
    p.btn = 0
    for _ in range(6):
        w.step()
    check(hp0 - b.hp == items.RAM_DMG,
          "Таран (8.4) снимает с босса ровно свои %d — ветки «если это босс» "
          "в бою нет" % items.RAM_DMG,
          "было %d, стало %d" % (hp0, b.hp))


# --- 6. лестница заперта, пока босс жив -----------------------------------

def t_stairs_locked():
    print("\n--- 6. лестница заперта, пока босс жив (8.1) ---")
    seed = 4242
    fl = gen.generate(seed, 5, 64, 48)
    w = W.World(fl.grid, seed=seed, floor=5, spawns=fl.spawns, stairs=fl.stairs)
    p = add_player(w, fl.stairs[0] + 0.5, fl.stairs[1] + 0.5, hp=10 ** 7)
    mobs = ai.populate(w, fl)
    b = [e for e in mobs if e.kind == W.K_BOSS][0]
    locked = w.on_stairs(p)
    check(not locked, "стоя на лестнице при живом боссе — спуска нет",
          "боссов живо %d, on_stairs=%s" % (w.boss_alive, locked))
    combat.kill(w, b)
    w.remove(b.id)
    check(w.on_stairs(p), "босс убит — лестница работает",
          "боссов живо %d, on_stairs=%s" % (w.boss_alive, w.on_stairs(p)))
    # на обычном этаже ничего не заперто
    fl4 = gen.generate(seed, 4, 64, 48)
    w4 = W.World(fl4.grid, seed=seed, floor=4, spawns=fl4.spawns,
                 stairs=fl4.stairs)
    p4 = add_player(w4, fl4.stairs[0] + 0.5, fl4.stairs[1] + 0.5)
    ai.populate(w4, fl4)
    check(w4.on_stairs(p4), "на этаже без босса лестница как была",
          "этаж 4, боссов живо %d" % w4.boss_alive)


# --- 7. алтарей по числу живых (8.4) --------------------------------------

def t_altars():
    print("\n--- 7. алтарей на этаж по числу живых (8.4) ---")
    note("формула", "items.altar_count = 1 + живых // 2; расшифровка 8.4 "
                    "«один — один, двое или трое — два, четверо или пятеро — три»")
    note("ВНИМАНИЕ", "записанная в 8.4 формула 1 + (живых-1)//2 даёт на чётных "
                     "ДРУГОЕ (двое -> один, четверо -> два) и противоречит "
                     "собственной расшифровке; взята расшифровка, см. отчёт")
    want = {1: 1, 2: 2, 3: 2, 4: 3, 5: 3}
    bad = []
    for n in sorted(want):
        room = Room("B%d" % n, settings=RoomSettings(seed=4242))
        for i in range(n):
            room.add(Player(i + 1, "p%d" % i, _Conn()))
        room.start()
        room.tick()
        w = room.world
        its = [e for e in w.entities.values() if items.is_item(e.kind)]
        alts = sorted(set(e.alt for e in its))
        note("живых %d" % n,
             "алтарей %d (надо %d), предметов %d, номера алтарей %s"
             % (len(alts), want[n], len(its), alts))
        if len(alts) != want[n] or len(its) != want[n] * items.ALTAR_SLOTS:
            bad.append((n, len(alts), want[n]))
        # алтари стоят ВРОЗЬ: иначе «разделяться выгодно» (4.4) не работает
        if len(alts) > 1:
            pts = [[e for e in its if e.alt == a][0] for a in alts]
            far = min(math.hypot(x.x - y.x, x.y - y.y)
                      for i, x in enumerate(pts) for y in pts[i + 1:])
            note("  расстояние между алтарями",
                 "минимальное %.1f клетки (радиус подбора %.2f)"
                 % (far, items.PICK_R))
            if far < 5.0:
                bad.append((n, "алтари в одной куче: %.1f клетки" % far, 0))
    check(not bad, "алтарей ровно столько, сколько требует число живых",
          "расхождения: %s" % (bad or "нет"))

    # один алтарь — один апгрейд: гасятся собратья ТОЛЬКО своего алтаря
    room = Room("BALT", settings=RoomSettings(seed=4242))
    for i in range(4):
        room.add(Player(i + 1, "p%d" % i, _Conn()))
    room.start()
    room.tick()
    w = room.world
    its = [e for e in w.entities.values() if items.is_item(e.kind)]
    first_alt = its[0].alt
    mine = [e for e in its if e.alt == first_alt]
    taker = w.alive_players()[0]
    taker.x, taker.y = mine[0].x, mine[0].y
    items.take(w, taker, mine[0])
    left = [e for e in w.entities.values() if items.is_item(e.kind)]
    check(len(left) == len(its) - items.ALTAR_SLOTS,
          "взял с одного алтаря — погас ТОЛЬКО он, чужие остались",
          "было %d предметов, стало %d, погасло %d (на алтаре %d)"
          % (len(its), len(left), len(its) - len(left), items.ALTAR_SLOTS))
    check(all(e.alt != first_alt for e in left),
          "от взятого алтаря не осталось ничего", "номер %d" % first_alt)


# --- 8. тик с боссом в бюджете (2.2) --------------------------------------

BENCH_TICKS = 300
BENCH_WARM = 60
BENCH_ENTS = 200        # потолок 2.2
BUDGET_MED = 12.0
BUDGET_P99 = 25.0


def t_budget():
    print("\n--- 8. тик на этаже с боссом в бюджете 2.2 ---")
    import random
    seed = 4242
    fl = gen.generate(seed, 5, 64, 48)
    w = W.World(fl.grid, seed=seed, floor=5, spawns=fl.spawns, stairs=fl.stairs)
    ps = [add_player(w, fl.spawns[i % len(fl.spawns)][0],
                     fl.spawns[i % len(fl.spawns)][1], hp=10 ** 7)
          for i in range(6)]
    mobs = ai.populate(w, fl)
    b = [e for e in mobs if e.kind == W.K_BOSS][0]
    b.hp = b.hp_max = 10 ** 7
    # добить до потолка 2.2: штатная расстановка этажа с боссом даёт 12+1
    rnd = random.Random(seed ^ 0xB0)
    while len(w.entities) < BENCH_ENTS:
        r = fl.rooms[rnd.randrange(len(fl.rooms))]
        x = rnd.randint(r[0], r[2]) + 0.5
        y = rnd.randint(r[1], r[3]) + 0.5
        if physics.circle_hits(w.grid, x, y, ai.ENEMY_R):
            continue
        kind = (ai.AI_RANGED if len(w.entities) % ai.RANGED_EVERY ==
                ai.RANGED_EVERY - 1 else ai.AI_MELEE)
        e = ai.make_enemy(w, x, y, kind)
        e.hp = e.hp_max = 10 ** 7
    n_ent = len(w.entities)
    dts = []
    for t in range(BENCH_TICKS + BENCH_WARM):
        for e in list(w.entities.values()):
            if e.kind in W.ENEMY_KINDS:
                e.ai_alert = w.tick + 10 ** 6      # худший случай: все подняты
        for p in ps:
            p.btn = proto.BTN_SHOOT               # и стреляют: шум 18 (8.2)
        t0 = CLOCK()
        w.step()
        dt = (CLOCK() - t0) * 1000.0
        for p in ps:
            p.btn = 0
        if t >= BENCH_WARM:
            dts.append(dt)
    dts.sort()
    med = statistics.median(dts)
    p99 = dts[min(len(dts) - 1, int(0.99 * (len(dts) - 1)))]
    note("состав", "%d сущностей (потолок 2.2 — %d), из них босс 1, "
         "все враги подняты, 6 игроков стреляют" % (n_ent, BENCH_ENTS))
    note("тик, мс", "медиана %.3f, p99 %.3f, среднее %.3f, max %.3f"
         % (med, p99, statistics.mean(dts), dts[-1]))
    note("uptime рядом с замером (правило 9)", uptime_line())
    check(med <= BUDGET_MED and p99 <= BUDGET_P99,
          "тик на этаже с боссом в бюджете 2.2 (медиана <= %.0f, p99 <= %.0f)"
          % (BUDGET_MED, BUDGET_P99),
          "запас x%.1f по медиане и x%.1f по p99"
          % (BUDGET_MED / med, BUDGET_P99 / p99))


def main():
    print("ПРИЁМКА БОССА (8.1) И АЛТАРЕЙ ПО ЖИВЫМ (8.4)")
    print("  " + uptime_line())
    t_every_fifth()
    t_readable()
    t_two_answers()
    t_physics()
    t_senses()
    t_killable()
    t_stairs_locked()
    t_altars()
    t_budget()
    print("\n" + ("КРАСНО: %d" % len(FAILS) if FAILS else "ВСЁ ЗЕЛЕНО"))
    for f in FAILS:
        print("   * " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
