"""Minimal PTP (ISO 15740) client built on top of the PTP/IP transport."""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field

from .ptpip import PtpIpTransport

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- opcodes
OC_GetDeviceInfo = 0x1001
OC_OpenSession = 0x1002
OC_CloseSession = 0x1003
OC_GetStorageIDs = 0x1004
OC_GetStorageInfo = 0x1005
OC_GetNumObjects = 0x1006
OC_GetObjectHandles = 0x1007
OC_GetObjectInfo = 0x1008
OC_GetObject = 0x1009
OC_GetThumb = 0x100A
OC_DeleteObject = 0x100B
OC_SendObjectInfo = 0x100C
OC_SendObject = 0x100D
OC_InitiateCapture = 0x100E
OC_FormatStore = 0x100F
OC_ResetDevice = 0x1010
OC_SelfTest = 0x1011
OC_SetObjectProtection = 0x1012
OC_PowerDown = 0x1013
OC_GetDevicePropDesc = 0x1014
OC_GetDevicePropValue = 0x1015
OC_SetDevicePropValue = 0x1016
OC_ResetDevicePropValue = 0x1017
OC_TerminateOpenCapture = 0x1018
OC_MoveObject = 0x1019
OC_CopyObject = 0x101A
OC_GetPartialObject = 0x101B
OC_InitiateOpenCapture = 0x101C

# --------------------------------------------------------------- responses
RC_OK = 0x2001
RC_GeneralError = 0x2002
RC_SessionNotOpen = 0x2003
RC_InvalidTransactionID = 0x2004
RC_OperationNotSupported = 0x2005
RC_ParameterNotSupported = 0x2006
RC_IncompleteTransfer = 0x2007
RC_InvalidStorageID = 0x2008
RC_InvalidObjectHandle = 0x2009
RC_DevicePropNotSupported = 0x200A
RC_InvalidObjectFormatCode = 0x200B
RC_StoreFull = 0x200C
RC_ObjectWriteProtected = 0x200D
RC_StoreReadOnly = 0x200E
RC_AccessDenied = 0x200F
RC_NoThumbnailPresent = 0x2010
RC_SelfTestFailed = 0x2011
RC_PartialDeletion = 0x2012
RC_StoreNotAvailable = 0x2013
RC_SpecificationByFormatUnsupported = 0x2014
RC_NoValidObjectInfo = 0x2015
RC_InvalidCodeFormat = 0x2016
RC_UnknownVendorCode = 0x2017
RC_CaptureAlreadyTerminated = 0x2018
RC_DeviceBusy = 0x2019
RC_InvalidParentObject = 0x201A
RC_InvalidDevicePropFormat = 0x201B
RC_InvalidDevicePropValue = 0x201C
RC_InvalidParameter = 0x201D
RC_SessionAlreadyOpened = 0x201E
RC_TransactionCancelled = 0x201F
RC_SpecificationOfDestinationUnsupported = 0x2020

RC_NAMES = {v: k[3:] for k, v in list(globals().items()) if k.startswith("RC_") and isinstance(v, int)}

# ---------------------------------------------------------------- events
EC_Undefined = 0x4000
EC_CancelTransaction = 0x4001
EC_ObjectAdded = 0x4002
EC_ObjectRemoved = 0x4003
EC_StoreAdded = 0x4004
EC_StoreRemoved = 0x4005
EC_DevicePropChanged = 0x4006
EC_ObjectInfoChanged = 0x4007
EC_DeviceInfoChanged = 0x4008
EC_RequestObjectTransfer = 0x4009
EC_StoreFull = 0x400A
EC_DeviceReset = 0x400B
EC_StorageInfoChanged = 0x400C
EC_CaptureComplete = 0x400D
EC_UnreportedStatus = 0x400E

# ------------------------------------------------------------- data types
DTC_INT8 = 0x0001
DTC_UINT8 = 0x0002
DTC_INT16 = 0x0003
DTC_UINT16 = 0x0004
DTC_INT32 = 0x0005
DTC_UINT32 = 0x0006
DTC_INT64 = 0x0007
DTC_UINT64 = 0x0008
DTC_STRING = 0xFFFF


class PtpError(Exception):
    def __init__(self, code: int, message: str = "") -> None:
        self.code = code
        name = RC_NAMES.get(code, "0x%04x" % code)
        super().__init__("PTP error RC_%s (0x%04x) %s" % (name, code, message))


class Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.off = 0

    def u8(self) -> int:
        v = self.data[self.off]
        self.off += 1
        return v

    def u16(self) -> int:
        v = struct.unpack_from("<H", self.data, self.off)[0]
        self.off += 2
        return v

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.data, self.off)[0]
        self.off += 4
        return v

    def u64(self) -> int:
        v = struct.unpack_from("<Q", self.data, self.off)[0]
        self.off += 8
        return v

    def string(self) -> str:
        # PTP strings carry their NUL terminator inside the declared length
        n = self.data[self.off]
        self.off += 1
        if n == 0:
            return ""
        raw = self.data[self.off:self.off + n * 2]
        self.off += n * 2
        return raw.decode("utf-16-le", "replace").rstrip("\x00")

    def array_u16(self) -> list[int]:
        n = self.u32()
        return [self.u16() for _ in range(n)]

    def array_u32(self) -> list[int]:
        n = self.u32()
        return [self.u32() for _ in range(n)]


@dataclass
class DeviceInfo:
    standard_version: int = 0
    vendor_extension_id: int = 0
    vendor_extension_version: int = 0
    vendor_extension_desc: str = ""
    functional_mode: int = 0
    operations: list[int] = field(default_factory=list)
    events: list[int] = field(default_factory=list)
    properties: list[int] = field(default_factory=list)
    capture_formats: list[int] = field(default_factory=list)
    image_formats: list[int] = field(default_factory=list)
    manufacturer: str = ""
    model: str = ""
    device_version: str = ""
    serial: str = ""

    @classmethod
    def parse(cls, data: bytes) -> "DeviceInfo":
        r = Reader(data)
        di = cls()
        di.standard_version = r.u16()
        di.vendor_extension_id = r.u32()
        di.vendor_extension_version = r.u16()
        di.vendor_extension_desc = r.string()
        di.functional_mode = r.u16()
        # libgphoto2 also unpacks these four arrays as uint16 (PTP 1.0 is
        # ambiguous here; every body we have seen, including the OM-1, uses 16 bit)
        di.operations = r.array_u16()
        di.events = r.array_u16()
        di.properties = r.array_u16()
        di.capture_formats = r.array_u16()
        di.image_formats = r.array_u16()
        di.manufacturer = r.string()
        di.model = r.string()
        di.device_version = r.string()
        di.serial = r.string()
        return di

    def supports(self, opcode: int) -> bool:
        return opcode in self.operations


@dataclass
class ObjectInfo:
    handle: int = 0
    storage_id: int = 0
    format: int = 0
    protection: int = 0
    size: int = 0
    thumb_format: int = 0
    thumb_size: int = 0
    thumb_width: int = 0
    thumb_height: int = 0
    image_width: int = 0
    image_height: int = 0
    image_bitdepth: int = 0
    parent: int = 0
    association_type: int = 0
    association_desc: int = 0
    sequence: int = 0
    filename: str = ""
    capture_date: str = ""
    modification_date: str = ""
    keywords: str = ""

    @classmethod
    def parse(cls, data: bytes) -> "ObjectInfo":
        r = Reader(data)
        oi = cls()
        oi.storage_id = r.u32()
        oi.format = r.u16()
        oi.protection = r.u16()
        oi.size = r.u32()
        oi.thumb_format = r.u16()
        oi.thumb_size = r.u32()
        oi.thumb_width = r.u32()
        oi.thumb_height = r.u32()
        oi.image_width = r.u32()
        oi.image_height = r.u32()
        oi.image_bitdepth = r.u32()
        oi.parent = r.u32()
        oi.association_type = r.u16()
        oi.association_desc = r.u32()
        oi.sequence = r.u32()
        oi.filename = r.string()
        oi.capture_date = r.string()
        oi.modification_date = r.string()
        oi.keywords = r.string()
        return oi

    @property
    def is_image(self) -> bool:
        # 0x3801 JPEG, 0x3000 undefined, 0xB101 Olympus ORF, 0xB802? etc.
        return self.format not in (0x3001,) and self.size > 0


class PtpSession:
    """PTP session over an already connected PTP/IP transport."""

    def __init__(self, transport: PtpIpTransport) -> None:
        self.t = transport
        self.info: DeviceInfo | None = None
        self.session_id = 0

    # -- plumbing ----------------------------------------------------------
    def op(
        self,
        code: int,
        params: list[int] | None = None,
        *,
        data_out: bytes | None = None,
        expect_data: bool = False,
        timeout: float | None = None,
        check: bool = True,
    ) -> tuple[int, list[int], bytes]:
        rc, rparams, data = self.t.operation(
            code, params, data_out=data_out, expect_data=expect_data, timeout=timeout
        )
        log.debug("op 0x%04x %s -> rc=0x%04x params=%s len(data)=%d",
                  code, params or [], rc, [hex(p) for p in rparams], len(data))
        if check and rc != RC_OK:
            raise PtpError(rc, "opcode 0x%04x params=%s" % (code, params))
        return rc, rparams, data

    # -- session -----------------------------------------------------------
    def open(self, session_id: int = 1) -> DeviceInfo:
        self.info = self.get_device_info()
        rc, _, _ = self.op(OC_OpenSession, [session_id], check=False)
        self.session_id = session_id
        if rc not in (RC_OK, RC_SessionAlreadyOpened):
            raise PtpError(rc, "OpenSession(%d)" % session_id)
        if rc == RC_SessionAlreadyOpened:
            log.warning("session already open on the camera")
        log.info("connected to %s %s (vendor ext 0x%08x, %d ops)",
                 self.info.manufacturer, self.info.model,
                 self.info.vendor_extension_id, len(self.info.operations))
        return self.info

    def close(self) -> None:
        try:
            self.op(OC_CloseSession, check=False)
        except Exception:  # noqa: BLE001 - best effort
            pass

    # -- device ------------------------------------------------------------
    def get_device_info(self) -> DeviceInfo:
        _, _, data = self.op(OC_GetDeviceInfo, expect_data=True)
        return DeviceInfo.parse(data)

    def get_storage_ids(self) -> list[int]:
        _, _, data = self.op(OC_GetStorageIDs, expect_data=True)
        return Reader(data).array_u32()

    def get_object_handles(self, storage: int = 0xFFFFFFFF, fmt: int = 0, parent: int = 0xFFFFFFFF) -> list[int]:
        _, _, data = self.op(OC_GetObjectHandles, [storage, fmt, parent], expect_data=True)
        return Reader(data).array_u32()

    def list_object_handles(self, parent: int = 0xFFFFFFFF,
                            recursive: bool = True) -> list[int]:
        """Object handles, walking folders.

        The camera only returns the top level for parent=0xFFFFFFFF, so folders
        (format 0x3001) have to be expanded explicitly; without this the object
        list looks like two empty DCIM entries and every download misses.
        """
        every, _folders = self._walk(parent, recursive)
        return every

    def _walk(self, parent: int, recursive: bool) -> tuple[list[int], set[int]]:
        """(todos los handles, conjunto de carpetas) en dos llamadas por nivel.

        Pedir `GetObjectInfo` de cada objeto —lo que hacía antes— son ~50 idas y
        vueltas por pasada y la cámara acaba dejando de responder en plena
        ráfaga; con el filtro de formato basta una llamada extra por carpeta.
        """
        def handles(parent_handle: int, fmt: int) -> list[int]:
            rc, _, data = self.op(OC_GetObjectHandles, [0xFFFFFFFF, fmt, parent_handle],
                                  expect_data=True, check=False)
            return Reader(data).array_u32() if rc == RC_OK and data else []

        seen: list[int] = []
        folders: set[int] = set()
        queue = [parent]
        visited: set[int] = set()
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for handle in handles(current, 0):
                if handle not in seen:
                    seen.append(handle)
            if recursive:
                for handle in handles(current, 0x3001):
                    folders.add(handle)
                    if handle not in visited:
                        queue.append(handle)
        return seen, folders

    def list_files(self) -> list[int]:
        """Handles de ficheros (sin carpetas), sin pedir ObjectInfo de cada uno."""
        every, folders = self._walk(0xFFFFFFFF, True)
        return [h for h in every if h not in folders]

    def iter_files(self) -> list[tuple[int, "ObjectInfo"]]:
        """All non-folder objects with their info (the tethering hot path)."""
        out: list[tuple[int, ObjectInfo]] = []
        for handle in self.list_object_handles():
            try:
                info = self.get_object_info(handle)
            except PtpError:
                continue
            if info.format != 0x3001:
                out.append((handle, info))
        return out

    def get_object_info(self, handle: int) -> ObjectInfo:
        _, _, data = self.op(OC_GetObjectInfo, [handle], expect_data=True)
        oi = ObjectInfo.parse(data)
        oi.handle = handle
        return oi

    def get_object(self, handle: int, timeout: float = 120.0) -> bytes:
        _, _, data = self.op(OC_GetObject, [handle], expect_data=True, timeout=timeout)
        return data

    def get_thumb(self, handle: int) -> bytes:
        _, _, data = self.op(OC_GetThumb, [handle], expect_data=True)
        return data

    def delete_object(self, handle: int, fmt: int = 0) -> None:
        self.op(OC_DeleteObject, [handle, fmt])

    def initiate_capture(self, storage: int = 0, fmt: int = 0) -> None:
        self.op(OC_InitiateCapture, [storage, fmt])

    # -- properties --------------------------------------------------------
    def get_prop(self, code: int) -> bytes:
        """Raw value dataset of a device property (length tells you the type)."""
        _, _, data = self.op(OC_GetDevicePropValue, [code], expect_data=True)
        return data

    def get_prop_u16(self, code: int) -> int:
        _, _, data = self.op(OC_GetDevicePropValue, [code], expect_data=True)
        return struct.unpack_from("<H", data)[0]

    def get_prop_u32(self, code: int) -> int:
        _, _, data = self.op(OC_GetDevicePropValue, [code], expect_data=True)
        return struct.unpack_from("<I", data)[0]

    def get_prop_string(self, code: int) -> str:
        _, _, data = self.op(OC_GetDevicePropValue, [code], expect_data=True)
        return Reader(data).string()

    def set_prop_u16(self, code: int, value: int) -> None:
        self.op(OC_SetDevicePropValue, [code], data_out=struct.pack("<H", value))

    def set_prop_u32(self, code: int, value: int) -> None:
        self.op(OC_SetDevicePropValue, [code], data_out=struct.pack("<I", value))

    def set_prop_string(self, code: int, value: str) -> None:
        raw = value.encode("utf-16-le")
        self.op(OC_SetDevicePropValue, [code], data_out=bytes([len(value)]) + raw)

    def prop_enum(self, code: int) -> tuple[int, object, list]:
        """(datatype, valor actual, valores admitidos) de una propiedad enum."""
        data = self.get_prop_desc(code)
        if len(data) < 5:
            return 0, None, []
        dtype = struct.unpack_from("<H", data, 2)[0]
        # tipos PTP: 1 INT8, 2 UINT8, 3 INT16, 4 UINT16, 5 INT32, 6 UINT32,
        # 7 INT64, 8 UINT64, 9 INT128, 10 UINT128
        size = {1: 1, 2: 1, 3: 2, 4: 2, 5: 4, 6: 4, 7: 8, 8: 8}.get(dtype, 0)
        if not size:
            return dtype, None, []
        fmt = {1: "<b", 2: "<B", 3: "<h", 4: "<H", 5: "<i", 6: "<I",
               7: "<q", 8: "<Q"}[dtype]
        off = 5 + size          # tras getset + factory default
        if off + size > len(data):
            return dtype, None, []
        current = struct.unpack_from(fmt, data, off)[0]
        off += size + 1         # + formflag
        values: list = []
        if off + 2 <= len(data) and data[off - 1] == 2:   # 2 = enumeración
            count = struct.unpack_from("<H", data, off)[0]
            off += 2
            for _ in range(count):
                if off + size > len(data):
                    break
                values.append(struct.unpack_from(fmt, data, off)[0])
                off += size
        return dtype, current, values

    def get_prop_desc(self, code: int) -> bytes:
        _, _, data = self.op(OC_GetDevicePropDesc, [code], expect_data=True)
        return data
