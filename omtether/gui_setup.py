"""Onboarding y descubrimiento de la cámara para la app de escritorio.

Dos piezas:

  * `ScanDialog`  — busca la cámara en la red local (mDNS + SSDP + barrido del
                    puerto 15740) y devuelve la IP elegida.
  * `OnboardDialog` — el emparejamiento por USB: escribe este equipo en una de
                    las cuatro ranuras de la cámara, que es lo que después le
                    permite aceptar la conexión PTP/IP por Wi-Fi.

Todo el trabajo lento (USB, red) va en hilos: la ventana nunca se bloquea.
"""

from __future__ import annotations

import socket

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from . import discovery, olympus, pairing, ptp, ptpip
from .i18n import tr


def slot_help() -> str:
    """Pasos del emparejamiento, en el idioma activo (antes era la constante SLOT_HELP)."""
    return tr("onboard.help")


def close_box(dialog: QDialog) -> QDialogButtonBox:
    """Botonera con un único «Cerrar» traducido por nuestro catálogo (no por Qt)."""
    box = QDialogButtonBox()
    box.addButton(tr("btn.close"), QDialogButtonBox.ButtonRole.RejectRole)
    box.rejected.connect(dialog.reject)
    return box


# --------------------------------------------------------------------------
class ScanWorker(QThread):
    """Busca cámaras en la red local sin bloquear la interfaz."""

    found = pyqtSignal(object)          # discovery.Found
    message = pyqtSignal(str)
    done = pyqtSignal()

    def __init__(self, extra_net: str | None = None) -> None:
        super().__init__()
        self.extra_net = extra_net

    def run(self) -> None:  # noqa: D401
        try:
            self.message.emit(tr("scan.searching"))
            results = discovery.discover(timeout=3.0, do_scan=True,
                                        extra_net=self.extra_net)
        except Exception as exc:  # noqa: BLE001
            self.message.emit(tr("scan.failed", error=exc))
            results = []
        if not results:
            self.message.emit(tr("scan.none_found"))
        for item in results:
            self.found.emit(item)
        self.done.emit()


class ScanDialog(QDialog):
    def __init__(self, parent=None, current: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("scan.title"))
        self.resize(560, 320)
        self.choice: str | None = None
        self._worker: ScanWorker | None = None

        self.list = QPlainTextEdit()
        self.list.setReadOnly(True)
        self.status = QLabel(tr("scan.hint_start"))
        self.ip = QLineEdit(current)
        self.ip.setPlaceholderText(tr("scan.ip_placeholder"))

        self.btn_scan = QPushButton(tr("btn.search"))
        self.btn_scan.clicked.connect(self.start_scan)
        self.btn_ok = QPushButton(tr("btn.use_ip"))
        self.btn_ok.clicked.connect(self.accept_ip)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("label.ip")))
        row.addWidget(self.ip, 1)
        row.addWidget(self.btn_scan)
        row.addWidget(self.btn_ok)

        box = close_box(self)

        lay = QVBoxLayout(self)
        intro = QLabel(tr("scan.intro"))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.status)
        lay.addLayout(row)
        lay.addWidget(box)

    def start_scan(self) -> None:
        self.list.clear()
        self.btn_scan.setEnabled(False)
        self._worker = ScanWorker()
        self._worker.message.connect(self.status.setText)
        self._worker.found.connect(self.add_found)
        self._worker.done.connect(self.scan_done)
        self._worker.start()

    def add_found(self, item) -> None:
        mode = item.mode or "?"
        line = tr("scan.row", host=item.host, port=item.port, mode=mode, detail=item.detail)
        self.list.appendPlainText(line)

    def scan_done(self) -> None:
        self.btn_scan.setEnabled(True)
        first = self.list.toPlainText().splitlines()
        if first:
            self.ip.setText(first[0].split()[0])
            self.status.setText(tr("scan.hint_choose"))

    def accept_ip(self) -> None:
        text = self.ip.text().strip()
        if text:
            self.choice = text
            self.accept()


# --------------------------------------------------------------------------
class UsbWorker(QThread):
    """Operaciones PTP por USB (leer ranuras, emparejar, borrar)."""

    message = pyqtSignal(str)
    slots = pyqtSignal(object)          # list[dict]
    done = pyqtSignal(bool, str)

    def __init__(self, action: str, slot: int = 1, name: str = "", macs=None) -> None:
        super().__init__()
        self.action = action
        self.slot = slot
        self.name = name
        self.macs = macs or []

    def run(self) -> None:  # noqa: D401
        usb = None
        try:
            cams = pairing.find_usb_cameras()
            if not cams:
                self.done.emit(False, tr("usb.none"))
                return
            for cam in cams:
                self.message.emit("USB %s  %04x:%04x  %s" % (
                    cam["node"], cam["vid"], cam["pid"], cam["product"]))
            usb = pairing.UsbPtp()
            rc = usb.open_session()
            self.message.emit("OpenSession -> 0x%04x" % rc)
            if rc not in (0x2001, 0x201E):
                self.message.emit(tr("usb.session_warning"))

            if self.action in ("list", "pair", "clear"):
                slots = []
                for idx in range(pairing.MAX_SLOTS):
                    try:
                        rc, _, data = usb.operation(olympus.OC_GetConnectPcInfo, [idx])
                    except Exception as exc:  # noqa: BLE001
                        slots.append({"slot": idx + 1, "error": str(exc)})
                        continue
                    if rc != ptp.RC_OK:
                        slots.append({"slot": idx + 1, "empty": True, "rc": rc})
                    else:
                        info = pairing.parse_connect_pc_info(data)
                        info["slot"] = idx + 1
                        slots.append(info)
                self.slots.emit(slots)

            if self.action == "pair":
                payload = pairing.build_connect_pc_info(self.name, self.macs)
                rc, _, _ = usb.operation(olympus.OC_SetConnectPcInfo,
                                         [self.slot - 1], data_out=payload)
                self.message.emit(tr("usb.set_result", slot=self.slot, rc=rc))
                if rc != ptp.RC_OK:
                    self.done.emit(False, tr("usb.rejected", rc=rc))
                    return
                rc, _, back = usb.operation(olympus.OC_GetConnectPcInfo, [self.slot - 1])
                if rc == ptp.RC_OK:
                    self.message.emit(tr("usb.reread", info=pairing.parse_connect_pc_info(back)))
                self.done.emit(True, tr("usb.paired", slot=self.slot))
            elif self.action == "clear":
                rc, _, _ = usb.operation(olympus.OC_ClearConnectPcInfo, [self.slot - 1])
                self.done.emit(rc == ptp.RC_OK, tr("usb.clear_result", slot=self.slot, rc=rc))
            else:
                self.done.emit(True, tr("usb.slots_read"))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, str(exc))
        finally:
            if usb is not None:
                try:
                    usb.close_session()
                except Exception:  # noqa: BLE001
                    pass
                usb.close()


class OnboardDialog(QDialog):
    """Asistente de emparejamiento por USB."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("onboard.title"))
        self.resize(620, 460)
        self._worker: UsbWorker | None = None

        self.help = QLabel(slot_help())
        self.help.setWordWrap(True)

        self.name = QLineEdit(pairing.default_name())
        self.slot = QComboBox()
        for idx in range(1, pairing.MAX_SLOTS + 1):
            self.slot.addItem(tr("onboard.slot_n", slot=idx), idx)

        self.macs = QPlainTextEdit()
        self.macs.setMaximumHeight(70)
        try:
            self.macs.setPlainText("\n".join(ptpip.primary_macs()))
        except Exception:  # noqa: BLE001
            self.macs.setPlainText(socket.gethostname())

        form = QFormLayout()
        form.addRow(tr("onboard.name"), self.name)
        form.addRow(tr("onboard.slot"), self.slot)
        form.addRow(tr("onboard.macs"), self.macs)

        self.btn_list = QPushButton(tr("btn.read_slots"))
        self.btn_pair = QPushButton(tr("btn.pair"))
        self.btn_clear = QPushButton(tr("btn.clear_slot"))
        self.btn_list.clicked.connect(lambda: self.run_action("list"))
        self.btn_pair.clicked.connect(lambda: self.run_action("pair"))
        self.btn_clear.clicked.connect(lambda: self.run_action("clear"))

        row = QHBoxLayout()
        for b in (self.btn_list, self.btn_pair, self.btn_clear):
            row.addWidget(b)
        row.addStretch(1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)

        box = close_box(self)

        lay = QVBoxLayout(self)
        lay.addWidget(self.help)
        lay.addLayout(form)
        lay.addLayout(row)
        lay.addWidget(self.log, 1)
        lay.addWidget(box)

    def run_action(self, action: str) -> None:
        macs = [line.strip() for line in self.macs.toPlainText().splitlines() if line.strip()]
        if action == "pair" and not macs:
            self.append(tr("onboard.need_mac"))
            return
        for b in (self.btn_list, self.btn_pair, self.btn_clear):
            b.setEnabled(False)
        self._worker = UsbWorker(action, int(self.slot.currentData()),
                                 self.name.text().strip() or pairing.default_name(), macs)
        self._worker.message.connect(self.append)
        self._worker.slots.connect(self.show_slots)
        self._worker.done.connect(self.finished_action)
        self._worker.start()

    def show_slots(self, slots) -> None:
        self.log.appendPlainText(tr("onboard.slots_header"))
        for item in slots:
            if item.get("empty"):
                self.log.appendPlainText(tr("onboard.slot_empty", slot=item["slot"]))
            elif item.get("error"):
                self.log.appendPlainText(tr("onboard.slot_error", slot=item["slot"],
                                            error=item["error"]))
            else:
                self.log.appendPlainText(tr("onboard.slot_entry", slot=item["slot"],
                                            name=item.get("name", "?"),
                                            macs=", ".join(item.get("macs", []))))

    def append(self, text: str) -> None:
        self.log.appendPlainText(text)

    def finished_action(self, ok: bool, text: str) -> None:
        self.append(("✔ " if ok else "✘ ") + text)
        for b in (self.btn_list, self.btn_pair, self.btn_clear):
            b.setEnabled(True)
