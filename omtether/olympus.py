"""Olympus / OM System (OMD) vendor extensions.

All opcodes and property codes below were cross-checked against libgphoto2's
ptp2 camlib (LGPL-2.1+), which documents them from USB traces of OM Capture
("Capture UI 0x..").  They are plain PTP vendor operations and are used
identically over PTP/IP.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass

from . import ptp
from .ptp import PtpSession

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- opcodes
OC_Capture = 0x9481                  # param1: 3 = shutter down, 6 = shutter up
OC_GetDateTime = 0x9482
OC_MagnifyLiveViewPoint = 0x9483
OC_GetLiveViewImage = 0x9484         # param1: 1
OC_GetImage = 0x9485                 # JPEG from SDRAM
OC_ChangedProperties = 0x9486        # heavy poll: returns changed prop descs
OC_MFDrive = 0x9487
OC_MagnifyLiveViewArea = 0x9488
OC_SetProperties = 0x9489
OC_PollProperties = 0x948A           # light poll
OC_SetProperties2 = 0x948B
OC_OpticalZoomDrive = 0x948D
OC_DetectOneTouchWB = 0x94A1
OC_TransferModeAlive = 0x94BA     # periodic keep-alive ("PC_CHECK_ALIVE" in firmware)
OC_TransferModeStartStop = 0x94BA  # libgphoto2 names the same opcode this way
OC_GetUntransferList = 0x94BB    # objects not yet transferred to the PC
OC_GetLocalObjectInfo = 0x94BC
OC_GetLocalObject = 0x94BD
OC_DeleteLocalObject = 0x94BE
OC_SetConnectPcInfo = 0x94BF         # store PC (IP/port?) entry in the camera
OC_GetConnectPcInfo = 0x94C0         # by index
OC_ClearConnectPcInfo = 0x94C1       # by index
OC_GetOTWBKelvin = 0x94C2
OC_StationMode = 0x94C3              # start/end station mode (camera joins an AP)
OC_GetAFTargetFrames = 0x94C4

# ------------------------------------------------------------- properties
DPC_CameraControlMode = 0xD052       # 1 = PC control, 2 = standalone
DPC_LiveViewModeOM = 0xD06D          # 0x04000300 enables OM-1 live view
DPC_CaptureTarget = 0xD0DC           # 1 = SDRAM, 2 = card
DPC_ISO = 0xD007                     # ISO en cuerpos anteriores
DPC_ISO2 = 0xD1C0                    # OM-1 and later
DPC_ImageFormat = 0xD00D
DPC_Aperture = 0xD002
DPC_ShutterSpeed = 0xD01C
DPC_ExposureCompensation = 0xD008
DPC_WhiteBalance = 0xD01E
DPC_DriveMode = 0xD009
DPC_FocusMode = 0xD003
DPC_ExposureMeteringMode = 0xD004
DPC_ISOSpeed = 0xD007
DPC_ExposureProgramMode = 0xD006
DPC_StillRecordingMode = 0xD11B
DPC_RemainingCapacity = 0xD0DD       # image count / free space

LIVEVIEW_ON = 0x04000300

# ---------------------------------------------------------------- events
EC_CreateRecView = 0xC001
EC_ObjectAdded = 0xC002
EC_AF_Frame = 0xC003
EC_DirectStoreImage = 0xC004
EC_CompleteCameraControlOff = 0xC005
EC_AF_FrameOverInfo = 0xC006
EC_DevicePropChanged = 0xC008
EC_ImageTransferModeFinish = 0xC00C
EC_ImageRecordFinish = 0xC00D
EC_CaptureComplete = 0xC103
EC_ObjectAddedOM = 0xC102      # param1 = handle del objeto nuevo (OM-1 por Wi-Fi)
EC_SlotStatusChange = 0xC00E
EC_PrioritizeRecord = 0xC00F
EC_FailCombiningAfterShooting = 0xC010
EC_NotifyAFTargetFrame = 0xC011

EC_NAMES = {
    EC_CreateRecView: "OLY_CreateRecView",
    EC_ObjectAdded: "OLY_ObjectAdded",
    EC_AF_Frame: "OLY_AF_Frame",
    EC_DirectStoreImage: "OLY_DirectStoreImage",
    EC_CompleteCameraControlOff: "OLY_CompleteCameraControlOff",
    EC_AF_FrameOverInfo: "OLY_AF_FrameOverInfo",
    EC_DevicePropChanged: "OLY_DevicePropChanged",
    EC_ImageTransferModeFinish: "OLY_ImageTransferModeFinish",
    EC_ImageRecordFinish: "OLY_ImageRecordFinish",
    EC_SlotStatusChange: "OLY_SlotStatusChange",
    EC_PrioritizeRecord: "OLY_PrioritizeRecord",
    EC_FailCombiningAfterShooting: "OLY_FailCombiningAfterShooting",
    EC_NotifyAFTargetFrame: "OLY_NotifyAFTargetFrame",
    ptp.EC_ObjectAdded: "ObjectAdded",
    ptp.EC_CaptureComplete: "CaptureComplete",
    ptp.EC_DevicePropChanged: "DevicePropChanged",
    ptp.EC_StoreRemoved: "StoreRemoved",
    ptp.EC_StoreAdded: "StoreAdded",
}


def event_name(code: int) -> str:
    return EC_NAMES.get(code, "0x%04x" % code)


def is_image_event(code: int) -> bool:
    return code in (EC_ObjectAdded, EC_ImageRecordFinish, EC_DirectStoreImage, ptp.EC_ObjectAdded, ptp.EC_CaptureComplete)


# ------------------------------------------------------------------ helpers
def find_jpeg(data: bytes) -> bytes | None:
    """Olympus live view / SDRAM images may be wrapped; return the JPEG part."""
    start = data.find(b"\xff\xd8\xff")
    if start < 0:
        return None
    end = data.rfind(b"\xff\xd9")
    if end > start:
        return data[start:end + 2]
    return data[start:]


def find_orf(data: bytes) -> bytes | None:
    """Locate an ORF (Olympus RAW) payload inside a wrapper."""
    for magic in (b"IIRO\x08\x00\x00\x00", b"MMOR\x00\x00\x00\x08", b"IIRS\x08\x00\x00\x00"):
        start = data.find(magic)
        if start >= 0:
            return data[start:]
    return None


# ------------------------------------------------------------------ session
class Olympus:
    def __init__(self, session: PtpSession) -> None:
        self.s = session
        self._liveview_prev: int | None = None
        self._control_mode_prev: int | None = None

    # -- PC control mode ---------------------------------------------------
    def enter_pc_mode(self, timeout: float = 3.0) -> None:
        """Put the camera in PC-control mode (what OM Capture does on connect)."""
        try:
            mode = self.s.get_prop_u16(DPC_CameraControlMode)
            self._control_mode_prev = mode
            log.debug("camera control mode was %d", mode)
        except ptp.PtpError as exc:
            log.debug("camera control mode unreadable: %s", exc)
        try:
            self.s.set_prop_u16(DPC_CameraControlMode, 1)
        except ptp.PtpError as exc:
            log.warning("could not enter PC control mode: %s", exc)
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ev = self.s.t.get_event(0.2)
            if ev is not None:
                log.debug("event while entering PC mode: %s", ev)
                return

    def exit_pc_mode(self) -> None:
        mode = self._control_mode_prev or 2
        try:
            self.s.set_prop_u16(DPC_CameraControlMode, mode)
        except ptp.PtpError as exc:
            log.debug("could not restore control mode: %s", exc)

    # -- capture -----------------------------------------------------------
    def capture(self, settle_timeout: float = 20.0) -> list[int]:
        """Trigger a capture; return object handles announced by the camera."""
        try:
            target = self.s.get_prop_u16(DPC_CaptureTarget)
            log.debug("capture target = %d", target)
        except ptp.PtpError:
            pass

        # libgphoto2 sequence: 0x9481(3) = shutter down, wait for 0xC103, 0x9481(0) = up
        self.s.op(OC_Capture, [3])
        deadline = time.monotonic() + settle_timeout
        while time.monotonic() < deadline:
            ev = self.s.t.get_event(0.25)
            if ev is None:
                continue
            log.debug("capture event (exposure): %s", ev)
            if ev.code in (EC_CaptureComplete, EC_ImageRecordFinish):
                break
        try:
            self.s.op(OC_Capture, [6])
        except ptp.PtpError as exc:
            log.debug("shutter release returned %s", exc)

        handles: list[int] = []
        while time.monotonic() < deadline:
            ev = self.s.t.get_event(0.25)
            if ev is None:
                continue
            log.debug("capture event (store): %s", ev)
            if ev.code in (EC_ObjectAdded, ptp.EC_ObjectAdded, EC_DirectStoreImage) and ev.params:
                if ev.params[0] and ev.params[0] not in handles:
                    handles.append(ev.params[0])
            if handles and ev.code in (EC_ImageTransferModeFinish, EC_DirectStoreImage):
                break
        return handles

    # -- live view ---------------------------------------------------------
    def liveview_start(self) -> None:
        try:
            cur = self.s.get_prop_u32(DPC_LiveViewModeOM)
            self._liveview_prev = cur
        except ptp.PtpError as exc:
            log.debug("live view mode property not readable: %s", exc)
            self._liveview_prev = None
        if self._liveview_prev != LIVEVIEW_ON:
            try:
                self.s.set_prop_u32(DPC_LiveViewModeOM, LIVEVIEW_ON)
            except ptp.PtpError as exc:
                log.warning("could not enable live view: %s", exc)

    def liveview_stop(self) -> None:
        if self._liveview_prev is None or self._liveview_prev == LIVEVIEW_ON:
            return
        try:
            self.s.set_prop_u32(DPC_LiveViewModeOM, self._liveview_prev)
        except ptp.PtpError:
            pass

    def liveview_frame(self, timeout: float = 5.0) -> bytes | None:
        """Return one live view JPEG frame, or None if the camera is busy."""
        rc, _, data = self.s.op(OC_GetLiveViewImage, [1], expect_data=True, check=False, timeout=timeout)
        if rc != ptp.RC_OK:
            log.debug("live view frame rc=0x%04x", rc)
            return None
        jpg = find_jpeg(data)
        if jpg is None:
            log.debug("live view frame without JPEG (%d bytes)", len(data))
            return None
        return jpg

    # -- properties --------------------------------------------------------
    def poll_changed_properties(self) -> list[tuple[int, bytes]]:
        """0x9486: returns a list of PTP device property datasets."""
        rc, _, data = self.s.op(OC_ChangedProperties, expect_data=True, check=False)
        if rc != ptp.RC_OK or not data:
            return []
        return parse_prop_list(data)

    def set_properties(self, values: dict[int, int], list_opcode: int = OC_SetProperties) -> None:
        """0x9489/0x948B: announce a batch of uint16 properties."""
        payload = struct.pack("<I", len(values))
        for code, val in values.items():
            payload += struct.pack("<HH", code, val)
        self.s.op(list_opcode, data_out=payload)

    def read_properties(self, codes: list[int]) -> dict[int, bytes]:
        out: dict[int, bytes] = {}
        for code in codes:
            try:
                _, _, data = self.s.op(ptp.OC_GetDevicePropValue, [code], expect_data=True)
            except ptp.PtpError:
                continue
            out[code] = data
        return out

    # -- wireless transfer mode -------------------------------------------
    def transfer_mode(self, start: bool) -> None:
        self.s.op(OC_TransferModeStartStop, [1 if start else 0], check=False)

    def keepalive(self) -> None:
        """Tell the camera the PC is still here (firmware task PC_CHECK_ALIVE)."""
        self.s.op(OC_TransferModeAlive, [1], check=False)

    def untransferred(self) -> list[int]:
        rc, _, data = self.s.op(OC_GetUntransferList, expect_data=True, check=False)
        if rc != ptp.RC_OK or len(data) < 4:
            return []
        n = struct.unpack_from("<I", data)[0]
        return list(struct.unpack_from("<%dI" % n, data, 4))

    def station_mode(self, start: bool) -> None:
        """0x94C3: camera joins (start=1) or leaves (0) the Wi-Fi access point."""
        self.s.op(OC_StationMode, [1 if start else 0], check=False)

    def set_connect_pc_info(self, data: bytes) -> None:
        self.s.op(OC_SetConnectPcInfo, data_out=data)

    def get_connect_pc_info(self, index: int = 0) -> bytes:
        _, _, data = self.s.op(OC_GetConnectPcInfo, [index], expect_data=True, check=False)
        return data


# ------------------------------------------------------------- prop parsing
def parse_prop_list(data: bytes) -> list[tuple[int, bytes]]:
    """Parse a list of PTP DevicePropDesc datasets (32 bit count prefix).

    Each dataset is: code(u16) datatype(u16) getset(u8) default current
    formflag(u8) form.  We return the *current* value of every property.
    """
    out: list[tuple[int, bytes]] = []
    if len(data) < 4:
        return out
    count = struct.unpack_from("<I", data)[0]
    off = 4
    for _ in range(count):
        if off + 5 > len(data):
            break
        code, dtype = struct.unpack_from("<HH", data, off)
        off += 4
        off += 1  # GetSet flags
        try:
            _, off = _read_dtype(data, off, dtype)     # factory default
            value, off = _read_dtype(data, off, dtype)  # current value
        except (IndexError, struct.error):
            break
        out.append((code, value))
        if off + 1 > len(data):
            break
        form = data[off]
        off += 1
        if form == 2:  # range: min, max, step
            try:
                for _ in range(3):
                    _, off = _read_dtype(data, off, dtype)
            except (IndexError, struct.error):
                break
        elif form == 1:  # enumeration
            if off + 2 > len(data):
                break
            n = struct.unpack_from("<H", data, off)[0]
            off += 2
            for _ in range(n):
                _, off = _read_dtype(data, off, dtype)
    return out


def _read_dtype(data: bytes, off: int, dtype: int) -> tuple[bytes, int]:
    sizes = {
        ptp.DTC_INT8: 1, ptp.DTC_UINT8: 1,
        ptp.DTC_INT16: 2, ptp.DTC_UINT16: 2,
        ptp.DTC_INT32: 4, ptp.DTC_UINT32: 4,
        ptp.DTC_INT64: 8, ptp.DTC_UINT64: 8,
    }
    if dtype == ptp.DTC_STRING:
        n = data[off]
        off += 1
        if n == 0:
            return b"", off
        return data[off:off + n * 2], off + n * 2
    size = sizes.get(dtype, 2)
    return data[off:off + size], off + size
