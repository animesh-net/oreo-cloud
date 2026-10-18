"""HTTP endpoint that oreo-monitor polls over the tailnet."""

import hmac
import json
import os
import socket
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from oreo import __version__, health, system
from oreo.checks import all_ok

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8787


def report() -> dict:
    checks = health.run() + system.run()
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
            if token and not hmac.compare_digest(
                self.headers.get("Authorization", ""), f"Bearer {token}"
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


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or os.environ.get("OREO_HEARTBEAT_HOST", DEFAULT_HOST)
    port = port or int(os.environ.get("OREO_HEARTBEAT_PORT", DEFAULT_PORT))
    token = os.environ.get("OREO_HEARTBEAT_TOKEN") or None
    server = make_server(host, port, token)
    print(f"oreo heartbeat listening on http://{host}:{port}/heartbeat", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
