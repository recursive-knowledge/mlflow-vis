# Cloudflare: tunnel + Access

The tunnel is the only public surface in this stack. It carries **UI and API
JSON only** — never checkpoint bytes. Cloudflare proxies cap request bodies at
100 MB and checkpoints are gigabytes, so bulk transfer goes over SSH/rsync on
the private network instead. Keep it that way.

Two different things authenticate through it:

| Who | How | What they get |
|---|---|---|
| Teammates | SSO in a browser | The MLflow UI |
| verl nodes | Access **service token** | The MLflow REST API |

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

## When it does not work

| Symptom | Cause |
|---|---|
| `403 Invalid Host header` | Hostname missing from `MLFLOW_ALLOWED_HOSTS`, or the container was restarted rather than recreated |
| Bare request returns **200** | No Access application matches the hostname — MLflow is open to the internet |
| Bare request returns **404** | The tunnel catch-all fired: the ingress hostname does not match what the edge received |
| **1033** in the browser | DNS record exists but the tunnel has no ingress rule, or `cloudflared` is not running |
| Node gets HTML / a JSON parse error | Service token missing, not in a Service Auth policy, or `rk-mlflow-client` not installed |
| `make health` says `HTTP 000` right after setup | Your resolver cached the NXDOMAIN from before the record existed. Cloudflare's SOA sets a 30-minute negative TTL; `dig @1.1.1.1` confirms the record is live |

If the hostname works in one browser but not another, it is client-side —
check secure DNS (DoH), IPv6 reachability, and extensions. Cloudflare serves
every client the same records; `curl` from the server settles it.
