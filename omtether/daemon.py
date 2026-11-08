"""Daemon de tethering: vuelca en tiempo real las fotos nuevas y las sube.

Diseño:
  * conexión PTP/IP con negociación de GUID (la cámara acepta el GUID derivado
    de cualquiera de sus MAC emparejadas, invertida);
  * eventos (ObjectAdded / DirectStoreImage / ImageRecordFinish) + sondeo
    periódico de GetObjectHandles como red de seguridad;
  * descarga atómica (fichero .part y rename) para que nunca se suba a medias;
  * estado persistente para no volver a bajar lo ya bajado tras un reinicio;
  * subida enchufable: comando de shell por fichero y/o copia a un directorio
    (pensado para una carpeta sincronizada, rclone, rsync, etc.);
  * reconexión con backoff: la cámara se cae de la Wi-Fi, entra en standby o
    reinicia el servidor PTP/IP con frecuencia.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import olympus, ptp
from .ptp import PtpError
from .ptpip import PtpIpError, PtpIpTransport

log = logging.getLogger(__name__)

# centinela: el objeto se ha visto pero no se descarga a propósito (filtro de
# formato). Se marca como visto para no reintentarlo en cada sondeo.
SKIPPED = object()

HANDLE_EVENTS = (
    olympus.EC_ObjectAddedOM,
    olympus.EC_ObjectAdded,
    ptp.EC_ObjectAdded,
    olympus.EC_DirectStoreImage,
    olympus.EC_ImageRecordFinish,
)


@dataclass
class DaemonConfig:
    host: str
    outdir: Path
    poll: float = 2.0
    keepalive: float = 10.0
    settle: float = 3.0        # reintentos si el objeto aún no está escrito
    upload_cmd: str | None = None
    upload_dir: Path | None = None
    state_file: Path | None = None
    once: bool = False
    skip_raw: bool = False
    skip_jpeg: bool = False


@dataclass
class DaemonStats:
    downloaded: int = 0
    uploaded: int = 0
    failures: int = 0
    reconnects: int = 0
    saved: list[str] = field(default_factory=list)


class TetherDaemon:
    def __init__(self, cfg: DaemonConfig) -> None:
        self.cfg = cfg
        self.stats = DaemonStats()
        self.transport: PtpIpTransport | None = None
        self.session: ptp.PtpSession | None = None
        self.seen: set[int] = set()
        self._stop = threading.Event()
        self._last_poll = 0.0
        self._last_alive = 0.0
        self._load_state()

    # -- estado ---------------------------------------------------------
    def _load_state(self) -> None:
        path = self.cfg.state_file
        if not path or not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            self.seen = {int(h) for h in data.get("seen", [])}
            log.info("estado cargado: %d handles ya procesados", len(self.seen))
        except (OSError, ValueError) as exc:
            log.warning("estado ilegible (%s); se empieza de cero", exc)

    def _save_state(self) -> None:
        path = self.cfg.state_file
        if not path:
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seen": sorted(self.seen)}))
        tmp.replace(path)

    # -- conexión -------------------------------------------------------
    def connect(self) -> None:
        self.transport = PtpIpTransport(self.cfg.host, timeout=10)
        self.transport.connect()
        self.session = ptp.PtpSession(self.transport)
        self.session.open()
        log.info("conectado a %s (GUID %s)", self.cfg.host, self.transport.guid.hex())
        try:
            self.session.op(olympus.OC_TransferModeAlive, [1], check=False)
        except ptp.PtpError as exc:
            log.debug("keep-alive inicial rechazado: %s", exc)
        if not self.seen:
            existing = set(self._handles())
            log.info("tarjeta con %d objetos; se ignoran los previos", len(existing))
            self.seen |= existing

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        for closer in (self.session, self.transport):
            if closer is not None:
                try:
                    closer.close()
                except Exception:  # noqa: BLE001
                    pass
        self.session = None
        self.transport = None

    def _handles(self) -> list[int]:
        """Only files: folders (0x3001) have no data phase and must be skipped."""
        assert self.session is not None
        return self.session.list_files()

    # -- transferencia --------------------------------------------------
    def _download(self, handle: int) -> Path | None:
        assert self.session is not None
        cfg = self.cfg
        deadline = time.monotonic() + max(cfg.settle, 1.0) * 5
        info = None
        while time.monotonic() < deadline:
            try:
                info = self.session.get_object_info(handle)
            except ptp.PtpError as exc:
                log.debug("0x%x sin info todavía: %s", handle, exc)
                time.sleep(0.5)
                continue
            if info.size:
                break
            time.sleep(0.5)
        if info is None or not info.size:
            log.warning("0x%x no se escribió a tiempo; se reintentará", handle)
            return None

        if self.cfg.skip_jpeg and info.format != 0x3800:
            log.info("JPEG omitido: %s", info.filename)
            return SKIPPED
        if self.cfg.skip_raw and info.format == 0x3800:
            log.info("ORF omitido: %s", info.filename)
            return SKIPPED
        target = cfg.outdir / Path(info.filename).name
        if target.exists() and target.stat().st_size == info.size:
            log.info("ya estaba: %s", target.name)
            return target
        part = target.with_suffix(target.suffix + ".part")
        data = self.session.get_object(handle, timeout=180)
        if len(data) != info.size:
            log.warning("0x%x tamaño %d != %d; se reintentará", handle, len(data), info.size)
            return None
        part.write_bytes(data)
        if target.exists():
            target.unlink()
        part.replace(target)
        log.info("guardado %s (%.1f KB)", target.name, info.size / 1024)
        self.stats.downloaded += 1
        self.stats.saved.append(str(target))
        return target

    def _upload(self, path: Path) -> None:
        cfg = self.cfg
        if cfg.upload_dir:
            cfg.upload_dir.mkdir(parents=True, exist_ok=True)
            dest = cfg.upload_dir / path.name
            dest.write_bytes(path.read_bytes())
            log.info("copiado a %s", dest)
            self.stats.uploaded += 1
        if cfg.upload_cmd:
            cmd = cfg.upload_cmd.replace("{file}", shlex.quote(str(path)))
            log.info("subiendo con: %s", cmd)
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if res.returncode != 0:
                self.stats.failures += 1
                log.error("subida falló (%d): %s", res.returncode, res.stderr.strip()[:300])
            else:
                self.stats.uploaded += 1
                if res.stdout.strip():
                    log.info("  %s", res.stdout.strip()[:300])

    def _process(self, handle: int) -> None:
        try:
            path = self._download(handle)
        except (PtpError, PtpIpError, OSError) as exc:
            self.stats.failures += 1
            log.warning("no se pudo bajar 0x%x: %s (se reintentará)", handle, exc)
            return
        if path is None:
            return
        if path is not SKIPPED:          # omitido a propósito: no reintentar
            self._upload(path)
        self.seen.add(handle)
        self._save_state()

    # -- bucle ----------------------------------------------------------
    def run(self) -> DaemonStats:
        cfg = self.cfg
        cfg.outdir.mkdir(parents=True, exist_ok=True)
        backoff = 2.0
        while not self._stop.is_set():
            try:
                if self.session is None:
                    self.connect()
                    backoff = 2.0
                assert self.transport is not None and self.session is not None

                if cfg.keepalive and time.monotonic() - self._last_alive > cfg.keepalive:
                    self._last_alive = time.monotonic()
                    self.session.op(olympus.OC_TransferModeAlive, [1], check=False)

                ev = self.transport.get_event(0.3)
                if ev is not None:
                    log.debug("evento %s 0x%04x %s", olympus.event_name(ev.code),
                              ev.code, [hex(p) for p in ev.params])
                    if ev.code in HANDLE_EVENTS and ev.params and ev.params[0]:
                        handle = ev.params[0]
                        if handle not in self.seen:
                            self._process(handle)

                if time.monotonic() - self._last_poll > cfg.poll:
                    self._last_poll = time.monotonic()
                    for handle in self._handles():
                        if handle not in self.seen:
                            self._process(handle)

                if cfg.once and self.stats.downloaded:
                    break
            except (PtpIpError, ptp.PtpError, OSError) as exc:
                self.stats.reconnects += 1
                log.warning("conexión perdida (%s); reintento en %.0fs", exc, backoff)
                self.close()
                time.sleep(backoff)
                backoff = min(backoff * 1.7, 30.0)
            except KeyboardInterrupt:
                log.info("interrumpido")
                break
        self.close()
        return self.stats
