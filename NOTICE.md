# Notices and provenance — omtether

`omtether` is an independent, clean-room reimplementation of the protocol used
by OM Digital Solutions' **OM Capture** for Windows, written to use an OM System
camera from Linux.

## What this repository contains

Only original source code, original documentation and a synthetic camera
simulator. **No vendor binaries, installers, APKs, firmware images or extracted
resources are distributed here.** The analysis that led to this code was done on
a copy of the vendor installer and on the firmware of the author's own camera,
and only the resulting facts (operation codes, property codes, encodings) are
documented.

## What it is based on

- **CIPA DC-005** (PTP/IP), the public standard for PTP over TCP/IP.
- **libgphoto2**, whose public header `camlibs/ptp2/ptp.h` (LGPL-2.1) is the
  best public catalogue of vendor PTP extensions. `omtether/ptp_names.json` is
  a generated table of operation/property/event **names and numbers** extracted
  from that header; no libgphoto2 source code is included. Credit and thanks to
  the libgphoto2 project.
- Public documentation and the observable behaviour of the camera over USB and
  Wi-Fi.

## Trademarks

OM System, Olympus, OM-1, OM Capture and OM Share are trademarks of OM Digital
Solutions Corporation. This project is not affiliated with, endorsed by or
supported by OM Digital Solutions. Use it at your own risk; it may void your
warranty.

## Privacy

No serial numbers, MAC addresses, credentials or personal network details are
included in this repository. The camera simulator uses synthetic values, and the
examples in the documentation use reserved/example numbers (`aa:bb:cc:dd:ee:ff`).
