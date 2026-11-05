import subprocess
from datetime import datetime
from types import SimpleNamespace

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
        self.cwds = []

    def __call__(self, command, cwd=None):
        self.commands.append(" ".join(command))
        self.cwds.append(cwd)
        out, code = "", 0
        text = " ".join(command)
        if " pull -q" in text:
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
    monkeypatch.setattr(update.updates, "HOST", tmp_path / "host")
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
    assert docker.ran("--project-directory /home/animesh/oreo-cloud up -d photos photos-ml")
    assert docker.cwds[docker.commands.index(next(c for c in docker.commands if "up -d" in c))] == (
        "/home/animesh/oreo-cloud"
    )
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


def test_caddy_pulls_the_proxy_and_cache_images_and_restarts_only_them(dumps):
    docker = FakeDocker()
    assert update.caddy(docker, lambda line: None)
    assert docker.ran("--project-directory /home/animesh/oreo-cloud pull -q proxy cache")
    assert docker.ran("--project-directory /home/animesh/oreo-cloud up -d proxy cache")
    assert not docker.ran("build")


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
    assert lines[-1] == "Pull failed (exit code 1). Nothing changed."


def test_update_waits_for_a_running_backup(dumps, monkeypatch):
    class Busy:
        def __init__(self, timeout):
            raise backup.BackupError("busy")

    monkeypatch.setattr(backup, "storage_lock", Busy)
    lines = []
    assert not update.run("prune", lines.append)
    assert lines == ["A backup or another job is running. Try again later."]


class FakeHost:
    def __init__(self, code=0, stderr=""):
        self.code, self.stderr, self.commands = code, stderr, []

    def __call__(self, command, cwd=None):
        self.commands.append(list(command))
        if command[0] == "journalctl":
            return subprocess.CompletedProcess(
                command, 0, "Unpacking libc6\nSetting up libc6\n", ""
            )
        return subprocess.CompletedProcess(command, self.code, "", self.stderr)


def test_os_update_runs_apt_as_a_host_service_and_shows_its_log(dumps):
    fake, lines = FakeHost(), []
    assert update.os_update(fake, lines.append, now=lambda: 1_800_000_000)
    command = fake.commands[0]
    assert command[:5] == ["systemd-run", "--unit=oreo-os-update", "--collect", "--wait", "--quiet"]
    assert "--with-new-pkgs upgrade" in command[-1] and "apt-get -y autoremove" in command[-1]
    assert "@1800000000" in fake.commands[1]
    assert "Unpacking libc6\nSetting up libc6" in lines
    assert "OS update finished." in lines
    assert "Reboot needed: no" in lines


def test_os_update_reports_a_failure_and_a_run_already_going(dumps):
    lines = []
    assert not update.os_update(FakeHost(1), lines.append)
    assert lines[-1] == "OS update FAILED. Full log: journalctl -u oreo-os-update"
    lines = []
    already = FakeHost(1, "Unit oreo-os-update.service was already loaded")
    assert not update.os_update(already, lines.append)
    assert lines[-1] == "An OS update is already running. Nothing more was started."


def test_os_is_an_update_option_but_not_part_of_all():
    assert update.ACTIONS["os"] == [update.os_update]
    assert update.os_update not in update.ACTIONS["all"]
    assert cli.build_parser().parse_args(["update", "os"]).action == "os"


def test_status_lists_available_updates(dumps, monkeypatch):
    monkeypatch.setattr(update.updates, "summary", lambda: ["✅ OS: up to date"])
    monkeypatch.setattr(update.shutil, "disk_usage", lambda path: SimpleNamespace(free=2048))
    lines = []
    assert update.status(FakeDocker(), lines.append)
    assert lines[-2:] == ["Available updates (nothing installs by itself):", "  ✅ OS: up to date"]


def test_host_commands_have_no_time_limit_and_start_in_the_folder(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    update.host(["docker", "compose", "pull"], cwd="/home/animesh/oreo-cloud")
    assert "timeout" not in seen["kwargs"]
    # Root on the host has no GHCR login; the Pi user's is used for private images.
    assert seen["kwargs"]["env"]["DOCKER_CONFIG"] == "/home/animesh/.docker"
    assert not any(arg.startswith("--wd") for arg in seen["argv"])
    after = seen["argv"][seen["argv"].index("--") + 1 :]
    assert after[:2] == ["sh", "-c"]
    assert after[4:] == ["/home/animesh/oreo-cloud", "docker", "compose", "pull"]


def test_host_command_changes_folder_after_entering_the_host(tmp_path):
    script = update.host(["pwd"], cwd=str(tmp_path)).args
    after = script[script.index("--") + 1 :]
    result = subprocess.run(after, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == str(tmp_path)


def test_a_failed_pull_says_why(dumps):
    class Failing(FakeDocker):
        def __call__(self, command, cwd=None):
            if " pull -q" in " ".join(command):
                return subprocess.CompletedProcess(command, 1, "", "error: denied\n")
            return super().__call__(command, cwd)

    ok, lines = go(Failing())
    assert lines[-1] == "Pull failed (error: denied). Nothing changed."


def test_host_command_failure_is_reported_not_raised(dumps, monkeypatch):
    def broken(out):
        raise FileNotFoundError("nsenter")

    monkeypatch.setitem(update.ACTIONS, "prune", [broken])
    lines = []
    assert not update.run("prune", lines.append)
    assert lines == ["Could not run a command on the host (nsenter). Nothing more was changed."]
