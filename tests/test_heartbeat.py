import errno
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


def test_bind_waits_for_the_address_to_appear(monkeypatch):
    attempts, slept, logged = [], [], []

    def flaky(host, port, token):
        attempts.append(host)
        if len(attempts) < 3:
            raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
        return "server"

    monkeypatch.setattr(heartbeat, "make_server", flaky)
    server = heartbeat.make_server_when_ready(
        "100.64.0.5", 8787, None, sleep=slept.append, log=logged.append
    )
    assert server == "server"
    assert slept == [5, 5]
    assert len(logged) == 1


def test_bind_fails_fast_on_other_errors(monkeypatch):
    def in_use(host, port, token):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(heartbeat, "make_server", in_use)
    with pytest.raises(OSError):
        heartbeat.make_server_when_ready("127.0.0.1", 8787, None, sleep=lambda s: None)


def test_agent_check_is_off_unless_configured(monkeypatch):
    monkeypatch.delenv("OREO_AGENT_ALIVE_FILE", raising=False)
    assert heartbeat.agent_check() == []


def test_agent_check_follows_the_alive_file(monkeypatch, tmp_path):
    alive = tmp_path / "agent.alive"
    monkeypatch.setenv("OREO_AGENT_ALIVE_FILE", str(alive))
    [missing] = heartbeat.agent_check()
    assert not missing.ok
    alive.touch()
    [fresh] = heartbeat.agent_check()
    assert fresh.ok
    [stale] = heartbeat.agent_check(now=alive.stat().st_mtime + 600)
    assert stale.detail == "no sign of life for 10 minutes"


def test_non_ascii_authorization_gets_401(checks):
    server = start(token="secret")
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/heartbeat",
            headers={"Authorization": "Bearer café"},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 401
    finally:
        server.shutdown()
