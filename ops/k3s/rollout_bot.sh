#!/usr/bin/env bash
set -euo pipefail

IMAGE_TAG="${1:-latest}"
NAMESPACE="${2:-vpn-prod}"
SECRET_NAME="${3:-vpn-bot-runtime}"
IMAGE_REPO="${4:-ghcr.io/eno004731-cpu/vpn_tg_bot}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_TOML_PATH="${RUNTIME_TOML_PATH:-$APP_DIR/secrets/runtime.toml}"
IMAGE_NAME="${IMAGE_REPO}:${IMAGE_TAG}"
ARCHIVE_PATH="/tmp/vpn-bot-image-${IMAGE_TAG}.tar"
if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  K3S_BIN=(k3s)
else
  K3S_BIN=(sudo k3s)
fi

APP_DIR="$APP_DIR" \
IMAGE_NAME="$IMAGE_NAME" \
ARCHIVE_PATH="$ARCHIVE_PATH" \
"$SCRIPT_DIR/build_and_import_image.sh"

NAMESPACE="$NAMESPACE" \
SECRET_NAME="$SECRET_NAME" \
RUNTIME_TOML_PATH="$RUNTIME_TOML_PATH" \
"$SCRIPT_DIR/create_runtime_secret.sh"

if ! "${K3S_BIN[@]}" kubectl get secret postgres-secret -n "$NAMESPACE" >/dev/null 2>&1; then
  cat >&2 <<EOF
postgres-secret is missing in namespace $NAMESPACE.
Create it before rollout so kubectl apply does not deploy Postgres with placeholder credentials.
EOF
  exit 1
fi

"${K3S_BIN[@]}" kubectl apply -k "$APP_DIR/k8s"
"${K3S_BIN[@]}" kubectl set image deployment/vpn-bot-web vpn-bot="$IMAGE_NAME" -n "$NAMESPACE"
"${K3S_BIN[@]}" kubectl set image deployment/vpn-bot-worker vpn-bot="$IMAGE_NAME" -n "$NAMESPACE"
"${K3S_BIN[@]}" kubectl rollout status deployment/vpn-bot-web -n "$NAMESPACE" --timeout=180s
"${K3S_BIN[@]}" kubectl rollout status deployment/vpn-bot-worker -n "$NAMESPACE" --timeout=180s
"${K3S_BIN[@]}" kubectl get pods -n "$NAMESPACE" -o wide

echo "Rolled out $IMAGE_NAME into namespace $NAMESPACE"
