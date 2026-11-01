#!/bin/bash
# Safe on/off for the Mac stand-in.
#   ./oreo-cloud.sh off     stop everything, quit Docker Desktop, eject the Passport
#   ./oreo-cloud.sh on      wait for the Passport, start Docker Desktop, start everything, check the site
#   ./oreo-cloud.sh status  containers, Passport and site at a glance
set -uo pipefail
cd "$(dirname "$0")"
VOL=/Volumes/ImmichTemp
SITE=https://cloud.animesh.org.in

docker_up() { docker info >/dev/null 2>&1; }
passport_ok() { [ -d "$VOL/library/upload" ]; }

quit_docker() {
  osascript -e 'quit app "Docker Desktop"' >/dev/null 2>&1 || osascript -e 'quit app "Docker"' >/dev/null 2>&1
  for _ in $(seq 1 60); do docker_up || return 0; sleep 2; done
  echo "Docker Desktop did not quit within 2 minutes. Quit it from the whale menu, then run this again."; return 1
}

start_docker() {
  docker_up && return 0
  open -a "Docker Desktop" 2>/dev/null || open -a Docker
  echo "Starting Docker Desktop..."
  for _ in $(seq 1 90); do docker_up && return 0; sleep 2; done
  echo "Docker Desktop did not start within 3 minutes."; return 1
}

site_check() {
  code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$SITE/api/server/ping")
  echo "Site $SITE: HTTP $code (200 means working)"
}

case "${1:-}" in
  off)
    if docker_up; then
      echo "Stopping oreo-cloud..."
      docker compose --profile proxy stop || exit 1
      echo "Quitting Docker Desktop, so it lets go of the Passport..."
      quit_docker || exit 1
    fi
    if [ -d "$VOL" ]; then
      for _ in 1 2 3; do diskutil eject "$VOL" && break; sleep 5; done
      [ -d "$VOL" ] && { echo "The Passport is still in use and was not ejected. Do not unplug it yet."; exit 1; }
    fi
    echo "Done. Safe to unplug the Passport, SSD and flash drive."
    ;;
  on)
    echo "Waiting for the Passport to appear as $VOL..."
    for _ in $(seq 1 30); do passport_ok && break; sleep 2; done
    passport_ok || { echo "The Passport is not mounted at $VOL (check: ls /Volumes). Nothing was started."; exit 1; }
    start_docker || exit 1
    echo "Starting oreo-cloud..."
    docker compose --profile proxy up -d || exit 1
    echo "Waiting for photos to be healthy..."
    for _ in $(seq 1 60); do
      [ "$(docker inspect -f '{{.State.Health.Status}}' oreo-cloud-photos 2>/dev/null)" = healthy ] && break; sleep 3
    done
    docker ps --format '  {{.Names}}  {{.Status}}'
    site_check
    ;;
  status)
    if passport_ok; then echo "Passport: connected at $VOL"; else echo "Passport: NOT connected"; fi
    if docker_up; then docker ps --format '  {{.Names}}  {{.Status}}'; else echo "Docker Desktop: not running"; fi
    site_check
    ;;
  *) echo "Usage: ./oreo-cloud.sh on | off | status"; exit 1 ;;
esac
