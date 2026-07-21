#!/usr/bin/env bash
# Keep the CKPT_KEEP_LAST most recent steps per run; delete older ones.
#
# /overflow is shared and near capacity, so this is not optional hygiene.
# Dry run by default — set APPLY=1 to actually delete.
#
# Protections, in order:
#   *.incoming  — an rsync in flight, never touched
#   .keep       — drop this file in a step dir to pin it forever
#   <30 min old — too fresh to be safely judged stale
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f env/server.env ]] && set -a && . ./env/server.env && set +a

root="${RK_DATA_ROOT}/checkpoints"
keep="${CKPT_KEEP_LAST:-3}"
[[ -d "$root" ]] || { echo "checkpoints: no such directory $root"; exit 0; }

total_freed=0
candidates=()

# Layout: checkpoints/<experiment>/<run_id>/step_<N>/
while IFS= read -r -d '' run_dir; do
  # Newest step first, numerically.
  mapfile -t steps < <(
    find "$run_dir" -mindepth 1 -maxdepth 1 -type d -name 'step_*' -printf '%f\n' 2>/dev/null \
      | grep -E '^step_[0-9]+$' \
      | sed 's/^step_//' | sort -rn | sed 's/^/step_/'
  )
  (( ${#steps[@]} > keep )) || continue

  for stale in "${steps[@]:$keep}"; do
    path="${run_dir}/${stale}"
    [[ -e "${path}/.keep" ]] && continue
    # Skip anything still warm — an interrupted sync must not be reaped.
    [[ -n "$(find "$path" -maxdepth 0 -mmin -30 2>/dev/null)" ]] && continue
    candidates+=("$path")
    total_freed=$(( total_freed + $(du -sm "$path" 2>/dev/null | cut -f1) ))
  done
done < <(find "$root" -mindepth 2 -maxdepth 2 -type d -print0 2>/dev/null)

if (( ${#candidates[@]} == 0 )); then
  echo "checkpoints: nothing to prune (keeping last ${keep} per run)"
  exit 0
fi

if [[ "${APPLY:-0}" == "1" ]]; then
  for path in "${candidates[@]}"; do rm -rf -- "$path"; done
  echo "checkpoints: deleted ${#candidates[@]} step(s), freed ~${total_freed} MB"
else
  echo "checkpoints: would delete ${#candidates[@]} step(s), freeing ~${total_freed} MB (APPLY=1 to do it)"
  printf '  %s\n' "${candidates[@]}"
fi
