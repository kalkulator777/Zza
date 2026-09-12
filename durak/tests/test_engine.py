# -*- coding: utf-8 -*-
"""Тесты правил. Запуск: python3 -m unittest discover -s tests -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (  # noqa: E402
    Card, Game, GameError, Settings, RANKS, THROW_IN_NEIGHBORS,
    beats, build_deck, card_from_id,
)


def C(cid):
    return card_from_id(cid)


def setup_game(hands, trump="H", deck=None, **kw):
    """Собирает партию с заранее заданными руками.

    hands: список списков id карт (по игроку). deck: остаток колоды снизу вверх.
    """
    names = [("p%d" % i, "Игрок%d" % i) for i in range(len(hands))]
    settings = Settings(max_players=len(hands), **kw)
    g = Game(names, settings, seed=1)
    g.trump = trump
    g.trump_card = Card(RANKS.index("6"), trump)
    g.deck = [C(x) for x in (deck or [])]
    if g.deck:
        g.trump_card = g.deck[0]
    for seat, cards in zip(g.seats, hands):
        seat.hand = [C(x) for x in cards]
        seat.hand.sort(key=g._sort_key)
    g.attacker = 0
    g.defender = 1
    g._start_bout()
    g.log = []
    return g


class TestCards(unittest.TestCase):
    def test_deck_sizes(self):
        for size in (24, 32, 36, 52):
            deck = build_deck(size)
            self.assertEqual(len(deck), size)
            self.assertEqual(len(set(deck)), size)
        self.assertEqual(min(c.rank for c in build_deck(24)), RANKS.index("9"))
        self.assertEqual(min(c.rank for c in build_deck(32)), RANKS.index("7"))
        self.assertEqual(min(c.rank for c in build_deck(36)), RANKS.index("6"))
        self.assertEqual(min(c.rank for c in build_deck(52)), RANKS.index("2"))

    def test_beats(self):
        self.assertTrue(beats(C("KS"), C("QS"), "H"))     # старше в масти
        self.assertFalse(beats(C("QS"), C("KS"), "H"))
        self.assertTrue(beats(C("6H"), C("AS"), "H"))     # козырь бьёт некозырь
        self.assertFalse(beats(C("AS"), C("6H"), "H"))
        self.assertTrue(beats(C("KH"), C("QH"), "H"))     # козырь козырем
        self.assertFalse(beats(C("KS"), C("QD"), "H"))    # разные масти

    def test_bad_card(self):
        self.assertRaises(GameError, card_from_id, "1X")


class TestSettings(unittest.TestCase):
    def test_too_many_players_for_deck(self):
        s = Settings(deck_size=24, max_players=6)
        self.assertRaises(GameError, s.validate)
        Settings(deck_size=24, max_players=4).validate()

    def test_unknown_deck(self):
        self.assertRaises(GameError, Settings(deck_size=30).validate)


class TestDealing(unittest.TestCase):
    def test_initial_deal(self):
        g = Game([("a", "A"), ("b", "B"), ("c", "C")], Settings(max_players=3), seed=7)
        self.assertEqual(len(g.deck), 36 - 18)
        for seat in g.seats:
            self.assertEqual(len(seat.hand), 6)
        self.assertEqual(g.trump, g.trump_card.suit)
        self.assertEqual(g.defender, (g.attacker + 1) % 3)

    def test_first_attacker_has_lowest_trump(self):
        g = Game([("a", "A"), ("b", "B")], Settings(max_players=2), seed=3)
        trumps = [min([c.rank for c in s.hand if c.suit == g.trump], default=None)
                  for s in g.seats]
        have = [t for t in trumps if t is not None]
        if have:
            self.assertEqual(trumps[g.attacker], min(have))


class TestBasicBout(unittest.TestCase):
    def test_attack_defend_beaten(self):
        g = setup_game([["9S", "9D", "AC"], ["10S", "KD", "7H"]], trump="H")
        g.apply("p0", "attack", "9S")
        self.assertEqual(len(g.table), 1)
        g.apply("p1", "defend", "10S", 0)
        self.assertEqual(g.table[0]["d"].id, "10S")
        # у p0 остались 9D (подходит по рангу) — пасуем вручную
        self.assertIn("9D", [c.id for c in g.attack_options(0)])
        g.apply("p0", "pass")
        self.assertEqual(g.table, [])
        self.assertEqual(g.discard_count, 2)
        self.assertEqual(g.attacker, 1)  # отбившийся ходит следующим

    def test_take(self):
        g = setup_game([["9S", "9D", "9C"], ["7C", "8C", "KH", "AH"]], trump="H")
        g.apply("p0", "attack", "9S")
        g.apply("p1", "take")
        self.assertTrue(g.taking)
        g.apply("p0", "attack", "9D")   # подкидываем при взятии
        g.apply("p0", "pass")
        self.assertEqual(g.table, [])
        self.assertEqual(len(g.seats[1].hand), 6)  # забрал две карты
        self.assertEqual(g.discard_count, 0)
        self.assertEqual(g.attacker, 0)  # взявший пропускает ход

    def test_auto_pass_when_nothing_to_throw(self):
        g = setup_game([["9S", "AC"], ["7C", "8C", "KH"]], trump="H")
        g.apply("p0", "attack", "9S")
        g.apply("p1", "take")
        # у p0 нет карт ранга 9 — пас ставится автоматически, кон закрыт
        self.assertEqual(g.table, [])
        self.assertEqual(len(g.seats[1].hand), 4)

    def test_cannot_throw_wrong_rank(self):
        g = setup_game([["9S", "KD"], ["10S", "7H"]], trump="H")
        g.apply("p0", "attack", "9S")
        g.apply("p1", "defend", "10S", 0)
        with self.assertRaises(GameError):
            g.apply("p0", "attack", "KD")

    def test_defender_cannot_attack(self):
        g = setup_game([["9S"], ["9D", "10S"]], trump="H")
        g.apply("p0", "attack", "9S")
        with self.assertRaises(GameError):
            g.apply("p1", "attack", "9D")

    def test_cannot_beat_with_weak_card(self):
        g = setup_game([["KS"], ["9S"]], trump="H")
        g.apply("p0", "attack", "KS")
        with self.assertRaises(GameError):
            g.apply("p1", "defend", "9S", 0)

    def test_attack_limited_by_defender_hand(self):
        g = setup_game([["9S", "9D", "9C"], ["10S"]], trump="H")
        g.apply("p0", "attack", "9S")
        with self.assertRaises(GameError):
            g.apply("p0", "attack", "9D")  # у защищающегося всего одна карта

    def test_max_six_cards(self):
        g = setup_game([["6S", "6D", "6C", "6H", "10D", "10C", "10H"],
                        ["10S", "JD", "QC", "KH", "AD", "AC", "8S", "9S"]],
                       trump="H")
        self.assertEqual(g.bout_limit, 6)
        pairs = [("6S", "10S"), ("6D", "JD"), ("6C", "QC"),
                 ("6H", "KH"), ("10D", "AD"), ("10C", "AC")]
        for i, (att, dfn) in enumerate(pairs):
            g.apply("p0", "attack", att)
            self.assertEqual(len(g.table), i + 1)
            g.apply("p1", "defend", dfn, i)
        # шесть карт — потолок: кон закрылся сам, седьмую подкинуть некуда
        self.assertEqual(g.table, [])
        self.assertEqual(g.discard_count, 12)
        self.assertIn("10H", [c.id for c in g.seats[0].hand])

    def test_custom_max_attack_cards(self):
        g = setup_game([["6S", "6D", "6C"], ["AS", "AD", "AC", "AH", "KS", "KD"]],
                       trump="H", max_attack_cards=2)
        self.assertEqual(g.bout_limit, 2)
        g.apply("p0", "attack", "6S")
        g.apply("p1", "take")
        g.apply("p0", "attack", "6D")
        self.assertEqual(g.table, [])  # предел в 2 карты — кон закрыт
        self.assertEqual(len(g.seats[1].hand), 8)


class TestThrowIn(unittest.TestCase):
    def test_all_can_throw(self):
        g = setup_game([["9S"], ["10S", "KH"], ["9D"], ["9C"]], trump="H")
        g.apply("p0", "attack", "9S")
        self.assertIn("9D", [c.id for c in g.attack_options(2)])
        self.assertIn("9C", [c.id for c in g.attack_options(3)])
        g.apply("p2", "attack", "9D")
        self.assertEqual(len(g.table), 2)

    def test_neighbors_only(self):
        g = setup_game([["9S"], ["10S", "KH"], ["9D"], ["9C"]], trump="H",
                       throw_in=THROW_IN_NEIGHBORS)
        g.apply("p0", "attack", "9S")
        # защищается p1; соседи — p0 (атакующий) и p2 (следующий за защитником)
        self.assertIn("9D", [c.id for c in g.attack_options(2)])
        self.assertEqual(g.attack_options(3), [])
        with self.assertRaises(GameError):
            g.apply("p3", "attack", "9C")


class TestTransfer(unittest.TestCase):
    def test_transfer_forbidden_by_default(self):
        g = setup_game([["9S"], ["9D", "10S"], ["AC", "AD"]], trump="H")
        g.apply("p0", "attack", "9S")
        self.assertEqual(g.transfer_options(1), [])
        with self.assertRaises(GameError):
            g.apply("p1", "transfer", "9D")

    def test_transfer_moves_defence(self):
        g = setup_game([["9S"], ["9D", "10S"], ["AC", "AD"]], trump="H", transfer=True)
        g.apply("p0", "attack", "9S")
        self.assertEqual([c.id for c in g.transfer_options(1)], ["9D"])
        g.apply("p1", "transfer", "9D")
        self.assertEqual(g.attacker, 1)
        self.assertEqual(g.defender, 2)
        self.assertEqual(len(g.table), 2)

    def test_cannot_transfer_after_defending(self):
        g = setup_game([["9S", "9C"], ["9D", "10S"], ["AC", "AD"]],
                       trump="H", transfer=True)
        g.apply("p0", "attack", "9S")
        g.apply("p1", "defend", "10S", 0)
        self.assertEqual(g.transfer_options(1), [])

    def test_cannot_transfer_if_target_has_few_cards(self):
        g = setup_game([["9S", "9C"], ["9D", "10S"], ["AC"]],
                       trump="H", transfer=True)
        g.apply("p0", "attack", "9S")
        g.apply("p0", "attack", "9C")
        # на столе 2 карты, перевод сделает 3 — у p2 всего одна карта
        self.assertEqual(g.transfer_options(1), [])


class TestEndGame(unittest.TestCase):
    def test_last_with_cards_is_durak(self):
        g = setup_game([["9S"], ["AH", "KH"]], trump="H")
        g.apply("p0", "attack", "9S")
        g.apply("p1", "defend", "AH", 0)
        self.assertTrue(g.finished)
        self.assertEqual(g.durak, "p1")

    def test_draw(self):
        g = setup_game([["9S"], ["AH"]], trump="H")
        g.apply("p0", "attack", "9S")
        g.apply("p1", "defend", "AH", 0)
        self.assertTrue(g.finished)
        self.assertTrue(g.draw)
        self.assertIsNone(g.durak)

    def test_refill_from_deck(self):
        deck = ["6H", "7C", "8C", "9C", "10C", "JC",
                "QC", "KC", "7D", "8D", "9D", "10D"]
        g = setup_game([["9S"], ["AH"]], trump="H", deck=deck)
        g.apply("p0", "attack", "9S")
        g.apply("p1", "defend", "AH", 0)
        self.assertFalse(g.finished)
        self.assertEqual(len(g.seats[0].hand), 6)
        self.assertEqual(len(g.seats[1].hand), 6)
        self.assertEqual(len(g.deck), 0)

    def test_refill_order_attacker_first(self):
        # в колоде 3 карты: сначала добирает атаковавший, отбивавшийся последним
        g = setup_game([["9S"], ["AH"]], trump="H", deck=["6H", "7C", "8C"])
        g.apply("p0", "attack", "9S")
        g.apply("p1", "defend", "AH", 0)
        self.assertEqual(len(g.seats[0].hand), 3)
        self.assertEqual(len(g.seats[1].hand), 0)
        self.assertTrue(g.finished)
        self.assertEqual(g.durak, "p0")

    def test_player_leaves_game_when_out_of_cards(self):
        g = setup_game([["9S"], ["10S"], ["AH", "KH"]], trump="H")
        g.apply("p0", "attack", "9S")
        g.apply("p1", "defend", "10S", 0)
        self.assertTrue(g.finished)
        self.assertEqual(g.durak, "p2")


class TestView(unittest.TestCase):
    def test_view_hides_other_hands(self):
        g = setup_game([["9S", "AC"], ["10S", "7H"]], trump="H")
        v = g.view_for("p0")
        self.assertEqual(sorted(v["hand"]), ["9S", "AC"])
        self.assertEqual(v["players"][1]["cards"], 2)
        self.assertNotIn("hand", v["players"][1])
        self.assertEqual(sorted(v["actions"]["attack"]), ["9S", "AC"])
        self.assertEqual(g.view_for("p1")["actions"]["attack"], [])

    def test_view_for_spectator(self):
        g = setup_game([["9S"], ["10S"]], trump="H")
        v = g.view_for("nobody")
        self.assertEqual(v["hand"], [])
        self.assertIsNone(v["seat"])


if __name__ == "__main__":
    unittest.main()
