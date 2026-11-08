#!/usr/bin/env python3
"""En cuanto aparezca la cámara por USB: leer 0xD052, ponerlo a 1 y volcar ops.

Hipótesis: OM Capture, al terminar el emparejamiento, deja la cámara en modo
control por PC (CameraControlMode = 1); sin eso el servidor PTP/IP rechaza.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import ptp  # noqa: E402
from omtether.pairing import UsbPtp, find_usb_cameras  # noqa: E402

DPC_CAMERA_CONTROL_MODE = 0xD052


def log(msg: str) -> None:
    print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def main() -> int:
    log("esperando la cámara por USB (5 min)")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if find_usb_cameras():
            break
        time.sleep(1)
    else:
        log("no apareció la cámara por USB")
        return 1

    time.sleep(1)
    try:
        u = UsbPtp()
    except RuntimeError as exc:
        log("USB: %s" % exc)
        return 2

    log("OpenSession -> 0x%04x" % u.open_session())
    rc, _, data = u.operation(0x1001)
    if rc == 0x2001:
        di = ptp.DeviceInfo.parse(data)
        log("%s %s fw %s" % (di.manufacturer, di.model, di.device_version))
        log("ops: %s" % " ".join("0x%04x" % o for o in di.operations))
        log("0xD052 en la lista de propiedades: %s" % (DPC_CAMERA_CONTROL_MODE in di.properties))

    rc, _, value = u.operation(0x1015, [DPC_CAMERA_CONTROL_MODE])
    log("GetDevicePropValue(0xD052) rc=0x%04x valor=%s" % (rc, value.hex() if value else "-"))

    if rc == 0x2001 and len(value) >= 2:
        import struct
        actual = struct.unpack_from("<H", value)[0]
        if actual != 1:
            rc, _, _ = u.operation(0x1016, [DPC_CAMERA_CONTROL_MODE], data_out=struct.pack("<H", 1))
            log("SetDevicePropValue(0xD052=1) rc=0x%04x" % rc)
            time.sleep(0.5)
            rc2, _, v2 = u.operation(0x1015, [DPC_CAMERA_CONTROL_MODE])
            log("relectura rc=0x%04x valor=%s" % (rc2, v2.hex() if v2 else "-"))
        else:
            log("ya estaba en 1 (control por PC)")
    u.operation(0x1003)
    u.close()
    log("hecho: desenchufa el USB y reintenta por Wi-Fi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
