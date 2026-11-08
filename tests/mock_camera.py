"""A tiny fake PTP/IP camera used to exercise the client without hardware.

It implements just enough of CIPA DC-005 + PTP to answer Init/OpenSession/
GetDeviceInfo and to hand out a synthetic object.
"""

from __future__ import annotations

import socket
import struct
import threading

PTPIP_PORT = 15740


def _send(sock: socket.socket, ptype: int, payload: bytes = b"") -> None:
    sock.sendall(struct.pack("<II", 8 + len(payload), ptype) + payload)


def _recv(sock: socket.socket):
    head = b""
    while len(head) < 8:
        chunk = sock.recv(8 - len(head))
        if not chunk:
            return None, None
        head += chunk
    length, ptype = struct.unpack("<II", head)
    body = b""
    while len(body) < length - 8:
        chunk = sock.recv(length - 8 - len(body))
        if not chunk:
            return None, None
        body += chunk
    return ptype, body


def _str(text: str) -> bytes:
    return bytes([len(text)]) + text.encode("utf-16-le")


def _array16(values) -> bytes:
    return struct.pack("<I", len(values)) + b"".join(struct.pack("<H", v) for v in values)


def _array32(values) -> bytes:
    return struct.pack("<I", len(values)) + b"".join(struct.pack("<I", v) for v in values)


DEVICE_INFO = b"".join([
    struct.pack("<H", 100),
    struct.pack("<I", 0x00000006),
    struct.pack("<H", 100),
    _str("microsoft.com: 1.0; 1.0;"),
    struct.pack("<H", 0),
    _array16([0x1001, 0x1002, 0x1003, 0x1004, 0x1007, 0x1008, 0x1009, 0x9481, 0x9484, 0x9486, 0x94BF]),
    _array16([0x4002, 0x400D, 0xC002]),
    _array16([0xD052, 0xD06D, 0xD0DC, 0xD002]),
    _array16([0x3801]),
    _array16([0x3801, 0x3000]),
    _str("OMSYSTEM"),
    _str("OM-1"),
    _str("1.6"),
    _str("MOCK0001"),
])


class MockCamera(threading.Thread):
    def __init__(self, host: str = "127.0.0.1", port: int = PTPIP_PORT) -> None:
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(4)
        self.running = True
        self.requests: list[int] = []
        self.evt_conn: socket.socket | None = None
        self.objects: dict[int, tuple[str, bytes]] = {}
        self.handles: list[int] = []
        self._next_handle = 0x1000

    def emit_event(self, code: int, *params: int) -> None:
        if self.evt_conn is None:
            return
        payload = struct.pack("<HI", code, 1) + b"".join(struct.pack("<I", p) for p in params)
        try:
            _send(self.evt_conn, 8, payload)
        except OSError:
            pass

    def add_object(self, name: str, data: bytes) -> int:
        self._next_handle += 1
        self.objects[self._next_handle] = (name, data)
        self.handles.append(self._next_handle)
        return self._next_handle

    def run(self) -> None:
        while self.running:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            ptype, body = _recv(conn)
            if ptype == 1:
                threading.Thread(target=self._cmd_loop, args=(conn, body), daemon=True).start()
            elif ptype == 3:
                threading.Thread(target=self._evt_loop, args=(conn,), daemon=True).start()
            else:
                conn.close()

    def _evt_loop(self, conn: socket.socket) -> None:
        self.evt_conn = conn
        _send(conn, 4)
        while self.running:
            if _recv(conn)[0] is None:
                return

    def _cmd_loop(self, conn: socket.socket, _first: bytes) -> None:
        _send(conn, 2, struct.pack("<I", 1) + b"\x11" * 16 + "MOCKCAM".encode("utf-16-le") + b"\x00\x00" + struct.pack("<I", 0x00010000))
        while self.running:
            ptype, body = _recv(conn)
            if ptype is None:
                return
            if ptype != 6:
                continue
            _, code, xid = struct.unpack_from("<IHI", body, 0)
            nparams = (len(body) - 10) // 4
            params = list(struct.unpack_from("<%dI" % nparams, body, 10)) if nparams else []
            self.requests.append(code)
            if code == 0x1001:  # GetDeviceInfo
                _send(conn, 9, struct.pack("<IQ", xid, len(DEVICE_INFO)))
                _send(conn, 10, struct.pack("<I", xid) + DEVICE_INFO)
                _send(conn, 12, struct.pack("<I", xid))
                _send(conn, 7, struct.pack("<HI", 0x2001, xid))
            elif code == 0x1004:  # GetStorageIDs
                ids = struct.pack("<I", 1) + struct.pack("<I", 0x00010001)
                _send(conn, 9, struct.pack("<IQ", xid, len(ids)))
                _send(conn, 10, struct.pack("<I", xid) + ids)
                _send(conn, 12, struct.pack("<I", xid))
                _send(conn, 7, struct.pack("<HI", 0x2001, xid))
            elif code == 0x1005:  # GetStorageInfo
                info = (struct.pack("<HH", 0x0002, 0x0003) + struct.pack("<Q", 64 << 30)
                        + struct.pack("<Q", 32 << 30) + struct.pack("<I", 0)
                        + _str("Mock card") + _str("MOCKVOL") + _str(""))
                _send(conn, 9, struct.pack("<IQ", xid, len(info)))
                _send(conn, 10, struct.pack("<I", xid) + info)
                _send(conn, 12, struct.pack("<I", xid))
                _send(conn, 7, struct.pack("<HI", 0x2001, xid))
            elif code == 0x1015:  # GetDevicePropValue
                value = {0xD052: struct.pack("<H", 2), 0xD06D: struct.pack("<I", 0x04000300), 0xD0DC: struct.pack("<H", 2)}.get(params[0], struct.pack("<H", 0))
                _send(conn, 9, struct.pack("<IQ", xid, len(value)))
                _send(conn, 10, struct.pack("<I", xid) + value)
                _send(conn, 12, struct.pack("<I", xid))
                _send(conn, 7, struct.pack("<HI", 0x2001, xid))
            elif code == 0x9484:  # live view
                jpg = b"\xff\xd8\xff\xe0" + b"MOCKJPEG" * 200 + b"\xff\xd9"
                _send(conn, 9, struct.pack("<IQ", xid, len(jpg)))
                _send(conn, 10, struct.pack("<I", xid) + jpg)
                _send(conn, 12, struct.pack("<I", xid))
                _send(conn, 7, struct.pack("<HI", 0x2001, xid))
            elif code == 0x9481:  # OMD capture: 3 = shutter down, 6 = release
                _send(conn, 7, struct.pack("<HI", 0x2001, xid))
                if params and params[0] == 3:
                    # la cámara real anuncia el fin de la exposición así
                    self.emit_event(0xC103, 0)
                if params and params[0] == 6:
                    jpg = b"\xff\xd8\xff\xe0" + b"MOCKPHOTO" * 500 + b"\xff\xd9"
                    handle = self.add_object("P%07d.JPG" % (self._next_handle + 1), jpg)
                    # eventos viejos y nuevos: el cliente acepta ambos
                    self.emit_event(0xC002, handle)
                    self.emit_event(0xC102, handle)
            elif code == 0x1007:  # GetObjectHandles
                # params = [storage, format, parent]: el mock no tiene carpetas,
                # así que sólo devuelve ficheros cuando no se piden carpetas
                fmt = params[1] if len(params) > 2 else 0
                data = _array32([] if fmt == 0x3001 else self.handles)
                _send(conn, 9, struct.pack("<IQ", xid, len(data)))
                _send(conn, 10, struct.pack("<I", xid) + data)
                _send(conn, 12, struct.pack("<I", xid))
                _send(conn, 7, struct.pack("<HI", 0x2001, xid))
            elif code == 0x1008:  # GetObjectInfo
                info = self.objects.get(params[0])
                if info is None:
                    _send(conn, 7, struct.pack("<HI", 0x2009, xid))
                    return
                name, payload = info
                fmt = 0x3800 if name.upper().endswith(".ORF") else 0x3801
                oi = (struct.pack("<I", 0x00010001) + struct.pack("<H", fmt) + struct.pack("<H", 0)
                      + struct.pack("<I", len(payload)) + struct.pack("<H", 0) + struct.pack("<I", 0)
                      + struct.pack("<I", 0) + struct.pack("<I", 0) + struct.pack("<I", 6000)
                      + struct.pack("<I", 4000) + struct.pack("<I", 8) + struct.pack("<I", 0)
                      + struct.pack("<H", 0) + struct.pack("<I", 0) + struct.pack("<I", 0)
                      + _str(name) + _str("20260101T120000") + _str("20260101T120000") + _str(""))
                _send(conn, 9, struct.pack("<IQ", xid, len(oi)))
                _send(conn, 10, struct.pack("<I", xid) + oi)
                _send(conn, 12, struct.pack("<I", xid))
                _send(conn, 7, struct.pack("<HI", 0x2001, xid))
            elif code == 0x1009:  # GetObject
                info = self.objects.get(params[0])
                if info is None:
                    _send(conn, 7, struct.pack("<HI", 0x2009, xid))
                    return
                payload = info[1]
                _send(conn, 9, struct.pack("<IQ", xid, len(payload)))
                _send(conn, 10, struct.pack("<I", xid) + payload)
                _send(conn, 12, struct.pack("<I", xid))
                _send(conn, 7, struct.pack("<HI", 0x2001, xid))
            else:
                _send(conn, 7, struct.pack("<HI", 0x2001, xid))

    def stop(self) -> None:
        self.running = False
        try:
            self.srv.close()
        except OSError:
            pass
