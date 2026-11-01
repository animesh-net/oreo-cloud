#!/bin/bash
# One-time cutover from the current "immich" project (name: immich, in
# ~/Projects/oreo-mac today) to the renamed "oreo-cloud" project (this folder). Three steps,
# each gating the next: back up and record counts, stop immich & start oreo-cloud, verify.
# immich and oreo-cloud publish the same host ports (2283, 443), so they cannot run at once —
# immich is stopped (containers only, nothing deleted) right before oreo-cloud starts. A
# failed verification rolls back automatically: oreo-cloud is stopped and immich is started
# again, unchanged.
#
# Run from this folder (mac/), with the old project still running and the Passport connected.
#   ./migrate.sh           run the cutover
#   ./migrate.sh --dry-run check what it would do, without stopping or starting anything
set -uo pipefail
cd "$(dirname "$0")"

OLD_DIR="${OLD_IMMICH_DIR:-$HOME/Projects/oreo-mac}"
SITE=https://cloud.animesh.org.in
DRY_RUN=false
[ "${1:-}" = "--dry-run" ] && DRY_RUN=true

old_compose() { (cd "$OLD_DIR" && docker compose --profile proxy "$@"); }
new_compose() { docker compose --profile proxy "$@"; }

count_assets() {
  docker exec immich_postgres psql -t -A -U "$(grep '^DB_USERNAME=' "$OLD_DIR/.env" | cut -d= -f2)" \
    -d "$(grep '^DB_DATABASE_NAME=' "$OLD_DIR/.env" | cut -d= -f2)" \
    -c 'SELECT count(*) FROM asset;' 2>/dev/null | tr -d '[:space:]'
}

site_ok() {
  [ "$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$SITE/api/server/ping")" = 200 ]
}

rollback() {
  echo "Rolling back: stopping oreo-cloud, restarting immich..."
  new_compose stop
  old_compose start
  echo "Rolled back. The Mac is back exactly where it started — nothing was lost."
  exit 1
}

echo "== 1. Back up & count =="
if [ "$DRY_RUN" = true ]; then
  echo "(dry run) would dump the immich database and record its asset count"
else
  old_compose exec -T database pg_dump -U "$(grep '^DB_USERNAME=' "$OLD_DIR/.env" | cut -d= -f2)" \
    -d "$(grep '^DB_DATABASE_NAME=' "$OLD_DIR/.env" | cut -d= -f2)" --clean --if-exists \
    | gzip >"$OLD_DIR/pre-migrate-$(date +%Y%m%d-%H%M%S).sql.gz" || { echo "Dump failed. Nothing changed."; exit 1; }
fi
OLD_COUNT=$(count_assets)
[ -n "$OLD_COUNT" ] || { echo "Could not read the asset count from immich. Nothing changed."; exit 1; }
echo "immich has $OLD_COUNT assets."

echo "== 2. Stop immich & start oreo-cloud =="
if [ "$DRY_RUN" = true ]; then
  echo "(dry run) would add DB_HOSTNAME, REDIS_HOSTNAME and IMMICH_MACHINE_LEARNING_URL to .env if missing (the renamed services don't match Immich's built-in defaults), stop immich (both publish 127.0.0.1:2283 and 100.70.213.121:443, so they can't run together), then run: docker compose --profile proxy up -d"
  echo "Dry run complete. Nothing was changed."
  exit 0
fi
# The renamed services (db, cache, photos-ml) don't match Immich's built-in default
# hostnames, so these three overrides are required — see the rename plan. Added once,
# idempotently, whether this .env came from restore.sh or was carried over as-is.
grep -q '^DB_HOSTNAME=' .env || printf '\nDB_HOSTNAME=db\n' >>.env
grep -q '^REDIS_HOSTNAME=' .env || printf '\nREDIS_HOSTNAME=cache\n' >>.env
grep -q '^IMMICH_MACHINE_LEARNING_URL=' .env || printf '\nIMMICH_MACHINE_LEARNING_URL=http://photos-ml:3003\n' >>.env
# immich is stopped, not removed — its containers, postgres/ and the Passport data all stay
# in place, so a rollback is just starting it again.
old_compose stop || { echo "Could not stop immich. Nothing else changed."; exit 1; }
new_compose up -d || { echo "oreo-cloud failed to start. Restarting immich..."; old_compose start; exit 1; }
echo "Waiting for photos to be healthy..."
for _ in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' oreo-cloud-photos 2>/dev/null)" = healthy ] && break; sleep 3
done

echo "== 3. Verify =="
NEW_COUNT=$(docker exec oreo-cloud-db psql -t -A -U "$(grep '^DB_USERNAME=' .env | cut -d= -f2)" \
  -d "$(grep '^DB_DATABASE_NAME=' .env | cut -d= -f2)" -c 'SELECT count(*) FROM asset;' 2>/dev/null | tr -d '[:space:]')
echo "oreo-cloud has ${NEW_COUNT:-0} assets (immich had $OLD_COUNT)."
if [ "$NEW_COUNT" != "$OLD_COUNT" ] || ! site_ok; then
  echo "Counts don't match, or the site isn't answering."
  rollback
fi

echo "Done. oreo-cloud is now the live stack. immich's stopped containers, its postgres/"
echo "folder and the Passport data are untouched, and can be removed by hand once you're confident."
