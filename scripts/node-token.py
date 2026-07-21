"""Issue a Cloudflare Access service token for a node, straight into its bundle.

`make node-provision` leaves CF_ACCESS_CLIENT_ID/SECRET blank because the
secret half is returned exactly once — in the creation response — and never
again. Doing that by hand means copying a one-shot value out of the dashboard
without fumbling it. This creates the token and writes both halves into
env/nodes/<node>.env in the same step, so the secret never touches a clipboard.

It also adds the token to the MLflow application's Service Auth policy, which
is the part people forget: a valid token with no policy referencing it still
gets a login page, and MLflow reports that as a JSON parse error.

    make node-token NODE=arjun

Needs CLOUDFLARE_API_TOKEN in env/server.env with two account-scoped
permissions: 'Access: Service Tokens > Edit' and 'Access: Apps and Policies >
Edit'. The tunnel's own cert.pem cannot do this — it is 403 on every Access
endpoint.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
API = "https://api.cloudflare.com/client/v4"

# The Service Auth policy every node joins. One policy holding N tokens, so
# provisioning node two does not mean re-editing the application by hand.
POLICY_NAME = "nodes"


def load_env() -> dict[str, str]:
    path = REPO / "env" / "server.env"
    if not path.exists():
        sys.exit("no env/server.env — run: make install")
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def account_id(env: dict[str, str]) -> str:
    """Prefer an explicit setting; otherwise reuse what `cloudflared login` saved.

    cert.pem's embedded token has no Access authority, but the account ID
    inside it is the same one these calls need — so a working tunnel means
    this never has to be configured separately.
    """
    if env.get("CLOUDFLARE_ACCOUNT_ID"):
        return env["CLOUDFLARE_ACCOUNT_ID"]
    cert = Path.home() / ".cloudflared" / "cert.pem"
    if not cert.exists():
        sys.exit(
            "cannot determine the account id — set CLOUDFLARE_ACCOUNT_ID in "
            "env/server.env, or run: cloudflared tunnel login"
        )
    blob = "".join(
        l.strip() for l in cert.read_text().splitlines() if not l.startswith("-----")
    )
    return json.loads(base64.b64decode(blob + "=="))["accountID"]


class CF:
    def __init__(self, token: str, account: str) -> None:
        self.account = account
        self.s = requests.Session()
        self.s.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    def __call__(self, method: str, path: str, body: dict | None = None) -> dict:
        r = self.s.request(method, f"{API}{path}", json=body, timeout=30)
        try:
            data = r.json()
        except ValueError:
            sys.exit(f"unparseable response from Cloudflare (HTTP {r.status_code})")
        if not data.get("success"):
            errs = "; ".join(
                f"{e.get('code')}: {e.get('message')}" for e in data.get("errors") or []
            )
            if r.status_code == 403:
                errs += "  — is the API token missing an Access permission?"
            sys.exit(f"Cloudflare API error (HTTP {r.status_code}) {errs}")
        return data["result"]


def write_bundle(node: str, client_id: str, client_secret: str) -> Path:
    """Fill the two blanks in place, leaving the bundle's comments intact."""
    path = REPO / "env" / "nodes" / f"{node}.env"
    if not path.exists():
        sys.exit(f"no bundle for '{node}' — run: make node-provision NODE={node}")
    out = []
    for line in path.read_text().splitlines():
        if re.match(r"^export CF_ACCESS_CLIENT_ID=", line):
            out.append(f'export CF_ACCESS_CLIENT_ID="{client_id}"')
        elif re.match(r"^export CF_ACCESS_CLIENT_SECRET=", line):
            out.append(f'export CF_ACCESS_CLIENT_SECRET="{client_secret}"')
        else:
            out.append(line)
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)
    return path


def main() -> int:
    node = sys.argv[1] if len(sys.argv) > 1 else ""
    if not node:
        sys.exit("usage: node-token.py <node-name>")

    env = load_env()
    api_token = env.get("CLOUDFLARE_API_TOKEN", "")
    if not api_token:
        sys.exit(
            "CLOUDFLARE_API_TOKEN is not set in env/server.env.\n"
            "  Create one at https://dash.cloudflare.com/profile/api-tokens with\n"
            "  account permissions: Access: Service Tokens > Edit,\n"
            "                       Access: Apps and Policies > Edit"
        )
    hostname = env.get("MLFLOW_HOSTNAME", "")
    if not hostname or "example.com" in hostname:
        sys.exit("MLFLOW_HOSTNAME is unset or still the placeholder in env/server.env")

    # Checked before anything is created remotely. A service token's secret is
    # returned once, so a local failure *after* the POST strands a token whose
    # secret is gone for good and has to be cleaned up by hand.
    bundle = REPO / "env" / "nodes" / f"{node}.env"
    if not bundle.exists():
        sys.exit(f"no bundle for '{node}' — run: make node-provision NODE={node}")

    cf = CF(api_token, account_id(env))
    acct = cf.account

    # --- 1. the service token ---------------------------------------------
    existing = {t["name"]: t for t in cf("GET", f"/accounts/{acct}/access/service_tokens")}
    if node in existing:
        # The secret is unrecoverable, so this cannot be quietly "fixed" —
        # reissuing means revoking the old one, which breaks a running node.
        sys.exit(
            f"a service token named '{node}' already exists "
            f"(id {existing[node]['id']}).\n"
            "  Its secret cannot be re-read. To reissue, delete it in the "
            "dashboard first:\n"
            "  Zero Trust > Access > Service Auth"
        )

    token = cf("POST", f"/accounts/{acct}/access/service_tokens", {"name": node})
    print(f"  created service token '{node}' (id {token['id']})")

    path = write_bundle(node, token["client_id"], token["client_secret"])
    print(f"  wrote both halves into {path.relative_to(REPO)}")

    # --- 2. the Service Auth policy ---------------------------------------
    apps = cf("GET", f"/accounts/{acct}/access/apps")
    app = next((a for a in apps if a.get("domain", "").rstrip("/") == hostname), None)
    if app is None:
        known = ", ".join(a.get("domain", "?") for a in apps) or "(none)"
        sys.exit(
            f"no Access application matches {hostname} — found: {known}\n"
            "  Create one: Zero Trust > Access > Applications > Self-hosted"
        )

    policies = cf("GET", f"/accounts/{acct}/access/apps/{app['id']}/policies")
    policy = next((p for p in policies if p.get("name") == POLICY_NAME), None)
    rule = {"service_token": {"token_id": token["id"]}}

    if policy is None:
        cf(
            "POST",
            f"/accounts/{acct}/access/apps/{app['id']}/policies",
            # non_identity is what the dashboard calls "Service Auth": it
            # authenticates a machine, so it must never carry an email rule.
            {"name": POLICY_NAME, "decision": "non_identity", "include": [rule]},
        )
        print(f"  created Service Auth policy '{POLICY_NAME}' on '{app['name']}'")
    else:
        include = list(policy.get("include") or [])
        include.append(rule)
        cf(
            "PUT",
            f"/accounts/{acct}/access/apps/{app['id']}/policies/{policy['id']}",
            {"name": POLICY_NAME, "decision": "non_identity", "include": include},
        )
        print(f"  added '{node}' to existing policy '{POLICY_NAME}' ({len(include)} tokens)")

    print(f"\n'{node}' can now reach {hostname}. Verify:")
    print(f"  make test-tunnel NODE={node}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
