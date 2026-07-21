#!/usr/bin/env bash
# Answers the only question that matters at 3am: is the coordination box
# accepting telemetry, and will it still have room tomorrow?
set -uo pipefail
cd "$(dirname "$0")/.."
[[ -f env/server.env ]] && set -a && . ./env/server.env && set +a

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED=1; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
FAILED=0

running() { docker compose --profile studio --profile tunnel ps --services --filter status=running 2>/dev/null | grep -qx "$1"; }

echo ""
echo "rk-mlflow health"
echo ""

# --- database -------------------------------------------------------------
if docker compose exec -T db pg_isready -U postgres -h localhost >/dev/null 2>&1; then
  runs=$(docker compose exec -T db psql -U postgres -d mlflow -tAc \
          'select count(*) from runs' 2>/dev/null | tr -d '[:space:]')
  if [[ -n "$runs" ]]; then
    ok "postgres up — ${runs} run(s) recorded"
  else
    warn "postgres up, but the mlflow schema is not initialised yet"
  fi
else
  bad "postgres not accepting connections"
fi

# --- storage --------------------------------------------------------------
if running storage; then
  if docker compose exec -T storage wget -q --spider http://127.0.0.1:5000/status 2>/dev/null; then
    ok "supabase storage responding"
  else
    bad "storage container is up but /status is failing"
  fi
else
  bad "storage not running"
fi

# --- tracking server ------------------------------------------------------
base="http://${MLFLOW_BIND_ADDR:-127.0.0.1}:${MLFLOW_PORT:-5000}"
if curl -fsS --max-time 5 "${base}/health" >/dev/null 2>&1; then
  ok "mlflow responding on ${base}"
else
  bad "mlflow not responding on ${base}"
fi

# --- tunnel ---------------------------------------------------------------
if running cloudflared; then
  if docker compose --profile tunnel exec -T cloudflared \
       cloudflared tunnel --metrics localhost:2000 ready >/dev/null 2>&1; then
    ok "cloudflare tunnel connected"
  else
    bad "cloudflared is up but has no healthy edge connection"
  fi
  if [[ -n "${MLFLOW_HOSTNAME:-}" && "${MLFLOW_HOSTNAME}" != "mlflow.example.com" ]]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://${MLFLOW_HOSTNAME}/health" 2>/dev/null)
    case "$code" in
      302|401|403) ok "https://${MLFLOW_HOSTNAME} reachable, gated by Access (HTTP ${code})" ;;
      200)         warn "https://${MLFLOW_HOSTNAME} returned 200 — Access is NOT in front of it" ;;
      *)           bad "https://${MLFLOW_HOSTNAME} returned HTTP ${code:-no-response}" ;;
    esac
  fi
else
  warn "tunnel not running — UI is loopback-only (make up-tunnel to publish)"
fi

# --- studio ---------------------------------------------------------------
if running kong; then
  if [[ "${STUDIO_BIND_ADDR:-127.0.0.1}" != "127.0.0.1" ]]; then
    bad "studio is bound to ${STUDIO_BIND_ADDR} — the admin console must stay on loopback"
  else
    ok "studio up on loopback :${STUDIO_PORT:-8000}"
  fi
fi

# --- backups --------------------------------------------------------------
# Captured rather than piped into grep -q: this script runs under pipefail,
# and grep -q exits on the first match, so the writer takes SIGPIPE and the
# whole pipeline reports failure even when the string was found.
cron_status=$(bash scripts/backup-cron.sh status 2>/dev/null || true)
if [[ "$cron_status" == *ENABLED* ]]; then
  d=$(find "${RK_DATA_ROOT}/backups/daily"  -name 'cluster-*.sql.gz' 2>/dev/null | wc -l)
  w=$(find "${RK_DATA_ROOT}/backups/weekly" -name 'cluster-*.sql.gz' 2>/dev/null | wc -l)
  if (( d + w >= 2 )); then
    ok "backups scheduled — ${d} daily, ${w} weekly retained"
  else
    warn "backups scheduled but only ${d} daily / ${w} weekly exist yet (make backup)"
  fi
else
  warn "scheduled backups are OFF (make backup-cron-enable)"
fi

# --- capacity -------------------------------------------------------------
root="${RK_DATA_ROOT:-/overflow/recursive-knowledge}"
avail_gb=$(df -BG --output=avail "$root" 2>/dev/null | tail -1 | tr -dc '0-9')
if [[ -n "$avail_gb" ]]; then
  if (( avail_gb < ${DISK_WARN_GB:-200} )); then
    warn "only ${avail_gb}G free on ${root} (threshold ${DISK_WARN_GB:-200}G) — make prune APPLY=1"
  else
    ok "${avail_gb}G free on ${root}"
  fi
fi

echo ""
exit $FAILED
