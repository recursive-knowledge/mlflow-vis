#!/usr/bin/env bash
# Dump the whole cluster into a tier (daily | weekly).
#
# Postgres holds every run, metric, and checkpoint pointer. The checkpoint
# bytes on disk are unusable without it, so this is the backup that matters.
# pg_dumpall (not pg_dump) because roles and grants are part of a working
# restore — storage-api authenticates as supabase_storage_admin.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f env/server.env ]] && set -a && . ./env/server.env && set +a

tier="${1:-${TIER:-daily}}"
case "$tier" in
  daily|weekly) ;;
  *) echo "usage: backup-postgres.sh [daily|weekly]"; exit 1 ;;
esac

dest="${RK_DATA_ROOT}/backups/${tier}"
mkdir -p "$dest"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="${dest}/cluster-${stamp}.sql.gz"

if ! docker compose ps --services --filter status=running 2>/dev/null | grep -qx db; then
  echo "backup: database is not running — nothing dumped" >&2
  exit 1
fi

# Write to .partial first so a crashed dump is never mistaken for a good one.
docker compose exec -T db pg_dumpall -U postgres --clean --if-exists \
  | gzip -6 > "${out}.partial"

# A gzip that decompresses to almost nothing means the dump failed upstream
# of the pipe — pg_dumpall's exit code is hidden by the pipeline.
size=$(stat -c %s "${out}.partial")
if (( size < 4096 )); then
  rm -f "${out}.partial"
  echo "backup: dump was only ${size} bytes — treating as failed" >&2
  exit 1
fi

mv "${out}.partial" "$out"
chmod 640 "$out"
# --apparent-size: ZFS block accounting otherwise reports a misleading 512.
echo "backup[${tier}]: wrote $(du -h --apparent-size "$out" | cut -f1)  $out"

bash scripts/prune-backups.sh
