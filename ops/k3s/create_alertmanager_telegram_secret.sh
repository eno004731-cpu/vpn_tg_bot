#!/usr/bin/env bash
set -euo pipefail

MONITORING_NAMESPACE="${MONITORING_NAMESPACE:-monitoring}"
SECRET_NAME="${ALERTMANAGER_SECRET_NAME:-alertmanager-vpn-bot}"
BOT_TOKEN="${ALERTMANAGER_TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${ALERTMANAGER_TELEGRAM_CHAT_ID:-}"
ACTIONABLE_ALERTS_REGEX="${ACTIONABLE_ALERTS_REGEX:-VpnBotReadyzDown|VpnBotWebhookErrorsHigh|VpnBotProvisionFailuresPresent|VpnBotJobsStuck|VpnBotWorkerDown|PostgresDown|VaultDown|PodCrashLooping|HighCpuOrMemoryUsage|VpnBotBackupJobFailed|K3sNodeNotReady|NodeCpuHigh|NodeMemoryHigh|NodeDiskSpaceLow|VpnBotTestAlert}"

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  K3S_BIN=(k3s)
else
  K3S_BIN=(sudo -n k3s)
fi

existing_config=""
if existing_base64="$("${K3S_BIN[@]}" kubectl get secret "$SECRET_NAME" \
  -n "$MONITORING_NAMESPACE" \
  -o jsonpath='{.data.alertmanager\.yaml}' 2>/dev/null || true)"; then
  if [ -n "$existing_base64" ]; then
    existing_config="$(printf '%s' "$existing_base64" | base64 -d)"
  fi
fi

if [ -z "$BOT_TOKEN" ] && [ -n "$existing_config" ]; then
  BOT_TOKEN="$(printf '%s\n' "$existing_config" | python3 -c '
import re
import sys

match = re.search(r"^[ \t]*bot_token:[ \t]*\"?([^\"\n]+)\"?", sys.stdin.read(), re.MULTILINE)
print(match.group(1) if match else "")
')"
fi

if [ -z "$CHAT_ID" ] && [ -n "$existing_config" ]; then
  CHAT_ID="$(printf '%s\n' "$existing_config" | python3 -c '
import re
import sys

match = re.search(r"^[ \t]*chat_id:[ \t]*(-?[0-9]+)", sys.stdin.read(), re.MULTILINE)
print(match.group(1) if match else "")
')"
fi

if [ -z "$BOT_TOKEN" ]; then
  echo "ALERTMANAGER_TELEGRAM_BOT_TOKEN is required when the existing secret cannot be reused" >&2
  exit 1
fi

if ! printf '%s' "$CHAT_ID" | grep -Eq '^-?[0-9]+$'; then
  echo "ALERTMANAGER_TELEGRAM_CHAT_ID must be a numeric Telegram chat id when the existing secret cannot be reused" >&2
  exit 1
fi

tmp_config="$(mktemp)"
trap 'rm -f "$tmp_config"' EXIT

cat >"$tmp_config" <<EOF
global:
  resolve_timeout: 5m

route:
  receiver: "null"
  group_by: ["alertname", "namespace", "severity"]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - receiver: telegram
      matchers:
        - 'alertname=~"$ACTIONABLE_ALERTS_REGEX"'
        - 'severity=~"critical|warning"'

receivers:
  - name: "null"
  - name: telegram
    telegram_configs:
      - bot_token: "$BOT_TOKEN"
        chat_id: $CHAT_ID
        parse_mode: HTML
        send_resolved: true
        message: |
          <b>{{ .Status | toUpper }}</b> {{ if .CommonLabels.severity }}[{{ .CommonLabels.severity }}]{{ end }}
          {{ range .Alerts }}
          <b>{{ .Labels.alertname }}</b>
          {{ .Annotations.summary }}
          {{ .Annotations.description }}
          {{ end }}
EOF

"${K3S_BIN[@]}" kubectl create namespace "$MONITORING_NAMESPACE" --dry-run=client -o yaml \
  | "${K3S_BIN[@]}" kubectl apply -f -

"${K3S_BIN[@]}" kubectl create secret generic "$SECRET_NAME" \
  -n "$MONITORING_NAMESPACE" \
  --from-file=alertmanager.yaml="$tmp_config" \
  --dry-run=client -o yaml \
  | "${K3S_BIN[@]}" kubectl apply -f -

echo "Updated Alertmanager Telegram secret $SECRET_NAME in namespace $MONITORING_NAMESPACE"
