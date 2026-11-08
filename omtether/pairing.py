"""USB side of the Wi-Fi pairing: opcode 0x94BF SetConnectPcInfo.

OM Capture's own instructions are: connect the camera by USB, select "Camera
Control" on the body, then unplug and let the camera join the access point.
Over USB the PC stores an entry in the camera's "connect PC info" list; that is
what the camera matches later, when a PTP/IP initiator introduces itself on the
network.

Entry layout (recovered from the response parser at 0x1002AB50 of
OlyCameraLib.dll, cross-checked against the camera itself):

    u8   nameLen            number of bytes of the computer name (0..15)
    u8   name[nameLen]      ANSI (CP_ACP), no terminator
    u32  count              number of adapters (0..5)
    u8   entries[count][8]  MAC address, big endian, padded to 8 bytes

The PTP operation takes one parameter: slot - 1 (slots are 1..4, i.e. four
computers can be remembered).  Always read the entry back with 0x94C0: it
validates both the layout and the MAC packing in one shot.
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import time
from pathlib import Path

from . import olympus, ptp

log = logging.getLogger(__name__)

OM_VIDS = (0x33A2, 0x07B4)
MAX_SLOTS = 4
MAX_MACS = 5


# --------------------------------------------------------------------- USB
def find_usb_cameras() -> list[dict]:
    """Look for OM Digital / Olympus USB devices through sysfs (no deps)."""
    out = []
    for dev in Path("/sys/bus/usb/devices").glob("*"):
        vid_f = dev / "idVendor"
        pid_f = dev / "idProduct"
        if not (vid_f.exists() and pid_f.exists()):
            continue
        try:
            vid = int(vid_f.read_text().strip(), 16)
            pid = int(pid_f.read_text().strip(), 16)
        except (OSError, ValueError):
            continue
        if vid not in OM_VIDS:
            continue
        busnum = _read_int(dev / "busnum") or 0
        devnum = _read_int(dev / "devnum") or 0
        out.append({
            "vid": vid,
            "pid": pid,
            "busnum": busnum,
            "devnum": devnum,
            "product": _read_str(dev / "product"),
            "manufacturer": _read_str(dev / "manufacturer"),
            "node": "/dev/bus/usb/%03d/%03d" % (busnum, devnum),
        })
    return out


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _read_str(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


class UsbPtp:
    """Minimal PTP over USB (still image class) using pyusb."""

    def __init__(self, vid: int | None = None, pid: int | None = None) -> None:
        try:
            import usb.core  # type: ignore
            import usb.util  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "pyusb is required for USB operations: pip install --user pyusb"
            ) from exc
        self._usb = usb
        devs = list(usb.core.find(find_all=True, custom_match=lambda d: d.idVendor in OM_VIDS))
        if vid is not None:
            devs = [d for d in devs if d.idVendor == vid and (pid is None or d.idProduct == pid)]
        if not devs:
            raise RuntimeError("no OM System / Olympus camera found on USB")
        first = devs[0]
        # a previous process (or the camera itself) can leave the bulk endpoints
        # stalled: reset and re-open the handle, otherwise the first write is EIO
        try:
            first.reset()
            usb.util.dispose_resources(first)
            time.sleep(0.6)
        except Exception:  # noqa: BLE001 - best effort
            pass
        devs = list(usb.core.find(find_all=True,
                                  custom_match=lambda d: d.idVendor in OM_VIDS))
        if not devs:
            raise RuntimeError("camera disappeared after USB reset")
        self.dev = devs[0]
        self.dev.set_configuration()
        usb.util.claim_interface(self.dev, 0)
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        self.ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        if self.ep_in is None or self.ep_out is None:
            raise RuntimeError("camera has no bulk endpoints (is it in a PTP mode?)")
        self.transaction = 0
        self._buf = b""

    def close(self) -> None:
        try:
            self._usb.util.release_interface(self.dev, 0)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._usb.util.dispose_resources(self.dev)
        except Exception:  # noqa: BLE001
            pass

    def _container(self, code: int, params: list[int]) -> bytes:
        # PTP/USB container: length(4) type(2) code(2) transaction(4) params...
        self.transaction += 1
        pkt = struct.pack("<IHH", 12 + 4 * len(params), 1, code) + struct.pack("<I", self.transaction)
        pkt += b"".join(struct.pack("<I", p) for p in params)
        return pkt

    def _fill(self, timeout: int = 10000) -> None:
        # always ask for a big buffer: libusb reports OVERFLOW (-75) when the
        # device sends more than the requested size
        chunk = self.ep_in.read(65536, timeout=timeout)
        if chunk:
            self._buf += bytes(chunk)

    def _read_exact(self, n: int, timeout: int = 10000) -> bytes:
        """Read exactly n bytes; PTP/USB containers can span bulk transfers."""
        while len(self._buf) < n:
            before = len(self._buf)
            self._fill(timeout)
            if len(self._buf) == before:
                break
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_container(self, timeout: int = 10000) -> tuple[int, int, bytes]:
        head = self._read_exact(12, timeout)
        if len(head) < 12:
            raise RuntimeError("short PTP container header (%d bytes)" % len(head))
        length, ptype, code = struct.unpack_from("<IHH", head)
        payload = self._read_exact(length - 12, timeout) if length > 12 else b""
        return ptype, code, payload

    def open_session(self, session_id: int = 1) -> int:
        rc, _, _ = self.operation(0x1002, [session_id])
        return rc

    def close_session(self) -> int:
        rc, _, _ = self.operation(0x1003, [])
        return rc

    def operation(self, code: int, params: list[int] | None = None,
                  data_out: bytes | None = None) -> tuple[int, list[int], bytes]:
        params = list(params or [])
        self.ep_out.write(self._container(code, params), timeout=10000)
        if data_out is not None:
            header = struct.pack("<IHH", 12 + len(data_out), 2, code) + struct.pack("<I", self.transaction)
            self.ep_out.write(header + data_out, timeout=30000)
        data = b""
        for _ in range(8):  # data container(s) then the response container
            try:
                ptype, field, payload = self._read_container()
            except Exception as exc:  # noqa: BLE001 - no data phase
                raise RuntimeError("no PTP response from camera (%s)" % exc) from exc
            if ptype == 2:  # data phase
                data += payload
                continue
            if ptype == 3:  # response: field is the response code
                n = len(payload) // 4
                rparams = list(struct.unpack_from("<%dI" % n, payload)) if n else []
                return field, rparams, data
        raise RuntimeError("too many PTP containers without a response")


# ----------------------------------------------------------------- payload
def parse_mac(text: str) -> bytes:
    digits = re.sub(r"[^0-9A-Fa-f]", "", text)
    if len(digits) != 12:
        raise ValueError("not a MAC address: %r" % text)
    return bytes.fromhex(digits)


def build_connect_pc_info(name: str, macs: list[str]) -> bytes:
    """Serialise one connect-PC entry (see the module docstring)."""
    # the camera stores the NUL terminator inside the length (nameLen = chars+1);
    # without it the operation is rejected with 0x201D InvalidParameter
    raw_name = name.encode("ascii", "replace")[:14] + b"\x00"
    if not macs:
        raise ValueError("at least one MAC address is required")
    if len(macs) > MAX_MACS:
        raise ValueError("the camera stores at most %d adapters" % MAX_MACS)
    payload = bytes([len(raw_name)]) + raw_name
    payload += struct.pack("<I", len(macs))
    for mac in macs:
        payload += parse_mac(mac) + b"\x00\x00"
    return payload


def parse_connect_pc_info(data: bytes) -> dict:
    """Inverse of build_connect_pc_info, for --list."""
    if len(data) < 5:
        return {"raw": data.hex()}
    name_len = data[0]
    name = data[1:1 + name_len].decode("ascii", "replace").rstrip("\x00")
    off = 1 + name_len
    count = struct.unpack_from("<I", data, off)[0]
    off += 4
    macs = []
    for _ in range(min(count, MAX_MACS)):
        chunk = data[off:off + 8]
        off += 8
        if len(chunk) < 8 or not any(chunk[:6]):
            continue
        macs.append(":".join("%02X" % b for b in chunk[:6]))
    return {"name": name, "macs": macs, "count": count, "raw": data.hex()}


def default_name() -> str:
    return socket.gethostname().split(".")[0][:15]


# ------------------------------------------------------------------ action
def register(args) -> int:
    from .ptpip import primary_macs

    name = args.name or default_name()
    macs = args.mac or primary_macs()
    slot = args.slot

    if args.dry_run:
        if not 1 <= slot <= MAX_SLOTS:
            print("slot must be 1..%d" % MAX_SLOTS)
            return 1
        payload = build_connect_pc_info(name, macs)
        print("name   : %r" % name)
        print("macs   : %s" % ", ".join(macs))
        print("slot   : %d (PTP param %d)" % (slot, slot - 1))
        print("payload: %s" % payload.hex())
        print("bytes  : %d" % len(payload))
        return 0

    cams = find_usb_cameras()
    if not cams:
        print("no OM System / Olympus camera on USB")
        print("(connect it, choose 'Camera Control' on the camera screen and retry)")
        return 1
    for c in cams:
        print("USB %s  %04x:%04x  %s %s" % (c["node"], c["vid"], c["pid"], c["manufacturer"], c["product"]))

    try:
        usb = UsbPtp()
    except RuntimeError as exc:
        print("USB: %s" % exc)
        return 2

    session_rc = usb.open_session()
    print("OpenSession -> rc=0x%04x" % session_rc)
    if session_rc not in (0x2001, 0x201E):   # OK / already open
        print("  no se pudo abrir sesión PTP; sigo de todos modos")
    try:
        return _register_ops(args, usb, name, macs, slot)
    finally:
        print("CloseSession -> rc=0x%04x" % usb.close_session())
        usb.close()


def _register_ops(args, usb, name: str, macs: list[str], slot: int) -> int:
    if args.list:
        for idx in range(MAX_SLOTS):
            try:
                rc, _, data = usb.operation(olympus.OC_GetConnectPcInfo, [idx])
            except Exception as exc:  # noqa: BLE001
                print("slot %d: GetConnectPcInfo failed: %s" % (idx + 1, exc))
                continue
            if rc != ptp.RC_OK:
                print("slot %d: rc=0x%04x (empty)" % (idx + 1, rc))
                continue
            print("slot %d: %s" % (idx + 1, parse_connect_pc_info(data)))
        return 0

    if args.clear:
        rc, _, _ = usb.operation(olympus.OC_ClearConnectPcInfo, [slot - 1])
        print("ClearConnectPcInfo(slot %d) -> rc=0x%04x" % (slot, rc))
        return 0 if rc == ptp.RC_OK else 4

    payload = bytes.fromhex(args.hex) if args.hex else build_connect_pc_info(name, macs)
    print("SetConnectPcInfo(slot %d) <- %s" % (slot, payload.hex()))
    rc, _, data = usb.operation(olympus.OC_SetConnectPcInfo, [slot - 1], data_out=payload)
    print("  rc=0x%04x" % rc)
    if rc != ptp.RC_OK:
        return 4

    # read it back: validates the layout and the MAC packing in one go
    rc, _, back = usb.operation(olympus.OC_GetConnectPcInfo, [slot - 1])
    print("  read back: rc=0x%04x %s" % (rc, parse_connect_pc_info(back) if rc == ptp.RC_OK else ""))
    return 0
