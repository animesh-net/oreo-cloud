from oreo import notify, scheduler
from oreo.scheduler import Job


def make(log):
    def job(name, every, fail=None, needs_clock=True):
        def run():
            log.append(name)
            if fail:
                raise fail

        return Job(name, every, run, needs_clock=needs_clock)

    return job


def test_due_jobs_run_and_are_rescheduled():
    ran = []
    job = make(ran)("net", 120)
    scheduler.run_due([job], 0)
    scheduler.run_due([job], 60)
    scheduler.run_due([job], 120)
    assert ran == ["net", "net"]
    assert job.next_due == 240


def test_each_job_keeps_its_own_interval():
    ran = []
    new = make(ran)
    jobs = [new("net", 120), new("health", 300)]
    for now in (0, 120, 240, 300, 360):
        scheduler.run_due(jobs, now)
    assert ran.count("net") == 4
    assert ran.count("health") == 2


def test_failing_job_does_not_stop_the_others_or_itself():
    ran, logged = [], []
    new = make(ran)
    jobs = [new("net", 120, notify.NotifyError("ntfy down")), new("health", 120)]
    scheduler.run_due(jobs, 0, logged.append)
    assert ran == ["net", "health"]
    assert logged == ["oreo agent: net: ntfy down"]
    scheduler.run_due(jobs, 120, logged.append)
    assert ran == ["net", "health", "net", "health"]


def test_unexpected_error_is_logged_not_raised():
    ran, logged = [], []
    job = make(ran)("system", 300, ValueError("bad"))
    scheduler.run_due([job], 0, logged.append)
    assert "system crashed" in logged[0]


def test_default_jobs_cover_network_health_and_system():
    jobs = {job.name: job for job in scheduler.default_jobs(100)}
    assert {name: job.every for name, job in jobs.items()} == {
        "net": 120, "clock": 60, "health": 300, "system": 60, "storage": 1800, "sites": 1800,
        "backup": 60, "compose": 1800, "backup-age": 1800, "power": 60,
    }  # fmt: skip
    assert not jobs["net"].needs_clock
    assert not jobs["clock"].needs_clock
    assert jobs["health"].needs_clock and jobs["system"].needs_clock and jobs["storage"].needs_clock
    assert jobs["net"].next_due == 100
    assert jobs["health"].next_due > 100


def test_unsynchronised_clock_holds_back_jobs_that_need_it():
    ran, logged = [], []
    new = make(ran)
    jobs = [new("net", 120, needs_clock=False), new("health", 300)]
    scheduler.run_due(jobs, 0, logged.append, clock_ok=lambda: False)
    assert ran == ["net"]
    assert logged == ["oreo agent: health held back, clock not synchronised"]
    assert jobs[1].next_due == scheduler.CLOCK_RETRY_SECONDS
    scheduler.run_due(jobs, 60, logged.append, clock_ok=lambda: True)
    assert ran == ["net", "health"]


def test_clock_is_asked_once_per_round_and_only_when_needed():
    asked = []

    def clock_ok():
        asked.append(1)
        return True

    new = make([])
    scheduler.run_due([new("net", 120, needs_clock=False)], 0, clock_ok=clock_ok)
    assert asked == []
    scheduler.run_due([new("health", 300), new("system", 300)], 0, clock_ok=clock_ok)
    assert asked == [1]


def test_background_job_runs_in_a_thread_and_does_not_overlap():
    import threading

    release = threading.Event()
    started = []

    def slow():
        started.append(1)
        release.wait(5)

    job = Job("backup", 60, slow, background=True)
    scheduler.run_due([job], 0)
    scheduler.run_due([job], 60)  # still running: not started again
    assert job.thread.is_alive()
    release.set()
    job.thread.join(5)
    assert started == [1]
    scheduler.run_due([job], 120)
    job.thread.join(5)
    assert started == [1, 1]
    assert {j.name: j for j in scheduler.default_jobs()}["backup"].background
