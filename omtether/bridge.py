"""PTP/IP bridge: expose the camera to libgphoto2 (darktable, digiKam…) as if it
were a local camera.

The camera cannot be used by libgphoto2 out of the box: it wants the initiator
GUID to carry a paired MAC, its init hangs when `0xD052` is written and, in
station mode, it takes the photo without announcing any event. Rather than
patching libgphoto2 on every machine, this module sits in the middle:

    darktable ── gphoto2 ──> 127.0.0.1:15740 (this bridge) ──> camera

The bridge speaks the initiator side of CIPA DC-005 to the camera (with the
right GUID) and the responder side to libgphoto2 (accepting any GUID it sends).
On top of forwarding operations it does three things the camera will not:

  * answers `0xD052` writes without touching the camera (that write is what
    makes the body disappear from the network);
  * after a capture, watches the card and **synthesises** the `0xC103`/`0xC102`
    events libgphoto2 waits for;
  * optionally downloads every new file to a directory, so the same process is
    also the tethering daemon.

Run it with `omtether bridge --host <camera-ip>` and point gphoto2 at
`ptpip:127.0.0.1`. With `--mdns` it also advertises `_ptp._tcp` so applications
that only list auto-detected cameras (darktable) see it without configuration.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import mdns, olympus, ptp
from .ptpip import (
    DATA, END_DATA, EVENT, INIT_COMMAND_ACK, INIT_COMMAND_REQUEST, INIT_EVENT_ACK,
    INIT_EVENT_REQUEST, INIT_FAIL, OPERATION_REQUEST, OPERATION_RESPONSE,
    PROTOCOL_VERSION, START_DATA, PtpIpError, PtpIpTransport,
)

log = logging.getLogger(__name__)

BRIDGE_PORT = 15740
# códigos que no se reenvían tal cual
OC_CAPTURE = olympus.OC_Capture
DPC_CAMERA_CONTROL_MODE = olympus.DPC_CameraControlMode
EVENT_CAPTURE_COMPLETE = 0xC103
EVENT_OBJECT_ADDED_OM = 0xC102


# --------------------------------------------------------------------------
# utilidades de paquete
# --------------------------------------------------------------------------
def recv_packet(sock: socket.socket, timeout: float) -> tuple[int, bytes]:
    sock.settimeout(timeout)
    head = b""
    while len(head) < 8:
        chunk = sock.recv(8 - len(head))
        if not chunk:
            raise PtpIpError("conexión cerrada")
        head += chunk
    length, ptype = struct.unpack("<II", head)
    if length < 8:
        raise PtpIpError("longitud inválida %d" % length)
    body = b""
    while len(body) < length - 8:
        chunk = sock.recv(min(65536, length - 8 - len(body)))
        if not chunk:
            raise PtpIpError("conexión cerrada a media trama")
        body += chunk
    return ptype, body


def send_packet(sock: socket.socket, ptype: int, payload: bytes = b"") -> None:
    sock.sendall(struct.pack("<II", 8 + len(payload), ptype) + payload)


def encode_name(name: str) -> bytes:
    return name.encode("utf-16-le") + b"\x00\x00"


# --------------------------------------------------------------------------
@dataclass
class BridgeConfig:
    host: str
    host_port: int = 15740              # puerto de la cámara (para pruebas con el mock)
    listen_host: str = "127.0.0.1"
    listen_port: int = BRIDGE_PORT
    name: str = "omtether"
    outdir: Path | None = None          # si se indica, descarga cada foto nueva
    download: bool = True
    skip_raw: bool = False               # filtros del volcado, como en el daemon
    skip_jpeg: bool = False
    mdns: bool = False                   # anunciarse para que darktable lo vea
    keepalive: float = 15.0
    event_delay: float = 1.2             # espera antes de mirar la tarjeta
    poll_window: float = 8.0             # cuánto insistir buscando el fichero


@dataclass
class BridgeStats:
    connections: int = 0
    operations: int = 0
    captures: int = 0
    events: int = 0
    downloaded: list[str] = field(default_factory=list)


class Bridge(threading.Thread):
    """Servidor PTP/IP local + cliente PTP/IP de la cámara."""

    def __init__(self, cfg: BridgeConfig) -> None:
        super().__init__(daemon=True)
        self.cfg = cfg
        self.stats = BridgeStats()
        self.session: ptp.PtpSession | None = None
        self.transport: PtpIpTransport | None = None
        self._lock = threading.RLock()      # una operación a la vez en la cámara
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._event_sock: socket.socket | None = None
        self._event_lock = threading.Lock()
        self._advertiser: mdns.Advertiser | None = None
        self._last_busy = 0.0

    # -- cámara ----------------------------------------------------------
    def connect(self) -> None:
        self.transport = PtpIpTransport(self.cfg.host, port=self.cfg.host_port, timeout=15)
        self.transport.connect()
        self.session = ptp.PtpSession(self.transport)
        self.session.open()
        log.info("puente: conectado a la cámara %s", self.cfg.host)

    def close(self) -> None:
        for obj in (self.session, self.transport):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass
        self.session = None
        self.transport = None

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._server, self._event_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self._advertiser is not None:
            self._advertiser.stop()

    # -- bucle del servidor ---------------------------------------------
    def run(self) -> None:
        try:
            self.connect()
        except Exception as exc:  # noqa: BLE001
            log.error("puente: no se pudo conectar con la cámara: %s", exc)
            return
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.cfg.listen_host, self.cfg.listen_port))
        except OSError as exc:
            log.error("puente: no se pudo escuchar en %s:%d (%s)",
                      self.cfg.listen_host, self.cfg.listen_port, exc)
            self.close()
            return
        srv.listen(4)
        srv.settimeout(0.5)
        self._server = srv
        log.info("puente PTP/IP escuchando en %s:%d", self.cfg.listen_host, self.cfg.listen_port)

        if self.cfg.mdns:
            self._advertiser = mdns.Advertiser(self.cfg.listen_port, self.cfg.name)
            self._advertiser.start()

        keepalive = threading.Thread(target=self._keepalive_loop, daemon=True)
        keepalive.start()
        while not self._stop.is_set():
            try:
                conn, peer = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.stats.connections += 1
            log.info("puente: conexión desde %s", peer[0])
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
        try:
            srv.close()
        except OSError:
            pass
        self.close()

    def _keepalive_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(1.0)
            if not self.cfg.keepalive:
                continue
            if time.monotonic() - self._last_busy < self.cfg.keepalive:
                continue
            with self._lock:
                if self.session is None:
                    continue
                try:
                    self.session.op(olympus.OC_TransferModeAlive, [1], check=False)
                    self._last_busy = time.monotonic()
                except Exception:  # noqa: BLE001
                    pass

    # -- una conexión de libgphoto2 -------------------------------------
    def _serve(self, conn: socket.socket) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            ptype, body = recv_packet(conn, 10)
            if ptype == INIT_EVENT_REQUEST:
                self._event_sock = conn
                send_packet(conn, INIT_EVENT_ACK)
                log.info("puente: canal de eventos del cliente abierto")
                while not self._stop.is_set():
                    try:
                        more, _ = recv_packet(conn, 1.0)
                    except socket.timeout:
                        continue
                    except (PtpIpError, OSError):
                        break
                    if more == INIT_EVENT_REQUEST:
                        send_packet(conn, INIT_EVENT_ACK)
                return
            if ptype != INIT_COMMAND_REQUEST:
                log.warning("puente: primer paquete inesperado (%d)", ptype)
                return
            # InitCommandAck: conexión + GUID + nombre + versión
            guid = mdns.bridge_guid()
            send_packet(conn, INIT_COMMAND_ACK,
                        struct.pack("<I", 1) + guid + encode_name(self.cfg.name)
                        + struct.pack("<I", PROTOCOL_VERSION))
            while not self._stop.is_set():
                ptype, body = recv_packet(conn, 0.5 if self._stop.is_set() else 300)
                if ptype == OPERATION_REQUEST:
                    self._operation(conn, body)
                elif ptype in (INIT_EVENT_REQUEST,):
                    send_packet(conn, INIT_EVENT_ACK)
                else:
                    log.debug("puente: paquete %d ignorado", ptype)
        except socket.timeout:
            pass
        except (PtpIpError, OSError) as exc:
            log.debug("puente: conexión terminada (%s)", exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # -- una operación PTP ----------------------------------------------
    def _operation(self, conn: socket.socket, body: bytes) -> None:
        if len(body) < 10:
            return
        dataphase, code, xid = struct.unpack("<IHI", body[:10])
        log.debug("op 0x%04x dataphase=%d xid=%d params=%d raw=%s", code, dataphase, xid,
                  (len(body) - 10) // 4, body[:18].hex(" "))
        n = (len(body) - 10) // 4
        params = list(struct.unpack("<%dI" % n, body[10:10 + 4 * n]))
        self.stats.operations += 1

        # datos que manda el cliente (SetDevicePropValue, SetConnectPcInfo…)
        data_out: bytes | None = None
        if dataphase == 2:
            data_out = self._read_data_in(conn, xid)
            if data_out is None:
                return

        # 0xD052: no se toca la cámara, es lo que la tumba
        if code == 0x1016 and params and params[0] == DPC_CAMERA_CONTROL_MODE:
            log.debug("puente: escritura de 0xD052 interceptada (xid=%d)", xid)
            self._respond(conn, 0x2001, xid)
            return

        before = None
        if code == OC_CAPTURE and params and params[0] == 3:
            before = self._handles()

        try:
            with self._lock:
                if self.session is None:
                    raise PtpIpError("sin sesión con la cámara")
                rc, rparams, data = self.session.op(
                    code, params, data_out=data_out,
                    expect_data=(dataphase == 1), check=False)
                self._last_busy = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            log.warning("puente: operación 0x%04x falló: %s", code, exc)
            self._respond(conn, 0x2001 if isinstance(exc, PtpIpError) else 0x2001, xid)
            return

        log.debug("respuesta 0x%04x (%d bytes de datos)", rc, len(data))
        if data:
            self._send_data_out(conn, xid, data)
        self._respond(conn, rc, xid, rparams)

        if code == OC_CAPTURE and params and params[0] in (3, 6):
            self.stats.captures += 1
            threading.Thread(target=self._after_capture, args=(before,), daemon=True).start()

    def _respond(self, conn: socket.socket, code: int, xid: int,
                 params: list[int] | None = None) -> None:
        payload = struct.pack("<HI", code, xid)
        payload += b"".join(struct.pack("<I", p) for p in (params or []))
        send_packet(conn, OPERATION_RESPONSE, payload)

    def _read_data_in(self, conn: socket.socket, xid: int) -> bytes | None:
        """Lee la fase de datos que manda libgphoto2."""
        buf = bytearray()
        ptype, body = recv_packet(conn, 30)
        if ptype != START_DATA:
            log.debug("puente: se esperaba StartData y llegó %d", ptype)
            return None
        while True:
            ptype, body = recv_packet(conn, 30)
            if ptype == DATA:
                buf.extend(body[4:])
            elif ptype == END_DATA:
                return bytes(buf)
            else:
                log.debug("puente: paquete %d en fase de datos de entrada", ptype)
                return bytes(buf)

    def _send_data_out(self, conn: socket.socket, xid: int, data: bytes) -> None:
        send_packet(conn, START_DATA, struct.pack("<IQ", xid, len(data)))
        off = 0
        while off < len(data):
            chunk = data[off:off + 256 * 1024]
            send_packet(conn, DATA, struct.pack("<I", xid) + chunk)
            off += len(chunk)
        send_packet(conn, END_DATA, struct.pack("<I", xid))

    # -- eventos y descarga ---------------------------------------------
    def _emit(self, code: int, params: list[int]) -> None:
        sock = self._event_sock
        if sock is None:
            return
        with self._event_lock:
            try:
                payload = struct.pack("<HI", code, 0)
                payload += b"".join(struct.pack("<I", p) for p in params)
                send_packet(sock, EVENT, payload)
                self.stats.events += 1
            except OSError:
                self._event_sock = None

    def _handles(self) -> set[int]:
        with self._lock:
            if self.session is None:
                return set()
            try:
                return set(self.session.list_files())
            except Exception:  # noqa: BLE001
                return set()

    def _after_capture(self, before: set[int] | None) -> None:
        """La cámara no anuncia nada: se mira la tarjeta y se fabrican los eventos."""
        time.sleep(self.cfg.event_delay)
        known = before or set()
        deadline = time.monotonic() + self.cfg.poll_window
        while time.monotonic() < deadline and not self._stop.is_set():
            now = self._handles()
            new = sorted(now - known)
            if new:
                handle = new[0]
                log.info("puente: foto nueva 0x%x, se anuncian 0xC103/0xC102", handle)
                self._emit(EVENT_CAPTURE_COMPLETE, [0])
                self._emit(EVENT_OBJECT_ADDED_OM, [handle])
                if self.cfg.download and self.cfg.outdir is not None:
                    self._download(new)
                return
            time.sleep(0.7)
        log.warning("puente: no apareció ningún fichero tras el disparo")

    def _download(self, handles: list[int]) -> None:
        outdir = self.cfg.outdir
        if outdir is None or self.session is None:
            return
        outdir.mkdir(parents=True, exist_ok=True)
        for handle in handles:
            with self._lock:
                try:
                    info = self.session.get_object_info(handle)
                    if info.format == 0x3001:
                        continue
                    if self.cfg.skip_jpeg and info.format != 0x3800:
                        log.info("puente: JPEG omitido (%s)", info.filename)
                        continue
                    if self.cfg.skip_raw and info.format == 0x3800:
                        log.info("puente: ORF omitido (%s)", info.filename)
                        continue
                    target = outdir / Path(info.filename).name
                    if target.exists() and target.stat().st_size == info.size:
                        continue
                    data = self.session.get_object(handle, timeout=180)
                    part = target.with_suffix(target.suffix + ".part")
                    part.write_bytes(data)
                    part.replace(target)
                    self.stats.downloaded.append(str(target))
                    log.info("puente: guardado %s (%.1f MB)", target.name, len(data) / 1e6)
                except Exception as exc:  # noqa: BLE001
                    log.warning("puente: no se pudo bajar 0x%x: %s", handle, exc)
