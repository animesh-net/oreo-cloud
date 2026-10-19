import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from oreo import net, notify

START = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)


class FakeHost:
    """Stands in for nsenter: records host commands and answers ip and ping."""

    def __init__(self, gateway_up: bool = False, has_route: bool = True):
        self.gateway_up = gateway_up
        self.has_route = has_route
        self.commands: list[list[str]] = []

    def __call__(self, command):
        self.commands.append(list(command))
        out, code = "", 0
        if command[:3] == ["ip", "-4", "route"]:
            out = "default via 192.168.0.1 dev wlan0 proto dhcp\n" if self.has_route else ""
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
