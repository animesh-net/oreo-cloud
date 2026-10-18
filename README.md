# oreo-cloud

Everything that runs on the oreo-cloud Raspberry Pi 5 home server, kept under version control.

- Public repository.
- All logic ships as a single Docker image, built by GitHub Actions and pulled on the Pi. Nothing of ours runs outside Docker.
- One `oreo` command with a subcommand for every operation: backup, health checks, notifications, system checks and heartbeat.
- No secrets in this repository. Credentials stay on the Pi.

The external 5-minute monitor lives in the public repository `oreo-monitor`.

## Commands

| Command | What it does |
| --- | --- |
| `oreo notify MESSAGE [--title] [--priority] [--tags]` | Sends a push notification through ntfy. |
| `oreo health [--json] [--notify]` | Fails if any Docker container is not running or reports `unhealthy`. Containers labelled `oreo.ignore=true` are skipped. |
| `oreo system [--json] [--notify]` | Fails if disk, memory, CPU temperature or load is over its threshold. |
| `oreo heartbeat [--host] [--port]` | Serves `GET /heartbeat` for `oreo-monitor`: runs `health` and `system` on each request and returns their results as JSON, with HTTP `200` when everything passes and `503` when anything fails. |

Checks exit `0` when everything passes and `1` when anything fails. `--notify` sends one ntfy alert listing the failures.

Configuration comes from environment variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `OREO_NTFY_URL` | none, required for alerts | ntfy topic URL, e.g. `https://ntfy.sh/<topic>` |
| `OREO_NTFY_TOKEN` | none | ntfy access token, if the topic is protected |
| `OREO_DOCKER_SOCKET` | `/var/run/docker.sock` | Docker socket for `health` |
| `OREO_DISK_PATHS` | `/` | `system`, comma-separated mount points |
| `OREO_DISK_MAX_PERCENT` | `90` | `system` |
| `OREO_MEMORY_MAX_PERCENT` | `90` | `system` |
| `OREO_TEMPERATURE_MAX_C` | `75` | `system` |
| `OREO_LOAD_MAX_PER_CPU` | `2` | `system`, 1-minute load average per CPU |
| `OREO_HEARTBEAT_HOST` | `0.0.0.0` | `heartbeat` bind address; `--host` overrides it |
| `OREO_HEARTBEAT_PORT` | `8787` | `heartbeat` port; `--port` overrides it |
| `OREO_HEARTBEAT_TOKEN` | none | If set, `/heartbeat` requires `Authorization: Bearer <token>` and returns `401` otherwise |

The heartbeat is meant to be reached only over Tailscale: don't forward its port on the router, and allow only `oreo-monitor`'s tag to reach it in the tailnet's access rules.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

`pre-commit install` sets up three git hooks from `.pre-commit-config.yaml`:

| Hook | Runs |
| --- | --- |
| pre-commit | ruff lint (with fixes) and format, whitespace and end-of-file fixes, YAML/TOML syntax, merge-conflict markers, large files, private keys, and a block on committing to `main` |
| commit-msg | `scripts/check_commit_msg.py`: one line, capitalised, at most 72 characters, no WIP, no AI attribution |
| pre-push | `pytest` |

Run every check by hand with `pre-commit run --all-files`. CI runs the same hooks plus the commit-message check on every commit and the title of each pull request.

## Changes

`main` is protected. Every change goes through a pull request and is squash-merged by the owner.

## License

Copyright © 2026 Animesh Singh. Released under the [MIT License](LICENSE).
