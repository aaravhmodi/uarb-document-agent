"""Serves the full ZIPs that are too big for email, at unguessable links that expire.

Only exact /<token>/<name>.zip paths answer; there is no listing, so a link is the only key.
The Cloudflare tunnel publishes this port at PUBLIC_URL.
"""
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_PATH = re.compile(r"^/([A-Za-z0-9_-]{20,})/([A-Za-z0-9_.-]+\.zip)$")


def serve(root: Path, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
            self._send(body=True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._send(body=False)

        def _send(self, body: bool) -> None:
            m = _PATH.match(self.path.split("?", 1)[0])
            path = root / m.group(1) / m.group(2) if m else None
            if not path or not path.is_file():
                self.send_error(404, "This link doesn't exist or has expired.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.end_headers()
            if body:
                with path.open("rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1024 * 1024)

        def log_message(self, *args) -> None:  # the path is the secret; keep it out of logs
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def cleanup(root: Path, days: float) -> None:
    """Deletes top-level entries in `root` older than `days`."""
    cutoff = time.time() - days * 86400
    for p in root.glob("*") if root.exists() else []:
        if p.stat().st_mtime < cutoff:
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
