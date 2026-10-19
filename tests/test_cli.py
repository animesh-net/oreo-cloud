import json

import pytest

from oreo import cli, health, net, notify, system
from oreo.checks import Check

PASSING = [Check("disk /", True, "40% used")]
FAILING = [Check("disk /", False, "95% used"), Check("memory", True, "50% used")]


@pytest.fixture
def alerts(monkeypatch, tmp_path):
    monkeypatch.setenv("OREO_STATE_DIR", str(tmp_path))
    sent = []
    monkeypatch.setattr(notify, "send", lambda message, **kwargs: sent.append((message, kwargs)))
    return sent


def test_passing_checks_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(system, "run", lambda: PASSING)
    assert cli.main(["system"]) == 0
    assert capsys.readouterr().out == "OK    disk /: 40% used\n"


def test_failing_checks_exit_one(monkeypatch, capsys):
    monkeypatch.setattr(health, "run", lambda: FAILING)
    assert cli.main(["health"]) == 1
    assert "FAIL  disk /: 95% used" in capsys.readouterr().out


def test_json_output(monkeypatch, capsys):
    monkeypatch.setattr(system, "run", lambda: FAILING)
    cli.main(["system", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["checks"][0] == {"name": "disk /", "ok": False, "detail": "95% used"}


def test_notify_flag_alerts_only_failures(monkeypatch, alerts):
    monkeypatch.setattr(system, "run", lambda: FAILING)
    cli.main(["system", "--notify"])
    [(message, kwargs)] = alerts
    assert message == "⚠️ System checks: 1 problem\ndisk /: 95% used"
    assert kwargs["title"] == "Oreo Cloud NEEDS ATTENTION"
    assert kwargs["priority"] == "high"


def test_notify_flag_alerts_once_while_failing(monkeypatch, alerts):
    monkeypatch.setattr(system, "run", lambda: FAILING)
    cli.main(["system", "--notify"])
    cli.main(["system", "--notify"])
    assert len(alerts) == 1


def test_notify_flag_reports_recovery(monkeypatch, alerts):
    monkeypatch.setattr(system, "run", lambda: FAILING)
    cli.main(["system", "--notify"])
    monkeypatch.setattr(system, "run", lambda: PASSING)
    assert cli.main(["system", "--notify"]) == 0
    assert alerts[-1][0].startswith("✅ System checks: back to normal")
    assert alerts[-1][1]["title"] == "Oreo Cloud OK"


def test_notify_flag_silent_when_passing(monkeypatch, alerts):
    monkeypatch.setattr(system, "run", lambda: PASSING)
    cli.main(["system", "--notify"])
    assert alerts == []


def test_failed_alert_still_reports_check_failure(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("OREO_STATE_DIR", str(tmp_path))

    def fail(message, **kwargs):
        raise notify.NotifyError("OREO_NTFY_URL is not set")

    monkeypatch.setattr(system, "run", lambda: FAILING)
    monkeypatch.setattr(notify, "send", fail)
    assert cli.main(["system", "--notify"]) == 1
    assert "OREO_NTFY_URL is not set" in capsys.readouterr().err


def test_notify_command(alerts):
    assert cli.main(["notify", "backup done", "--title", "oreo", "--tags", "white_check_mark"]) == 0
    [(message, kwargs)] = alerts
    assert message == "backup done"
    assert kwargs == {"title": "oreo", "priority": None, "tags": "white_check_mark"}


def test_notify_command_error_exits_one(monkeypatch, capsys):
    def fail(message, **kwargs):
        raise notify.NotifyError("OREO_NTFY_URL is not set")

    monkeypatch.setattr(notify, "send", fail)
    assert cli.main(["notify", "hello"]) == 1
    assert "OREO_NTFY_URL is not set" in capsys.readouterr().err


def test_net_command_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(net, "run_once", lambda: True)
    assert cli.main(["net"]) == 0
    monkeypatch.setattr(net, "run_once", lambda: False)
    assert cli.main(["net"]) == 1
    assert "FAIL  gateway does not answer" in capsys.readouterr().out


def test_net_command_reports_missing_host_access(monkeypatch, capsys):
    def no_nsenter():
        raise FileNotFoundError("nsenter")

    monkeypatch.setattr(net, "run_once", no_nsenter)
    assert cli.main(["net"]) == 2
    assert "nsenter" in capsys.readouterr().err
