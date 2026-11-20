import fcntl
import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from oreo import backup, power

# 00:30 in Asia/Kolkata on Sunday 15 November 2026
OFF = datetime(2026, 11, 14, 19, 0, tzinfo=UTC)
# 06:30 the same morning
WAKE = datetime(2026, 11, 15, 1, 0, tzinfo=UTC)


class FakeHost:
    """Answers the host commands the night off runs, and records them."""

    def __init__(self, eeprom="POWER_OFF_ON_HALT=1\nWAKE_ON_GPIO=0\n", alarm=None, os_update=False):
        self.commands = []
        self.eeprom = eeprom
        self.alarm = alarm  # None: read back whatever was written
        self.os_update = os_update
        self.written = None

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        if command == ["rpi-eeprom-config"]:
            return subprocess.CompletedProcess(command, 0 if self.eeprom else 1, self.eeprom, "")
        if command[:2] == ["sh", "-c"]:
            self.written = command[2].split("echo ")[2].split(" >")[0]
            out = self.written if self.alarm is None else self.alarm
            return subprocess.CompletedProcess(command, 0, out + "\n", "")
        if command[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(command, 0 if self.os_update else 3, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    @property
    def powered_off(self):
        return ["systemctl", "poweroff", "--no-block"] in self.commands


@pytest.fixture
def pi(tmp_path, monkeypatch):
    monkeypatch.setenv("OREO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OREO_POWER_OFF_AT", "00:30")
    monkeypatch.setenv("OREO_POWER_ON_AT", "06:30")
    monkeypatch.setattr(backup, "LOCK_FILE", tmp_path / "lock")
    boot = tmp_path / "stat"
    boot.write_text(f"cpu 1 2 3\nbtime {int((OFF - timedelta(days=3)).timestamp())}\n")
    monkeypatch.setattr(power, "BOOT_TIME", boot)
    return tmp_path


def booted(pi, moment):
    (pi / "stat").write_text(f"btime {int(moment.timestamp())}\n")


def test_powers_off_with_a_verified_alarm_for_the_morning(pi):
    host, sent = FakeHost(), []
    assert power.run_once(host, send=lambda m, **k: sent.append(m), now=OFF)
    assert host.written == str(int(WAKE.timestamp()))
    assert host.powered_off
    assert sent == ["🌙 Night off: powering off now, back on at 06:30"]


def test_does_nothing_before_the_off_time_or_without_a_schedule(pi, monkeypatch):
    host = FakeHost()
    assert not power.run_once(host, send=lambda m, **k: None, now=OFF - timedelta(minutes=1))
    monkeypatch.setenv("OREO_POWER_OFF_AT", "")
    assert not power.run_once(host, send=lambda m, **k: None, now=OFF)
    assert host.commands == []


def test_powers_off_once_per_night(pi):
    host = FakeHost()
    power.run_once(host, send=lambda m, **k: None, now=OFF)
    host.commands.clear()
    # Switched on by hand during the window: it must stay on.
    assert not power.run_once(host, send=lambda m, **k: None, now=OFF + timedelta(minutes=10))
    assert not host.powered_off


def test_stays_on_when_the_alarm_does_not_read_back(pi):
    host, sent = FakeHost(alarm="0"), []
    assert not power.run_once(host, send=lambda m, **k: sent.append(m), now=OFF)
    assert not host.powered_off
    assert "wake alarm reads back 0" in sent[0]


def test_stays_on_without_the_bootloader_setting(pi):
    host, sent = FakeHost(eeprom="WAKE_ON_GPIO=1\n"), []
    assert not power.run_once(host, send=lambda m, **k: sent.append(m), now=OFF)
    assert not host.powered_off
    assert host.written is None
    assert "POWER_OFF_ON_HALT=1 is not set" in sent[0]


def test_waits_for_a_running_backup_then_powers_off(pi):
    host, sent = FakeHost(), []
    with backup.LOCK_FILE.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert not power.run_once(host, send=lambda m, **k: sent.append(m), now=OFF)
    assert not host.powered_off
    assert power.run_once(host, send=lambda m, **k: sent.append(m), now=OFF + timedelta(minutes=5))
    assert host.powered_off


def test_skips_the_night_when_busy_for_the_whole_window(pi):
    host, sent = FakeHost(os_update=True), []
    power.run_once(host, send=lambda m, **k: sent.append(m), now=OFF)
    power.run_once(host, send=lambda m, **k: sent.append(m), now=OFF + timedelta(minutes=31))
    power.run_once(host, send=lambda m, **k: sent.append(m), now=OFF + timedelta(minutes=32))
    assert not host.powered_off
    assert sent == [
        "🌙 Night off skipped: an OS update is running until 01:00. It stays on tonight."
    ]


def test_reports_the_wake_up_once(pi):
    host, sent = FakeHost(), []
    power.run_once(host, send=lambda m, **k: sent.append(m), now=OFF)
    booted(pi, WAKE + timedelta(minutes=1))
    later = WAKE + timedelta(minutes=2)
    power.run_once(host, send=lambda m, **k: sent.append(m), now=later)
    power.run_once(host, send=lambda m, **k: sent.append(m), now=later + timedelta(minutes=1))
    assert sent[1:] == ["☀️ Night off: back on at 06:31 (off at 00:30, wake alarm 06:30)"]


def test_warns_when_the_pi_never_powered_off(pi):
    host, sent = FakeHost(), []
    power.run_once(host, send=lambda m, **k: sent.append(m), now=OFF)
    power.run_once(host, send=lambda m, **k: sent.append(m), now=WAKE)
    assert sent[1] == "⚠️ Night off: the Pi did not power off at 00:30; it stayed on"


def test_state_survives_a_restart_of_the_agent(pi):
    power.run_once(FakeHost(), send=lambda m, **k: None, now=OFF)
    state = json.loads((pi / "state/power.json").read_text())
    assert state["date"] == "2026-11-15"
    assert datetime.fromisoformat(state["wake_at"]) == WAKE


def test_try_wake_refuses_without_a_verified_alarm(pi):
    host = FakeHost(alarm="")
    lines = power.try_wake(10, host, now=OFF)
    assert lines[0].startswith("FAIL")
    assert not host.powered_off


def test_try_wake_sets_the_alarm_minutes_ahead(pi):
    host = FakeHost()
    lines = power.try_wake(10, host, now=OFF)
    assert host.written == str(int((OFF + timedelta(minutes=10)).timestamp()))
    assert host.powered_off
    assert lines == ["OK    powering off; the Pi should switch itself on at 00:40"]


# 22:30 in Asia/Kolkata on Friday 20 November 2026, and 08:30 the next morning
EVENING = datetime(2026, 11, 20, 17, 0, tzinfo=UTC)
MORNING = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)


@pytest.fixture
def evening(pi, monkeypatch):
    monkeypatch.setenv("OREO_POWER_OFF_AT", "22:30")
    monkeypatch.setenv("OREO_POWER_ON_AT", "08:30")
    return pi


def test_evening_schedule_wakes_the_next_morning(evening):
    host, sent = FakeHost(), []
    assert power.run_once(host, send=lambda m, **k: sent.append(m), now=EVENING)
    assert host.written == str(int(MORNING.timestamp()))
    assert sent == ["🌙 Night off: powering off now, back on at 08:30"]


def test_evening_schedule_stays_on_after_the_morning_wake(evening):
    host, sent = FakeHost(), []
    power.run_once(host, send=lambda m, **k: sent.append(m), now=EVENING)
    booted(evening, MORNING + timedelta(minutes=1))
    host.commands.clear()
    for minutes in (2, 120, 600):
        assert not power.run_once(
            host, send=lambda m, **k: sent.append(m), now=MORNING + timedelta(minutes=minutes)
        )
    assert not host.powered_off
    assert sent[1:] == ["☀️ Night off: back on at 08:31 (off at 22:30, wake alarm 08:30)"]


def test_window_across_midnight_still_powers_off(evening, monkeypatch):
    monkeypatch.setenv("OREO_POWER_OFF_AT", "23:50")
    host = FakeHost()
    # 00:05, inside the 30-minute window that started at 23:50 the evening before
    assert power.run_once(host, send=lambda m, **k: None, now=EVENING + timedelta(minutes=95))
    assert host.powered_off


def test_status_flags_a_backup_inside_the_off_hours(evening, monkeypatch):
    monkeypatch.setenv("OREO_BACKUP_TIME", "23:30")
    lines = power.status(FakeHost(), now=EVENING)
    assert lines[1].startswith("Backup: FAIL at 23:30")
    monkeypatch.setenv("OREO_BACKUP_TIME", "21:30")
    assert power.status(FakeHost(), now=EVENING)[1] == (
        "Backup: OK   at 21:30, before the Pi powers off"
    )
