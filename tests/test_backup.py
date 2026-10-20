import json
import os
import subprocess
from collections import namedtuple
from datetime import UTC, datetime, timedelta

import pytest

from oreo import backup, clock, drives, net, storage, updates
from oreo.checks import Check

# 03:30 in Asia/Kolkata on Monday 19 October 2026
NIGHT = datetime(2026, 10, 18, 22, 0, tzinfo=UTC)
Usage = namedtuple("Usage", "total used free")

SUMMARY = {
    "message_type": "summary",
    "files_new": 3,
    "files_changed": 12,
    "total_files_processed": 4210,
    "snapshot_id": "abc123",
}


class FakeRestic:
    def __init__(self, fail=None):
        self.commands = []
        self.fail = fail

    def __call__(self, command):
        self.commands.append(list(command))
        verb = command[7]
        if verb == self.fail:
            return subprocess.CompletedProcess(command, 1, "", "Fatal: repository is locked")
        out = ""
        if verb == "backup":
            out = '{"message_type":"status"}\n' + json.dumps(SUMMARY) + "\n"
        elif verb == "snapshots":
            out = json.dumps([{}] * 30)
        return subprocess.CompletedProcess(command, 0, out, "")

    def verbs(self):
        return [c[7] for c in self.commands]


@pytest.fixture
def pi(tmp_path, monkeypatch):
    """A fake host: backup sources, flash drive, SSD dumps, and passing checks."""
    sources = []
    for name in ("etc", "home", "bin", "tailscale", "firmware", "cron"):
        (tmp_path / "src" / name).mkdir(parents=True)
        sources.append(str(tmp_path / "src" / name))
    (tmp_path / "flash/restic").mkdir(parents=True)
    dumps = tmp_path / "ssd/backups"
    dumps.mkdir(parents=True)
    dump = dumps / "immich-db-backup-20261019T020000.sql.gz"
    dump.write_bytes(b"x" * 2048)
    os.utime(dump, (NIGHT.timestamp() - 3600, NIGHT.timestamp() - 3600))
    old_copy = tmp_path / "flash/immich-db/immich-db-backup-20260901T020000.sql.gz"
    old_copy.parent.mkdir()
    old_copy.write_bytes(b"old")
    os.utime(old_copy, (NIGHT.timestamp() - 30 * 86400,) * 2)

    patches = {
        "HOST": tmp_path,
        "SOURCES": tuple(sources),
        "EXCLUDES": (),
        "REPOSITORY": tmp_path / "flash/restic",
        "PASSWORD_FILE": tmp_path / "pass",
        "PACKAGE_LIST": tmp_path / "packages.txt",
        "DUMPS": dumps,
        "DUMP_COPIES": tmp_path / "flash/immich-db",
        "LOCK_FILE": tmp_path / "lock",
    }
    for name, value in patches.items():
        monkeypatch.setattr(backup, name, value)
    monkeypatch.setattr(backup, "FLASH_MOUNT", "/flash")
    monkeypatch.setenv("OREO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(clock, "check", lambda: Check("clock", True, "synchronised"))
    monkeypatch.setattr(storage, "read_mounts", lambda: {"/": False, "/flash": False})
    monkeypatch.setattr(
        storage,
        "run",
        lambda: [Check("mount /", True, "40% used"), Check("disk errors", True, "none")],
    )
    monkeypatch.setattr(
        drives, "run", lambda: [Check("Data (Hot Tier | data-hot-ssd1)", True, "healthy")]
    )
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda path: Usage(100, 0, 52 * 1024**3))
    monkeypatch.setattr(updates, "report", lambda: ["✅ OS: up to date", "✅ Images: up to date"])
    monkeypatch.setattr(
        net, "host_run", lambda command: subprocess.CompletedProcess(command, 3, "pkg-a\n", "")
    )
    return tmp_path


@pytest.fixture
def sent():
    return []


def go(sent, restic=None, now=NIGHT):
    restic = restic or FakeRestic()
    ok = backup.run(restic, send=lambda m, **k: sent.append((m, k)), now=now)
    return ok, restic


def test_report_matches_the_nightly_message(pi, sent):
    ok, restic = go(sent)
    assert ok
    [(message, kwargs)] = sent
    assert message == (
        "✅ System settings: 6 of 6 folders saved (4210 files, 3 new, 12 changed), "
        "30 versions kept\n"
        "✅ Immich database: 2K dump, copied to flash drive and verified, 1 copy kept\n"
        "✅ Data (Hot Tier | data-hot-ssd1): healthy\n"
        "✅ Storage: all drives mounted, none nearly full, no disk errors logged\n"
        "💾 Flash drive: 52G free\n"
        "✅ OS: up to date\n"
        "✅ Images: up to date"
    )
    assert kwargs == {"title": "Oreo Cloud OK", "tags": "white_check_mark"}
    assert restic.verbs() == ["unlock", "backup", "snapshots"]
    assert "--remove-all" not in restic.commands[0]
    assert "--host" in restic.commands[1] and "oreo-cloud" in restic.commands[1]
    assert restic.commands[1][6].endswith("state/restic-cache")


def test_dump_is_copied_and_old_copies_removed(pi, sent):
    go(sent)
    copies = sorted(p.name for p in (pi / "flash/immich-db").iterdir())
    assert copies == ["immich-db-backup-20261019T020000.sql.gz"]


def test_pre_update_dump_does_not_count_as_a_fresh_immich_dump(pi, sent):
    own = next((pi / "ssd/backups").iterdir())
    os.utime(own, (NIGHT.timestamp() - 40 * 3600,) * 2)
    pre_update = pi / "ssd/backups/pre-update-2026-10-19-0100.sql.gz"
    pre_update.write_bytes(b"x" * 4096)
    os.utime(pre_update, (NIGHT.timestamp() - 600,) * 2)
    ok, _ = go(sent)
    assert not ok
    assert "no Immich database dump newer than 36 hours" in sent[0][0]


def test_available_updates_are_listed_but_keep_the_report_ok(pi, sent, monkeypatch):
    lines = ["⬆️ OS: 3 updates available, 1 security", "⬆️ Images: newer immich-server"]
    monkeypatch.setattr(updates, "report", lambda: lines)
    go(sent)
    [(message, kwargs)] = sent
    assert message.endswith("\n".join(lines))
    assert kwargs["title"] == "Oreo Cloud OK"


def test_pre_update_dumps_are_copied_but_the_report_names_immichs_own(pi, sent):
    pre_update = pi / "ssd/backups/pre-update-2026-10-19-0100.sql.gz"
    pre_update.write_bytes(b"x" * 9000)
    os.utime(pre_update, (NIGHT.timestamp() - 600,) * 2)
    go(sent)
    assert (pi / "flash/immich-db" / pre_update.name).exists()
    assert "Immich database: 2K dump" in sent[0][0]


def test_drive_problem_turns_the_report_into_needs_attention(pi, sent, monkeypatch):
    monkeypatch.setattr(
        drives, "run", lambda: [Check("Data", False, "NEEDS ATTENTION, status FAILED")]
    )
    go(sent)
    assert sent[0][1]["title"] == "Oreo Cloud NEEDS ATTENTION"


def test_sunday_prunes_and_the_first_checks(pi, sent):
    sunday = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)  # 03:30 Sunday 25 Oct local
    dump = next((pi / "ssd/backups").iterdir())
    os.utime(dump, (sunday.timestamp() - 60,) * 2)
    _, restic = go(sent, now=sunday)
    assert "forget" in restic.verbs()
    first = datetime(2026, 10, 31, 22, 0, tzinfo=UTC)  # 03:30 Sunday 1 Nov local
    os.utime(dump, (first.timestamp() - 60,) * 2)
    _, restic = go(sent, now=first)
    assert "check" in restic.verbs()


def test_unmounted_flash_drive_skips_with_an_alert(pi, sent, monkeypatch):
    monkeypatch.setattr(storage, "read_mounts", lambda: {"/": False})
    ok, restic = go(sent)
    assert not ok
    assert restic.commands == []
    assert sent == [
        (
            "oreo backup: /flash is not mounted, job skipped",
            {"title": "Oreo Cloud FAILED", "priority": "high", "tags": "x"},
        )
    ]


def test_unsynchronised_clock_skips(pi, sent, monkeypatch):
    monkeypatch.setattr(clock, "check", lambda: Check("clock", False, "not synchronised"))
    ok, _ = go(sent)
    assert not ok
    assert "clock not synchronised" in sent[0][0]


def test_missing_source_skips(pi, sent):
    os.rmdir(backup.SOURCES[3])
    ok, restic = go(sent)
    assert not ok
    assert "is missing, job skipped" in sent[0][0]
    assert restic.commands == []


def test_failed_unlock_stops_before_backing_up(pi, sent):
    ok, restic = go(sent, FakeRestic(fail="unlock"))
    assert not ok
    assert restic.verbs() == ["unlock"]
    assert sent[0][0] == "osbackup: restic unlock failed: Fatal: repository is locked"


def test_restic_failure_alerts(pi, sent):
    ok, _ = go(sent, FakeRestic(fail="backup"))
    assert not ok
    assert sent[0][0] == "osbackup: restic backup failed: Fatal: repository is locked"


def test_stale_dump_alerts(pi, sent):
    dump = next((pi / "ssd/backups").iterdir())
    os.utime(dump, (NIGHT.timestamp() - 40 * 3600,) * 2)
    ok, _ = go(sent)
    assert not ok
    assert "no Immich database dump newer than 36 hours" in sent[0][0]


def legacy(active):
    return lambda command: subprocess.CompletedProcess(command, 0 if active else 3, "", "")


def test_nightly_runs_once_a_day_after_the_time(pi, sent, monkeypatch):
    runs = []
    monkeypatch.setattr(backup, "run", lambda **kwargs: runs.append(kwargs["now"]))
    monkeypatch.setattr(net, "host_run", legacy(False))
    before = datetime(2026, 10, 18, 21, 50, tzinfo=UTC)  # 03:20 local
    backup.nightly(send=sent.append, now=before)
    assert runs == []
    backup.nightly(send=sent.append, now=NIGHT)
    backup.nightly(send=sent.append, now=datetime(2026, 10, 19, 4, 0, tzinfo=UTC))
    assert runs == [NIGHT]


def test_nightly_catches_up_after_the_pi_was_off(pi, sent, monkeypatch):
    runs = []
    monkeypatch.setattr(backup, "run", lambda **kwargs: runs.append(1))
    monkeypatch.setattr(net, "host_run", legacy(False))
    backup.nightly(send=sent.append, now=datetime(2026, 10, 19, 3, 0, tzinfo=UTC))  # 08:30 local
    assert runs == [1]


def test_nightly_stands_aside_while_the_host_timer_is_active(pi, sent, monkeypatch):
    runs = []
    monkeypatch.setattr(backup, "run", lambda **kwargs: runs.append(1))
    monkeypatch.setattr(net, "host_run", legacy(True))
    backup.nightly(send=lambda m, **k: sent.append(m), now=NIGHT)
    assert runs == []
    assert "sudo systemctl disable --now oreo-osbackup.timer" in sent[0]


def test_prune_groups_old_and_new_snapshots_by_host(pi, sent):
    sunday = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    dump = next((pi / "ssd/backups").iterdir())
    os.utime(dump, (sunday.timestamp() - 60,) * 2)
    _, restic = go(sent, now=sunday)
    [forget] = [c for c in restic.commands if c[7] == "forget"]
    assert forget[forget.index("--group-by") + 1] == "host"


def test_report_is_retried_and_never_fails_a_good_backup(pi):
    attempts, waits = [], []

    def flaky(message, **kwargs):
        attempts.append(message)
        raise backup.notify.NotifyError("ntfy unreachable")

    ok = backup.run(FakeRestic(), send=flaky, now=NIGHT, sleep=waits.append)
    assert ok
    assert len(attempts) == backup.SEND_ATTEMPTS
    assert waits == [backup.SEND_RETRY_SECONDS] * (backup.SEND_ATTEMPTS - 1)


def test_report_delivered_on_a_later_attempt(pi):
    attempts = []

    def second_time_lucky(message, **kwargs):
        attempts.append(1)
        if len(attempts) < 2:
            raise backup.notify.NotifyError("ntfy unreachable")

    assert backup.run(FakeRestic(), send=second_time_lucky, now=NIGHT, sleep=lambda s: None)
    assert len(attempts) == 2


def test_success_is_recorded_and_failure_is_not(pi, sent, monkeypatch):
    go(sent)
    recorded = json.loads((pi / "state/backup-success.json").read_text())["time"]
    assert recorded == NIGHT.isoformat()
    (pi / "state/backup-success.json").unlink()
    monkeypatch.setattr(storage, "read_mounts", lambda: {"/": False})
    go(sent)
    assert not (pi / "state/backup-success.json").exists()


def test_interrupted_backup_is_tried_once_more_the_same_day(pi, sent, monkeypatch):
    runs = []
    monkeypatch.setattr(net, "host_run", legacy(False))

    def killed(**kwargs):
        runs.append(1)
        raise SystemExit("agent stopped mid-run")

    monkeypatch.setattr(backup, "run", killed)
    with pytest.raises(SystemExit):
        backup.nightly(send=sent.append, now=NIGHT)
    with pytest.raises(SystemExit):
        backup.nightly(send=sent.append, now=NIGHT)
    backup.nightly(send=sent.append, now=NIGHT)  # two attempts used up
    assert runs == [1, 1]


def test_finished_backup_is_not_repeated(pi, sent, monkeypatch):
    runs = []
    monkeypatch.setattr(net, "host_run", legacy(False))
    monkeypatch.setattr(backup, "run", lambda **kwargs: runs.append(1) or False)
    backup.nightly(send=sent.append, now=NIGHT)
    backup.nightly(send=sent.append, now=NIGHT)
    assert runs == [1]


ACTIVE = Check("oreo-osbackup.timer", False, "still active")
RETIRED = Check("oreo-osbackup.timer", True, "disabled")


def test_freshness_passes_while_the_host_timer_still_backs_up(pi):
    assert backup.freshness_check(NIGHT, ACTIVE).ok


def test_freshness_fails_without_any_agent_backup(pi):
    check = backup.freshness_check(NIGHT, RETIRED)
    assert check == Check("backup", False, "no successful backup recorded by the agent yet")


def test_freshness_after_a_backup(pi, sent):
    go(sent)
    later = NIGHT + timedelta(hours=1)
    assert backup.freshness_check(later, RETIRED) == Check(
        "backup", True, "last successful backup 1 hour ago"
    )
    assert not backup.freshness_check(NIGHT + timedelta(hours=27), RETIRED).ok
