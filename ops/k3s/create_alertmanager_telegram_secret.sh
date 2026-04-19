#!/usr/bin/env bash
set -euo pipefail

MONITORING_NAMESPACE="${MONITORING_NAMESPACE:-monitoring}"
SECRET_NAME="${ALERTMANAGER_SECRET_NAME:-alertmanager-vpn-bot}"
BOT_TOKEN="${ALERTMANAGER_TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${ALERTMANAGER_TELEGRAM_CHAT_ID:-}"

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  K3S_BIN=(k3s)
else
  K3S_BIN=(sudo -n k3s)
fi

if [ -z "$BOT_TOKEN" ]; then
  echo "ALERTMANAGER_TELEGRAM_BOT_TOKEN is required" >&2
  exit 1
fi

if ! printf '%s' "$CHAT_ID" | grep -Eq '^-?[0-9]+$'; then
  echo "ALERTMANAGER_TELEGRAM_CHAT_ID must be a numeric Telegram chat id" >&2
  exit 1
fi

tmp_config="$(mktemp)"
trap 'rm -f "$tmp_config"' EXIT

cat >"$tmp_config" <<EOF
global:
  resolve_timeout: 5m

route:
  receiver: telegram
  group_by: ["alertname", "namespace", "severity"]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h

receivers:
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
