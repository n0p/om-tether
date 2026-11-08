#!/usr/bin/env python3
"""Live test of the mDNS browse path with a fake `_ptp._tcp` responder.

No camera needed: a helper thread joins 224.0.0.251:5353, waits for our PTR
query and answers it with a synthetic DNS-SD response (PTR+SRV+TXT+A).
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import mdns  # noqa: E402

INSTANCE = "OM-1-LOCAL._ptp._tcp.local"
TARGET = "OM-1-LOCAL.local"


def _name(text: str) -> bytes:
    out = b""
    for label in text.split("."):
        if label:
            raw = label.encode()
            out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _rr(owner: str, rtype: int, rdata: bytes) -> bytes:
    return _name(owner) + struct.pack(">HHIH", rtype, 1, 120, len(rdata)) + rdata


def response_packet(ip: bytes = bytes([127, 0, 0, 1])) -> bytes:
    pkt = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 3)
    pkt += _rr("_ptp._tcp.local", 12, _name(INSTANCE))
    pkt += _rr(INSTANCE, 33, struct.pack(">HHH", 0, 0, 15740) + _name(TARGET))
    pkt += _rr(INSTANCE, 16, b"\x08vid=04B0")
    pkt += _rr(TARGET, 1, ip)
    return pkt


def responder(stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        sock.bind(("", mdns.MDNS_PORT))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        struct.pack("4s4s", socket.inet_aton(mdns.MDNS_ADDR), socket.inet_aton("0.0.0.0")))
    except OSError as exc:
        print("  (no se pudo abrir el respondedor: %s)" % exc)
        return
    sock.settimeout(1.0)
    deadline = time.monotonic() + 8
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(9000)
        except socket.timeout:
            continue
        if b"\x04_ptp\x04_tcp" in data and data[2:4] == b"\x00\x00":
            sock.sendto(response_packet(), addr)
            return
    sock.close()


def main() -> int:
    stop = threading.Event()
    thread = threading.Thread(target=responder, args=(stop,), daemon=True)
    thread.start()
    time.sleep(0.3)
    found = mdns.browse_ptp(3.0)
    stop.set()
    ok = any(item["instance"] == INSTANCE and item["port"] == 15740
             and item["txt"].get("vid") == "04B0" for item in found)
    print(("  ok   " if ok else "  FAIL ") + "mDNS browse -> %s" % (found or "sin respuesta"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
