#!/usr/bin/env python3
"""Build a Debian binary package (.deb) without debhelper.

`dpkg-deb` is not available on Arch, so the archive is assembled here: a .deb is
an `ar` archive with three members — `debian-binary` ("2.0\\n"), `control.tar.gz`
(control, md5sums) and `data.tar.gz` (the files, paths relative to /).

    python3 packaging/debian/build_deb.py            # -> dist/omtether_*.deb

The package installs into /usr/lib/python3/dist-packages (the Debian path for
`python3 -m omtether`), plus the `omtether` launcher, the desktop entry, the
icon and the user systemd unit.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.5.0~beta1-1"          # Debian version (tilde sorts before a final release)
PKG = "omtether"
MAINTAINER = "n0p <n0p@users.noreply.github.com>"
DIST = ROOT / "dist"


def add_file(tar: tarfile.TarFile, path: Path, arcname: str, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(arcname)
    data = path.read_bytes()
    info.size = len(data)
    info.mode = mode
    info.mtime = int(os.environ.get("SOURCE_DATE_EPOCH", "0")) or 0
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    tar.addfile(info, io.BytesIO(data))


def add_bytes(tar: tarfile.TarFile, data: bytes, arcname: str, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(os.environ.get("SOURCE_DATE_EPOCH", "0")) or 0
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    tar.addfile(info, io.BytesIO(data))


def gz(path: Path) -> bytes:
    raw = path.read_bytes()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
        fh.write(raw)
    return buf.getvalue()


def main() -> int:
    DIST.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="omtether-deb-"))
    data_dir = work / "data"
    ctrl_dir = work / "control"
    data_dir.mkdir()
    ctrl_dir.mkdir()

    # --- data.tar: qué se instala y dónde -------------------------------
    site = data_dir / "usr/lib/python3/dist-packages"
    site.mkdir(parents=True)
    shutil.copytree(ROOT / "omtether", site / "omtether",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    bin_dir = data_dir / "usr/bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / PKG
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from omtether.cli import main\n\n"
        "sys.exit(main())\n")
    launcher.chmod(0o755)

    apps = data_dir / "usr/share/applications"
    apps.mkdir(parents=True)
    shutil.copy2(ROOT / "packaging/common/omtether.desktop", apps / "omtether.desktop")

    icons = data_dir / "usr/share/icons/hicolor/scalable/apps"
    icons.mkdir(parents=True)
    shutil.copy2(ROOT / "packaging/common/omtether.svg", icons / "omtether.svg")

    units = data_dir / "usr/lib/systemd/user"
    units.mkdir(parents=True)
    shutil.copy2(ROOT / "packaging/common/omtether-daemon.service",
                 units / "omtether-daemon.service")

    doc = data_dir / "usr/share/doc" / PKG
    doc.mkdir(parents=True)
    shutil.copy2(ROOT / "LICENSE", doc / "copyright")
    shutil.copy2(ROOT / "README.md", doc / "README.md")
    shutil.copy2(ROOT / "README.es.md", doc / "README.es.md")
    shutil.copy2(ROOT / "docs/PROTOCOL.md", doc / "PROTOCOL.md")
    (doc / "changelog.gz").write_bytes(gz(ROOT / "packaging/debian/changelog"))

    # md5sums de todo lo instalado (Debian lo espera para dpkg --verify)
    sums = []
    for path in sorted(data_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(data_dir).as_posix()
            sums.append("%s  %s" % (hashlib.md5(path.read_bytes()).hexdigest(), rel))
    (ctrl_dir / "md5sums").write_text("\n".join(sums) + "\n")

    installed_kb = sum(p.stat().st_size for p in data_dir.rglob("*") if p.is_file()) // 1024
    control = "\n".join([
        "Package: %s" % PKG,
        "Version: %s" % VERSION,
        "Architecture: all",
        "Maintainer: %s" % MAINTAINER,
        "Installed-Size: %d" % installed_kb,
        "Depends: python3 (>= 3.9)",
        "Recommends: python3-pyqt6, python3-usb",
        "Suggests: indi-bin, kstars",
        "Section: graphics",
        "Priority: optional",
        "Homepage: https://github.com/n0p/om-tether",
        "Description: tethering for OM System / Olympus cameras over PTP/IP",
        " Free reimplementation of the protocol used by OM Capture, so an OM",
        " System camera can be driven from Linux over Wi-Fi: real-time download",
        " of what you shoot on the body, remote shutter, timed exposure, ISO,",
        " live view, an INDI server for KStars/Ekos and a desktop app.",
        " .",
        " No vendor code is included; see NOTICE.md in the source tree.",
        "",
    ])
    (ctrl_dir / "control").write_text(control)

    # --- los tres miembros del .deb -------------------------------------
    def tar_gz(src: Path, out: Path) -> None:
        with tarfile.open(out, "w:gz", format=tarfile.GNU_FORMAT) as tar:
            for path in sorted(src.rglob("*")):
                rel = "./" + path.relative_to(src).as_posix()
                if path.is_dir():
                    info = tarfile.TarInfo(rel)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    tar.addfile(info)
                else:
                    add_file(tar, path, rel, 0o755 if path.name == PKG else 0o644)

    tar_gz(ctrl_dir, work / "control.tar.gz")
    tar_gz(data_dir, work / "data.tar.gz")
    (work / "debian-binary").write_text("2.0\n")

    deb = DIST / ("%s_%s_all.deb" % (PKG, VERSION.replace("~", "~")))
    if deb.exists():
        deb.unlink()
    subprocess.run(["ar", "rc", str(deb), "debian-binary", "control.tar.gz", "data.tar.gz"],
                   cwd=work, check=True)

    size = deb.stat().st_size
    print("deb: %s (%.1f KB, %d ficheros instalados)" % (deb, size / 1024, len(sums)))
    listing = subprocess.run(["ar", "t", str(deb)], capture_output=True, text=True).stdout
    print("miembros:", " ".join(listing.split()))
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
