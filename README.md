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
| `oreo drives [--json] [--notify]` | One line per drive in `OREO_DRIVES`, found by filesystem label: SMART status, life left, data written, days powered on (left out for the NVMe, whose counter is unreliable) and free space, worded as in the nightly report. Fails on a failed SMART status, reallocated or uncorrectable sectors, cable (CRC) errors, NVMe media errors or critical warnings. Runs `smartctl` against the host's devices, so it needs host access like `net`. |
| `oreo health [--json] [--notify]` | Fails if any Docker container is not running or reports `unhealthy`. Containers labelled `oreo.ignore=true` are skipped. |
| `oreo system [--json] [--notify]` | Fails if disk, memory, CPU temperature or load is over its threshold. |
| `oreo agent` | Runs the recurring jobs in the foreground: `net` every 2 minutes, a clock check every minute, `health` and `system` every 5 and `storage` every 30 (with alerts on change). While the host's clock is not synchronised with NTP (asked through the host's `timedatectl`), `health` and `system` are held back and retried a minute later, and the clock check alerts "Clock: not synchronised with NTP"; `net` is never held back, because without network the clock cannot sync. One failing job never stops the others, a signal stops it at once, and it touches `agent.alive` in the state directory every 15 seconds for its health check. Needs host access like `net`. |
| `oreo net` | One network check: pings the default gateway on the host. After 2 failed checks in a row it renews the Wi-Fi address (`networkctl reconfigure`); after 3 it restarts Wi-Fi (`netplan apply`), then again every 5th failed check. While the network is down no alert can be sent, so the only message is "Oreo Cloud OK" once it is back, naming the fix and the Wi-Fi address. While the host's old `oreo-netwatch.timer` is still active it repairs nothing and sends one alert with the command to disable the timer, so the two never act at once. Outages during `OREO_NET_QUIET_WINDOW`, the router's daily restart, are left alone: no repair is attempted inside the window, nothing is reported if the network is back before the window closes, and counting starts from zero when it closes. Exits `0` when the gateway answers, `1` when it does not, `2` when the host cannot be reached. Needs host access, see below. |
| `oreo storage [--json] [--notify]` | Fails if a drive in `OREO_STORAGE_MOUNTS` is not mounted, is read-only (including a filesystem that remounted itself read-only after errors) or is 90% full or more, or if the host's kernel log has disk errors from the last 26 hours. Reads the host's mounts through `/proc/1`, so it needs host access like `net`; the agent runs it every 30 minutes. |
| `oreo heartbeat [--host] [--port]` | Serves `GET /heartbeat` for `oreo-monitor`: runs `health` and `system` on each request and returns their results as JSON, with HTTP `200` when everything passes and `503` when anything fails. |

Checks exit `0` when everything passes and `1` when anything fails. `--notify` sends an ntfy alert only when the result changes: "Oreo Cloud NEEDS ATTENTION" listing the failures when something starts failing (and again when the list of failures changes, or as a reminder every `OREO_ALERT_REMINDER_HOURS` while it keeps failing), and "Oreo Cloud OK" with how long it lasted when everything is back to normal. What is already failing is remembered in `OREO_STATE_DIR`, a named volume in `compose.yaml`; if sending fails, nothing is remembered, so the next run tries again.

Configuration comes from environment variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `OREO_NTFY_URL` | none, required for alerts | ntfy topic URL, e.g. `https://ntfy.sh/<topic>`; `compose.yaml` will not start the agent without it |
| `OREO_NTFY_TOKEN` | none | ntfy access token, if the topic is protected |
| `OREO_STATE_DIR` | `/var/lib/oreo` | where `--notify` remembers what is already failing |
| `OREO_ALERT_REMINDER_HOURS` | `6` | repeat the alert this often while a check keeps failing |
| `OREO_TIMEZONE` | `Asia/Kolkata` | time zone for the times in alert messages |
| `OREO_STORAGE_MOUNTS` | `/,/boot/firmware,/mnt/data-hot-ssd1,/mnt/osbackup` | `storage`, host mount points that must be present |
| `OREO_DRIVES` | `nvme-root=System (NVMe \| nvme-root);data-hot-ssd1=Data (Hot Tier \| data-hot-ssd1)` | `drives`, `label=Name` pairs separated by `;` |
| `OREO_NET_INTERFACE` | `wlan0` | `net`, the Wi-Fi interface to renew |
| `OREO_NET_QUIET_WINDOW` | `05:55-06:15` | `net`, local time when the router restarts itself; empty turns it off |
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

`compose.yaml` runs two services from the same image:

- `agent` runs `oreo agent` as root, privileged and in the host's process namespace, so the recurring jobs (network watchdog, container and system checks) need no cron or systemd timer on the Pi. It publishes no port and runs nothing but this image. Its health check fails if the loop stops, so `oreo health` reports it.
- `oreo` runs `oreo heartbeat`, the only service with a listening port, as the non-root `oreo` user, with:

- the Docker socket mounted read-only and the socket's group added (`DOCKER_GID`), so `health` can list containers;
- the host's `/mnt` mounted read-only at `/host/mnt` with `rslave` propagation, so a drive that mounts after the container starts still shows up. `system` checks the container's `/`, which is the NVMe, and the data SSD at `/host/mnt/data-hot-ssd1`, which must be a mount point; add the HDDs to both variables in `compose.yaml` once they are mounted. The rest of the host's filesystem is not shared;
- a read-only root filesystem, all Linux capabilities dropped, `no-new-privileges`, a 256 MB memory limit, a 100-process limit and 3 x 10 MB of logs;
- `init: true`, so `docker stop` and updates end the container at once instead of waiting 10 seconds for a hard kill;
- the host's network, binding port `8787` on `OREO_BIND_IP` only (required: set it to `tailscale ip -4` to keep the heartbeat off the LAN). If that address does not exist yet, as at boot before Tailscale is up, it waits and retries every 5 seconds instead of exiting; Docker does not restart a container whose port binding failed, so publishing the port through `ports:` would leave it down after a reboot. Reports name the Pi by its own hostname;
- `OREO_HEARTBEAT_TOKEN` required, so it refuses to start without one.

Read-only mounting stops writes to the socket file, not Docker API calls: anything that can reach the socket can control Docker. Only the two services above get it, and the agent has root on the host anyway.

`oreo net` runs the host's own `ip`, `ping`, `networkctl` and `netplan` by entering the host's namespaces with `nsenter`, so it needs a privileged container that shares the host's process namespace and runs as root. That is root on the Pi: give it only to a container that has no listening port, never to the heartbeat service. For example:

```bash
docker run --rm --privileged --pid=host --user root -v oreo-cloud_oreo-state:/var/lib/oreo \
  --env-file .env ghcr.io/animesh-net/oreo-cloud:latest net
```

Run any other command in a one-off container:

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
| commit-msg | `scripts/check_commit_msg.py`: one line, capitalised, at most 72 characters, no WIP, no co-author or other disallowed trailer |
| pre-push | `pytest` |

### Integration tests

`tests/integration/test_image.py` runs the built image the way `compose.yaml` does on the Pi and checks that it runs as the non-root `oreo` user with access to Docker, that `/heartbeat` rejects missing or wrong tokens, reports a stopped container with `503` and recovers to `200`, that the container's own health check stays healthy while checks fail, that one-off `health` and `system` runs work, and that `compose.yaml` refuses to start without its required `.env` values. They need Docker and are skipped unless `OREO_IMAGE` is set:

```bash
docker build -t oreo-cloud:test .
OREO_IMAGE=oreo-cloud:test pytest tests/integration -v
```

Run every check by hand with `pre-commit run --all-files`.

The `PR description` workflow tidies every pull request description when it is opened, edited or pushed to: `scripts/clean_pr_body.py` removes co-author and session lines and any "generated by" or "requested by" footer, with the rule above it, and saves the description back. It never fails and never comments. CI runs the same hooks plus the commit-message check on every commit and the title of each pull request.

## Changes

Every change goes through a pull request and is squash-merged by the owner. GitHub only enforces the `main` ruleset on public repositories or paid plans, so for this private repository the guard is the `no-commit-to-branch` pre-commit hook plus the conventions in `CLAUDE.md`.

## License

Copyright © 2026 Animesh Singh. Released under the [MIT License](LICENSE).
