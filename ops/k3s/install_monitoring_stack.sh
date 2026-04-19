#!/usr/bin/env bash
set -euo pipefail

MONITORING_NAMESPACE="${MONITORING_NAMESPACE:-monitoring}"
ALERTMANAGER_SECRET_NAME="${ALERTMANAGER_SECRET_NAME:-alertmanager-vpn-bot}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
VALUES_FILE="${VALUES_FILE:-$APP_DIR/k8s/monitoring/kube-prometheus-stack-values.yaml}"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Run this script with sudo so helm can use the k3s kubeconfig." >&2
  exit 1
fi

K3S_BIN=(k3s)
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

if ! command -v helm >/dev/null 2>&1; then
  echo "helm is required. Install Helm first, then rerun this script." >&2
  exit 1
fi

"${K3S_BIN[@]}" kubectl create namespace "$MONITORING_NAMESPACE" --dry-run=client -o yaml \
  | "${K3S_BIN[@]}" kubectl apply -f -

if ! "${K3S_BIN[@]}" kubectl get secret "$ALERTMANAGER_SECRET_NAME" -n "$MONITORING_NAMESPACE" >/dev/null 2>&1; then
  cat >&2 <<EOF
Alertmanager secret $ALERTMANAGER_SECRET_NAME is missing in namespace $MONITORING_NAMESPACE.
Run:
  sudo env ALERTMANAGER_TELEGRAM_BOT_TOKEN=... ALERTMANAGER_TELEGRAM_CHAT_ID=... \\
    $APP_DIR/ops/k3s/create_alertmanager_telegram_secret.sh
EOF
  exit 1
fi

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n "$MONITORING_NAMESPACE" \
  -f "$VALUES_FILE"

"${K3S_BIN[@]}" kubectl apply -k "$APP_DIR/k8s/monitoring"
"${K3S_BIN[@]}" kubectl rollout status statefulset/alertmanager-kube-prometheus-stack-alertmanager \
  -n "$MONITORING_NAMESPACE" \
  --timeout=180s
"${K3S_BIN[@]}" kubectl rollout status statefulset/prometheus-kube-prometheus-stack-prometheus \
  -n "$MONITORING_NAMESPACE" \
  --timeout=300s

echo "Monitoring stack is installed and vpn-bot alert rules are applied"
