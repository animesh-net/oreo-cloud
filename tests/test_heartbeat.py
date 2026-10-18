import json
import threading
import urllib.error
import urllib.request

import pytest

from oreo import cli, health, heartbeat, system
from oreo.checks import Check


@pytest.fixture
def checks(monkeypatch):
    results = {"health": [Check("container web", True, "Up")], "system": []}
    monkeypatch.setattr(health, "run", lambda: results["health"])
    monkeypatch.setattr(system, "run", lambda: results["system"])
    return results


def start(token=None):
    server = heartbeat.make_server("127.0.0.1", 0, token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def get(server, path="/heartbeat", token=None):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture
def server():
    server = start()
    yield server
    server.shutdown()
    server.server_close()


def test_report_combines_health_and_system(checks):
    checks["system"] = [Check("disk /", True, "40% used")]
    body = heartbeat.report()
    assert body["ok"] is True
    assert [c["name"] for c in body["checks"]] == ["container web", "disk /"]
    assert {"host", "time", "version"} <= body.keys()


def test_healthy_returns_200(checks, server):
    status, body = get(server)
    assert status == 200
    assert body["ok"] is True


def test_failing_check_returns_503(checks, server):
    checks["system"] = [Check("disk /", False, "95% used")]
    status, body = get(server)
    assert status == 503
    assert body["ok"] is False


def test_unknown_path_returns_404(checks, server):
    assert get(server, "/")[0] == 404


def test_token_is_required_when_set(checks):
    server = start(token="s3cret")
    try:
        assert get(server)[0] == 401
        assert get(server, token="wrong")[0] == 401
        assert get(server, token="s3cret")[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_serve_reads_environment(monkeypatch):
    seen = {}

    class FakeServer:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            seen["closed"] = True

    def fake_make_server(host, port, token):
        seen.update(host=host, port=port, token=token)
        return FakeServer()

    monkeypatch.setenv("OREO_HEARTBEAT_PORT", "9000")
    monkeypatch.setenv("OREO_HEARTBEAT_TOKEN", "s3cret")
    monkeypatch.delenv("OREO_HEARTBEAT_HOST", raising=False)
    monkeypatch.setattr(heartbeat, "make_server", fake_make_server)
    heartbeat.serve()
    assert seen == {"host": "0.0.0.0", "port": 9000, "token": "s3cret", "closed": True}


def test_cli_flags_override_environment(monkeypatch):
    seen = {}
    monkeypatch.setattr(heartbeat, "serve", lambda host, port: seen.update(host=host, port=port))
    assert cli.main(["heartbeat", "--host", "100.64.0.1", "--port", "8000"]) == 0
    assert seen == {"host": "100.64.0.1", "port": 8000}
