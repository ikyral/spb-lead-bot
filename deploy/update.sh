#!/usr/bin/env bash
# Reference copy of /opt/spb-lead-bot-update.sh (installed by hand, outside the repository).
# Run by spb-lead-bot-update.timer every 5 minutes as user leadbot.
# Postpones the update while a collection is running (a restart would kill Chrome mid-run).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/spb-lead-bot}"
BRANCH="${BRANCH:-main}"
SERVICE="${SERVICE:-spb-lead-bot}"
VENV="${VENV:-$APP_DIR/venv}"
LOCK_FILE="${LOCK_FILE:-/tmp/${SERVICE}-update.lock}"
SETTLE_SECONDS="${SETTLE_SECONDS:-15}"
# Collection guard settings (overridable for tests).
DB_PATH="${UPDATE_DB_PATH:-$APP_DIR/leads.db}"
SQLITE_BIN="${UPDATE_SQLITE_BIN:-sqlite3}"
COLLECT_MAX_AGE_SEC="${UPDATE_COLLECT_MAX_AGE_SEC:-10800}"

# Returns 0 if a collection is running now. Fails open: any problem means "not running".
collection_running() {
  local count
  if ! command -v "$SQLITE_BIN" >/dev/null 2>&1; then
    echo "guard skipped: sqlite3 not found, updating anyway" >&2
    return 1
  fi
  if [ ! -r "$DB_PATH" ]; then
    echo "guard skipped: cannot read $DB_PATH, updating anyway" >&2
    return 1
  fi
  if ! count=$("$SQLITE_BIN" -readonly -cmd ".timeout 3000" "$DB_PATH" \
    "SELECT COUNT(*) FROM collector_state WHERE id = 1 AND last_status = 'running'
       AND last_started_at IS NOT NULL
       AND (last_finished_at IS NULL OR last_finished_at < last_started_at)
       AND julianday(last_started_at) > julianday('now') - ${COLLECT_MAX_AGE_SEC} / 86400.0;" 2>/dev/null); then
    echo "guard skipped: query on $DB_PATH failed, updating anyway" >&2
    return 1
  fi
  [ "$count" = "1" ]
}

main() {
  exec 9>"$LOCK_FILE"
  flock -n 9 || { echo "update already running"; return 0; }

  if collection_running; then
    echo "$(date -Is) collection in progress, update postponed"
    return 0
  fi

  cd "$APP_DIR"
  git fetch --quiet origin "$BRANCH"
  local old new
  old=$(git rev-parse HEAD)
  new=$(git rev-parse "origin/$BRANCH")
  [ "$old" = "$new" ] && return 0

  echo "$(date -Is) update $old -> $new"
  local req_old
  req_old=$(sha256sum requirements.txt | cut -d' ' -f1)

  git merge --ff-only "origin/$BRANCH"

  if [ "$req_old" != "$(sha256sum requirements.txt | cut -d' ' -f1)" ]; then
    "$VENV/bin/pip" install -r requirements.txt
    "$VENV/bin/playwright" install chromium || true
  fi

  sudo systemctl restart "$SERVICE"
  sleep "$SETTLE_SECONDS"
  if systemctl is-active --quiet "$SERVICE"; then
    echo "$(date -Is) OK"
  else
    echo "$(date -Is) FAILED, rollback to $old"
    git reset --hard "$old"
    "$VENV/bin/pip" install -r requirements.txt
    sudo systemctl restart "$SERVICE"
    return 1
  fi
}

main "$@"
exit $?
