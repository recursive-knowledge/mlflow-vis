#!/usr/bin/env bash
# Age out old dumps, per tier.
#
# Hard floor of 2 retained per tier regardless of the configured count: the
# whole point of two tiers is that a bad dump never leaves you with zero
# recoverable copies.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f env/server.env ]] && set -a && . ./env/server.env && set +a

FLOOR=2

prune_tier() {
  local tier="$1" keep="$2"
  local dir="${RK_DATA_ROOT}/backups/${tier}"
  [[ -d "$dir" ]] || return 0
  (( keep < FLOOR )) && keep=$FLOOR

  mapfile -t dumps < <(find "$dir" -maxdepth 1 -name 'cluster-*.sql.gz' | sort -r)
  local total=${#dumps[@]}
  if (( total <= keep )); then
    echo "backups[${tier}]: ${total} kept (limit ${keep})"
    return 0
  fi

  local stale=("${dumps[@]:$keep}")
  if [[ "${APPLY:-1}" == "1" ]]; then
    printf '%s\n' "${stale[@]}" | xargs -r rm -f
    echo "backups[${tier}]: ${keep} kept, ${#stale[@]} removed"
  else
    echo "backups[${tier}]: would remove ${#stale[@]} (APPLY=1 to do it)"
    printf '  %s\n' "${stale[@]}"
  fi
}

prune_tier daily  "${BACKUP_KEEP_DAILY:-7}"
prune_tier weekly "${BACKUP_KEEP_WEEKLY:-8}"
