"""Cliente de la API HTTP CGI que usa la app móvil OI.Share.

Es el camino "de la app": la cámara crea su propia Wi-Fi (modo punto de acceso),
el PC se une a ella y todo va por HTTP a 192.168.0.10. A diferencia de PTP/IP,
aquí el disparo es repetible sin límites ni modos que se agoten, y el bulb se
controla con starttake/stoptake.

Endpoints (obtenidos del descriptor de la API que lleva la app OI.Share):

  switch_cammode.cgi?mode=rec&lvqty=0640x0480   modo disparo + tamaño de live view
  switch_cammode.cgi?mode=shutter               modo obturador (sin live view)
  exec_shutter.cgi?com=1stpush                  medio pulso (AF)
  exec_shutter.cgi?com=2ndpush                  disparo (con AF ya bloqueado)
  exec_shutter.cgi?com=1st2ndpush               disparo directo
  exec_shutter.cgi?com=2ndrelease               suelta
  exec_takemotion.cgi?com=takeready             preparar bulb
  exec_takemotion.cgi?com=starttake[&exposuremin=N&upperlimit=200]
  exec_takemotion.cgi?com=stoptake
  exec_takemisc.cgi?com=startliveview&port=N    live view por UDP a ese puerto
  exec_takemisc.cgi?com=stopliveview
  get_camprop.cgi?com=get&propname=<name>
  set_camprop.cgi?com=set&propname=<name>&value=<v>
  get_imglist.cgi?DIR=<dir>
  get_thumbnail.cgi?DIR=<path>  /  get_screennail.cgi?DIR=<path>

Las respuestas son texto plano: "ok" cuando va bien.
"""

from __future__ import annotations

import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_HOST = "192.168.0.10"


class CgiError(RuntimeError):
    pass


@dataclass
class Frame:
    data: bytes
    index: int


class Oishare:
    def __init__(self, host: str = DEFAULT_HOST, timeout: float = 10.0) -> None:
        self.base = "http://%s" % host
        self.timeout = timeout
        self._udp: socket.socket | None = None
        self._frames = 0

    # -- HTTP básico ----------------------------------------------------
    def get(self, path: str, timeout: float | None = None) -> str:
        url = self.base + path
        req = urllib.request.Request(url, headers={"User-Agent": "omtether"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.read().decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace").strip()
            raise CgiError("%s -> HTTP %d %s" % (path, exc.code, body[:120])) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise CgiError("%s -> %s" % (path, exc)) from exc

    def get_bytes(self, path: str, timeout: float | None = None) -> bytes:
        url = self.base + path
        req = urllib.request.Request(url, headers={"User-Agent": "omtether"})
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            return resp.read()

    def expect_ok(self, path: str, timeout: float | None = None) -> str:
        body = self.get(path, timeout=timeout)
        if body.lower() not in ("ok", "success", ""):
            raise CgiError("%s -> %r" % (path, body[:200]))
        return body

    # -- modo -----------------------------------------------------------
    def cammode(self, mode: str, lvqty: str | None = None) -> str:
        path = "/switch_cammode.cgi?mode=%s" % mode
        if lvqty:
            path += "&lvqty=%s" % lvqty
        return self.expect_ok(path, timeout=20)

    # -- disparo --------------------------------------------------------
    def focus(self) -> str:
        return self.expect_ok("/exec_shutter.cgi?com=1stpush")

    def release_focus(self) -> str:
        return self.expect_ok("/exec_shutter.cgi?com=1strelease")

    def shoot(self, af: bool = True, settle: float = 0.4) -> str:
        """Dispara. La app pone antes `mode=shutter`; con `af`, medio pulso y disparo."""
        try:
            self.cammode("shutter")
        except CgiError as exc:
            log.debug("switch_cammode(shutter): %s", exc)
        if af:
            self.focus()
            time.sleep(settle)
            return self.expect_ok("/exec_shutter.cgi?com=2ndpush", timeout=30)
        return self.expect_ok("/exec_shutter.cgi?com=1st2ndpush", timeout=30)

    def bulb(self, seconds: float) -> None:
        """Exposición larga: prepara, abre, espera y cierra."""
        self.expect_ok("/exec_takemotion.cgi?com=takeready", timeout=15)
        self.expect_ok("/exec_takemotion.cgi?com=starttake&upperlimit=3600", timeout=20)
        log.info("bulb abierto %.1fs", seconds)
        time.sleep(seconds)
        self.expect_ok("/exec_takemotion.cgi?com=stoptake", timeout=30)

    def stop_take(self) -> str:
        return self.expect_ok("/exec_takemotion.cgi?com=stoptake", timeout=30)

    # -- live view (UDP) ------------------------------------------------
    def liveview_start(self, port: int = 40000) -> None:
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.bind(("0.0.0.0", port))
        self._udp.settimeout(5.0)
        self.expect_ok("/exec_takemisc.cgi?com=startliveview&port=%d" % port, timeout=20)

    def liveview_stop(self) -> None:
        try:
            self.get("/exec_takemisc.cgi?com=stopliveview", timeout=8)
        except CgiError as exc:
            log.debug("stopliveview: %s", exc)
        if self._udp is not None:
            self._udp.close()
            self._udp = None

    def liveview_frame(self, timeout: float = 5.0) -> Frame | None:
        """Cada datagrama UDP trae un JPEG completo (empieza en FF D8)."""
        if self._udp is None:
            raise CgiError("live view no iniciado")
        self._udp.settimeout(timeout)
        try:
            data, _ = self._udp.recvfrom(2 * 1024 * 1024)
        except socket.timeout:
            return None
        start = data.find(b"\xff\xd8")
        end = data.rfind(b"\xff\xd9")
        if start < 0 or end < 0:
            return None
        self._frames += 1
        return Frame(data=data[start:end + 2], index=self._frames)

    # -- propiedades y ficheros ----------------------------------------
    def camprop(self, name: str) -> str:
        return self.get("/get_camprop.cgi?com=get&propname=%s" % urllib.parse.quote(name))

    def set_camprop(self, name: str, value: str) -> str:
        return self.expect_ok("/set_camprop.cgi?com=set&propname=%s&value=%s"
                              % (urllib.parse.quote(name), urllib.parse.quote(value)))

    def imglist(self, directory: str = "/DCIM") -> str:
        return self.get("/get_imglist.cgi?DIR=%s" % urllib.parse.quote(directory), timeout=30)

    def download(self, path: str, outdir: Path) -> Path:
        """Descarga un fichero de la cámara (`DIR` = ruta tipo /DCIM/100OMSYS/x.JPG)."""
        outdir.mkdir(parents=True, exist_ok=True)
        target = outdir / Path(path).name
        data = self.get_bytes("/%s" % path.lstrip("/"), timeout=180)
        part = target.with_suffix(target.suffix + ".part")
        part.write_bytes(data)
        part.replace(target)
        return target
