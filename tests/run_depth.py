#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Стенд глубины забега (DESIGN.md 8.1, 11.8): на каком этаже гибнет группа.

ЭТО ЗАМЕР, А НЕ ПРИЁМКА. Он ничего не запрещает и всегда возвращает 0:
«правильной» глубины забега в контракте нет, есть числа 11.8, снятые один
раз руками на этапе 3b и с тех пор не воспроизводимые. Этот файл делает их
воспроизводимыми: любая правка темпа, бестиария или апгрейдов меряется им ДО
и ПОСЛЕ, и «стало заметно легче» перестаёт быть ощущением.

ЧТО ЗА БОЙЦЫ. Скриптовые, ровно как в 11.8, то есть НИЖНЯЯ оценка:

  * идут к лестнице по волне (nav), по дороге сворачивают за апгрейдом,
    если алтарь ближе ALTAR_REACH клеток пути — 8.4 говорит, что за крюком
    ходят, а не пробегают мимо;
  * дерутся ближним боем: подошёл на дальность удара — бьёт;
  * уходят от замаха: увидел F_WINDUP — рывок ПРОЧЬ, а у босса по правилу
    двух ответов (4.2: круг — прочь, линия — вбок);
  * НЕ умеют отступать на низком здоровье, беречь рывок, стрелять и
    разделяться. Живой игрок умеет всё это, поэтому настоящая глубина выше
    измеренной; сравнивать надо замер с замером, а не замер с ощущением.

ПОЧЕМУ БЕЗ КОМНАТЫ И ПРОТОКОЛА. Меряется баланс, а не сеть: мир крутится
напрямую (world.step), спуск повторяет room.descend один в один (генерация
этажа, enter_floor, расстановка врагов и алтарей). Сериализация снапшотов на
20 этажей стоила бы больше, чем весь бой, и не меряет ничего.

ПОРОГ ОСТАНОВКИ — ПРОГРЕСС, А НЕ ЧАСЫ (правило 10). Этаж кончается смертью
всех, спуском или предохранителем FLOOR_CAP тиков. Предохранитель выведен из
карты: путь вниз по 8.1 медиана 86 клеток плюс крюк за алтарём медиана 60,
то есть 146 клеток; на самом медленном бойце (5.0 кл/с) это 29 с = 880
тиков. FLOOR_CAP = 6000 тиков (200 с) — запас больше чем в шесть раз, и это
именно предохранитель от зависшего бойца, а не «сколько ему отведено».

Запуск:  python3 tests/run_depth.py [--players N] [--seeds K] [--max-floor F]
Выход: всегда 0 (это стенд).
"""

import argparse
import math
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import ai, boss, combat, gen, items, nav, proto   # noqa: E402
from server import world as world_mod                         # noqa: E402

W = world_mod
CLOCK = time.perf_counter

BTN_DASH = proto.BTN_DASH
BTN_ATTACK = proto.BTN_ATTACK
BTN_ITEM = proto.BTN_ITEM

# --- числа стенда ---------------------------------------------------------
FLOOR_CAP = 6000        # тиков на этаж; предохранитель, вывод см. в шапке
FIGHT_R = 7.0           # ближе этого боец занимается врагом, а не лестницей
FORGET_R = 12.0         # дальше этого выбранная цель забывается
CHASE_CAP = 300         # тиков на одну цель без единого снятого hp — и хватит
# ВЫВОД FIGHT_R: радиус обзора 10 клеток (4.1), полоса стрелка 5..9 (4.2).
# 7 — внутри обзора и внутри полосы стрелка: боец не бросается через весь
# этаж на каждого, кого заметил, но и не игнорирует того, кто уже стреляет.
ALTAR_REACH = 40        # клеток ПУТИ: дальше за апгрейдом не сворачивают
# ВЫВОД ALTAR_REACH: 8.4 меряет крюк медианой 60 клеток в одну сторону при
# пути вниз 86. Половина крюка — это и есть «по дороге»; 40 клеток пути от
# текущего места бойца до алтаря он проходит за 8 с на 5.0 кл/с.
NAV_EVERY = 6           # как часто боец пересчитывает свою волну до цели
# Анти-залипание: те же числа, что у client_common.walk_to, только в тиках.
# Полшага тела за 12 тиков — это заведомо не ходьба (на 5.0 кл/с боец за 12
# тиков проходит 2 клетки), а 6 тиков поперёк хватает, чтобы выйти из
# полосы 1-2r = 0.30 при любой скорости из бестиария.
STILL_STEP = 0.02       # клетки за тик: меньше — считаем, что стоим
STILL_TICKS = 12
NUDGE_TICKS = 6


def uptime_line():
    try:
        return subprocess.run(["uptime"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:               # noqa: BLE001
        return "uptime недоступен"


class Bot(object):
    """Скриптовый боец: одна цель, одна волна до неё, один приём боя."""

    __slots__ = ("ent", "field", "goal", "at_tick", "picked_floor",
                 "last_xy", "still", "nudge", "side", "foe_id",
                 "foe_since", "foe_hp0", "drop")

    def __init__(self, ent):
        self.ent = ent
        self.field = nav.Field()
        self.goal = None
        self.at_tick = -10 ** 9
        self.picked_floor = -1
        self.last_xy = (ent.x, ent.y)
        self.still = 0
        self.nudge = 0
        self.side = 1
        self.foe_id = 0
        self.foe_since = 0
        self.foe_hp0 = 0
        self.drop = set()

    def new_floor(self):
        self.field.drop()
        self.goal = None
        self.at_tick = -10 ** 9
        self.still = 0
        self.nudge = 0
        self.foe_id = 0
        self.drop = set()
        self.last_xy = (self.ent.x, self.ent.y)

    # --- куда идти --------------------------------------------------------

    def _goal_cell(self, w):
        """Лестница или алтарь: за апгрейдом сворачивают, если он по дороге.

        Цель выбирается ОДИН РАЗ на этаж и потом не пересматривается. Первый
        вариант брал ближайший предмет каждый тик — и на алтаре из трёх
        предметов боец топтался между двумя из них: они лежат в разные
        стороны от створа, волна на них разная, а поперечный ход у тела
        гасится стеной (4.2a). Замерено: 700 тиков в одной точке.
        """
        e = self.ent
        st = w.stairs
        stair_cell = (st[0], st[1]) if st else None
        if self.goal is not None:
            if self.goal == stair_cell or self.picked_floor == w.floor:
                return stair_cell if self.picked_floor == w.floor else self.goal
            # алтарь мог погаснуть целиком (взял напарник) — тогда вниз
            for t in w.entities.values():
                if items.is_item(t.kind) and (int(t.x), int(t.y)) == self.goal:
                    return self.goal
            return stair_cell
        if self.picked_floor != w.floor:
            best = None
            best_d = ALTAR_REACH
            for t in w.entities.values():
                if not items.is_item(t.kind):
                    continue
                d = math.hypot(t.x - e.x, t.y - e.y)
                if d < best_d:
                    best_d = d
                    best = (int(t.x), int(t.y))
            if best is not None:
                return best
        return stair_cell

    def _steer(self, w):
        """Направление к своей цели по волне (8.3), как у врагов.

        Плюс то, что делает руками живой игрок и что уже пришлось написать
        в client_common.walk_to: упёрся плечом в угол — дёрнись поперёк.
        Тело круглое, коридор шириной в клетку, и подталкивание (4.2a)
        помогает только вдоль той оси, по которой у тела уже есть ход.
        """
        e = self.ent
        goal = self._goal_cell(w)
        if goal is None:
            return 0.0, 0.0
        if goal != self.goal or w.tick - self.at_tick >= NAV_EVERY \
                or self.field.dist is None or self.field.grid is not w.grid:
            self.field.rebuild(w.grid, [goal], w.tick)
            self.goal = goal
            self.at_tick = w.tick
        dx, dy, _d = self.field.step_dir(e.x, e.y)
        if dx == 0.0 and dy == 0.0:
            # уже на клетке цели: доводим по прямой к её центру
            dx = goal[0] + 0.5 - e.x
            dy = goal[1] + 0.5 - e.y
            n = math.hypot(dx, dy)
            if n > 1e-6:
                dx, dy = dx / n, dy / n
            else:
                dx = dy = 0.0
        return dx, dy

    def _unstick(self, dx, dy):
        """Упёрся плечом — идти поперёк курса, попеременно в обе стороны.

        Зовётся и в бою, и в дороге, и это важно: залипает боец чаще всего
        как раз в бою — видит врага за углом (туман считает клетку
        освещённой), прёт на него по прямой и упирается в косяк. Замерено:
        без этого 700+ тиков в одной точке на сиде 1685.
        """
        e = self.ent
        moved = math.hypot(e.x - self.last_xy[0], e.y - self.last_xy[1])
        self.last_xy = (e.x, e.y)
        if self.nudge > 0:
            self.nudge -= 1
            return -dy * self.side, dx * self.side
        if moved < STILL_STEP:
            self.still += 1
            if self.still >= STILL_TICKS:
                self.still = 0
                self.nudge = NUDGE_TICKS
                self.side = -self.side
        else:
            self.still = 0
        return dx, dy

    # --- что делать в бою -------------------------------------------------

    def _enemy(self, w):
        """Ближайший враг, которого ВИДНО, с памятью на выбранную цель.

        Два условия, и оба заработаны прогоном.

        ВИДНО — из тумана (4.4): без этого боец упирается в стену и стоит,
        потому что враг за стеной ближе FIGHT_R по прямой, а дороги к нему по
        прямой нет. Тем же байтом пользуется ИИ врага (8.3), чтобы не гонять
        луч на каждого.

        ПАМЯТЬ — потому что «видно» мигает. Клетка врага на кромке обзора
        становится то освещённой, то нет, и боец без памяти бросал цель и
        снова её брал: замерено, 2 тика на состояние, 18 600 тиков на месте
        (сид 1685, этаж 2) — ровно между «иду к лестнице» и «иду к врагу»,
        которые смотрят в противоположные стороны. Взял цель — держит её,
        пока та жива и не ушла за FORGET_R.
        """
        e = self.ent
        ents = w.entities
        if self.foe_id:
            t = ents.get(self.foe_id)
            if t is not None and not (t.flags & W.F_DEAD):
                d = math.hypot(t.x - e.x, t.y - e.y)
                # догонялки с пятящимся стрелком (4.2) — не бой, а
                # предохранитель этажа: если за CHASE_CAP тиков цель не
                # потеряла ни одного hp, она бросается до конца этажа.
                if (w.tick - self.foe_since > CHASE_CAP
                        and t.hp >= self.foe_hp0):
                    self.drop.add(self.foe_id)
                elif d <= FORGET_R:
                    return t, d
            self.foe_id = 0
        fog = w.fog
        best = None
        best_d = FIGHT_R
        for t in ents.values():
            if t.kind not in W.ENEMY_KINDS or (t.flags & W.F_DEAD):
                continue
            if t.id in self.drop:
                continue
            d = math.hypot(t.x - e.x, t.y - e.y)
            if d >= best_d:
                continue
            if not fog.is_lit(int(t.x), int(t.y)):
                continue
            best_d = d
            best = t
        if best is not None:
            self.foe_id = best.id
            self.foe_since = w.tick
            self.foe_hp0 = best.hp
        return best, best_d

    def act(self, w):
        e = self.ent
        if e.flags & W.F_DEAD:
            e.mv = (0.0, 0.0)
            e.btn = 0
            return
        btn = 0
        foe, d = self._enemy(w)
        if foe is not None:
            ux = (foe.x - e.x) / (d or 1e-9)
            uy = (foe.y - e.y) / (d or 1e-9)
            e.aim = (foe.x, foe.y)
            reach = combat.MELEE_REACH + foe.r
            if foe.flags & W.F_WINDUP:
                # 4.2: у рядового ответ один — прочь; у босса их два (8.1),
                # и выбирает их дистанция, а не вид атаки: внутри круга
                # замах может быть только «Обвалом» (boss.VOLLEY_NEAR).
                if foe.kind == W.K_BOSS and d > boss.SLAM_R + e.r:
                    mv = (-uy, ux)          # с линии залпа — вбок
                else:
                    mv = (-ux, -uy)         # из круга — прочь
                if w.tick >= e.dash_ready:
                    btn |= BTN_DASH
            elif d > reach - 0.1:
                mv = (ux, uy)
            else:
                mv = (0.0, 0.0)
                btn |= BTN_ATTACK
        else:
            mv = self._steer(w)
            # предмет под ногами берётся всегда: это и есть «взять апгрейд»
            it = items.nearest_item(w, e)
            if it is not None:
                btn |= BTN_ITEM
                self.picked_floor = w.floor
        if mv[0] or mv[1]:
            mv = self._unstick(mv[0], mv[1])
        else:
            self.last_xy = (e.x, e.y)
            self.still = 0
        e.mv = mv
        e.btn = btn


def make_floor(seed, floor, w=None):
    return gen.generate(seed, floor, gen.ROOM_W, gen.ROOM_H)


def run_once(seed, n_players, max_floor, hp=100):
    """Один забег. Возвращает (этаж гибели или 0, тиков, этажи и причины)."""
    fl = make_floor(seed, 1)
    w = W.World(fl.grid, seed=seed, floor=1, spawns=fl.spawns,
                stairs=fl.stairs)
    bots = []
    for i in range(n_players):
        e = w.spawn_player("Б%d" % (i + 1), i)
        e.hp = e.hp_max = hp
        bots.append(Bot(e))
    ai.populate(w, fl)
    items.populate(w, fl)
    floor = 1
    ticks = 0
    floor_ticks = 0
    stalls = 0
    while True:
        for b in bots:
            b.act(w)
        w.step()
        for b in bots:
            b.ent.btn = 0
        ticks += 1
        floor_ticks += 1
        alive = w.alive_players()
        if not alive:
            return floor, ticks, stalls          # конец забега (8.1)
        if all(w.on_stairs(e) for e in alive):
            floor += 1
            if floor > max_floor:
                return 0, ticks, stalls          # дошли до дна замера
            fl = make_floor(seed, floor)
            w.enter_floor(floor, fl.grid, fl.spawns, fl.stairs)
            ai.populate(w, fl)
            items.populate(w, fl)
            for b in bots:
                b.new_floor()
            floor_ticks = 0
            continue
        if floor_ticks >= FLOOR_CAP:
            stalls += 1
            return -floor, ticks, stalls         # предохранитель
    # недостижимо


def table(label, seeds, n_players, max_floor):
    print("\n--- %s ---" % label)
    rows = []
    t0 = CLOCK()
    for s in seeds:
        died, ticks, stalls = run_once(s, n_players, max_floor)
        rows.append((s, died, ticks))
        if died > 0:
            what = "гибель на этаже %d" % died
        elif died == 0:
            what = "дошли до %d" % max_floor
        else:
            what = "ПРЕДОХРАНИТЕЛЬ на этаже %d" % -died
        print("    сид %-6d  %-24s  %6.1f с игрового времени"
              % (s, what, ticks / float(W.TICK_HZ)))
    deaths = [r[1] for r in rows if r[1] > 0]
    reached = sum(1 for r in rows if r[1] == 0)
    stuck = sum(1 for r in rows if r[1] < 0)
    depth = [(r[1] if r[1] > 0 else max_floor) for r in rows if r[1] >= 0]
    med = statistics.median(depth) if depth else 0
    print("    ИТОГ: медиана этажа гибели %.1f, дошли до %d-го %d из %d,"
          " предохранитель %d, стенд %.1f с"
          % (med, max_floor, reached, len(rows), stuck, CLOCK() - t0))
    return {"label": label, "med": med, "reached": reached, "n": len(rows),
            "stuck": stuck, "deaths": deaths}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", type=int, default=0,
                    help="только один состав: 1, 2 или 4")
    ap.add_argument("--seeds", type=int, default=0, help="сколько сидов")
    ap.add_argument("--max-floor", type=int, default=20)
    args = ap.parse_args()

    print("=" * 72)
    print("ГЛУБИНА ЗАБЕГА (8.1, 11.8). Скриптовые бойцы — НИЖНЯЯ оценка.")
    print("  " + uptime_line())
    print("  скорости: игрок %.1f, рубака %.1f, стрелок %.1f, босс %.1f кл/с"
          % (W.SPEED_RUN, ai.MELEE_SPEED, ai.RANGED_SPEED, boss.BOSS_SPEED))
    print("=" * 72)

    plans = [(1, 14), (2, 10), (4, 10)]
    if args.players:
        plans = [(args.players, args.seeds or 10)]
    out = []
    for n, k in plans:
        if args.seeds:
            k = args.seeds
        seeds = [1000 + 137 * i for i in range(k)]
        out.append(table("состав %d, сидов %d" % (n, k), seeds, n,
                         args.max_floor))
    print("\nСВОДКА")
    print("  %-22s %8s %10s %8s" % ("состав", "медиана", "до дна", "предохр."))
    for r in out:
        print("  %-22s %8.1f %10s %8d"
              % (r["label"], r["med"], "%d из %d" % (r["reached"], r["n"]),
                 r["stuck"]))
    print("  " + uptime_line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
