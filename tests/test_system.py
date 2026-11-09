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


def _stat(tmp_path, busy, idle):
    path = tmp_path / "stat"
    path.write_text(f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 0 0 1 0 0 0 0 0 0\n")
    return path


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_memory_over_the_limit_fails_only_once_it_lasts(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "_high_since", {})
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:  1000 kB\nMemAvailable:  200 kB\n")
    clock, results = Clock(), []
    for minute in range(3):
        clock.now = minute * 60
        results.append(system.check_memory(70, meminfo, 120, clock))
    assert [check.ok for check in results] == [True, True, False]
    assert results[2].detail == "80% used, over 70% for 2 min"


def test_memory_back_under_the_limit_resets_the_timer(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "_high_since", {})
    meminfo = tmp_path / "meminfo"
    clock = Clock()
    meminfo.write_text("MemTotal:  1000 kB\nMemAvailable:  200 kB\n")
    system.check_memory(70, meminfo, 120, clock)
    meminfo.write_text("MemTotal:  1000 kB\nMemAvailable:  500 kB\n")
    clock.now = 60
    assert system.check_memory(70, meminfo, 120, clock).ok
    assert "memory" not in system._high_since


def test_hot_temperature_fails_only_once_it_lasts(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "_high_since", {})
    sensor = tmp_path / "temp"
    sensor.write_text("58000\n")
    clock, results = Clock(), []
    for minute in range(3):
        clock.now = minute * 60
        results.append(system.check_temperature(55, sensor, 120, clock))
    assert [check.ok for check in results] == [True, True, False]
    assert results[2].detail == "58.0°C, over 55°C for 2 min"


def test_cpu_usage_is_measured_between_readings(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "_cpu_last", (0.0, 100, 200))
    monkeypatch.setattr(system, "_cpu_high_since", None)
    clock = Clock()
    clock.now = 60
    check = system.check_cpu(50, 120, _stat(tmp_path, 130, 170), clock)
    assert check == system.Check("cpu", True, "30% used")


def test_cpu_over_the_limit_fails_only_once_it_lasts(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "_cpu_last", (0.0, 0, 0))
    monkeypatch.setattr(system, "_cpu_high_since", None)
    clock, results = Clock(), []
    for minute, busy in enumerate([80, 160, 240], start=1):
        clock.now = minute * 60
        results.append(system.check_cpu(50, 120, _stat(tmp_path, busy, minute * 20), clock))
    assert [check.ok for check in results] == [True, False, False]
    assert results[1].detail == "80% used, over 50% for 2 min"


def test_cpu_back_under_the_limit_resets_the_timer(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "_cpu_last", (0.0, 0, 0))
    monkeypatch.setattr(system, "_cpu_high_since", None)
    clock = Clock()
    clock.now = 60
    system.check_cpu(50, 120, _stat(tmp_path, 80, 20), clock)
    clock.now = 120
    assert system.check_cpu(50, 120, _stat(tmp_path, 90, 110), clock).ok
    assert system._cpu_high_since is None


def test_first_cpu_reading_samples_for_a_second(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "_cpu_last", None)
    slept = []
    check = system.check_cpu(50, 120, _stat(tmp_path, 10, 10), Clock(), slept.append)
    assert slept == [1]
    assert check.ok


def test_unreadable_proc_stat_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "_cpu_last", None)
    assert not system.check_cpu(50, 120, tmp_path / "missing", Clock(), lambda s: None).ok


def test_run_checks_every_configured_disk(monkeypatch):
    monkeypatch.setenv("OREO_DISK_PATHS", "/, /tmp")
    names = [check.name for check in system.run()]
    assert names[:2] == ["disk /", "disk /tmp"]
    assert names[2:] == ["memory", "temperature", "cpu"]


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
