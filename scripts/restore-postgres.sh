#!/usr/bin/env bash
# Destructive: replays a pg_dumpall over the running cluster, dropping and
# recreating every database and role it contains.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f env/server.env ]] && set -a && . ./env/server.env && set +a

dump="${1:?usage: restore-postgres.sh <cluster-*.sql.gz>}"
[[ -f "$dump" ]] || { echo "no such file: $dump"; exit 1; }

echo "This REPLACES every database and role in the running cluster with:"
echo "  $dump"
echo "  $(du -h "$dump" | cut -f1), taken $(date -r "$dump" '+%Y-%m-%d %H:%M')"
echo ""
echo "MLflow will be stopped during the restore."
read -rp "Type RESTORE to confirm: " confirm
[[ "$confirm" == "RESTORE" ]] || { echo "aborted"; exit 1; }

# MLflow holds pooled connections that would block DROP DATABASE.
docker compose stop mlflow storage rest >/dev/null

gunzip -c "$dump" | docker compose exec -T db psql -U postgres -d postgres

echo "restored — restarting services…"
docker compose start rest storage mlflow >/dev/null
sleep 5
bash scripts/health.sh
