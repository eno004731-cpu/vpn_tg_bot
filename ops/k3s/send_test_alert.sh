#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-vpn-prod}"
ALERT_NAME="${ALERT_NAME:-VpnBotTestAlert}"
WAIT_SECONDS="${WAIT_SECONDS:-150}"

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  K3S_BIN=(k3s)
else
  K3S_BIN=(sudo -n k3s)
fi

cat <<EOF | "${K3S_BIN[@]}" kubectl apply -f -
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: vpn-bot-test-alert
  namespace: $NAMESPACE
  labels:
    app.kubernetes.io/part-of: vpn-bot
spec:
  groups:
    - name: vpn-bot.test.rules
      rules:
        - alert: $ALERT_NAME
          expr: vector(1)
          for: 0m
          labels:
            severity: warning
          annotations:
            summary: Test alert from vpn-bot monitoring
            description: This is a manual test alert. If it reached Telegram, Alertmanager routing works.
EOF

echo "Created test alert $ALERT_NAME. Waiting ${WAIT_SECONDS}s for Prometheus and Alertmanager..."
sleep "$WAIT_SECONDS"
"${K3S_BIN[@]}" kubectl delete prometheusrule vpn-bot-test-alert -n "$NAMESPACE" --ignore-not-found
echo "Deleted test alert $ALERT_NAME"
