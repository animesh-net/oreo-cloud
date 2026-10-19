from oreo import notify, scheduler
from oreo.scheduler import Job


def make(log):
    def job(name, every, fail=None):
        def run():
            log.append(name)
            if fail:
                raise fail

        return Job(name, every, run)

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
        "net": 120, "health": 300, "system": 300,
    }  # fmt: skip
    assert jobs["net"].next_due == 100
    assert jobs["health"].next_due > 100
