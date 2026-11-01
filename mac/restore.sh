#!/bin/bash
# One-time restore of the Pi's newest Immich database dump into a fresh oreo-cloud on this Mac.
# Follows Immich's documented restore steps. Reads the photos copy; never touches the Pi's drives.
set -euo pipefail
cd "$(dirname "$0")"
if grep -q '^DB_PASSWORD=SET_BY_RESTORE$' .env; then
  NEW=$(openssl rand -hex 24)
  sed -i '' "s/^DB_PASSWORD=SET_BY_RESTORE$/DB_PASSWORD=$NEW/" .env
fi
# The renamed services (db, cache, photos-ml) don't match Immich's built-in default
# hostnames, so these three overrides are required — see the rename plan. Added once,
# idempotently, so running this script again is still safe. The leading newline is
# deliberate: it keeps a line safe to append even if .env's last line has none of its own.
grep -q '^DB_HOSTNAME=' .env || printf '\nDB_HOSTNAME=db\n' >>.env
grep -q '^REDIS_HOSTNAME=' .env || printf '\nREDIS_HOSTNAME=cache\n' >>.env
grep -q '^IMMICH_MACHINE_LEARNING_URL=' .env || printf '\nIMMICH_MACHINE_LEARNING_URL=http://photos-ml:3003\n' >>.env
source .env
if [ ! -d "$UPLOAD_LOCATION" ]; then
  echo "Photos not found at $UPLOAD_LOCATION. Is the WD Passport (ImmichTemp) connected?"; exit 1
fi
if [ -n "$(ls -A "$DB_DATA_LOCATION" 2>/dev/null)" ]; then
  echo "$DB_DATA_LOCATION is not empty, so a database already exists. Stopping to avoid overwriting it."; exit 1
fi

DUMP=$(ls "$UPLOAD_LOCATION"/backups/immich-db-backup-*.sql.gz | sort | tail -1)
echo "Restoring: $DUMP"

docker compose pull
docker compose create
docker start oreo-cloud-db
until docker exec oreo-cloud-db pg_isready -U "$DB_USERNAME" >/dev/null 2>&1; do sleep 2; done
sleep 5

gunzip --stdout "$DUMP" \
  | sed "s/SELECT pg_catalog.set_config('search_path', '', false);/SELECT pg_catalog.set_config('search_path', 'public, pg_catalog', true);/g" \
  | docker exec -i oreo-cloud-db psql --dbname="$DB_DATABASE_NAME" --username="$DB_USERNAME" >restore.log 2>&1

# The dump carries the Pi's database password; switch it to the one in .env.
docker exec oreo-cloud-db psql --username="$DB_USERNAME" --dbname=postgres \
  -c "ALTER USER \"$DB_USERNAME\" WITH PASSWORD '$DB_PASSWORD';" >/dev/null

echo "Database restored (details in restore.log). Starting oreo-cloud..."
docker compose up -d
echo "Open http://localhost:2283 in a minute and sign in with your usual Immich account."
