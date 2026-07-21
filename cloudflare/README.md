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

---

## 1. Create the tunnel

Zero Trust dashboard → **Networks → Tunnels → Create a tunnel** → `cloudflared`.

Name it (e.g. `rk-mlflow`), then copy the **install token** — the long string
in the `cloudflared service install <TOKEN>` command. Put it in `.env`:

```
CLOUDFLARE_TUNNEL_TOKEN=<token>
```

That token alone authorizes an outbound tunnel for your account. Treat it like
a password; `.env` is gitignored and chmod 600.

## 2. Point it at MLflow — and only MLflow

On the tunnel's **Public Hostname** tab, add exactly one route:

| Field | Value |
|---|---|
| Subdomain | `mlflow` |
| Domain | your domain |
| Service | `HTTP` → `mlflow:5000` |

> If the host's Docker bridge cannot reach the internet, `cloudflared` runs
> with `network_mode: host` instead and this becomes `http://127.0.0.1:5000`.
> Check `compose.yaml` for which applies.

**Do not** add a hostname for Studio (`kong:8000`). Studio is an admin console
holding database credentials; it stays on loopback and is reached with
`ssh -L 8000:localhost:8000 ml-login`. MLflow is the visualization surface.

Then set the hostname in `.env` so `make health` can verify it:

```
MLFLOW_HOSTNAME=mlflow.<your-domain>
```

## 3. Gate it with Access

**Access → Applications → Add an application → Self-hosted**, matching
`mlflow.<your-domain>`.

Add a policy for humans:

- Action **Allow**, e.g. include *Emails ending in* `@your-org.edu`.

Verify it took effect — an unauthenticated request must not return 200:

```bash
make health          # flags a bare 200 as "Access is NOT in front of it"
```

## 4. Issue a service token per node

Browser SSO cannot work for a training process, so each node gets its own
non-interactive credential.

**Access → Service Auth → Create service token**, one per node (`julius`,
`ml2`, `nl6`). Save each Client ID and Secret — the secret is shown once.

Then add a second policy to the MLflow application:

- Action **Service Auth**
- Include → *Service Token* → select all three.

Per-node tokens rather than one shared token: a compromised node is revoked
on its own, and the Access logs attribute traffic to a specific machine.

On each node:

```bash
export MLFLOW_TRACKING_URI="https://mlflow.<your-domain>"
export CF_ACCESS_CLIENT_ID="<node>.access"
export CF_ACCESS_CLIENT_SECRET="<secret>"
```

The `rk-mlflow-client` package registers an MLflow request-header provider
that attaches these to every REST call, so training code needs no changes.
Without it, MLflow receives an HTML login page and fails with a confusing
JSON parse error.

## 5. Verify

```bash
make up-tunnel
make health
```

From a node, after installing the client package:

```bash
python -c "import mlflow; mlflow.set_tracking_uri('https://mlflow.<domain>'); \
           print(mlflow.search_experiments())"
```

An HTML page or a 302 in the error means the service token is not attached or
the Service Auth policy is missing.
