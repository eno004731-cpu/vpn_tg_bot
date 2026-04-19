#!/usr/bin/env bash
set -euo pipefail

DEPLOY_MODE="${1:-webhook}"
POLLING_SERVICE_NAME="vpn-bot"
WEBHOOK_SERVICE_NAMES=(vpn-bot-web vpn-bot-worker)
PROCESS_PATTERNS=(
  "/opt/vpn-bot/.venv/bin/python -m vpn_bot run"
  "/opt/vpn-bot/.venv/bin/python -m vpn_bot web"
  "/opt/vpn-bot/.venv/bin/python -m vpn_bot worker"
)

if [[ "$DEPLOY_MODE" != "webhook" && "$DEPLOY_MODE" != "polling" ]]; then
  echo "Unsupported deploy mode for systemd restart: $DEPLOY_MODE" >&2
  exit 1
fi

if systemctl cat "$POLLING_SERVICE_NAME" >/dev/null 2>&1; then
  systemctl stop "$POLLING_SERVICE_NAME" || true
fi

for service in "${WEBHOOK_SERVICE_NAMES[@]}"; do
  if systemctl cat "$service" >/dev/null 2>&1; then
    systemctl stop "$service" || true
  fi
done

for pattern in "${PROCESS_PATTERNS[@]}"; do
  pkill -f "$pattern" || true
done

sleep 2

if [[ "$DEPLOY_MODE" == "webhook" ]]; then
  if systemctl cat "$POLLING_SERVICE_NAME" >/dev/null 2>&1; then
    systemctl disable --now "$POLLING_SERVICE_NAME" || true
  fi

  systemctl enable "${WEBHOOK_SERVICE_NAMES[@]}"
  for service in "${WEBHOOK_SERVICE_NAMES[@]}"; do
    systemctl reset-failed "$service" || true
    systemctl restart "$service"
    sleep 2
    systemctl is-active --quiet "$service"
    systemctl status "$service" --no-pager
  done
else
  for service in "${WEBHOOK_SERVICE_NAMES[@]}"; do
    if systemctl cat "$service" >/dev/null 2>&1; then
      systemctl disable --now "$service" || true
    fi
  done

  systemctl enable "$POLLING_SERVICE_NAME"
  systemctl reset-failed "$POLLING_SERVICE_NAME" || true
  systemctl restart "$POLLING_SERVICE_NAME"
  sleep 2
  systemctl is-active --quiet "$POLLING_SERVICE_NAME"
  systemctl status "$POLLING_SERVICE_NAME" --no-pager
fi

echo "Restarted services in $DEPLOY_MODE mode"
