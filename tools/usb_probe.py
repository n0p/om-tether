#!/usr/bin/env python3
"""Sonda del modo control-por-PC por USB, tolerante a sesiones zombie."""

from __future__ import annotations

import struct
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether.pairing import UsbPtp  # noqa: E402

DPC = 0xD052


def log(msg: str) -> None:
    print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def main() -> int:
    u = UsbPtp()
    rc = u.open_session()
    if rc == 0x201E:
        log("sesión ya abierta (0x201E): cierro y reabro")
        try:
            u.close_session()
        except Exception as exc:  # noqa: BLE001
            log("CloseSession falló: %s" % exc)
        rc = u.open_session()
    log("OpenSession -> 0x%04x" % rc)
    if rc != 0x2001:
        u.close()
        return 1

    try:
        rc, _, v = u.operation(0x1015, [DPC])
        log("GetDevicePropValue(0xD052) rc=0x%04x valor=%s" % (rc, v.hex() if v else "-"))
    except Exception as exc:  # noqa: BLE001
        log("lectura falló: %s" % exc)
        u.close()
        return 2

    try:
        rc, _, _ = u.operation(0x910B, [1])
        log("0x910B(SetCameraControlMode=1) -> rc=0x%04x" % rc)
    except Exception as exc:  # noqa: BLE001
        log("0x910B falló: %s" % exc)

    for fmt, label in ((struct.pack("<H", 1), "u16"), (struct.pack("<I", 1), "u32")):
        log("SetDevicePropValue(0xD052=1) %s..." % label)
        try:
            rc, _, _ = u.operation(0x1016, [DPC], data_out=fmt)
            log("  rc=0x%04x" % rc)
        except Exception as exc:  # noqa: BLE001
            log("  timeout/error: %s" % exc)
            break
        time.sleep(1.0)
        try:
            rc, _, v = u.operation(0x1015, [DPC])
            log("  relectura rc=0x%04x valor=%s" % (rc, v.hex() if v else "-"))
            if v == b"\x01\x00":
                log("  modo control por PC activado")
                break
        except Exception as exc:  # noqa: BLE001
            log("  relectura falló: %s" % exc)

    try:
        u.close_session()
    except Exception:  # noqa: BLE001
        pass
    u.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
