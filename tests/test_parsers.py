#!/usr/bin/env python3
"""Unit tests for the binary parsers (no camera, no network)."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import olympus, pairing, ptp  # noqa: E402


def prop_desc(code: int, dtype: int, default, current, enum=None) -> bytes:
    out = struct.pack("<HH", code, dtype) + b"\x01"
    for value in (default, current):
        if dtype == ptp.DTC_UINT16:
            out += struct.pack("<H", value)
        elif dtype == ptp.DTC_UINT32:
            out += struct.pack("<I", value)
        else:
            out += bytes([len(value)]) + value.encode("utf-16-le")
    if enum:
        out += b"\x01" + struct.pack("<H", len(enum))
        for value in enum:
            out += struct.pack("<H", value) if dtype == ptp.DTC_UINT16 else struct.pack("<I", value)
    else:
        out += b"\x00"
    return out


def test_prop_list_uint16() -> bool:
    data = struct.pack("<I", 2)
    data += prop_desc(0xD002, ptp.DTC_UINT16, 0x0110, 0x0128, enum=[0x0110, 0x0128, 0x0160])
    data += prop_desc(0xD009, ptp.DTC_UINT16, 1, 7)
    parsed = olympus.parse_prop_list(data)
    ok = [c for c, _ in parsed] == [0xD002, 0xD009]
    ok = ok and struct.unpack("<H", parsed[0][1])[0] == 0x0128
    ok = ok and struct.unpack("<H", parsed[1][1])[0] == 7
    return ok


def test_prop_list_uint32() -> bool:
    data = struct.pack("<I", 1) + prop_desc(0xD06D, ptp.DTC_UINT32, 0, 0x04000300)
    parsed = olympus.parse_prop_list(data)
    return len(parsed) == 1 and struct.unpack("<I", parsed[0][1])[0] == 0x04000300


def test_object_info() -> bool:
    raw = (struct.pack("<I", 0x00010001) + struct.pack("<H", 0x3801) + struct.pack("<H", 0)
           + struct.pack("<I", 1234) + struct.pack("<H", 0) + struct.pack("<I", 0)
           + struct.pack("<I", 0) + struct.pack("<I", 0) + struct.pack("<I", 6000)
           + struct.pack("<I", 4000) + struct.pack("<I", 8) + struct.pack("<I", 0)
           + struct.pack("<H", 0) + struct.pack("<I", 0) + struct.pack("<I", 0)
           + bytes([10]) + "P1010001.J".encode("utf-16-le") + b"G"  # placeholder, fixed below
           + bytes([16]) + "20260101T120000".encode("utf-16-le")
           + bytes([16]) + "20260101T120000".encode("utf-16-le")
           + bytes([0]))
    # rebuild the filename properly: 10 characters "P1010001.J"? use 9
    name = "P1010001.JPG"
    raw = (struct.pack("<I", 0x00010001) + struct.pack("<H", 0x3801) + struct.pack("<H", 0)
           + struct.pack("<I", 1234) + struct.pack("<H", 0) + struct.pack("<I", 0)
           + struct.pack("<I", 0) + struct.pack("<I", 0) + struct.pack("<I", 6000)
           + struct.pack("<I", 4000) + struct.pack("<I", 8) + struct.pack("<I", 0)
           + struct.pack("<H", 0) + struct.pack("<I", 0) + struct.pack("<I", 0)
           + bytes([len(name)]) + name.encode("utf-16-le")
           + bytes([16]) + "20260101T120000".encode("utf-16-le")
           + bytes([16]) + "20260101T120000".encode("utf-16-le")
           + bytes([0]))
    oi = ptp.ObjectInfo.parse(raw)
    return oi.filename == name and oi.size == 1234 and oi.image_width == 6000 and oi.format == 0x3801


def test_connect_pc_info() -> bool:
    payload = pairing.build_connect_pc_info("OM-PC", ["AA:BB:CC:DD:EE:FF"])
    # el nombre viaja con su NUL dentro de nameLen (la cámara lo exige)
    expected = "064f4d2d50430001000000aabbccddeeff0000"
    fields = pairing.parse_connect_pc_info(payload)
    return (payload.hex() == expected and fields["name"] == "OM-PC"
            and fields["macs"] == ["AA:BB:CC:DD:EE:FF"])


def test_device_info() -> bool:
    from tests.mock_camera import DEVICE_INFO
    di = ptp.DeviceInfo.parse(DEVICE_INFO)
    return (di.manufacturer == "OMSYSTEM" and di.model == "OM-1"
            and di.supports(0x9484) and 0xC002 in di.events)


def test_mdns_parser() -> bool:
    import struct as _s
    from omtether import mdns

    def name(text: str) -> bytes:
        out = b""
        for label in text.split("."):
            if label:
                raw = label.encode()
                out += bytes([len(raw)]) + raw
        return out + b"\x00"

    def rr(owner: str, rtype: int, rdata: bytes) -> bytes:
        return name(owner) + _s.pack(">HHIH", rtype, 1, 120, len(rdata)) + rdata

    instance = "OM-1-1234567._ptp._tcp.local"
    target = "OM-1-1234567.local"
    txt = b"\x08vid=04B0"
    packet = _s.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 3)
    packet += rr("_ptp._tcp.local", 12, name(instance))
    packet += rr(instance, 33, _s.pack(">HHH", 0, 0, 15740) + name(target))
    packet += rr(instance, 16, txt)
    packet += rr(target, 1, bytes([192, 168, 1, 50]))
    found = mdns.parse_response(packet)
    return (len(found) == 1 and found[0]["host"] == "192.168.1.50"
            and found[0]["port"] == 15740 and found[0]["txt"].get("vid") == "04B0")


def main() -> int:
    tests = [
        ("prop list (uint16 + enum)", test_prop_list_uint16),
        ("prop list (uint32)", test_prop_list_uint32),
        ("ObjectInfo", test_object_info),
        ("ConnectPcInfo", test_connect_pc_info),
        ("DeviceInfo", test_device_info),
        ("mDNS _ptp._tcp parser", test_mdns_parser),
    ]
    failed = 0
    for name, fn in tests:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  FAIL %s - %s" % (name, exc))
        else:
            print(("  ok   " if ok else "  FAIL ") + name)
        failed += 0 if ok else 1
    print("\n%d/%d parser tests passed" % (len(tests) - failed, len(tests)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
