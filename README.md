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

## Deployment

Every push to `main` builds a `linux/arm64` image on GitHub's native ARM runners and publishes it as `ghcr.io/animesh-org/oreo-cloud:latest` (plus a `sha-<commit>` tag). Pull requests build the image without publishing it.

On the Pi:

```bash
git clone https://github.com/animesh-org/oreo-cloud && cd oreo-cloud
cp .env.example .env    # fill in DOCKER_GID, OREO_HEARTBEAT_TOKEN, OREO_NTFY_URL, OREO_BIND_IP
docker compose up -d
```

`compose.yaml` runs `oreo heartbeat` as the non-root `oreo` user, with:

- the Docker socket mounted read-only and the socket's group added (`DOCKER_GID`), so `health` can list containers;
- the host's root filesystem mounted read-only at `/host`, so `system` checks the Pi's disk rather than the container's;
- hostname `oreo-cloud`, so heartbeat reports and alerts name the Pi;
- port `8787` published on `OREO_BIND_IP` (set it to `tailscale ip -4` to keep the heartbeat off the LAN);
- `OREO_HEARTBEAT_TOKEN` required, so it refuses to start without one.

Read-only mounting stops writes to the socket file, not Docker API calls: anything that can reach the socket can control Docker. Only `oreo` gets it.

Run any other command in a one-off container, for example from cron:

```bash
docker compose run --rm oreo system --notify
docker compose run --rm oreo health --notify
```

Update to the latest image with `docker compose pull && docker compose up -d`.

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
