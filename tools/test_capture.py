#!/usr/bin/env python3
"""Prueba de disparo + descarga por PTP/IP, con traza de eventos.

Uso: test_capture.py [host] [segundos_de_espera]
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import olympus, ptp  # noqa: E402
from omtether.cli import download_object  # noqa: E402
from omtether.ptpip import PtpIpTransport  # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.10"
WAIT = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
OUT = Path("logs/shots")


def log(msg: str) -> None:
    print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    t = PtpIpTransport(HOST, timeout=10)
    t.connect()
    s = ptp.PtpSession(t)
    s.open()
    log("sesión abierta")

    try:
        mode = s.get_prop_u16(olympus.DPC_CameraControlMode)
        log("0xD052 CameraControlMode = %d" % mode)
    except ptp.PtpError as exc:
        log("0xD052 no legible: %s" % exc)

    before = set(s.get_object_handles())
    log("objetos en tarjeta antes: %d" % len(before))

    log("disparo: 0x9481(3)")
    rc, _, _ = s.op(olympus.OC_Capture, [3])
    log("  rc=0x%04x" % rc)

    deadline = time.monotonic() + WAIT
    released = False
    handles: list[int] = []
    while time.monotonic() < deadline:
        ev = t.get_event(0.3)
        if ev is not None:
            log("  evento %s (0x%04x) params=%s" % (
                olympus.event_name(ev.code), ev.code, [hex(p) for p in ev.params]))
            if ev.code in (olympus.EC_CaptureComplete, olympus.EC_ImageRecordFinish) and not released:
                log("  liberando: 0x9481(0)")
                try:
                    rc, _, _ = s.op(olympus.OC_Capture, [0])
                    log("    rc=0x%04x" % rc)
                except ptp.PtpError as exc:
                    log("    error: %s" % exc)
                released = True
            if ev.code in (olympus.EC_ObjectAdded, ptp.EC_ObjectAdded,
                           olympus.EC_DirectStoreImage) and ev.params and ev.params[0]:
                handles.append(ev.params[0])
        new = [h for h in s.get_object_handles() if h not in before]
        if new:
            handles.extend(h for h in new if h not in handles)
            if handles and (released or time.monotonic() > deadline - WAIT + 5):
                break
    if not released:
        log("no llegó CaptureComplete; liberando igualmente")
        try:
            s.op(olympus.OC_Capture, [0])
        except ptp.PtpError:
            pass

    log("handles nuevos: %s" % [hex(h) for h in handles])
    for h in handles:
        try:
            info = s.get_object_info(h)
        except ptp.PtpError as exc:
            log("  0x%x sin info: %s" % (h, exc))
            continue
        log("  0x%x %s %d B" % (h, info.filename, info.size))
        if info.size and info.format != 0x3001:
            path = download_object(s, h, OUT)
            log("  guardado %s (%.1f KB)" % (path, Path(path).stat().st_size / 1024))
    s.close()
    return 0 if handles else 1


if __name__ == "__main__":
    sys.exit(main())
