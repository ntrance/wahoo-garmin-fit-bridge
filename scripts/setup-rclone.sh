#!/usr/bin/env bash
set -euo pipefail

mkdir -p appdata/rclone

docker run --rm -it \
  -v "$PWD/appdata/rclone:/config/rclone" \
  rclone/rclone:latest \
  config --config /config/rclone/rclone.conf

echo
echo "Use remote name: dropbox"

