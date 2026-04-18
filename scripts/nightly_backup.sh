#!/usr/bin/env bash
set -euo pipefail

export TZ="${BACKUP_TIMEZONE:-Europe/Moscow}"

BACKUP_ROOT="${BACKUP_ROOT:-/srv/backups/vpn-bot}"
APP_DIR="${APP_DIR:-/opt/vpn-bot}"
SECRETS_FILE="${SECRETS_FILE:-/opt/vpn-bot/secrets/runtime.toml}"
SQLITE_DB="${SQLITE_DB:-$APP_DIR/data/bot.sqlite3}"
VPN_BOT_SYSTEMD="${VPN_BOT_SYSTEMD:-/etc/systemd/system/vpn-bot.service}"
VPN_BOT_ENV_FILE="${VPN_BOT_ENV_FILE:-/etc/default/vpn-bot}"
XUI_SYSTEMD="${XUI_SYSTEMD:-/etc/systemd/system/x-ui.service}"
XUI_HOME="${XUI_HOME:-/usr/local/x-ui}"
XUI_DATA_DIR="${XUI_DATA_DIR:-/etc/x-ui}"
K3S_CONFIG_DIR="${K3S_CONFIG_DIR:-/etc/rancher/k3s}"
K3S_MANIFESTS_DIR="${K3S_MANIFESTS_DIR:-/var/lib/rancher/k3s/server/manifests}"
RETENTION_DAILY="${RETENTION_DAILY:-14}"
RETENTION_WEEKLY="${RETENTION_WEEKLY:-8}"
CERT_DIRS="${CERT_DIRS:-/root/cert /etc/letsencrypt}"

timestamp="$(date +%Y%m%d-%H%M%S)"
weekday="$(date +%u)"
daily_dir="$BACKUP_ROOT/host/daily"
weekly_dir="$BACKUP_ROOT/host/weekly"
logs_dir="$BACKUP_ROOT/logs"
restore_dir="$BACKUP_ROOT/restore-check"
mkdir -p "$daily_dir" "$weekly_dir" "$logs_dir" "$restore_dir"

log_file="$logs_dir/host-backup-$timestamp.log"
tmp_dir="$(mktemp -d)"
stage_dir="$tmp_dir/rootfs"
mkdir -p "$stage_dir"

cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

exec > >(tee -a "$log_file") 2>&1

echo "[$(date -Is)] Starting host-level nightly backup"

copy_path() {
  local path="$1"
  if [ -e "$path" ]; then
    mkdir -p "$stage_dir"
    cp -a --parents "$path" "$stage_dir"
    echo "Included: $path"
  else
    echo "Skipped missing path: $path"
  fi
}

verify_sqlite() {
  local database="$1"
  if [ ! -f "$database" ]; then
    echo "SQLite database not found, skipping integrity check: $database"
    return 0
  fi
  python3 - "$database" <<'PY'
import sqlite3
import sys

database = sys.argv[1]
conn = sqlite3.connect(database)
try:
    row = conn.execute("PRAGMA integrity_check").fetchone()
finally:
    conn.close()
if not row or row[0].lower() != "ok":
    raise SystemExit(f"SQLite integrity check failed: {row!r}")
PY
  echo "SQLite integrity check passed: $database"
}

prune_backups() {
  local directory="$1"
  local keep="$2"
  mapfile -t backups < <(find "$directory" -maxdepth 1 -type f | sort -r)
  if [ "${#backups[@]}" -le "$keep" ]; then
    return 0
  fi
  for old_backup in "${backups[@]:$keep}"; do
    rm -f "$old_backup"
    echo "Pruned: $old_backup"
  done
}

verify_sqlite "$SQLITE_DB"

copy_path "$SQLITE_DB"
copy_path "$SECRETS_FILE"
copy_path "$VPN_BOT_SYSTEMD"
copy_path "$VPN_BOT_ENV_FILE"
copy_path "$XUI_SYSTEMD"
copy_path "$XUI_HOME"
copy_path "$XUI_DATA_DIR"
copy_path "$K3S_CONFIG_DIR"
copy_path "$K3S_MANIFESTS_DIR"

for cert_dir in $CERT_DIRS; do
  copy_path "$cert_dir"
done

for app_path in "$APP_DIR/config" "$APP_DIR/k8s" "$APP_DIR/data" "$APP_DIR/scripts"; do
  copy_path "$app_path"
done

archive_path="$daily_dir/host-$timestamp.tar.gz"
tar -czf "$archive_path" -C "$stage_dir" .
echo "Created daily archive: $archive_path"

if [ "$weekday" = "1" ]; then
  weekly_archive="$weekly_dir/host-week-$(date +%G-W%V).tar.gz"
  cp "$archive_path" "$weekly_archive"
  echo "Created weekly archive: $weekly_archive"
fi

prune_backups "$daily_dir" "$RETENTION_DAILY"
prune_backups "$weekly_dir" "$RETENTION_WEEKLY"

echo "[$(date -Is)] Host-level nightly backup finished"
