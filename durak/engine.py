# -*- coding: utf-8 -*-
"""Правила игры «Дурак» (подкидной / переводной).

Модуль намеренно не знает ничего про сеть, tornado и JSON: это чистая
логика, которую можно гонять в юнит-тестах. Сервер вызывает только
Game.apply() и Game.view_for().
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

# --- карты -----------------------------------------------------------------

SUITS = ("S", "H", "D", "C")  # пики, черви, бубны, трефы
RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")

# для читаемых сообщений в журнале
SUIT_CHARS = {"S": "\u2660", "H": "\u2665", "D": "\u2666", "C": "\u2663"}
RANK_CHARS = {"J": "\u0412", "Q": "\u0414", "K": "\u041a", "A": "\u0422"}


def plural(n, one, few, many):
    """«1 карту», «2 карты», «5 карт»."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "%d %s" % (n, one)
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "%d %s" % (n, few)
    return "%d %s" % (n, many)

# размер колоды -> индекс младшего ранга в RANKS
DECK_SIZES = {24: 7, 32: 5, 36: 4, 52: 0}
HAND_SIZE = 6


class GameError(Exception):
    """Недопустимый ход. Текст показывается игроку."""


@dataclass(frozen=True, order=True)
class Card:
    rank: int  # индекс в RANKS
    suit: str

    @property
    def id(self) -> str:
        return RANKS[self.rank] + self.suit

    @property
    def label(self) -> str:
        """Как карта выглядит в журнале: «В\u2663», «10\u2666»."""
        rank = RANKS[self.rank]
        return RANK_CHARS.get(rank, rank) + SUIT_CHARS[self.suit]

    def __str__(self) -> str:
        return self.id


def card_from_id(cid: str) -> Card:
    if not cid or cid[-1] not in SUITS:
        raise GameError("Неизвестная карта: %s" % cid)
    rank = cid[:-1]
    if rank not in RANKS:
        raise GameError("Неизвестная карта: %s" % cid)
    return Card(RANKS.index(rank), cid[-1])


def build_deck(size: int) -> list:
    if size not in DECK_SIZES:
        raise GameError("Неподдерживаемый размер колоды: %s" % size)
    low = DECK_SIZES[size]
    return [Card(r, s) for s in SUITS for r in range(low, len(RANKS))]


def beats(defence: Card, attack: Card, trump: str) -> bool:
    """Бьёт ли карта defence карту attack при козыре trump."""
    if defence.suit == attack.suit:
        return defence.rank > attack.rank
    return defence.suit == trump and attack.suit != trump


# --- настройки -------------------------------------------------------------

THROW_IN_ALL = "all"          # подкидывают все
THROW_IN_NEIGHBORS = "neighbors"  # подкидывают только соседи отбивающегося


@dataclass
class Settings:
    deck_size: int = 36
    max_players: int = 4
    transfer: bool = False              # переводной дурак
    throw_in: str = THROW_IN_ALL        # кто может подкидывать
    max_attack_cards: int = 6           # потолок карт в одном кону

    def validate(self) -> None:
        if self.deck_size not in DECK_SIZES:
            raise GameError("Размер колоды должен быть одним из: 24, 32, 36, 52")
        if not 2 <= self.max_players <= 6:
            raise GameError("Игроков может быть от 2 до 6")
        if self.throw_in not in (THROW_IN_ALL, THROW_IN_NEIGHBORS):
            raise GameError("Неизвестное правило подкидывания")
        if not 1 <= self.max_attack_cards <= 6:
            raise GameError("Карт в кону может быть от 1 до 6")
        if self.max_players * HAND_SIZE > self.deck_size:
            raise GameError(
                "В колоде из %d карт не раздать по 6 карт %d игрокам "
                "(максимум %d)" % (self.deck_size, self.max_players,
                                   self.deck_size // HAND_SIZE))

    def to_dict(self) -> dict:
        return {
            "deck_size": self.deck_size,
            "max_players": self.max_players,
            "transfer": self.transfer,
            "throw_in": self.throw_in,
            "max_attack_cards": self.max_attack_cards,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        raw = data or {}
        try:
            s = cls(
                deck_size=int(raw.get("deck_size", 36)),
                max_players=int(raw.get("max_players", 4)),
                transfer=bool(raw.get("transfer", False)),
                throw_in=str(raw.get("throw_in", THROW_IN_ALL)),
                max_attack_cards=int(raw.get("max_attack_cards", 6)),
            )
        except (TypeError, ValueError):
            raise GameError("Некорректные настройки комнаты")
        s.validate()
        return s


# --- игра ------------------------------------------------------------------

@dataclass
class Seat:
    pid: str
    name: str
    hand: list = field(default_factory=list)
    out: bool = False  # вышел из игры (карт нет и колода пуста)


class Game:
    """Одна партия. Места (seats) нумеруются по кругу, ход идёт по возрастанию."""

    def __init__(self, players, settings: Settings, seed=None):
        # players: [(pid, name), ...]
        settings.validate()
        if not 2 <= len(players) <= 6:
            raise GameError("Для игры нужно от 2 до 6 игроков")
        if len(players) * HAND_SIZE > settings.deck_size:
            raise GameError("Колоды не хватит на такое число игроков")

        self.settings = settings
        self.rng = random.Random(seed)
        self.seats = [Seat(pid=p, name=n) for p, n in players]
        self.log = []
        self.finished = False
        self.durak = None      # pid проигравшего
        self.draw = False      # ничья
        self.finish_order = []  # pid в порядке выхода из игры

        deck = build_deck(settings.deck_size)
        self.rng.shuffle(deck)
        self.deck = deck                    # deck[0] — нижняя карта (козырь)
        self.trump_card = deck[0]
        self.trump = self.trump_card.suit

        for _ in range(HAND_SIZE):
            for seat in self.seats:
                seat.hand.append(self.deck.pop())
        for seat in self.seats:
            seat.hand.sort(key=self._sort_key)

        self.discard_count = 0
        self.table = []        # [{"a": Card, "d": Card | None}]
        self.passed = set()    # pid игроков, нажавших «пас»/«бито»
        self.taking = False    # защищающийся объявил «беру»
        self.bout_limit = settings.max_attack_cards

        self.attacker = self._choose_first_attacker()
        self.defender = self._next_active(self.attacker)
        self._start_bout()
        self._log("Козырь — %s. Первым ходит %s" %
                  (self.trump_card.label, self.seats[self.attacker].name))

    # -- служебное ----------------------------------------------------------

    def _sort_key(self, c: Card):
        return (c.suit == self.trump, c.suit, c.rank)

    def _log(self, text: str) -> None:
        self.log.append(text)
        del self.log[:-60]

    def _choose_first_attacker(self) -> int:
        best, best_key = None, None
        for i, seat in enumerate(self.seats):
            trumps = [c for c in seat.hand if c.suit == self.trump]
            if not trumps:
                continue
            key = min(c.rank for c in trumps)
            if best_key is None or key < best_key:
                best, best_key = i, key
        if best is None:
            best = self.rng.randrange(len(self.seats))
        return best

    def _active_indexes(self) -> list:
        return [i for i, s in enumerate(self.seats) if not s.out]

    def _next_active(self, idx: int) -> int:
        n = len(self.seats)
        for step in range(1, n + 1):
            j = (idx + step) % n
            if not self.seats[j].out:
                return j
        return idx

    def seat_of(self, pid: str):
        for i, s in enumerate(self.seats):
            if s.pid == pid:
                return i
        return None

    def _start_bout(self) -> None:
        self.table = []
        self.taking = False
        self.passed = set()
        self.bout_limit = min(self.settings.max_attack_cards,
                              len(self.seats[self.defender].hand))
        self._auto_pass()

    # -- кто что может ------------------------------------------------------

    def _throwers(self) -> list:
        """Места игроков, которые сейчас могут подкидывать (кроме защищающегося)."""
        active = [i for i in self._active_indexes() if i != self.defender]
        if self.settings.throw_in == THROW_IN_ALL or len(active) <= 1:
            return active
        # только соседи отбивающегося: атакующий и следующий за защитником
        neighbors = {self.attacker, self._next_active(self.defender)}
        neighbors.discard(self.defender)
        return [i for i in active if i in neighbors]

    def _table_ranks(self) -> set:
        ranks = set()
        for pair in self.table:
            ranks.add(pair["a"].rank)
            if pair["d"] is not None:
                ranks.add(pair["d"].rank)
        return ranks

    def _undefended(self) -> list:
        return [i for i, p in enumerate(self.table) if p["d"] is None]

    def _attack_slots_left(self) -> int:
        """Сколько карт ещё можно положить в атаку."""
        left = self.bout_limit - len(self.table)
        if not self.taking:
            # нельзя класть больше, чем защищающийся способен отбить
            left = min(left, len(self.seats[self.defender].hand) - len(self._undefended()))
        return max(0, left)

    def attack_options(self, idx: int) -> list:
        """Карты, которыми игрок idx может ходить/подкидывать."""
        if self.finished or idx == self.defender or self.seats[idx].out:
            return []
        hand = self.seats[idx].hand
        if not self.table:
            return list(hand) if idx == self.attacker else []
        if idx not in self._throwers() or idx in self._passed_seats():
            return []
        if self._attack_slots_left() <= 0:
            return []
        ranks = self._table_ranks()
        return [c for c in hand if c.rank in ranks]

    def defence_options(self, idx: int) -> dict:
        """{card_id: [индексы карт на столе, которые она бьёт]}"""
        if self.finished or idx != self.defender or self.taking or not self.table:
            return {}
        res = {}
        for c in self.seats[idx].hand:
            targets = [i for i in self._undefended()
                       if beats(c, self.table[i]["a"], self.trump)]
            if targets:
                res[c.id] = targets
        return res

    def transfer_options(self, idx: int) -> list:
        """Карты, которыми можно перевести."""
        if (self.finished or not self.settings.transfer or idx != self.defender
                or self.taking or not self.table):
            return []
        if any(p["d"] is not None for p in self.table):
            return []
        rank = self.table[0]["a"].rank
        if any(p["a"].rank != rank for p in self.table):
            return []
        nxt = self._next_active(self.defender)
        if nxt == self.defender:
            return []
        # переводить можно, только если новому защищающемуся есть чем отбиваться
        if len(self.seats[nxt].hand) < len(self.table) + 1:
            return []
        if len(self.table) + 1 > self.settings.max_attack_cards:
            return []
        return [c for c in self.seats[idx].hand if c.rank == rank]

    def _passed_seats(self) -> set:
        return {i for i, s in enumerate(self.seats) if s.pid in self.passed}

    def _auto_pass(self) -> None:
        """Игроков, которым нечем подкинуть, пасуем автоматически."""
        if self.finished or not self.table:
            return
        for i in self._throwers():
            if self.seats[i].pid in self.passed:
                continue
            if not self.attack_options(i):
                self.passed.add(self.seats[i].pid)
        self._maybe_end_bout()

    def can_pass(self, idx: int) -> bool:
        if self.finished or not self.table or idx == self.defender:
            return False
        if idx not in self._throwers() or self.seats[idx].pid in self.passed:
            return False
        # пока есть неотбитые карты и защищающийся не сказал «беру» — ждём его
        return self.taking or not self._undefended()

    def can_take(self, idx: int) -> bool:
        return (not self.finished and idx == self.defender and bool(self.table)
                and not self.taking)

    # -- ходы ---------------------------------------------------------------

    def apply(self, pid: str, action: str, card_id=None, target=None) -> None:
        if self.finished:
            raise GameError("Партия уже закончена")
        idx = self.seat_of(pid)
        if idx is None:
            raise GameError("Вы не участвуете в этой партии")
        if self.seats[idx].out:
            raise GameError("Вы уже вышли из игры")

        if action == "attack":
            self._do_attack(idx, card_id)
        elif action == "defend":
            self._do_defend(idx, card_id, target)
        elif action == "transfer":
            self._do_transfer(idx, card_id)
        elif action == "take":
            self._do_take(idx)
        elif action == "pass":
            self._do_pass(idx)
        else:
            raise GameError("Неизвестное действие: %s" % action)

    def _take_card(self, idx: int, card_id: str) -> Card:
        card = card_from_id(card_id)
        hand = self.seats[idx].hand
        if card not in hand:
            raise GameError("Такой карты у вас нет")
        return card

    def _do_attack(self, idx: int, card_id: str) -> None:
        card = self._take_card(idx, card_id)
        if card not in self.attack_options(idx):
            if idx == self.defender:
                raise GameError("Вы отбиваетесь, ходить нельзя")
            if not self.table and idx != self.attacker:
                raise GameError("Сейчас не ваш ход")
            if self._attack_slots_left() <= 0:
                raise GameError("Больше подкидывать нельзя")
            raise GameError("Такой картой подкинуть нельзя")
        self.seats[idx].hand.remove(card)
        self.table.append({"a": card, "d": None})
        self._log("%s ходит %s" % (self.seats[idx].name, card.label))
        # новая карта — все снова могут подкидывать
        self.passed = set()
        self._auto_pass()

    def _do_defend(self, idx: int, card_id: str, target) -> None:
        if idx != self.defender:
            raise GameError("Отбивается другой игрок")
        if self.taking:
            raise GameError("Вы уже забрали карты")
        card = self._take_card(idx, card_id)
        options = self.defence_options(idx).get(card.id, [])
        if not options:
            raise GameError("Этой картой отбиться нельзя")
        if target is None:
            slot = options[0]
        else:
            try:
                slot = int(target)
            except (TypeError, ValueError):
                raise GameError("Некорректная карта для отбоя")
            if slot not in options:
                raise GameError("Этой картой ту карту не побить")
        self.seats[idx].hand.remove(card)
        self.table[slot]["d"] = card
        self._log("%s кроет %s картой %s" %
                  (self.seats[idx].name, self.table[slot]["a"].label, card.label))
        self.passed = set()
        self._auto_pass()

    def _do_transfer(self, idx: int, card_id: str) -> None:
        card = self._take_card(idx, card_id)
        if card not in self.transfer_options(idx):
            if not self.settings.transfer:
                raise GameError("В этой комнате перевод запрещён")
            raise GameError("Перевести этой картой нельзя")
        self.seats[idx].hand.remove(card)
        self.table.append({"a": card, "d": None})
        new_defender = self._next_active(self.defender)
        self._log("%s переводит на %s: %s" %
                  (self.seats[idx].name, self.seats[new_defender].name, card.label))
        self.attacker = self.defender
        self.defender = new_defender
        self.passed = set()
        self.bout_limit = min(self.settings.max_attack_cards,
                              len(self.seats[self.defender].hand))
        self._auto_pass()

    def _do_take(self, idx: int) -> None:
        if not self.can_take(idx):
            if idx != self.defender:
                raise GameError("Забирает только защищающийся")
            raise GameError("Забирать нечего")
        self.taking = True
        self._log("%s забирает" % self.seats[idx].name)
        self.passed = set()
        self._auto_pass()

    def _do_pass(self, idx: int) -> None:
        if not self.can_pass(idx):
            if idx == self.defender:
                raise GameError("Защищающийся не пасует")
            if self._undefended() and not self.taking:
                raise GameError("Сначала дождитесь ответа защищающегося")
            raise GameError("Сейчас нельзя пасовать")
        self.passed.add(self.seats[idx].pid)
        self._maybe_end_bout()

    # -- завершение кона ----------------------------------------------------

    def _maybe_end_bout(self) -> None:
        if self.finished or not self.table:
            return
        throwers = self._throwers()
        if any(self.seats[i].pid not in self.passed for i in throwers):
            return
        if not self.taking and self._undefended():
            return  # ждём защищающегося
        if self.taking:
            self._end_bout_taken()
        else:
            self._end_bout_beaten()

    def _end_bout_beaten(self) -> None:
        cards = 0
        for pair in self.table:
            cards += 1 + (1 if pair["d"] else 0)
        self.discard_count += cards
        self._log("Бито")
        defender = self.defender
        self._refill(start=self.attacker, defender=defender)
        self._advance(next_attacker=defender)

    def _end_bout_taken(self) -> None:
        taken = []
        for pair in self.table:
            taken.append(pair["a"])
            if pair["d"]:
                taken.append(pair["d"])
        seat = self.seats[self.defender]
        seat.hand.extend(taken)
        seat.hand.sort(key=self._sort_key)
        self._log("%s забирает %s" %
                  (seat.name, plural(len(taken), "карту", "карты", "карт")))
        defender = self.defender
        self._refill(start=self.attacker, defender=defender)
        self._advance(next_attacker=self._next_active(defender))

    def _refill(self, start: int, defender: int) -> None:
        """Добор до 6: сначала атаковавший, потом по кругу, защищавшийся последним."""
        n = len(self.seats)
        order = []
        for step in range(n):
            i = (start + step) % n
            if i == defender or self.seats[i].out:
                continue
            order.append(i)
        if not self.seats[defender].out:
            order.append(defender)
        for i in order:
            seat = self.seats[i]
            while len(seat.hand) < HAND_SIZE and self.deck:
                seat.hand.append(self.deck.pop())
            seat.hand.sort(key=self._sort_key)

    def _advance(self, next_attacker: int) -> None:
        # кто остался без карт при пустой колоде — вышел из игры
        for seat in self.seats:
            if not seat.out and not seat.hand and not self.deck:
                seat.out = True
                self.finish_order.append(seat.pid)
                self._log("%s вышел из игры" % seat.name)

        active = self._active_indexes()
        if len(active) <= 1:
            self.finished = True
            if active:
                self.durak = self.seats[active[0]].pid
                self._log("Дурак — %s" % self.seats[active[0]].name)
            else:
                self.draw = True
                self._log("Ничья")
            return

        if self.seats[next_attacker].out:
            next_attacker = self._next_active(next_attacker)
        self.attacker = next_attacker
        self.defender = self._next_active(self.attacker)
        self._start_bout()

    # -- представление для клиента -----------------------------------------

    def view_for(self, pid, online=None) -> dict:
        online = online or {}
        me = self.seat_of(pid)
        players = []
        for i, seat in enumerate(self.seats):
            role = None
            if not self.finished:
                if i == self.attacker:
                    role = "attacker"
                elif i == self.defender:
                    role = "defender"
            players.append({
                "seat": i,
                "id": seat.pid,
                "name": seat.name,
                "cards": len(seat.hand),
                "out": seat.out,
                "role": role,
                "passed": seat.pid in self.passed,
                "online": bool(online.get(seat.pid, True)),
            })

        actions = {
            "attack": [], "defend": {}, "transfer": [],
            "can_take": False, "can_pass": False,
        }
        if me is not None and not self.finished:
            actions["attack"] = [c.id for c in self.attack_options(me)]
            actions["defend"] = self.defence_options(me)
            actions["transfer"] = [c.id for c in self.transfer_options(me)]
            actions["can_take"] = self.can_take(me)
            actions["can_pass"] = self.can_pass(me)

        return {
            "trump": self.trump,
            "trump_card": self.trump_card.id if self.deck else None,
            "deck": len(self.deck),
            "discard": self.discard_count,
            "table": [{"a": p["a"].id, "d": p["d"].id if p["d"] else None}
                      for p in self.table],
            "attacker": self.attacker,
            "defender": self.defender,
            "taking": self.taking,
            "players": players,
            "seat": me,
            "hand": [c.id for c in self.seats[me].hand] if me is not None else [],
            "actions": actions,
            "finished": self.finished,
            "durak": self.durak,
            "draw": self.draw,
            "log": self.log[-12:],
        }
