"""PTP/IP (CIPA DC-005) transport, as used by OM System / Olympus cameras.

Windows' "MTP/IP" (WPD) is exactly this: the CIPA PTP/IP binding plus UPnP
for discovery, as Microsoft's WPD team documented ("we used the PTP/IP
binding, then used UPnP for pairing and discovery").

Packet layout (little endian)::

    uint32 length (including this 8 byte header)
    uint32 type
    ...    type specific payload

Two TCP connections are used:
  * command/data channel (initiator = PC drives it)
  * event channel (responder = camera drives it)
"""

from __future__ import annotations

import logging
import os
import queue
import re
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

PTPIP_PORT = 15740

# packet types
INIT_COMMAND_REQUEST = 0x00000001
INIT_COMMAND_ACK = 0x00000002
INIT_EVENT_REQUEST = 0x00000003
INIT_EVENT_ACK = 0x00000004
INIT_FAIL = 0x00000005
OPERATION_REQUEST = 0x00000006
OPERATION_RESPONSE = 0x00000007
EVENT = 0x00000008
START_DATA = 0x00000009
DATA = 0x0000000A
CANCEL_TRANSACTION = 0x0000000B
END_DATA = 0x0000000C
PING = 0x0000000D
PONG = 0x0000000E

# data phase information field
DP_NO_DATA = 1
DP_DATA_OUT = 2  # data goes from initiator to responder
# note: for data-in (responder -> initiator) the field is also 1; the direction
# is implied by the operation code.

PROTOCOL_VERSION = 0x00010000


class PtpIpError(Exception):
    pass


class PtpIpInitError(PtpIpError):
    pass


@dataclass
class PtpIpEvent:
    code: int
    transaction_id: int
    params: list[int] = field(default_factory=list)

    def __str__(self) -> str:
        return "event 0x%04x xid=%d params=%s" % (
            self.code,
            self.transaction_id,
            ",".join("0x%x" % p for p in self.params),
        )


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise PtpIpError("connection closed by peer (wanted %d, got %d)" % (n, len(buf)))
        buf.extend(chunk)
    return bytes(buf)


def _recv_packet(sock: socket.socket, timeout: float | None = None) -> tuple[int, bytes]:
    if timeout is not None:
        sock.settimeout(timeout)
    head = _read_exact(sock, 8)
    length, ptype = struct.unpack("<II", head)
    if length < 8:
        raise PtpIpError("bogus packet length %d" % length)
    payload = _read_exact(sock, length - 8)
    log.debug("<< type=%d len=%d payload=%s", ptype, length, payload[:64].hex())
    return ptype, payload


def _send_packet(sock: socket.socket, ptype: int, payload: bytes = b"") -> None:
    pkt = struct.pack("<II", 8 + len(payload), ptype) + payload
    log.debug(">> type=%d len=%d payload=%s", ptype, len(pkt), payload[:64].hex())
    sock.sendall(pkt)


def _encodestr(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\x00\x00"


def _decodestr(data: bytes, off: int) -> tuple[str, int]:
    end = off
    while end + 1 < len(data):
        if data[end] == 0 and data[end + 1] == 0:
            break
        end += 2
    return data[off:end].decode("utf-16-le", "replace"), end + 2


@dataclass
class PtpIpHandshake:
    connection_number: int
    responder_guid: bytes
    responder_name: str
    protocol_version: int


def primary_macs() -> list[str]:
    """MACs of the 'real' NICs (Ethernet + Wi-Fi), the ones OM Capture reports."""
    import glob

    macs: list[str] = []
    for path in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(path)
        if name == "lo" or name.startswith(("docker", "veth", "virbr", "br-", "vnet", "tap", "tun")):
            continue
        try:
            with open(os.path.join(path, "address")) as fh:
                mac = fh.read().strip()
        except OSError:
            continue
        if mac and mac != "00:00:00:00:00:00":
            macs.append(mac)
    return macs


def default_guid() -> bytes:
    """GUID accepted by the camera: the first MAC byte-reversed in bytes 10..15.

    The firmware compares Init_Command_Request GUID[10..15] against the MAC table
    stored by SetConnectPcInfo, and the writer stores each MAC in reverse byte
    order (dst[5-i] = src[i]); bytes 0..9 are ignored.
    """
    macs = primary_macs()
    if not macs:
        return uuid.uuid5(uuid.NAMESPACE_OID, socket.gethostname()).bytes
    raw = bytes.fromhex(re.sub(r"[^0-9A-Fa-f]", "", macs[0]))
    return b"\x00" * 10 + raw[::-1]


def _guid_candidates(explicit: bytes | None) -> list[bytes]:
    """GUIDs to try: the explicit one, then the reversed MAC of every local NIC."""
    seen: list[bytes] = []
    for guid in ([explicit] if explicit else []) + [
        b"\x00" * 10 + bytes.fromhex(mac.replace(":", ""))[::-1] for mac in primary_macs()
    ]:
        if guid not in seen:
            seen.append(guid)
    return seen


def default_name() -> str:
    """NetBIOS-ish computer name, as OM Capture stores it in the camera."""
    name = socket.gethostname().split(".")[0]
    return name[:15] if name else "omtether"


class PtpIpTransport:
    """PTP/IP client: init handshake, event channel and PTP operation I/O."""

    def __init__(
        self,
        host: str,
        port: int = PTPIP_PORT,
        event_port: int | None = None,
        name: str | None = None,
        guid: bytes | None = None,
        timeout: float = 10.0,
        negotiate: bool | None = None,
    ) -> None:
        self.host = host
        # negotiate=True: probar todas las MACs locales (comportamiento normal).
        # Con guid explícito y negotiate=None se usa sólo ese GUID.
        self.negotiate = (guid is None) if negotiate is None else negotiate
        self.port = port
        self.event_port = event_port or port
        self.name = name or default_name()
        self.guid = guid or default_guid()
        self.timeout = timeout

        self.cmd: socket.socket | None = None
        self.evt: socket.socket | None = None
        self._listener: socket.socket | None = None
        self.handshake: PtpIpHandshake | None = None
        self.transaction = 0

        self._lock = threading.Lock()
        self._events: queue.Queue[PtpIpEvent] = queue.Queue()
        self._evt_thread: threading.Thread | None = None
        self._evt_stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> PtpIpHandshake:
        log.info("connecting to %s:%d (PTP/IP) as %r", self.host, self.port, self.name)
        self._open_listener()
        last_exc: Exception | None = None
        # con un GUID explícito no se prueban otros: quien pregunta quiere saber
        # si ESE GUID vale (si no, "aceptado" para todos sería una mentira)
        for guid in (_guid_candidates(self.guid) if self.negotiate else [self.guid]):
            self.guid = guid
            try:
                self.cmd = socket.create_connection((self.host, self.port), timeout=self.timeout)
                self.cmd.settimeout(self.timeout)
                self.cmd.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._init_command()
                break
            except PtpIpInitError as exc:
                last_exc = exc
                log.info("init rejected with GUID %s (%s); trying the next local MAC",
                         guid.hex(), exc)
                if self.cmd is not None:
                    try:
                        self.cmd.close()
                    except OSError:
                        pass
                    self.cmd = None
            except OSError:
                if self._listener is not None:
                    self._listener.close()
                    self._listener = None
                raise
        else:
            if self._listener is not None:
                self._listener.close()
                self._listener = None
            raise last_exc or PtpIpInitError("no GUID accepted by the camera")

        try:
            self._init_event()
            self._start_event_reader()
        except PtpIpError as exc:
            log.warning("event channel unavailable (%s); continuing without events", exc)
            if self.evt:
                try:
                    self.evt.close()
                except OSError:
                    pass
                self.evt = None
        finally:
            if self._listener is not None:
                try:
                    self._listener.close()
                except OSError:
                    pass
                self._listener = None
        return self.handshake  # type: ignore[return-value]

    def _open_listener(self) -> None:
        """Some responders dial back for the event channel; be ready for both."""
        for port in (self.event_port, 0):
            try:
                ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                ls.bind(("0.0.0.0", port))
                ls.listen(1)
                ls.settimeout(self.timeout)
                self._listener = ls
                log.debug("listening for a camera-initiated event channel on port %d", ls.getsockname()[1])
                return
            except OSError as exc:
                log.debug("cannot listen on port %d: %s", port, exc)
        log.debug("no event listener; only the standard dial-out will be used")

    def _init_command(self) -> None:
        assert self.cmd is not None
        payload = self.guid + _encodestr(self.name) + struct.pack("<I", PROTOCOL_VERSION)
        _send_packet(self.cmd, INIT_COMMAND_REQUEST, payload)
        ptype, data = _recv_packet(self.cmd, self.timeout)
        if ptype == INIT_FAIL:
            code = struct.unpack("<I", data[:4])[0] if len(data) >= 4 else 0
            raise PtpIpInitError("camera refused InitCommandRequest (0x%08x)" % code)
        if ptype != INIT_COMMAND_ACK:
            raise PtpIpInitError("unexpected packet type %d in init phase" % ptype)
        if len(data) < 20:
            raise PtpIpInitError("short InitCommandAck")
        conn, = struct.unpack("<I", data[:4])
        rguid = data[4:20]
        rname, off = _decodestr(data, 20)
        version = struct.unpack("<I", data[off:off + 4])[0] if len(data) >= off + 4 else 0
        self.handshake = PtpIpHandshake(conn, rguid, rname, version)
        log.info("camera: %r connection=%d proto=0x%08x", rname, conn, version)

    def _init_event(self) -> None:
        assert self.cmd is not None and self.handshake is not None
        # standard role split: the initiator (us) opens the event connection
        try:
            log.debug("opening event channel on port %d", self.event_port)
            self.evt = socket.create_connection((self.host, self.event_port), timeout=3.0)
        except OSError as exc:
            log.debug("camera refused the event connection (%s); waiting for it to dial back", exc)
            if self._listener is None:
                raise PtpIpError("no event channel: %s" % exc) from exc
            try:
                self.evt, peer = self._listener.accept()
            except OSError as exc2:
                raise PtpIpError("no event channel: %s" % exc2) from exc2
            log.info("camera opened the event channel from %s", peer)
        self.evt.settimeout(self.timeout)
        self.evt.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _send_packet(self.evt, INIT_EVENT_REQUEST, struct.pack("<I", self.handshake.connection_number))
        ptype, data = _recv_packet(self.evt, self.timeout)
        if ptype == INIT_FAIL:
            code = struct.unpack("<I", data[:4])[0] if len(data) >= 4 else 0
            raise PtpIpInitError("camera refused event channel (0x%08x)" % code)
        if ptype != INIT_EVENT_ACK:
            raise PtpIpInitError("unexpected packet type %d on event channel" % ptype)

    def _start_event_reader(self) -> None:
        assert self.evt is not None
        self.evt.settimeout(None)

        def reader() -> None:
            assert self.evt is not None
            while not self._evt_stop.is_set():
                try:
                    ptype, data = _recv_packet(self.evt)
                except (PtpIpError, OSError) as exc:
                    if not self._evt_stop.is_set():
                        log.debug("event channel closed: %s", exc)
                    return
                if ptype == EVENT and len(data) >= 6:
                    code, xid = struct.unpack("<HI", data[:6])
                    params = list(struct.unpack("<%dI" % ((len(data) - 6) // 4), data[6:6 + 4 * ((len(data) - 6) // 4)]))
                    ev = PtpIpEvent(code, xid, params)
                    log.debug("event %s", ev)
                    self._events.put(ev)
                elif ptype == PING:
                    try:
                        _send_packet(self.evt, PONG)
                    except OSError:
                        return
                else:
                    log.debug("ignoring packet type %d on event channel", ptype)

        self._evt_thread = threading.Thread(target=reader, name="ptpip-events", daemon=True)
        self._evt_thread.start()

    def close(self) -> None:
        self._evt_stop.set()
        for sock in (self.evt, self.cmd):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self.evt = None
        self.cmd = None

    def __enter__(self) -> "PtpIpTransport":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- events ------------------------------------------------------------
    def get_event(self, timeout: float | None = None) -> PtpIpEvent | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain_events(self) -> list[PtpIpEvent]:
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    # -- operations --------------------------------------------------------
    def next_transaction(self) -> int:
        self.transaction = (self.transaction + 1) & 0xFFFFFFFF
        if self.transaction == 0:
            self.transaction = 1
        return self.transaction

    def operation(
        self,
        code: int,
        params: list[int] | None = None,
        *,
        data_out: bytes | None = None,
        expect_data: bool = False,
        timeout: float | None = None,
    ) -> tuple[int, list[int], bytes]:
        """Run one PTP operation.

        Returns (response_code, response_params, data).
        """
        params = list(params or [])
        assert self.cmd is not None, "not connected"
        with self._lock:
            xid = self.next_transaction()
            dataphase = DP_DATA_OUT if data_out is not None else DP_NO_DATA
            payload = struct.pack("<IHI", dataphase, code, xid)
            payload += b"".join(struct.pack("<I", p) for p in params)
            _send_packet(self.cmd, OPERATION_REQUEST, payload)

            if data_out is not None:
                self._send_data(xid, data_out)

            data = b""
            while True:
                ptype, body = _recv_packet(self.cmd, timeout or self.timeout)
                if ptype == OPERATION_RESPONSE:
                    if len(body) < 6:
                        raise PtpIpError("short operation response")
                    rc, rxid = struct.unpack("<HI", body[:6])
                    n = (len(body) - 6) // 4
                    rparams = list(struct.unpack("<%dI" % n, body[6:6 + 4 * n]))
                    if rxid != xid:
                        log.debug("response for xid %d while expecting %d", rxid, xid)
                    return rc, rparams, data
                if ptype == START_DATA:
                    dxid, total = struct.unpack("<IQ", body[:12])
                    log.debug("start data xid=%d size=%d", dxid, total)
                    chunks = bytearray()
                    while True:
                        itype, ibody = _recv_packet(self.cmd, timeout or self.timeout)
                        if itype == DATA:
                            chunks.extend(ibody[4:])
                        elif itype == END_DATA:
                            break
                        elif itype == OPERATION_RESPONSE:
                            # some cameras end the data phase with the response
                            rc, rxid = struct.unpack("<HI", ibody[:6])
                            n = (len(ibody) - 6) // 4
                            rparams = list(struct.unpack("<%dI" % n, ibody[6:6 + 4 * n]))
                            return rc, rparams, bytes(chunks)
                        else:
                            raise PtpIpError("unexpected packet %d during data phase" % itype)
                    data = bytes(chunks)
                    continue
                if ptype == DATA:
                    data += body[4:]
                    continue
                if ptype == END_DATA:
                    continue
                if ptype == EVENT:
                    code_ev, xid_ev = struct.unpack("<HI", body[:6])
                    self._events.put(PtpIpEvent(code_ev, xid_ev, list(struct.unpack("<%dI" % ((len(body) - 6) // 4), body[6:]))))
                    continue
                raise PtpIpError("unexpected packet type %d" % ptype)

    def _send_data(self, xid: int, data: bytes) -> None:
        assert self.cmd is not None
        _send_packet(self.cmd, START_DATA, struct.pack("<IQ", xid, len(data)))
        off = 0
        chunk_size = 256 * 1024
        while off < len(data):
            chunk = data[off:off + chunk_size]
            _send_packet(self.cmd, DATA, struct.pack("<I", xid) + chunk)
            off += len(chunk)
        _send_packet(self.cmd, END_DATA, struct.pack("<I", xid))
