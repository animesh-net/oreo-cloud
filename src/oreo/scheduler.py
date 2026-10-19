"""The agent: runs the recurring jobs inside a container, so nothing needs host cron."""

import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from oreo import alerts, health, net, notify, system

TICK_SECONDS = 15
ALIVE_FILE = "agent.alive"


@dataclass
class Job:
    name: str
    every: float  # seconds between runs
    run: Callable[[], object]
    next_due: float = 0.0


def _check_job(key: str, label: str, run: Callable[[], list]) -> Callable[[], None]:
    def job() -> None:
        alerts.notify_on_change(key, label, run())

    return job


def default_jobs(start: float = 0.0) -> list[Job]:
    """Network every 2 minutes, container and system checks every 5, staggered at start."""
    return [
        Job("net", 120, net.run_once, start),
        Job("health", 300, _check_job("health", "Containers", health.run), start + 10),
        Job("system", 300, _check_job("system", "System checks", system.run), start + 20),
    ]


def run_due(jobs: list[Job], now: float, log: Callable[[str], None] = print) -> None:
    """Run every job that is due. One failing job never stops the others."""
    for job in jobs:
        if job.next_due > now:
            continue
        job.next_due = now + job.every
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
        run_due(jobs, time.monotonic())
        _touch_alive()
        stop.wait(TICK_SECONDS)
