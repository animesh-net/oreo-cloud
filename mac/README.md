# oreo-cloud on the MacBook (temporary, while the Pi is replaced)

Everything for the Mac stand-in lives in this folder, except the photos, which are on the
WD Passport (ImmichTemp) at `/Volumes/ImmichTemp/library`.

Containers, volumes and the compose project are named `oreo-cloud-*` rather than after Immich
or Caddy, so this folder and the eventual Pi fold-in of these same services share one naming
scheme — see the project's "vendor-agnostic container rename" plan for the full map.

| Path | What it is |
| --- | --- |
| `docker-compose.yml` | The photo app (same images as the Pi) and Caddy (profile `proxy`), under renamed service and container names |
| `.env` | Photo and database locations, version, time zone, plus `DB_HOSTNAME`, `REDIS_HOSTNAME` and `IMMICH_MACHINE_LEARNING_URL` so Immich finds the renamed services. `DB_PASSWORD` is set by `restore.sh` |
| `migrate.sh` | One-time cutover from the old `immich` project to this one, with an automatic rollback if the counts or the site don't check out |
| `restore.sh` | One-time restore of the Pi's newest Immich database dump, then starts oreo-cloud |
| `caddy/` | The Pi's Caddy, adapted: serves `https://cloud.animesh.org.in` on `100.70.213.121` |
| `caddy/caddy.env` | Cloudflare API token (secret, from the Pi's backup) |
| `postgres/` | The database on the Mac (created by `restore.sh`) |
| `db-dumps/` | Second copy of the nightly database dumps (the `db-dump-copy` container, hourly, 14 days) |
| `sync-to-pi.sh` | At swap-back: sends what changed on the Mac (new files, newest dump) to the Pi |
| `pi-backup/` | Copy of the Pi's restic backup and the restored Caddy/Immich settings (contain secrets) |

`.env`, `caddy/caddy.env`, `postgres/`, `db-dumps/` and `pi-backup/` are gitignored — everything
else here is tracked, so a deploy on the Mac is `git pull`, not a hand-copied file.

## Run it

Docker Desktop must be running and the Passport connected. All commands from this folder:
`cd ~/Projects/GitHub/oreo-cloud/mac` (or wherever this repo is cloned on the Mac).

- Safe stop before unplugging the drives: `./oreo-cloud.sh off` (stops everything, quits Docker Desktop, ejects the Passport)
- Start again after plugging the Passport in: `./oreo-cloud.sh on`
- Quick check: `./oreo-cloud.sh status`
- First time only: `./restore.sh`
- One-time cutover from an older, unrenamed stand-in: `./migrate.sh` (or `./migrate.sh --dry-run` first)
- Start everything: `docker compose --profile proxy up -d`
- Stop everything: `docker compose --profile proxy down`
- Status: `docker ps`
- Photo app logs: `docker logs -f --tail 50 oreo-cloud-photos`

Phones: keep Backup off in the Immich app while the Mac stands in, so new photos wait for the Pi.

Keep the Mac on the charger with the lid open. It is set never to sleep on the charger:
`sudo pmset -c sleep 0 displaysleep 10` (original charger values: sleep 1, displaysleep 10).

## Before the new Pi boots

1. `docker compose --profile proxy down`
2. Tailscale admin console: MacBook back to its own address, Pi (oreo-cloud) back to 100.70.213.121.
3. Start the Pi. If anything changed on the Mac (albums, edits, uploads), run
   `./sync-to-pi.sh` from this folder and follow the steps it prints, so the Pi gets the Mac's
   latest database and files. Then turn Backup back on in the Immich app and re-enable the
   Monitor workflow in oreo-monitor.
4. Let the Mac sleep again on the charger: `sudo pmset -c sleep 1`
5. Later: delete `pi-backup/` (secrets) and wipe the Passport, or keep it as an extra copy.
