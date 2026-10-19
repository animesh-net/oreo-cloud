import subprocess
from datetime import datetime

import pytest

from oreo import backup, cli, update

NOW = datetime(2026, 10, 19, 9, 15)


class FakeDocker:
    """Answers the host docker commands update.py runs."""

    def __init__(self, new_image=True, healthy=True, pull_ok=True):
        self.new_image = new_image
        self.healthy = healthy
        self.pull_ok = pull_ok
        self.commands = []

    def __call__(self, command):
        self.commands.append(" ".join(command))
        out, code = "", 0
        text = " ".join(command)
        if text.endswith("pull -q"):
            code = 0 if self.pull_ok else 1
        elif "{{.Image}}" in text:
            out = "sha256:old"
        elif "{{.Config.Image}}" in text:
            out = "ghcr.io/immich-app/immich-server:v3"
        elif "{{.Id}}" in text:
            out = "sha256:new" if self.new_image else "sha256:old"
        elif "{{.State.Health.Status}}" in text:
            out = "healthy" if self.healthy else "starting"
        return subprocess.CompletedProcess(command, code, out, "")

    def ran(self, fragment):
        return any(fragment in c for c in self.commands)


@pytest.fixture
def dumps(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "DUMPS", tmp_path)
    monkeypatch.setattr(update, "HOST", tmp_path / "host")
    monkeypatch.setattr(backup, "LOCK_FILE", tmp_path / "lock")
    return tmp_path


def fake_dump(size):
    def dump(path, user):
        path.write_bytes(b"x" * size)

    return dump


def go(docker, dump=None):
    lines = []
    ok = update.immich(
        docker, lines.append, dump=dump or fake_dump(6_000_000), sleep=lambda s: None, now=NOW
    )
    return ok, lines


def test_up_to_date_immich_changes_nothing(dumps):
    docker = FakeDocker(new_image=False)
    ok, lines = go(docker)
    assert ok
    assert "Immich is already up to date. Nothing to do." in lines
    assert not docker.ran("up -d")


def test_new_immich_is_dumped_then_started(dumps):
    docker = FakeDocker()
    ok, lines = go(docker)
    assert ok
    assert (dumps / "pre-update-2026-10-19-0915.sql.gz").exists()
    assert (
        "Dump saved: /mnt/data-hot-ssd1/library/backups/pre-update-2026-10-19-0915.sql.gz" in lines
    )
    assert docker.ran("--project-directory /home/animesh/immich up -d")
    assert "Immich is healthy." in lines


def test_small_dump_cancels_the_update(dumps):
    docker = FakeDocker()
    ok, lines = go(docker, fake_dump(1000))
    assert not ok
    assert "Database dump is too small. Update cancelled, nothing changed." in lines
    assert not docker.ran("up -d")
    assert list(dumps.glob("*.sql.gz")) == []


def test_only_the_three_newest_pre_update_dumps_are_kept(dumps):
    for day in range(1, 5):
        (dumps / f"pre-update-2026-10-0{day}-0900.sql.gz").write_bytes(b"x")
    go(FakeDocker())
    assert len(list(dumps.glob("pre-update-*.sql.gz"))) == 3


def test_unhealthy_server_alerts(dumps, monkeypatch):
    sent = []
    monkeypatch.setattr(update.notify, "send", lambda message, **kwargs: sent.append(message))
    ok, lines = go(FakeDocker(healthy=False))
    assert not ok
    assert sent == ["Immich update: server not healthy after 3 minutes"]


def test_failed_pull_changes_nothing(dumps):
    docker = FakeDocker(pull_ok=False)
    ok, lines = go(docker)
    assert not ok
    assert lines[-1] == "Pull failed. Nothing changed."


def test_update_waits_for_a_running_backup(dumps, monkeypatch):
    class Busy:
        def __init__(self, timeout):
            raise backup.BackupError("busy")

    monkeypatch.setattr(backup, "storage_lock", Busy)
    lines = []
    assert not update.run("prune", lines.append)
    assert lines == ["A backup or another job is running. Try again later."]


def test_update_command_has_no_os_option():
    with pytest.raises(SystemExit):
        cli.main(["update", "os"])
