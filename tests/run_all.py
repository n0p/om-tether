#!/usr/bin/env python3
"""Run every self-test against the mock camera (no hardware required)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from omtether import cli  # noqa: E402
from tests.mock_camera import MockCamera  # noqa: E402


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(("  ok   " if ok else "  FAIL ") + name + ((" - " + detail) if detail else ""))
    return ok


def test_transport(cam: MockCamera) -> bool:
    return subprocess.call([sys.executable, "-m", "tests.test_transport"], cwd=ROOT) == 0


def test_info(cam: MockCamera) -> bool:
    return cli.main(["info", "--host", "127.0.0.1"]) == 0


def test_capture(cam: MockCamera) -> bool:
    out = Path(tempfile.mkdtemp())
    rc = cli.main(["capture", "--host", "127.0.0.1", "--count", "2", "--out", str(out)])
    files = sorted(out.glob("*.JPG"))
    ok = rc == 0 and len(files) == 2 and all(f.read_bytes()[:3] == b"\xff\xd8\xff" for f in files)
    return check("capture+download", ok, "%d files" % len(files))


def test_mjpeg(cam: MockCamera) -> bool:
    thread = threading.Thread(
        target=lambda: cli.main(["liveview", "--host", "127.0.0.1", "--http", "8099",
                                 "--count", "120", "--interval", "0.02"]),
        daemon=True)
    thread.start()
    time.sleep(1.0)
    try:
        page = urllib.request.urlopen("http://127.0.0.1:8099/", timeout=5).read()
        chunk = urllib.request.urlopen("http://127.0.0.1:8099/stream.mjpg", timeout=5).read(4000)
        ok = b"stream.mjpg" in page and b"--frame" in chunk and b"\xff\xd8\xff" in chunk
    except OSError as exc:
        return check("live view MJPEG", False, str(exc))
    return check("live view MJPEG", ok)


def test_watch(cam: MockCamera) -> bool:
    out = Path(tempfile.mkdtemp())
    proc = subprocess.Popen(
        [sys.executable, "-m", "omtether", "watch", "--host", "127.0.0.1", "--out", str(out), "--poll", "0.5"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.5)
    handle = cam.add_object("P9999999.JPG", b"\xff\xd8\xff\xe0" + b"X" * 2000 + b"\xff\xd9")
    cam.emit_event(0xC002, handle)
    time.sleep(1.5)
    proc.send_signal(signal.SIGINT)
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    files = sorted(out.glob("*.JPG"))
    return check("watch (real-time dump)", len(files) == 1, "%d files" % len(files))


def test_daemon_skip(cam: MockCamera) -> bool:
    """Un ORF omitido se marca como visto: su aviso no se repite en cada sondeo."""
    import threading, time
    from omtether.daemon import DaemonConfig, TetherDaemon

    out = Path(tempfile.mkdtemp())
    cfg = DaemonConfig(host="127.0.0.1", outdir=out, poll=0.5, skip_raw=True)
    daemon = TetherDaemon(cfg)
    threading.Thread(target=daemon.run, daemon=True).start()
    time.sleep(1.5)
    cam.add_object("P9000001.ORF", b"II*\x00" + b"R" * 4000)
    cam.add_object("P9000001.JPG", b"\xff\xd8\xff\xe0" + b"J" * 4000)
    time.sleep(5.0)
    daemon.stop()
    time.sleep(0.8)
    jpgs = sorted(out.glob("*.JPG"))
    ok = daemon.stats.downloaded == 1 and len(jpgs) == 1
    return check("daemon: ORF omitido no se repite", ok,
                 "%d descargados, %d jpg" % (daemon.stats.downloaded, len(jpgs)))


def test_state_file() -> bool:
    """El fichero de estado debe crear su carpeta: en la GUI vive en ~/.local/state."""
    import json
    import tempfile

    from omtether.daemon import DaemonConfig, TetherDaemon

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "no" / "existe" / "daemon.json"
        cfg = DaemonConfig(host="127.0.0.1", outdir=Path(tmp) / "out", state_file=state)
        daemon = TetherDaemon(cfg)
        daemon.seen = {7, 9}
        daemon._save_state()
        ok = state.is_file() and json.loads(state.read_text())["seen"] == [7, 9]
    return check("estado del daemon crea la carpeta", ok)


def test_bridge() -> bool:
    return subprocess.call([sys.executable, "-m", "tests.test_bridge"], cwd=ROOT) == 0


def test_gphoto() -> bool:
    return subprocess.call([sys.executable, "-m", "tests.test_gphoto"], cwd=ROOT) == 0


def test_parsers() -> bool:
    return subprocess.call([sys.executable, "-m", "tests.test_parsers"], cwd=ROOT) == 0


def test_mdns() -> bool:
    return subprocess.call([sys.executable, "-m", "tests.test_mdns_live"], cwd=ROOT) == 0


def main() -> int:
    cam = MockCamera()
    cam.start()
    try:
        results = [
            check("bridge (PTP/IP server)", test_bridge()),
            check("estado del daemon", test_state_file()),
            check("gphoto helpers", test_gphoto()),
            check("parsers", test_parsers()),
            check("mDNS _ptp._tcp browse", test_mdns()),
            check("transport handshake + session", test_transport(cam)),
            check("info", test_info(cam)),
            test_capture(cam),
            test_mjpeg(cam),
            test_watch(cam),
            test_daemon_skip(cam),
        ]
    finally:
        cam.stop()
    print("\n%d/%d tests passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
