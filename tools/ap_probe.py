#!/usr/bin/env python3
"""Sonda del modo punto de acceso de la cámara (192.168.0.10).

Comprueba, en este orden:
  1. Puertos abiertos (80, 15740, 2869).
  2. Handshake PTP/IP con varias identidades.
  3. API HTTP de OI.Share: descriptor CGI y un par de consultas de estado.

Pensado para ejecutarse cuando el portátil ya está unido al SSID de la cámara.
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import ptp  # noqa: E402
from omtether.ptpip import PtpIpTransport  # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.10"


def tcp(port: int, timeout: float = 2.0) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http(path: str, timeout: float = 4.0) -> tuple[int, str]:
    url = "http://%s%s" % (HOST, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2000).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, "%s: %s" % (type(exc).__name__, exc)


def main() -> int:
    print("== puertos ==")
    for port in (80, 15740, 2869):
        print("  %5d %s" % (port, "abierto" if tcp(port) else "cerrado"))

    if tcp(15740):
        print("== PTP/IP ==")
        import os
        host_name = socket.gethostname().split(".")[0]
        tries = [("por defecto", None), ("GUID ceros", b"\x00" * 16)]
        for label, guid in tries:
            t = PtpIpTransport(HOST, name=host_name, timeout=8, **({"guid": guid} if guid else {}))
            try:
                t.connect()
                s = ptp.PtpSession(t)
                di = s.open()
                print("  %-12s ACEPTADO: %s %s fw %s ops=%d props=%d" % (
                    label, di.manufacturer, di.model, di.device_version,
                    len(di.operations), len(di.properties)))
                print("  ops: %s" % " ".join("0x%04x" % o for o in di.operations))
                s.close()
                return 0
            except Exception as exc:  # noqa: BLE001
                print("  %-12s %s" % (label, exc))
            finally:
                t.close()

    print("== HTTP OI.Share ==")
    for path in ("/", "/exec/oishare_descriptor.xml", "/exec/", "/DCIM/"):
        code, body = http(path)
        print("  %-32s -> %s (%d bytes)" % (path, code, len(body)))
        if code == 200 and body:
            print("     %s" % body[:300].replace("\n", " "))
    return 1


if __name__ == "__main__":
    sys.exit(main())
