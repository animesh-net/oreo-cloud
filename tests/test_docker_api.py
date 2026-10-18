import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler

import pytest

from oreo import docker_api


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(socket_path, status, body):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.paths.append(self.path)
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def address_string(self):
            return "unix"

        def log_message(self, *args):
            pass

    server = UnixHTTPServer(str(socket_path), Handler)
    server.paths = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def socket_path(tmp_path):
    return tmp_path / "docker.sock"


def test_lists_all_containers(socket_path):
    containers = [{"Names": ["/web"], "State": "running"}]
    server = serve(socket_path, 200, containers)
    try:
        assert docker_api.list_containers(str(socket_path)) == containers
        assert server.paths == ["/containers/json?all=1"]
    finally:
        server.shutdown()


def test_http_error_raises(socket_path):
    server = serve(socket_path, 500, {"message": "boom"})
    try:
        with pytest.raises(docker_api.DockerError, match="HTTP 500"):
            docker_api.list_containers(str(socket_path))
    finally:
        server.shutdown()


def test_missing_socket_raises(socket_path):
    with pytest.raises(docker_api.DockerError, match="cannot reach Docker"):
        docker_api.list_containers(str(socket_path))
