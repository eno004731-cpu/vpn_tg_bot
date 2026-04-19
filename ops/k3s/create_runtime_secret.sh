#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-vpn-prod}"
SECRET_NAME="${SECRET_NAME:-vpn-bot-runtime}"
RUNTIME_TOML_PATH="${RUNTIME_TOML_PATH:-/opt/vpn-bot/secrets/runtime.toml}"
if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  K3S_BIN=(k3s)
else
  K3S_BIN=(sudo k3s)
fi

if [ ! -f "$RUNTIME_TOML_PATH" ]; then
  echo "Runtime file not found: $RUNTIME_TOML_PATH" >&2
  exit 1
fi

"${K3S_BIN[@]}" kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | "${K3S_BIN[@]}" kubectl apply -f -
"${K3S_BIN[@]}" kubectl create secret generic "$SECRET_NAME" \
  -n "$NAMESPACE" \
  --from-file=runtime.toml="$RUNTIME_TOML_PATH" \
  --dry-run=client -o yaml | "${K3S_BIN[@]}" kubectl apply -f -

echo "Updated secret $SECRET_NAME in namespace $NAMESPACE from $RUNTIME_TOML_PATH"
