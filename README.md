# Oreo Cloud

Everything that runs on my Raspberry Pi 5 home server: checks, phone alerts, the network watchdog, the nightly backup, container updates, and the photo app with its front door.

Part of the Oreo stack: [oreo-ai](https://github.com/animesh-net/oreo-ai) · [oreo-code](https://github.com/animesh-net/oreo-code) · [oreo-cloud](https://github.com/animesh-net/oreo-cloud) · [oreo-monitor](https://github.com/animesh-net/oreo-monitor)

## Architecture

- `compose.yaml` is the whole Pi: one compose project, `oreo-cloud`, every image pulled from GHCR, nothing built on the Pi.
- The agent runs the scheduled jobs and sends alerts to my phone through ntfy; `oreo-monitor` checks the heartbeat from outside over Tailscale.
- Caddy serves `cloud.`, `pi.` and `ai.animesh.org.in` on the Tailscale address only; `ai.` forwards to oreo-ai, its own compose project in `~/oreo-ai`.
- No secrets live here: they stay in `.env` and `proxy.env` on the Pi.

| Path                           | What it is                               |
| ------------------------------ | ---------------------------------------- |
| `src/oreo/`                    | Agent and heartbeat code (one image)     |
| `compose.yaml`                 | Every container on the Pi                |
| `config/settings.env`          | Drive paths and alert thresholds         |
| `proxy/`, `cache/`             | Caddy and Valkey images                  |
| `pi/migrate.sh`                | One-time move from `~/immich`, `~/caddy` |
| [`mac/`](mac/)                 | Temporary Mac stand-in for the photo app |
| `tests/`, `tests/integration/` | Unit and image tests                     |

## Run it

- Raspberry Pi with Docker, Tailscale and a classic GitHub token with only `read:packages`

1. Log in to the registry once: `echo "<token>" | docker login ghcr.io -u animesh-net --password-stdin`
2. Get the files from the image: `mkdir -p ~/oreo-cloud && cd ~/oreo-cloud && docker run --rm ghcr.io/animesh-net/oreo-cloud show compose > compose.yaml && docker run --rm ghcr.io/animesh-net/oreo-cloud show env-example > .env && chmod 600 .env`
3. Fill in the settings below in `.env` (the photo app's and `proxy.env` are described in it).
4. Start it: `docker compose pull && docker compose up -d`, then check every container is `healthy` with `docker ps`.
5. Run one backup by hand: `docker compose run --rm agent backup`.

| Setting                | Default  | What it is                                                     |
| ---------------------- | -------- | -------------------------------------------------------------- |
| `DOCKER_GID`           |          | Docker socket group: `stat -c %g /var/run/docker.sock`         |
| `OREO_BIND_IP`         |          | The Pi's Tailscale address: `tailscale ip -4`                  |
| `OREO_HEARTBEAT_TOKEN` |          | Heartbeat password shared with `oreo-monitor`                  |
| `OREO_NTFY_URL`        |          | The ntfy topic alerts go to                                    |
| `OREO_IMAGE_TAG`       | `latest` | Image to run; set `sha-<commit>` to roll back                  |

Other commands run as `docker compose run --rm agent <command>` (`system`, `storage`, `drives`, `update status`, ...). Nothing updates by itself, and never run a plain `docker compose pull` after setup: `update immich` dumps the database first. This sets up the `os-update` and `image-update` aliases (oreo-cloud, then oreo-ai):

```bash
sed -i '/^alias os-update=/d;/^alias image-update=/d' ~/.bashrc ~/.bash_aliases 2>/dev/null; printf '%s\n' "alias os-update='docker compose -f ~/oreo-cloud/compose.yaml run --rm agent update os'" "alias image-update='(cd ~/oreo-cloud && docker compose run --rm agent update status && docker compose pull heartbeat agent && docker run --rm ghcr.io/animesh-net/oreo-cloud show compose > compose.yaml && docker compose up -d && docker compose run --rm agent update all) && docker run --rm --pull always ghcr.io/animesh-net/oreo-ai | tar -xf - -C ~/oreo-ai && bash ~/oreo-ai/scripts/oreo-ai.sh on'" >> ~/.bashrc && source ~/.bashrc
```

The nightly backup (23:30) saves the Pi's settings with restic and the Immich and oreo-ai database dumps to `/mnt/osbackup`; restore oreo-ai's with its `scripts/restore-db.sh`.

The storage check (every 30 minutes) names a drive that logs disk errors by tier and label, says when it disconnected and under which name it came back, and flags any filesystem that needs `e2fsck`, both from the kernel log and from the errors ext4 keeps on the drive until it is checked. Errors logged before the drive's last `e2fsck` no longer alert.

Night off: at 00:30 the agent sets the Pi's wake alarm for 06:30, checks it reads back, and powers off; it waits while a backup or OS update runs, stays on if anything fails, and reports the wake-up in the morning. It needs the bootloader set once with `sudo rpi-eeprom-config --edit`: add `POWER_OFF_ON_HALT=1` and `WAKE_ON_GPIO=0`, save, reboot. Then try one wake-up with `docker compose run --rm agent power test` (off for 10 minutes); `power status` shows the setting and the alarm. Immich's own jobs must finish before 00:30: its database backup at 23:00 and its nightly tasks at 22:00 (Administration → Settings).

## Develop

`python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]" && pre-commit install && pytest`; image tests: `docker build -t oreo-cloud:test . && OREO_IMAGE=oreo-cloud:test pytest tests/integration`. Repo rules are in CLAUDE.md.

## License

Copyright © 2026 Animesh Singh. Released under the [MIT License](LICENSE).
