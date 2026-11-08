#!/usr/bin/env python3
"""Self-test of the PTP/IP client against the mock camera (no hardware needed)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import olympus, ptp  # noqa: E402
from omtether.ptpip import PtpIpTransport  # noqa: E402
from tests.mock_camera import MockCamera  # noqa: E402


def main() -> int:
    cam = None
    try:
        cam = MockCamera()
        cam.start()
    except OSError:
        # another mock (tests/run_all.py) is already listening on 15740
        cam = None
    try:
        t = PtpIpTransport("127.0.0.1", 15740, timeout=5)
        hs = t.connect()
        print("handshake:", hs.responder_name, hs.connection_number)
        assert hs.responder_name == "MOCKCAM"

        s = ptp.PtpSession(t)
        di = s.open()
        print("model:", di.manufacturer, di.model, di.serial)
        assert di.manufacturer == "OMSYSTEM"
        assert di.model == "OM-1"
        assert di.supports(0x9484)
        assert 0xC002 in di.events

        oly = olympus.Olympus(s)
        print("control mode:", s.get_prop_u16(olympus.DPC_CameraControlMode))
        oly.liveview_start()
        frame = oly.liveview_frame()
        assert frame and frame.startswith(b"\xff\xd8\xff") and frame.endswith(b"\xff\xd9")
        print("live view frame:", len(frame), "bytes")

        oly.enter_pc_mode()
        oly.exit_pc_mode()
        s.close()
        t.close()
        print("OK - transport, session, properties and live view paths work")
        return 0
    finally:
        if cam is not None:
            cam.stop()


if __name__ == "__main__":
    sys.exit(main())
