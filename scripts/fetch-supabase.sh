#!/usr/bin/env bash
# Vendor the Supabase init SQL and Kong config at the pinned commit.
#
# We run a trimmed Supabase (no auth/realtime/functions/analytics/pooler), but
# the supabase/postgres image still expects its full init-script set — those
# scripts create the roles storage-api authenticates as. So we fetch all of
# them rather than guessing which are load-bearing.
set -euo pipefail
cd "$(dirname "$0")/.."

ref=$(grep -vE '^\s*#|^\s*$' supabase/PINNED_REF | head -1 | tr -d '[:space:]')
[[ -n "$ref" ]] || { echo "supabase/PINNED_REF has no commit SHA"; exit 1; }

base="https://raw.githubusercontent.com/supabase/supabase/${ref}/docker"
dest="supabase/upstream"
mkdir -p "$dest/db" "$dest/api"

fetch() {
  local rel="$1" out="$2"
  if curl -sfL --max-time 30 "${base}/${rel}" -o "${out}.tmp"; then
    mv "${out}.tmp" "$out"
    printf '  fetched %s\n' "$rel"
  else
    rm -f "${out}.tmp"
    echo "  FAILED  ${rel}" >&2
    return 1
  fi
}

echo "vendoring supabase assets @ ${ref:0:12}"
for f in roles jwt webhooks realtime logs pooler _supabase; do
  fetch "volumes/db/${f}.sql" "${dest}/db/${f}.sql"
done
fetch "volumes/api/kong.yml"           "${dest}/api/kong.yml"
fetch "volumes/api/kong-entrypoint.sh" "${dest}/api/kong-entrypoint.sh"

echo "$ref" > "${dest}/.ref"
echo "done — ${dest}/ is at ${ref:0:12}"
