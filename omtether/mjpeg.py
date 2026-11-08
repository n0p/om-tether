"""Tiny MJPEG-over-HTTP server so any browser can act as the live view screen."""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>omtether live view</title>
<style>html,body{margin:0;height:100%;background:#111;display:flex;align-items:center;justify-content:center}
img{max-width:100%;max-height:100%;image-rendering:auto}</style></head>
<body><img src="/stream.mjpg" alt="live view"></body></html>
"""


class _State:
    def __init__(self) -> None:
        self.frame: bytes | None = None
        self.seq = 0
        self.cond = threading.Condition()


class MjpegServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        self.state = _State()
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):  # noqa: A003 - silence per-request logs
                log.debug("http: " + fmt, *args)

            def do_GET(self):  # noqa: N802
                if self.path in ("/", "/index.html"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(PAGE)))
                    self.end_headers()
                    self.wfile.write(PAGE)
                    return
                if self.path.startswith("/stream.mjpg"):
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.end_headers()
                    last = -1
                    try:
                        while True:
                            with state.cond:
                                if state.seq == last:
                                    state.cond.wait(2.0)
                                frame, seq = state.frame, state.seq
                            if frame is None or seq == last:
                                continue
                            last = seq
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame))
                            self.wfile.write(frame)
                            self.wfile.write(b"\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    return
                self.send_error(404)

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="mjpeg-http", daemon=True)

    def start(self) -> str:
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        url = "http://%s:%d/" % ("127.0.0.1" if host == "0.0.0.0" else host, port)
        log.info("live view on %s", url)
        return url

    def push(self, jpeg: bytes) -> None:
        with self.state.cond:
            self.state.frame = jpeg
            self.state.seq += 1
            self.state.cond.notify_all()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
