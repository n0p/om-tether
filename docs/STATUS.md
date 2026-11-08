# Project status (hardware test session)

🇬🇧 English · [🇪🇸 Español](ESTADO.md)

Camera: OM System OM-1 Mark II, firmware 1.00. Computer: Arch Linux, Python 3.14.
Everything below was measured against the real camera, not inferred.

## What works

| function | command | notes |
|---|---|---|
| find the camera | `discover` | mDNS `_ptp._tcp`, SSDP, 15740 scan |
| connect over Wi-Fi | `info --host <ip>` | the GUID must carry the paired MAC **reversed** in bytes 10..15 |
| live view | `liveview --host <ip>` | JPEG 1024×768 via `0x9484` |
| real-time download | `daemon --host <ip> --out DIR` | events + polling, atomic download, persistent state, optional upload |
| remote shutter | `remote --host <ip>` | needs PC control mode; see limitation |
| bulb | `remote` → `b <seconds>` | opens with `0x9481(3)`, closes with `0x9481(6)` |
| mobile app API | `app shoot` / `app bulb` / `app live` | HTTP CGI in AP mode (`192.168.0.10`) |

## Real camera limitations

1. **PC control mode (`0xD052=1`)**: the camera grants it in the first session
   after a clean power-up. In later sessions it answers `0x2019`, which in the
   firmware is the setter's **default failure code** (it does not mean
   "busy"); retrying does not help. Within a granted session, shots are
   unlimited.
2. **`0x9481` needs the pair 3 → 6**: 3 presses the shutter and 6 releases it.
   Without the 6 the camera stays with the shutter pressed and rejects the next
   one. Parameter 0 (what libgphoto2 uses for "release") gives `0x201D` here.
3. **Never write `0xD06D = 0` if it is already 0**: it hangs the PTP/IP service
   and drops the camera off the Wi-Fi for minutes. Read first and write only if
   it is on.
4. **Enumeration must be light**: asking `GetObjectInfo` for every object (50+)
   while shooting takes the service down. Two calls per folder are used (all
   handles + format filter `0x3001`).
5. **New objects are announced with event `0xC102`** (param1 = handle), not
   with `0xC002`.
6. The camera drops off the Wi-Fi easily and **does not come back on its own**:
   you have to pick the network in the menu (or restart it).

## Recommended procedures

### Downloading photos shot on the body (the most stable)

```bash
python3 -m omtether daemon --host <ip> --out ~/Pictures/omtether
```

It does not touch the camera mode, does not lock the controls and reconnects
by itself.

### Astrophotography with the shutter driven from the computer

```bash
# 1) power the camera on and pick the Wi-Fi
# 2) first connection after power-up, one single session for the whole night:
python3 -m omtether remote --host <ip> --out ~/Pictures/astro
```

Inside the console: `s` shoots and downloads, `b 300` is a 300 s bulb,
`p/g` write and read properties, `?` is help. Do not close the session: on
reconnect the mode permission is lost.

### The mobile app path (access-point mode)

The camera creates its own Wi-Fi and the computer joins it; everything goes
over HTTP to `192.168.0.10`. It is the path OI.Share uses and it does not
depend on the PC control permission.

```bash
python3 -m omtether app info
python3 -m omtether app mode rec
python3 -m omtether app shoot
python3 -m omtether app bulb 300
python3 -m omtether app live --count 20 --out frames
python3 -m omtether app get /DCIM/100OMSYS/PA300001.JPG --out ~/Pictures/astro
```

## Diagnostic tools

- `tools/sniff.py` — captures UDP 1900/5353 and TCP 15740/80 with packet names.
- `tools/retry_connect.py` — handshake retries with the `InitFail` reason.
- `tools/pair_and_start.py` — pairing + mode transition + wait.
- `tools/wake_and_shoot.py` — waits for live view and shoots.
- Internal analysis notes (not distributed): disassembly of the firmware and of
  the OI.Share APK; only the facts that came out of them are kept here.

## Stacking recipe (verified: 10 shots in a row)

```bash
python3 -m omtether remote --host <ip> --shots 50 --interval 5 \
    --skip-raw --out ~/Pictures/astro
```

- `--skip-raw` downloads only the JPG (half the traffic; plenty for stacking).
- It shoots, waits for the camera's `0xC102` event and downloads at the end of
  the sequence, plus a final sweep for the RAWs that get announced late.
- Shots are unlimited **within the same session**; if the camera drops out the
  client reconnects and retries the pending shot.

Keys that were hard to find and are implemented:

1. The shutter is released with `0x9481(6)`; without it the next shot is
   rejected (it looked like "one photo per power-up").
2. **Do not poll the card while the camera writes the RAW**: listening to the
   event and waiting a few seconds is enough. Polling took it down.
3. Do not trust `0xD052`: shoot and check whether a file appears.
4. Never write `0xD06D = 0` if it is already 0 (hangs the camera for minutes).

## Desktop application (PyQt6)

```bash
/usr/bin/python3 -m omtether gui      # or: python3 -m omtether gui (re-executes itself)
```

Three tabs:

- **Tether (auto-download)**: continuous download of whatever is shot on the
  body. Destination folder and format: `JPEG + RAW`, `JPEG only` or `RAW only`.
- **Remote control**: live view with preview, a shoot button and bulb with
  seconds. Files go to the chosen folder.
- **Astrophotography**: sequence of N shots with interval, format and folder.
  **"Enable INDI service"** box with port (7624 by default).

The interface talks to the camera in separate threads: it does not freeze even
if the camera is slow or drops out. On close, it stops the running jobs.

The interface is available in **English and Spanish**: the texts live in
`omtether/i18n.py` (dicts `ES` and `EN`, same keys) and go through `tr()`. The
language selector in the top bar switches straight away by rebuilding the
window (same IP, same tab) and stores the choice in QSettings (`language`); on
first run the language comes from `LANG`/`LC_ALL`. Messages written by the
library through `logging` stay in Spanish.

## INDI service (KStars / Ekos)

Two ways to start it, with the same code:

```bash
# from the app: Astrophotography tab -> tick "Enable INDI service"
# from the console:
python3 -m omtether remote --host <ip> --indi 7624
```

KStars connects as an INDI server to `localhost:7624`. A single device
`OMTether` is published with:

| property | type | use |
|---|---|---|
| `CONNECTION` | switch | connect/disconnect |
| `DRIVER_INFO` | text | name and version |
| `CCD_EXPOSURE` | number | writing seconds takes a shot |
| `CCD_ABORT_EXPOSURE` | switch | abort |
| `CCD1` | BLOB | JPEG of the last shot (base64) |

The connection to the camera is lazy: it opens on KStars' first shot. The real
exposure time is whatever the camera has set on the body (`0xD01C` is not
touched); the INDI number is used for the state and the timeout.

Tested without a camera: definitions, `CONNECTION`, `CCD_EXPOSURE` → `Busy` →
`Alert` when the camera does not answer. The happy path (JPEG in the BLOB) is
tested with a simulated trigger: 92 KB of valid JPEG.

## Camera bar: IP, search and onboarding

At the top of the window there is a single bar (the IP is shared by the three
tabs and remembered between sessions with QSettings):

- **Camera: [IP]** — can be typed by hand. **Probe** button: checks that port
  15740 answers and says so in green/red.
- **Find camera…** — searches the local network three ways at once: mDNS
  (`_ptp._tcp`), SSDP and a sweep of port 15740 on this computer's networks.
  Lists what it found with its mode (`ptpip` or `http`) and lets you use that
  IP.
- **Pair (USB)…** — the onboarding: read the four slots (`GetConnectPcInfo`),
  pair this computer in the chosen one (`SetConnectPcInfo`) or clear a slot.
  Shows the computer name and the MACs that will be registered (those from
  `primary_macs()`, without docker/veth).
- **Language** — selector (Español / English); applies straight away and is
  remembered.

The onboarding shows the steps on screen: USB → "Camera Control" on the camera
→ read slots → pair → unplug USB and join the camera to the Wi-Fi.

## ISO and exposure

Verified against the camera:

- **ISO**: property `0xD1C0` (uint32) on the OM-1; the value is the ISO number
  as is and `0xFFFFFFFF` is Auto. On earlier bodies it is `0xD007` (uint16).
  The range is read from the descriptor: 80…102400, twelve values.
- **Shutter speed**: property `0xD01C` (uint32). The value is a **fraction**:
  `(numerator << 16) | denominator`, in seconds. Checked examples:
  `1/250` → `0x000100FA`, `2 s` → `0x0014000A`, `30 s` → `0x012C000A`,
  `0.5 s` → `0x000A0014`. Range 1/32000…60 s.
- **Timed exposure**: if the requested time is not in the table, or a long time
  is wanted, `expose()` opens the shutter (`0x9481(3)`), waits and closes it
  (`0x9481(6)`). With the dial on M/Bulb that gives the real time.

INDI publishes `CCD_EXPOSURE` (seconds: sets the speed or does bulb) and
`CCD_ISO` (number, range read from the camera). The astrophotography tab has
ISO and exposure fields; 0 means "leave as is" / "camera setting".

## PTP/IP bridge (darktable, digiKam)

`omtether bridge --host <ip>` (or the checkbox in the Tether tab) exposes the
camera on `127.0.0.1:15740` so libgphoto2 can use it unpatched: it accepts any
initiator GUID, intercepts `0xD052`, synthesises the `0xC102` event after a
capture (the body sends none in station mode) and keeps downloading the photos.
Only `0xC102`, with the handle: libgphoto2 ≤ 2.5.32 reads a `0xC103` with
`Param1 = 0` as "no file" and aborts the shot.

With `--mdns` it advertises `_ptp._tcp` through avahi (which owns port 5353).
darktable still would not see it: it only lists what `gp_abilities_list_detect`
returns, and on Linux the `ptpip:` port carries no host. That is what the
**"Open darktable…"** button in the Tether tab —or `omtether darktable`— is for:
it launches darktable with an `LD_PRELOAD` shim that rewrites that port's path to
`ptpip:127.0.0.1:15740`. Nothing system-wide is touched.

Verified against the camera: `gphoto2 --capture-image-and-download` through the
bridge downloads the ORF with no error, and with the shim `gphoto2 --auto-detect`
lists `PTP/IP Camera  ptpip:127.0.0.1:15740` (the same detection darktable uses).
