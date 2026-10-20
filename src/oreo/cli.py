"""Entry point for the `oreo` command."""

import argparse
import json
import subprocess
import sys
from collections.abc import Callable

from oreo import (
    __version__,
    alerts,
    backup,
    drives,
    health,
    heartbeat,
    net,
    notify,
    scheduler,
    settings,
    sites,
    storage,
    system,
    update,
)
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


def _cmd_storage(args: argparse.Namespace) -> int:
    return _run_checks(storage.run, "storage", "Storage", args)


def _cmd_sites(args: argparse.Namespace) -> int:
    return _run_checks(sites.run, "sites", "Websites", args)


def _cmd_drives(args: argparse.Namespace) -> int:
    checks = drives.run()
    if args.json:
        _report(checks, True)
    else:
        print("\n".join(drives.line(check) for check in checks))
    if args.notify:
        try:
            alerts.notify_on_change("drives", "Drives", checks)
        except (notify.NotifyError, OSError) as error:
            print(f"oreo: {error}", file=sys.stderr)
    return 0 if all_ok(checks) else 1


def _cmd_backup(args: argparse.Namespace) -> int:
    try:
        return 0 if backup.run() else 1
    except notify.NotifyError as error:
        print(f"oreo: {error}", file=sys.stderr)
        return 2


def _cmd_update(args: argparse.Namespace) -> int:
    return 0 if update.run(args.action) else 1


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
        ("system", _cmd_system, "check disk, memory, CPU temperature and CPU usage"),
        (
            "storage",
            _cmd_storage,
            "check every drive is mounted, writable, not nearly full and logs no disk errors "
            "(needs the privileged agent service)",
        ),
        (
            "sites",
            _cmd_sites,
            "check each website answers through Caddy and its certificate is not expiring",
        ),
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

    drives_cmd = commands.add_parser(
        "drives",
        help="SMART health, life left and free space of each drive "
        "(needs the privileged agent service)",
    )
    drives_cmd.add_argument("--json", action="store_true", help="print results as JSON")
    drives_cmd.add_argument(
        "--notify", action="store_true", help="alert via ntfy when the result changes"
    )
    drives_cmd.set_defaults(handler=_cmd_drives)

    backup_cmd = commands.add_parser(
        "backup",
        help="run the nightly backup now and send its report (needs the privileged agent service)",
    )
    backup_cmd.set_defaults(handler=_cmd_backup)

    update_cmd = commands.add_parser(
        "update",
        help="update Immich (database dump first), Caddy or the OS, remove unused images, or "
        "show what can be updated; nothing updates by itself (needs the privileged agent service)",
    )
    update_cmd.add_argument("action", choices=sorted(update.ACTIONS))
    update_cmd.set_defaults(handler=_cmd_update)

    net_cmd = commands.add_parser(
        "net",
        help="check the gateway once and repair the connection if it is down "
        "(needs the privileged agent service)",
    )
    net_cmd.set_defaults(handler=_cmd_net)

    show_cmd = commands.add_parser(
        "show",
        help="print the compose.yaml or .env.example this image was built with, to set up "
        "or refresh the Pi without git",
    )
    show_cmd.add_argument("file", choices=sorted(settings.FILES))
    show_cmd.set_defaults(handler=_cmd_show)

    heartbeat_cmd = commands.add_parser(
        "heartbeat", help="serve health and system results at /heartbeat for oreo-monitor"
    )
    heartbeat_cmd.add_argument("--host", help=f"default {heartbeat.DEFAULT_HOST}")
    heartbeat_cmd.add_argument("--port", type=int, help=f"default {heartbeat.DEFAULT_PORT}")
    heartbeat_cmd.set_defaults(handler=_cmd_heartbeat)

    return parser


def _cmd_show(args: argparse.Namespace) -> int:
    print(settings.shared(settings.FILES[args.file]).read_text(), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings.load()
    args = build_parser().parse_args(argv)
    return args.handler(args)
