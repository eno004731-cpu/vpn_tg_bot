#!/usr/bin/env bash
set -euo pipefail

DEPLOY_MODE="${1:-webhook}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
UNITS_DIR="/etc/systemd/system"
STALE_TEMPLATE_UNITS=(
  "vpn-bot-web@.service"
  "vpn-bot-worker@.service"
)

if [[ "$DEPLOY_MODE" != "webhook" && "$DEPLOY_MODE" != "polling" ]]; then
  echo "Unsupported deploy mode for systemd unit installation: $DEPLOY_MODE" >&2
  exit 1
fi

if [[ "$DEPLOY_MODE" == "webhook" ]]; then
  install -m 0644 "$SCRIPT_DIR/vpn-bot-web.service" "$UNITS_DIR/vpn-bot-web.service"
  install -m 0644 "$SCRIPT_DIR/vpn-bot-worker.service" "$UNITS_DIR/vpn-bot-worker.service"
fi

for unit in "${STALE_TEMPLATE_UNITS[@]}"; do
  rm -f "$UNITS_DIR/$unit"
done

systemctl daemon-reload

echo "Installed systemd units for $DEPLOY_MODE from $APP_DIR"
