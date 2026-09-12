# -*- coding: utf-8 -*-
"""Тесты поиска соседей по сети (без настоящих сокетов)."""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discovery import (  # noqa: E402
    Discovery, broadcast_targets, interface_broadcasts, local_ipv4,
)


def packet(instance, host="ПК", port=8888, rooms=None):
    return json.dumps({
        "app": "durak", "v": 1, "id": instance, "host": host,
        "port": port, "rooms": rooms if rooms is not None else [],
    }).encode("utf-8")


class TestTargets(unittest.TestCase):
    def test_broadcast_targets(self):
        targets = broadcast_targets()
        self.assertIn("255.255.255.255", targets)
        self.assertEqual(len(targets), len(set(targets)))
        for t in targets:
            parts = t.split(".")
            self.assertEqual(len(parts), 4, t)
            self.assertTrue(all(p.isdigit() and int(p) < 256 for p in parts), t)

    def test_interface_broadcasts_are_real(self):
        # адреса берём у ядра, а не угадываем по маске /24
        for addr in interface_broadcasts():
            self.assertNotEqual(addr, "0.0.0.0")
            self.assertEqual(len(addr.split(".")), 4)
        self.assertTrue(all(a in broadcast_targets() for a in interface_broadcasts()))

    def test_custom_targets_are_respected(self):
        d = Discovery(http_port=8888, targets=["10.7.0.255", "172.16.255.255"])
        self.assertEqual(d.targets, ["10.7.0.255", "172.16.255.255"])

    def test_local_ipv4(self):
        self.assertTrue(all(a.count(".") == 3 for a in local_ipv4()))


class TestPackets(unittest.TestCase):
    def setUp(self):
        self.d = Discovery(http_port=8888, name="Мой ПК")

    def test_own_packet_has_rooms(self):
        self.d.set_rooms([{"id": "A1B2", "title": "Обед", "players": 2,
                           "max": 4, "in_game": False}])
        msg = json.loads(self.d._packet().decode("utf-8"))
        self.assertEqual(msg["app"], "durak")
        self.assertEqual(msg["host"], "Мой ПК")
        self.assertEqual(msg["rooms"][0]["id"], "A1B2")

    def test_packet_keeps_only_first_rooms(self):
        self.d.set_rooms([{"id": str(i)} for i in range(20)])
        msg = json.loads(self.d._packet().decode("utf-8"))
        self.assertLessEqual(len(msg["rooms"]), 6)

    def test_accepts_neighbour(self):
        rooms = [{"id": "Z9Z9", "title": "Соседи", "players": 1, "max": 2,
                  "in_game": False}]
        self.d._accept(packet("other", host="ПК-Пети", rooms=rooms),
                       ("192.168.1.7", 8889))
        peers = self.d.snapshot()
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["ip"], "192.168.1.7")
        self.assertEqual(peers[0]["host"], "ПК-Пети")
        self.assertEqual(peers[0]["rooms"][0]["id"], "Z9Z9")
        self.assertNotIn("seen", peers[0])

    def test_ignores_own_broadcast(self):
        self.d._accept(packet(self.d.instance), ("192.168.1.5", 8889))
        self.assertEqual(self.d.snapshot(), [])

    def test_ignores_garbage(self):
        for raw in (b"", b"{", b"\xff\xfe", json.dumps({"app": "other"}).encode(),
                    json.dumps(["list"]).encode()):
            self.d._accept(raw, ("192.168.1.9", 8889))
        self.assertEqual(self.d.snapshot(), [])

    def test_peer_updates_in_place(self):
        self.d._accept(packet("other", rooms=[]), ("192.168.1.7", 8889))
        self.d._accept(packet("other", rooms=[{"id": "NEW1"}]), ("192.168.1.7", 8889))
        peers = self.d.snapshot()
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["rooms"][0]["id"], "NEW1")

    def test_silent_peer_disappears(self):
        self.d._accept(packet("other"), ("192.168.1.7", 8889))
        self.assertEqual(len(self.d.snapshot()), 1)
        self.d._peers["other"]["seen"] = time.time() - 60
        self.assertEqual(self.d.snapshot(), [])
        self.d._expire()
        self.assertEqual(self.d._peers, {})

    def test_signature_changes_with_rooms(self):
        self.d._accept(packet("other", rooms=[]), ("192.168.1.7", 8889))
        before = self.d.signature()
        self.d._accept(packet("other", rooms=[{"id": "AAAA"}]), ("192.168.1.7", 8889))
        self.assertNotEqual(before, self.d.signature())


if __name__ == "__main__":
    unittest.main()
