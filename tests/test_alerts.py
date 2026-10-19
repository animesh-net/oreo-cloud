import json
from datetime import UTC, datetime, timedelta

import pytest

from oreo import alerts, notify
from oreo.checks import Check

START = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
DISK_FAIL = [Check("disk /", False, "95% used")]
MEMORY_FAIL = [Check("memory", False, "96% used")]
PASSING = [Check("disk /", True, "40% used")]


@pytest.fixture(autouse=True)
def state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OREO_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def sent():
    return []


def run(sent, checks, minutes=0):
    def send(message, **kwargs):
        sent.append((message, kwargs))

    alerts.notify_on_change(
        "system", "System checks", checks, send=send, now=START + timedelta(minutes=minutes)
    )


def test_passing_checks_send_nothing(sent):
    run(sent, PASSING)
    assert sent == []


def test_first_failure_alerts(sent):
    run(sent, DISK_FAIL)
    [(message, kwargs)] = sent
    assert message == "⚠️ System checks: 1 problem\ndisk /: 95% used"
    assert kwargs == {"title": "Oreo Cloud NEEDS ATTENTION", "priority": "high", "tags": "warning"}


def test_same_failure_does_not_repeat(sent):
    run(sent, DISK_FAIL)
    run(sent, DISK_FAIL, minutes=5)
    run(sent, DISK_FAIL, minutes=10)
    assert len(sent) == 1


def test_changed_failure_alerts_again(sent):
    run(sent, DISK_FAIL)
    run(sent, DISK_FAIL + MEMORY_FAIL, minutes=5)
    assert len(sent) == 2
    assert sent[1][0].startswith("⚠️ System checks: 2 problems")


def test_reminder_after_the_configured_hours(sent, monkeypatch):
    monkeypatch.setenv("OREO_ALERT_REMINDER_HOURS", "1")
    run(sent, DISK_FAIL)
    run(sent, DISK_FAIL, minutes=59)
    assert len(sent) == 1
    run(sent, DISK_FAIL, minutes=61)
    assert len(sent) == 2


def test_recovery_message_reports_duration_in_local_time(sent):
    run(sent, DISK_FAIL)
    run(sent, PASSING, minutes=25)
    message, kwargs = sent[-1]
    assert message == (
        "✅ System checks: back to normal after about 25 minutes "
        "(problem from 11:30, resolved at 11:55)"
    )
    assert kwargs == {"title": "Oreo Cloud OK", "tags": "white_check_mark"}


def test_recovery_is_sent_once(sent):
    run(sent, DISK_FAIL)
    run(sent, PASSING, minutes=5)
    run(sent, PASSING, minutes=10)
    assert len(sent) == 2


def test_failed_send_keeps_state_so_the_next_run_retries(sent, state_dir):
    def broken(message, **kwargs):
        raise notify.NotifyError("down")

    with pytest.raises(notify.NotifyError):
        alerts.notify_on_change("system", "System checks", DISK_FAIL, send=broken, now=START)
    assert not (state_dir / "system.json").exists()
    run(sent, DISK_FAIL, minutes=5)
    assert len(sent) == 1


def test_keys_are_independent(sent, state_dir):
    run(sent, DISK_FAIL)
    alerts.notify_on_change(
        "health", "Containers", DISK_FAIL, send=lambda m, **k: sent.append((m, k)), now=START
    )
    assert len(sent) == 2
    assert json.loads((state_dir / "health.json").read_text())["failing"] == ["disk /"]


def test_corrupt_state_is_treated_as_empty(sent, state_dir):
    (state_dir / "system.json").write_text("{not json")
    run(sent, DISK_FAIL)
    assert len(sent) == 1


def test_a_leftover_temporary_file_does_not_block_saving(sent, state_dir):
    leftover = state_dir / "system.tmp"
    leftover.write_text("{}")
    leftover.chmod(0o400)
    run(sent, DISK_FAIL)
    assert json.loads((state_dir / "system.json").read_text())["failing"] == ["disk /"]
    assert [p.name for p in state_dir.iterdir() if p.suffix == ".tmp"] == ["system.tmp"]


def test_one_minute_is_singular(sent):
    run(sent, DISK_FAIL)
    run(sent, PASSING, minutes=1)
    assert "after about 1 minute (" in sent[-1][0]
