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

# 4) desktop app (PyQt6)
python3 -m omtether gui
```

### Packages

Ready-made packages live in `packaging/`:

- **Arch / AUR** — `packaging/arch/PKGBUILD` installs the Python module, the
  `omtether` launcher, the desktop entry, the icon and the user systemd unit.
  Build with `makepkg -si` inside that directory (or install the
  `omtether` AUR package once published).
- **Debian / Ubuntu** — `python3 packaging/debian/build_deb.py` produces
  `dist/omtether_<version>_all.deb`, installable with
  `sudo apt install ./omtether_<version>_all.deb`. It depends only on
  `python3`; the GUI needs `python3-pyqt6` and USB pairing `python3-usb`
  (Recommends).

Both packages add **omtether to the applications menu** (Graphics/Photography),
so you can open the GUI without touching a terminal. In the menu it opens with
`omtether gui`; the daemon is a user service:
`systemctl --user enable --now omtether-daemon` (edit the camera IP in the unit
first).

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

## Other apps: darktable, digiKam, Entangle… (via libgphoto2)

darktable has **no plugin API for tethering**: its tethering view is C code that
goes through libgphoto2 (`gp_abilities_list_detect()` to list cameras,
`gp_camera_init()` to open them), so the way in is libgphoto2, not a darktable
plugin.

The good news is that libgphoto2 already has everything for this body: the
`ptpip` port driver discovers the camera over mDNS, `Olympus:OM-1MarkII` is in
its model table, the capture path is the same `0x9481(3)` + `0x9481(6)` sequence
this project uses, and live view uses `0xD06D`. Only one thing does not match:
the **initiator GUID**. libgphoto2 invents a random one and stores it in
`~/.gphoto/settings`, while the camera only accepts `00*10 + reversed(paired
MAC)`, so it answers `InitFail 1`.

`omtether gphoto` writes the right GUID where libgphoto2 will read it:

```bash
omtether gphoto --host <camera-ip>            # show the candidates
omtether gphoto --host <camera-ip> --write    # test them and store the accepted one
gphoto2 --camera "PTP/IP Camera" --port ptpip:<camera-ip> --summary
darktable                                     # the camera shows up in tethering
```

The command tries each local MAC against the camera, so the value it stores is
the one the camera really accepts. If none is accepted, pair the computer first
(`omtether pair --slot N`) — the slot is what the camera compares against.

Caveat: libgphoto2 reads the card right after the shot. On this body that can
drop the PTP/IP link while the RAW is still being written; if darktable loses
the camera after a few frames, fall back to `omtether daemon` plus darktable's
import-from-folder. A proper upstream fix (try the local MACs instead of a
random GUID, and wait for the `0xC102` event before touching the card) belongs
in `camlibs/ptp2/ptpip.c`.

## Using the camera from darktable, digiKam… (PTP/IP bridge)

darktable has no tethering plugin API: it drives libgphoto2, and libgphoto2's
`ptp2` driver trips on three things with this body — it wants the initiator GUID
to carry a paired MAC, it writes `0xD052` at init (which drops the camera off
Wi-Fi) and in station mode the camera takes the photo **without announcing any
event**, so libgphoto2 waits 65 s and reports failure.

Instead of patching libgphoto2, `omtether` can sit in the middle:

```
darktable ── gphoto2 ──> 127.0.0.1:15740 (bridge) ──> camera
```

Tick **“PTP/IP bridge (darktable, digiKam, gPhoto2)”** in the Tether tab, or run
it from the console:

```bash
omtether bridge --host <camera-ip> --out ~/Pictures/omtether --mdns
```

The bridge accepts whatever GUID libgphoto2 sends, forwards the operations,
answers `0xD052` writes without touching the camera, watches the card after a
capture and **synthesises** the `0xC103`/`0xC102` events libgphoto2 is waiting
for. With `--mdns` it also advertises `_ptp._tcp`, so darktable — which only
lists auto-detected cameras — shows it with no configuration. It keeps
downloading every new photo to the output directory at the same time, so the
same process is still the tethering daemon.

Then:

```bash
gphoto2 --camera "PTP/IP Camera" --port ptpip:127.0.0.1 --summary
```

or open darktable and go to the tethering view.

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

## What was reimplemented, and from where

Two original apps were studied and their **behaviour** was reimplemented from
scratch: no vendor code is used, only the protocol facts recovered from their
traffic and from the camera itself.

**OM Capture** (Windows desktop app, Wi-Fi tethering — the PTP/IP path)

| in the original | here |
|---|---|
| pairing the computer with the camera over USB, four slots | `omtether pair`, and “Pair (USB)…” in the GUI |
| live view window | `liveview` (MJPEG over HTTP), “Remote control” tab |
| shutter button and bulb | `remote`, `expose()` (timed exposure) |
| ISO, shutter speed, image quality | ISO (`0xD1C0`) and shutter (`0xD01C`), read and written |
| automatic download of every shot | `daemon` (“Tether” tab) |
| browsing and downloading what is on the card | `capture`, `watch` |
| keeping the link alive and reconnecting | `0x94BA` keep-alive, reconnect with backoff |

**OI.Share** (phone app, camera's own Wi-Fi access point — the HTTP path)

| in the original | here |
|---|---|
| switching the camera mode | `omtether app mode …` |
| shooting (with or without AF), bulb start/stop | `app shoot`, `app bulb` |
| live view over HTTP | `app liveview` |
| reading and writing camera settings by name | `app props`, `app set` |
| listing and downloading images | `app ls`, `app get` |

Out of scope: video recording control, firmware update, GPS tagging and myset
backup/restore.

## Status

Everything below has been run against a real **OM-1 Mark II (firmware 1.00)**,
over Wi-Fi in station mode, with the USB cable only for the initial pairing:

| Piece | Status |
|---|---|
| PTP/IP transport (handshake, event channel, data phases) | ✅ verified on hardware and against the simulator |
| USB pairing (`0x94BF` SetConnectPcInfo) | ✅ slot written and read back from the camera |
| PTP session (DeviceInfo, properties, objects) | ✅ 48 operations over Wi-Fi (60 over USB) |
| Discovery (mDNS, SSDP, port 15740 scan) | ✅ finds the camera on the LAN |
| Live view (`0x9484`) | ✅ JPEG 1024×768, ~1 fps |
| Shutter + download (`0x9481`, events, `GetObject`) | ✅ 10 shots in a row, JPEG and RAW |
| Real-time download (`daemon`) | ✅ 3 JPG + 3 ORF (101 MB) in ~40 s |
| ISO and shutter speed | ✅ written and read back (3200, 1/250, 2 s, 30 s) |
| Timed exposure (bulb) | ✅ 2-3 s exposures |
| INDI server | ✅ BLOB verified; same exposure path as the CLI |
| Desktop app (PyQt6) | ✅ three tabs, bilingual, live view, menu entry |

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

This is the sequence that was actually run on the OM-1 Mark II, and the one to
repeat on another body. With the camera in Wi-Fi tether mode and on the same
network as the computer:

```bash
sudo tcpdump -ni any 'port 15740 or port 1900' &   # see what the camera emits
python3 -m omtether discover -v
python3 -m omtether info --host <ip> -vv
python3 -m omtether liveview --host <ip> --http 8080   # panel in the browser
python3 -m omtether remote --host <ip> --shots 10 --interval 2 --skip-raw
python3 -m omtether daemon --host <ip> --out ~/Pictures/omtether
```

If `info` answers, the rest works the same way: live view first, then a short
series, then the daemon while you shoot on the body.

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
| remote shutter | ✅ | `0x9481(3)` … `0x9481(6)`, event `0xC102`; 10 shots in a row |
| PC control mode | ⚠️ | granted **once per power-up**; **not needed** for the shutter |
| ISO / shutter speed | ✅ | written and read back from the camera |
| timed exposure | ✅ | 2-3 s exposures, shutter opened and closed by the client |

### About PC control mode (`0xD052`)

The camera accepts `0xD052 = 1` once per power-up, and only with live view off
(with `0xD06D != 0` the write returns `0x2019`). It is **not required** for the
shutter: `0x9481` works in plain station mode. What does hurt is polling the
card while the camera is writing the RAW — it can drop off Wi-Fi — so the client
waits for the `0xC102` event and looks at the card once, a few seconds later.

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
