#!/usr/bin/env python3
"""Captura mínima de paquetes (sin tcpdump): PTP/IP, SSDP, mDNS y ARP.

Escribe un .pcap (se abre con Wireshark) y una línea por paquete en el log.
Necesita root:  sudo python3 tools/sniff.py --iface wlan0 --out logs/cap

Se usa AF_PACKET, así que no depende de libpcap ni de tcpdump.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

PTPIP_PORT = 15740
WATCH_UDP = {1900, 5353, 15740}
WATCH_TCP = {15740, 80, 2869}

TYPE_NAMES = {
    1: "InitCommandRequest", 2: "InitCommandAck", 3: "InitEventRequest", 4: "InitEventAck",
    5: "InitFail", 6: "OperationRequest", 7: "OperationResponse", 8: "Event",
    9: "StartData", 10: "Data", 11: "CancelTransaction", 12: "EndData",
    13: "ProbeRequest", 14: "ProbeResponse",
}


def log(msg: str) -> None:
    print("%s %s" % (datetime.now().strftime("%H:%M:%S.%f")[:-3], msg), flush=True)


def parse_eth(data: bytes):
    if len(data) < 14:
        return None
    ethertype = struct.unpack_from("!H", data, 12)[0]
    return 14, ethertype


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="wlan0")
    ap.add_argument("--out", default="logs/capture")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pcap = open(str(out) + ".pcap", "wb")
    pcap.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))  # LINKTYPE_ETHERNET

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
    sock.bind((args.iface, 0))
    sock.settimeout(1.0)
    log("capturando en %s -> %s.pcap (Ctrl-C para parar)" % (args.iface, out))
    count = 0
    try:
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            parsed = parse_eth(data)
            if parsed is None:
                continue
            off, ethertype = parsed
            if ethertype != 0x0800:  # IPv4 only
                continue
            if len(data) < off + 20:
                continue
            ihl = (data[off] & 0x0F) * 4
            proto = data[off + 9]
            src = socket.inet_ntoa(data[off + 12:off + 16])
            dst = socket.inet_ntoa(data[off + 16:off + 20])
            l4 = off + ihl
            keep = False
            desc = ""
            ptp = ""
            if proto == 6 and len(data) >= l4 + 20:  # TCP
                sport, dport = struct.unpack_from("!HH", data, l4)
                flags = data[l4 + 13]
                payload = data[l4 + ((data[l4 + 12] >> 4) * 4):]
                if sport in WATCH_TCP or dport in WATCH_TCP:
                    keep = True
                    fstr = "".join(n for b, n in ((1, "F"), (2, "S"), (4, "R"), (8, "P"), (16, "A")) if flags & b)
                    desc = "TCP %s:%d -> %s:%d [%s] %d bytes" % (src, sport, dst, dport, fstr, len(payload))
                    if len(payload) >= 8 and (sport == PTPIP_PORT or dport == PTPIP_PORT):
                        plen, ptype = struct.unpack_from("<II", payload, 0)
                        if 8 <= plen <= 1024 * 1024 and ptype in TYPE_NAMES:
                            ptp = "    PTPIP len=%d type=%s" % (plen, TYPE_NAMES[ptype])
                            if ptype == 6 and len(payload) >= 18:
                                phase, code = struct.unpack_from("<IH", payload, 8)
                                ptp += " phase=%d op=0x%04x" % (phase, code)
                            elif ptype == 7 and len(payload) >= 10:
                                rc = struct.unpack_from("<H", payload, 8)[0]
                                ptp += " rc=0x%04x" % rc
                            elif ptype == 8 and len(payload) >= 10:
                                ev = struct.unpack_from("<H", payload, 8)[0]
                                ptp += " event=0x%04x" % ev
            elif proto == 17 and len(data) >= l4 + 8:  # UDP
                sport, dport, ulen = struct.unpack_from("!HHH", data, l4)
                if sport in WATCH_UDP or dport in WATCH_UDP:
                    keep = True
                    payload = data[l4 + 8:l4 + ulen]
                    desc = "UDP %s:%d -> %s:%d %d bytes" % (src, sport, dst, dport, len(payload))
                    text = payload[:80]
                    if b"M-SEARCH" in text:
                        desc += " [M-SEARCH]"
                    elif b"HTTP/1.1 200" in text:
                        desc += " [SSDP resp]"
                    elif b"_ptp" in text:
                        desc += " [mDNS _ptp]"
            if not keep:
                continue
            count += 1
            ts = time.time()
            pcap.write(struct.pack("<IIII", int(ts), int((ts % 1) * 1e6), len(data), len(data)))
            pcap.write(data)
            pcap.flush()
            log("#%d %s" % (count, desc))
            if ptp:
                log(ptp)
    except KeyboardInterrupt:
        pass
    finally:
        pcap.close()
        log("captura terminada, %d paquetes" % count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
