# Cloudflare: tunnel + Access

The tunnel is the only public surface in this stack. It carries **UI and API
JSON only** — never checkpoint bytes. Cloudflare proxies cap request bodies at
100 MB and checkpoints are gigabytes, so bulk transfer goes over SSH/rsync on
the private network instead. Keep it that way.

Three different things authenticate through it:

| Who | How | What they get |
|---|---|---|
| Teammates | SSO in a browser | The MLflow UI |
| verl nodes | Access **service token** | The MLflow REST API |
| Clients that cannot do Access | MLflow **basic auth**, on a second hostname | One or more named experiments (§6) |

Everything below is configured in `env/server.env` — there is no `.env` in
this repo. That file is gitignored and `chmod 600`; the Makefile passes it to
compose with `--env-file`.

---

## 1. Create the tunnel

Zero Trust dashboard → **Networks → Tunnels → Create a tunnel** → `cloudflared`.

The name is only a dashboard label — nothing in this repo references it. What
matters is the **install token**: the long string in the
`cloudflared service install <TOKEN>` command. Put it in `env/server.env`:

```
CLOUDFLARE_TUNNEL_TOKEN=<token>
```

That token alone authorizes an outbound tunnel for your account. Treat it like
a password.

## 2. Point it at MLflow — and only MLflow

On the tunnel's **Public Hostname** tab, add exactly one route:

| Field | Value |
|---|---|
| Subdomain | `mlflow` |
| Domain | your domain |
| Path | *(empty)* |
| Service | `HTTP` → `mlflow:5000` |

`mlflow:5000` is a Docker service name, not an address — `cloudflared` shares
the `rk` bridge network with the tracking server, so Docker's DNS resolves it.
Adding the hostname here also creates the proxied DNS record for you.

> If the host's Docker bridge cannot reach the internet, `cloudflared` runs
> with `network_mode: host` instead and this becomes `http://127.0.0.1:5000`.
> Check `compose.yaml` for which applies.

**Do not** add a hostname for Studio (`kong:8000`). Studio is an admin console
holding database credentials; it stays on loopback and is reached with
`ssh -L 8000:localhost:8000 ml-login`. MLflow is the visualization surface.

### Then set three variables, not one

```
MLFLOW_HOSTNAME=mlflow.<your-domain>
MLFLOW_ALLOWED_HOSTS=localhost,localhost:5000,127.0.0.1,127.0.0.1:5000,mlflow.<your-domain>
MLFLOW_CORS_ORIGINS=http://localhost:5000,http://127.0.0.1:5000,https://mlflow.<your-domain>
```

**All three must carry the hostname.** MLflow 3's DNS-rebinding guard returns
`403 Invalid Host header` for anything not in `MLFLOW_ALLOWED_HOSTS` — but
`/health` keeps returning 200, so `make health` looks green while the UI and
every node call fail. Host matching includes the port, hence both forms.

These are command-line flags, so changing them requires recreating the
container rather than restarting it. `make up-tunnel` does that.

## 3. Gate it with Access

**Access → Applications → Add an application → Self-hosted**, matching
`mlflow.<your-domain>`.

Add a policy for humans:

- Name `team`, action **Allow**, Include → *Emails ending in* `@your-org.edu`

With no identity provider configured, Access falls back to **One-time PIN**:
the visitor enters an email, Cloudflare mails a 6-digit code, and a match sets
a session cookie. Confirm it is enabled under **Settings → Authentication →
Login methods**. No mail infrastructure of your own is involved.

> Scope this deliberately. *Emails ending in* a university domain admits
> everyone at that institution, self-service, with no approval step — and
> **MLflow has no user model**, so anyone past Access has read *and write* on
> every experiment. There is no read-only tier behind the door. An explicit
> list of addresses is easy to widen later; a domain-wide policy is awkward to
> narrow once people have bookmarked the URL.

Verify it took effect — an unauthenticated request must not return 200:

```bash
make health          # flags a bare 200 as "Access is NOT in front of it"
```

## 4. Issue a service token per node

Browser SSO cannot work for a training process, so each node gets its own
non-interactive credential. Access gates by **hostname, not by path** — the UI
and the REST API are the same origin — so nodes need a credential rather than
an exemption. Do not add a Bypass policy for `/api/*`: that endpoint is full
read *and* write, including `DELETE` on runs and experiments.

```bash
make node-provision NODE=julius    # bundle + SSH key
make node-token     NODE=julius    # service token, straight into the bundle
```

`make node-token` creates the token, writes both halves into
`env/nodes/julius.env`, and adds it to the application's `nodes` Service Auth
policy (creating that policy on first use). This is automated for a reason:
**the client secret is returned exactly once**, in the creation response, and
can never be read again.

It needs `CLOUDFLARE_API_TOKEN` in `env/server.env`, created at
[dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens)
with two **account**-scoped permissions:

- Access: Service Tokens → **Edit**
- Access: Apps and Policies → **Edit**

> The credential `cloudflared tunnel login` writes to `~/.cloudflared/cert.pem`
> is **not** sufficient. It can read and write tunnel configuration and DNS,
> but returns 403 on every Access endpoint — Access is a separate permission
> domain.

<details>
<summary>Doing it by hand instead</summary>

**Access → Service Auth → Create service token**, one per node. Save each
Client ID and Secret — the secret is shown once. Paste both into
`env/nodes/<node>.env`. Then add a second policy to the MLflow application:
action **Service Auth**, Include → *Service Token* → select them.
</details>

Per-node tokens rather than one shared token: a compromised node is revoked on
its own, and the Access logs attribute traffic to a specific machine.

On each node:

```bash
source julius.env
uv pip install 'git+ssh://git@github.com/recursive-knowledge/mlflow-vis.git#subdirectory=client'
```

The `rk-mlflow-client` package registers an MLflow request-header provider
that attaches `CF-Access-Client-Id` / `CF-Access-Client-Secret` to every REST
call, so training code needs no changes. Without it, MLflow receives an HTML
login page and fails with a confusing JSON parse error.

## 5. Verify

```bash
make up-tunnel
make health
make test-tunnel NODE=julius
```

`make test-tunnel` is the one that matters: it confirms an unauthenticated
request is *blocked*, that the service token is *accepted*, and then logs
metrics and an artifact exactly as verl would.

---

## 6. A second hostname for clients that cannot do Access

Some clients can only send `Authorization: Bearer …` or HTTP Basic. The
`@mlflow/codex` Node plugin is the one that forced this: its `@mlflow/core`
client has no way to attach `CF-Access-Client-Id` / `-Secret`, so it cannot pass
the Access application no matter how the token is issued. Cloudflare's
single-header service-token mode does not rescue it either — that wants the raw
JSON `{"cf-access-client-id": …, "cf-access-client-secret": …}` in
`Authorization`, and the client always sends `Bearer <token>`.

The answer is **not** a Bypass policy on `mlflow.<domain>`: §4 explains why —
`/api/*` is full read *and* write, including `DELETE`. Instead there is a second
tracking server with a real user model, on its own hostname:

| | `mlflow.<domain>` | `mlflow-api.<domain>` |
|---|---|---|
| Gate | Cloudflare Access | MLflow basic auth (`--app-name basic-auth`) |
| Container | `rk-mlflow` | `rk-mlflow-auth` (compose profile `auth`) |
| Store + bucket | **the same** | **the same** |
| Users | teammates, verl nodes | one MLflow user per client |

Tracking servers are stateless over the shared SQL backend, so both processes
serve the same runs; `--app-name basic-auth` applies only to the second. Users
and permissions live in their own `mlflow_auth` database, which holds no
tracking data.

```bash
make up-auth                  # start rk-mlflow-auth (loopback 5001)
make auth-hostname            # show the edge plan: route, DNS, rate limit
make auth-hostname APPLY=1    # make those changes
```

### Creating a user

There is deliberately no script for this — one user is expected, and the admin
API is three calls. As admin (password in `MLFLOW_AUTH_ADMIN_PASSWORD`), against
loopback so the tunnel need not be up:

```bash
A=admin:$MLFLOW_AUTH_ADMIN_PASSWORD
H=http://127.0.0.1:5001

# 1. the user (password >= 12 chars; generate, do not invent)
PW=$(python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(32)))')
curl -s -u $A -X POST -H 'Content-Type: application/json' \
  -d "{\"username\":\"datasmith\",\"password\":\"$PW\"}" \
  $H/api/2.0/mlflow/users/create

# 2. one experiment, explicitly — default_permission is NO_PERMISSIONS
curl -s -u $A -X POST -H 'Content-Type: application/json' \
  -d '{"username":"datasmith","resource_type":"experiment","resource_id":"23","permission":"EDIT"}' \
  $H/api/3.0/mlflow/users/permissions/grant

# 3. what it actually has (explicit rows, not the resolved default)
curl -s -u $A "$H/api/3.0/mlflow/users/permissions/list?username=datasmith"
```

Two traps worth knowing. `permissions/grant` is **not** an upsert — it returns
`RESOURCE_ALREADY_EXISTS`, and there is no update route, so changing a grant is
`permissions/revoke` then `grant`. And `permissions/get` reports the *effective*
permission, answering `200 {"permission":"NO_PERMISSIONS"}` for a user with no
row at all; only `permissions/list` shows real grants. To rotate a password,
`PATCH /api/2.0/mlflow/users/update-password` with `{username, password}`.

Keep the credentials in `env/nodes/<user>-basic.env` (gitignored, `chmod 600`),
holding the three variables both the Python and the Node client read natively:

```
MLFLOW_TRACKING_URI=https://mlflow-api.<domain>
MLFLOW_TRACKING_USERNAME=<user>
MLFLOW_TRACKING_PASSWORD=<generated>
```

No `rk-mlflow-client` and no request-header provider — that package exists only
to attach the `CF-Access-*` headers, which this hostname neither needs nor
accepts. Do not put `CF_ACCESS_*` in this bundle.

### What keeps it safe without Access in front

`default_permission = NO_PERMISSIONS`, not MLflow's own `READ` default. A new
user can reach nothing until it is granted a specific experiment, so a leaked
credential exposes one experiment rather than the whole store. Only `/health`,
`/static` and `/favicon.ico` answer without credentials; `/signup` and
user-creation require an authenticated workspace admin, so there is no
self-registration.

`make auth-hostname` also refuses to run if any Access application matches the
hostname — including a wildcard — because a login page in front of a Basic-only
client fails in a way that is tedious to diagnose. And since Access is no longer
absorbing credential-guessing traffic, it adds a rate-limiting rule.

> Rate limiting outside Enterprise is narrower than it looks. The period must be
> **10 seconds** (`not entitled to use the period 60`), `mitigation_timeout` must
> **equal** the period, and `counting_expression` — which would let the rule
> count only 401s and so never touch a legitimate client — needs a paid Advanced
> Rate Limiting plan. The script tries the 401-counting rule first and falls back
> to 100 requests per 10 s per IP. Override with `RATELIMIT_REQUESTS`.

### Verifying

```bash
# grep, not a bare sed: the file has comment lines, and `export # …` turns into
# a bare `export` that dumps the whole environment.
eval "$(grep '^MLFLOW_' env/nodes/datasmith-basic.env | sed 's/^/export /')"
H=$MLFLOW_TRACKING_URI
U=$MLFLOW_TRACKING_USERNAME:$MLFLOW_TRACKING_PASSWORD
E=$H/api/2.0/mlflow/experiments/get

curl -so /dev/null -w '%{http_code} want 200\n' $H/health              # open by design
curl -so /dev/null -w '%{http_code} want 401\n' "$E?experiment_id=23"  # no credentials
curl -so /dev/null -w '%{http_code} want 200\n' -u $U "$E?experiment_id=23"
curl -so /dev/null -w '%{http_code} want 403\n' -u $U "$E?experiment_id=0"   # not granted
curl -so /dev/null -w '%{http_code} want 200\n' -u $U -X POST \
  -H 'Content-Type: application/json' \
  -d '{"locations":[{"type":"MLFLOW_EXPERIMENT","mlflow_experiment":{"experiment_id":"23"}}],"max_results":1}' \
  $H/api/3.0/mlflow/traces/search                       # the v3 trace API under auth
```

A **401** for the anonymous call is the one that matters: 200 means the backend
store is open to the internet, and 403 means the Host guard rejected the request
before auth ran (hostname missing from `MLFLOW_AUTH_ALLOWED_HOSTS`).

### Rotating and removing

Editing `MLFLOW_AUTH_ADMIN_PASSWORD` afterwards does **not** rotate the admin
account — `create_admin_user()` runs only when the user is absent; use the
`users/update-password` API. To remove the hostname entirely: stop and remove
`rk-mlflow-auth`, delete the `mlflow-api` ingress rule, its DNS record and the
rate-limiting rule. The `mlflow_auth` database can stay.

---

## When it does not work

| Symptom | Cause |
|---|---|
| `403 Invalid Host header` | Hostname missing from `MLFLOW_ALLOWED_HOSTS`, or the container was restarted rather than recreated |
| Bare request returns **200** | No Access application matches the hostname — MLflow is open to the internet |
| Bare request returns **404** | The tunnel catch-all fired: the ingress hostname does not match what the edge received |
| **1033** in the browser | DNS record exists but the tunnel has no ingress rule, or `cloudflared` is not running |
| Node gets HTML / a JSON parse error | Service token missing, not in a Service Auth policy, or `rk-mlflow-client` not installed |
| `make health` says `HTTP 000` right after setup | Your resolver cached the NXDOMAIN from before the record existed. Cloudflare's SOA sets a 30-minute negative TTL; `dig @1.1.1.1` confirms the record is live |
| `403 Invalid Host header` on `127.0.0.1:5001` | `MLFLOW_AUTH_ALLOWED_HOSTS` must list the **published** port (5001), not the container's internal 5000. Matching is an exact string compare, with no port stripping |
| Basic-auth API returns **403**, not 401 | The Host guard rejected the request before auth ran — the hostname is missing from `MLFLOW_AUTH_ALLOWED_HOSTS` |
| Basic-auth API returns **200** with no credentials | `--app-name basic-auth` is not actually on the container. `make logs S=mlflow-auth` |
| A granted experiment still returns **403** | `default_permission` is `NO_PERMISSIONS`; grant it explicitly via `permissions/grant` (§6) |
| `rk-mlflow-auth` restart-loops on `Invalid placeholder in string` | `docker/mlflow/basic_auth.ini.template` contains a literal `$`. `string.Template` reads every one as a placeholder |

If the hostname works in one browser but not another, it is client-side —
check secure DNS (DoH), IPv6 reachability, and extensions. Cloudflare serves
every client the same records; `curl` from the server settles it.
