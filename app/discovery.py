"""Поиск лобби в локальной сети через UDP-broadcast.

Каждая копия игры слушает фиксированный UDP-порт и отвечает на запросы,
если у неё есть открытое лобби. Клиент, который жмёт «Найти лобби»,
кидает broadcast и собирает ответы.
"""

import json
import logging
import re
import socket
import subprocess
import time

from tornado.ioloop import IOLoop

DISCOVERY_PORT = 45789
MAGIC = "risovashki/1"
SCAN_TIMEOUT = 1.2

log = logging.getLogger("discovery")


def local_ip():
    """IP, который видят соседи по сети. Наружу ничего не отправляется."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        probe.close()


def broadcast_targets():
    """Широковещательные адреса всех интерфейсов + общий 255.255.255.255."""
    targets = ["255.255.255.255"]
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        for addr in re.findall(r"brd (\d+\.\d+\.\d+\.\d+)", out):
            if addr not in targets:
                targets.append(addr)
    except (OSError, subprocess.SubprocessError):
        pass
    return targets


class DiscoveryResponder:
    """Отвечает соседям, какие лобби крутятся на этой машине."""

    def __init__(self, manager, http_port):
        self.manager = manager
        self.http_port = http_port
        self.sock = None

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("", DISCOVERY_PORT))
        except OSError as exc:
            log.warning("Не удалось занять UDP %d (%s). Поиск лобби в сети работать не будет, "
                        "подключение по IP — будет.", DISCOVERY_PORT, exc)
            sock.close()
            return
        sock.setblocking(False)
        self.sock = sock
        IOLoop.current().add_handler(sock.fileno(), self._on_read, IOLoop.READ)
        log.info("Поиск лобби слушает UDP %d", DISCOVERY_PORT)

    def _on_read(self, fd, events):
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            try:
                query = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if query.get("magic") != MAGIC:
                continue
            rooms = self.manager.public_rooms()
            if not rooms:
                continue
            reply = json.dumps({
                "magic": MAGIC,
                "port": self.http_port,
                "host": socket.gethostname(),
                "rooms": rooms,
            }).encode("utf-8")
            try:
                self.sock.sendto(reply, addr)
            except OSError:
                pass


def scan(timeout=SCAN_TIMEOUT):
    """Блокирующий поиск лобби. Вызывать через run_in_executor."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.2)
    payload = json.dumps({"magic": MAGIC, "q": "rooms"}).encode("utf-8")
    for target in broadcast_targets():
        try:
            sock.sendto(payload, (target, DISCOVERY_PORT))
        except OSError:
            continue

    found = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(8192)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            reply = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if reply.get("magic") != MAGIC:
            continue
        for room in reply.get("rooms", []):
            room = dict(room)
            room["ip"] = addr[0]
            room["port"] = reply.get("port")
            room["host"] = reply.get("host", addr[0])
            found[(addr[0], room.get("code"))] = room
    sock.close()
    return sorted(found.values(), key=lambda r: (r.get("host", ""), r.get("code", "")))
