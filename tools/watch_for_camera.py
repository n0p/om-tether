#!/usr/bin/env python3
"""Espera a que aparezca la cámara en la LAN y sale cuando la encuentra.

Uso:  python3 tools/watch_for_camera.py [--net 192.168.1.0/24] [--interval 5]
Pensado para lanzarlo en segundo plano y que avise al terminar.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import discovery  # noqa: E402


def log(msg: str) -> None:
    print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", default="192.168.1.0/24")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--timeout", type=float, default=60)
    args = ap.parse_args()

    log("buscando cámara en %s (mDNS _ptp._tcp, SSDP, 15740, 80, AP 192.168.0.10)" % args.net)
    deadline = time.monotonic() + args.timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        found = discovery.discover(timeout=2.0, do_scan=True, extra_net=args.net)
        if found:
            log("ENCONTRADA:")
            for f in found:
                log("  %-16s :%-5d modo=%-5s origen=%s %s" % (f.host, f.port, f.mode or "?", f.source, f.detail))
            log("fin de la búsqueda, la cámara está en la red")
            return 0
        log("intento %d: nada todavía" % attempt)
        time.sleep(args.interval)
    log("agotado el tiempo sin encontrar la cámara")
    return 1


if __name__ == "__main__":
    sys.exit(main())
