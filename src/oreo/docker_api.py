"""Minimal Docker Engine API client over the Unix socket."""

import http.client
import json
import os
import socket

DEFAULT_SOCKET = "/var/run/docker.sock"


class DockerError(Exception):
    pass


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def list_containers(socket_path: str | None = None, timeout: float = 10) -> list[dict]:
    socket_path = socket_path or os.environ.get("OREO_DOCKER_SOCKET", DEFAULT_SOCKET)
    connection = _UnixHTTPConnection(socket_path, timeout)
    try:
        connection.request("GET", "/containers/json?all=1")
        response = connection.getresponse()
        body = response.read()
    except OSError as error:
        raise DockerError(f"cannot reach Docker at {socket_path}: {error}") from error
    finally:
        connection.close()
    if response.status != 200:
        raise DockerError(f"Docker API returned HTTP {response.status}")
    return json.loads(body)
