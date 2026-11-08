"""Command line interface for omtether."""

from __future__ import annotations

import argparse
import json
import signal
import logging
import os
import sys
import time
from pathlib import Path

from . import discovery, olympus, ptp
from .ptpip import PtpIpError, PtpIpTransport

log = logging.getLogger("omtether")

NAMES_PATH = Path(__file__).with_name("ptp_names.json")
try:
    NAMES = json.loads(NAMES_PATH.read_text())
except OSError:  # pragma: no cover
    NAMES = {"OC": {}, "DPC": {}, "EC": {}}


def op_name(code: int) -> str:
    return NAMES["OC"].get(str(code), "0x%04x" % code)


def prop_name(code: int) -> str:
    return NAMES["DPC"].get(str(code), "0x%04x" % code)


def event_name(code: int) -> str:
    return NAMES["EC"].get(str(code)) or olympus.event_name(code)


# --------------------------------------------------------------------- setup
def make_session(args) -> tuple[PtpIpTransport, ptp.PtpSession]:
    transport = PtpIpTransport(args.host, args.port, args.event_port, name=args.name, timeout=args.timeout)
    transport.connect()
    session = ptp.PtpSession(transport)
    session.open()
    return transport, session


def _fmt_prop(code: int, raw: bytes) -> str:
    if len(raw) == 1:
        val = str(raw[0])
    elif len(raw) == 2:
        val = "0x%04x (%d)" % (int.from_bytes(raw, "little"), int.from_bytes(raw, "little"))
    elif len(raw) == 4:
        val = "0x%08x (%d)" % (int.from_bytes(raw, "little"), int.from_bytes(raw, "little"))
    else:
        val = raw[:32].hex()
    return "%-40s %s" % (prop_name(code), val)


# ------------------------------------------------------------------ commands
def cmd_discover(args) -> int:
    deadline = time.monotonic() + args.wait if args.wait else 0
    while True:
        found = discovery.discover(timeout=args.timeout, do_scan=not args.ssdp_only, extra_net=args.subnet)
        if found:
            for f in found:
                mode = f.mode or ("ptpip" if f.port == PTPIP_PORT else "http")
                print("%-16s :%-5d  %-4s  %s %s" % (f.host, f.port, mode, f.source, f.detail))
            if all((f.mode or "ptpip") == "http" for f in found):
                print("\nsolo OI.Share (HTTP): la cámara está en modo AP/privado.")
                print("para tethering PTP/IP ponla en Camera Control y conéctala al AP "
                      "(infraestructura).")
            return 0
        if not args.wait or time.monotonic() >= deadline:
            print("no cameras found (is the camera in Wi-Fi tether mode and on the same network?)")
            return 1
        time.sleep(2)


def cmd_info(args) -> int:
    transport, session = make_session(args)
    try:
        di = session.info
        assert di is not None
        print("manufacturer : %s" % di.manufacturer)
        print("model        : %s" % di.model)
        print("firmware     : %s" % di.device_version)
        print("serial       : %s" % di.serial)
        print("vendor ext   : 0x%08x (%s)" % (di.vendor_extension_id, di.vendor_extension_desc))
        print("operations   : %d" % len(di.operations))
        print("events       : %d" % len(di.events))
        print("properties   : %d" % len(di.properties))
        if args.verbose:
            print("\n-- operations --")
            for op in di.operations:
                print("  0x%04x %s" % (op, op_name(op)))
            print("\n-- events --")
            for ev in di.events:
                print("  0x%04x %s" % (ev, event_name(ev)))
            print("\n-- properties --")
            for prop in di.properties:
                print("  0x%04x %s" % (prop, prop_name(prop)))
    finally:
        session.close()
        transport.close()
    return 0


def cmd_props(args) -> int:
    transport, session = make_session(args)
    try:
        di = session.info
        assert di is not None
        codes = [int(c, 0) for c in args.codes.split(",")] if args.codes else di.properties
        for code in codes:
            try:
                raw = session.op(ptp.OC_GetDevicePropValue, [code], expect_data=True)[2]
            except ptp.PtpError as exc:
                print("%-40s <%s>" % (prop_name(code), exc))
                continue
            print(_fmt_prop(code, raw))
    finally:
        session.close()
        transport.close()
    return 0


def cmd_raw(args) -> int:
    params = [int(p, 0) for p in args.params.split(",")] if args.params else []
    data = bytes.fromhex(args.data) if args.data else None
    transport, session = make_session(args)
    try:
        rc, rparams, out = session.op(int(args.op, 0), params, data_out=data,
                                      expect_data=not args.no_data, check=False)
        print("rc     = 0x%04x %s" % (rc, ptp.RC_NAMES.get(rc, "")))
        print("params = %s" % [hex(p) for p in rparams])
        print("data   = %d bytes" % len(out))
        if out:
            if args.out:
                Path(args.out).write_bytes(out)
                print("saved  -> %s" % args.out)
            else:
                print(out[:64].hex())
    finally:
        session.close()
        transport.close()
    return 0


def cmd_http_info(args) -> int:
    """Identify a camera that is in AP / OI.Share mode (HTTP CGI API)."""
    from . import oishare
    info = oishare.camera_info(args.host, args.port)
    ok = False
    for cgi, (status, body) in info.items():
        print("== %s -> HTTP %s, %d bytes" % (cgi, status or "sin respuesta", len(body)))
        if status == 200:
            ok = True
            print(body[:1500].strip())
    if not ok:
        print("\nla API OI.Share no responde. Si la cámara está en Camera Control "
              "(tethering) esto es normal: usa `omtether info --host ...`.")
        return 1
    return 0


def cmd_capture(args) -> int:
    outdir = Path(args.out).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    transport, session = make_session(args)
    oly = olympus.Olympus(session)
    rc = 0
    try:
        oly.enter_pc_mode()
        for i in range(args.count):
            print("capture %d/%d ..." % (i + 1, args.count), flush=True)
            handles = oly.capture()
            if not handles:
                print("  no object announced")
                rc = 1
                continue
            for handle in handles:
                path = download_object(session, handle, outdir)
                print("  -> %s" % path)
            if args.interval:
                time.sleep(args.interval)
    finally:
        oly.exit_pc_mode()
        session.close()
        transport.close()
    return rc


def download_object(session: ptp.PtpSession, handle: int, outdir: Path, attempts: int = 5) -> Path:
    """Fetch one object, retrying while the camera is still writing it."""
    info = None
    data = b""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            info = session.get_object_info(handle)
            if info.size == 0:
                raise ptp.PtpError(ptp.RC_DeviceBusy, "object still empty")
            data = session.get_object(handle)
            if len(data) < info.size:
                raise ptp.PtpError(ptp.RC_IncompleteTransfer,
                                   "got %d of %d bytes" % (len(data), info.size))
            break
        except ptp.PtpError as exc:
            last_error = exc
            log.debug("download of 0x%x failed (attempt %d/%d): %s", handle, attempt + 1, attempts, exc)
            time.sleep(0.3 * (attempt + 1))
    else:
        raise ptp.PtpError(getattr(last_error, "code", ptp.RC_GeneralError),
                           "could not download object 0x%x: %s" % (handle, last_error))

    assert info is not None
    name = info.filename or ("obj_%08x" % handle)
    name = os.path.basename(name.replace("\\", "/"))
    path = outdir / name
    if path.exists():
        stem, suffix = path.stem, path.suffix
        n = 1
        while path.exists():
            path = outdir / ("%s_%d%s" % (stem, n, suffix))
            n += 1
    path.write_bytes(data)
    log.debug("downloaded %s (%d bytes, fmt 0x%04x)", path, len(data), info.format)
    return path


def cmd_watch(args) -> int:
    """Wait for new images and download them as they appear (real-time dump)."""
    outdir = Path(args.out).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    transport, session = make_session(args)
    oly = olympus.Olympus(session)
    known = set(session.list_object_handles())
    seen = set(known)
    print("watching for new images (already %d objects on card) -> %s" % (len(known), outdir))
    last_poll = 0.0
    last_alive = 0.0
    t0 = time.monotonic()
    pending_shots = args.capture
    next_shot = t0 + 2.0
    try:
        while True:
            if args.duration and time.monotonic() - t0 > args.duration:
                print("duration reached")
                break
            if pending_shots > 0 and time.monotonic() >= next_shot:
                pending_shots -= 1
                next_shot = time.monotonic() + 2.0
                try:
                    handles = oly.capture()
                    print("  shot fired, camera announced %s" % [hex(h) for h in handles])
                except Exception as exc:  # noqa: BLE001
                    print("  capture failed: %s" % exc)
            ev = transport.get_event(0.5)
            handles: list[int] = []
            if ev is not None:
                print("event %s %s" % (event_name(ev.code), [hex(p) for p in ev.params]))
                if ev.code in (olympus.EC_ObjectAdded, olympus.EC_ImageRecordFinish,
                               olympus.EC_DirectStoreImage, ptp.EC_ObjectAdded) and ev.params:
                    handles = [ev.params[0]]
            if args.keepalive and time.monotonic() - last_alive > args.keepalive:
                last_alive = time.monotonic()
                oly.keepalive()
            if time.monotonic() - last_poll > args.poll:
                last_poll = time.monotonic()
                try:
                    handles.extend(h for h in session.list_object_handles() if h not in seen)
                except ptp.PtpError:
                    pass
            for handle in handles:
                if not handle or handle in seen:
                    continue
                seen.add(handle)
                try:
                    info = session.get_object_info(handle)
                except ptp.PtpError as exc:
                    print("  object 0x%x vanished: %s" % (handle, exc))
                    seen.discard(handle)
                    continue
                if info.size == 0 or info.format == 0x3001:
                    seen.discard(handle)
                    continue
                try:
                    path = download_object(session, handle, outdir)
                except ptp.PtpError as exc:
                    print("  could not download 0x%x: %s (will retry)" % (handle, exc))
                    seen.discard(handle)
                    continue
                print("  saved %s (%.1f KB)" % (path, info.size / 1024))
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        session.close()
        transport.close()
    return 0


def cmd_gui(args) -> int:
    """Lanza la interfaz gráfica (necesita PyQt6, en el Python del sistema)."""
    try:
        from .gui import main as gui_main
    except ImportError as exc:
        if sys.executable != "/usr/bin/python3":
            print("PyQt6 no está en este intérprete; relanzando con /usr/bin/python3",
                  file=sys.stderr)
            os.execv("/usr/bin/python3",
                     ["/usr/bin/python3", "-m", "omtether", "gui"])
        # lanzada desde el menú de aplicaciones no hay terminal donde leer el
        # error: se avisa con un diálogo si hay alguno disponible
        msg = ("No se puede abrir la interfaz gráfica: falta PyQt6.\n\n"
               "Instala python-pyqt6 (Arch) o python3-pyqt6 (Debian) y vuelve "
               "a intentarlo.\n\n%s" % exc)
        print(msg, file=sys.stderr)
        for cmd in (["zenity", "--error", "--no-wrap", "--title=omtether", "--text=" + msg],
                    ["kdialog", "--title", "omtether", "--error", msg],
                    ["xmessage", "-center", msg]):
            try:
                os.execvp(cmd[0], cmd)
            except OSError:
                continue
        return 2
    return gui_main()


def cmd_bridge(args) -> int:
    """Puente PTP/IP para que libgphoto2 (darktable, digiKam…) use la cámara."""
    import threading as _th
    from pathlib import Path as _Path

    from .bridge import Bridge, BridgeConfig

    outdir = _Path(args.out).expanduser() if args.out else None
    cfg = BridgeConfig(host=args.host, host_port=args.host_port,
                       listen_host=args.listen, listen_port=args.port,
                       outdir=outdir, download=not args.no_download, mdns=args.mdns)
    bridge = Bridge(cfg)
    bridge.start()
    print("puente en ptpip:%s:%d -> cámara %s" % (args.listen, args.port, args.host))
    if outdir:
        print("descarga en %s" % outdir)
    if args.mdns:
        print("anunciado por mDNS (_ptp._tcp) para que darktable lo descubra")
    print("en otra terminal:  gphoto2 --camera \"PTP/IP Camera\" --port ptpip:%s"
          % args.listen)
    try:
        while True:
            _th.Event().wait(1.0)
    except KeyboardInterrupt:
        print("\ncerrando")
    finally:
        bridge.stop()
    return 0


def cmd_darktable(args) -> int:
    """Abre darktable con el shim que hace visible el puente PTP/IP."""
    from . import darktable

    if args.check:
        print("shim:", darktable.ensure_shim())
        out = darktable.detect(host=args.host, port=args.port)
        print(out if out else "no se pudo comprobar (¿falta libgphoto2?)")
        return 0 if out and "PTP/IP Camera" in out else 1
    return darktable.launch(args.extra, host=args.host, port=args.port)


def cmd_gphoto(args) -> int:
    """Configura libgphoto2 (darktable, digiKam…) para hablar con la cámara."""
    from . import gphoto

    current = gphoto.read_setting()
    print("fichero de ajustes: %s" % gphoto.SETTINGS)
    print("ptp2_ip=guid actual: %s" % (current or "(no hay)"))
    cands = gphoto.candidates()
    if not cands:
        print("no hay interfaces de red con MAC utilizable")
        return 1
    print("\ncandidatos (MAC local -> GUID que espera la cámara):")
    for mac, guid in cands:
        mark = " *" if current and gphoto.parse_guid(current) == guid else ""
        print("  %-17s  %s%s" % (mac, gphoto.guid_text(guid), mark))

    chosen: tuple[str, bytes] | None = None
    if args.host:
        print("\nprobando el handshake contra %s..." % args.host)
        for mac, guid in cands:
            ok = gphoto.probe(args.host, guid, timeout=args.timeout)
            print("  %-17s  %s" % (mac, "aceptado" if ok else "rechazado"))
            if ok and chosen is None:
                chosen = (mac, guid)
        if chosen is None:
            print("\nningún GUID fue aceptado: la cámara no responde o esta MAC no")
            print("está en ninguna ranura; empareja con `omtether pair --slot N`.")
            return 2
    else:
        chosen = cands[0]

    mac, guid = chosen
    text = gphoto.guid_text(guid)
    print("\nelegido: %s -> %s" % (mac, text))
    if args.write:
        gphoto.write_setting(text)
        print("escrito en %s" % gphoto.SETTINGS)
        print("\nahora:")
        print('  gphoto2 --camera "PTP/IP Camera" --port ptpip:%s --summary'
              % (args.host or "<ip-de-la-cámara>"))
        print("  darktable   -> la cámara sale en la sala de tethering")
    else:
        print("(no se ha escrito nada; añade --write)")
        print("línea para %s:" % gphoto.SETTINGS)
        print("  ptp2_ip=guid=%s" % text)
    return 0


def cmd_app(args) -> int:
    """API HTTP de la app móvil (OI.Share): la cámara crea su propia Wi-Fi."""
    from .cgi import CgiError, Oishare

    cam = Oishare(host=args.host, timeout=args.timeout)
    action = args.action
    try:
        if action == "mode":
            print(cam.cammode(args.value, lvqty=args.lvqty))
        elif action == "shoot":
            print("disparando (%s)..." % ("con AF" if not args.no_af else "directo"))
            print(cam.shoot(af=not args.no_af))
        elif action == "bulb":
            cam.bulb(float(args.value))
            print("bulb de %s s completado" % args.value)
        elif action == "stop":
            print(cam.stop_take())
        elif action == "prop":
            print(cam.camprop(args.value) if args.value else "falta el nombre")
        elif action == "setprop":
            name, _, value = args.value.partition("=")
            print(cam.set_camprop(name, value))
        elif action == "list":
            print(cam.imglist(args.value or "/DCIM"))
        elif action == "get":
            outdir = Path(args.out).expanduser()
            print(cam.download(args.value, outdir))
        elif action == "live":
            outdir = Path(args.out).expanduser() if args.out else None
            if outdir:
                outdir.mkdir(parents=True, exist_ok=True)
            cam.liveview_start(args.port)
            if args.lvqty:
                cam.cammode("rec", lvqty=args.lvqty)
            print("live view UDP en el puerto %d" % args.port, flush=True)
            try:
                shown = 0
                t0 = time.monotonic()
                while args.count == 0 or shown < args.count:
                    frame = cam.liveview_frame(timeout=5.0)
                    if frame is None:
                        continue
                    shown += 1
                    if outdir:
                        (outdir / ("lv_%05d.jpg" % frame.index)).write_bytes(frame.data)
                    if shown % 10 == 0 or shown == 1:
                        print("  %d frames (%.1f fps)" % (
                            shown, shown / max(time.monotonic() - t0, 0.001)), flush=True)
            finally:
                cam.liveview_stop()
            if outdir:
                print("guardados en %s" % outdir)
        elif action == "info":
            for name in ("takemode", "drivemode", "shutspeedvalue", "isospeedvalue",
                         "focalvalue", "expcomp", "wbvalue"):
                try:
                    print("%-14s %s" % (name, cam.camprop(name)))
                except CgiError as exc:
                    print("%-14s error: %s" % (name, exc))
        else:
            print("acción desconocida: %s" % action)
            return 2
    except CgiError as exc:
        print("CGI error: %s" % exc, file=sys.stderr)
        return 2
    return 0


def _run_indi(cfg, port: int) -> int:
    """Modo astro driven by an INDI client (KStars/Ekos)."""
    import threading as _th

    from .indi import IndiServer
    from .remote import Remote

    r = Remote(cfg)
    lock = _th.Lock()

    def shoot(seconds: float, abort) -> bytes:
        with lock:
            # conexión perezosa: el primer disparo de KStars abre la sesión
            if r.session is None:
                r.connect()
                r.enter(timeout=10)
            files = r.expose(seconds, abort)
            jpg = next((p for p in files if p.suffix.lower() in (".jpg", ".jpeg")), None)
            if jpg is None:
                raise RuntimeError("la cámara no devolvió JPEG")
            print("  INDI: %s (%.1fs)" % (jpg.name, seconds), flush=True)
            return jpg.read_bytes()

    srv = IndiServer(shoot, port=port, set_iso=r.set_iso, get_iso=r.get_iso)
    srv.start()
    print("INDI escuchando en el puerto %d (KStars -> localhost)" % port, flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("parando…", flush=True)
    finally:
        srv.stop()
        r.close()
    return 0


def cmd_remote(args) -> int:
    """Modo control remoto: live view, disparo y descarga (astrofotografía)."""
    from .remote import Remote, RemoteConfig

    cfg = RemoteConfig(
        host=args.host,
        outdir=Path(args.out).expanduser(),
        http_port=args.http,
        frames=Path(args.frames).expanduser() if args.frames else None,
        shots=args.shots,
        interval=args.interval,
        settle=args.settle,
        restore=args.restore_mode,
        live=args.liveview or not args.shots,
        skip_raw=args.skip_raw,
        skip_jpeg=args.skip_jpeg,
        keepalive=args.keepalive,
        exposure=args.exposure,
        iso=args.iso,
    )
    if args.indi:
        return _run_indi(cfg, args.indi)
    r = Remote(cfg)
    stats = r.sequence() if cfg.shots else r.interactive()
    print("fin: %d disparos, %d ficheros, %d frames" % (
        stats.shots, len(stats.files), stats.frames))
    for path in stats.files:
        print("  %s" % path)
    return 0


def cmd_daemon(args) -> int:
    """Volcado continuo: descarga cada foto nueva y la sube."""
    from .daemon import DaemonConfig, TetherDaemon

    cfg = DaemonConfig(
        host=args.host,
        outdir=Path(args.out).expanduser(),
        poll=args.poll,
        keepalive=args.keepalive,
        upload_cmd=args.upload_cmd,
        upload_dir=Path(args.upload_dir).expanduser() if args.upload_dir else None,
        state_file=Path(args.state).expanduser() if args.state else None,
        once=args.once,
    )
    cfg.outdir.mkdir(parents=True, exist_ok=True)
    d = TetherDaemon(cfg)
    print("daemon: vigilando %s -> %s" % (cfg.host, cfg.outdir), flush=True)
    stats = d.run()
    print("fin: %d descargadas, %d subidas, %d fallos, %d reconexiones" % (
        stats.downloaded, stats.uploaded, stats.failures, stats.reconnects))
    for path in stats.saved:
        print("  %s" % path)
    return 0


def cmd_liveview(args) -> int:
    outdir = Path(args.out).expanduser() if args.out else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)
    server = None
    if args.http:
        from .mjpeg import MjpegServer
        server = MjpegServer(port=args.http)
        print("live view: %s" % server.start())
    transport, session = make_session(args)
    oly = olympus.Olympus(session)
    count = 0
    t0 = time.monotonic()
    try:
        oly.enter_pc_mode()
        oly.liveview_start()
        while args.count == 0 or count < args.count:
            frame = oly.liveview_frame()
            if frame is None:
                time.sleep(args.interval)
                continue
            count += 1
            if server is not None:
                server.push(frame)
            if outdir:
                (outdir / ("lv_%05d.jpg" % count)).write_bytes(frame)
            elif args.raw_stdout:
                sys.stdout.buffer.write(frame)
                sys.stdout.buffer.flush()
            elif server is None:
                print("frame %d: %d bytes" % (count, len(frame)), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = time.monotonic() - t0
        if elapsed > 0:
            print("\n%d frames in %.1fs (%.1f fps)" % (count, elapsed, count / elapsed), file=sys.stderr)
        oly.liveview_stop()
        oly.exit_pc_mode()
        session.close()
        transport.close()
        if server is not None:
            server.stop()
    return 0


def cmd_pair(args) -> int:
    """Register this PC in the camera's connect-PC list (USB side of Wi-Fi pairing)."""
    from . import pairing
    return pairing.register(args)


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omtether", description="OM System / Olympus tethering over PTP/IP")
    p.add_argument("-v", "--verbose", action="count", default=0, help="more logging (-vv for hex dumps)")
    from . import __version__
    p.add_argument("-V", "--version", action="version",
                   version="omtether %s" % __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_verbose(sp):
        # SUPPRESS keeps the global -v value when the flag is not repeated here
        sp.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS,
                        help="more logging (-vv for hex dumps)")

    def net_args(sp):
        add_verbose(sp)
        sp.add_argument("--host", required=True, help="camera IP address")
        sp.add_argument("--port", type=int, default=15740)
        sp.add_argument("--event-port", type=int, default=None)
        sp.add_argument("--name", default=None, help="initiator name shown to the camera (default: hostname)")
        sp.add_argument("--timeout", type=float, default=15.0)

    d = sub.add_parser("discover", help="find cameras on the LAN")
    add_verbose(d)
    d.add_argument("--timeout", type=float, default=3.0)
    d.add_argument("--ssdp-only", action="store_true")
    d.add_argument("--subnet", help="extra subnet to scan, e.g. 192.168.0.0/24")
    d.add_argument("--wait", type=float, metavar="SECONDS", help="keep looking until a camera shows up")
    d.set_defaults(func=cmd_discover)

    i = sub.add_parser("info", help="connect and show camera information")
    net_args(i)
    i.set_defaults(func=cmd_info)

    hi = sub.add_parser("http-info", help="identify a camera in AP/OI.Share mode (HTTP CGI)")
    add_verbose(hi)
    hi.add_argument("--host", required=True, help="camera IP (usually 192.168.0.10)")
    hi.add_argument("--port", type=int, default=80)
    hi.set_defaults(func=cmd_http_info)

    pr = sub.add_parser("props", help="read camera properties")
    net_args(pr)
    pr.add_argument("--codes", help="comma separated property codes")
    pr.set_defaults(func=cmd_props)

    r = sub.add_parser("raw", help="send a raw PTP operation (protocol probing)")
    net_args(r)
    r.add_argument("--op", required=True)
    r.add_argument("--params", default="")
    r.add_argument("--data", default="", help="hex payload to send")
    r.add_argument("--no-data", action="store_true", help="do not expect a data phase")
    r.add_argument("--out", help="write returned data to this file")
    r.set_defaults(func=cmd_raw)

    c = sub.add_parser("capture", help="capture and download")
    net_args(c)
    c.add_argument("--out", default="~/Pictures/omtether")
    c.add_argument("--count", type=int, default=1)
    c.add_argument("--interval", type=float, default=0.0)
    c.set_defaults(func=cmd_capture)

    w = sub.add_parser("watch", help="auto-download new images")
    net_args(w)
    w.add_argument("--out", default="~/Pictures/omtether")
    w.add_argument("--poll", type=float, default=1.0, help="seconds between object-list polls")
    w.add_argument("--capture", type=int, metavar="N", default=0,
                   help="trigger N shots at start, then download them")
    w.add_argument("--duration", type=float, metavar="SECONDS", default=0,
                   help="exit after this many seconds (0 = forever)")
    w.add_argument("--keepalive", type=float, metavar="SECONDS", default=0,
                   help="send the wireless keep-alive (0x94BA) every N seconds; 0 = never")
    w.set_defaults(func=cmd_watch)

    g = sub.add_parser("gui", help="desktop app (PyQt6): tether, remote and astro")
    g.add_argument("-v", "--verbose", action="count", default=0)
    g.set_defaults(func=cmd_gui)

    ap_br = sub.add_parser("bridge",
                           help="PTP/IP bridge so libgphoto2 (darktable) can use the camera")
    ap_br.add_argument("--host", required=True, help="camera IP")
    ap_br.add_argument("--listen", default="127.0.0.1", help="address to listen on")
    ap_br.add_argument("--port", type=int, default=15740, help="port to listen on")
    ap_br.add_argument("--host-port", type=int, default=15740,
                       help="camera port (15740 unless you are testing against the mock)")
    ap_br.add_argument("--out", help="download every new photo to this directory")
    ap_br.add_argument("--no-download", action="store_true", help="only bridge, no download")
    ap_br.add_argument("--mdns", action="store_true",
                       help="advertise _ptp._tcp so darktable auto-detects it")
    ap_br.set_defaults(func=cmd_bridge)

    ap_gph = sub.add_parser("gphoto",
                            help="configure libgphoto2 (darktable, digiKam) for this camera")
    ap_gph.add_argument("--host", help="camera IP; if given, check which GUID it accepts")
    ap_gph.add_argument("--write", action="store_true", help="write the setting to ~/.gphoto/settings")
    ap_gph.add_argument("--timeout", type=float, default=6.0)
    ap_gph.set_defaults(func=cmd_gphoto)

    ap_dt = sub.add_parser("darktable",
                           help="open darktable so it sees the PTP/IP bridge")
    ap_dt.add_argument("--host", default="127.0.0.1", help="bridge address")
    ap_dt.add_argument("--port", type=int, default=15740, help="bridge port")
    ap_dt.add_argument("--check", action="store_true",
                       help="only check whether libgphoto2 sees the bridge")
    ap_dt.add_argument("extra", nargs="*", help="arguments passed to darktable")
    ap_dt.set_defaults(func=cmd_darktable)

    ap = sub.add_parser("app", help="OI.Share HTTP API (camera's own access point)")
    ap.add_argument("action", choices=["mode", "shoot", "bulb", "stop", "prop",
                                      "setprop", "list", "get", "live", "info"])
    ap.add_argument("value", nargs="?", default="")
    ap.add_argument("--host", default="192.168.0.10")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--no-af", action="store_true", help="shoot without the half-press")
    ap.add_argument("--lvqty", default=None, help="live view size, e.g. 0640x0480")
    ap.add_argument("--port", type=int, default=40000, help="UDP port for live view")
    ap.add_argument("--count", type=int, default=0, help="frames to grab (0 = forever)")
    ap.add_argument("--out", help="directory for frames / downloads")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    ap.set_defaults(func=cmd_app)

    rm = sub.add_parser("remote", help="remote control mode: live view + shutter + download")
    net_args(rm)
    rm.add_argument("--out", default="~/Pictures/omtether")
    rm.add_argument("--http", type=int, metavar="PORT", help="serve live view as MJPEG")
    rm.add_argument("--frames", help="save live view frames to this directory")
    rm.add_argument("--shots", type=int, default=0,
                    help="take N shots then exit (0 = interactive console)")
    rm.add_argument("--interval", type=float, default=5.0,
                    help="seconds between shots in sequence mode")
    rm.add_argument("--settle", type=float, default=60.0,
                    help="max seconds to wait for the file to appear")
    rm.add_argument("--iso", type=int, metavar="N", default=0,
                    help="set ISO before shooting (0 = leave as is)")
    rm.add_argument("--exposure", type=float, metavar="SECONDS", default=0.0,
                    help="timed exposure per shot (bulb: open, wait, close)")
    rm.add_argument("--indi", type=int, metavar="PORT", nargs="?", const=7624, default=None,
                    help="expose an INDI server on this port and let KStars drive the shots")
    rm.add_argument("--skip-jpeg", action="store_true",
                    help="sólo RAW: no descargar JPEG")
    rm.add_argument("--skip-raw", action="store_true",
                    help="download only JPEGs (enough for stacking, half the traffic)")
    rm.add_argument("--liveview", action="store_true",
                    help="keep live view running (off during sequences by default: "
                         "it fights with PC-control mode)")
    rm.add_argument("--restore-mode", action="store_true",
                    help="put the camera back in standalone mode on exit "
                         "(default: leave it in PC control, which it only grants once per boot)")
    rm.add_argument("--keepalive", type=float, default=10.0)
    rm.set_defaults(func=cmd_remote)

    dm = sub.add_parser("daemon", help="watch the camera and upload every new photo")
    net_args(dm)
    dm.add_argument("--out", default="~/Pictures/omtether", help="spool directory")
    dm.add_argument("--poll", type=float, default=2.0, help="seconds between object-list polls")
    dm.add_argument("--keepalive", type=float, default=10.0,
                    help="send 0x94BA(1) every N seconds (0 = never)")
    dm.add_argument("--upload-cmd", metavar="CMD",
                    help="shell command run per file; {file} is replaced by the path")
    dm.add_argument("--upload-dir", metavar="DIR",
                    help="also copy each file into this directory (synced folder)")
    dm.add_argument("--state", metavar="FILE", default="~/.local/state/omtether/tether.json",
                    help="json file remembering processed handles")
    dm.add_argument("--once", action="store_true", help="exit after the first download")
    dm.set_defaults(func=cmd_daemon)

    lv = sub.add_parser("liveview", help="grab live view frames")
    net_args(lv)
    lv.add_argument("--out", help="directory for JPEG frames")
    lv.add_argument("--count", type=int, default=0, help="0 = until Ctrl-C")
    lv.add_argument("--interval", type=float, default=0.05)
    lv.add_argument("--raw-stdout", action="store_true", help="write JPEG frames to stdout")
    lv.add_argument("--http", type=int, metavar="PORT", help="serve an MJPEG stream on this port (open it in a browser)")
    lv.set_defaults(func=cmd_liveview)

    pa = sub.add_parser("pair", help="register this PC in the camera (Wi-Fi pairing over USB)")
    add_verbose(pa)
    pa.add_argument("--slot", type=int, default=1, help="camera slot 1..4 (default 1)")
    pa.add_argument("--name", help="computer name to store (default: hostname, max 15 chars)")
    pa.add_argument("--mac", action="append", help="MAC to store (repeatable; default: real NICs)")
    pa.add_argument("--list", action="store_true", help="dump the camera's connect-PC entries")
    pa.add_argument("--clear", action="store_true", help="remove the entry in --slot")
    pa.add_argument("--dry-run", action="store_true", help="only print the payload that would be sent")
    pa.add_argument("--hex", help="raw SetConnectPcInfo payload (overrides the automatic one)")
    pa.set_defaults(func=cmd_pair)
    return p


def _sigterm(_signum, _frame):
    """Convierte SIGTERM en KeyboardInterrupt para que los `finally` limpien."""
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # `omtether darktable --qualidad ...`: lo que no sea nuestro se le pasa tal cual
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        if getattr(args, "extra", None) is not None:
            args.extra.extend(unknown)
        else:
            parser.error("unrecognized arguments: %s" % " ".join(unknown))
    # `timeout` y los gestores de servicios mandan SIGTERM: sin esto el proceso
    # muere sin cerrar la sesión PTP y la cámara se queda con una sesión zombie
    # (responde a ping pero no acepta nuevas conexiones hasta que se reinicia).
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (ValueError, OSError):
        pass
    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except PtpIpError as exc:
        print("PTP/IP error: %s" % exc, file=sys.stderr)
        return 2
    except ptp.PtpError as exc:
        print("PTP error: %s" % exc, file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
