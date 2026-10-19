"""Entry point for the `oreo` command."""

import argparse
import json
import subprocess
import sys
from collections.abc import Callable

from oreo import __version__, alerts, health, heartbeat, net, notify, scheduler, system
from oreo.checks import Check, all_ok


def _report(checks: list[Check], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": all_ok(checks), "checks": [c.to_dict() for c in checks]}))
        return
    for check in checks:
        print(f"{'OK  ' if check.ok else 'FAIL'}  {check.name}: {check.detail}")


def _run_checks(
    run: Callable[[], list[Check]], key: str, label: str, args: argparse.Namespace
) -> int:
    checks = run()
    _report(checks, args.json)
    if args.notify:
        try:
            alerts.notify_on_change(key, label, checks)
        except (notify.NotifyError, OSError) as error:
            print(f"oreo: {error}", file=sys.stderr)
    return 0 if all_ok(checks) else 1


def _cmd_notify(args: argparse.Namespace) -> int:
    try:
        notify.send(args.message, title=args.title, priority=args.priority, tags=args.tags)
    except notify.NotifyError as error:
        print(f"oreo: {error}", file=sys.stderr)
        return 1
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    return _run_checks(health.run, "health", "Containers", args)


def _cmd_system(args: argparse.Namespace) -> int:
    return _run_checks(system.run, "system", "System checks", args)


def _cmd_net(args: argparse.Namespace) -> int:
    try:
        reachable = net.run_once()
    except (notify.NotifyError, OSError, subprocess.SubprocessError) as error:
        print(f"oreo: {error}", file=sys.stderr)
        return 2
    print("OK    gateway answers" if reachable else "FAIL  gateway does not answer")
    return 0 if reachable else 1


def _cmd_agent(args: argparse.Namespace) -> int:
    scheduler.serve()
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    heartbeat.serve(args.host, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oreo", description="Operations CLI for the oreo-cloud home server."
    )
    parser.add_argument("--version", action="version", version=f"oreo {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    notify_cmd = commands.add_parser("notify", help="send a push notification through ntfy")
    notify_cmd.add_argument("message")
    notify_cmd.add_argument("--title")
    notify_cmd.add_argument("--priority", help="min, low, default, high or urgent")
    notify_cmd.add_argument("--tags", help="comma-separated ntfy tags or emoji names")
    notify_cmd.set_defaults(handler=_cmd_notify)

    for name, handler, help_text in (
        ("health", _cmd_health, "check that every Docker container is running and healthy"),
        ("system", _cmd_system, "check disk, memory, CPU temperature and load"),
    ):
        cmd = commands.add_parser(name, help=help_text)
        cmd.add_argument("--json", action="store_true", help="print results as JSON")
        cmd.add_argument(
            "--notify", action="store_true", help="alert via ntfy when the result changes"
        )
        cmd.set_defaults(handler=handler)

    agent_cmd = commands.add_parser(
        "agent",
        help="run the recurring jobs (net, health, system) in the foreground; "
        "needs the privileged agent service",
    )
    agent_cmd.set_defaults(handler=_cmd_agent)

    net_cmd = commands.add_parser(
        "net",
        help="check the gateway once and repair the connection if it is down "
        "(needs the privileged agent service)",
    )
    net_cmd.set_defaults(handler=_cmd_net)

    heartbeat_cmd = commands.add_parser(
        "heartbeat", help="serve health and system results at /heartbeat for oreo-monitor"
    )
    heartbeat_cmd.add_argument("--host", help=f"default {heartbeat.DEFAULT_HOST}")
    heartbeat_cmd.add_argument("--port", type=int, help=f"default {heartbeat.DEFAULT_PORT}")
    heartbeat_cmd.set_defaults(handler=_cmd_heartbeat)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)
