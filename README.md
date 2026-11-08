🇬🇧 English · [🇪🇸 Español](README.es.md)

# omtether — tethering for OM System / Olympus cameras on Linux

A free reimplementation of the protocol used by **OM Capture** (OM Digital
Solutions) for Windows, so you can shoot and download photos from Linux, both
over USB and over **Wi-Fi**.

## Install and quick start

There is no package: run it from the repo directory.

```bash
cd ~/src/om-tether

# 1) find the camera and pair it once (over USB)
python3 -m omtether discover
python3 -m omtether pair --list          # show the 4 slots
python3 -m omtether pair --slot 2        # pair this computer
python3 -m omtether info --host <ip>     # check PTP/IP

# 2) auto-download whatever you shoot on the body
python3 -m omtether daemon --host <ip> --out ~/Pictures/omtether

# 3) remote control / astrophotography
python3 -m omtether remote --host <ip> --shots 20 --interval 5 --skip-raw
python3 -m omtether remote --host <ip> --shots 20 --interval 5 --exposure 2 --iso 1600
python3 -m omtether remote --host <ip> --indi        # INDI service on :7624

# 4) desktop application (PyQt6)
python3 -m omtether gui
```

### The desktop application

**How to launch it**: `python3 -m omtether gui` from the repo directory. If the
`python3` on your `PATH` lacks PyQt6, the command re-executes itself with
`/usr/bin/python3`, which is where it is installed (Python 3.14 + Qt 6.12).
`/usr/bin/python3 -m omtether gui` works directly too.

**Top bar** (shared by the three tabs): a field with the **camera IP**
(remembered between sessions), a **Probe** button (checks port 15740),
**Find camera…** (mDNS `_ptp._tcp`, SSDP and a sweep of 15740) and
**Pair (USB)…** (onboarding: read/write/clear the `ConnectPcInfo` slots).
On the right there is a **language** selector (English / Español): changing it
rebuilds the window in the new language straight away, keeping the camera IP
and the current tab, and the choice is remembered between sessions. The first
time, the language comes from `LANG`/`LC_ALL` (Spanish if it starts with `es`,
English otherwise).

**Tabs**:

| Tab | What it does |
|---|---|
| **Tether (auto-download)** | Continuous download of whatever you shoot on the body. Destination folder and format: `JPEG + RAW`, `JPEG only` or `RAW only`. |
| **Remote control** | **Live view** preview (JPEG 1024×768, ~1 fps) in the large panel, a **Shoot** button and **Bulb** with seconds. |
| **Astrophotography** | Sequence of N shots with interval, ISO and exposure (0 = leave as is / camera setting), format `JPEG + RAW` / `JPEG only` / `RAW only`, plus the **Enable INDI service** box for KStars/Ekos. |

Live view starts with **Connect** in the remote control tab; the panel shows
`no live view` until the first frame arrives. All camera traffic runs in
separate threads, so the window never freezes even if the camera is slow or
drops out, and closing the window stops the running jobs.

The interface texts live in `omtether/i18n.py` (two dictionaries, `ES` and
`EN`). Messages coming from the library through `logging` are still in
Spanish.

## What is inside the original installer

`OCSetup.exe` is a bootstrapper with an embedded CAB:

```
OCSetup.exe
└── outer.cab
    ├── OCSetup.exe          (stub)
    ├── setup.ini
    ├── MSI/OMDS Install.msi (custom actions in C++)
    ├── MSI/OC_Pack_Framework.cab
    │   └── application.cab  ← the application (129 MB)
    └── Data/VC_redist.*.exe
```

Inside `application.cab` is the MFC application (`OM Capture.exe`,
`OlyCameraLib.dll`, `OlyComSvc.exe`, ...). What matters for the protocol:

* `OlyCameraLib.dll` is the camera "SDK": it talks to the **WPD** stack
  (Windows Portable Devices) and imports `iphlpapi.dll` to read the local IPs.
  It exposes `IOlyCamera::*` and contains the names of the proprietary
  operations (`OLYSetConnectPCInfo`, `OLYTransferModeStartStop`,
  `OLYGetUntransferObjectList`, `StartStationMode`, ...), the event codes
  `OLY_EVCMD_*` (0xC001–0xC114) and the VIDs `33A2` (OM Digital) / `07B4`
  (Olympus).
* Not a single `ws2_32.dll` import in the whole application: **all networking
  is provided by Windows**. Microsoft documents that its "MTP/IP" transport
  for WPD is the **CIPA PTP/IP binding** plus **UPnP** for discovery and
  pairing ("we used the PTP/IP binding, then used UPnP for pairing and
  discovery", WPD team blog). In other words: the camera speaks standard
  PTP/IP (TCP 15740).
* The proprietary Olympus/OMD operations are already documented in libgphoto2
  (`camlibs/ptp2/ptp.h`), which got them from OM Capture traces: `0x9481`
  shutter, `0x9484` live view, `0x9486`/`0x948A` property polling,
  `0x94BA`–`0x94C4` transfer/station mode and ConnectPcInfo. This project uses
  those codes (they are facts, not copied code).

## Status

| Piece | Status |
|---|---|
| PTP/IP transport (handshake, event channel, data phases) | implemented and tested against a simulated server (`tests/`) |
| PTP session (DeviceInfo, properties, objects) | implemented |
| Discovery (SSDP + port 15740 scan) | implemented |
| Live view (`0x9484`) | implemented |
| Shutter + download (`0x9481`, events, `GetObject`) | implemented |
| Real-time download (`watch`) | implemented |
| Wi-Fi pairing (`0x94BF` SetConnectPcInfo over USB) | layout recovered from the binary; still to be validated on hardware |
| Tested against hardware | **pending** |

## Usage

```bash
python3 -m omtether discover                 # find the camera on the LAN
python3 -m omtether info --host 192.168.1.50 -v
python3 -m omtether liveview --host 192.168.1.50 --http 8080     # watch in the browser
python3 -m omtether liveview --host 192.168.1.50 --out /tmp/lv   # save frames
python3 -m omtether capture --host 192.168.1.50 --count 3 --out ~/Pictures/session
python3 -m omtether watch --host 192.168.1.50 --out ~/Pictures/session
python3 -m omtether watch --host 192.168.1.50 --out ~/Pictures/session --keepalive 5
python3 -m omtether raw --host 192.168.1.50 --op 0x9486   # poke the protocol
```

`watch` is the real-time download: it listens to the camera events and
downloads each photo as soon as it appears, with a `GetObjectHandles` poll as
a safety net.

`discover` tries, in this order: **mDNS `_ptp._tcp`** (what the camera itself
announces), SSDP, a scan of 15740, and the camera's AP address
(`192.168.0.10`). Each result carries its mode: `ptpip` (tethering) or `http`
(OI.Share).

`liveview --http 8080` starts an MJPEG server: open `http://localhost:8080/`
in the browser and there is your live viewer.

## Tests without a camera

`tests/mock_camera.py` is a fake PTP/IP camera (handshake, DeviceInfo,
properties, live view, shutter with event and download). It validates the
transport, the session, capture, MJPEG and `watch`:

```bash
python3 tests/run_all.py     # 8/8
```

`raw` is for exploring the camera: it sends any opcode with parameters and
shows the response and the data. It is the tool to finish mapping whatever is
missing.

## Wi-Fi mode: how OM Capture pairs

The interface texts (extracted from `i18n/ENU/app.loc`) describe:

1. Connect the camera to the computer **over USB** and choose *Camera Control*
   on the camera.
2. Unplug the USB and join the camera to the **Wi-Fi access point**.
3. The computer opens the firewall for the matching network profile
   (*Domain/Private/Public*), which implies inbound traffic to the computer.

That is: the computer registers its details in the camera over USB (operation
`0x94BF`) and afterwards, over Wi-Fi, PTP/IP is spoken. The record is
**{computer name + MACs of the Ethernet/Wi-Fi adapters}**, in one of the 4
slots the camera keeps:

```
u8   nameLen           name length (0..15)
u8   name[nameLen]     ANSI, no NUL
u32  count             number of adapters (0..5)
u8   entries[count][8] big-endian MAC + two zero bytes
```

One PTP parameter: `slot - 1`. `omtether pair` builds that record with the
computer name and the real MACs, writes it and reads it back with `0x94C0` to
check that the camera returns the same thing:

```bash
python3 -m omtether pair --dry-run          # show the payload without touching the camera
python3 -m omtether pair --slot 1           # write (camera over USB in Camera Control)
python3 -m omtether pair --list             # dump the 4 slots
```

## The two Wi-Fi modes (they are not the same path)

The firmware distinguishes several link kinds (`WIFI_LINK_KIND_PRIVATE 0`,
`AUTO 1`, `EASY_TRANS 2`, `STA_MODE`) and an application mode
(`WIFI_APP_MODE_CAMERA_CONTROL`). In practice there are two ways to connect it:

| | Camera's own AP ("private") | Infrastructure (`STA_MODE`) |
|---|---|---|
| Who is the AP | the camera | your router |
| Camera IP | `192.168.0.10`, with its own DHCP | whatever the router gives |
| Discovery | join its SSID; UPnP and CGI on port 80 | SSDP + scan; PTP/IP on 15740 |
| API | **OI.Share HTTP CGI** (62 CGIs: `get_caminfo`, `get_imglist`, `exec_takemotion`, `exec_takemisc?com=startliveview&port=…`, `OLYSetConnectPCInfo`…) | **PTP/IP + 0x94xx extensions** |
| What it is for here | identify the camera and, if needed, pair without USB (`omtether http-info`) | full tethering: shutter, live view and real-time download |

**Tethering as OM Capture does it is the second one**: the camera joins the
AP, enters *Camera Control* and the computer speaks PTP/IP. According to OM
Digital, access-point transfer is supported on **OM-1, OM-1 Mark II, E-M1X and
E-M1 Mark III**; the other Wi-Fi bodies stay in OI.Share's AP/private mode
(HTTP). The firmware confirms it on its side: the HTTP server limits itself
with `[httpd]not_CONN_MODE_TRANSFER`, so in Camera Control no CGI works.

`omtether discover` looks at **both**: if it finds 15740 it marks it `ptpip`
(tethering) and if it finds 80 with OI.Share CGI it marks it `http`. When only
`http` shows up, the camera is in AP/private mode and the warning tells you so.
Whether the camera also exposes 15740 in AP mode remains to be verified on
hardware; `discover` itself will answer that as soon as you have it in front
of you.

## Field test

With the camera in Wi-Fi tether mode and on the same network as the computer:

```bash
sudo tcpdump -ni any 'port 15740 or port 1900' &   # see what the camera emits
python3 -m omtether discover -v
python3 -m omtether info --host <ip> -vv
```

If `info` answers, the rest (live view, shutter, download) can be validated
right away with the commands above.

## Layout

```
omtether/
  ptpip.py      PTP/IP transport (CIPA DC-005)
  ptp.py        PTP session (operations, properties, objects)
  olympus.py    OM System/Olympus extensions (0x94xx, D0xx properties)
  discovery.py  SSDP + port scan
  cli.py        command-line interface
  pairing.py    Wi-Fi pairing (USB)
  gui.py        desktop application (PyQt6)
  gui_setup.py  network scan and USB pairing dialogs
  i18n.py       interface texts (Spanish / English)
tests/          simulated camera + transport test
docs/           protocol notes (docs/STATUS.md, docs/ESTADO.md)
```

## Real status (measured on an OM-1 Mark II, fw 1.00)

What works today, over Wi-Fi, against the camera:

| function | status | notes |
|---|---|---|
| PTP/IP handshake | ✅ | the GUID must carry the paired MAC **reversed** in bytes 10..15 |
| `info` (DeviceInfo) | ✅ | 48 operations over Wi-Fi (60 over USB) |
| live view | ✅ | `0x9484`, JPEG 1024×768 |
| real-time download | ✅ | `daemon`: events + polling, atomic download, persistent state |
| remote shutter | ⚠️ | `0x9481(3)` **only in PC control mode** (`0xD052=1`) |
| PC control | ⚠️ | the camera grants it **once per power-up**; afterwards `DeviceBusy` |

### The remote shutter limitation

`0xD052 = 1` is accepted once per camera power-up (with live view off: with
`0xD06D != 0` the write returns `0x2019`). After the first shot the property
goes back to 2 and there is no way to get it back without a restart. With the
mode at 2, `0x9481` answers `0x2001` or `0x201D` but **produces no file**: an
ACK is not a photo.

### Astrophotography recipe (the one that works today)

Since the camera can do the interval itself, the natural split is:

1. Set up **interval shooting** on the camera (or shoot by hand).
2. Leave the daemon downloading:
   ```bash
   python3 -m omtether daemon --host <ip-de-la-cámara> --out ~/Pictures/astro \
       --upload-dir ~/sync/astro
   ```
   Each photo shows up on the computer within seconds, without touching the
   camera mode and without locking any control.

For a single photo controlled from the computer, `remote --shots 1` works in
the first session after powering the camera on. The interactive mode (`remote`
without `--shots`) keeps the session open with keep-alives for a whole night.

## Trademarks

OM System, Olympus, OM-1, OM Capture and OM Share are trademarks or registered
trademarks of OM Digital Solutions Corporation. This project is not affiliated
with, endorsed by or sponsored by OM Digital Solutions. All other trademarks are
the property of their respective owners.

## License

MIT — see [LICENSE](LICENSE). You can use, modify and redistribute it,
including inside closed-source software, keeping the copyright notice.
