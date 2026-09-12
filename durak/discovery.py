# -*- coding: utf-8 -*-
"""Поиск других запущенных «Дураков» в локальной сети.

Каждая копия программы раз в пару секунд кричит в широковещательный UDP:
«я тут, вот мой адрес и мои комнаты», и слушает такие же крики соседей.
Благодаря этому не нужно заранее договариваться, кто сервер: запускают все
одинаково, а в меню видно, у кого уже собирается игра.

Работает в отдельном потоке, чтобы не мешать tornado. Наружу отдаёт снимок
списка соседей (snapshot) и принимает снимок своих комнат (set_rooms).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import struct
import threading
import time
import uuid

log = logging.getLogger("durak.discovery")

DEFAULT_UDP_PORT = 8889
ANNOUNCE_EVERY = 2.0    # как часто объявляем о себе
PEER_TTL = 7.0          # через сколько секунд молчащий сосед пропадает из списка
MAX_ROOMS_IN_PACKET = 6
MAX_PACKET = 4096


def local_ipv4() -> list:
    """IPv4-адреса этой машины (без петлевого, если есть другие)."""
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except socket.gaierror:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    real = sorted(a for a in addrs if not a.startswith("127."))
    return real or sorted(addrs)


SIOCGIFBRDADDR = 0x8919  # спросить у ядра широковещательный адрес интерфейса


def interface_broadcasts() -> list:
    """Широковещательные адреса всех интерфейсов — как их видит само ядро.

    Не угадываем маску: при /16, /22 и прочих нестандартных сетях угадывание
    даёт неверный адрес. Работает на Linux; на других системах вернёт пусто.
    """
    if not os.path.exists("/proc/net/dev"):
        return []
    result = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with open("/proc/net/dev", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[2:]
        for line in lines:
            name = line.split(":")[0].strip()
            if not name:
                continue
            try:
                raw = fcntl.ioctl(sock.fileno(), SIOCGIFBRDADDR,
                                  struct.pack("256s", name[:15].encode("utf-8")))
                addr = socket.inet_ntoa(raw[20:24])
            except (OSError, struct.error):
                continue
            if addr and addr != "0.0.0.0" and addr not in result:
                result.append(addr)
    except OSError:
        return result
    finally:
        sock.close()
    return result


def broadcast_targets() -> list:
    """Куда слать объявления.

    Общий широковещательный адрес плюс адреса каждого интерфейса (чтобы
    объявление ушло и в ту сеть, через которую не идёт маршрут по умолчанию).
    Если ядро спросить не удалось — пробуем /24 как последнюю догадку.
    """
    targets = ["255.255.255.255"]
    found = interface_broadcasts()
    for addr in found:
        if addr not in targets:
            targets.append(addr)
    if not found:
        for ip in local_ipv4():
            parts = ip.split(".")
            if len(parts) == 4:
                guess = ".".join(parts[:3]) + ".255"
                if guess not in targets:
                    targets.append(guess)
    return targets


class Discovery(threading.Thread):
    def __init__(self, http_port, udp_port=DEFAULT_UDP_PORT, name=None,
                 targets=None):
        super().__init__(daemon=True)
        self.http_port = int(http_port)
        self.udp_port = int(udp_port)
        self.instance = uuid.uuid4().hex[:12]
        self.host_name = name or socket.gethostname() or "компьютер"
        self.targets = targets or broadcast_targets()
        self.enabled = True
        self.error = None
        self._rooms = []          # снимок своих комнат, обновляет ioloop
        self._peers = {}          # instance -> запись о соседе
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # -- наружу -------------------------------------------------------------

    def set_rooms(self, rooms) -> None:
        """Принять готовый снимок комнат (вызывается из потока tornado)."""
        self._rooms = list(rooms)[:MAX_ROOMS_IN_PACKET]

    def snapshot(self) -> list:
        """Соседи, которых слышно прямо сейчас."""
        now = time.time()
        with self._lock:
            peers = [dict(p) for p in self._peers.values()
                     if now - p["seen"] <= PEER_TTL]
        for p in peers:
            p.pop("seen", None)
        peers.sort(key=lambda p: (p["host"], p["ip"]))
        return peers

    def signature(self) -> str:
        """Короткая подпись состояния — чтобы понять, изменилось ли что-то."""
        return json.dumps(self.snapshot(), sort_keys=True, ensure_ascii=False)

    def stop(self) -> None:
        self._stop.set()

    # -- внутреннее ---------------------------------------------------------

    def _packet(self) -> bytes:
        return json.dumps({
            "app": "durak",
            "v": 1,
            "id": self.instance,
            "port": self.http_port,
            "host": self.host_name,
            "rooms": self._rooms,
        }, ensure_ascii=False).encode("utf-8")

    def _announce(self, sock) -> None:
        data = self._packet()
        if len(data) > MAX_PACKET:
            data = json.dumps({
                "app": "durak", "v": 1, "id": self.instance,
                "port": self.http_port, "host": self.host_name, "rooms": [],
            }, ensure_ascii=False).encode("utf-8")
        for target in self.targets:
            try:
                sock.sendto(data, (target, self.udp_port))
            except OSError as exc:
                log.debug("не отправил объявление на %s: %s", target, exc)

    def _accept(self, raw, addr) -> None:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict) or msg.get("app") != "durak":
            return
        pid = msg.get("id")
        if not pid or pid == self.instance:
            return  # свой же крик
        rooms = msg.get("rooms")
        if not isinstance(rooms, list):
            rooms = []
        entry = {
            "id": str(pid)[:32],
            "ip": addr[0],
            "port": int(msg.get("port") or self.http_port),
            "host": str(msg.get("host") or addr[0])[:40],
            "rooms": rooms[:MAX_ROOMS_IN_PACKET],
            "seen": time.time(),
        }
        with self._lock:
            self._peers[entry["id"]] = entry

    def _expire(self) -> None:
        now = time.time()
        with self._lock:
            for pid, peer in list(self._peers.items()):
                if now - peer["seen"] > PEER_TTL:
                    del self._peers[pid]

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("", self.udp_port))
        except OSError as exc:
            self.enabled = False
            self.error = str(exc)
            log.warning("Поиск игр в сети выключен: %s", exc)
            sock.close()
            return
        sock.settimeout(0.5)

        last = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last >= ANNOUNCE_EVERY:
                self._announce(sock)
                last = now
            try:
                raw, addr = sock.recvfrom(MAX_PACKET)
            except socket.timeout:
                pass
            except OSError as exc:
                log.debug("ошибка приёма: %s", exc)
                time.sleep(0.5)
            else:
                self._accept(raw, addr)
            self._expire()
        sock.close()
