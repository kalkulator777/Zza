# -*- coding: utf-8 -*-
"""Простой бот: держит дистанцию по роли, не падает с арены, жмёт способности."""

import math
import random

from . import const as C

MELEE = {"rezak", "bunker", "gayka", "zerkalo", "yakor"}
PREF_RANGE = {"rezak": 70, "bunker": 80, "gayka": 90, "igla": 430, "vyuga": 330,
              "puls": 300, "gorn": 290, "zerkalo": 100, "yakor": 115}
# герои, которые стреляют и потому целятся с упреждением
LEADERS = {"igla", "vyuga", "puls", "gorn"}
# как герой возвращается на арену: (кулдаун, бит ввода, прицел_x к арене, прицел_y)
# знак x умножается на направление «к арене»; отрицательный = целиться от арены,
# потому что способность отбрасывает назад (отдача Горна, отскок Иглы)
RECOVERY = {
    "rezak":   ("q", C.IN_Q, 0.75, -0.66),   # рывок-разрез по прицелу
    "yakor":   ("q", C.IN_Q, 0.70, -0.72),   # воронка тянет самого Якоря
    "gayka":   ("e", C.IN_E, 0.60, -0.80),   # крюк в стену
    "gorn":    ("e", C.IN_E, -1.0, -0.12),   # отдача выхлопа
    "igla":    ("e", C.IN_E, -1.0, -0.12),   # отскок
    "bunker":  ("e", C.IN_E, 1.0, -0.10),    # таран
}

REACT = {1: 14, 2: 7, 3: 3}
MISS = {1: 0.28, 2: 0.12, 3: 0.04}


def incoming(w, f, rng=230.0):
    """Во f летит вражеский снаряд и уже близко?"""
    r2 = rng * rng
    for p in w.projectiles:
        if p.team == f.team:
            continue
        dx, dy = f.cx - p.x, f.cy - p.y
        if dx * dx + dy * dy > r2:
            continue
        if dx * p.vx + dy * p.vy > 0:      # летит в нашу сторону, а не мимо
            return True
    return False


class Bot:
    def __init__(self, pid, level=2, seed=None):
        self.pid = pid
        self.level = max(1, min(3, int(level)))
        self.rng = random.Random(seed if seed is not None else pid * 7919)
        self.t = 0
        self.move = 0
        self.want_jump = False
        self.hold = 0
        self.hp_prev = None
        self.hurt_t = -99      # тик последнего полученного урона

    def think(self, w):
        f = w.fighters.get(self.pid)
        if f is None:
            return
        if not f.alive or w.state != C.ST_PLAY:
            f.inp = 0
            return

        self.t += 1
        inp = 0
        r = self.rng

        if self.hp_prev is not None and f.hp < self.hp_prev - 0.4:
            self.hurt_t = self.t
        self.hp_prev = f.hp

        # --- геометрия арены
        ground = w.platforms[0]
        gx0, gx1 = ground["x"] + 20, ground["x"] + ground["w"] - 20
        gy = ground["y"]

        tgt = w.nearest_enemy(f)
        hid = f.hero.id

        # --- возврат на арену
        danger = (f.cx < gx0 - 40 or f.cx > gx1 + 40) and f.cy > gy - 220
        if danger or f.cy > gy + 140:
            cx = (gx0 + gx1) * 0.5
            self.move = 1 if cx > f.cx else -1
            inp |= C.IN_RIGHT if self.move > 0 else C.IN_LEFT
            if f.vy > -60 and f.jumps > 0 and self.t % 7 == 0:
                inp |= C.IN_JUMP
            if f.dash_cd <= 0 and abs(f.cx - cx) > 260 and self.t % 13 == 0:
                inp |= C.IN_DASH
            to = float(self.move)              # знак «в сторону арены»
            f.aim_x, f.aim_y = to, -0.2
            # --- спасаемся способностями, а не только прыжками:
            # без этого симуляция не видит ни рывка Резака, ни воронки Якоря,
            # и любой герой с возвратом меряется как герой без возврата
            rec = RECOVERY.get(hid)
            if rec is not None and f.cy > gy - 170:
                key, bit, ax, ay = rec
                if f.cds[key] <= 0:
                    f.aim_x, f.aim_y = to * ax, ay
                    inp |= bit
            f.inp = inp
            return

        if tgt is None:
            f.inp = 0
            return

        dx = tgt.cx - f.cx
        dy = tgt.cy - f.cy
        dist = math.hypot(dx, dy)
        want = PREF_RANGE.get(f.hero.id, 200)
        melee = hid in MELEE
        # враг вне арены — его надо добивать, а не ждать на своей половине
        edge = (tgt.cx < gx0 - 30 or tgt.cx > gx1 + 30 or tgt.cy > gy + 70)
        just_hurt = self.t - self.hurt_t < 24

        # --- прицел (с упреждением и погрешностью)
        lead = 0.12 if f.hero.id in LEADERS else 0.0
        ax = dx + tgt.vx * lead
        ay = dy + tgt.vy * lead - 6
        miss = MISS[self.level]
        ax += r.uniform(-1, 1) * dist * miss
        ay += r.uniform(-1, 1) * dist * miss * 0.6
        n = math.hypot(ax, ay) or 1.0
        f.aim_x, f.aim_y = ax / n, ay / n

        # --- перемещение (решение пересматриваем не каждый тик)
        if self.t % REACT[self.level] == 0:
            if dist > want * 1.25:
                self.move = 1 if dx > 0 else -1
            elif dist < want * 0.55:
                self.move = -1 if dx > 0 else 1
            else:
                self.move = r.choice((0, 0, 1, -1))
            # дальнобойный не стоит в ближнем бою: разрываем дистанцию
            if not melee and dist < 175:
                self.move = -1 if dx > 0 else 1
            # враг за краем — подходим добивать (клэмп ниже не даст свалиться)
            if edge and dist > 120:
                self.move = 1 if dx > 0 else -1
            # не убегаем за край (у края подходим ближе — иначе не дотянемся)
            nx = f.cx + self.move * (60 if edge else 120)
            if nx < gx0 or nx > gx1:
                self.move = -self.move
            self.want_jump = (dy < -70 and f.on_ground) or (
                dist < 140 and melee and r.random() < 0.2)

        if self.move > 0:
            inp |= C.IN_RIGHT
        elif self.move < 0:
            inp |= C.IN_LEFT
        if self.want_jump and f.jumps > 0 and self.t % 9 == 0:
            inp |= C.IN_JUMP
            self.want_jump = False
        if dy > 130 and not f.on_ground:
            inp |= C.IN_DOWN

        # --- атаки (нажатия делаем импульсами через чётность тика)
        beat = self.t % 4 == 0
        in_range = dist < (115 if melee else 700)
        los = abs(dy) < 260
        if edge and dist < 780:
            los = True          # вслед за край стреляем под любым углом
            in_range = in_range or not melee

        if beat and in_range and los and f.cds["basic"] <= 0:
            inp |= C.IN_BASIC

        if f.cds["q"] <= 0 and self.t % 5 == 1:
            use = False
            if hid == "rezak":
                use = 150 < dist < 420
            elif hid == "bunker":
                use = (dist < 260 and f.hp < f.max_hp * 0.75) or incoming(w, f)
            elif hid == "igla":
                use = dist > 250 and los
            elif hid == "vyuga":
                use = 120 < dist < 400
            elif hid == "gayka":
                use = dist < 520
            elif hid == "puls":
                use = f.hp < f.max_hp * 0.8 or dist < 300
            elif hid == "gorn":
                use = 150 < dist < 620 and abs(dy) < 300
            elif hid == "zerkalo":
                use = dist < 175 or incoming(w, f, 200)
            elif hid == "yakor":
                use = 130 < dist < 430
            if use:
                inp |= C.IN_Q
        # Игла держит заряд
        if hid == "igla" and f.act is not None and f.act.key == "q" and f.act.t < 62:
            inp |= C.IN_Q

        if f.cds["e"] <= 0 and self.t % 7 == 2:
            use = False
            if hid == "rezak":
                use = dist < 150
            elif hid == "bunker":
                use = 140 < dist < 520 and abs(dy) < 120
            elif hid == "igla":
                use = dist < 220
            elif hid == "vyuga":
                use = dist < 210
            elif hid == "gayka":
                use = 180 < dist < 520 and los
            elif hid == "puls":
                use = dist < 200 or incoming(w, f, 170)
            elif hid == "gorn":
                use = dist < 250 and abs(dy) < 140
            elif hid == "zerkalo":
                use = dist < 150
            elif hid == "yakor":
                use = dist < 320 and abs(dy) < 220
            if use:
                inp |= C.IN_E

        if f.ult >= C.ULT_MAX and f.cds["r"] <= 0 and self.t % 11 == 3:
            use = dist < 900
            if hid == "puls":
                use = f.hp < f.max_hp * 0.7 or any(
                    o.hp < o.max_hp * 0.6 for o in w.allies_of(f, False))
            elif hid == "zerkalo":
                use = dist < 260 or f.hp < f.max_hp * 0.6
            elif hid in ("gorn", "yakor"):
                use = dist < 520 and abs(dy) < 320
            if use:
                inp |= C.IN_R

        if f.dash_cd <= 0 and self.level >= 2:
            away = (not melee and (dist < want * 0.55 or (just_hurt and dist < 260)))
            close = melee and dist > want * 2 and self.t % 17 == 5
            if away or close:
                d = 1 if dx > 0 else -1
                nx = f.cx + d * (300 if close else -300)
                if gx0 < nx < gx1:
                    inp |= C.IN_DASH

        f.inp = inp
