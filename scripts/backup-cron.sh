#!/usr/bin/env bash
# Install / remove the daily + weekly backup cron entries for this user.
#
# No root needed — these go in the invoking user's crontab. Entries are
# fenced by marker comments so enable/disable is exact and never disturbs
# unrelated crontab lines.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

BEGIN="# >>> rk-mlflow backups >>>"
END="# <<< rk-mlflow backups <<<"
# Logs live with the data they describe, not in the code repo — same rule as
# the dumps themselves. Keeps the repo clean and puts a backup's log next to
# the backup when you are working out why one is missing.
LOG_DIR="${RK_DATA_ROOT:?RK_DATA_ROOT unset — is env/server.env present?}/backups/logs"

usage() { echo "usage: backup-cron.sh {enable|disable|status}"; exit 1; }

current_crontab() { crontab -l 2>/dev/null || true; }

strip_block() {
  # Delete anything between the markers, inclusive.
  current_crontab | sed "\|${BEGIN}|,\|${END}|d"
}

case "${1:-}" in
  enable)
    mkdir -p "$LOG_DIR"
    # Staggered off the hour so the two never overlap, and weekly runs after
    # the daily has finished on Sunday.
    block=$(cat <<EOF
${BEGIN}
# Managed by 'make backup-cron-enable'. Edit the Makefile, not this block.
17 3 * * *  cd ${REPO} && /usr/bin/env bash scripts/backup-postgres.sh daily  >> ${LOG_DIR}/daily.log 2>&1
47 4 * * 0  cd ${REPO} && /usr/bin/env bash scripts/backup-postgres.sh weekly >> ${LOG_DIR}/weekly.log 2>&1
${END}
EOF
)
    { strip_block; printf '%s\n' "$block"; } | crontab -
    echo "backup cron enabled:"
    echo "  daily   03:17 every day     -> ${RK_DATA_ROOT:-\$RK_DATA_ROOT}/backups/daily"
    echo "  weekly  04:47 every Sunday  -> ${RK_DATA_ROOT:-\$RK_DATA_ROOT}/backups/weekly"
    echo "  logs    ${LOG_DIR}/"
    ;;
  disable)
    if ! current_crontab | grep -qF "$BEGIN"; then
      echo "backup cron was not enabled"
      exit 0
    fi
    strip_block | crontab -
    echo "backup cron disabled (existing dumps left untouched)"
    ;;
  status)
    if current_crontab | grep -qF "$BEGIN"; then
      echo "backup cron: ENABLED"
      current_crontab | sed -n "\|${BEGIN}|,\|${END}|p" | grep -E '^[0-9]' | sed 's/^/  /'
    else
      echo "backup cron: disabled  (make backup-cron-enable)"
    fi
    ;;
  *) usage ;;
esac
