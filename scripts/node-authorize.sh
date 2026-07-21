#!/usr/bin/env bash
# Grant a provisioned node rsync access by adding its key to authorized_keys.
#
# This hands out real access to this account, so it confirms first and shows
# exactly what will be appended. Removal: make node-revoke NODE=<name>, or
# delete the marked line from ~/.ssh/authorized_keys.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f env/server.env ]] && set -a && . ./env/server.env && set +a

node="${1:?usage: node-authorize.sh <node-name>}"
pub="env/nodes/${node}_id_ed25519.pub"
[[ -f "$pub" ]] || { echo "no key for '${node}' — run: make node-provision NODE=${node}"; exit 1; }

AUTH="${HOME}/.ssh/authorized_keys"
marker="rk-mlflow-${node}"

if grep -qF "$marker" "$AUTH" 2>/dev/null; then
  echo "'${node}' is already authorized"
  exit 0
fi

# restrict = no port forwarding, no agent forwarding, no PTY, no X11.
# The node needs to run rsync's server side and nothing more.
entry="restrict,pty $(cat "$pub")"

echo "This appends the following to ${AUTH}:"
echo ""
echo "  ${entry}"
echo ""
echo "It grants '${node}' SSH access to $(whoami)@$(hostname) for rsync."
read -rp "Type the node name to confirm: " confirm
[[ "$confirm" == "$node" ]] || { echo "aborted"; exit 1; }

mkdir -p "${HOME}/.ssh"
touch "$AUTH"
chmod 600 "$AUTH"
printf '%s\n' "$entry" >> "$AUTH"

echo "authorized '${node}'"
echo "revoke with: sed -i '/${marker}/d' ${AUTH}"
