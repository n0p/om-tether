"""Make libgphoto2 (and therefore darktable, digiKam, Entangle…) talk to the camera.

The pieces are already in libgphoto2: the `ptpip` port driver discovers the
camera over mDNS, the OM-1 Mark II is in the model table and the capture path is
the same `0x9481(3)` + `0x9481(6)` sequence this project uses. What does not
match is the **initiator GUID**: `ptp_nikon_getptpipguid()` invents a random one
and stores it in `~/.gphoto/settings`, while the camera only accepts
`00*10 + reversed(paired MAC)` — anything else is answered with `InitFail 1`.

This module reads the MACs of the local interfaces, computes the GUID each one
produces, checks against the camera which one it actually accepts, and writes
that one into the gphoto settings file:

    omtether gphoto --host 192.168.0.10          # only report
    omtether gphoto --host 192.168.0.10 --write  # report and write the setting

After that:

    gphoto2 --camera "PTP/IP Camera" --port ptpip:<ip> --summary
    darktable   # the camera shows up in the tethering view

Nothing here patches libgphoto2; the proper fix (try the local MACs instead of a
random GUID when no setting exists) belongs upstream in `camlibs/ptp2/ptpip.c`.
"""

from __future__ import annotations

import socket
from pathlib import Path

from . import ptpip

SETTINGS = Path.home() / ".gphoto/settings"
SECTION = "ptp2_ip"
KEY = "guid"


def guid_for_mac(mac: str) -> bytes:
    """GUID que acepta la cámara para una MAC emparejada: 00*10 + MAC invertida."""
    raw = bytes(int(part, 16) for part in mac.split(":"))
    return b"\x00" * 10 + bytes(reversed(raw))


def guid_text(guid: bytes) -> str:
    """Formato del fichero de ajustes de gphoto2: 16 bytes en hex separados por ':'."""
    return ":".join("%02x" % byte for byte in guid)


def parse_guid(text: str) -> bytes | None:
    parts = text.strip().split(":")
    if len(parts) != 16:
        return None
    try:
        return bytes(int(part, 16) for part in parts)
    except ValueError:
        return None


def read_setting(path: Path = SETTINGS) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    prefix = "%s=%s=" % (SECTION, KEY)
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def write_setting(value: str, path: Path = SETTINGS) -> None:
    """Escribe `ptp2_ip=guid=…` conservando el resto de ajustes."""
    prefix = "%s=%s=" % (SECTION, KEY)
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        lines = []
    out = [line for line in lines if not line.startswith(prefix)]
    out.append(prefix + value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def candidates() -> list[tuple[str, bytes]]:
    """[(mac, guid)] para cada interfaz local que puede estar en la ranura."""
    out: list[tuple[str, bytes]] = []
    for mac in ptpip.primary_macs():
        try:
            out.append((mac, guid_for_mac(mac)))
        except ValueError:
            continue
    return out


def probe(host: str, guid: bytes, timeout: float = 6.0) -> bool:
    """¿Acepta la cámara un handshake PTP/IP con este GUID?

    Sólo abre el transporte y lo cierra: no toca sesión, tarjeta ni ajustes.
    """
    transport = None
    try:
        transport = ptpip.PtpIpTransport(host, guid=guid, timeout=timeout)
        transport.connect()
        return True
    except Exception:  # noqa: BLE001 - cualquier fallo es "no aceptado"
        return False
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass


def accepted_guid(host: str, timeout: float = 6.0) -> tuple[str, bytes] | None:
    for mac, guid in candidates():
        if probe(host, guid, timeout):
            return mac, guid
    return None
