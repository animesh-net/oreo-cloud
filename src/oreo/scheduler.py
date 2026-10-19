"""The agent: runs the recurring jobs inside a container, so nothing needs host cron."""

import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from oreo import alerts, backup, clock, health, net, notify, storage, system

TICK_SECONDS = 15
# A job that needs the clock waits this long before trying again while it is not synchronised.
CLOCK_RETRY_SECONDS = 60
ALIVE_FILE = "agent.alive"


@dataclass
class Job:
    name: str
    every: float  # seconds between runs
    run: Callable[[], object]
    next_due: float = 0.0
    # Jobs that time-stamp or schedule anything wait for a synchronised clock. The network
    # watchdog does not: without network there is no clock sync, so it must run first.
    needs_clock: bool = True
    # Long jobs run in their own thread so the network watchdog never waits behind them; a
    # background job is not started again while its previous run is still going.
    background: bool = False
    thread: threading.Thread | None = None


def _check_job(key: str, label: str, run: Callable[[], list]) -> Callable[[], None]:
    def job() -> None:
        alerts.notify_on_change(key, label, run())

    return job


def default_jobs(start: float = 0.0) -> list[Job]:
    """Network every 2 minutes, clock every minute, container and system checks every 5."""
    return [
        Job("net", 120, net.run_once, start, needs_clock=False),
        Job(
            "clock",
            60,
            _check_job("clock", "Clock", lambda: [clock.check()]),
            start + 5,
            needs_clock=False,
        ),
        Job("health", 300, _check_job("health", "Containers", health.run), start + 10),
        Job("system", 300, _check_job("system", "System checks", system.run), start + 20),
        Job("storage", 1800, _check_job("storage", "Storage", storage.run), start + 30),
        Job("backup", 60, backup.nightly, start + 40, background=True),
        Job(
            "backup-age",
            1800,
            _check_job("backup-age", "Backup", lambda: [backup.freshness_check()]),
            start + 50,
        ),
    ]


def run_due(
    jobs: list[Job],
    now: float,
    log: Callable[[str], None] = print,
    clock_ok: Callable[[], bool] = lambda: True,
) -> None:
    """Run every job that is due. One failing job never stops the others.

    A due job that needs the clock is held back while it is not synchronised, and tried
    again CLOCK_RETRY_SECONDS later; the clock job alerts about it.
    """
    synced = None
    for job in jobs:
        if job.next_due > now:
            continue
        if job.needs_clock:
            if synced is None:
                synced = clock_ok()
            if not synced:
                job.next_due = now + CLOCK_RETRY_SECONDS
                log(f"oreo agent: {job.name} held back, clock not synchronised")
                continue
        job.next_due = now + job.every
        if not job.background:
            _run_job(job, log)
        elif job.thread is None or not job.thread.is_alive():
            job.thread = threading.Thread(target=_run_job, args=(job, log), daemon=True)
            job.thread.start()


def _run_job(job: Job, log: Callable[[str], None]) -> None:
    try:
        job.run()
    except (notify.NotifyError, OSError) as error:
        log(f"oreo agent: {job.name}: {error}")
    except Exception as error:  # a bug in one job must not end the agent
        log(f"oreo agent: {job.name} crashed: {error!r}")


def _touch_alive() -> None:
    path = Path(os.environ.get("OREO_STATE_DIR", alerts.DEFAULT_STATE_DIR)) / ALIVE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError as error:
        print(f"oreo agent: cannot write {path}: {error}", file=sys.stderr, flush=True)


def serve(jobs: list[Job] | None = None) -> None:
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    jobs = jobs or default_jobs(time.monotonic())
    print(
        "oreo agent started: " + ", ".join(f"{j.name} every {j.every:.0f}s" for j in jobs),
        flush=True,
    )
    while not stop.is_set():
        run_due(jobs, time.monotonic(), clock_ok=lambda: clock.check().ok)
        _touch_alive()
        stop.wait(TICK_SECONDS)
