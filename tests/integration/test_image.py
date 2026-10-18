"""End-to-end tests of the Docker image, run the way compose.yaml runs it on the Pi.

Skipped unless OREO_IMAGE names a built image, e.g.
    docker build -t oreo-cloud:test . && OREO_IMAGE=oreo-cloud:test pytest tests/integration
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

IMAGE = os.environ.get("OREO_IMAGE")
TOKEN = "integration-test-token"
SOCKET = "/var/run/docker.sock"
ROOT = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.skipif(not IMAGE, reason="set OREO_IMAGE to an image tag to run")


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def wait_for(condition, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.5)
    raise AssertionError("condition not met within timeout")


def docker_args() -> list[str]:
    gid = str(os.stat(SOCKET).st_gid)
    return ["--group-add", gid, "-v", f"{SOCKET}:{SOCKET}:ro"]


@pytest.fixture
def containers():
    names = []

    def create(*args: str) -> str:
        name = f"oreo-it-{uuid.uuid4().hex[:8]}"
        names.append(name)
        docker(*args[:1], "--name", name, *args[1:])
        return name

    yield create
    for name in names:
        docker("rm", "-f", name, check=False)


@pytest.fixture
def heartbeat(containers):
    name = containers(
        "run", "-d", "--hostname", "oreo-cloud", "--health-interval", "1s",
        *docker_args(), "-v", "/:/host:ro",
        "-e", f"OREO_HEARTBEAT_TOKEN={TOKEN}", "-e", "OREO_DISK_PATHS=/host",
        "-p", "127.0.0.1::8787", IMAGE,
    )  # fmt: skip
    port = docker("port", name, "8787/tcp").stdout.split(":")[-1].strip()
    url = f"http://127.0.0.1:{port}/heartbeat"

    def get(token: str | None = TOKEN) -> tuple[int, dict]:
        request = urllib.request.Request(url)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def reachable() -> bool:
        try:
            get()
            return True
        except OSError:
            return False

    wait_for(reachable)
    return name, get


def test_runs_as_non_root_with_docker_group(heartbeat):
    name, _ = heartbeat
    identity = docker("exec", name, "id").stdout
    assert "uid=10001(oreo)" in identity
    assert str(os.stat(SOCKET).st_gid) in identity


def test_heartbeat_requires_token(heartbeat):
    _, get = heartbeat
    assert get(token=None)[0] == 401
    assert get(token="wrong")[0] == 401


def test_heartbeat_reports_stopped_container_then_recovers(heartbeat, containers):
    _, get = heartbeat
    stopped = containers("create", "--entrypoint", "true", IMAGE)

    status, body = get()
    assert status == 503
    assert body["host"] == "oreo-cloud"
    failing = {c["name"] for c in body["checks"] if not c["ok"]}
    assert failing == {f"container {stopped}"}
    assert "disk /host" in {c["name"] for c in body["checks"]}

    docker("rm", stopped)
    wait_for(lambda: get()[0] == 200)


def test_container_healthcheck_passes_while_checks_fail(heartbeat, containers):
    name, get = heartbeat
    containers("create", "--entrypoint", "true", IMAGE)
    assert get()[0] == 503
    wait_for(
        lambda: (
            docker("inspect", "-f", "{{.State.Health.Status}}", name).stdout.strip() == "healthy"
        )
    )


def test_one_off_health_reads_container_health(containers):
    healthy = containers(
        "run", "-d", "--health-cmd", "true", "--health-interval", "1s",
        "--entrypoint", "sleep", IMAGE, "infinity",
    )  # fmt: skip
    wait_for(lambda: "(healthy)" in docker("ps", "-f", f"name={healthy}").stdout)
    result = docker("run", "--rm", *docker_args(), IMAGE, "health", check=False)
    assert result.returncode == 0, result.stdout
    assert f"OK    container {healthy}" in result.stdout


def test_one_off_system_checks_host_disk():
    result = docker(
        "run", "--rm", "-v", "/:/host:ro", "-e", "OREO_DISK_PATHS=/host",
        IMAGE, "system", "--json",
    )  # fmt: skip
    names = [check["name"] for check in json.loads(result.stdout)["checks"]]
    assert names == ["disk /host", "memory", "temperature", "load"]


REQUIRED_ENV = {"DOCKER_GID": "996", "OREO_HEARTBEAT_TOKEN": "x"}


# One variable missing at a time: with several missing, compose reports
# whichever it interpolates first, and that order is not stable.
@pytest.mark.parametrize("missing", REQUIRED_ENV)
def test_compose_requires_env_values(missing):
    env = {**os.environ, **REQUIRED_ENV}
    del env[missing]
    result = subprocess.run(
        ["docker", "compose", "-f", ROOT / "compose.yaml", "config"],
        capture_output=True, text=True, env=env,
    )  # fmt: skip
    assert result.returncode != 0
    assert f"set {missing} in .env" in result.stderr


def test_compose_binds_heartbeat_to_bind_ip():
    env = {
        **os.environ,
        "DOCKER_GID": "996",
        "OREO_HEARTBEAT_TOKEN": "x",
        "OREO_BIND_IP": "100.64.0.5",
    }
    result = subprocess.run(
        ["docker", "compose", "-f", ROOT / "compose.yaml", "config", "--format", "json"],
        capture_output=True, text=True, env=env, check=True,
    )  # fmt: skip
    service = json.loads(result.stdout)["services"]["oreo"]
    assert service["ports"][0]["host_ip"] == "100.64.0.5"
    assert service["group_add"] == ["996"]
    assert service["environment"]["OREO_DISK_PATHS"] == "/host,/host/mnt/data-hot-ssd1"
    assert service["environment"]["OREO_MOUNT_PATHS"] == "/host/mnt/data-hot-ssd1"
    [host_mount] = [v for v in service["volumes"] if v["target"] == "/host"]
    assert host_mount["bind"]["propagation"] == "rslave"
