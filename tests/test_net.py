import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from oreo import net, notify

START = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)


class FakeHost:
    """Stands in for nsenter: records host commands and answers ip and ping."""

    def __init__(self, gateway_up: bool = False, has_route: bool = True, legacy: bool = False):
        self.legacy = legacy
        self.gateway_up = gateway_up
        self.has_route = has_route
        self.commands: list[list[str]] = []

    def __call__(self, command):
        self.commands.append(list(command))
        out, code = "", 0
        if command[:3] == ["ip", "-4", "route"]:
            out = "default via 192.168.0.1 dev wlan0 proto dhcp\n" if self.has_route else ""
        elif command[0] == "systemctl":
            code = 0 if self.legacy else 3
        elif command[0] == "ping":
            code = 0 if self.gateway_up else 1
        elif command[:3] == ["ip", "-4", "-br"]:
            out = "wlan0 UP 192.168.0.228/24\n"
        return subprocess.CompletedProcess(command, code, out, "")

    def repairs(self) -> list[str]:
        return [c[0] for c in self.commands if c[0] in ("networkctl", "netplan")]


@pytest.fixture(autouse=True)
def state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OREO_STATE_DIR", str(tmp_path))


@pytest.fixture
def sent():
    return []


def check(host, sent, minutes=0):
    def send(message, **kwargs):
        sent.append((message, kwargs))

    return net.run_once(host, send=send, now=START + timedelta(minutes=2 * minutes))


def test_healthy_network_does_nothing(sent):
    host = FakeHost(gateway_up=True)
    assert check(host, sent)
    assert host.repairs() == []
    assert sent == []


def test_first_failure_waits(sent):
    host = FakeHost()
    assert not check(host, sent)
    assert host.repairs() == []


def test_second_failure_renews_the_address(sent):
    host = FakeHost()
    check(host, sent, 0)
    check(host, sent, 1)
    assert host.repairs() == ["networkctl"]
    assert host.commands[-1] == ["networkctl", "reconfigure", "wlan0"]


def test_third_failure_restarts_wifi_then_every_fifth_check(sent):
    host = FakeHost()
    for step in range(1, 9):
        check(host, sent, step)
    # checks 1-8: renew on 2, restart on 3 and again on 8
    assert host.repairs() == ["networkctl", "netplan", "netplan"]


def test_no_default_route_counts_as_down(sent):
    host = FakeHost(has_route=False)
    assert not check(host, sent)
    assert not [c for c in host.commands if c[0] == "ping"]


def test_recovery_message_names_the_fix_and_the_address(sent):
    host = FakeHost()
    for step in range(3):
        check(host, sent, step)
    host.gateway_up = True
    assert check(host, sent, 3)
    [(message, kwargs)] = sent
    assert message == (
        "✅ Network: back online after about 6 minutes (down from 11:30, back at 11:36)\n"
        "🔧 Recovery: automatic Wi-Fi restart\n"
        "📶 Wi-Fi address: 192.168.0.228/24"
    )
    assert kwargs == {"title": "Oreo Cloud OK", "tags": "white_check_mark"}


def test_recovery_without_a_repair_says_it_came_back_alone(sent):
    host = FakeHost()
    check(host, sent, 0)
    host.gateway_up = True
    check(host, sent, 1)
    assert "none needed, it came back by itself" in sent[0][0]


def test_recovery_is_reported_once(sent):
    host = FakeHost()
    check(host, sent, 0)
    host.gateway_up = True
    check(host, sent, 1)
    check(host, sent, 2)
    assert len(sent) == 1


def test_failed_recovery_alert_is_retried_next_check(sent):
    host = FakeHost()
    check(host, sent, 0)
    host.gateway_up = True

    def broken(message, **kwargs):
        raise notify.NotifyError("ntfy unreachable")

    with pytest.raises(notify.NotifyError):
        net.run_once(host, send=broken, now=START + timedelta(minutes=2))
    check(host, sent, 2)
    assert len(sent) == 1


def test_host_commands_run_through_nsenter(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(net.subprocess, "run", fake_run)
    net.host_run(["netplan", "apply"])
    assert seen["argv"][:2] == ["nsenter", "--target"]
    assert seen["argv"][-2:] == ["netplan", "apply"]


# START is 11:30 in Asia/Kolkata; the router restarts at 06:00 local, which is 00:30 UTC.
ROUTER_RESTART = datetime(2026, 10, 19, 0, 30, tzinfo=UTC)


def at(minutes):
    return ROUTER_RESTART + timedelta(minutes=minutes)


def test_quiet_window_defaults_to_the_router_restart():
    assert net.in_quiet_window(at(0))
    assert net.in_quiet_window(at(-5))
    assert not net.in_quiet_window(at(15))
    assert not net.in_quiet_window(START)


def test_outage_inside_the_window_is_neither_repaired_nor_reported(sent):
    host = FakeHost()
    for minute in (0, 2, 4, 6, 8):
        net.run_once(host, send=sent.append, now=at(minute))
    host.gateway_up = True
    net.run_once(host, send=sent.append, now=at(10))
    assert host.repairs() == []
    assert sent == []


def test_outage_past_the_window_is_repaired_after_it_closes(sent):
    host = FakeHost()
    for minute in (10, 12, 14, 16, 18, 20):
        net.run_once(host, send=lambda m, **k: sent.append(m), now=at(minute))
    # 10, 12, 14 are inside the window; 16 and 18 are failures 1 and 2 after it
    assert host.repairs() == ["networkctl", "netplan"]
    host.gateway_up = True
    net.run_once(host, send=lambda m, **k: sent.append(m), now=at(22))
    assert "down from 06:10" in sent[0]


def test_empty_quiet_window_disables_it(monkeypatch):
    monkeypatch.setenv("OREO_NET_QUIET_WINDOW", "")
    assert not net.in_quiet_window(at(0))


def test_quiet_window_can_cross_midnight(monkeypatch):
    monkeypatch.setenv("OREO_NET_QUIET_WINDOW", "23:50-00:10")
    assert net.in_quiet_window(datetime(2026, 10, 19, 18, 25, tzinfo=UTC))  # 23:55 local
    assert net.in_quiet_window(datetime(2026, 10, 19, 18, 35, tzinfo=UTC))  # 00:05 local
    assert not net.in_quiet_window(datetime(2026, 10, 19, 18, 45, tzinfo=UTC))


def test_active_host_timer_stops_repairs_and_alerts_once(sent):
    host = FakeHost(legacy=True)
    for step in range(4):
        assert not check(host, sent, step)
    assert host.repairs() == []
    [(message, kwargs)] = sent
    assert "oreo-netwatch.timer: still active on the host" in message
    assert "sudo systemctl disable --now oreo-netwatch.timer" in message
    assert kwargs["title"] == "Oreo Cloud NEEDS ATTENTION"


def test_repairs_resume_once_the_host_timer_is_disabled(sent):
    host = FakeHost(legacy=True)
    check(host, sent, 0)
    host.legacy = False
    check(host, sent, 1)
    check(host, sent, 2)
    assert host.repairs() == ["networkctl"]
    assert sent[1][1]["title"] == "Oreo Cloud OK"


def test_unsendable_timer_message_does_not_block_repairs():
    host = FakeHost(legacy=True)

    def offline(message, **kwargs):
        raise notify.NotifyError("ntfy unreachable")

    net.run_once(host, send=offline, now=START)
    host.legacy = False  # disabled mid-outage: the "back to normal" message cannot be sent
    for step in (1, 2):
        net.run_once(host, send=offline, now=START + timedelta(minutes=2 * step))
    assert host.repairs() == ["networkctl"]
