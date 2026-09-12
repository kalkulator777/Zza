# -*- coding: utf-8 -*-
"""Автопоиск игр в локальной сети через UDP-броадкаст.

Каждый запущенный экземпляр раз в 1.5 с рассылает пакет со списком своих
открытых комнат и слушает такие же пакеты от соседей. Никакой настройки.
"""

import json
import socket
import struct
import time
import uuid

from tornado.ioloop import IOLoop, PeriodicCallback

PORT = 48777
MAGIC = "zza1"
TTL = 6.0


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _, name in socket.if_nameindex():
            try:
                raw = fcntl.ioctl(s.fileno(), 0x8915,
                                  struct.pack("256s", name.encode()[:15]))
                ip = socket.inet_ntoa(raw[20:24])
                if not ip.startswith("127."):
                    ips.add(ip)
            except OSError:
                pass
        s.close()
    except Exception:
        pass
    for info in _safe_hostaddrs():
        ips.add(info)
    return sorted(i for i in ips if i and not i.startswith("127."))


def _safe_hostaddrs():
    try:
        return [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None,
                                                    socket.AF_INET)]
    except OSError:
        return []


def broadcast_addrs():
    out = {"255.255.255.255"}
    try:
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _, name in socket.if_nameindex():
            try:
                raw = fcntl.ioctl(s.fileno(), 0x8919,
                                  struct.pack("256s", name.encode()[:15]))
                addr = socket.inet_ntoa(raw[20:24])
                if addr and not addr.startswith("127."):
                    out.add(addr)
            except OSError:
                pass
        s.close()
    except Exception:
        pass
    for ip in local_ips():
        parts = ip.split(".")
        if len(parts) == 4:
            out.add(".".join(parts[:3] + ["255"]))
    return sorted(out)


class Discovery:
    def __init__(self, http_port, rooms_fn, nick="игрок"):
        self.http_port = http_port
        self.rooms_fn = rooms_fn
        self.nick = nick
        self.iid = uuid.uuid4().hex[:12]
        self.host = socket.gethostname()
        self.peers = {}
        self.sock = None
        self.pc = None
        self.enabled = False
        self.error = ""

    def start(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", PORT))
            s.setblocking(False)
            self.sock = s
            IOLoop.current().add_handler(s.fileno(), self._on_read, IOLoop.READ)
            self.pc = PeriodicCallback(self.announce, 1500)
            self.pc.start()
            self.enabled = True
            self.announce()
        except OSError as e:
            self.error = str(e)
            self.enabled = False

    def stop(self):
        if self.pc:
            self.pc.stop()
        if self.sock:
            try:
                IOLoop.current().remove_handler(self.sock.fileno())
            except Exception:
                pass
            self.sock.close()
            self.sock = None
        self.enabled = False

    def announce(self):
        if not self.sock:
            return
        payload = {
            "m": MAGIC, "iid": self.iid, "host": self.host,
            "nick": self.nick, "port": self.http_port,
            "rooms": (self.rooms_fn() or [])[:10],
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")[:1400]
        for addr in broadcast_addrs():
            try:
                self.sock.sendto(data, (addr, PORT))
            except OSError:
                pass

    def _on_read(self, fd, events):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except BlockingIOError:
                return
            except OSError:
                return
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if msg.get("m") != MAGIC or msg.get("iid") == self.iid:
                continue
            msg["ip"] = addr[0]
            msg["ts"] = time.time()
            self.peers[msg["iid"]] = msg

    def games(self):
        now = time.time()
        out = []
        for iid, p in list(self.peers.items()):
            if now - p["ts"] > TTL:
                self.peers.pop(iid, None)
                continue
            for r in p.get("rooms", []):
                g = dict(r)
                g["ip"] = p["ip"]
                g["port"] = p["port"]
                g["host"] = p.get("host", p["ip"])
                g["remote"] = True
                g["url"] = "http://%s:%s/?join=%s" % (p["ip"], p["port"], r.get("code"))
                out.append(g)
        return out

    def peer_count(self):
        now = time.time()
        return len([1 for p in self.peers.values() if now - p["ts"] <= TTL])
