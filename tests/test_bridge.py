#!/usr/bin/env python3
"""Bridge test: our own client talks to the bridge, the bridge to the mock camera.

Covers the three things the bridge has to do: accept any initiator GUID, forward
operations (device info), and synthesise the capture events libgphoto2 waits for.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import olympus, ptp  # noqa: E402
from omtether.bridge import Bridge, BridgeConfig  # noqa: E402
from omtether.ptpip import PtpIpTransport  # noqa: E402
from tests.mock_camera import MockCamera  # noqa: E402


def main() -> int:
    cam = MockCamera(port=15744)
    cam.start()
    bridge = Bridge(BridgeConfig(host="127.0.0.1", host_port=15744,
                                 listen_port=15745, download=False))
    bridge.start()
    time.sleep(2.0)
    ok = True
    try:
        # GUID cualquiera: el puente no lo valida (libgphoto2 manda el suyo)
        transport = PtpIpTransport("127.0.0.1", port=15745, guid=b"\x77" * 16, timeout=8)
        transport.connect()
        session = ptp.PtpSession(transport)
        di = session.open()
        info_ok = di.manufacturer == "OMSYSTEM" and di.supports(0x9481)
        print("  %s device info a través del puente (%s %s)"
              % ("ok  " if info_ok else "FAIL", di.manufacturer, di.model))
        ok &= info_ok

        session.op(olympus.OC_Capture, [3], check=False)
        session.op(olympus.OC_Capture, [6], check=False)
        seen: list[tuple[int, list[int]]] = []
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            ev = transport.get_event(0.3)
            if ev is not None:
                seen.append((ev.code, ev.params))
                if ev.code in (0xC102, 0xC002) and ev.params and ev.params[0]:
                    break
        # sólo 0xC102: libgphoto2 <= 2.5.32 aborta si ve un 0xC103 con Param1=0
        got = next(((c, p) for c, p in seen if c == 0xC102 and p and p[0]), None)
        ev_ok = got is not None and 0xC103 not in [c for c, _ in seen]
        print("  %s eventos sintetizados tras el disparo (%s)"
              % ("ok  " if ev_ok else "FAIL",
                 ", ".join("%s %s" % (hex(c), [hex(x) for x in p]) for c, p in seen) or "ninguno"))
        ok &= ev_ok

        session.close()
        transport.close()
    except Exception as exc:  # noqa: BLE001
        print("  FAIL excepción: %s" % exc)
        ok = False
    finally:
        bridge.stop()
        cam.stop()
    print("\n%s" % ("bridge OK" if ok else "bridge FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
