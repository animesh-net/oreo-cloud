# oreo-cloud

Everything that runs on the oreo-cloud Raspberry Pi 5 home server, kept under version control.

- Private repository.
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
| `OREO_DISK_PATHS` | `/` | `system`, comma-separated paths to check |
| `OREO_MOUNT_PATHS` | none | `system`, paths from `OREO_DISK_PATHS` that must be mount points; a path that is not mounted fails instead of reporting its parent's usage |
| `OREO_DISK_MAX_PERCENT` | `90` | `system` |
| `OREO_MEMORY_MAX_PERCENT` | `90` | `system` |
| `OREO_TEMPERATURE_MAX_C` | `75` | `system` |
| `OREO_LOAD_MAX_PER_CPU` | `2` | `system`, 1-minute load average per CPU |
| `OREO_HEARTBEAT_HOST` | `0.0.0.0` | `heartbeat` bind address; `--host` overrides it |
| `OREO_HEARTBEAT_PORT` | `8787` | `heartbeat` port; `--port` overrides it |
| `OREO_HEARTBEAT_TOKEN` | none | If set, `/heartbeat` requires `Authorization: Bearer <token>` and returns `401` otherwise |

The heartbeat is meant to be reached only over Tailscale: don't forward its port on the router, and allow only `oreo-monitor`'s tag to reach it in the tailnet's access rules.

## Deployment

Every push to `main` builds a `linux/arm64` image on GitHub's native ARM runners and publishes it as `ghcr.io/animesh-net/oreo-cloud:latest` (plus a `sha-<commit>` tag). Before anything is published, the workflow runs the integration tests in `tests/integration/` against the freshly built image; pull requests build and test the image without publishing it.

The repository and the image are private, so the Pi authenticates for both. Use a classic personal access token with only the `read:packages` scope for the registry, and `gh auth login` (or a read-only deploy key) for the clone.

On the Pi:

```bash
gh repo clone animesh-net/oreo-cloud && cd oreo-cloud
echo "$GHCR_TOKEN" | docker login ghcr.io -u animesh-net --password-stdin
cp .env.example .env    # fill in DOCKER_GID, OREO_HEARTBEAT_TOKEN, OREO_NTFY_URL, OREO_BIND_IP
docker compose up -d
```

`docker login` stores the token in `~/.docker/config.json` on the Pi; it is never needed in this repository.

Private packages share the account's 500 MB of free storage, so after publishing, the workflow deletes all but the 10 most recent image versions. Images are pushed without build attestations: with them, each push creates several linked versions, and pruning by count could delete part of a newer image.

`compose.yaml` runs `oreo heartbeat` as the non-root `oreo` user, with:

- the Docker socket mounted read-only and the socket's group added (`DOCKER_GID`), so `health` can list containers;
- the host's `/mnt` mounted read-only at `/host/mnt` with `rslave` propagation, so a drive that mounts after the container starts still shows up. `system` checks the container's `/`, which is the NVMe, and the data SSD at `/host/mnt/data-hot-ssd1`, which must be a mount point; add the HDDs to both variables in `compose.yaml` once they are mounted. The rest of the host's filesystem is not shared;
- a read-only root filesystem, all Linux capabilities dropped, `no-new-privileges`, a 256 MB memory limit, a 100-process limit and 3 x 10 MB of logs;
- `init: true`, so `docker stop` and updates end the container at once instead of waiting 10 seconds for a hard kill;
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

### Integration tests

`tests/integration/test_image.py` runs the built image the way `compose.yaml` does on the Pi and checks that it runs as the non-root `oreo` user with access to Docker, that `/heartbeat` rejects missing or wrong tokens, reports a stopped container with `503` and recovers to `200`, that the container's own health check stays healthy while checks fail, that one-off `health` and `system` runs work, and that `compose.yaml` refuses to start without its required `.env` values. They need Docker and are skipped unless `OREO_IMAGE` is set:

```bash
docker build -t oreo-cloud:test .
OREO_IMAGE=oreo-cloud:test pytest tests/integration -v
```

Run every check by hand with `pre-commit run --all-files`. CI runs the same hooks plus the commit-message check on every commit and the title of each pull request.

## Changes

Every change goes through a pull request and is squash-merged by the owner. GitHub only enforces the `main` ruleset on public repositories or paid plans, so for this private repository the guard is the `no-commit-to-branch` pre-commit hook plus the conventions in `CLAUDE.md`.

## License

Copyright © 2026 Animesh Singh. Released under the [MIT License](LICENSE).
