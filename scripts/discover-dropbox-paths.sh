#!/usr/bin/env bash
set -euo pipefail

REMOTE="${RCLONE_REMOTE:-dropbox}"

docker run --rm -it \
  -v "$PWD/appdata/rclone:/config/rclone" \
  rclone/rclone:latest \
  lsf "$REMOTE:Apps" --max-depth 3 --config /config/rclone/rclone.conf

echo
echo "Set DROPBOX_WAHOO_PATH in .env to the Wahoo folder you see above, often Apps/WahooFitness."

