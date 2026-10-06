"""Configure the edge for the basic-auth hostname: tunnel route, DNS, WAF.

The counterpart of the manual dashboard steps in cloudflare/README.md, for the
hostname that has NO Access application in front of it. Three things have to be
true for mlflow-api to work and be safe:

  1. the tunnel serves it, routed to mlflow-auth:5000 (not mlflow:5000)
  2. a proxied DNS record points at the tunnel
  3. nothing gates it with Access, and a rate limit stands in for the
     brute-force protection Access would otherwise have provided

Dry run by default — it prints the plan and changes nothing. Apply with:

    make auth-hostname            # show the plan
    make auth-hostname APPLY=1    # make the changes

Verify afterwards with the curl checks in cloudflare/README.md section 6.

Re-running is safe: every step is checked before it is made, so an already
correct edge reports "unchanged" rather than creating a duplicate.

Needs CLOUDFLARE_INFRA_API_TOKEN in env/server.env, with three permissions the
Access-only CLOUDFLARE_API_TOKEN does not have:

    Account > Cloudflare Tunnel > Edit
    Zone    > DNS               > Edit     (the zone holding MLFLOW_API_HOSTNAME)
    Zone    > Zone WAF          > Edit     (same zone)

They are kept as a separate token on purpose: CLOUDFLARE_API_TOKEN is used by
`make node-token` routinely, and broadening it would mean a routinely-used
credential could also rewrite tunnel ingress and DNS.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
API = "https://api.cloudflare.com/client/v4"

ORIGIN = "http://mlflow-auth:5000"
RULE_DESCRIPTION = "rk-mlflow mlflow-api basic-auth brute-force guard"

GREEN, RED, YELLOW, BOLD, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"


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


def tunnel_id(env: dict[str, str]) -> tuple[str, str]:
    """(account id, tunnel id), read out of the tunnel token.

    The token is base64 JSON {"a": account, "t": tunnel, "s": secret}. Reading it
    means this script identifies the right tunnel even with a token that cannot
    list tunnels, and it can never pick the wrong one on an account with several.
    """
    token = env.get("CLOUDFLARE_TUNNEL_TOKEN", "")
    if not token:
        sys.exit("CLOUDFLARE_TUNNEL_TOKEN is not set in env/server.env")
    try:
        blob = json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))
        return blob["a"], blob["t"]
    except Exception as exc:
        sys.exit(f"cannot read the tunnel id out of CLOUDFLARE_TUNNEL_TOKEN: {exc}")


class CF:
    def __init__(self, token: str, label: str) -> None:
        self.label = label
        self.s = requests.Session()
        self.s.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    def __call__(self, method: str, path: str, body: dict | None = None, *, allow_fail: bool = False):
        r = self.s.request(method, f"{API}{path}", json=body, timeout=30)
        try:
            data = r.json()
        except ValueError:
            sys.exit(f"unparseable response from Cloudflare (HTTP {r.status_code}) on {path}")
        if not data.get("success"):
            errs = "; ".join(
                f"{e.get('code')}: {e.get('message')}" for e in data.get("errors") or []
            )
            if allow_fail:
                return None, errs
            if r.status_code == 403:
                errs += f"  — is {self.label} missing a permission? See the docstring."
            sys.exit(f"Cloudflare API error (HTTP {r.status_code}) on {method} {path}: {errs}")
        return data["result"], None


def main() -> int:
    apply = os.environ.get("APPLY") == "1"
    failed: list[str] = []
    env = load_env()

    hostname = env.get("MLFLOW_API_HOSTNAME", "")
    if not hostname or "example.com" in hostname:
        sys.exit("MLFLOW_API_HOSTNAME is unset or still the placeholder in env/server.env")
    subdomain, _, zone_name = hostname.partition(".")

    infra_token = env.get("CLOUDFLARE_INFRA_API_TOKEN", "")
    if not infra_token:
        sys.exit(
            "CLOUDFLARE_INFRA_API_TOKEN is not set in env/server.env.\n"
            "  Create one at https://dash.cloudflare.com/profile/api-tokens with:\n"
            "    Account > Cloudflare Tunnel > Edit\n"
            f"    Zone    > DNS               > Edit   (zone: {zone_name})\n"
            f"    Zone    > Zone WAF          > Edit   (zone: {zone_name})"
        )
    access_token = env.get("CLOUDFLARE_API_TOKEN", "")

    account, tunnel = tunnel_id(env)
    cf = CF(infra_token, "CLOUDFLARE_INFRA_API_TOKEN")

    print(f"\n{BOLD}edge configuration for {hostname}{OFF}")
    print(f"  tunnel {tunnel}  account {account}")
    print(f"  mode: {'APPLY — changes will be made' if apply else 'dry run — nothing will change'}\n")
    planned: list[str] = []

    # --- 0. nothing may gate this hostname with Access -----------------------
    # Needs the Access-scoped token, not the infra one. A wildcard application
    # matching *.<zone> would silently put a login page in front of a client
    # that can only send HTTP Basic.
    if access_token:
        acf = CF(access_token, "CLOUDFLARE_API_TOKEN")
        apps, _ = acf("GET", f"/accounts/{account}/access/apps")
        matches = []
        for app in apps:
            domains = [app.get("domain", "")] + [
                d.get("uri", "") for d in (app.get("destinations") or [])
            ]
            for d in domains:
                d = (d or "").rstrip("/")
                if not d:
                    continue
                if d == hostname or (d.startswith("*.") and hostname.endswith(d[1:])):
                    matches.append((app.get("name"), app.get("id"), d))
        if matches:
            print(f"  {RED}✗{OFF} an Access application matches {hostname}:")
            for name, app_id, d in matches:
                print(f"      {name!r} (id {app_id}) via {d!r}")
            print("    A client that can only send HTTP Basic cannot pass Access.")
            print("    Either narrow that application, or add a self-hosted application for")
            print(f"    exactly {hostname} with one Bypass/Everyone policy, which takes")
            print("    precedence over a wildcard. See the handoff, step 5.")
            return 1
        print(f"  {GREEN}✓{OFF} no Access application matches {hostname} "
              f"({len(apps)} application(s) checked)")
    else:
        print(f"  {YELLOW}!{OFF} CLOUDFLARE_API_TOKEN unset — cannot verify that no Access "
              "application matches. Check by hand.")

    # --- 1. tunnel ingress ---------------------------------------------------
    result, _ = cf("GET", f"/accounts/{account}/cfd_tunnel/{tunnel}/configurations")
    config = (result or {}).get("config") or {}
    ingress = list(config.get("ingress") or [])
    if not ingress:
        sys.exit(
            "the tunnel has no ingress configuration to extend — is it a locally\n"
            "  managed tunnel (config.yml) rather than a dashboard-managed one?"
        )

    existing = {r.get("hostname"): r.get("service") for r in ingress if r.get("hostname")}
    print(f"  current ingress: {len(ingress)} rule(s)")
    for r in ingress:
        print(f"      {r.get('hostname') or '(catch-all)':42} -> {r.get('service')}")

    # Refuse to touch a tunnel that does not look like the one we expect.
    mlflow_host = env.get("MLFLOW_HOSTNAME", "")
    if mlflow_host and mlflow_host not in existing:
        sys.exit(
            f"\n  refusing to edit: this tunnel does not route {mlflow_host}, so it is not\n"
            "  the tunnel this repo manages. Check CLOUDFLARE_TUNNEL_TOKEN."
        )

    if existing.get(hostname) == ORIGIN:
        print(f"  {GREEN}✓{OFF} tunnel already routes {hostname} -> {ORIGIN}")
    else:
        if hostname in existing:
            planned.append(f"repoint tunnel route {hostname}: {existing[hostname]} -> {ORIGIN}")
            new_ingress = [
                {**r, "service": ORIGIN} if r.get("hostname") == hostname else r for r in ingress
            ]
        else:
            planned.append(f"add tunnel route {hostname} -> {ORIGIN}")
            # Insert before the trailing catch-all (the one rule with no
            # hostname); appending after it would make the new rule unreachable.
            rule = {"hostname": hostname, "service": ORIGIN}
            if ingress[-1].get("hostname"):
                new_ingress = ingress + [rule]
            else:
                new_ingress = ingress[:-1] + [rule, ingress[-1]]
        if apply:
            cf("PUT", f"/accounts/{account}/cfd_tunnel/{tunnel}/configurations",
               {"config": {**config, "ingress": new_ingress}})
            print(f"  {GREEN}✓{OFF} tunnel ingress updated ({len(new_ingress)} rules)")

    # --- 2. DNS ---------------------------------------------------------------
    zones, _ = cf("GET", f"/zones?name={zone_name}")
    if not zones:
        sys.exit(f"no zone named {zone_name} is visible to CLOUDFLARE_INFRA_API_TOKEN")
    zone = zones[0]["id"]

    target = f"{tunnel}.cfargotunnel.com"
    records, _ = cf("GET", f"/zones/{zone}/dns_records?name={hostname}")
    record = next((r for r in records if r.get("name") == hostname), None)
    if record and record.get("content") == target and record.get("proxied"):
        print(f"  {GREEN}✓{OFF} DNS already: {hostname} CNAME {target} (proxied)")
    elif record:
        planned.append(
            f"update DNS {hostname}: {record.get('type')} {record.get('content')} "
            f"(proxied={record.get('proxied')}) -> CNAME {target} (proxied)"
        )
        if apply:
            cf("PATCH", f"/zones/{zone}/dns_records/{record['id']}",
               {"type": "CNAME", "name": hostname, "content": target, "proxied": True})
            print(f"  {GREEN}✓{OFF} DNS record updated")
    else:
        planned.append(f"create DNS {hostname} CNAME {target} (proxied)")
        if apply:
            cf("POST", f"/zones/{zone}/dns_records",
               {"type": "CNAME", "name": hostname, "content": target, "proxied": True,
                "comment": "rk-mlflow basic-auth hostname (mlflow-auth:5000)"})
            print(f"  {GREEN}✓{OFF} DNS record created")

    # --- 3. rate limiting -----------------------------------------------------
    # Access is not in front of this hostname, so HTTP Basic is exposed to
    # brute force. Preferred rule counts only 401s, which throttles guessing
    # without ever touching a legitimate client; counting_expression needs a
    # higher plan, so fall back to counting every request at a threshold loose
    # enough for real trace traffic.
    rs, err = cf("GET", f"/zones/{zone}/rulesets/phases/http_ratelimit/entrypoint",
                 allow_fail=True)
    if rs is None:
        ruleset_id, rules = None, []
        print(f"  (no existing http_ratelimit ruleset: {err})")
    else:
        ruleset_id, rules = rs.get("id"), list(rs.get("rules") or [])

    if any(r.get("description") == RULE_DESCRIPTION for r in rules):
        print(f"  {GREEN}✓{OFF} rate-limiting rule already present ({RULE_DESCRIPTION!r})")
    else:
        # period 10, not 60: outside Enterprise, Cloudflare rejects anything else
        # with "not entitled to use the period 60, can only use a period among
        # [10]". Thresholds below are therefore per 10 seconds.
        period = int(os.environ.get("RATELIMIT_PERIOD") or 10)
        precise = {
            "description": RULE_DESCRIPTION,
            "expression": f'(http.host eq "{hostname}")',
            "action": "block",
            "ratelimit": {
                "characteristics": ["ip.src", "cf.colo.id"],
                "period": period,
                # ~30/min of wrong passwords from one IP. A correct client never
                # produces a 401, so this cannot throttle legitimate traffic.
                "requests_per_period": int(os.environ.get("RATELIMIT_FAILS") or 5),
                # Must equal period outside Enterprise: "not entitled to use a
                # mitigation timeout different from 10".
                "mitigation_timeout": period,
                "counting_expression": f'(http.host eq "{hostname}" and http.response.code eq 401)',
            },
        }
        loose = {
            "description": RULE_DESCRIPTION,
            "expression": f'(http.host eq "{hostname}")',
            "action": "block",
            "ratelimit": {
                "characteristics": ["ip.src", "cf.colo.id"],
                "period": period,
                # Counts every request, so this has to clear real trace bursts.
                "requests_per_period": int(os.environ.get("RATELIMIT_REQUESTS") or 100),
                "mitigation_timeout": period,
            },
        }
        planned.append(
            f"add rate-limiting rule on {hostname}: 20 failed logins/min per IP "
            "(fallback: 600 requests/min per IP)"
        )
        if apply:
            for attempt, rule in (("401-counting", precise), ("all-requests", loose)):
                body = {"rules": rules + [rule]}
                if ruleset_id:
                    out, err = cf("PUT", f"/zones/{zone}/rulesets/{ruleset_id}", body,
                                  allow_fail=True)
                else:
                    # The phase-entrypoint PUT accepts `rules` (and description)
                    # only. name/kind/phase belong to POST /zones/{z}/rulesets,
                    # and sending them here fails with
                    # `invalid JSON: unknown field "kind"`. Cloudflare creates
                    # the entrypoint ruleset implicitly on this call.
                    out, err = cf("PUT", f"/zones/{zone}/rulesets/phases/http_ratelimit/entrypoint",
                                  body, allow_fail=True)
                if out is not None:
                    rid = out.get("id", ruleset_id)
                    new = next((x for x in out.get("rules") or []
                                if x.get("description") == RULE_DESCRIPTION), {})
                    print(f"  {GREEN}✓{OFF} rate-limiting rule created ({attempt}) "
                          f"ruleset {rid} rule {new.get('id')}")
                    break
                print(f"  {YELLOW}!{OFF} {attempt} rule rejected: {err}")
            else:
                print(f"  {RED}✗{OFF} could not create a rate-limiting rule — configure one by "
                      f"hand for {hostname} (Security > WAF > Rate limiting rules)")
                failed.append("rate-limiting rule")

    # --- summary --------------------------------------------------------------
    if not planned:
        print(f"\n{GREEN}edge is already correct — nothing to do.{OFF}\n")
        return 0
    if apply:
        done = len(planned) - len(failed)
        print(f"\n{GREEN}applied {done} of {len(planned)} change(s).{OFF}")
        print("verify: see the curl checks in cloudflare/README.md section 6")
        if failed:
            print(f"{RED}still missing: {', '.join(failed)}{OFF}")
            print("see the hints above; this exits non-zero so it is not mistaken for success\n")
            return 1
        print()
    else:
        print(f"\n{BOLD}would make {len(planned)} change(s):{OFF}")
        for p in planned:
            print(f"  - {p}")
        print(f"\nre-run with APPLY=1 to make them:  make auth-hostname APPLY=1\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
