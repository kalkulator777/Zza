"""Серверная симуляция боя. Полностью детерминирована и не знает про сеть."""

import math
import random

from . import arena as arena_mod
from . import const as C
from .entities import (Action, Fighter, Hitbox, Projectile, Structure, Zone,
                       ang_vec, circle_rect, rects_overlap)


class World:
    def __init__(self, arena_id, players, stocks=C.DEFAULT_STOCKS, time_limit=C.MATCH_TIME_LIMIT):
        from . import heroes as H

        self.arena = arena_mod.get(arena_id)
        self.platforms = list(self.arena["platforms"])
        self.blast = self.arena["blast"]
        self.stocks = stocks
        self.time_limit = time_limit
        self.frame = 0
        self.time = 0.0
        self.state = C.ST_COUNTDOWN
        self.state_t = C.COUNTDOWN
        self.winner = None
        self._id = 0
        self.rng = random.Random(1337)

        self.fighters = {}
        self.order = []
        team_slots = [0, 0]
        for p in players:
            hero = H.get(p["hero"])
            team = int(p["team"]) & 1
            slot = team_slots[team]
            team_slots[team] += 1
            f = Fighter(p["pid"], team, slot, p["name"], hero, p.get("bot", False))
            f.stocks = stocks
            self._place_at_spawn(f)
            if hero.on_spawn:
                hero.on_spawn(self, f)
            self.fighters[f.pid] = f
            self.order.append(f.pid)

        self.hitboxes = []
        self.projectiles = []
        self.structs = []
        self.zones = []
        self.events = []
        self.shake = 0.0

    # ------------------------------------------------------------------ utils
    def new_id(self):
        self._id += 1
        return self._id

    def fx(self, kind, x, y, **kw):
        if len(self.events) > 220:
            return
        e = {"k": kind, "x": round(x, 1), "y": round(y, 1)}
        e.update(kw)
        self.events.append(e)

    def alive_fighters(self):
        return [f for f in self.fighters.values() if f.alive]

    def enemies_of(self, f):
        return [o for o in self.fighters.values() if o.alive and o.team != f.team]

    def allies_of(self, f, include_self=True):
        return [o for o in self.fighters.values()
                if o.alive and o.team == f.team and (include_self or o.pid != f.pid)]

    def nearest_enemy(self, f, max_dist=1e9):
        best, bd = None, max_dist * max_dist
        for o in self.enemies_of(f):
            d = (o.cx - f.cx) ** 2 + (o.cy - f.cy) ** 2
            if d < bd:
                bd, best = d, o
        return best

    def nearest_ally(self, f, max_dist=1e9):
        best, bd = None, max_dist * max_dist
        for o in self.allies_of(f, include_self=False):
            d = (o.cx - f.cx) ** 2 + (o.cy - f.cy) ** 2
            if d < bd:
                bd, best = d, o
        return best

    def _place_at_spawn(self, f):
        pts = self.arena["spawns"][f.team]
        x, y = pts[f.slot % len(pts)]
        f.x, f.y = float(x) - C.FW / 2, float(y) - C.FH
        f.vx = f.vy = 0.0
        f.face = 1 if f.team == 0 else -1

    # -------------------------------------------------------------- спавнеры
    def spawn_hitbox(self, owner, ox, oy, w, h, dmg, kb, angle, ttl=3,
                     follow=False, on_hit=None, fx="hit", hitstun_mul=1.0,
                     hits_structs=True, tag="", abs_angle=False):
        """ox/oy — смещение от центра бойца, ox зеркалится по направлению взгляда."""
        face = owner.face
        ax = owner.cx + ox * face - w / 2
        ay = owner.cy + oy - h / 2
        ang = angle if (abs_angle or face > 0) else (180.0 - angle)
        hb = Hitbox(self.new_id(), owner.pid, owner.team, ax, ay, w, h,
                    dmg, kb, ang, ttl, follow=owner.pid if follow else None,
                    ox=ox * face, oy=oy, on_hit=on_hit, fx=fx,
                    hitstun_mul=hitstun_mul, hits_structs=hits_structs, tag=tag)
        self.hitboxes.append(hb)
        return hb

    def spawn_hitbox_at(self, owner, x, y, w, h, dmg, kb, angle, ttl=3,
                        on_hit=None, fx="hit", hitstun_mul=1.0, radial=False, tag=""):
        hb = Hitbox(self.new_id(), owner.pid, owner.team, x - w / 2, y - h / 2, w, h,
                    dmg, kb, angle, ttl, on_hit=on_hit, fx=fx,
                    hitstun_mul=hitstun_mul, tag=tag)
        hb.freeze_angle = not radial
        if radial:
            hb.angle = None  # отброс от центра
        self.hitboxes.append(hb)
        return hb

    def spawn_proj(self, owner, kind, vx, vy, dmg, kb, ttl, r=10.0, ox=0.0, oy=0.0,
                   grav=0.0, pierce=1, on_hit=None, terrain=True, heal=0.0,
                   angle=None, data=None, homing=0.0):
        p = Projectile(self.new_id(), owner.pid, owner.team, kind,
                       owner.cx + ox * owner.face, owner.cy + oy,
                       vx, vy, r, dmg, kb, ttl, angle=angle, grav=grav,
                       pierce=pierce, on_hit=on_hit, terrain=terrain, heal=heal,
                       data=data, homing=homing)
        self.projectiles.append(p)
        self.fx("shoot", p.x, p.y, kk=kind)
        return p

    def spawn_struct(self, owner, kind, x, y, w, h, hp, ttl, solid=False,
                     blocks_shots=False, data=None):
        s = Structure(self.new_id(), owner.pid, owner.team, kind, x - w / 2, y - h,
                      w, h, hp, ttl, solid=solid, blocks_shots=blocks_shots, data=data)
        self.structs.append(s)
        self.fx("build", s.cx, s.cy, kk=kind)
        return s

    def spawn_zone(self, owner, kind, x, y, r, ttl, on_tick=None, on_end=None,
                   data=None, follow=False):
        z = Zone(self.new_id(), owner.pid, owner.team, kind, x, y, r, ttl,
                 on_tick=on_tick, on_end=on_end, data=data,
                 follow=owner.pid if follow else None)
        self.zones.append(z)
        return z

    # ---------------------------------------------------------------- урон
    def damage(self, src, target, dmg, kb=0.0, angle=0.0, hitstun_mul=1.0,
               fx="hit", ignore_iframes=False, radial_from=None, tag=""):
        if not target.alive or self.state != C.ST_PLAY:
            return 0.0
        if not ignore_iframes and (target.iframes > 0 or target.has("invuln")):
            return 0.0

        dmg = float(dmg)
        if src is not None:
            dmg *= src.dmg_mul
            if src.hero.on_deal:
                dmg = src.hero.on_deal(self, src, target, dmg, tag)
        if target.hero.on_take:
            dmg = target.hero.on_take(self, target, src, dmg, tag)
        dmg *= max(0.0, 1.0 - target.dr)

        if target.shield > 0:
            absorbed = min(target.shield, dmg)
            target.shield -= absorbed
            dmg -= absorbed
            self.fx("shieldhit", target.cx, target.cy)

        dmg = max(0.0, dmg)
        target.hp -= dmg
        target.dmg_taken += dmg
        target.no_dmg_t = 0.0
        if src is not None and src.pid != target.pid:
            src.dmg_dealt += dmg
            src.ult = min(C.ULT_MAX, src.ult + dmg * C.ULT_DEALT)
            target.last_hit_by = src.pid
            target.last_hit_t = self.time
        target.ult = min(C.ULT_MAX, target.ult + dmg * C.ULT_TAKEN)

        # отброс
        if kb > 0:
            rage = 1.0 + (1.0 - max(0.0, target.hp) / target.max_hp) * C.KB_RAGE
            mag = kb * rage / max(0.4, target.hero.weight)
            if target.act is not None:
                mag *= (1.0 - target.act.armor)
            if target.has("anchor"):
                mag *= 0.35
            if radial_from is not None:
                dx = target.cx - radial_from[0]
                dy = target.cy - radial_from[1]
                d = math.hypot(dx, dy) or 1.0
                ux, uy = dx / d, dy / d * 0.9 - 0.25
                n = math.hypot(ux, uy) or 1.0
                ux, uy = ux / n, uy / n
            else:
                ux, uy = ang_vec(angle)
            target.vx = ux * mag
            target.vy = uy * mag
            target.on_ground = False
            hs = min(C.HITSTUN_MAX, max(C.HITSTUN_MIN, mag * C.HITSTUN_PER_KB)) * hitstun_mul
            target.hitstun = max(target.hitstun, hs)
            target.act = None
            target.dash_t = 0.0
            target.iframes = max(target.iframes, C.HIT_IFRAMES)
            target.hitlag = max(target.hitlag, min(5, 1 + int(dmg / 9)))
            self.shake = min(18.0, self.shake + mag * 0.006)

        if dmg > 0:
            self.fx(fx, target.cx, target.cy, a=round(angle, 0), m=round(dmg, 1))
        if target.hp <= 0:
            self._kill(target, src)
        return dmg

    def damage_struct(self, src, s, dmg):
        s.hp -= dmg
        if src is not None:
            src.ult = min(C.ULT_MAX, src.ult + dmg * 0.15)
        self.fx("hit", s.cx, s.cy, m=round(dmg, 1))
        if s.hp <= 0:
            self.fx("boom", s.cx, s.cy, r=30)

    def heal(self, target, amount, src=None):
        if not target.alive:
            return 0.0
        before = target.hp
        target.hp = min(target.max_hp, target.hp + amount)
        got = target.hp - before
        if got > 0.5:
            self.fx("heal", target.cx, target.cy, m=round(got, 1))
        return got

    def _kill(self, f, src, out_of_bounds=False):
        if not f.alive:
            return
        f.alive = False
        f.deaths += 1
        f.stocks -= 1
        f.hp = 0.0
        f.act = None
        f.effects.clear()
        f.shield = 0.0
        f.hitstun = 0.0
        f.dash_t = 0.0
        f.respawn_t = C.RESPAWN_TIME
        killer = src
        if killer is None and f.last_hit_by is not None and self.time - f.last_hit_t < 6.0:
            killer = self.fighters.get(f.last_hit_by)
        if killer is not None and killer.pid != f.pid and killer.team != f.team:
            killer.kills += 1
            killer.ult = min(C.ULT_MAX, killer.ult + 12.0)
        self.fx("death", f.cx, f.cy, p=f.pid, ob=1 if out_of_bounds else 0)
        self.events.append({"k": "ko", "pid": f.pid,
                            "by": killer.pid if killer else None, "x": round(f.cx, 1),
                            "y": round(f.cy, 1)})
        self.shake = min(22.0, self.shake + 10.0)
        # чистим принадлежащие ему конструкции
        for s in self.structs:
            if s.owner == f.pid and s.kind in ("turret", "mine"):
                s.ttl = 0.0

    # ------------------------------------------------------------- физика
    def _solids(self):
        out = [(p["x"], p["y"], p["w"], p["h"], p["oneway"]) for p in self.platforms]
        for s in self.structs:
            if s.solid and s.hp > 0:
                out.append((s.x, s.y, s.w, s.h, False))
        return out

    def _physics(self, f, solids):
        # горизонталь
        f.x += f.vx * C.DT
        for (px, py, pw, ph, oneway) in solids:
            if oneway:
                continue
            if rects_overlap(f.x, f.y, C.FW, C.FH, px, py, pw, ph):
                # не выталкиваем, если стоим сверху (мелкая ступенька)
                if f.y + C.FH <= py + 6 and f.vy >= 0:
                    continue
                if f.vx > 0:
                    f.x = px - C.FW
                elif f.vx < 0:
                    f.x = px + pw
                f.vx = 0.0

        # вертикаль
        prev_bottom = f.y + C.FH
        f.y += f.vy * C.DT
        f.on_ground = False
        f.mem["oneway"] = False
        for (px, py, pw, ph, oneway) in solids:
            if not rects_overlap(f.x, f.y, C.FW, C.FH, px, py, pw, ph):
                continue
            if oneway:
                if f.vy < 0 or f.platform_drop > 0 or prev_bottom > py + 8:
                    continue
                f.y = py - C.FH
                f.vy = 0.0
                f.on_ground = True
                f.mem["oneway"] = True
            else:
                if f.vy >= 0 and prev_bottom <= py + max(14.0, abs(f.vy) * C.DT + 2):
                    f.y = py - C.FH
                    f.vy = 0.0
                    f.on_ground = True
                elif f.vy < 0:
                    f.y = py + ph
                    f.vy = 0.0
                else:
                    # выталкиваем вбок как аварийный случай
                    f.x += (C.FW if f.cx > px + pw / 2 else -C.FW) * 0.4
        if f.on_ground:
            f.jumps = f.hero.jumps

    def _control(self, f):
        frozen = f.hitstun > 0 or f.has("freeze") or f.has("stun")
        ctrl = 0.0 if frozen else (1.0 if f.act is None else f.act.move)

        if not frozen and not f.act:
            f.mem.pop("chan", None)

        # направление взгляда — по прицелу
        if not frozen and (f.act is None or not f.act.face_lock):
            if abs(f.aim_x) > 0.05:
                f.face = 1 if f.aim_x > 0 else -1

        move = 0.0
        if f.held(C.IN_LEFT):
            move -= 1.0
        if f.held(C.IN_RIGHT):
            move += 1.0

        if f.dash_t > 0:
            return

        speed = C.RUN_SPEED * f.hero.speed * f.speed_mul
        target = move * speed * ctrl
        if abs(move) > 0.01 and ctrl > 0:
            acc = (C.GROUND_ACCEL if f.on_ground else C.AIR_ACCEL) * C.DT
            if f.vx < target:
                f.vx = min(target, f.vx + acc)
            else:
                f.vx = max(target, f.vx - acc)
        else:
            fr = (C.GROUND_FRICTION if f.on_ground else C.AIR_FRICTION) * C.DT
            if abs(f.vx) <= fr:
                f.vx = 0.0
            else:
                f.vx -= math.copysign(fr, f.vx)

        if frozen:
            return

        # прыжок / падение сквозь платформу
        if f.pressed(C.IN_JUMP):
            if f.on_ground and f.held(C.IN_DOWN) and f.mem.get("oneway"):
                f.platform_drop = 0.22
                f.y += 4
                f.on_ground = False
            elif f.jumps > 0 and (f.act is None or f.act.move > 0.4):
                f.vy = -(C.JUMP_VEL if f.on_ground else C.DJUMP_VEL)
                f.jumps -= 1
                f.on_ground = False
                self.fx("jump", f.cx, f.y + C.FH)

        # рывок
        if f.pressed(C.IN_DASH) and f.dash_cd <= 0 and (f.act is None or f.act.move > 0.4):
            d = 0
            if f.held(C.IN_LEFT):
                d -= 1
            if f.held(C.IN_RIGHT):
                d += 1
            if d == 0:
                d = f.face
            f.vx = C.DASH_SPEED * d
            f.vy = min(f.vy, 0.0) * 0.2
            f.dash_t = C.DASH_TIME
            f.dash_cd = C.DASH_CD
            f.iframes = max(f.iframes, C.DASH_IFRAMES)
            f.act = None
            self.fx("dash", f.cx, f.cy, d=d)

    def _try_cast(self, f):
        if f.hitstun > 0 or f.has("freeze") or f.has("stun") or f.dash_t > 0:
            return
        if f.act is not None:
            return
        h = f.hero
        for bit, key in ((C.IN_BASIC, "basic"), (C.IN_Q, "q"), (C.IN_E, "e"), (C.IN_R, "r")):
            if not f.pressed(bit):
                continue
            ab = h.ab.get(key)
            if ab is None or f.cds[key] > 0:
                continue
            if key == "r":
                if f.ult < C.ULT_MAX:
                    continue
                f.ult = 0.0
                self.events.append({"k": "ult", "pid": f.pid, "hero": h.id})
            if key == "basic":
                if f.combo_t <= 0:
                    f.combo = 0
                f.combo_t = 0.9
            f.cds[key] = ab.cd
            f.act = Action(key, ab.fn, max(1, int(round(ab.dur * C.TICK))),
                           move=ab.move, face_lock=ab.face_lock, armor=ab.armor)
            self.fx("cast", f.cx, f.cy, kk=h.id + ":" + key)
            return

    # --------------------------------------------------------------- эффекты
    def _effects(self, f):
        f.dr = 0.0
        f.speed_mul = 1.0
        f.dmg_mul = 1.0
        dead = []
        for name, e in f.effects.items():
            e["t"] -= C.DT
            if e["t"] <= 0:
                dead.append(name)
                continue
            m = e["mag"]
            if name == "slow":
                f.speed_mul *= max(0.15, 1.0 - m)
            elif name == "haste":
                f.speed_mul *= (1.0 + m)
            elif name == "dr":
                f.dr = max(f.dr, m)
            elif name == "dmg_up":
                f.dmg_mul *= (1.0 + m)
            elif name == "weak":
                f.dmg_mul *= max(0.2, 1.0 - m)
            elif name == "invuln":
                f.iframes = max(f.iframes, C.DT * 2)
            elif name == "burn":
                self.damage(self.fighters.get(e.get("src")), f, m * C.DT, fx="burn")
            elif name == "regen":
                self.heal(f, m * C.DT)
        for name in dead:
            f.effects.pop(name, None)

    # ------------------------------------------------------------- снаряды
    def _blocked_by_struct(self, team, x, y, r):
        for s in self.structs:
            if s.hp <= 0 or s.team == team:
                continue
            if s.blocks_shots and circle_rect(x, y, r, s.x, s.y, s.w, s.h):
                return s
        return None

    def _update_projectiles(self):
        alive = []
        for p in self.projectiles:
            p.ttl -= C.DT
            if p.ttl <= 0:
                self.fx("pdie", p.x, p.y, kk=p.kind)
                continue
            if p.homing > 0:
                owner = self.fighters.get(p.owner)
                tgt = None
                if owner:
                    bd = 1e18
                    for o in self.enemies_of(owner):
                        d = (o.cx - p.x) ** 2 + (o.cy - p.y) ** 2
                        if d < bd:
                            bd, tgt = d, o
                if tgt:
                    sp = math.hypot(p.vx, p.vy) or 1.0
                    dx, dy = tgt.cx - p.x, tgt.cy - p.y
                    d = math.hypot(dx, dy) or 1.0
                    p.vx += (dx / d * sp - p.vx) * p.homing * C.DT
                    p.vy += (dy / d * sp - p.vy) * p.homing * C.DT
            p.vy += p.grav * C.DT
            p.x += p.vx * C.DT
            p.y += p.vy * C.DT

            if (p.x < self.blast["x0"] - 200 or p.x > self.blast["x1"] + 200
                    or p.y < self.blast["y0"] - 200 or p.y > self.blast["y1"]):
                continue

            if p.terrain:
                hitwall = False
                for (px, py, pw, ph, oneway) in self._solids():
                    if oneway:
                        continue
                    if circle_rect(p.x, p.y, p.r, px, py, pw, ph):
                        hitwall = True
                        break
                if hitwall:
                    self.fx("pdie", p.x, p.y, kk=p.kind)
                    if p.on_hit:
                        p.on_hit(self, p, None)
                    continue
            blocker = self._blocked_by_struct(p.team, p.x, p.y, p.r)
            if blocker is not None:
                self.fx("pdie", p.x, p.y, kk=p.kind)
                continue

            owner = self.fighters.get(p.owner)
            consumed = False
            for t in self.fighters.values():
                if not t.alive or t.pid in p.hit:
                    continue
                friendly = (t.team == p.team)
                if friendly and p.heal <= 0:
                    continue
                if friendly and t.pid == p.owner:
                    continue
                if not circle_rect(p.x, p.y, p.r + 6, t.x, t.y, C.FW, C.FH):
                    continue
                p.hit.add(t.pid)
                if friendly:
                    self.heal(t, p.heal, owner)
                    if p.on_hit:
                        p.on_hit(self, p, t)
                    consumed = True
                    break
                ang = p.angle
                if ang is None:
                    ang = math.degrees(math.atan2(-p.vy, p.vx))
                self.damage(owner, t, p.dmg, p.kb, ang, tag=p.kind)
                if p.on_hit:
                    p.on_hit(self, p, t)
                p.pierce -= 1
                if p.pierce <= 0:
                    consumed = True
                    break
            if consumed:
                continue

            # снаряды бьют вражеские постройки
            for s in self.structs:
                if s.hp <= 0 or s.team == p.team or s.id in p.hit or s.kind == "wall":
                    continue
                if circle_rect(p.x, p.y, p.r, s.x, s.y, s.w, s.h):
                    p.hit.add(s.id)
                    self.damage_struct(owner, s, p.dmg)
                    p.pierce -= 1
                    if p.pierce <= 0:
                        consumed = True
                    break
            if consumed:
                continue
            alive.append(p)
        self.projectiles = alive

    def _update_hitboxes(self):
        alive = []
        for hb in self.hitboxes:
            if hb.follow is not None:
                o = self.fighters.get(hb.follow)
                if o is None or not o.alive:
                    continue
                hb.x = o.cx + hb.ox - hb.w / 2
                hb.y = o.cy + hb.oy - hb.h / 2
            owner = self.fighters.get(hb.owner)
            cx, cy = hb.x + hb.w / 2, hb.y + hb.h / 2
            for t in self.fighters.values():
                if not t.alive or t.team == hb.team or t.pid in hb.hit:
                    continue
                if not rects_overlap(hb.x, hb.y, hb.w, hb.h, t.x, t.y, C.FW, C.FH):
                    continue
                hb.hit.add(t.pid)
                if hb.angle is None:
                    self.damage(owner, t, hb.dmg, hb.kb, 0, hb.hitstun_mul,
                                hb.fx, radial_from=(cx, cy), tag=hb.tag)
                else:
                    self.damage(owner, t, hb.dmg, hb.kb, hb.angle, hb.hitstun_mul,
                                hb.fx, tag=hb.tag)
                if hb.on_hit:
                    hb.on_hit(self, owner, t)
            if hb.hits_structs:
                for s in self.structs:
                    if s.hp <= 0 or s.team == hb.team or s.id in hb.hit:
                        continue
                    if rects_overlap(hb.x, hb.y, hb.w, hb.h, s.x, s.y, s.w, s.h):
                        hb.hit.add(s.id)
                        self.damage_struct(owner, s, hb.dmg)
            hb.ttl -= 1
            if hb.ttl > 0:
                alive.append(hb)
        self.hitboxes = alive

    def _update_structs(self):
        alive = []
        for s in self.structs:
            s.t += C.DT
            s.ttl -= C.DT
            fn = s.data.get("tick")
            if fn:
                fn(self, s)
            if s.ttl > 0 and s.hp > 0:
                alive.append(s)
            else:
                end = s.data.get("end")
                if end:
                    end(self, s)
                self.fx("pdie", s.cx, s.cy, kk=s.kind)
        self.structs = alive

    def _update_zones(self):
        alive = []
        for z in self.zones:
            if z.follow is not None:
                o = self.fighters.get(z.follow)
                if o and o.alive:
                    z.x, z.y = o.cx, o.cy
            z.ttl -= C.DT
            z.tick += C.DT
            if z.on_tick:
                z.on_tick(self, z)
            if z.ttl > 0:
                alive.append(z)
            elif z.on_end:
                z.on_end(self, z)
        self.zones = alive

    def in_zone(self, z, f):
        return circle_rect(z.x, z.y, z.r, f.x, f.y, C.FW, C.FH)

    # ----------------------------------------------------------------- шаг
    def step(self):
        self.frame += 1
        self.shake = max(0.0, self.shake - 45.0 * C.DT)

        if self.state == C.ST_COUNTDOWN:
            self.state_t -= C.DT
            for f in self.fighters.values():
                f.prev_inp = f.inp
            if self.state_t <= 0:
                self.state = C.ST_PLAY
                self.state_t = 0.0
                self.events.append({"k": "go"})
            return
        if self.state == C.ST_OVER:
            self.state_t -= C.DT
            return

        self.time += C.DT
        solids = self._solids()

        for pid in self.order:
            f = self.fighters[pid]
            # таймеры
            for k in f.cds:
                if f.cds[k] > 0:
                    f.cds[k] = max(0.0, f.cds[k] - C.DT)
            f.dash_cd = max(0.0, f.dash_cd - C.DT)
            f.iframes = max(0.0, f.iframes - C.DT)
            f.hitstun = max(0.0, f.hitstun - C.DT)
            f.platform_drop = max(0.0, f.platform_drop - C.DT)
            f.combo_t = max(0.0, f.combo_t - C.DT)
            f.no_dmg_t += C.DT
            f.ult = min(C.ULT_MAX, f.ult + C.ULT_PER_SEC * C.DT)

            if not f.alive:
                if f.stocks > 0:
                    f.respawn_t -= C.DT
                    if f.respawn_t <= 0:
                        f.alive = True
                        f.hp = f.max_hp
                        f.shield = 0.0
                        f.effects.clear()
                        f.iframes = C.SPAWN_INVULN
                        f.jumps = f.hero.jumps
                        self._place_at_spawn(f)
                        if f.hero.on_spawn:
                            f.hero.on_spawn(self, f)
                        self.fx("spawn", f.cx, f.cy, p=f.pid)
                f.prev_inp = f.inp
                continue

            if f.hitlag > 0:
                f.hitlag -= 1
                f.prev_inp = f.inp
                continue

            self._effects(f)
            if f.hero.on_tick:
                f.hero.on_tick(self, f)
            self._control(f)
            self._try_cast(f)

            if f.act is not None:
                act = f.act
                act.fn(self, f, act)
                if f.act is act:            # способность могла завершиться сама
                    act.t += 1
                    if act.t >= act.dur:
                        f.act = None

            # гравитация
            if f.dash_t > 0:
                f.dash_t -= C.DT
                if f.dash_t <= 0:
                    f.vx *= 0.45
            elif not f.mem.get("nograv"):
                g = C.GRAVITY * f.hero.grav
                if f.held(C.IN_DOWN) and f.vy > 0:
                    g *= C.FASTFALL_MUL
                f.vy = min(C.MAX_FALL, f.vy + g * C.DT)
            f.mem.pop("nograv", None)

            self._physics(f, solids)
            f.prev_inp = f.inp

        self._update_hitboxes()
        self._update_projectiles()
        self._update_structs()
        self._update_zones()

        # вылет за границы
        for f in self.fighters.values():
            if not f.alive:
                continue
            if (f.cx < self.blast["x0"] or f.cx > self.blast["x1"]
                    or f.cy > self.blast["y1"] or f.cy < self.blast["y0"]):
                self._kill(f, None, out_of_bounds=True)

        self._check_end()

    def _team_stocks(self, team):
        return sum(max(0, f.stocks) for f in self.fighters.values() if f.team == team)

    def _check_end(self):
        if self.state != C.ST_PLAY:
            return
        t0 = self._team_stocks(0)
        t1 = self._team_stocks(1)
        if t0 <= 0 or t1 <= 0:
            self.winner = 0 if t1 <= 0 and t0 > 0 else (1 if t0 <= 0 and t1 > 0 else -1)
            self.state = C.ST_OVER
            self.state_t = C.POST_MATCH
            self.events.append({"k": "end", "w": self.winner})
            return
        if self.time_limit and self.time >= self.time_limit:
            if t0 != t1:
                self.winner = 0 if t0 > t1 else 1
            else:
                h0 = sum(f.hp for f in self.fighters.values() if f.team == 0)
                h1 = sum(f.hp for f in self.fighters.values() if f.team == 1)
                self.winner = 0 if h0 > h1 else (1 if h1 > h0 else -1)
            self.state = C.ST_OVER
            self.state_t = C.POST_MATCH
            self.events.append({"k": "end", "w": self.winner, "time": 1})

    # ------------------------------------------------------------- снапшот
    def snapshot(self):
        ps = []
        for pid in self.order:
            f = self.fighters[pid]
            ef = [k for k in f.effects if k in
                  ("slow", "freeze", "stun", "invuln", "haste", "dr", "dmg_up", "burn", "regen")]
            ps.append({
                "i": f.pid,
                "x": round(f.cx, 1), "y": round(f.cy, 1),
                "vx": round(f.vx, 1), "vy": round(f.vy, 1),
                "f": f.face,
                "hp": round(max(0.0, f.hp), 1),
                "sh": round(f.shield, 1),
                "st": max(0, f.stocks),
                "u": round(f.ult, 1),
                "a": (f.act.key if f.act else ("dash" if f.dash_t > 0 else "")),
                "ap": round(f.act.t / f.act.dur, 2) if f.act else 0,
                "al": 1 if f.alive else 0,
                "rt": round(max(0.0, f.respawn_t), 1),
                "iv": 1 if (f.iframes > 0 or f.has("invuln")) else 0,
                "hs": 1 if f.hitstun > 0 else 0,
                "g": 1 if f.on_ground else 0,
                "cd": [round(f.cds["q"], 1), round(f.cds["e"], 1), round(f.dash_cd, 1)],
                "ax": round(f.aim_x, 2), "ay": round(f.aim_y, 2),
            })
        objs = [{"i": p.id, "k": p.kind, "x": round(p.x, 1), "y": round(p.y, 1),
                 "r": round(p.r, 1), "t": p.team,
                 "a": round(math.degrees(math.atan2(-p.vy, p.vx)), 0)}
                for p in self.projectiles]
        zs = [{"i": z.id, "k": z.kind, "x": round(z.x, 1), "y": round(z.y, 1),
               "r": round(z.r, 1), "t": z.team,
               "p": round(max(0.0, z.ttl) / z.max_ttl, 2)} for z in self.zones]
        bs = [{"i": s.id, "k": s.kind, "x": round(s.x, 1), "y": round(s.y, 1),
               "w": s.w, "h": s.h, "t": s.team,
               "hp": round(max(0.0, s.hp) / s.max_hp, 2),
               "d": round(s.data.get("aim", 0.0), 0)} for s in self.structs]
        snap = {
            "t": "s", "f": self.frame,
            "st": self.state, "ct": round(self.state_t, 2),
            "tl": round(max(0.0, (self.time_limit - self.time)) if self.time_limit else 0, 1),
            "p": ps, "o": objs, "z": zs, "b": bs,
            "e": self.events, "sk": round(self.shake, 1),
            "w": self.winner,
        }
        self.events = []
        return snap

    def scoreboard(self):
        return [{"pid": f.pid, "name": f.name, "hero": f.hero.id, "team": f.team,
                 "kills": f.kills, "deaths": f.deaths,
                 "dmg": round(f.dmg_dealt), "taken": round(f.dmg_taken),
                 "stocks": max(0, f.stocks)} for f in self.fighters.values()]
