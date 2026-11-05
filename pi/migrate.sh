#!/bin/bash
# One-time cutover on the Pi from the separate "immich" (~/immich) and "caddy" (~/caddy)
# projects to the photo app and proxy inside the oreo-cloud project (~/oreo-cloud), all named
# oreo-cloud-*. Ships in the image:
#   cd ~/oreo-cloud && docker compose pull heartbeat agent
#   docker run --rm ghcr.io/animesh-net/oreo-cloud show migrate > migrate.sh
#   bash migrate.sh --dry-run
# then `bash migrate.sh` for the real run. It writes the image's compose.yaml itself.
#
# Photos and the database are bind mounts, so they stay exactly where they are. The proxy's
# certificates and the ML model cache are copied into the new volumes. The old containers are
# removed with `down` (never their data): a stopped container with restart: always would
# start again after a reboot, and two databases on one data folder corrupt it. A failed check
# rolls back: the new services are removed, the .env lines this script added are taken out
# again, and the old projects are started as before.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

OLD_IMMICH="$HOME/immich"
OLD_CADDY="$HOME/caddy"
SITE=https://cloud.animesh.org.in
OREO_IMAGE=ghcr.io/animesh-net/oreo-cloud:latest
NEW_SERVICES=(photos photos-ml cache db proxy)
BEGIN="# >>> photo app, added by migrate.sh"
END="# <<< photo app"
DRY_RUN=false
[ "${1:-}" = "--dry-run" ] && DRY_RUN=true

say() { printf '\n== %s ==\n' "$1"; }
die() { echo "$1"; exit 1; }
value() { grep -m1 "^$2=" "$1" | cut -d= -f2- | tr -d "\"'"; }
old_immich() { docker compose --project-directory "$OLD_IMMICH" -f "$OLD_IMMICH/docker-compose.yml" "$@"; }
old_caddy() { docker compose --project-directory "$OLD_CADDY" -f "$OLD_CADDY/docker-compose.yml" "$@"; }

count_assets() {
  docker exec "$1" psql -t -A -U "$DB_USERNAME" -d "$DB_DATABASE_NAME" \
    -c 'SELECT count(*) FROM asset;' 2>/dev/null | tr -d '[:space:]'
}

copy_volume() {
  local from="$1" to="oreo-cloud_$2"
  if ! docker volume inspect "$from" >/dev/null 2>&1; then
    echo "  $from not found, $to starts empty."; return 0
  fi
  docker run --rm --user root --entrypoint sh -v "$from:/from:ro" -v "$to:/to" "$OREO_IMAGE" \
    -c 'cp -a /from/. /to/' && echo "  $from -> $to"
}

remove_env_block() { sed -i "/^$BEGIN\$/,/^$END\$/d" .env; }

undo_settings() {
  remove_env_block
  rm -f proxy.env
  mv -f compose.yaml.pre-migrate compose.yaml
}

rollback() {
  echo "Rolling back: removing the new services, starting immich and caddy again..."
  docker compose rm -sf "${NEW_SERVICES[@]}"
  undo_settings
  docker compose up -d --remove-orphans
  old_immich up -d
  old_caddy up -d
  die "Rolled back. Immich and Caddy run as before; nothing was lost."
}

say "0. Checks"
[ -f compose.yaml ] && [ -f .env ] || die "Run this from ~/oreo-cloud, where compose.yaml and .env are."
for f in "$OLD_IMMICH/.env" "$OLD_IMMICH/docker-compose.yml" "$OLD_CADDY/.env" "$OLD_CADDY/Caddyfile"; do
  [ -f "$f" ] || die "$f not found. Nothing changed."
done
grep -q "^$BEGIN\$" .env && die ".env already has the photo app's lines: the cutover ran before. Nothing changed."
[ "$(docker inspect -f '{{.State.Running}}' immich_postgres 2>/dev/null)" = true ] \
  || die "immich_postgres is not running. Nothing changed."
DB_USERNAME=$(value "$OLD_IMMICH/.env" DB_USERNAME)
DB_DATABASE_NAME=$(value "$OLD_IMMICH/.env" DB_DATABASE_NAME)
DB_USERNAME=${DB_USERNAME:-postgres}
DB_DATABASE_NAME=${DB_DATABASE_NAME:-immich}
UPLOAD_LOCATION=$(value "$OLD_IMMICH/.env" UPLOAD_LOCATION)
DB_DATA_LOCATION=$(value "$OLD_IMMICH/.env" DB_DATA_LOCATION)
[ -n "$UPLOAD_LOCATION" ] && [ -n "$DB_DATA_LOCATION" ] && [ -n "$(value "$OLD_IMMICH/.env" DB_PASSWORD)" ] \
  || die "UPLOAD_LOCATION, DB_DATA_LOCATION or DB_PASSWORD missing in $OLD_IMMICH/.env. Nothing changed."
AUTH_HASH=$(awk '/basic_auth/ {getline; print $2; exit}' "$OLD_CADDY/Caddyfile")
[ -n "$AUTH_HASH" ] || die "No basic_auth hash found in $OLD_CADDY/Caddyfile. Nothing changed."
docker run --rm "$OREO_IMAGE" show compose | grep -q 'container_name: oreo-cloud-photos$' \
  || die "The local $OREO_IMAGE has no photo app yet: docker compose pull heartbeat agent first. Nothing changed."
OLD_COUNT=$(count_assets immich_postgres)
[ -n "$OLD_COUNT" ] || die "Could not read the asset count from immich_postgres. Nothing changed."
echo "Photos: $UPLOAD_LOCATION, database: $DB_DATA_LOCATION, $OLD_COUNT assets."

if [ "$DRY_RUN" = true ]; then
  echo "(dry run) would pull the proxy image, dump the database to $UPLOAD_LOCATION/backups,"
  echo "  write the image's compose.yaml (old one kept as compose.yaml.pre-migrate),"
  echo "  add the photo app's settings to .env, write proxy.env from $OLD_CADDY,"
  echo "  remove the old immich and caddy containers (data kept), copy the certificate and"
  echo "  model volumes, start oreo-cloud, and check the asset count and $SITE."
  echo "Dry run complete. Nothing was changed."
  exit 0
fi

say "1. Pull and dump (the site stays up)"
docker pull -q ghcr.io/animesh-net/oreo-cloud-proxy:latest || die "Proxy image pull failed. Nothing changed."
DUMP="$UPLOAD_LOCATION/backups/pre-migrate-$(date +%Y-%m-%d-%H%M).sql.gz"
docker exec immich_postgres pg_dumpall --clean --if-exists --username="$DB_USERNAME" | gzip | sudo tee "$DUMP" >/dev/null
[ "$(sudo stat -c %s "$DUMP" 2>/dev/null || echo 0)" -gt 5000000 ] || die "Database dump failed or too small ($DUMP). Nothing changed."
echo "Dump saved: $DUMP"

say "2. Settings"
cp compose.yaml compose.yaml.pre-migrate
docker run --rm "$OREO_IMAGE" show compose >compose.yaml
{
  echo "$BEGIN"
  echo "UPLOAD_LOCATION=$UPLOAD_LOCATION"
  echo "DB_DATA_LOCATION=$DB_DATA_LOCATION"
  grep -E '^(IMMICH_VERSION|DB_PASSWORD|DB_USERNAME|DB_DATABASE_NAME)=' "$OLD_IMMICH/.env"
  echo "$END"
} >>.env
# Single quotes: the hash is full of $ and must reach Caddy as written.
(umask 077 && { cat "$OLD_CADDY/.env"; echo; echo "PI_AUTH_HASH='$AUTH_HASH'"; } >proxy.env)
docker compose config -q || { undo_settings; die "compose.yaml does not accept the new settings. Nothing else changed."; }

say "3. Stop the old projects and start oreo-cloud (the site is down for a minute)"
old_caddy down || { undo_settings; die "Could not stop caddy. Nothing else changed."; }
old_immich down || { old_caddy up -d; undo_settings; die "Could not stop immich. Caddy restarted, nothing else changed."; }
docker compose create "${NEW_SERVICES[@]}" || rollback
copy_volume caddy_caddy_data oreo-cloud-proxy-data || rollback
copy_volume caddy_caddy_config oreo-cloud-proxy-config || rollback
copy_volume immich_model-cache oreo-cloud-photos-ml-cache || rollback
docker compose up -d --remove-orphans || rollback
echo "Waiting for the photo app to be healthy..."
for _ in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' oreo-cloud-photos 2>/dev/null)" = healthy ] && break
  sleep 3
done

say "4. Verify"
NEW_COUNT=$(count_assets oreo-cloud-db)
echo "oreo-cloud has ${NEW_COUNT:-0} assets (immich had $OLD_COUNT)."
[ "$NEW_COUNT" = "$OLD_COUNT" ] || { echo "Asset counts don't match."; rollback; }
CODE=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$SITE/api/server/ping")
[ "$CODE" = 200 ] || { echo "$SITE/api/server/ping answered HTTP $CODE, not 200."; rollback; }

# A plain `docker compose pull` would now pull a new Immich without its database dump.
for f in "$HOME/.bashrc" "$HOME/.bash_aliases"; do
  [ -f "$f" ] && sed -i "/^alias image-update=/s/docker compose pull &&/docker compose pull heartbeat agent \&\&/" "$f"
done

echo
echo "Done. Everything runs as oreo-cloud-*. Open a new shell for the updated image-update alias."
echo "Kept for now, remove once you're confident: $OLD_IMMICH, $OLD_CADDY, compose.yaml.pre-migrate,"
echo "  the volumes caddy_caddy_data, caddy_caddy_config, immich_model-cache, the local image"
echo "  caddy-caddy, and $DUMP"
