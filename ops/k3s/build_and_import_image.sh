#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/vpn-bot}"
IMAGE_NAME="${IMAGE_NAME:-ghcr.io/eno004731-cpu/vpn_tg_bot:latest}"
ARCHIVE_PATH="${ARCHIVE_PATH:-/tmp/vpn-bot-image.tar}"
if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  K3S_BIN=(k3s)
else
  K3S_BIN=(sudo k3s)
fi

if [ ! -f "$APP_DIR/Dockerfile" ]; then
  echo "Dockerfile not found in $APP_DIR" >&2
  exit 1
fi

echo "Building image $IMAGE_NAME from $APP_DIR"
docker build -t "$IMAGE_NAME" "$APP_DIR"
docker save "$IMAGE_NAME" -o "$ARCHIVE_PATH"
"${K3S_BIN[@]}" ctr images import "$ARCHIVE_PATH"
rm -f "$ARCHIVE_PATH"

echo "Imported $IMAGE_NAME into k3s containerd"
