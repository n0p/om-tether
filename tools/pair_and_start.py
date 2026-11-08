#!/usr/bin/env python3
"""Secuencia completa de arranque del tethering Wi-Fi.

1. Espera la cámara por USB.
2. Reescribe la entrada de emparejamiento (0x94BF) y verifica con 0x94C0.
3. Pone CameraControlMode (0xD052) = 1: la cámara pasa a
   CAM_START_PC_WAIT_USB_DISCONNECT y deja de responder (timeout esperado).
4. Avisa para desenchufar el USB.
5. Sondea 15740 + handshake PTP/IP hasta que acepte o se agote el tiempo.

Uso: pair_and_start.py [host] [segundos]
"""

from __future__ import annotations

import struct
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import discovery, ptp  # noqa: E402
from omtether.pairing import (UsbPtp, build_connect_pc_info, default_name,  # noqa: E402
                              find_usb_cameras)
from omtether.ptpip import PtpIpTransport  # noqa: E402

DPC_CAMERA_CONTROL_MODE = 0xD052
SLOT = 2


def log(msg: str) -> None:
    print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def local_macs() -> list[str]:
    macs = []
    base = Path("/sys/class/net")
    for nic in sorted(p.name for p in base.iterdir()):
        if nic.startswith(("lo", "docker", "veth", "virbr", "br-", "vnet", "tap", "tun")):
            continue
        try:
            mac = (base / nic / "address").read_text().strip()
        except OSError:
            continue
        if mac and mac != "00:00:00:00:00:00":
            macs.append(mac)
    return macs


def usb_phase() -> bool:
    import os
    wait = int(os.environ.get("OM_USB_WAIT", "120"))
    log("esperando la cámara por USB (%d s)" % wait)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if find_usb_cameras():
            break
        time.sleep(1)
    else:
        log("no apareció por USB")
        return False

    time.sleep(1)
    try:
        u = UsbPtp()
    except RuntimeError as exc:
        log("USB: %s" % exc)
        return False

    try:
        rc = u.open_session()
        if rc == 0x201E:
            log("sesión ya abierta: cerrando")
            try:
                u.close_session()
            except Exception:  # noqa: BLE001
                pass
            rc = u.open_session()
        log("OpenSession -> 0x%04x" % rc)
        if rc != 0x2001:
            return False

        macs = local_macs()
        payload = build_connect_pc_info(default_name(), macs)
        log("SetConnectPcInfo(slot %d): %s -> %s" % (SLOT, macs, payload.hex()))
        rc, _, _ = u.operation(0x94BF, [SLOT - 1], data_out=payload)
        log("  rc=0x%04x" % rc)
        rc, _, d = u.operation(0x94C0, [SLOT - 1])
        log("  relectura rc=0x%04x %s" % (rc, d.hex()))

        log("SetDevicePropValue(0xD052=1) — la cámara cambiará de estado")
        try:
            rc, _, _ = u.operation(0x1016, [DPC_CAMERA_CONTROL_MODE], data_out=struct.pack("<H", 1))
            log("  rc=0x%04x" % rc)
        except Exception as exc:  # noqa: BLE001
            log("  sin respuesta (esperado en la transición): %s" % exc)
        return True
    finally:
        try:
            u.close()
        except Exception:  # noqa: BLE001
            pass


def wifi_phase(host: str, seconds: int) -> int:
    log("=== DESENCHUFA EL CABLE USB AHORA ===")
    log("sondeando %s durante %d s" % (host, seconds))
    deadline = time.monotonic() + seconds
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        if not discovery.probe_ptpip(host, timeout=1.0):
            time.sleep(2)
            continue
        t = PtpIpTransport(host, timeout=10)
        try:
            t.connect()
            s = ptp.PtpSession(t)
            di = s.open()
            log("ACEPTADO: %s %s (fw %s)" % (di.manufacturer, di.model, di.device_version))
            log("ops=%d props=%d | live view 0x9484=%s | keep-alive 0x94BA=%s" % (
                len(di.operations), len(di.properties),
                0x9484 in di.operations, 0x94BA in di.operations))
            s.close()
            return 0
        except Exception as exc:  # noqa: BLE001
            log("intento %d: %s" % (attempt, exc))
        finally:
            t.close()
        time.sleep(2)
    log("sin aceptación en el plazo")
    return 1


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.10"
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    if not usb_phase():
        return 1
    return wifi_phase(host, seconds)


if __name__ == "__main__":
    sys.exit(main())
