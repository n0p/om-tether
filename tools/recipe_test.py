#!/usr/bin/env python3
"""Receta de control remoto: ¿conviven live view y 0xD052=1?

Pasos, con lectura del modo después de cada uno:
  1. live view OFF
  2. 0xD052 = 1
  3. live view ON  -> ¿sigue el modo en 1?
  4. tres frames  -> ¿llegan datos?
  5. disparo      -> ¿aparece fichero?
  6. tras 8 s, modo y ficheros
"""

from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import olympus, ptp  # noqa: E402
from omtether.ptpip import PtpIpTransport  # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.10"


def main() -> int:
    t = PtpIpTransport(HOST, timeout=10)
    t.connect()
    s = ptp.PtpSession(t)
    s.open()
    # NADA de CloseSession+OpenSession: cerrar y reabrir la sesión de golpe
    # deja el servicio PTP/IP de la cámara sin responder (ARP vivo, 15740 mudo)
    # y hay que reiniciarla a mano.

    def mode() -> str:
        rc, _, d = s.op(0x1015, [olympus.DPC_CameraControlMode], check=False)
        return d.hex() if rc == ptp.RC_OK else "err 0x%04x" % rc

    def setprop(code: int, value: int, size: int = 2) -> str:
        fmt = "<H" if size == 2 else "<I"
        for attempt in range(3):
            try:
                rc, _, _ = s.op(0x1016, [code], data_out=struct.pack(fmt, value), check=False)
                return "0x%04x" % rc
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1.0)
        return "timeout (%s)" % last

    print("0) modo inicial: %s" % mode())
    print("1) live view off -> %s" % setprop(olympus.DPC_LiveViewModeOM, 0, 4))
    time.sleep(0.5)
    print("   modo: %s" % mode())
    print("2) 0xD052=1 -> %s   modo: %s" % (setprop(olympus.DPC_CameraControlMode, 1), mode()))
    print("3) live view on -> %s   modo: %s" % (
        setprop(olympus.DPC_LiveViewModeOM, 0x04000300, 4), mode()))
    for i in range(3):
        rc, _, d = s.op(olympus.OC_GetLiveViewImage, [1], expect_data=True,
                        check=False, timeout=6)
        print("   frame %d rc=0x%04x len=%d  modo=%s" % (i + 1, rc, len(d), mode()))
        time.sleep(0.2)

    before = {h for h, _ in s.iter_files()}
    rc, _, _ = s.op(olympus.OC_Capture, [3], check=False)
    print("5) disparo rc=0x%04x" % rc)
    time.sleep(8)
    new = {h for h, _ in s.iter_files()} - before
    print("6) nuevas: %s   modo: %s" % ([hex(h) for h in new], mode()))
    s.close()
    return 0 if new else 1


if __name__ == "__main__":
    sys.exit(main())
