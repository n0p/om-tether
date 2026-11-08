#!/usr/bin/env python3
"""Tests for the libgphoto2 helper: GUID derivation and the settings file."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omtether import gphoto  # noqa: E402


def test_guid_for_mac() -> bool:
    guid = gphoto.guid_for_mac("aa:bb:cc:dd:ee:ff")
    return (len(guid) == 16 and guid[:10] == b"\x00" * 10
            and guid[10:] == bytes([0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA]))


def test_guid_text() -> bool:
    text = gphoto.guid_text(gphoto.guid_for_mac("b6:21:22:79:29:f0"))
    # 16 bytes en hex + 15 dos puntos = 47 caracteres, que es lo que espera
    # ptp_nikon_getptpipguid()
    return (len(text) == 47 and text.startswith("00:00:00:00:00:00:00:00:00:00:")
            and text.endswith(":f0:29:79:22:21:b6")
            and gphoto.parse_guid(text) == gphoto.guid_for_mac("b6:21:22:79:29:f0"))


def test_settings_roundtrip() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings"
        path.write_text("ptp2=autofocus=on\nptp2_ip=guid=aa:bb:cc:dd:ee:ff:00:11:"
                        "22:33:44:55:66:77:88:99\ngphoto2=idle=1\n", encoding="utf-8")
        before = gphoto.read_setting(path)
        if before is None:
            return False
        new = gphoto.guid_text(gphoto.guid_for_mac("aa:bb:cc:dd:ee:ff"))
        gphoto.write_setting(new, path)
        text = path.read_text(encoding="utf-8")
        return (gphoto.read_setting(path) == new
                and "ptp2=autofocus=on" in text
                and "gphoto2=idle=1" in text
                and text.count("ptp2_ip=guid=") == 1)


def test_settings_missing_file() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sub" / "settings"
        if gphoto.read_setting(path) is not None:
            return False
        gphoto.write_setting("00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff", path)
        return gphoto.read_setting(path) == ("00:11:22:33:44:55:66:77:88:99:"
                                             "aa:bb:cc:dd:ee:ff")


def test_candidates() -> bool:
    cands = gphoto.candidates()
    return all(len(guid) == 16 and guid[:10] == b"\x00" * 10 for _mac, guid in cands)


def main() -> int:
    tests = [
        ("GUID desde MAC (00*10 + invertida)", test_guid_for_mac),
        ("texto del GUID (47 caracteres)", test_guid_text),
        ("settings: conserva otras claves", test_settings_roundtrip),
        ("settings: fichero inexistente", test_settings_missing_file),
        ("candidatos de las interfaces", test_candidates),
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
    print("\n%d/%d gphoto tests passed" % (len(tests) - failed, len(tests)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
