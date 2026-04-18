#!/usr/bin/env bash
set -euo pipefail

export TZ="${BACKUP_TIMEZONE:-Europe/Moscow}"

BACKUP_ROOT="${BACKUP_ROOT:-/srv/backups/vpn-bot}"
SQLITE_DB="${SQLITE_DB:-/opt/vpn-bot/data/bot.sqlite3}"
RESTORE_ROOT="$BACKUP_ROOT/restore-check"
archive_dir="$BACKUP_ROOT/host/daily"
mkdir -p "$RESTORE_ROOT"

latest_archive="$(find "$archive_dir" -maxdepth 1 -type f -name 'host-*.tar.gz' | sort -r | head -n 1 || true)"
if [ -z "$latest_archive" ]; then
  echo "No host backup archive found in $archive_dir"
  exit 1
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

echo "[$(date -Is)] Running restore check for $latest_archive"
tar -xzf "$latest_archive" -C "$tmp_dir"

restored_sqlite="$tmp_dir$SQLITE_DB"
if [ -f "$restored_sqlite" ]; then
  python3 - "$restored_sqlite" <<'PY'
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
  echo "SQLite restore check passed: $restored_sqlite"
else
  echo "SQLite file absent in archive, skipping database restore check: $restored_sqlite"
fi

checksum_file="$RESTORE_ROOT/latest-host-backup.sha256"
shasum -a 256 "$latest_archive" > "$checksum_file"
echo "Wrote checksum: $checksum_file"
echo "[$(date -Is)] Restore check finished"
