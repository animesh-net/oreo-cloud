# oreo-cloud

Everything that runs on **oreo-cloud**, my Raspberry Pi 5 home server: checks, alerts, the network watchdog, the nightly backup and container updates.

- It all ships as **one Docker image**, built by GitHub Actions and pulled on the Pi. Nothing of ours runs outside Docker.
- Alerts go to my phone through **ntfy**.
- **No secrets live here.** They stay in `.env` on the Pi.
- The outside check that the Pi is alive lives in the separate `oreo-monitor` repository.

## How it works

`compose.yaml` runs two containers from the same image:

| Container | What it does | Access |
| --- | --- | --- |
| `oreo-agent` | Runs every scheduled job (see below) and sends the alerts. | Root on the Pi, so it can repair Wi-Fi, read drive health and back up. No open port. |
| `oreo` | Answers the heartbeat that `oreo-monitor` checks over Tailscale. | Locked down: not root, read-only, listens only on the Tailscale address. |

### What runs when

| Job | When | What happens |
| --- | --- | --- |
| Network watchdog | Every 2 minutes | Pings the router. After 2 misses it renews the Wi-Fi address; after 3 it restarts Wi-Fi, and again every 5th miss. Left alone 05:55 to 06:15, when the router restarts itself. |
| Clock | Every minute | Alerts if the clock is not synchronised. Container and system checks wait until it is. |
| Containers | Every 5 minutes | Every container running and healthy. |
| System | Every 5 minutes | Disk, memory, temperature and load. |
| Storage | Every 30 minutes | All four drives mounted, writable, under 90% full, no disk errors logged. |
| Websites | Every 30 minutes | `cloud.animesh.org.in` and `pi.animesh.org.in` answer, and their certificates have at least 14 days left. |
| Backup age | Every 30 minutes | Alerts if the last good backup is more than 26 hours old. |
| Nightly backup | 03:30 | Saves the Pi's settings with restic and copies Immich's database dump to the flash drive, then sends the nightly report. Runs later if the Pi was off at 03:30. |

### Alerts on my phone

| Title | Meaning |
| --- | --- |
| **Oreo Cloud NEEDS ATTENTION** | Something started failing. Sent once, again if what is failing changes, and as a reminder every 6 hours. |
| **Oreo Cloud OK** | It is fixed, with how long it lasted. Also the title of a clean nightly report. |
| **Oreo Cloud FAILED** | A backup or update step failed; the message says why. |

While the network is down no alert can get out, so a Wi-Fi outage shows up as one "OK" message afterwards, saying what fixed it.

This is how they look, from top to bottom: a nightly report, a container problem and its fix, and a Wi-Fi outage that the watchdog repaired. The messages come from the real code, fed with sample data.

<img src="docs/images/alerts.png" alt="Example alerts on the phone" width="400">

## Set up on the Pi

You need a GitHub token with only the `read:packages` permission, so the Pi can pull the private image.

1. Get the files and log in to the image registry:

   ```bash
   gh repo clone animesh-net/oreo-cloud && cd oreo-cloud
   echo "<token>" | docker login ghcr.io -u animesh-net --password-stdin
   ```

2. Create `.env` and fill in the four required values. The comments in the file say where each one comes from.

   ```bash
   cp .env.example .env && chmod 600 .env && nano .env
   ```

3. Start it:

   ```bash
   docker compose up -d
   ```

4. Check it. Both containers should say `healthy` within a minute, and the two commands should match the last nightly report.

   ```bash
   docker ps
   docker compose run --rm agent storage
   docker compose run --rm agent drives
   ```

5. Run one backup by hand. The first one takes longer.

   ```bash
   docker compose run --rm agent backup
   ```

6. Turn off the old host timers. Until you do, the agent leaves the network repairs and the backup to them and sends one alert reminding you.

   ```bash
   sudo systemctl disable --now oreo-osbackup.timer oreo-netwatch.timer
   ```

## Everyday use

Run any command with `docker compose run --rm agent <command>`:

![Output of the system and health checks](docs/images/checks.png)


| Command | What it does |
| --- | --- |
| `system`, `health`, `storage`, `sites` | Run that check now and print the result. Add `--notify` to send alerts too. |
| `drives` | Drive health: status, life left, data written, free space. |
| `backup` | Run the nightly backup now. |
| `update status` | Containers, free space, and whether the Pi needs a reboot. |
| `update immich` | Update Immich. Takes a database dump first and stops if the dump looks wrong. |
| `update caddy` | Rebuild Caddy on the latest base image. |
| `update prune` | Remove unused Docker images. |
| `update all` | Immich, then Caddy, then prune. |
| `notify "message"` | Send a test notification. |

OS updates are not included, because upgrading the OS can restart Docker mid-way. Run them on the Pi yourself: `sudo apt update && sudo apt upgrade`.

### Update or roll back this image

```bash
docker compose pull && docker compose up -d
```

The last 10 images are kept. To roll back, add `OREO_IMAGE_TAG=sha-<commit>` to `.env` and run `docker compose up -d`. Remove that line to go back to the latest.

## If something goes wrong

| Problem | What to do |
| --- | --- |
| A check alerts | The alert names the failing check. Run that check by hand to see the detail, for example `docker compose run --rm agent storage`. |
| `oreo-agent` is unhealthy or missing | `docker compose logs agent`, then `docker compose up -d`. |
| Docker itself is down | Nothing inside Docker can fix this. Connect over Tailscale with SSH, then run `sudo systemctl restart docker` and `docker compose up -d`. |
| A backup failed | The alert says which step failed. Fix that, then run `docker compose run --rm agent backup`. |
| An old stopped container keeps alerting | Remove it, or give it the label `oreo.ignore=true`. |
| `docker compose up` says `/mnt` "is not a shared or slave mount" | Run `sudo mount --make-rshared /`. Ubuntu with systemd already does this at boot, so it only happens on unusual setups. |

## Settings

Required, in `.env`:

| Setting | What it is |
| --- | --- |
| `DOCKER_GID` | Group of the Docker socket: `stat -c %g /var/run/docker.sock` |
| `OREO_HEARTBEAT_TOKEN` | Password for the heartbeat, shared with `oreo-monitor`: `openssl rand -hex 32` |
| `OREO_NTFY_URL` | The ntfy topic alerts go to |
| `OREO_BIND_IP` | The Pi's Tailscale address: `tailscale ip -4` |

Everything else is optional and listed, with its default, at the bottom of `.env.example`. Uncomment a line to change it. The most useful ones:

| Setting | Default | What it changes |
| --- | --- | --- |
| `OREO_BACKUP_TIME` | `03:30` | Time of the nightly backup |
| `OREO_ALERT_REMINDER_HOURS` | `6` | How often a problem is repeated while it lasts |
| `OREO_NET_QUIET_WINDOW` | `05:55-06:15` | When the router restarts itself |
| `OREO_SITES` | the two sites | Websites the agent checks |
| `OREO_IMAGE_TAG` | `latest` | Image to run; set a `sha-` tag to roll back |

When the HDDs are mounted, add them to `x-drives` at the top of `compose.yaml` and to `OREO_STORAGE_MOUNTS` and `OREO_DRIVES`.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
pytest
```

- Pre-commit hooks lint and format the code, check each commit message (one line, capitalised, at most 72 characters, no extra trailers), block commits to `main` and run the tests before a push.
- Every push to `main` builds the `linux/arm64` image, runs the integration tests in `tests/integration/` against it, scans it for critical vulnerabilities and publishes it to `ghcr.io/animesh-net/oreo-cloud`. Pull requests build and test it without publishing.
- The integration tests need Docker: `docker build -t oreo-cloud:test . && OREO_IMAGE=oreo-cloud:test pytest tests/integration`.
- Dependabot opens weekly pull requests for the pinned Actions, the base image and Python packages.

## Changes

Every change goes through a pull request against `main` and is squash-merged by me. The rules are in `CLAUDE.md`. GitHub does not enforce branch rules on this private repository, so the pre-commit hooks and CI are the guard.

## License

Copyright © 2026 Animesh Singh. Released under the [MIT License](LICENSE).
