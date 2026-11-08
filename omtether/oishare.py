"""Minimal client for the OI.Share HTTP API (the camera's own access point mode).

The camera firmware embeds the API descriptor (v5.20, 62 CGIs); it is not
redistributed here, only the endpoints that matter for this client.  This is the interface OI.Share uses
when the phone joins the camera's Wi-Fi access point ("private" mode), and it is
a different thing from the PTP/IP tethering that OM Capture uses in
infrastructure mode.

The camera only answers these CGIs in transfer mode: its own log says
``[httpd]not_CONN_MODE_TRANSFER`` when asked in camera-control mode.

Nothing here is needed for PTP/IP tethering; it exists to (a) identify a camera
that is sitting in AP mode and (b) offer the pairing CGI
(``OLYSetConnectPCInfo``) as an alternative to the USB route.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_PORT = 80
USER_AGENT = "OI.Share"


def get(host: str, cgi: str, params: dict | None = None, port: int = DEFAULT_PORT,
        timeout: float = 4.0) -> tuple[int, str]:
    """GET one CGI and return (http_status, body)."""
    query = ""
    if params:
        query = "?" + "&".join("%s=%s" % (k, v) for k, v in params.items())
    url = "http://%s:%d/%s%s" % (host, port, cgi, query)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4096).decode("utf-8", "replace") if exc.fp else ""
    except (urllib.error.URLError, OSError) as exc:
        log.debug("GET %s failed: %s", url, exc)
        return 0, ""


def looks_like_camera(host: str, port: int = DEFAULT_PORT, timeout: float = 2.0) -> bool:
    """True if the host answers the OI.Share CGI set (i.e. it is the camera)."""
    status, body = get(host, "get_caminfo", port=port, timeout=timeout)
    if status != 200 or not body:
        return False
    return "<" in body and ("caminfo" in body.lower() or "camera" in body.lower()
                            or "model" in body.lower() or "oishare" in body.lower())


def camera_info(host: str, port: int = DEFAULT_PORT) -> dict:
    """Best effort identification of a camera in AP / OI.Share mode."""
    out: dict = {}
    for cgi, params in (
        ("get_caminfo", None),
        ("get_connectmode", None),
        ("get_camprop", {"com": "desc", "propname": "takemode"}),
    ):
        status, body = get(host, cgi, params, port=port)
        out[cgi] = (status, body[:2000])
    return out
