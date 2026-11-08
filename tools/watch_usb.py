#!/usr/bin/env python3
"""Espera a que aparezca la cámara por USB (VID OM Digital/Olympus) y sale."""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import pairing  # noqa: E402


def log(msg: str) -> None:
    print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def main() -> int:
    deadline = time.monotonic() + 900
    log("esperando cámara por USB (VID 33A2 / 07B4)")
    while time.monotonic() < deadline:
        cams = pairing.find_usb_cameras()
        if cams:
            for c in cams:
                log("USB %s  %04x:%04x  %s %s" % (c["node"], c["vid"], c["pid"], c["manufacturer"], c["product"]))
            log("cámara en USB, listo para emparejar")
            return 0
        time.sleep(2)
    log("tiempo agotado esperando la cámara por USB")
    return 1


if __name__ == "__main__":
    sys.exit(main())
