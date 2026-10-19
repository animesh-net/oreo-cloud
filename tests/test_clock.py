import subprocess

from oreo import clock


def host(stdout="", code=0, stderr=""):
    def run(command):
        assert command[0] == "timedatectl"
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    return run


def test_synchronised():
    check = clock.check(host("yes\n"))
    assert check.ok
    assert check.detail == "synchronised"


def test_not_synchronised():
    check = clock.check(host("no\n"))
    assert not check.ok
    assert check.detail == "not synchronised with NTP"


def test_timedatectl_failure_fails_the_check():
    check = clock.check(host(code=1, stderr="Failed to connect to bus"))
    assert not check.ok
    assert "Failed to connect to bus" in check.detail


def test_no_host_access_fails_the_check():
    def missing(command):
        raise FileNotFoundError("nsenter")

    assert not clock.check(missing).ok
