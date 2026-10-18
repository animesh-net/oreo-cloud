from oreo import docker_api, health


def container(name, state="running", status="Up 2 hours", labels=None):
    return {"Names": [f"/{name}"], "State": state, "Status": status, "Labels": labels or {}}


def test_running_container_passes():
    [check] = health.check_containers([container("web")])
    assert check.ok
    assert check.name == "container web"


def test_running_healthy_container_passes():
    [check] = health.check_containers([container("web", status="Up 2 hours (healthy)")])
    assert check.ok


def test_unhealthy_container_fails():
    [check] = health.check_containers([container("web", status="Up 2 hours (unhealthy)")])
    assert not check.ok
    assert "unhealthy" in check.detail


def test_stopped_container_fails():
    [check] = health.check_containers(
        [container("web", state="exited", status="Exited (1) 3 minutes ago")]
    )
    assert not check.ok
    assert check.detail == "exited: Exited (1) 3 minutes ago"


def test_ignored_container_is_skipped():
    checks = health.check_containers(
        [container("job", state="exited", labels={"oreo.ignore": "true"}), container("web")]
    )
    assert [c.name for c in checks] == ["container web"]


def test_no_containers_passes():
    [check] = health.check_containers([])
    assert check.ok


def test_unreachable_docker_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("OREO_DOCKER_SOCKET", str(tmp_path / "missing.sock"))
    [check] = health.run()
    assert check.name == "docker"
    assert not check.ok


def test_docker_error_is_reported(monkeypatch):
    def fail():
        raise docker_api.DockerError("Docker API returned HTTP 500")

    monkeypatch.setattr(docker_api, "list_containers", fail)
    [check] = health.run()
    assert check.detail == "Docker API returned HTTP 500"
