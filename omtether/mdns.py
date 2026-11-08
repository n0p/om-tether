"""mDNS / DNS-SD discovery of the camera's PTP/IP service.

The OM-1 II firmware advertises `_ptp._tcp` on port 15740 with a TXT record
(`wifi_mdns.c`, `15740 vid=%08lX` in the decrypted image), which is what CIPA
recommends for finding a PTP/IP responder.  That makes mDNS the most reliable
way to locate the camera in infrastructure mode; SSDP/UPnP belongs to the
OI.Share side.

Implemented with plain sockets: one PTR query for `_ptp._tcp.local`, then join
the SRV/TXT/A records of the answers.
"""

from __future__ import annotations

import logging
import random
import shutil
import socket
import subprocess
import struct
import threading
import time

log = logging.getLogger(__name__)

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
SERVICE = "_ptp._tcp.local"

TYPE_A = 1
TYPE_PTR = 12
TYPE_TXT = 16
TYPE_SRV = 33
CLASS_IN = 1
CLASS_QU = 0x8000  # ask for a unicast reply


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        if not label:
            continue
        raw = label.encode("utf-8")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def build_query(service: str = SERVICE) -> bytes:
    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    question = _encode_name(service) + struct.pack(">HH", TYPE_PTR, CLASS_IN | CLASS_QU)
    return header + question


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.off = 0

    def u16(self) -> int:
        v = struct.unpack_from(">H", self.data, self.off)[0]
        self.off += 2
        return v

    def u32(self) -> int:
        v = struct.unpack_from(">I", self.data, self.off)[0]
        self.off += 4
        return v

    def name(self) -> str:
        labels: list[str] = []
        off = self.off
        jumped = False
        hops = 0
        size = len(self.data)
        while 0 <= off < size:
            length = self.data[off]
            if length == 0:
                off += 1
                if not jumped:
                    self.off = off
                break
            if length & 0xC0 == 0xC0:  # compression pointer
                if off + 1 >= size:
                    break
                pointer = ((length & 0x3F) << 8) | self.data[off + 1]
                if not jumped:
                    self.off = off + 2
                if pointer >= size:
                    break
                off = pointer
                jumped = True
                hops += 1
                if hops > 16:
                    break
                continue
            if off + 1 + length > size:
                break
            labels.append(self.data[off + 1:off + 1 + length].decode("utf-8", "replace"))
            off += 1 + length
            if not jumped:
                self.off = off
        return ".".join(labels)


def parse_records(data: bytes) -> dict:
    """Collect the DNS-SD records of one mDNS packet.

    Returns {"ptr": [instances], "srv": {...}, "txt": {...}, "a": {...}} so the
    caller can merge records that arrive in different packets (cameras split
    them: one message carries the PTR, another the SRV/TXT/A).
    """
    out: dict = {"ptr": [], "srv": {}, "txt": {}, "a": {}}
    if len(data) < 12:
        return out
    _id, flags, qd, an, ns, ar = struct.unpack_from(">HHHHHH", data, 0)
    if not flags & 0x8000:  # a query, not a response
        return out
    r = _Reader(data)
    r.off = 12
    records: list[tuple[str, int, bytes, int]] = []
    try:
        for _ in range(qd):
            r.name()
            r.off += 4
        for _ in range(an + ns + ar):
            name = r.name()
            rtype = r.u16()
            rclass = r.u16() & 0x7FFF
            _ttl = r.u32()
            rdlength = r.u16()
            if r.off + rdlength > len(data):
                break
            rdata = data[r.off:r.off + rdlength]
            rdata_off = r.off
            r.off += rdlength
            if rclass != CLASS_IN:
                continue
            records.append((name, rtype, rdata, rdata_off))
    except (struct.error, IndexError):
        pass  # keep whatever was parsed before the malformed record

    for name, rtype, rdata, rdata_off in records:
        if rtype == TYPE_PTR:
            # the name inside PTR/SRV may be a compression pointer into the packet
            rr = _Reader(data)
            rr.off = rdata_off
            target = rr.name()
            if name.lower().rstrip(".") == SERVICE:
                out["ptr"].append(target)
        elif rtype == TYPE_SRV:
            _prio, _weight, port = struct.unpack_from(">HHH", rdata, 0) if len(rdata) >= 6 else (0, 0, 0)
            rr = _Reader(data)
            rr.off = rdata_off + 6
            out["srv"][name] = (rr.name(), port)
        elif rtype == TYPE_TXT:
            entries: dict[str, str] = {}
            i = 0
            while i < len(rdata):
                n = rdata[i]
                i += 1
                chunk = rdata[i:i + n]
                i += n
                if b"=" in chunk:
                    k, _, v = chunk.partition(b"=")
                    entries[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
                elif chunk:
                    entries[chunk.decode("utf-8", "replace")] = ""
            out["txt"][name] = entries
        elif rtype == TYPE_A and len(rdata) == 4:
            out["a"][name] = ".".join(str(b) for b in rdata)
    return out


def resolve(records: dict) -> list[dict]:
    """Join PTR/SRV/TXT/A records into service instances."""
    out = []
    for instance in records.get("ptr", []):
        target, port = records.get("srv", {}).get(instance, ("", 0))
        host = records.get("a", {}).get(target, "")
        out.append({
            "instance": instance,
            "host": host,
            "port": port,
            "txt": records.get("txt", {}).get(instance, {}),
        })
    return out


def parse_response(data: bytes) -> list[dict]:
    """Extract PTP/IP service instances from one mDNS response packet."""
    return resolve(parse_records(data))


# --------------------------------------------------------------------------
# anuncio del puente PTP/IP (para que darktable lo descubra solo)
# --------------------------------------------------------------------------
def bridge_guid() -> bytes:
    """GUID que el puente dice ser; libgphoto2 no lo valida."""
    return b"\x00" * 8 + b"omtether"


def _rr(name: str, rtype: int, rdata: bytes, ttl: int = 120,
        cache_flush: bool = True) -> bytes:
    rclass = CLASS_IN | (0x8000 if cache_flush else 0)
    return (_encode_name(name) + struct.pack(">HHIH", rtype, rclass, ttl, len(rdata))
            + rdata)


class Advertiser(threading.Thread):
    """Responde a las consultas `_ptp._tcp.local` con un puerto de este equipo.

    Es lo que hace que darktable (que sólo lista cámaras autodetectadas) vea el
    puente sin configurar nada: su driver de puerto ptpip pregunta por mDNS.
    """

    def __init__(self, port: int, instance: str = "omtether", address: str = "0.0.0.0") -> None:
        super().__init__(daemon=True)
        self.port = port
        self.address = address
        self._child = None
        self.instance = "%s-%04x._ptp._tcp.local" % (instance, random.randint(0, 0xFFFF))
        self.host = "%s.local" % instance
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    def _publish_with_avahi(self) -> bool:
        """Si hay avahi, se publica por su CLI: es quien tiene el puerto 5353."""
        exe = shutil.which("avahi-publish-service")
        if not exe:
            return False
        try:
            self._child = subprocess.Popen(
                [exe, "--no-fail", self.instance.split("._")[0], "_ptp._tcp", str(self.port),
                 "txtvers=1", "vid=33A2", "bridge=1"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        time.sleep(0.4)
        if self._child.poll() is not None:
            log.debug("mDNS: avahi-publish-service terminó (%d)", self._child.returncode)
            self._child = None
            return False
        log.info("mDNS: publicado con avahi como %s (_ptp._tcp, puerto %d)",
                 self.instance.split("._")[0], self.port)
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._child is not None:
            try:
                self._child.terminate()
                self._child.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            self._child = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _records(self) -> bytes:
        txt = b"".join(bytes([len(part)]) + part for part in
                       (b"txtvers=1", b"vid=33A2", b"bridge=1"))
        out = _rr(SERVICE, TYPE_PTR, _encode_name(self.instance), cache_flush=False)
        out += _rr(self.instance, TYPE_SRV,
                   struct.pack(">HHH", 0, 0, self.port) + _encode_name(self.host))
        out += _rr(self.instance, TYPE_TXT, txt)
        out += _rr(self.host, TYPE_A, socket.inet_aton(self.address))
        return out

    def run(self) -> None:
        if self._publish_with_avahi():
            while not self._stop.is_set():
                time.sleep(0.5)
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", MDNS_PORT))
            mreq = struct.pack("4s4s", socket.inet_aton(MDNS_ADDR),
                               socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(0.5)
        except OSError as exc:
            log.warning("mDNS: no se pudo anunciar el puente (%s)", exc)
            return
        self._sock = sock
        log.info("mDNS: anunciando %s -> %s:%d", self.instance, self.address, self.port)
        answer = self._records()
        # un anuncio espontáneo acelera el descubrimiento
        for _ in range(2):
            try:
                sock.sendto(_header(0, 0, 0, 4) + answer, (MDNS_ADDR, MDNS_PORT))
            except OSError:
                pass
            time.sleep(0.2)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                reader = _Reader(data)
                reader.u16()
                flags = reader.u16()
                qd = reader.u16()
                if flags & 0x8000:      # es una respuesta, no una pregunta
                    continue
                wanted = False
                for _ in range(qd):
                    name = reader.name()
                    qtype = reader.u16()
                    reader.u16()
                    if name.lower() in (SERVICE, self.instance.lower()) or qtype in (TYPE_PTR, 255):
                        wanted = True
                if not wanted:
                    continue
                payload = _header(0, 0x8400, 0, 4) + answer
                sock.sendto(payload, (MDNS_ADDR, MDNS_PORT))
                if addr[0] != "0.0.0.0":
                    sock.sendto(payload, (addr[0], addr[1]))
            except Exception as exc:  # noqa: BLE001
                log.debug("mDNS: consulta descartada: %s", exc)


def _header(qid: int, flags: int, qd: int, an: int) -> bytes:
    return struct.pack(">HHHHHH", qid, flags, qd, an, 0, 0)


def _open_mdns_sockets() -> tuple[socket.socket, socket.socket | None]:
    """One socket for unicast replies, one for multicast answers (if possible)."""
    uni = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    uni.bind(("", 0))
    uni.settimeout(0.4)

    mc = None
    try:
        mc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        mc.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                mc.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        mc.bind(("", MDNS_PORT))
        mreq = struct.pack("4s4s", socket.inet_aton(MDNS_ADDR), socket.inet_aton("0.0.0.0"))
        mc.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        mc.settimeout(0.4)
    except OSError as exc:
        log.debug("mDNS: no multicast listener (%s); unicast replies only", exc)
        if mc is not None:
            mc.close()
            mc = None
    return uni, mc


def browse_ptp(timeout: float = 2.0) -> list[dict]:
    """Ask the LAN for `_ptp._tcp` services and return what answers.

    Records are accumulated across packets: cameras often send the PTR in one
    message and the SRV/TXT/A in another.
    """
    import select

    uni, mc = _open_mdns_sockets()
    sockets = [s for s in (uni, mc) if s is not None]
    merged: dict = {"ptr": [], "srv": {}, "txt": {}, "a": {}}
    peer: dict[str, str] = {}
    try:
        uni.sendto(build_query(), (MDNS_ADDR, MDNS_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select(sockets, [], [], 0.3)
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(9000)
                except OSError:
                    continue
                try:
                    rec = parse_records(data)
                except Exception as exc:  # noqa: BLE001 - never die on LAN noise
                    log.debug("mDNS: discarding malformed packet from %s: %s", addr[0], exc)
                    continue
                merged["ptr"].extend(rec["ptr"])
                merged["srv"].update(rec["srv"])
                merged["txt"].update(rec["txt"])
                merged["a"].update(rec["a"])
                for instance in rec["ptr"]:
                    peer[instance] = addr[0]
    finally:
        for sock in sockets:
            sock.close()

    results: dict[str, dict] = {}
    for item in resolve(merged):
        if not item["host"]:
            item["host"] = peer.get(item["instance"], "")
        if item["host"]:
            results[item["instance"] or item["host"]] = item
    log.debug("mDNS: %d _ptp._tcp instances (multicast listener: %s)", len(results), mc is not None)
    return list(results.values())
