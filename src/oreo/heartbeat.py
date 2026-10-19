"""HTTP endpoint that oreo-monitor polls over the tailnet."""

import errno
import hmac
import json
import os
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from oreo import __version__, health, system
from oreo.checks import Check, all_ok

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8787
AGENT_STALE_SECONDS = 120


def agent_check(now: float | None = None) -> list[Check]:
    """Whether the agent is still alive, from the file it touches every 15 seconds.

    Only when OREO_AGENT_ALIVE_FILE is set (compose sets it): a stopped or removed agent
    container would otherwise show only as one stopped container, or not at all.
    """
    path = os.environ.get("OREO_AGENT_ALIVE_FILE")
    if not path:
        return []
    try:
        age = (now or time.time()) - os.path.getmtime(path)
    except OSError:
        return [Check("agent", False, "no sign of life from the agent yet")]
    if age > AGENT_STALE_SECONDS:
        return [Check("agent", False, f"no sign of life for {round(age / 60)} minutes")]
    return [Check("agent", True, "running")]


def report() -> dict:
    checks = health.run() + system.run() + agent_check()
    return {
        "ok": all_ok(checks),
        "host": socket.gethostname(),
        "time": datetime.now(UTC).isoformat(timespec="seconds"),
        "version": __version__,
        "checks": [check.to_dict() for check in checks],
    }


def make_handler(token: str | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"oreo/{__version__}"

        def do_GET(self) -> None:
            if self.path != "/heartbeat":
                self._send(404, {"error": "not found"})
                return
            # Compared as bytes: compare_digest raises on non-ASCII text.
            if token and not hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()
            ):
                self._send(401, {"error": "unauthorized"})
                return
            body = report()
            self._send(200 if body["ok"] else 503, body)

        def _send(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def make_server(host: str, port: int, token: str | None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(token))


def make_server_when_ready(
    host: str,
    port: int,
    token: str | None,
    *,
    retry_seconds: float = 5,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> ThreadingHTTPServer:
    """Bind, waiting while `host` is not yet assigned, e.g. Tailscale still starting at boot.

    Docker restarts a container that exits but not one that fails to start, so binding to
    the Tailscale address through `ports:` would leave the heartbeat down after a reboot.
    """
    waiting = False
    while True:
        try:
            return make_server(host, port, token)
        except OSError as error:
            if error.errno != errno.EADDRNOTAVAIL:
                raise
            if not waiting:
                log(f"oreo heartbeat: {host} not available yet, retrying every {retry_seconds:g}s")
                waiting = True
            sleep(retry_seconds)


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or os.environ.get("OREO_HEARTBEAT_HOST", DEFAULT_HOST)
    port = port or int(os.environ.get("OREO_HEARTBEAT_PORT", DEFAULT_PORT))
    token = os.environ.get("OREO_HEARTBEAT_TOKEN") or None
    server = make_server_when_ready(host, port, token)
    print(f"oreo heartbeat listening on http://{host}:{port}/heartbeat", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
