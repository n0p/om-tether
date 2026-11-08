"""Modo control remoto (astrofotografía): live view, disparo y descarga.

La cámara sólo obedece el disparo remoto (`0x9481`) cuando está en modo control
por PC (`0xD052 = 1`); en modo autónomo contesta OK o `InvalidParameter` y no
genera nada. `Remote.enter()` hace esa transición con reintentos porque la
cámara responde `DeviceBusy` (0x2019) mientras está ocupada (menús, escritura,
standby).

Todo el estado PTP vive en un único hilo: el bucle pide un frame de live view,
atiende el teclado sin bloquear (select) y dispara cuando toca. La cámara no
admite dos sesiones, así que no hay hilos hablando con ella en paralelo.
"""

from __future__ import annotations

import logging
import math
import select
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import mjpeg, olympus, ptp
from .ptpip import PtpIpError, PtpIpTransport

log = logging.getLogger(__name__)


@dataclass
class RemoteConfig:
    host: str
    outdir: Path
    http_port: int | None = None      # servir live view MJPEG en este puerto
    frames: Path | None = None        # guardar los frames de live view
    shots: int = 0                    # 0 = interactivo
    interval: float = 5.0             # segundos entre disparos
    settle: float = 60.0              # espera máxima a que aparezca el fichero
    restore: bool = False             # dejar la cámara en control remoto al salir
    live: bool = False                # live view durante la secuencia (off por defecto)
    skip_raw: bool = False            # no descargar ORF (para apilar basta el JPG)
    skip_jpeg: bool = False           # no descargar JPG (sólo RAW)
    exposure: float = 0.0             # segundos de exposición por toma (0 = la de la cámara)
    iso: int = 0                      # ISO a fijar al conectar (0 = no tocar)
    keepalive: float = 10.0


@dataclass
class RemoteStats:
    shots: int = 0
    files: list[str] = field(default_factory=list)
    frames: int = 0


class Remote:
    def __init__(self, cfg: RemoteConfig) -> None:
        self.cfg = cfg
        self.stats = RemoteStats()
        self.seen: set[int] = set()       # handles ya descargados (evita duplicados)
        self.transport: PtpIpTransport | None = None
        self.session: ptp.PtpSession | None = None
        self.server: mjpeg.MjpegServer | None = None
        self._prev_mode: int | None = None
        self._prev_lv: int | None = None
        self._last_alive = 0.0
        self._frame_index = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # -- ciclo de vida --------------------------------------------------
    def connect(self) -> None:
        self.transport = PtpIpTransport(self.cfg.host, timeout=15)
        self.transport.connect()
        self.session = ptp.PtpSession(self.transport)
        self.session.open()

    def close(self) -> None:
        try:
            if self.session is not None:
                # apagar live view SÓLO si está encendido: escribir el mismo
                # valor (0 sobre 0) cuelga la cámara y la saca de la Wi-Fi
                try:
                    if self.session.get_prop_u32(olympus.DPC_LiveViewModeOM) != 0:
                        self.session.set_prop_u32(olympus.DPC_LiveViewModeOM, 0)
                except Exception as exc:  # noqa: BLE001
                    log.debug("live view al cerrar: %s", exc)
                self.liveview_stop()
                if self.cfg.restore and self._prev_mode is not None:
                    self.set_mode(self._prev_mode)
        except Exception as exc:  # noqa: BLE001
            log.debug("restaurando estado: %s", exc)
        if self.server is not None:
            self.server.stop()
            self.server = None
        for obj in (self.session, self.transport):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass
        self.session = None
        self.transport = None

    # -- modo control por PC --------------------------------------------
    def get_mode(self) -> int:
        assert self.session is not None
        return self.session.get_prop_u16(olympus.DPC_CameraControlMode)

    def set_mode(self, value: int) -> int:
        assert self.session is not None
        rc, _, _ = self.session.op(0x1016, [olympus.DPC_CameraControlMode],
                                   data_out=struct.pack("<H", value), check=False)
        return rc

    def enter(self, timeout: float = 20.0) -> bool:
        """Pasa a control remoto; reintenta mientras la cámara esté ocupada.

        Devuelve False si no lo consigue (la cámara responde `DeviceBusy` cuando
        no está en la pantalla de disparo: rec view tras una foto, menú o
        reproducción). No es fatal: se sigue intentando disparar.
        """
        assert self.session is not None
        self._prev_mode = self.get_mode()
        if self._prev_mode == 1:
            log.info("ya estaba en modo control por PC")
            return True
        # con el live view activo la cámara no cambia de modo, hay que apagarlo
        # antes; pero escribir 0 cuando YA vale 0 la cuelga y la saca de la Wi-Fi
        # varios minutos, así que sólo se escribe si hace falta.
        try:
            current_lv = self.session.get_prop_u32(olympus.DPC_LiveViewModeOM)
            if current_lv != 0:
                self.session.set_prop_u32(olympus.DPC_LiveViewModeOM, 0)
                time.sleep(0.5)
        except ptp.PtpError as exc:
            log.debug("no se pudo leer/apagar live view antes del cambio de modo: %s", exc)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc = self.set_mode(1)
            if rc == ptp.RC_OK:
                time.sleep(0.4)
                if self.get_mode() == 1:
                    log.info("modo control por PC activado")
                    return True
            elif rc not in (0x2019,):     # DeviceBusy
                log.warning("0xD052=1 devolvió 0x%04x", rc)
            time.sleep(1.0)
        print("AVISO: la cámara no acepta control remoto (DeviceBusy).\n"
              "       Suele hacer falta reiniciarla: el modo se concede una vez por\n"
              "       arranque, tras el primer enlace Wi-Fi. Reinicia la cámara,\n"
              "       reconecta la red y vuelve a lanzar esto.",
              flush=True)
        return False

    # -- live view ------------------------------------------------------
    def liveview_start(self) -> None:
        assert self.session is not None
        try:
            self._prev_lv = self.session.get_prop_u32(olympus.DPC_LiveViewModeOM)
        except ptp.PtpError:
            self._prev_lv = None
        # escribir la propiedad cuando ya tiene ese valor también puede colgar
        # la cámara: sólo se escribe si de verdad hay que cambiarla
        if self._prev_lv != 0x04000300:
            self.session.set_prop_u32(olympus.DPC_LiveViewModeOM, 0x04000300)
        if self.get_mode() != 1:
            print("AVISO: el live view ha sacado a la cámara del modo remoto "
                  "(0xD052=%d); el disparo puede fallar" % self.get_mode(), flush=True)
        if self.cfg.frames:
            self.cfg.frames.mkdir(parents=True, exist_ok=True)
        if self.cfg.http_port:
            self.server = mjpeg.MjpegServer(port=self.cfg.http_port)
            print("live view: %s" % self.server.start(), flush=True)

    def liveview_stop(self) -> None:
        if self.session is None:
            return
        if self._prev_lv is not None and self._prev_lv != 0x04000300:
            try:
                self.session.set_prop_u32(olympus.DPC_LiveViewModeOM, self._prev_lv)
            except ptp.PtpError:
                pass

    def liveview_frame(self, timeout: float = 3.0) -> bool:
        """Pide un frame y lo reparte (servidor MJPEG / disco). True si hubo."""
        assert self.session is not None
        rc, _, data = self.session.op(olympus.OC_GetLiveViewImage, [1],
                                      expect_data=True, check=False, timeout=timeout)
        if rc != ptp.RC_OK or not data:
            return False
        frame = olympus.find_jpeg(data)
        if frame is None:
            return False
        self._frame_index += 1
        self.stats.frames += 1
        if self.server is not None:
            self.server.push(frame)
        if self.cfg.frames is not None:
            (self.cfg.frames / ("lv_%05d.jpg" % self._frame_index)).write_bytes(frame)
        return True

    # -- ISO y exposición ----------------------------------------------
    def iso_candidates(self) -> list[tuple[int, int]]:
        """[(propcode, tamaño)] de las propiedades de ISO de este cuerpo."""
        assert self.session is not None
        out: list[tuple[int, int]] = []
        for code, size in ((olympus.DPC_ISO2, 4), (olympus.DPC_ISO, 2)):
            try:
                dtype, _cur, values = self.session.prop_enum(code)
            except ptp.PtpError:
                continue
            if values:
                out.append((code, {4: 2, 6: 4}.get(dtype, size)))
        return out

    def get_iso(self) -> tuple[int, int]:
        """(iso actual, código de propiedad usado)."""
        assert self.session is not None
        for code, size in self.iso_candidates():
            reader = self.session.get_prop_u32 if size == 4 else self.session.get_prop_u16
            try:
                return int(reader(code)), code
            except ptp.PtpError:
                continue
        raise ptp.PtpError(0x200A, "el cuerpo no expone ISO")

    def set_iso(self, iso: int) -> int:
        """Escribe el ISO (valor crudo, como hace libgphoto2). Devuelve el usado."""
        assert self.session is not None
        candidates = self.iso_candidates() or [(olympus.DPC_ISO2, 4), (olympus.DPC_ISO, 2)]
        last = 0
        for code, size in candidates:
            setter = self.set_property
            rc = setter(code, iso, 4 if size == 4 else 2)
            last = rc
            if rc == ptp.RC_OK:
                return code
        raise ptp.PtpError(last or 0x200A, "no se pudo escribir ISO=%d" % iso)

    def iso_range(self) -> tuple[int, int, list[int]]:
        assert self.session is not None
        for code, _size in self.iso_candidates():
            dtype, _cur, values = self.session.prop_enum(code)
            nums = [v for v in values if 0 < v < 0xFFFF0000]
            if nums:
                return min(nums), max(nums), sorted(set(nums))
        return 200, 25600, []

    def shutter_table(self) -> list[tuple[float, int, int]]:
        """[(segundos_efectivos, raw, prioridad)] de la propiedad de velocidad.

        La cámara codifica así (verificado en la OM-1 II):
          fracciones:  (denominador << 16) | 0x000A
          enteros:     (segundos    << 16) | código bajo (0x03..0x0A)
        """
        assert self.session is not None
        _dt, _cur, values = self.session.prop_enum(olympus.DPC_ShutterSpeed)
        out: list[tuple[float, int, int]] = []
        for raw in values:
            high, low = raw >> 16, raw & 0xFFFF
            if high == 0 or low == 0:
                continue
            # el valor es una fracción: segundos = numerador / denominador
            out.append((high / low, raw, 0))
        return out

    def set_shutter(self, seconds: float) -> int:
        """Fija la velocidad de obturación más cercana; devuelve el valor crudo."""
        assert self.session is not None
        if not math.isfinite(seconds) or seconds <= 0:
            raise ptp.PtpError(0x201C, "exposición no válida: %r" % (seconds,))
        table = self.shutter_table()
        if not table:
            raise ptp.PtpError(0x200A, "sin tabla de velocidades")
        best = min(table, key=lambda t: abs(t[0] - seconds))
        rc = self.set_property(olympus.DPC_ShutterSpeed, best[1], 4)
        if rc != ptp.RC_OK:
            raise ptp.PtpError(rc, "no se pudo fijar la velocidad")
        return best[1]

    def shutter_range(self) -> tuple[float, float]:
        table = self.shutter_table()
        if not table:
            return 1 / 8000, 60.0
        return min(t[0] for t in table), max(t[0] for t in table)

    def expose(self, seconds: float, abort=None) -> list[Path]:
        """Exposición: abre el obturador, espera lo pedido y lo cierra.

        Con la cámara en Bulb (dial M) esto da tiempos largos reales; con la
        cámara en otro modo es un disparo normal y `seconds` sólo marca la espera.
        """
        assert self.session is not None
        if not math.isfinite(seconds) or seconds <= 0:
            raise ptp.PtpError(0x201C, "exposición no válida: %r" % (seconds,))
        lo, hi = self.shutter_range()
        timed = seconds > hi or seconds < lo
        if not timed:
            try:
                self.set_shutter(seconds)
            except ptp.PtpError as exc:
                # sin velocidad fiable, mejor cronometrar que disparar a ~0 s
                log.warning("no se pudo fijar la velocidad (%s); se cronometra", exc)
                timed = True
        before = self.files()
        rc, _, _ = self.session.op(olympus.OC_Capture, [3], check=False)
        if rc not in (ptp.RC_OK, 0x2001):
            if self.enter(timeout=20):
                rc, _, _ = self.session.op(olympus.OC_Capture, [3], check=False)
        if rc not in (ptp.RC_OK, 0x2001):
            raise ptp.PtpError(rc, "apertura de exposición")
        hold = seconds if timed else 0.0

        def _aborted() -> bool:
            return self._stop.is_set() or (abort is not None and abort.is_set())

        if hold >= 1.0:
            end = time.monotonic() + hold
            while time.monotonic() < end and not _aborted():
                time.sleep(min(0.5, max(0.0, end - time.monotonic())))
        self.session.op(olympus.OC_Capture, [6], check=False)
        self.stats.shots += 1
        if abort is not None and abort.is_set():
            log.info("exposición abortada")
            return []
        handles = self.wait_objects(before, timeout=max(20.0, seconds + 15))
        return self.download(handles) if handles else []

    # -- disparo --------------------------------------------------------
    def _keepalive(self) -> None:
        assert self.session is not None
        if not self.cfg.keepalive:
            return
        if time.monotonic() - self._last_alive < self.cfg.keepalive:
            return
        self._last_alive = time.monotonic()
        self.session.op(olympus.OC_TransferModeAlive, [1], check=False)

    def files(self) -> set[int]:
        """Handles de ficheros, sin pedir ObjectInfo (la cámara es frágil)."""
        assert self.session is not None
        return set(self.session.list_files())

    def trigger(self) -> int:
        """Pulsa el disparador; devuelve el handle-base para detectar lo nuevo.

        Tras cada foto la cámara entra en revisión y responde `InvalidParameter`
        al siguiente disparo, así que se reentra en modo remoto y se reintenta.
        """
        assert self.session is not None
        # No nos fiamos de 0xD052: el setter devuelve 0x2019 como fallo por
        # defecto y el valor leído no refleja si la cámara está en control. Se
        # dispara y se comprueba si aparece fichero; sólo si no aparece se
        # intenta entrar en modo y repetir.
        rc, _, _ = self.session.op(olympus.OC_Capture, [3], check=False)
        if rc not in (ptp.RC_OK, 0x2001):
            log.debug("disparo rechazado (0x%04x); reentrando en modo remoto", rc)
            if self.enter(timeout=45):
                rc, _, _ = self.session.op(olympus.OC_Capture, [3], check=False)
        if rc not in (ptp.RC_OK, 0x2001):
            raise ptp.PtpError(rc, "0x9481(3) disparo")
        # 6 = soltar el obturador (libgphoto2 cierra así, no con 0). Sin esto la
        # cámara se queda con el disparo pulsado y rechaza el siguiente.
        try:
            self.session.op(olympus.OC_Capture, [6], check=False)
        except Exception as exc:  # noqa: BLE001
            log.debug("release 0x9481(6): %s", exc)
        self.stats.shots += 1
        return rc

    def shoot_with_fallback(self) -> list[Path]:
        """Dispara; si no aparece fichero, entra en modo control y reintenta."""
        before = self.files()
        self.trigger()
        handles = self.wait_objects(before, timeout=12)
        if handles:
            return self.download(handles)
        log.info("sin fichero tras el disparo; entrando en modo control y repitiendo")
        if self.enter(timeout=20):
            before = self.files()
            self.trigger()
            handles = self.wait_objects(before, timeout=25)
            if handles:
                return self.download(handles)
        raise ptp.PtpError(0x2019, "el disparo no generó ningún fichero")

    def wait_objects(self, before: set[int], timeout: float | None = None) -> list[int]:
        """Espera a que aparezcan los ficheros del disparo y estén completos."""
        assert self.session is not None
        announced: list[int] = []
        limit = timeout if timeout is not None else self.cfg.settle
        deadline = time.monotonic() + limit
        # Fase 1: sólo eventos, cero tráfico. Sondear la tarjeta mientras la
        # cámara escribe el RAW la tumba (deja de responder y se cae del Wi-Fi).
        while time.monotonic() < deadline:
            ev = self.transport.get_event(0.3) if self.transport else None
            if ev is None:
                continue
            log.debug("evento %s 0x%04x %s", olympus.event_name(ev.code),
                      ev.code, [hex(p) for p in ev.params])
            if ev.code in (olympus.EC_ObjectAddedOM, olympus.EC_ObjectAdded,
                           ptp.EC_ObjectAdded,
                           olympus.EC_DirectStoreImage) and ev.params and ev.params[0]:
                handle = ev.params[0]
                if handle not in announced:
                    announced.append(handle)
                # el evento llega antes de que el fichero esté completo: se
                # espera un margen generoso sin preguntar nada
                time.sleep(min(6.0, max(2.0, limit / 4)))
                try:
                    if all(self.session.get_object_info(h).size for h in announced):
                        return announced
                except ptp.PtpError:
                    pass
        # Fase 2 (sólo si no hubo evento): una pasada de sondeo
        try:
            new = [h for h in self.files() if h not in before]
        except (PtpIpError, ptp.PtpError):
            new = []
        return list(dict.fromkeys(announced + new))

    def download(self, handles: list[int]) -> list[Path]:
        assert self.session is not None
        saved: list[Path] = []
        for handle in handles:
            try:
                info = self.session.get_object_info(handle)
            except ptp.PtpError as exc:
                log.warning("0x%x sin info: %s", handle, exc)
                continue
            if self.cfg.skip_jpeg and info.format != 0x3800:
                log.info("JPEG omitido: %s", info.filename)
                self.seen.add(handle)      # para que el barrido final no lo repita
                continue
            if self.cfg.skip_raw and info.format == 0x3800:
                log.info("ORF omitido: %s", info.filename)
                self.seen.add(handle)
                continue
            part = self.cfg.outdir / (Path(info.filename).name + ".part")
            target = self.cfg.outdir / Path(info.filename).name
            data = self.session.get_object(handle, timeout=180)
            if len(data) != info.size:
                log.warning("%s: %d de %d bytes, se reintentará",
                            info.filename, len(data), info.size)
                continue
            part.write_bytes(data)
            part.replace(target)
            saved.append(target)
            self.seen.add(handle)
            self.stats.files.append(str(target))
            print("  %s (%.1f MB)" % (target.name, info.size / 1e6), flush=True)
        return saved

    def shoot(self, download: bool = True) -> list[Path]:
        """Disparo suelto: dispara, espera el fichero y lo descarga."""
        if not download:
            before = self.files()
            self.trigger()
            return [Path("handle-0x%x" % h) for h in self.wait_objects(before)]
        return self.shoot_with_fallback()

    def bulb(self, seconds: float) -> list[Path]:
        """Exposición larga: abre, espera y cierra (params 3 y 0)."""
        assert self.session is not None
        before = set(self.files())
        rc, _, _ = self.session.op(olympus.OC_Capture, [3], check=False)
        if rc != ptp.RC_OK:
            raise ptp.PtpError(rc, "apertura de bulb")
        print("  bulb abierto, %.1fs" % seconds, flush=True)
        time.sleep(seconds)
        self.session.op(olympus.OC_Capture, [6], check=False)
        self.stats.shots += 1
        handles = self.wait_objects(before)
        return self.download(handles) if handles else []

    # -- propiedades ----------------------------------------------------
    def set_property(self, code: int, value: int, size: int = 2) -> int:
        assert self.session is not None
        if not isinstance(value, int) or value < 0 or value >= (1 << (8 * size)):
            raise ptp.PtpError(0x201C, "valor %r fuera de rango para %d bytes"
                               % (value, size))
        fmt = "<H" if size == 2 else "<I"
        rc, _, _ = self.session.op(0x1016, [code],
                                   data_out=struct.pack(fmt, value), check=False)
        return rc

    def get_property(self, code: int) -> bytes:
        assert self.session is not None
        rc, _, data = self.session.op(0x1015, [code], check=False)
        return data if rc == ptp.RC_OK else b""

    # -- bucles ---------------------------------------------------------
    def sequence(self) -> RemoteStats:
        """Modo astro no interactivo: N disparos cada `interval` segundos.

        La cámara se cae de la Wi-Fi con frecuencia (y a veces tarda en volver),
        así que cada disparo va en su propio intento: si la sesión muere se
        reconecta, se reentra en modo remoto y se reintenta ese disparo.
        """
        cfg = self.cfg
        cfg.outdir.mkdir(parents=True, exist_ok=True)
        pending: list[int] = []
        baseline: set[int] | None = None
        done = 0
        failures = 0
        backoff = 3.0
        try:
            while done < cfg.shots and not self._stop.is_set():
                try:
                    if self.session is None:
                        self.connect()
                        self.enter(timeout=15)   # best effort: el valor miente
                        if cfg.iso:
                            try:
                                code = self.set_iso(cfg.iso)
                                print("ISO %d (0x%04X)" % (cfg.iso, code), flush=True)
                            except ptp.PtpError as exc:
                                print("aviso: ISO no aceptado (%s)" % exc, flush=True)
                        if baseline is None:
                            baseline = self.files()
                        if cfg.live:
                            self.liveview_start()
                        backoff = 3.0
                    t0 = time.monotonic()
                    print("disparo %d/%d" % (done + 1, cfg.shots), flush=True)
                    before = self.files()
                    if cfg.live:
                        self.liveview_frame()
                    if cfg.exposure and cfg.exposure > 0:
                        # expose() ya abre, espera, cierra y descarga
                        self.expose(cfg.exposure)   # descarga dentro
                        done += 1
                        wait = cfg.interval - (time.monotonic() - t0)
                        while wait > 0:
                            self._keepalive()
                            time.sleep(min(0.2, wait))
                            wait -= 0.2
                        continue
                    self.trigger()
                    handles = self.wait_objects(before, timeout=15)
                    if not handles:
                        # el disparo no dejó fichero: entrar en modo y repetir
                        print("  sin fichero; entrando en modo control y repitiendo", flush=True)
                        if self.enter(timeout=20):
                            before = self.files()
                            self.trigger()
                            handles = self.wait_objects(before, timeout=25)
                    if not handles:
                        raise ptp.PtpError(0x2019, "el disparo no generó fichero")
                    pending.extend(handles)
                    done += 1
                    wait = cfg.interval - (time.monotonic() - t0)
                    while wait > 0:
                        if cfg.live and self.session is not None:
                            try:
                                self.liveview_frame(timeout=2.0)
                            except (PtpIpError, OSError):
                                pass
                        self._keepalive()
                        time.sleep(min(0.2, wait))
                        wait -= 0.2
                except (PtpIpError, ptp.PtpError, OSError) as exc:
                    failures += 1
                    print("  aviso: %s (reintento en %.0fs)" % (exc, backoff), flush=True)
                    self.close()
                    time.sleep(backoff)
                    backoff = min(backoff * 1.6, 20.0)
                    if failures > 40:
                        raise
            if pending:
                print("descargando %d ficheros..." % len(pending), flush=True)
                self.download(pending)
            # barrido final: los RAW se anuncian tarde y alguno se queda fuera
            if baseline is not None:
                time.sleep(3)
                try:
                    rest = [h for h in sorted(self.files())
                            if h not in baseline and h not in pending
                            and h not in self.seen]
                except (PtpIpError, ptp.PtpError) as exc:
                    print("  barrido final no disponible: %s" % exc, flush=True)
                    rest = []
                if rest:
                    print("barrido final: %d ficheros más" % len(rest), flush=True)
                    self.download(rest)
        finally:
            self.close()
        return self.stats

    def interactive(self) -> RemoteStats:
        """Consola de control: s dispara, b bulb, l live view, p propiedad, q sale."""
        self.cfg.outdir.mkdir(parents=True, exist_ok=True)
        self.connect()
        live = True
        try:
            self.enter()
            self.liveview_start()
            print(__doc_help__)
            while True:
                if live:
                    self.liveview_frame(timeout=1.0)
                self._keepalive()
                if not select.select([sys.stdin], [], [], 0.05)[0]:
                    continue
                line = sys.stdin.readline().strip()
                if not line:
                    continue
                cmd, _, arg = line.partition(" ")
                cmd = cmd.lower()
                if cmd in ("q", "quit", "exit"):
                    break
                if cmd in ("s", "shoot"):
                    print("disparando...", flush=True)
                    self.shoot()
                elif cmd == "b":
                    secs = float(arg) if arg else 10.0
                    self.bulb(secs)
                elif cmd == "l":
                    live = not live
                    print("live view %s" % ("on" if live else "off"))
                elif cmd in ("p", "set"):
                    parts = arg.split()
                    if len(parts) != 2:
                        print("uso: p 0xD01C 0x1234")
                        continue
                    code = int(parts[0], 0)
                    value = int(parts[1], 0)
                    rc = self.set_property(code, value, 2 if value <= 0xFFFF else 4)
                    print("0x%04X = 0x%X -> 0x%04X" % (code, value, rc))
                elif cmd == "g":
                    code = int(arg, 0)
                    print("0x%04X = %s" % (code, self.get_property(code).hex()))
                elif cmd == "?":
                    print(__doc_help__)
                else:
                    print("orden desconocida; ? para ayuda")
        finally:
            self.close()
        return self.stats


__doc_help__ = """
órdenes:
  s            disparar y descargar
  b [seg]      exposición bulb (por defecto 10 s)
  l            encender/apagar live view
  p CODE VAL   escribir propiedad (p 0xD01C 0x1234)
  g CODE       leer propiedad
  ?            ayuda
  q            salir
"""
