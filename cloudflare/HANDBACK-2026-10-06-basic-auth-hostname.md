# Handback: MLflow basic auth on a second hostname

Answers `HANDOFF-2026-10-06-basic-auth-hostname.md`. Written 2026-10-06.
Branch `feat/mlflow-basic-auth-hostname`, **not committed**.

**Status: LIVE and verified end to end at https://mlflow-api.formulacode.org.**

## Hand back

| | |
|---|---|
| Hostname | `mlflow-api.formulacode.org` (`MLFLOW_API_HOSTNAME` in `env/server.env`) |
| `datasmith` credentials | `env/nodes/datasmith-basic.env` — gitignored, `chmod 600` |
| Container | `rk-mlflow-auth`, image `rk-mlflow-mlflow-auth`, compose profile `auth`, loopback `127.0.0.1:5001` |
| Auth database | `mlflow_auth` in the same Postgres, owned by the `mlflow` role |
| Access objects created | **none, and none were needed** — see below |
| Tunnel route | `mlflow-api.formulacode.org` -> `http://mlflow-auth:5000`, tunnel `83ca2a04-b2c5-4f1c-a64e-5684368c914c` |
| DNS | `mlflow-api` CNAME `83ca2a04-….cfargotunnel.com`, proxied |
| WAF rate limit | ruleset `549862bc19d14269b857bfb9c2e2f341`, rule `934e613f9ba14101931d2a3b895aca7a` — 100 req / 10 s per IP, block 10 s |

### Access: nothing gates the new hostname

The account has exactly two Access applications, and neither matches:

| name | id | domain |
|---|---|---|
| `mlflow` | `cf64571e-6cd7-417f-9515-5cf3ac0555ae` | `mlflow.formulacode.org` |
| `db` | `6325b544-2edf-41ea-b0bb-f97524c2c4a1` | `db.formulacode.org` |

No wildcard application exists, so **no Bypass application was created** — step 5
of the handoff only called for one if a wildcard matched. `make auth-hostname`
re-checks this on every run and refuses to proceed if that ever changes.

The existing `mlflow` application, its policies and its service tokens were not
touched.

## The edge, as applied

`make auth-hostname APPLY=1` added one ingress rule before the catch-all, leaving
the existing `mlflow.formulacode.org` rule untouched:

```
mlflow.formulacode.org       -> http://mlflow:5000
mlflow-api.formulacode.org   -> http://mlflow-auth:5000     <- added
(catch-all)                  -> http_status:404
```

This needed a second API token, `CLOUDFLARE_INFRA_API_TOKEN` (Account >
Cloudflare Tunnel > Edit; Zone > DNS > Edit; Zone > Zone WAF > Edit), because
`CLOUDFLARE_API_TOKEN` is Access-scoped only — verified against the live API:
`access/apps` 200, `cfd_tunnel` empty, `dns_records` 403, `rulesets` 403. Kept
separate so the token `make node-token` uses routinely stays least-privilege.

### Rate limiting is narrower than the handoff assumed

Three plan limits, each found by being rejected:

- `period` must be **10** seconds — `not entitled to use the period 60, can only
  use a period among [10]`
- `mitigation_timeout` must **equal** the period — `not entitled to use a
  mitigation timeout different from 10`
- `counting_expression` needs paid Advanced Rate Limiting — `an higher Advanced
  Rate Limiting plan is required`

The counting expression was the one worth having: counting only 401s throttles
password guessing and can never touch a legitimate client. Without it the rule
counts every request, so the threshold has to clear real trace bursts: **100
requests per 10 s per IP, block 10 s**. Override with `RATELIMIT_REQUESTS`.
`scripts/auth-hostname.py` still tries the 401-counting rule first, so the rule
improves by itself if the plan is ever upgraded.

A fourth was a bug of mine, not a plan limit: the phase-entrypoint `PUT` takes
`rules` only, and I was sending `name`/`kind`/`phase` (which belong to
`POST /zones/{z}/rulesets`), giving `invalid JSON: unknown field "kind"`. Fixed.
The script also used to print `applied N change(s)` counting *planned* rather
than *successful* changes, so a failed rule read as success; it now reports
`applied N of M` and exits non-zero.

## What did not behave as the handoff described

1. **`MLFLOW_AUTH_ADMIN_PASSWORD` is not a variable MLflow 3.14 reads.**
   `read_auth_config()` does `config["mlflow"]["admin_password"]` — required key,
   `KeyError` if absent — and configparser does no environment interpolation.
   The admin password and the auth `database_uri` can therefore only come from
   the ini file. Resolved with `docker/mlflow/basic_auth.ini.template` plus
   `docker/mlflow/auth-entrypoint.sh`, which renders it into `/dev/shm` at
   container start, so neither secret reaches git, an image layer, or
   `docker history`. The variable name is kept, but it feeds the renderer.

2. **"Unset the admin password afterwards" is neither needed nor possible.**
   `create_admin_user()` is `if not store.has_user(username)`, so the value is
   ignored once the user exists; and the ini key is mandatory, so it cannot be
   removed. Editing it later does **not** rotate the password — that needs the
   `users/update-password` API. Noted in `env/server.env` and §6 of the README.

3. **A separate `MLFLOW_AUTH_DB_PASSWORD` cannot exist.** The handoff asks for
   `mlflow_auth` to be owned by the `mlflow` role, and a role has exactly one
   password. The auth `database_uri` uses `MLFLOW_DB_PASSWORD`.

4. **`CREATE DATABASE ... OWNER mlflow` needs `-U supabase_admin`.** The
   `postgres` role is not a superuser in this image and is not a member of
   `mlflow`, so it fails with `must be able to SET ROLE "mlflow"`. Noted in
   `supabase/init/99-mlflow.sql`, which now also creates `mlflow_auth` so a
   fresh cluster reproduces this.

5. **`--allowed-hosts` must carry the *published* port, 5001, not 5000.**
   Matching is an exact string compare (`is_allowed_host_header`: `host ==
   allowed`, no port stripping), and a loopback client's Host header carries the
   published port. Listing `127.0.0.1:5000` gave `403 Invalid Host header` on
   every request to `127.0.0.1:5001` — the README's own trap, with a port twist.

6. **Alembic races across uvicorn workers on a fresh auth database.** Each
   worker calls `store.init_db()` → `migrate_if_needed()`; with `--workers 2`
   one died with `table users already exists` before uvicorn restarted it. The
   entrypoint now runs the same migration once, single-threaded, before uvicorn
   forks. Start logs show `auth schema at head` and zero restarts.

7. **`grant` is not an upsert and there is no user-permission update route.**
   `grant_user_resource_permission` raises `RESOURCE_ALREADY_EXISTS`; changing a
   grant is revoke-then-grant. Also `permissions/get` resolves the *effective*
   permission and answers `200 {"permission":"NO_PERMISSIONS"}` for a user with
   no row at all, so explicit grants must be read from `permissions/list`.
   Both handled in `scripts/auth-user.py`.

8. **`make test-tunnel NODE=all` failed for a pre-existing, unrelated reason:**
   experiment 2 (`rk-tunnel-test-all`) had been `deleted` since 2026-07-21, and
   `set_experiment` refuses a deleted experiment. The Access path itself passed
   (unauthenticated blocked 302, service token accepted). I restored experiment
   2 so the check could run; it then passed fully. **This is a change to
   existing data** — re-delete it if that is unwanted.

### Also worth knowing

- **`--app-name basic-auth` works under uvicorn with `--allowed-hosts` and
  `--workers`.** The handoff asked this to be verified early; it was, on an
  isolated SQLite instance before anything shared was touched. `--app-name`
  swaps the FastAPI app for the Flask one, but `mlflow/server/__init__.py:82`
  still calls `security.init_security_middleware(app)`, so the host guard and
  CORS apply exactly as on the existing service. No change to the existing
  service was needed or made.
- **No open self-registration.** Only `/health`, `/static` and `/favicon.ico`
  answer unauthenticated; `/signup` and `users/create` both require an
  authenticated workspace admin.
- **`mlflow-auth` is behind compose profile `auth`**, like `studio` and
  `tunnel`. Plain `make up` therefore targets the same four services it did
  before, which keeps `rk-mlflow` out of the blast radius and keeps a fresh
  clone working even though `gen-secrets.py` does not know the new keys.

## The compose diff

`git diff compose.yaml` — one new service, `mlflow-auth`, plus one line in the
header comment. It copies `mlflow` and changes only: `container_name`,
`entrypoint`, the three auth environment variables, `--app-name basic-auth`, the
`MLFLOW_AUTH_*` host/CORS/worker variables, the published port, and
`profiles: [auth]`. The `mlflow` service is **unchanged**.

Other files: `pyproject.toml` + `uv.lock` (`mlflow[auth]`, which adds only
`flask-wtf` and `wtforms`), `docker/mlflow/Dockerfile` (two `COPY`s after the
dependency layer, so its cache is preserved; both inert for the `mlflow`
service), `supabase/init/99-mlflow.sql`, `Makefile` (`up-auth`, `auth-user`,
`auth-hostname`, `test-auth`; `DC_ALL` gains `--profile auth`),
`cloudflare/README.md` (§6).

New: `docker/mlflow/basic_auth.ini.template`,
`docker/mlflow/auth-entrypoint.sh`, `scripts/auth-hostname.py`.

I first wrote `scripts/auth-user.py` and `scripts/test-auth.py` with
`make auth-user` / `make test-auth` targets, which was more than the handoff
asked for — it gave a short Python snippet and a list of curls. Only one user is
needed, so both were **deleted** at the owner's request and §6 of the README now
carries the equivalent curl flows for creating a user, rotating a password, and
verifying. The curl verification block there was run verbatim against the live
hostname and all five status codes match.

## Verification output

`rk-mlflow` was never recreated: image digest `sha256:84d6e0c5…75a23` and
start time `2026-07-21T03:50:58Z` are identical before and after. Only
`rk-mlflow-mlflow-auth` was built; `rk-mlflow-mlflow:latest` kept its digest.

```
make test-auth USER=datasmith     # against https://mlflow-api.formulacode.org
                                  # (script since deleted; see README section 6)
  ✓ /health reachable (the one unauthenticated route)
  ✓ unauthenticated API request rejected by MLflow (HTTP 401)
  ✓ wrong password rejected (HTTP 401)
  ✓ experiment 23 (datasmith-stage6) readable
  ✓ ungranted experiment 0 denied (HTTP 403)
  ✓ v3 traces/search works under auth (1 trace(s) returned)
  ✓ wrote a run to experiment 23 with HTTP Basic only
  ✓ run read back from the server

make health
  ✓ postgres up — 2178 run(s) recorded
  ✓ supabase storage responding
  ✓ mlflow responding on http://127.0.0.1:5000
  ✓ cloudflare tunnel connected
  ✓ https://mlflow.formulacode.org reachable, gated by Access (HTTP 302)
  ✓ backups scheduled — 7 daily, 8 weekly retained
  ✓ 2798G free on /overflow/recursive-knowledge

make test-tunnel NODE=all          # the existing service-token path
  ✓ unauthenticated request blocked (HTTP 302)
  ✓ service token accepted
  ✓ metrics logged through the tunnel
  ✓ artifact uploaded through the tunnel
  ✓ run read back from the server
```

Database separation — auth tables live only in `mlflow_auth`:

```
mlflow_auth:  users, roles, role_permissions, user_role_assignments,
              experiment_permissions, registered_model_permissions, … (12)
mlflow:       0 tables named users / alembic_version_auth / role_permissions
users:        1 admin (is_admin=t), 2 datasmith (is_admin=f)
grant:        datasmith -> experiment 23 = EDIT  (and nothing else)
```

### Node client

`@mlflow/core@0.4.0` greps clean for any Access header and reads exactly
`MLFLOW_TRACKING_USERNAME`, `MLFLOW_TRACKING_PASSWORD`, `MLFLOW_TRACKING_TOKEN`
— the handoff's premise confirmed. A trace was written with the real client over
loopback using only the three bundle variables, with `CF_ACCESS_*` explicitly
unset:

```
trace tr-bd64a4cc83dd4a1df14ed79f8dffd3fe  ->  experiment 23, state OK
same client with no credentials            ->  401 MlflowHttpError Unauthorized
```

`@mlflow/codex@0.4.0` installs cleanly alongside it. A full `codex exec` run was
**not** performed — it spends model credits and wants a global install, and it
adds nothing the above does not already prove about the auth path. Worth doing
once from the datasmith side after `make auth-hostname APPLY=1`, since that also
exercises `mlflow-codex setup`'s `notify` wiring.

## Rollback

```bash
docker compose --env-file env/server.env --profile auth down mlflow-auth
make auth-hostname            # shows what exists at the edge
```
Then delete the `mlflow-api` ingress rule, DNS record `mlflow-api`, and rule
`934e613f9ba14101931d2a3b895aca7a` in ruleset `549862bc19d14269b857bfb9c2e2f341`. `git checkout main` reverts every file. The `mlflow_auth` database can
stay; it holds no tracking data. Experiment 2 can be re-deleted (see item 8).

## One thing to fix regardless

While redacting output I printed one line too many and **exposed the existing
`CLOUDFLARE_API_TOKEN` value in the session transcript.** It is not in any file
and not in git, but it is in the session log. Rotate it at
dash.cloudflare.com/profile/api-tokens; `make node-token` is the only consumer.
