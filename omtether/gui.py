"""Aplicación de escritorio (PyQt6) para el tethering de la OM-1.

Tres pestañas:

  1. Tether      — volcado continuo de lo que se dispare en el cuerpo, con
                   directorio de destino y formato (JPEG+RAW o sólo JPEG).
  2. Control remoto — live view, disparo y bulb desde el PC.
  3. Astrofotografía — serie de N disparos con intervalo y descarga, más un
                   servicio INDI opcional para que KStars/Ekos mande las tomas.

Todo el tráfico con la cámara va en hilos propios: la interfaz nunca se queda
bloqueada esperando a la cámara (que además es lenta y se cae).

Se lanza con el Python del sistema (necesita PyQt6):

    /usr/bin/python3 -m omtether gui
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QSettings, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit,
    QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from . import discovery, olympus, ptp
from .gui_setup import OnboardDialog, ScanDialog
from .bridge import Bridge, BridgeConfig
from .daemon import DaemonConfig, TetherDaemon
from .i18n import LANGUAGES, detect_default_language, get_language, set_language, tr
from .indi import DEFAULT_PORT as INDI_PORT
from .indi import IndiServer
from .remote import Remote, RemoteConfig

log = logging.getLogger(__name__)

DEFAULT_HOST = ""          # sin IP por defecto: la cámara está en la red de cada uno
# (clave i18n de la etiqueta, no bajar ORF, no bajar JPEG)
FORMATS = [
    ("fmt.jpeg_raw", False, False),
    ("fmt.jpeg_only", True, False),
    ("fmt.raw_only", False, True),
]


def fmt_combo(default: int):
    combo = QComboBox()
    combo.addItems([tr(f[0]) for f in FORMATS])
    combo.setCurrentIndex(default)
    return combo


def fmt_flags(combo) -> tuple[bool, bool]:
    idx = max(0, min(combo.currentIndex(), len(FORMATS) - 1))
    return FORMATS[idx][1], FORMATS[idx][2]


PICTURES = Path.home() / "Pictures"
STATE_DIR = Path.home() / ".local/state/omtether"


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------
class LogBus(QObject):
    """Canal único para que la librería escriba en el panel de la pestaña."""

    message = pyqtSignal(str)


class QtLogHandler(logging.Handler):
    def __init__(self, bus: LogBus) -> None:
        super().__init__()
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bus.message.emit("%s %s" % (record.levelname[0], record.getMessage()))
        except RuntimeError:
            pass


class LogPane(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.setStyleSheet("font-family: monospace; font-size: 11px;")

    def add(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.appendPlainText("%s  %s" % (stamp, text))


def hline(*widgets) -> QHBoxLayout:
    lay = QHBoxLayout()
    for w in widgets:
        if isinstance(w, str):
            lay.addWidget(QLabel(w))
        else:
            lay.addWidget(w)
    return lay


def path_row(line: QLineEdit) -> QHBoxLayout:
    btn = QPushButton("…")

    def browse() -> None:
        chosen = QFileDialog.getExistingDirectory(None, tr("dialog.choose_dir"), line.text())
        if chosen:
            line.setText(chosen)

    btn.clicked.connect(browse)
    btn.setFixedWidth(32)
    return hline(line, btn)


# --------------------------------------------------------------------------
# 1. Tether: volcado continuo
# --------------------------------------------------------------------------
class TetherTab(QWidget):
    def __init__(self, host_edit: QLineEdit) -> None:
        super().__init__()
        self.daemon: TetherDaemon | None = None
        self.bridge: Bridge | None = None
        self.thread: threading.Thread | None = None

        self.host = host_edit
        self.outdir = QLineEdit(str(PICTURES / "omtether"))
        self.fmt = fmt_combo(0)
        self.poll = QDoubleSpinBox()
        self.poll.setRange(0.5, 30.0)
        self.poll.setValue(2.0)
        self.poll.setSuffix(" s")
        self.button = QPushButton(tr("btn.tether_start"))
        self.button.setCheckable(True)
        self.state = QLabel(tr("state.stopped"))
        self.log = LogPane()

        self.bridge_on = QCheckBox(tr("tether.bridge"))
        self.bridge_port = QSpinBox()
        self.bridge_port.setRange(1024, 65535)
        self.bridge_port.setValue(15740)
        self.bridge_on.toggled.connect(self.bridge_port.setEnabled)

        form = QFormLayout()
        form.addRow(tr("label.directory"), path_row(self.outdir))
        form.addRow(tr("label.format"), self.fmt)
        form.addRow(tr("label.poll"), self.poll)
        form.addRow("", self.bridge_on)
        form.addRow(tr("tether.bridge_port"), self.bridge_port)

        box = QGroupBox(tr("tether.group"))
        box.setLayout(form)

        lay = QVBoxLayout(self)
        lay.addWidget(box)
        lay.addLayout(hline(self.button, self.state))
        lay.addWidget(QLabel(tr("label.log")))
        lay.addWidget(self.log, 1)
        self.button.clicked.connect(self.toggle)

    def toggle(self) -> None:
        if self.daemon is None and self.bridge is None:
            self.start()
        else:
            self.stop()

    def start(self) -> None:
        if self.bridge_on.isChecked():
            self.start_bridge()
            return
        cfg = DaemonConfig(
            host=self.host.text().strip(),
            outdir=Path(self.outdir.text()).expanduser(),
            poll=self.poll.value(),
            skip_raw=fmt_flags(self.fmt)[0],
            skip_jpeg=fmt_flags(self.fmt)[1],
            state_file=STATE_DIR / "daemon.json",
        )
        cfg.outdir.mkdir(parents=True, exist_ok=True)
        self.daemon = TetherDaemon(cfg)
        d = self.daemon
        self.thread = threading.Thread(target=self._run, args=(d,), daemon=True)
        self.thread.start()
        self.button.setText(tr("btn.tether_stop"))
        self.state.setText(tr("tether.state_running", dir=cfg.outdir))
        self.log.add(tr("tether.log_started", dir=cfg.outdir))

    def start_bridge(self) -> None:
        """Puente PTP/IP: la cámara se expone a darktable/digiKam y sigue volcando."""
        outdir = Path(self.outdir.text()).expanduser()
        outdir.mkdir(parents=True, exist_ok=True)
        cfg = BridgeConfig(
            host=self.host.text().strip(),
            outdir=outdir,
            listen_port=self.bridge_port.value(),
            mdns=True,
            skip_raw=fmt_flags(self.fmt)[0],
            skip_jpeg=fmt_flags(self.fmt)[1],
        )
        self.bridge = Bridge(cfg)
        self.bridge.start()
        self.button.setText(tr("btn.tether_stop"))
        self.state.setText(tr("tether.state_bridge", port=cfg.listen_port))
        self.log.add(tr("tether.log_bridge", port=cfg.listen_port))
        self.log.add(tr("tether.log_bridge_hint"))
        self.poll.setEnabled(False)
        self.bridge_port.setEnabled(False)

    def _run(self, d: TetherDaemon) -> None:
        try:
            stats = d.run()
        except Exception as exc:  # noqa: BLE001
            self.log.add(tr("log.error", error=exc))
            return
        self.log.add(tr("tether.log_stopped", downloaded=stats.downloaded,
                        uploaded=stats.uploaded, reconnects=stats.reconnects))

    def stop(self) -> None:
        if self.daemon is not None:
            self.daemon.stop()
            self.daemon = None
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge = None
            self.log.add(tr("tether.log_bridge_stopped"))
        self.poll.setEnabled(True)
        self.bridge_port.setEnabled(not self.bridge_on.isChecked())
        self.button.setText(tr("btn.tether_start"))
        self.state.setText(tr("state.stopped"))
        self.log.add(tr("tether.log_stopping"))


# --------------------------------------------------------------------------
# 2. Control remoto
# --------------------------------------------------------------------------
class RemoteWorker(QThread):
    frame = pyqtSignal(QImage)
    status = pyqtSignal(str)
    message = pyqtSignal(str)

    def __init__(self, cfg: RemoteConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.commands: "queue.Queue[tuple[str, float]]" = queue.Queue()
        self.remote: Remote | None = None
        self._alive = True

    def send(self, cmd: str, arg: float = 0.0) -> None:
        self.commands.put((cmd, arg))

    def run(self) -> None:
        r = Remote(self.cfg)
        self.remote = r
        try:
            self.status.emit(tr("state.connecting"))
            r.connect()
            r.enter(timeout=10)
            r.liveview_start()
            self.status.emit(tr("state.connected"))
        except Exception as exc:  # noqa: BLE001
            self.message.emit(tr("remote.log_connect_failed", error=exc))
            self.status.emit(tr("state.disconnected"))
            return
        try:
            while self._alive:
                try:
                    cmd, arg = self.commands.get_nowait()
                except queue.Empty:
                    cmd = ""
                if cmd == "stop":
                    break
                if cmd == "shoot":
                    try:
                        self.status.emit(tr("state.shooting"))
                        files = r.shoot()
                        self.message.emit(tr("remote.log_downloaded",
                                             files=", ".join(p.name for p in files)))
                        self.status.emit(tr("state.connected"))
                    except Exception as exc:  # noqa: BLE001
                        self.message.emit(tr("remote.log_shoot_error", error=exc))
                        self.status.emit(tr("state.error"))
                    continue
                if cmd == "bulb":
                    try:
                        self.status.emit(tr("state.bulb", seconds=arg))
                        files = r.bulb(arg)
                        self.message.emit(tr("remote.log_bulb_done",
                                             files=", ".join(p.name for p in files)))
                        self.status.emit(tr("state.connected"))
                    except Exception as exc:  # noqa: BLE001
                        self.message.emit(tr("remote.log_bulb_error", error=exc))
                    continue
                if not self._alive:
                    break
                try:
                    rc, _, data = r.session.op(olympus.OC_GetLiveViewImage, [1],
                                               expect_data=True, check=False, timeout=2.0)
                except Exception:  # noqa: BLE001
                    time.sleep(0.2)
                    continue
                if rc != ptp.RC_OK or not data:
                    time.sleep(0.1)
                    continue
                jpg = olympus.find_jpeg(data)
                if jpg:
                    img = QImage.fromData(jpg)
                    if not img.isNull():
                        self.frame.emit(img)
                time.sleep(0.05)
        finally:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass
            self.status.emit(tr("state.disconnected"))

    def stop(self) -> None:
        self._alive = False
        self.commands.put(("stop", 0))


class RemoteTab(QWidget):
    def __init__(self, host_edit: QLineEdit) -> None:
        super().__init__()
        self.worker: RemoteWorker | None = None

        self.host = host_edit
        self.outdir = QLineEdit(str(PICTURES / "omtether"))
        self.connect_btn = QPushButton(tr("btn.connect"))
        self.connect_btn.setCheckable(True)
        self.shoot_btn = QPushButton(tr("btn.shoot"))
        self.bulb_spin = QDoubleSpinBox()
        self.bulb_spin.setRange(0.5, 3600)
        self.bulb_spin.setValue(10)
        self.bulb_spin.setSuffix(" s")
        self.bulb_btn = QPushButton(tr("btn.bulb"))
        self.preview = QLabel(tr("remote.no_liveview"))
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(640, 480)
        self.preview.setStyleSheet("background:#111; color:#888;")
        self.status = QLabel(tr("state.disconnected"))
        self.log = LogPane()

        form = QFormLayout()
        form.addRow(tr("label.directory"), path_row(self.outdir))
        box = QGroupBox(tr("remote.group"))
        box.setLayout(form)

        lay = QVBoxLayout(self)
        lay.addWidget(box)
        lay.addLayout(hline(self.connect_btn, self.status))
        lay.addWidget(self.preview, 1)
        lay.addLayout(hline(self.shoot_btn, tr("label.bulb"), self.bulb_spin, self.bulb_btn))
        lay.addWidget(QLabel(tr("label.log")))
        lay.addWidget(self.log, 1)

        self.connect_btn.clicked.connect(self.toggle)
        self.shoot_btn.clicked.connect(lambda: self._send("shoot"))
        self.bulb_btn.clicked.connect(lambda: self._send("bulb", self.bulb_spin.value()))

    def toggle(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(3000)
            self.worker = None
            self.connect_btn.setText(tr("btn.connect"))
            self.preview.setText(tr("remote.no_liveview"))
            return
        cfg = RemoteConfig(
            host=self.host.text().strip(),
            outdir=Path(self.outdir.text()).expanduser(),
            live=True,
        )
        cfg.outdir.mkdir(parents=True, exist_ok=True)
        self.worker = RemoteWorker(cfg)
        self.worker.frame.connect(self._show_frame)
        self.worker.status.connect(self.status.setText)
        self.worker.message.connect(self.log.add)
        self.worker.start()
        self.connect_btn.setText(tr("btn.disconnect"))

    def _send(self, cmd: str, arg: float = 0.0) -> None:
        if self.worker is None:
            self.log.add(tr("remote.log_connect_first"))
            return
        self.worker.send(cmd, arg)

    def _show_frame(self, img: QImage) -> None:
        self.preview.setPixmap(QPixmap.fromImage(img).scaled(
            self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))


# --------------------------------------------------------------------------
# 3. Astrofotografía
# --------------------------------------------------------------------------
class AstroWorker(QThread):
    status = pyqtSignal(str)
    message = pyqtSignal(str)
    progress = pyqtSignal(int, int)

    def __init__(self, cfg: RemoteConfig, indi_port: int | None) -> None:
        super().__init__()
        self.cfg = cfg
        self.indi_port = indi_port
        self.remote: Remote | None = None
        self.server: IndiServer | None = None
        self._lock = threading.Lock()

    # el servicio INDI usa la misma sesión, con cerrojo
    def _indi_shoot(self, seconds: float, abort: threading.Event) -> bytes:
        with self._lock:
            r = self.remote
            if r is None:
                raise RuntimeError(tr("error.not_connected"))
            # conexión perezosa: la primera exposición de KStars abre la sesión
            if r.session is None:
                r.connect()
                r.enter(timeout=10)
                if self.cfg.iso:
                    r.set_iso(self.cfg.iso)
            files = r.expose(seconds, abort)
            jpg = next((p for p in files if p.suffix.lower() in (".jpg", ".jpeg")), None)
            if jpg is None:
                raise RuntimeError(tr("error.no_jpeg"))
            return jpg.read_bytes()

    def run(self) -> None:
        r = Remote(self.cfg)
        self.remote = r
        try:
            if self.indi_port:
                self.status.emit(tr("astro.state_indi", port=self.indi_port))
                self.server = IndiServer(self._indi_shoot, port=self.indi_port,
                                        set_iso=r.set_iso, get_iso=r.get_iso)
                self.server.start()
                self.message.emit(tr("astro.log_indi_listening", port=self.indi_port))
                # se mantiene la sesión viva y se atienden las peticiones INDI
                while not r._stop.is_set():
                    time.sleep(0.5)
                return
            r.connect()
            r.enter(timeout=10)
            self.status.emit(tr("astro.state_running"))
            stats = r.sequence()
            self.message.emit(tr("astro.log_done", shots=stats.shots, files=len(stats.files)))
            self.status.emit(tr("state.finished"))
        except Exception as exc:  # noqa: BLE001
            self.message.emit(tr("log.error", error=exc))
            self.status.emit(tr("state.error"))
        finally:
            if self.server is not None:
                self.server.stop()
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        if self.remote is not None:
            self.remote.stop()
        if self.server is not None:
            self.server.stop()


class AstroTab(QWidget):
    def __init__(self, host_edit: QLineEdit) -> None:
        super().__init__()
        self.worker: AstroWorker | None = None

        self.host = host_edit
        self.outdir = QLineEdit(str(PICTURES / "astro"))
        self.shots = QSpinBox()
        self.shots.setRange(1, 9999)
        self.shots.setValue(30)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.5, 3600)
        self.interval.setValue(5)
        self.interval.setSuffix(" s")
        self.fmt = fmt_combo(1)
        self.iso = QSpinBox()
        self.iso.setRange(0, 25600)
        self.iso.setSingleStep(100)
        self.iso.setValue(0)
        self.iso.setSpecialValueText(tr("astro.iso_untouched"))
        self.exposure = QDoubleSpinBox()
        self.exposure.setRange(0, 7200)
        self.exposure.setDecimals(1)
        self.exposure.setSuffix(" s")
        self.exposure.setValue(0)
        self.exposure.setSpecialValueText(tr("astro.exposure_camera"))
        self.indi = QCheckBox(tr("astro.indi_enable"))
        self.indi_port = QSpinBox()
        self.indi_port.setRange(1024, 65535)
        self.indi_port.setValue(INDI_PORT)
        self.button = QPushButton(tr("btn.astro_start"))
        self.button.setCheckable(True)
        self.status = QLabel(tr("state.stopped"))
        self.log = LogPane()

        form = QFormLayout()
        form.addRow(tr("label.directory"), path_row(self.outdir))
        form.addRow(tr("astro.shots"), self.shots)
        form.addRow(tr("astro.interval"), self.interval)
        form.addRow(tr("label.format"), self.fmt)
        form.addRow(tr("astro.iso"), self.iso)
        form.addRow(tr("astro.exposure"), self.exposure)

        box = QGroupBox(tr("astro.group"))
        box.setLayout(form)

        indi_box = QGroupBox(tr("astro.indi_group"))
        indi_lay = QVBoxLayout()
        indi_lay.addWidget(self.indi)
        indi_lay.addLayout(hline(self.indi_port, tr("astro.indi_port_hint")))
        indi_box.setLayout(indi_lay)

        lay = QVBoxLayout(self)
        lay.addWidget(box)
        lay.addWidget(indi_box)
        lay.addLayout(hline(self.button, self.status))
        lay.addWidget(QLabel(tr("label.log")))
        lay.addWidget(self.log, 1)
        self.button.clicked.connect(self.toggle)
        self.indi.toggled.connect(self._indi_changed)

    def _indi_changed(self, on: bool) -> None:
        self.shots.setEnabled(not on)
        self.interval.setEnabled(not on)
        self.log.add(tr("astro.log_indi_on") if on else tr("astro.log_indi_off"))

    def toggle(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(3000)
            self.worker = None
            self.button.setText(tr("btn.astro_start"))
            self.status.setText(tr("state.stopped"))
            return
        cfg = RemoteConfig(
            host=self.host.text().strip(),
            outdir=Path(self.outdir.text()).expanduser(),
            shots=self.shots.value(),
            interval=self.interval.value(),
            skip_raw=fmt_flags(self.fmt)[0],
            skip_jpeg=fmt_flags(self.fmt)[1],
            live=False,
            iso=self.iso.value(),
            exposure=self.exposure.value(),
        )
        cfg.outdir.mkdir(parents=True, exist_ok=True)
        port = self.indi_port.value() if self.indi.isChecked() else None
        self.worker = AstroWorker(cfg, port)
        self.worker.status.connect(self.status.setText)
        self.worker.message.connect(self.log.add)
        self.worker.start()
        self.button.setText(tr("btn.stop"))
        self.log.add(tr("astro.log_start", shots=cfg.shots, interval=cfg.interval,
                        dir=cfg.outdir))


# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    probe_result = pyqtSignal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(tr("app.title"))
        self.resize(980, 820)

        self.settings = QSettings("omtether", "gui")
        self.host = QLineEdit(self.settings.value("host", DEFAULT_HOST) or DEFAULT_HOST)
        self.host.setMinimumWidth(150)
        self.host.setPlaceholderText(tr("label.camera_placeholder"))
        self.host.textChanged.connect(
            lambda text: self.settings.setValue("host", text.strip()))

        self.btn_scan = QPushButton(tr("btn.scan"))
        self.btn_scan.clicked.connect(self.scan_network)
        self.btn_onboard = QPushButton(tr("btn.onboard"))
        self.btn_onboard.clicked.connect(self.onboard)
        self.btn_probe = QPushButton(tr("btn.probe"))
        self.btn_probe.clicked.connect(self.probe)
        self.probe_result.connect(self._on_probe)

        # selector de idioma: los nombres («Español», «English») no se traducen
        self.lang = QComboBox()
        for code, name in LANGUAGES:
            self.lang.addItem(name, code)
        idx = self.lang.findData(get_language())
        self.lang.setCurrentIndex(max(idx, 0))
        self.lang.currentIndexChanged.connect(self._language_changed)

        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 6, 8, 2)
        row.addWidget(QLabel(tr("label.camera")))
        row.addWidget(self.host)
        row.addWidget(self.btn_probe)
        row.addWidget(self.btn_scan)
        row.addWidget(self.btn_onboard)
        self.host_state = QLabel(tr("probe.unchecked"))
        row.addWidget(self.host_state)
        row.addStretch(1)
        row.addWidget(QLabel(tr("label.language")))
        row.addWidget(self.lang)

        self.tabs = QTabWidget()
        self.tether = TetherTab(self.host)
        self.remote = RemoteTab(self.host)
        self.astro = AstroTab(self.host)
        self.tabs.addTab(self.tether, tr("tab.tether"))
        self.tabs.addTab(self.remote, tr("tab.remote"))
        self.tabs.addTab(self.astro, tr("tab.astro"))

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(bar)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self.bus = LogBus()
        for pane in (self.tether.log, self.remote.log, self.astro.log):
            self.bus.message.connect(pane.add)
        self.log_handler = QtLogHandler(self.bus)
        self.log_handler.setLevel(logging.INFO)
        logging.getLogger("omtether").addHandler(self.log_handler)
        logging.getLogger("omtether").setLevel(logging.INFO)

    # -- idioma ------------------------------------------------------------
    def _language_changed(self, index: int) -> None:
        code = self.lang.itemData(index)
        if not code or code == get_language():
            return
        set_language(code)
        self.settings.setValue("language", code)
        self.settings.sync()
        # La vía segura para reaplicar los textos: una ventana nueva en el mismo
        # sitio, con la misma IP (viene de QSettings) y la misma pestaña activa.
        # La anterior se cierra (y para sus trabajos) como si el usuario la cerrase.
        replacement = MainWindow()
        replacement.setGeometry(self.geometry())
        replacement.tabs.setCurrentIndex(self.tabs.currentIndex())
        _keep_alive(replacement)
        replacement.show()
        self.close()
        self.deleteLater()

    # -- barra de cámara -------------------------------------------------
    def probe(self) -> None:
        host = self.host.text().strip()
        if not host:
            self.host_state.setText(tr("probe.missing_ip"))
            return
        self.btn_probe.setEnabled(False)
        self.host_state.setText(tr("probe.checking"))
        threading.Thread(target=self._probe_thread, args=(host,), daemon=True).start()

    def _probe_thread(self, host: str) -> None:
        try:
            ok = discovery.probe_ptpip(host, timeout=1.5)
        except Exception:  # noqa: BLE001
            ok = False
        self.probe_result.emit(host, ok)

    def _on_probe(self, host: str, ok: bool) -> None:
        self.host_state.setText(tr("probe.ok", host=host) if ok
                                else tr("probe.fail", host=host))
        self.btn_probe.setEnabled(True)

    def scan_network(self) -> None:
        dlg = ScanDialog(self, self.host.text().strip())
        if dlg.exec() and dlg.choice:
            self.host.setText(dlg.choice)
            self.probe()

    def onboard(self) -> None:
        OnboardDialog(self).exec()

    def closeEvent(self, event) -> None:  # noqa: N802
        for tab in (self.tether, self.remote, self.astro):
            try:
                if tab is self.tether:
                    if tab.daemon is not None:
                        tab.daemon.stop()
                    if tab.bridge is not None:
                        tab.bridge.stop()
                if tab is not self.tether and tab.worker is not None:
                    tab.worker.stop()
            except Exception:  # noqa: BLE001
                pass
        # que la librería no siga escribiendo en paneles de una ventana cerrada
        logging.getLogger("omtether").removeHandler(self.log_handler)
        super().closeEvent(event)


# Ventanas vivas: al cambiar de idioma se crea una MainWindow nueva y hay que
# conservar una referencia en Python para que no la recoja el recolector.
_WINDOWS: list[MainWindow] = []


def _keep_alive(win: MainWindow) -> None:
    _WINDOWS[:] = [w for w in _WINDOWS if w is not win and _is_live(w)]
    _WINDOWS.append(win)


def _is_live(win: MainWindow) -> bool:
    try:
        return win.isVisible()
    except RuntimeError:  # el objeto C++ ya no existe
        return False


def initial_language(settings: QSettings | None = None) -> str:
    """Idioma de arranque: el guardado en QSettings y, si no hay, el del entorno."""
    settings = settings or QSettings("omtether", "gui")
    saved = settings.value("language", "")
    return str(saved) if saved else detect_default_language()


def app_icon() -> QIcon:
    """Icono de la app: del tema y, si no está indexado, del fichero instalado."""
    icon = QIcon.fromTheme("omtether")
    if not icon.isNull():
        return icon
    here = Path(__file__).resolve().parent
    for cand in (Path("/usr/share/icons/hicolor/scalable/apps/omtether.svg"),
                 Path.home() / ".local/share/icons/hicolor/scalable/apps/omtether.svg",
                 here.parent / "packaging/common/omtether.svg"):
        if cand.is_file():
            return QIcon(str(cand))
    return QIcon()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("omtether")
    app.setDesktopFileName("omtether")
    set_language(initial_language())
    win = MainWindow()
    _keep_alive(win)
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
