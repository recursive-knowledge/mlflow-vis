# Handoff: a second MLflow hostname with MLflow basic auth (no Cloudflare Access)

Written 2026-10-06 for the agent that implements it. Read `cloudflare/README.md`
first. This document extends it and does not replace it.

## Why

The datasmith repo (`/mnt/sdd1/atharvas/formulacode/datasmith_new` on the build
host) logs stage 6 agent runs to experiment `datasmith-stage6` (id 23). It wants
MLflow's **native** Codex tracing, the `@mlflow/codex` npm plugin. MLflow
recommends it for conversation traces:
<https://mlflow.org/docs/latest/genai/tracing/integrations/listing/codex/>

The plugin's Node client (`@mlflow/core`) can authenticate only with
`MLFLOW_TRACKING_TOKEN` (`Authorization: Bearer ...`) or
`MLFLOW_TRACKING_USERNAME`/`PASSWORD` (HTTP Basic). It cannot send
`CF-Access-Client-Id`/`-Secret`, so it cannot pass the Access application on
`mlflow.formulacode.org`. Cloudflare's single-header service-token mode does not
help either, because it needs the raw JSON value
`{"cf-access-client-id": ..., "cf-access-client-secret": ...}` in
`Authorization`, and the client always sends `Bearer <token>`.

The fix uses MLflow's own auth on a separate hostname. Both the Python and Node
clients support it natively, with no custom code:
<https://mlflow.org/docs/latest/self-hosting/security/basic-http-auth/>

## Target state

| | Existing (unchanged) | New |
|---|---|---|
| Hostname | `mlflow.formulacode.org` | `mlflow-api.formulacode.org` (any name is fine; record it) |
| Gate | Cloudflare Access (SSO + service tokens) | **No Access gate.** MLflow basic auth (`--app-name basic-auth`) |
| Container | `rk-mlflow` (`mlflow:5000`) | new `rk-mlflow-auth` (`mlflow-auth:5000`) |
| Backend store and artifacts | Postgres `mlflow` DB, S3 bucket | **The same** store and bucket, so both hostnames show the same data |
| Users | verl nodes, teammates | the `datasmith` user (and any other non-browser client later) |

MLflow tracking servers are stateless over a shared SQL backend, so a second
server process on the same `--backend-store-uri` and `--artifacts-destination`
is supported. Basic auth applies only on the server that runs
`--app-name basic-auth`. The existing server and its users are not affected.

## Hard constraints

- **Do not touch** the existing `mlflow` service, the existing Access
  application, its policies, or the existing tunnel route. The verl nodes
  depend on them. `make down`, `docker compose down`, `docker volume prune`
  and a Docker restart are all forbidden.
- **Do not add a Bypass policy to the existing hostname** (README section 4
  explains why: `/api/*` is full read and write).
- Recreating `rk-mlflow` restarts the server everyone uses. Build the new image
  and start **only** the new service (`docker compose up -d --no-deps mlflow-auth`).
  Leave `rk-mlflow` running on its current image.
- Edits to `env/server.env` need the user's approval (they are listed as
  destructive in the datasmith skill). Ask before you write to it.
- Never print or commit secrets. `env/server.env` and `env/nodes/*` are
  gitignored; keep every new secret in gitignored files.
- `/overflow` is shared and was 100% full on 2026-10-05. Check `df -h /overflow`
  before you build.

## Steps

### 1. Server image: add the auth extra

The current image lacks the auth dependencies. This was checked:
`docker exec rk-mlflow python -c "import mlflow.server.auth"` raises
`ImportError: The MLflow basic auth app requires the Flask-WTF package`.

- In `pyproject.toml`, change `"mlflow==3.14.0"` to `"mlflow[auth]==3.14.0"`, then run `uv lock`.
- Build the image (`docker compose build mlflow-auth` once the service exists,
  or `docker compose build mlflow`) **without** recreating `rk-mlflow`.

### 2. Auth database and secrets

- Create a separate database for users and permissions in the same Postgres,
  for example `mlflow_auth`, owned by the `mlflow` role (`make psql DB=postgres`).
  The auth schema migrates itself on startup, using its own
  `alembic_version_auth` table.
- Generate three secrets and add them to `env/server.env` (with approval).
  `scripts/gen-secrets.py` shows how this repo generates them.
  - `MLFLOW_FLASK_SERVER_SECRET_KEY`: the CSRF/session key. It must be the
    same on every basic-auth server.
  - `MLFLOW_AUTH_ADMIN_PASSWORD`: at least 12 characters. It is used only when
    the admin user is first created; unset it afterwards.
  - `MLFLOW_API_HOSTNAME=mlflow-api.formulacode.org`.
- Add an auth config file, for example `docker/mlflow/basic_auth.ini`, with the
  password passed in by environment, not committed:

  ```ini
  [mlflow]
  default_permission = NO_PERMISSIONS
  database_uri = postgresql://mlflow:<from env>@db:5432/mlflow_auth
  admin_username = admin
  ```

  `NO_PERMISSIONS` as the default means the `datasmith` user sees only what it
  is granted. MLflow's own default is `READ` on everything.

### 3. Compose service `mlflow-auth`

Copy the `mlflow` service and change only what follows:

- `container_name: rk-mlflow-auth`
- Environment: add `MLFLOW_AUTH_CONFIG_PATH`, `MLFLOW_FLASK_SERVER_SECRET_KEY`
  and `MLFLOW_AUTH_ADMIN_PASSWORD`. Keep the same S3 variables.
- Command: the same backend store and artifact flags, plus `--app-name basic-auth`.
  `--allowed-hosts` and `--cors-allowed-origins` must contain the **new**
  hostname: the README's "three variables" rule applies here too.
- Ports: loopback `127.0.0.1:5001:5000`, or no published port.
- **Verify early** that `--app-name basic-auth` starts under the default
  uvicorn server with `--allowed-hosts` and `--workers`. The current service
  avoids gunicorn because of those flags. If basic auth refuses uvicorn, stop
  and report back; do not change the existing service to work around it.

### 4. Tunnel route

Zero Trust → Networks → Tunnels → this tunnel → **Public Hostname** → add:

| Field | Value |
|---|---|
| Subdomain | `mlflow-api` |
| Service | `HTTP` → `mlflow-auth:5000` |

This is the same token-based tunnel; there is no second `cloudflared`.

### 5. Access: make sure nothing gates the new hostname

- List the account's Access applications (API:
  `GET /accounts/{account_id}/access/apps`, using `CLOUDFLARE_API_TOKEN`, as
  `scripts/node-token.py` does). Check that no application matches
  `mlflow-api.formulacode.org`, including wildcards such as `*.formulacode.org`.
- If a wildcard matches, create a self-hosted application for exactly
  `mlflow-api.formulacode.org` with **one** policy: action **Bypass**,
  Include → **Everyone**. A more specific application takes precedence over
  the wildcard. Bypass skips Access for this hostname only; MLflow basic auth
  is the gate.
- Add a WAF **rate-limiting rule** for `mlflow-api.formulacode.org`, for example
  login-failure-heavy paths at 60 requests per minute per IP. Basic auth is
  open to brute force once Access is gone. Record what you configured.

### 6. The `datasmith` user

Use MLflow's auth client, through loopback or the new hostname, as admin:

```python
from mlflow.server import get_app_client
auth = get_app_client("basic-auth", tracking_uri="http://127.0.0.1:5001/")
auth.create_user(username="datasmith", password="<generated, >= 12 chars>")
auth.grant_user_permission(username="datasmith", resource_type="experiment", resource_id="23", permission="EDIT")
```

Put the credentials for the user in a gitignored file, for example
`env/nodes/datasmith-basic.env`:

```
MLFLOW_TRACKING_URI=https://mlflow-api.formulacode.org
MLFLOW_TRACKING_USERNAME=datasmith
MLFLOW_TRACKING_PASSWORD=<...>
```

The datasmith side copies these into its `tokens.env`. Do not paste them into
chat or logs.

## Verify (all must pass)

```bash
H=https://mlflow-api.formulacode.org
curl -s -o /dev/null -w '%{http_code}\n' $H/api/2.0/mlflow/experiments/get?experiment_id=23          # 401: no credentials
curl -s -o /dev/null -w '%{http_code}\n' -u datasmith:$PW $H/api/2.0/mlflow/experiments/get?experiment_id=23   # 200
curl -s -o /dev/null -w '%{http_code}\n' -u datasmith:$PW $H/api/2.0/mlflow/experiments/get?experiment_id=<another id>  # 403: not granted
curl -s -u datasmith:$PW -X POST -H 'Content-Type: application/json' \
  -d '{"locations":[{"type":"MLFLOW_EXPERIMENT","mlflow_experiment":{"experiment_id":"23"}}],"max_results":1}' \
  $H/api/3.0/mlflow/traces/search                                                  # 200 with traces: v3 trace API works under auth
make health                                                                         # the existing stack is unchanged
make test-tunnel NODE=<an existing node>                                            # the existing service-token path still works
```

Also check that one trace can be **written** with the Node client: `npm i -g
@mlflow/codex@0.4.0`; set `MLFLOW_TRACKING_URI`, `MLFLOW_TRACKING_USERNAME`,
`MLFLOW_TRACKING_PASSWORD` and `MLFLOW_EXPERIMENT_ID=23`; run one
`codex exec --json -c 'notify=[...]' "reply OK"` **without** `--ephemeral`;
then confirm the trace appears in experiment 23. `mlflow-codex setup` shows the
exact `notify` value it writes into `config.toml`.

## Hand back

- the hostname
- where the `datasmith` credentials file is
- the Access/WAF objects you created (names and IDs)
- the compose diff
- the verification output (no secrets)
- anything that did not behave as described here, especially basic auth under uvicorn

## Rollback

Stop and remove only `rk-mlflow-auth`. Delete the `mlflow-api` public hostname
route, the Bypass application if you made one, and the WAF rule. The
`mlflow_auth` database can stay; it holds no tracking data.
