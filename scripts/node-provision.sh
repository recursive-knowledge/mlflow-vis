#!/usr/bin/env bash
# Build a shareable credential bundle for one node or teammate.
#
# The point of this script is the split: env/server.env holds database
# passwords, the JWT secret, the service-role key, and the tunnel token —
# handing that to anyone gives them the whole box. The bundle produced here
# grants exactly two things, telemetry and rsync, and nothing else.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f env/server.env ]] && set -a && . ./env/server.env && set +a

node="${1:?usage: node-provision.sh <node-name>}"
[[ "$node" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo "node name must be lowercase alphanumeric/dash"; exit 1; }

bundle="env/nodes/${node}.env"
keyfile="env/nodes/${node}_id_ed25519"

if [[ -e "$bundle" ]]; then
  echo "$bundle already exists — delete it first to reissue"
  exit 1
fi

# A dedicated key per node, so access can be revoked one node at a time
# without rotating anything else.
ssh-keygen -t ed25519 -N "" -C "rk-mlflow-${node}" -f "$keyfile" >/dev/null
chmod 600 "$keyfile"

# The install line has to name a real remote — a bundle telling its recipient
# to install from a placeholder is worse than one that omits the step. Derived
# from origin rather than hardcoded so it cannot drift if the repo moves.
origin=$(git remote get-url origin 2>/dev/null || true)
case "$origin" in
  # scp-style (git@host:org/repo) needs the colon turned into a slash before
  # it is a valid URL for pip/uv.
  git@*)     install_line="#   uv pip install 'git+ssh://${origin/://}#subdirectory=client'" ;;
  https://*) install_line="#   uv pip install 'git+${origin}#subdirectory=client'" ;;
  *)         install_line="#   uv pip install '/path/to/mlflow-vis/client'   # no git remote configured" ;;
esac

cat > "$bundle" <<EOF
# ---------------------------------------------------------------------------
# rk-mlflow credentials for: ${node}
#
# Safe to hand to whoever runs this node. Grants exactly two things:
#   1. logging telemetry to MLflow through the Cloudflare tunnel
#   2. rsyncing checkpoints to the coordination box over SSH
# It contains no database password and no tunnel token.
#
# Install:
#   source ${node}.env
${install_line}
# ---------------------------------------------------------------------------

# --- telemetry (through the tunnel) ---
export MLFLOW_TRACKING_URI="https://${MLFLOW_HOSTNAME}"

# Cloudflare Access service token. Filled in by \`make node-token NODE=${node}\`;
# if these are still empty, that step has not been run yet.
export CF_ACCESS_CLIENT_ID=""
export CF_ACCESS_CLIENT_SECRET=""

# --- checkpoints (rsync over SSH, never the tunnel) ---
export RK_CKPT_HOST="${CKPT_SSH_USER}@${CKPT_SSH_HOST}"
export RK_CKPT_PORT="${CKPT_SSH_PORT}"
export RK_CKPT_ROOT="${RK_DATA_ROOT}/checkpoints"
# Ships alongside this file; keep it chmod 600.
export RK_CKPT_KEY="\$(dirname "\${BASH_SOURCE[0]}")/${node}_id_ed25519"

# --- verl trainer block ---
#   trainer:
#     project_name: rl-posttraining
#     experiment_name: ${node}
#     logger: ['console', 'mlflow']
#     save_freq: 50
EOF

chmod 600 "$bundle"

cat <<EOF

provisioned '${node}'

  ${bundle}
  ${keyfile}(.pub)

still to do:
  1. make node-token NODE=${node}         # Access service token, into the bundle
     (Needs CLOUDFLARE_API_TOKEN in env/server.env. Without it, create the
      token by hand under Zero Trust > Access > Service Auth, add it to the
      MLflow app's Service Auth policy, and paste both halves into ${bundle} —
      the secret is shown only once.)
  2. make node-authorize NODE=${node}     # grants rsync access over SSH
  3. Send the whole bundle over a private channel — it is a credential.
  4. make test-tunnel NODE=${node}        # verify end to end

EOF
