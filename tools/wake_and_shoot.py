#!/usr/bin/env python3
"""Espera a que la cámara esté despierta (live view con datos) y dispara.

Sirve para separar "la cámara está en standby" de "el disparo remoto no funciona".
Mantiene el modo transferencia con 0x94BA(1) cada pocos segundos.
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
WAIT = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0
OUT = Path("logs/shots")


def log(msg: str) -> None:
    print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    t = PtpIpTransport(HOST, timeout=10)
    t.connect()
    s = ptp.PtpSession(t)
    s.open()
    log("sesión abierta; esperando a que la cámara esté despierta (%ds)" % WAIT)

    s.op(olympus.OC_TransferModeAlive, [1], check=False)
    s.set_prop_u32(olympus.DPC_LiveViewModeOM, 0x04000300)

    deadline = time.monotonic() + WAIT
    awake = False
    while time.monotonic() < deadline:
        rc, _, d = s.op(olympus.OC_GetLiveViewImage, [1], expect_data=True,
                        check=False, timeout=6)
        if rc == ptp.RC_OK and d:
            awake = True
            log("cámara despierta (live view %d B)" % len(d))
            break
        s.op(olympus.OC_TransferModeAlive, [1], check=False)
        time.sleep(2)

    if not awake:
        log("no se despertó: sigue en standby")
        s.close()
        return 1

    before = set(s.get_object_handles())
    log("disparando 0x9481(3)")
    rc, _, _ = s.op(olympus.OC_Capture, [3], check=False)
    log("  rc=0x%04x" % rc)

    handles: list[int] = []
    released = False
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        ev = t.get_event(0.3)
        if ev is not None:
            log("  evento %s 0x%04x %s" % (olympus.event_name(ev.code), ev.code,
                                           [hex(p) for p in ev.params]))
            if ev.code in (olympus.EC_CaptureComplete, olympus.EC_ImageRecordFinish) and not released:
                s.op(olympus.OC_Capture, [0], check=False)
                released = True
                log("  liberado")
            if ev.code in (olympus.EC_ObjectAdded, ptp.EC_ObjectAdded,
                           olympus.EC_DirectStoreImage) and ev.params and ev.params[0]:
                handles.append(ev.params[0])
        handles.extend(h for h in s.get_object_handles()
                       if h not in before and h not in handles)
        if handles and released:
            break

    for h in handles:
        try:
            info = s.get_object_info(h)
        except ptp.PtpError as exc:
            log("  0x%x: %s" % (h, exc))
            continue
        log("  objeto 0x%x %s %d B" % (h, info.filename, info.size))
        if info.size and info.format != 0x3001:
            path = download_object(s, h, OUT)
            log("  guardado %s (%.1f KB)" % (path, Path(path).stat().st_size / 1024))
    log("handles nuevos: %s" % [hex(h) for h in handles])
    s.close()
    return 0 if handles else 2


if __name__ == "__main__":
    sys.exit(main())
