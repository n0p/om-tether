#!/usr/bin/env python3
"""Reintenta el handshake PTP/IP hasta que la cámara acepte (tras el pairing USB)."""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import discovery, ptp  # noqa: E402
from omtether.ptpip import PtpIpError, PtpIpTransport  # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.10"
TRIES = int(sys.argv[2]) if len(sys.argv) > 2 else 40
DELAY = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0


def log(msg: str) -> None:
    print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def main() -> int:
    log("reintentando PTP/IP contra %s (cada %.0fs, %d intentos)" % (HOST, DELAY, TRIES))
    for attempt in range(1, TRIES + 1):
        if not discovery.probe_ptpip(HOST, timeout=1.0):
            if attempt % 6 == 1:
                log("intento %d: el 15740 no responde todavía" % attempt)
            time.sleep(DELAY)
            continue
        t = PtpIpTransport(HOST, timeout=10)
        try:
            hs = t.connect()
            s = ptp.PtpSession(t)
            di = s.open()
            log("ACEPTADO en el intento %d: %s %s (fw %s)" % (
                attempt, di.manufacturer, di.model, di.device_version))
            log("ops=%d props=%d eventos=%d | 0x9484 live view: %s | 0x94BA keep-alive: %s" % (
                len(di.operations), len(di.properties), len(di.events),
                di.supports(0x9484), di.supports(0x94BA)))
            s.close()
            t.close()
            return 0
        except PtpIpError as exc:
            log("intento %d: rechazado: %s" % (attempt, exc))
        except Exception as exc:  # noqa: BLE001
            log("intento %d: error: %s" % (attempt, exc))
        finally:
            t.close()
        time.sleep(DELAY)
    log("agotados los intentos")
    return 1


if __name__ == "__main__":
    sys.exit(main())
