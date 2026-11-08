"""Camera discovery on the LAN.

Windows' MTP/IP uses UPnP (SSDP) for discovery, per Microsoft's WPD blog
("we used the PTP/IP binding, then used UPnP for pairing and discovery").
In practice the most robust way to find an OM System body is to probe the
PTP/IP port directly, so we do both.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import mdns

log = logging.getLogger(__name__)

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
PTPIP_PORT = 15740
# The camera runs its own DHCP server and uses this address when acting as an AP
# (literal string found in the OM-1 II firmware; confirms the OI.Share setup).
CAMERA_AP_DEFAULT = "192.168.0.10"

M_SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: {st}\r\n"
    "\r\n"
)


@dataclass
class Found:
    host: str
    port: int = PTPIP_PORT
    source: str = "scan"
    detail: str = ""
    mode: str = ""      # "ptpip" (tethering), "http" (OI.Share/AP mode) or both


def _local_ipv4_networks() -> list[ipaddress.IPv4Network]:
    nets: list[ipaddress.IPv4Network] = []
    try:
        out = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        addrs = {a[4][0] for a in out}
    except OSError:
        addrs = set()
    # the default-route trick gives the primary address
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    for addr in addrs:
        if addr.startswith("127."):
            continue
        try:
            nets.append(ipaddress.IPv4Network(addr + "/24", strict=False))
        except ValueError:
            continue
    return nets


def ssdp_search(timeout: float = 3.0, st: str = "ssdp:all") -> list[tuple[str, str]]:
    """Send an SSDP M-SEARCH and return [(addr, response_text)]."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.4)
    responses: list[tuple[str, str]] = []
    try:
        sock.sendto(M_SEARCH.format(st=st).encode(), (SSDP_ADDR, SSDP_PORT))
        deadline = time.monotonic() + timeout
        seen = set()
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            text = data.decode("utf-8", "replace")
            key = (addr[0], text[:120])
            if key in seen:
                continue
            seen.add(key)
            responses.append((addr[0], text))
    finally:
        sock.close()
    return responses


def _parse_ssdp(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip().upper()] = v.strip()
    return out


def probe_ptpip(host: str, port: int = PTPIP_PORT, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan(networks: list[ipaddress.IPv4Network] | None = None, port: int = PTPIP_PORT,
         workers: int = 128, timeout: float = 0.5, check_http: bool = True) -> list[Found]:
    nets = networks or _local_ipv4_networks()
    hosts: list[str] = []
    for net in nets:
        hosts.extend(str(h) for h in net.hosts())
    log.info("scanning %d hosts on port %d%s", len(hosts), port, " (+80/http)" if check_http else "")
    found: list[Found] = []

    def check(host: str) -> Found | None:
        if probe_ptpip(host, port, timeout):
            return Found(host, port, "scan", mode="ptpip")
        if check_http and _probe_oishare(host, timeout):
            return Found(host, 80, "scan", mode="http")
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(check, hosts):
            if res is not None:
                found.append(res)
    return found


def _probe_oishare(host: str, timeout: float = 1.0) -> bool:
    """Only report port 80 when it actually answers the OI.Share CGI set."""
    from . import oishare

    if not probe_ptpip(host, 80, timeout * 0.6):
        return False
    return oishare.looks_like_camera(host, timeout=timeout)


def discover(timeout: float = 3.0, do_scan: bool = True, extra_net: str | None = None) -> list[Found]:
    found: dict[str, Found] = {}

    # 1) the camera's own advertisement: _ptp._tcp via mDNS (see omtether/mdns.py)
    for item in mdns.browse_ptp(timeout=min(timeout, 2.0)):
        host = item.get("host") or item.get("addr") or ""
        if not host:
            continue
        txt = item.get("txt") or {}
        detail = " ".join("%s=%s" % (k, v) for k, v in txt.items())
        found.setdefault(host, Found(host, item.get("port") or PTPIP_PORT, "mdns", detail, mode="ptpip"))

    for addr, text in ssdp_search(timeout):
        hdr = _parse_ssdp(text)
        blob = " ".join(hdr.values()).lower()
        detail = hdr.get("SERVER", "") + " " + hdr.get("ST", "") + " " + hdr.get("USN", "")
        interesting = any(k in blob for k in ("om digital", "olympus", "omsystem", "camera", "ptp", "mtp"))
        log.debug("SSDP from %s: %s", addr, detail.strip())
        if interesting:
            found[addr] = Found(addr, PTPIP_PORT, "ssdp", detail.strip())

    nets = []
    if extra_net:
        nets.append(ipaddress.IPv4Network(extra_net, strict=False))
    if do_scan or not found:
        nets = nets or None
        for f in scan(nets):
            found.setdefault(f.host, f)

    # the camera's own access point hands out 192.168.0.x and answers on .10
    if CAMERA_AP_DEFAULT not in found:
        if probe_ptpip(CAMERA_AP_DEFAULT):
            found[CAMERA_AP_DEFAULT] = Found(CAMERA_AP_DEFAULT, PTPIP_PORT, "ap-default", mode="ptpip")
        elif _probe_oishare(CAMERA_AP_DEFAULT, 1.5):
            found[CAMERA_AP_DEFAULT] = Found(CAMERA_AP_DEFAULT, 80, "ap-default", mode="http")

    return sorted(found.values(), key=lambda f: tuple(int(x) for x in f.host.split(".")))


def guess_location_host(location: str) -> str | None:
    m = re.match(r"http://([^/:]+)", location or "")
    return m.group(1) if m else None
