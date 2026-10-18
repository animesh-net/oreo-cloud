from collections import namedtuple

from oreo import system

Usage = namedtuple("Usage", "total used free")


def test_disk_below_threshold_passes(monkeypatch):
    monkeypatch.setattr(system.shutil, "disk_usage", lambda path: Usage(100, 40, 60))
    check = system.check_disk("/", 90)
    assert check.ok
    assert check.detail == "40% used"


def test_disk_above_threshold_fails(monkeypatch):
    monkeypatch.setattr(system.shutil, "disk_usage", lambda path: Usage(100, 95, 5))
    assert not system.check_disk("/", 90).ok


def test_missing_disk_path_fails():
    check = system.check_disk("/does/not/exist", 90)
    assert not check.ok


def test_memory_uses_available_memory(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:  1000 kB\nMemFree:  100 kB\nMemAvailable:  250 kB\n")
    check = system.check_memory(90, meminfo)
    assert check.ok
    assert check.detail == "75% used"


def test_memory_above_threshold_fails(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:  1000 kB\nMemAvailable:  50 kB\n")
    assert not system.check_memory(90, meminfo).ok


def test_unreadable_meminfo_fails(tmp_path):
    assert not system.check_memory(90, tmp_path / "missing").ok


def test_temperature_reads_millidegrees(tmp_path):
    sensor = tmp_path / "temp"
    sensor.write_text("52345\n")
    check = system.check_temperature(75, sensor)
    assert check.ok
    assert check.detail == "52.3°C"


def test_hot_temperature_fails(tmp_path):
    sensor = tmp_path / "temp"
    sensor.write_text("81000\n")
    assert not system.check_temperature(75, sensor).ok


def test_missing_temperature_sensor_passes(tmp_path):
    check = system.check_temperature(75, tmp_path / "missing")
    assert check.ok
    assert check.detail == "no sensor"


def test_load_is_per_cpu(monkeypatch):
    monkeypatch.setattr(system.os, "getloadavg", lambda: (6.0, 0, 0))
    monkeypatch.setattr(system.os, "cpu_count", lambda: 4)
    check = system.check_load(2)
    assert check.ok
    assert check.detail == "1.50 per CPU (1 min)"


def test_run_checks_every_configured_disk(monkeypatch):
    monkeypatch.setenv("OREO_DISK_PATHS", "/, /tmp")
    names = [check.name for check in system.run()]
    assert names[:2] == ["disk /", "disk /tmp"]
    assert names[2:] == ["memory", "temperature", "load"]


def test_unmounted_required_path_fails(tmp_path):
    check = system.check_disk(str(tmp_path), 90, require_mount=True)
    assert not check.ok
    assert check.detail == "not mounted"


def test_mounted_required_path_is_checked(monkeypatch, tmp_path):
    monkeypatch.setattr(system.os.path, "ismount", lambda path: True)
    monkeypatch.setattr(system.shutil, "disk_usage", lambda path: Usage(100, 40, 60))
    assert system.check_disk(str(tmp_path), 90, require_mount=True).ok


def test_run_requires_listed_mount_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("OREO_DISK_PATHS", f"/, {tmp_path}")
    monkeypatch.setenv("OREO_MOUNT_PATHS", str(tmp_path))
    by_name = {check.name: check for check in system.run()}
    assert not by_name[f"disk {tmp_path}"].ok
