"""Lanzar darktable de forma que vea el puente PTP/IP.

darktable sólo lista las cámaras que devuelve `gp_abilities_list_detect`, y en
Linux el puerto `ptpip:` de libgphoto2 no lleva host (el descubrimiento mDNS
sólo se compila con Bonjour), así que sin ayuda no hay manera de que aparezca
una cámara por Wi-Fi. El shim de `packaging/ldpreload/` reescribe la ruta de ese
puerto para que apunte al puente; aquí sólo se compila si hace falta y se lanza
darktable con `LD_PRELOAD`.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

SHIM_NAME = "omtether_ptpip_shim.so"
INSTALLED_SHIM = Path("/usr/lib/omtether") / SHIM_NAME


def _source() -> Path | None:
    """El .c del shim: junto al paquete, en el árbol de fuentes o en el repo."""
    here = Path(__file__).resolve().parent
    for candidate in (
        here / SHIM_NAME.replace(".so", ".c"),
        here.parent / "packaging" / "ldpreload" / "omtether_ptpip_shim.c",
        Path("/usr/share/omtether") / "omtether_ptpip_shim.c",
    ):
        if candidate.is_file():
            return candidate
    return None


def ensure_shim() -> Path | None:
    """Devuelve el .so del shim, compilándolo en la caché del usuario si falta."""
    env = os.environ.get("OMTETHER_SHIM")
    if env and Path(env).is_file():
        return Path(env)
    if INSTALLED_SHIM.is_file():
        return INSTALLED_SHIM

    source = _source()
    if source is None:
        log.error("no encuentro el código del shim (%s)", SHIM_NAME)
        return None
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "omtether"
    target = cache / SHIM_NAME
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return target

    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        log.error("hace falta un compilador (cc/gcc) para construir el shim")
        return None
    cache.mkdir(parents=True, exist_ok=True)
    cmd = [cc, "-shared", "-fPIC", "-O2", "-o", str(target), str(source), "-ldl"]
    log.info("compilando el shim: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("no se pudo compilar el shim: %s", result.stderr.strip()[:300])
        return None
    return target


def detect(host: str = "127.0.0.1", port: int = 15740) -> str | None:
    """¿Ve libgphoto2 el puente en esta máquina? Devuelve la salida de gphoto2.

    Se usa el binario `gphoto2 --auto-detect` porque hace exactamente la misma
    detección que darktable y, al ser un programa que enlaza libgphoto2_port de
    verdad, el shim lo intercepta (con ctypes no: dlsym sobre la librería se
    salta la interposición).
    """
    shim = ensure_shim()
    if shim is None:
        return None
    exe = shutil.which("gphoto2")
    if exe is None:
        return "gphoto2 no está instalado; no se puede comprobar desde aquí"
    env = dict(os.environ)
    env["LD_PRELOAD"] = f"{shim}:{env['LD_PRELOAD']}" if env.get("LD_PRELOAD") else str(shim)
    env["OMTETHER_BRIDGE_HOST"] = host
    env["OMTETHER_BRIDGE_PORT"] = str(port)
    result = subprocess.run([exe, "--auto-detect"], env=env,
                            capture_output=True, text=True, timeout=120)
    return (result.stdout + result.stderr).strip()


def spawn(args: list[str] | None = None, host: str = "127.0.0.1",
          port: int = 15740) -> subprocess.Popen | None:
    """Arranca darktable con el shim cargado, sin esperar a que termine."""
    exe = shutil.which("darktable")
    if exe is None:
        log.error("darktable no está instalado")
        return None
    shim = ensure_shim()
    if shim is None:
        return None
    env = dict(os.environ)
    env["LD_PRELOAD"] = f"{shim}:{env['LD_PRELOAD']}" if env.get("LD_PRELOAD") else str(shim)
    env["OMTETHER_BRIDGE_HOST"] = host
    env["OMTETHER_BRIDGE_PORT"] = str(port)
    cmd = [exe] + list(args or [])
    log.info("lanzando darktable con el puente en %s:%d", host, port)
    try:
        return subprocess.Popen(cmd, env=env)
    except OSError as exc:
        log.error("no se pudo lanzar darktable: %s", exc)
        return None


def launch(args: list[str] | None = None, host: str = "127.0.0.1",
           port: int = 15740) -> int:
    """Igual que spawn() pero devuelve el código de salida al cerrar darktable."""
    proc = spawn(args, host=host, port=port)
    if proc is None:
        return 1
    try:
        return proc.wait()
    except KeyboardInterrupt:
        return 130
