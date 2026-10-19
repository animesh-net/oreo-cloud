"""The backup against a real restic and a real repository, to catch command-line and output
mistakes that the faked restic in test_backup.py cannot. Skipped where restic is missing,
except in CI, which installs it and sets OREO_REQUIRE_RESTIC."""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime

import pytest

from oreo import backup, clock, drives, net, storage
from oreo.checks import Check

if shutil.which("restic") is None and os.environ.get("OREO_REQUIRE_RESTIC"):
    # CI sets this, so a missing restic fails the run instead of quietly skipping the test.
    raise RuntimeError("OREO_REQUIRE_RESTIC is set but restic is not installed")
pytestmark = pytest.mark.skipif(shutil.which("restic") is None, reason="restic not installed")

NIGHT = datetime(2026, 10, 18, 22, 0, tzinfo=UTC)  # 03:30 Monday in Asia/Kolkata
SUNDAY = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)  # 03:30 Sunday


@pytest.fixture
def pi(tmp_path, monkeypatch):
    sources = []
    for name in ("etc", "home", "bin", "tailscale", "firmware", "cron"):
        folder = tmp_path / "src" / name
        folder.mkdir(parents=True)
        (folder / "settings.conf").write_text(f"{name} = 1\n")
        sources.append(str(folder))
    flash = tmp_path / "flash"
    password = tmp_path / "pass"
    password.write_text("integration-test-only\n")
    subprocess.run(
        ["restic", "-r", str(flash / "restic"), "--password-file", str(password), "init"],
        check=True, capture_output=True,
    )  # fmt: skip
    dumps = tmp_path / "ssd/backups"
    dumps.mkdir(parents=True)
    dump = dumps / "immich-db-backup-20261019T020000.sql.gz"
    dump.write_bytes(b"x" * 4096)

    for name, value in {
        "HOST": tmp_path,
        "SOURCES": tuple(sources),
        "EXCLUDES": (),
        "REPOSITORY": flash / "restic",
        "PASSWORD_FILE": password,
        "PACKAGE_LIST": tmp_path / "packages.txt",
        "DUMPS": dumps,
        "DUMP_COPIES": flash / "immich-db",
        "LOCK_FILE": tmp_path / "lock",
        "FLASH_MOUNT": "/flash",
    }.items():
        monkeypatch.setattr(backup, name, value)
    monkeypatch.setenv("OREO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(clock, "check", lambda: Check("clock", True, "synchronised"))
    monkeypatch.setattr(storage, "read_mounts", lambda: {"/flash": False})
    monkeypatch.setattr(storage, "run", lambda: [Check("disk errors", True, "none")])
    monkeypatch.setattr(drives, "run", lambda: [Check("Data", True, "healthy")])
    monkeypatch.setattr(
        net, "host_run", lambda command: subprocess.CompletedProcess(command, 0, "pkg\n", "")
    )
    return {"dump": dump, "flash": flash, "password": password}


def back_up(pi, now):
    os.utime(pi["dump"], (now.timestamp() - 60,) * 2)
    sent = []
    ok = backup.run(send=lambda message, **kwargs: sent.append(message), now=now)
    return ok, sent


def snapshots(pi):
    result = subprocess.run(
        ["restic", "-r", str(pi["flash"] / "restic"), "--password-file", str(pi["password"]),
         "snapshots", "--json"],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return json.loads(result.stdout)


def test_real_backup_saves_every_folder_and_reports(pi):
    ok, [report] = back_up(pi, NIGHT)
    assert ok, report
    first = report.splitlines()[0]
    assert first.startswith("✅ System settings: 6 of 6 folders saved (")
    assert "6 files, 6 new, 0 changed), 1 versions kept" in first
    [snapshot] = snapshots(pi)
    assert snapshot["hostname"] == "oreo-cloud"
    assert len(snapshot["paths"]) == 6


def test_real_second_backup_and_sunday_prune(pi):
    back_up(pi, NIGHT)
    ok, [report] = back_up(pi, SUNDAY)
    assert ok, report
    assert "0 new, 0 changed), 2 versions kept" in report.splitlines()[0]
    assert len(snapshots(pi)) == 2
