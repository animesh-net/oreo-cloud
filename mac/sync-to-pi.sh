#!/bin/bash
# Run on the Mac once the new Pi is up (after the Tailscale addresses are swapped back and
# oreo-cloud on the Mac is stopped). Sends everything the Mac's Immich added since the restore
# (new photos and videos, and its newest database dump) to a staging folder on the Pi. It
# never deletes or overwrites anything on the Pi. Thumbnails and encoded videos are skipped;
# the Pi rebuilds them.
set -euo pipefail
PI="${1:-animesh@oreo-cloud}"
SRC=/Volumes/ImmichTemp/library/
[ -d "$SRC" ] || { echo "Passport (ImmichTemp) not connected."; exit 1; }

echo "Copying new files to $PI:~/from-mac/library (only files the Pi's SSD does not already have)..."
rsync -av --progress \
  --exclude thumbs/ --exclude encoded-video/ \
  --compare-dest=/mnt/data-hot-ssd1/library/ \
  "$SRC" "$PI:from-mac/library/"

cat <<'NEXT'

Done. Now on the Pi:
  1. See what arrived:   find ~/from-mac/library -type f | head -50 && du -sh ~/from-mac
  2. Move it into place: sudo rsync -a ~/from-mac/library/ /mnt/data-hot-ssd1/library/
  3. Update the Pi's oreo-cloud first (image-update): the Mac runs a newer Immich release, and a
     dump must not be restored into an older Immich.
  4. In Immich on the Pi: Administration -> restore from backup -> pick the newest
     immich-db-backup-* (the one made on the Mac). Then run the "missing thumbnails" job.
  5. When Immich looks right: rm -rf ~/from-mac
NEXT
