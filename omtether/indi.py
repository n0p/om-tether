"""Servidor INDI mínimo para que KStars/Ekos dispare la cámara.

No es un driver de indiserver: habla el protocolo INDI directamente por TCP
(puerto 7624 por defecto) y se presenta como un único dispositivo CCD. Es
suficiente para que Ekos lo use como cámara en el módulo Capture.

Propiedades que expone:

  CONNECTION          Conectar / Desconectar (switch)
  DRIVER_INFO         nombre del driver (texto, sólo lectura)
  DEBUG               depuración (switch, opcional)
  CCD_EXPOSURE        segundos de exposición (número, escribe -> dispara)
  CCD_ABORT_EXPOSURE  abortar (switch)
  CCD1                BLOB con el JPEG de la última toma

Flujo de una exposición:

  cliente -> newNumberVector CCD_EXPOSURE = N
  servidor -> setNumberVector CCD_EXPOSURE state=Busy
  (se dispara la cámara y se descarga el JPEG)
  servidor -> setBLOBVector CCD1 (base64)  +  setNumberVector CCD_EXPOSURE state=Ok

El protocolo es XML sobre TCP; los BLOB van en base64 con `enclen` = longitud
del base64, como espera libindi.
"""

from __future__ import annotations

import base64
import logging
import math
import socket
import threading
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

DEFAULT_PORT = 7624

# lista de reserva si aún no hay sesión con la cámara
DEFAULT_ISOS = [100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600]


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class IndiClient(threading.Thread):
    def __init__(self, sock: socket.socket, server: "IndiServer") -> None:
        super().__init__(daemon=True)
        self.sock = sock
        self.server = server
        self.buf = b""
        self.connected = False
        self.abort = threading.Event()
        self._exposing = threading.Event()   # sólo una exposición a la vez
        self._send_lock = threading.Lock()   # el BLOB y las respuestas van en hilos distintos

    # -- envío ----------------------------------------------------------
    def send(self, xml: str) -> None:
        try:
            with self._send_lock:
                self.sock.sendall(xml.encode("utf-8"))
        except OSError:
            self.connected = False

    def send_defs(self) -> None:
        dev = self.server.device
        self.send(
            '<defTextVector device="%s" name="DRIVER_INFO" label="Driver Info" '
            'group="Options" state="Ok" perm="ro">'
            '<defText name="DRIVER_NAME" label="Name">omtether</defText>'
            '<defText name="DRIVER_EXEC" label="Exec">omtether</defText>'
            '<defText name="DRIVER_VERSION" label="Version">1.0</defText>'
            '<defText name="DRIVER_INTERFACE" label="Interface">2</defText>'
            "</defTextVector>" % dev)
        self.send(
            '<defSwitchVector device="%s" name="CONNECTION" label="Connection" '
            'group="Main Control" state="Idle" perm="rw" rule="OneOfMany">'
            '<defSwitch name="CONNECT" label="Connect">Off</defSwitch>'
            '<defSwitch name="DISCONNECT" label="Disconnect">On</defSwitch>'
            "</defSwitchVector>" % dev)
        self.send(
            '<defSwitchVector device="%s" name="DEBUG" label="Debug" '
            'group="Options" state="Idle" perm="rw" rule="OneOfMany">'
            '<defSwitch name="ENABLE" label="Enable">Off</defSwitch>'
            '<defSwitch name="DISABLE" label="Disable">On</defSwitch>'
            "</defSwitchVector>" % dev)
        self.send(
            '<defNumberVector device="%s" name="CCD_EXPOSURE" label="Exposure" '
            'group="Main Control" state="Idle" perm="rw" '
            'timeout="%d" step="0.01" format="%%5.2f">'
            '<defNumber name="CCD_EXPOSURE" label="Exposure (s)" format="%%5.2f" '
            'min="0" max="86400" step="0.01">1</defNumber>'
            "</defNumberVector>" % (dev, 86400 + 60))
        if self.server.set_iso is not None:
            # Ekos busca CCD_ISO como switch OneOfMany (así lo publican los
            # drivers DSLR de libindi), no como número
            values = list(self.server.iso_values or DEFAULT_ISOS)
            current = None
            if self.server.get_iso is not None:
                try:
                    current = int(self.server.get_iso()[0])
                except Exception:  # noqa: BLE001
                    current = None
            if current not in values:
                current = values[len(values) // 2] if values else None
            switches = "".join(
                '<defSwitch name="%d" label="%d">%s</defSwitch>'
                % (iso, iso, "On" if iso == current else "Off") for iso in values)
            self.send(
                '<defSwitchVector device="%s" name="CCD_ISO" label="ISO" '
                'group="Main Control" state="Ok" perm="rw" rule="OneOfMany">'
                '%s</defSwitchVector>' % (dev, switches))
        self.send(
            '<defSwitchVector device="%s" name="CCD_ABORT_EXPOSURE" '
            'label="Abort Exposure" group="Main Control" state="Idle" perm="rw" rule="OneOfMany">'
            '<defSwitch name="ABORT" label="Abort">Off</defSwitch>'
            "</defSwitchVector>" % dev)
        self.send(
            '<defBLOBVector device="%s" name="CCD1" label="Image" group="Main Control" '
            'state="Idle" perm="ro" timeout="60">'
            '<defBLOB name="CCD1" label="Image Data" />'
            "</defBLOBVector>" % dev)

    # -- recepción ------------------------------------------------------
    def run(self) -> None:
        self.connected = True
        try:
            while self.connected:
                data = self.sock.recv(65536)
                if not data:
                    break
                self.buf += data
                self._consume()
        except OSError:
            pass
        finally:
            self.connected = False
            try:
                self.sock.close()
            except OSError:
                pass
            self.server.clients.discard(self)

    def _consume(self) -> None:
        while True:
            start = self.buf.find(b"<")
            if start < 0:
                self.buf = b""
                return
            self.buf = self.buf[start:]
            end = self._message_end(self.buf)
            if end < 0:
                return
            raw, self.buf = self.buf[:end], self.buf[end:]
            try:
                elem = ET.fromstring(raw.decode("utf-8", "replace"))
            except ET.ParseError as exc:
                log.debug("XML inválido: %s (%r)", exc, raw[:120])
                continue
            self.handle(elem)

    @staticmethod
    def _message_end(buf: bytes) -> int:
        """Devuelve la longitud del primer mensaje XML completo, o -1."""
        if buf.startswith(b"<?") or buf.startswith(b"<!") or buf.startswith(b"<getProperties"):
            close = buf.find(b">")
            if close < 0:
                return -1
            if buf[close - 1:close + 1] == b"/>":
                return close + 1
            tag = buf[1:buf.find(b" ")] if b" " in buf[:close] else buf[1:close]
            end = buf.find(b"</" + tag + b">")
            return end + len(tag) + 3 if end >= 0 else -1
        if b" " in buf[:buf.find(b">")]:
            tag = buf[1:buf.find(b" ")]
        else:
            tag = buf[1:buf.find(b">")]
        end = buf.find(b"</" + tag + b">")
        return end + len(tag) + 3 if end >= 0 else -1

    # -- mensajes -------------------------------------------------------
    def handle(self, elem: ET.Element) -> None:
        tag = elem.tag
        dev = elem.get("device", "")
        name = elem.get("name", "")
        if tag == "getProperties":
            self.send_defs()
            return
        if dev and dev != self.server.device:
            return
        if tag == "newSwitchVector" and name == "CONNECTION":
            on = elem.find("oneSwitch[@name='CONNECT']")
            connect = on is not None and (on.text or "").strip().lower() == "on"
            self.send(
                '<setSwitchVector device="%s" name="CONNECTION" state="Ok">'
                '<oneSwitch name="CONNECT">%s</oneSwitch>'
                '<oneSwitch name="DISCONNECT">%s</oneSwitch>'
                "</setSwitchVector>" % (self.server.device,
                                        "On" if connect else "Off",
                                        "Off" if connect else "On"))
        elif tag == "newSwitchVector" and name == "CCD_ABORT_EXPOSURE":
            self.abort.set()
            self.server.abort()
            self.send('<setSwitchVector device="%s" name="CCD_ABORT_EXPOSURE" state="Ok">'
                      '<oneSwitch name="ABORT">On</oneSwitch></setSwitchVector>'
                      % self.server.device)
        elif tag == "newSwitchVector" and name == "CCD_ISO" and self.server.set_iso:
            chosen = None
            for sw in elem.findall("oneSwitch"):
                if (sw.text or "").strip() == "On":
                    try:
                        chosen = int(sw.get("name") or 0)
                    except ValueError:
                        chosen = None
            try:
                if chosen is None:
                    raise ValueError("sin valor")
                self.server.set_iso(chosen)
                state = "Ok"
            except Exception as exc:  # noqa: BLE001
                log.warning("ISO %s rechazado: %s", chosen, exc)
                state = "Alert"
            values = list(self.server.iso_values or DEFAULT_ISOS)
            switches = "".join('<oneSwitch name="%d">%s</oneSwitch>'
                               % (iso, "On" if iso == chosen else "Off") for iso in values)
            self.send('<setSwitchVector device="%s" name="CCD_ISO" state="%s">%s'
                      '</setSwitchVector>' % (self.server.device, state, switches))
        elif tag == "newNumberVector" and name == "CCD_EXPOSURE":
            num = elem.find("oneNumber[@name='CCD_EXPOSURE']")
            try:
                seconds = float((num.text or "0").strip())
            except (AttributeError, ValueError):
                seconds = float("nan")
            if not math.isfinite(seconds) or seconds <= 0:
                self.send('<setNumberVector device="%s" name="CCD_EXPOSURE" state="Alert">'
                          '<oneNumber name="CCD_EXPOSURE">0</oneNumber></setNumberVector>'
                          % self.server.device)
            elif self._exposing.is_set():
                self.send('<setNumberVector device="%s" name="CCD_EXPOSURE" state="Alert">'
                          '<oneNumber name="CCD_EXPOSURE">%.2f</oneNumber></setNumberVector>'
                          % (self.server.device, seconds))
                log.warning("ya hay una exposición en curso")
            else:
                self._exposing.set()
                self.abort.clear()
                threading.Thread(target=self._expose, args=(seconds,),
                                 daemon=True).start()
        elif tag == "newSwitchVector" and name == "DEBUG":
            self.send('<setSwitchVector device="%s" name="DEBUG" state="Ok">'
                      '<oneSwitch name="ENABLE">On</oneSwitch>'
                      '<oneSwitch name="DISABLE">Off</oneSwitch></setSwitchVector>'
                      % self.server.device)

    def _expose(self, seconds: float) -> None:
        try:
            self._expose_inner(seconds)
        finally:
            self._exposing.clear()

    def _expose_inner(self, seconds: float) -> None:
        dev = self.server.device
        self.send('<setNumberVector device="%s" name="CCD_EXPOSURE" state="Busy">'
                  '<oneNumber name="CCD_EXPOSURE">%.2f</oneNumber></setNumberVector>'
                  % (dev, seconds))
        try:
            jpeg = self.server.shoot(seconds, self.abort)
        except Exception as exc:  # noqa: BLE001
            log.warning("exposición fallida: %s", exc)
            self.send('<setNumberVector device="%s" name="CCD_EXPOSURE" state="Alert">'
                      '<oneNumber name="CCD_EXPOSURE">%.2f</oneNumber></setNumberVector>'
                      % (dev, seconds))
            self.send('<setBLOBVector device="%s" name="CCD1" state="Alert">'
                      '<oneBLOB name="CCD1" size="0" format=".jpg" enclen="0"></oneBLOB>'
                      "</setBLOBVector>" % dev)
            return
        data64 = base64.b64encode(jpeg).decode("ascii")
        self.send('<setBLOBVector device="%s" name="CCD1" state="Ok" timeout="60">'
                  '<oneBLOB name="CCD1" size="%d" format=".jpg" enclen="%d">%s</oneBLOB>'
                  "</setBLOBVector>" % (dev, len(jpeg), len(data64), data64))
        self.send('<setNumberVector device="%s" name="CCD_EXPOSURE" state="Ok">'
                  '<oneNumber name="CCD_EXPOSURE">%.2f</oneNumber></setNumberVector>'
                  % (dev, seconds))


class IndiServer(threading.Thread):
    """Servidor INDI de un solo dispositivo CCD."""

    def __init__(self, shoot, port: int = DEFAULT_PORT, device: str = "OMTether",
                 set_iso=None, get_iso=None, iso_limits: tuple[int, int] | None = None,
                 iso_values: list[int] | None = None) -> None:
        super().__init__(daemon=True)
        self.shoot = shoot                # callable(segundos, abort_event) -> bytes JPEG
        self.set_iso = set_iso            # callable(iso) -> None
        self.get_iso = get_iso            # callable() -> (iso, propcode)
        self.iso_limits = iso_limits or (200, 25600)
        self.iso_values = list(iso_values or [])
        self.port = port
        self.device = device
        self.clients: set[IndiClient] = set()
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._abort = threading.Event()

    def abort(self) -> None:
        self._abort.set()

    def run(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(4)
        srv.settimeout(1.0)
        self._sock = srv
        log.info("INDI escuchando en el puerto %d", self.port)
        while not self._stop.is_set():
            try:
                client, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            log.info("cliente INDI desde %s", addr[0])
            c = IndiClient(client, self)
            self.clients.add(c)
            c.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        for c in list(self.clients):
            try:
                c.sock.close()
            except OSError:
                pass
