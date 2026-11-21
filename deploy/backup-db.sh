#!/usr/bin/env bash
# Reference copy of /opt/spb-lead-bot-backup.sh (installed by hand, outside the repository).
# Run by spb-lead-bot-backup.timer once a day as user leadbot.
# Consistent online backup of leads.db via sqlite3 ".backup", integrity check, rotation,
# Telegram alert to the owner on failure. The token never goes to argv, logs or stdout.
set -euo pipefail
umask 077

DB="${DB:-/opt/spb-lead-bot/leads.db}"
DEST="${DEST:-/opt/spb-lead-bot/backups/daily}"
KEEP="${KEEP:-7}"
SQLITE_BIN="${SQLITE_BIN:-sqlite3}"

TMP=""
ALERTED=0

cleanup_tmp() {
  if [ -n "$TMP" ]; then
    rm -f -- "$TMP" "$TMP-wal" "$TMP-shm" "$TMP-journal"
  fi
}

# Sends the alert; never prints the token or the URL. Returns 0 on success.
send_alert() {
  local reason="$1" text
  if [ -z "${BOT_TOKEN:-}" ] || [ -z "${ADMIN_TG_ID:-}" ]; then
    echo "alert skipped: BOT_TOKEN or ADMIN_TG_ID is not set" >&2
    return 1
  fi
  # Reject values that could break the curl config syntax.
  if ! [[ "$BOT_TOKEN" =~ ^[A-Za-z0-9:_-]+$ ]] || ! [[ "$ADMIN_TG_ID" =~ ^-?[0-9]+$ ]]; then
    echo "alert skipped: BOT_TOKEN or ADMIN_TG_ID has an unexpected format" >&2
    return 1
  fi
  text="⚠️ Бэкап leads.db не удался: ${reason}. Проверьте: journalctl -u spb-lead-bot-backup"
  if ! printf '%s\n' \
    "url = \"https://api.telegram.org/bot${BOT_TOKEN}/sendMessage\"" \
    "fail" \
    "data-urlencode = \"chat_id=${ADMIN_TG_ID}\"" \
    "data-urlencode = \"text=${text}\"" |
    curl -sS -m 20 -o /dev/null -K - >/dev/null 2>&1; then
    echo "alert could not be sent (Telegram request failed)" >&2
    return 1
  fi
  return 0
}

fail() {
  local reason="$1"
  cleanup_tmp
  echo "backup failed: ${reason}" >&2
  ALERTED=1
  send_alert "$reason" || true
  exit 1
}

# Any non-zero exit that did not go through fail() (set -e, SIGTERM from the unit timeout)
# still sends exactly one alert. send_alert never exits, so the handler cannot loop.
on_exit() {
  local rc=$?
  trap - EXIT
  cleanup_tmp
  if [ "$rc" -ne 0 ] && [ "$ALERTED" = 0 ]; then
    ALERTED=1
    echo "backup failed: unexpected error (code $rc)" >&2
    send_alert "непредвиденная ошибка (код $rc)" || true
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 143' TERM INT

if ! [[ "$KEEP" =~ ^[0-9]+$ ]] || [ "$KEEP" -lt 1 ]; then
  fail "KEEP должен быть числом не меньше 1"
fi
command -v "$SQLITE_BIN" >/dev/null 2>&1 || fail "sqlite3 не найден"
[ -f "$DB" ] && [ -r "$DB" ] || fail "файл базы не найден или не читается"

mkdir -p -m 700 -- "$DEST" || fail "не удалось создать папку бэкапов"

# Free space check: at least 2x the DB size (KiB).
db_kb=$(($(wc -c <"$DB") / 1024 + 1))
free_kb=$(df -Pk -- "$DEST" | awk 'NR==2 {print $4}')
if [ -z "$free_kb" ] || [ "$free_kb" -lt $((db_kb * 2)) ]; then
  fail "мало свободного места на диске"
fi

day=$(date +%Y%m%d)
final="$DEST/leads-${day}.db"
if [ -e "$final" ]; then
  final="$DEST/leads-${day}-$(date +%H%M%S).db"
  if [ -e "$final" ]; then
    fail "файл ${final##*/} уже существует"
  fi
fi

TMP="$DEST/.leads-$$.tmp"
rm -f -- "$TMP"

"$SQLITE_BIN" "$DB" ".timeout 30000" ".backup '$TMP'" || fail "sqlite3 .backup завершился с ошибкой"
check=$("$SQLITE_BIN" "$TMP" "PRAGMA integrity_check;") || fail "integrity_check не выполнился"
[ "$check" = "ok" ] || fail "integrity_check не прошёл"
"$SQLITE_BIN" "$TMP" "SELECT count(*) FROM leads;" >/dev/null || fail "таблица leads не читается"

chmod 600 "$TMP" || fail "не удалось выставить права на файл"
mv -- "$TMP" "$final" || fail "не удалось переименовать бэкап"
rm -f -- "$TMP-wal" "$TMP-shm"
TMP=""
echo "backup created: ${final##*/}"

# Rotation: only after a successful backup, only leads-YYYYMMDD[-HHMMSS].db inside DEST.
# Sort by name without the extension so that leads-D-HHMMSS sorts after leads-D.
index=0
while IFS= read -r name; do
  index=$((index + 1))
  if [ "$index" -gt "$KEEP" ]; then
    rm -f -- "$DEST/${name}.db"
    echo "rotated out: ${name}.db"
  fi
done < <(
  for f in "$DEST"/leads-*.db; do
    [ -f "$f" ] || continue
    base="${f##*/}"
    if [[ "$base" =~ ^leads-[0-9]{8}(-[0-9]{6})?\.db$ ]]; then
      echo "${base%.db}"
    fi
  done | LC_ALL=C sort -r
)
