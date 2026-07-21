# rk-mlflow

Coordination server for RL training runs. Three verl nodes train independently
and report to this box; MLflow is the one place to watch all of it.

**Code and secrets live in this repo. All data lives in `/overflow/recursive-knowledge`.**

---

# Setting up a compute node

**Start here if someone sent you a bundle** (e.g. `all.zip`). You do not need
this repo checked out, and you do not need access to the server.

The zip contains three files:

| File | What it is |
|---|---|
| `<name>.env` | Tracking URI, Cloudflare Access token, rsync target |
| `<name>_id_ed25519` | SSH key for checkpoint transfer |
| `<name>_id_ed25519.pub` | Its public half |

It is a **credential**. Treat it like a password: no shared filesystems, no
git, no Slack.

### 1. Unpack, and fix the permissions

```bash
mkdir -p ~/.rk && unzip all.zip -d ~/.rk
chmod 600 ~/.rk/all.env ~/.rk/all_id_ed25519
```

The `chmod` is not optional — `ssh` refuses to use a private key that other
users can read, and the failure message points at the key rather than at
permissions.

### 2. Install the client

```bash
uv pip install 'git+ssh://git@github.com/<org>/mlflow-vis.git#subdirectory=client'
```

This is what makes authentication automatic: it registers an MLflow
request-header provider that attaches the Cloudflare Access headers to every
REST call, so training code never has to know the server is gated. It also
installs the `rk-ckpt-sync` command.

If you do not have access to the repo, ask for the `client/` directory and
`uv pip install ./client` instead. Without this package MLflow receives an
HTML login page and fails with a confusing JSON parse error.

### 3. Load the environment

```bash
source ~/.rk/all.env
```

Put that in your shell profile *and* in any batch/job script — a scheduler
job does not inherit your interactive shell.

### 4. Verify both paths before you train

Telemetry, over the tunnel:

```bash
python -c "import mlflow; print(mlflow.search_experiments())"
```

Checkpoints, over SSH — a separate path with separate credentials, so it can
fail independently:

```bash
ssh -i "$RK_CKPT_KEY" -p "$RK_CKPT_PORT" "$RK_CKPT_HOST" true && echo "rsync path OK"
```

A list of experiments and a silent `OK` mean you are done. A login page or a
302 means the Access token is missing; `Permission denied (publickey)` means
the server has not authorized your key yet — ask whoever sent the bundle to
run `make node-authorize`.

### 5. Point verl at it

```yaml
trainer:
  project_name: rl-posttraining
  experiment_name: all           # unique per node
  logger: ['console', 'mlflow']  # console = local fallback if tracking blips
  save_freq: 50
  default_local_dir: /scratch/ckpts/all
```

## Shipping checkpoints — `rk-ckpt-sync`

Metrics go over the tunnel; **checkpoint bytes never do.** Cloudflare caps
request bodies at 100 MB. `rk-ckpt-sync` rsyncs the directory over SSH and
logs only a *pointer* to MLflow.

```bash
rk-ckpt-sync --local-dir /scratch/ckpts/all/global_step_50 --step 50 --run-id <run-id>
```

| Flag | Default | Meaning |
|---|---|---|
| `--local-dir` | *required* | Checkpoint directory on this node |
| `--step` | *required* | `global_step` this checkpoint corresponds to |
| `--run-id` | `$MLFLOW_RUN_ID`, else the active run | Which MLflow run to attach the pointer to |
| `--experiment` | `$MLFLOW_EXPERIMENT_NAME`, else `default` | Groups the remote directory |
| `--kind` | `sharded` | `sharded` = resumable, tied to topology; `hf` = exported weights |
| `--dry-run` | off | Print the size and destination, transfer nothing |

Always start with `--dry-run`. It shows exactly where the bytes would land
without moving any.

**Two defaults that bite from a shell hook.** After training exits there is no
active MLflow run and `MLFLOW_RUN_ID` is usually unset, so pass `--run-id`
explicitly — copy it from the run's URL in the UI. Likewise
`MLFLOW_EXPERIMENT_NAME` is not exported by the verl config, so without
`--experiment` your checkpoints land under `default/` while the run itself
lives elsewhere. Set both:

```bash
export MLFLOW_EXPERIMENT_NAME=all
rk-ckpt-sync --local-dir /scratch/ckpts/all/global_step_50 --step 50 --run-id abc123…
```

### What it does

Bytes land at `$RK_CKPT_ROOT/<experiment>/<run_id>/step_<N>` on the server,
staged as `step_<N>.incoming` and renamed only on success — the server's
retention sweep skips `*.incoming`, so an interrupted transfer is never
mistaken for a complete checkpoint nor reaped mid-flight. Re-running the same
step is safe; it resumes with `--partial` and replaces on completion.

It then tags the MLflow run so the checkpoint is discoverable from the UI:

| Tag / metric | Value |
|---|---|
| `ckpt.step_<N>.uri` | `user@host:/path/to/step_<N>` |
| `ckpt.step_<N>.kind` | `sharded` or `hf` |
| `ckpt.step_<N>.size_gb` | Transferred size |
| `ckpt.step_<N>.config_hash` | Fingerprint of `config.json` / `config.yaml` / `params.json` |
| `ckpt.latest_step` | Most recent step synced |
| `ckpt_size_gb` (metric) | Size, plotted against step |

The config hash matters for resuming: a sharded checkpoint needs the exact
config it was written with, and a mismatch is the difference between a
resumable artifact and a directory of unusable tensors.

> Checkpoints are pruned on the server by retention policy (`CKPT_KEEP_LAST`
> steps per run). To keep one permanently, ask for a `.keep` file to be placed
> in its directory — the sweep never touches those.

---

## The one thing to understand

Two kinds of traffic, two completely different paths:

| | Telemetry | Checkpoints |
|---|---|---|
| What | metrics, params, tags, small artifacts | sharded model weights |
| Size | kilobytes, continuous | gigabytes, every `save_freq` |
| Path | **Cloudflare tunnel** → MLflow | **rsync over SSH** → `/overflow/.../checkpoints` |
| Auth | Access service token | per-node SSH key |

Cloudflare proxies cap request bodies at 100 MB. Checkpoints are far larger, so
they never touch the tunnel — nodes rsync the bytes and log only a *pointer*
(URI, step, config hash) to MLflow. Keep that split intact.

## Access the dashboard

**Through the tunnel** (once configured) — the normal way:

    https://mlflow.<your-domain>

Cloudflare Access asks for SSO first. That hostname is the only thing this
stack exposes to the internet.

**Over SSH** — always works, no Cloudflare needed:

    ssh -L 5000:localhost:5000 ml-login
    # then open http://localhost:5000

**Supabase Studio** (admin console: SQL editor, table browser) is deliberately
*not* published. MLflow is the visualization surface; Studio is for poking at
the database.

    make studio
    ssh -L 8000:localhost:8000 ml-login    # then http://localhost:8000

---

## Quickstart

### Server (this box)

```bash
make install     # uv env, secrets, vendored Supabase assets, images
make up          # postgres + storage + mlflow
make bucket      # create the artifact bucket (first run only)
make smoke       # prove the whole path works
```

`make install` is idempotent and reproducible — it never overwrites an
existing secret, and dependencies come from the committed `uv.lock`.

### Turn on the tunnel

Set up the tunnel and Access application first — full walkthrough in
[`cloudflare/README.md`](cloudflare/README.md). Then:

```bash
# in env/server.env:
#   CLOUDFLARE_TUNNEL_TOKEN=<install token from the dashboard>
#   MLFLOW_HOSTNAME=mlflow.<your-domain>
#   MLFLOW_ALLOWED_HOSTS=...,mlflow.<your-domain>   <- required, see below
make up-tunnel
make health
```

> **`MLFLOW_ALLOWED_HOSTS` must contain your tunnel hostname.** MLflow 3 has a
> DNS-rebinding guard that returns `403 Invalid Host header` for anything not
> listed — while `/health` keeps returning 200, so the stack looks fine and
> every node fails. Host matching includes the port, so list both `host` and
> `host:5000` for local access.

### Onboard a node or teammate

```bash
make node-provision NODE=julius   # generates env/nodes/julius.env + an SSH key
make node-token     NODE=julius   # Access service token, written into that file
make node-authorize NODE=julius   # grants rsync access (asks for confirmation)
make test-tunnel    NODE=julius   # verifies telemetry end to end
```

Zip `env/nodes/julius.env` together with its keyfile and send it over a
private channel. What the recipient does with it is
[Setting up a compute node](#setting-up-a-compute-node) at the top of this
file — point them there rather than explaining it again.

`make node-authorize` is the step that is easy to forget: without it telemetry
works and rsync fails with `Permission denied (publickey)`.

Teammates who only want to *look* at the dashboard need none of this — send
them the URL. Access lets any address matching your policy sign in by email.

---

## Secrets

Two tiers, and the distinction is the point.

### `env/server.env` — **never share**

Full control of the box. Generated by `make install`; `chmod 600`, gitignored.

| Variable | What it is |
|---|---|
| `POSTGRES_PASSWORD` | Supabase Postgres superuser + service roles |
| `MLFLOW_DB_PASSWORD` | MLflow's own least-privilege database role |
| `JWT_SECRET` | Signs `ANON_KEY` / `SERVICE_ROLE_KEY`; changing it regenerates both |
| `ANON_KEY` | Supabase anonymous JWT (derived) |
| `SERVICE_ROLE_KEY` | Supabase **full-access** JWT (derived) — bypasses row-level security |
| `S3_PROTOCOL_ACCESS_KEY_ID` / `_SECRET` | MLflow's credentials for the artifact bucket |
| `PG_META_CRYPTO_KEY` | Studio's credential encryption key |
| `DASHBOARD_PASSWORD` | Studio login |
| `CLOUDFLARE_TUNNEL_TOKEN` | Authorizes an outbound tunnel for your account |
| `CLOUDFLARE_API_TOKEN` | Optional; lets `make node-token` issue Access service tokens |

Non-secret knobs in the same file: `RK_DATA_ROOT`, `MLFLOW_HOSTNAME`,
`MLFLOW_ALLOWED_HOSTS`, `CKPT_SSH_*`, and the retention settings
(`CKPT_KEEP_LAST`, `BACKUP_KEEP_DAILY`, `BACKUP_KEEP_WEEKLY`, `DISK_WARN_GB`).

### `env/nodes/<name>.env` — **shareable**

What a teammate or node actually needs. Contains no database password and no
tunnel token; grants exactly telemetry + rsync, revocable per node.

| Variable | What it is |
|---|---|
| `MLFLOW_TRACKING_URI` | `https://mlflow.<your-domain>` |
| `CF_ACCESS_CLIENT_ID` / `_SECRET` | That node's Cloudflare Access service token |
| `RK_CKPT_HOST` / `_PORT` / `_ROOT` | Where to rsync checkpoints |
| `<name>_id_ed25519` | That node's SSH key (ships alongside) |

Revoke one node without touching anything else:

```bash
sed -i '/rk-mlflow-julius/d' ~/.ssh/authorized_keys   # rsync access
# then delete its service token in the Cloudflare dashboard    # telemetry
```

---

## Data layout

```
/overflow/recursive-knowledge/
├── supabase/db/        Postgres — runs, metrics, checkpoint pointers
├── supabase/storage/   artifact bucket bytes
├── checkpoints/        <experiment>/<run_id>/step_<N>/   (rsync target)
└── backups/{daily,weekly}/   +  backups/logs/  (cron output)
```

## Backups

Postgres holds every run, metric, and checkpoint pointer — the checkpoint bytes
on disk are unusable without it. Dumps are `pg_dumpall` (roles included, since
storage-api authenticates as `supabase_storage_admin`).

```bash
make backup-cron-enable    # daily 03:17, weekly Sun 04:47
make backup-cron-status
make backup-cron-disable   # existing dumps are kept
make backup TIER=weekly    # run one now
make backup-list
make restore F=/overflow/recursive-knowledge/backups/weekly/cluster-*.sql.gz
```

Retention keeps 7 daily and 8 weekly, with a **hard floor of 2 per tier** —
a bad dump can never leave you with zero recoverable copies.

## Housekeeping

`/overflow` is a shared pool that runs near capacity, so retention is
load-bearing rather than hygiene.

```bash
make usage                 # what the data root is consuming
make prune                 # dry run: which checkpoints would go
make prune APPLY=1         # actually delete
make health                # warns below DISK_WARN_GB
```

Pruning keeps the last `CKPT_KEEP_LAST` steps per run and never touches an
in-flight `*.incoming` transfer, anything under 30 minutes old, or a step
directory containing a `.keep` file.

## Everything else

```bash
make help
```

---

## Notes for whoever maintains this

- **Supabase is pinned** to the commit in `supabase/PINNED_REF`; `make install`
  vendors its init SQL and Kong config from exactly that ref. To upgrade, bump
  the SHA, re-run install, diff `supabase/upstream/`, and update the image tags
  in `compose.yaml` to match that commit's `docker-compose.yml`.
- **The tracking server runs Python 3.13, not 3.14.** MLflow 3.14.0's uvicorn
  entry point imports `importlib.abc.Traversable`, which 3.14 removed — and
  uvicorn is mandatory because `--allowed-hosts` is rejected under gunicorn.
  The local ops venv stays on 3.14; only the image is pinned back.
- **`STORAGE_INTERNAL_URL` must match `MLFLOW_S3_ENDPOINT_URL`'s host.**
  storage-api rebuilds the S3 canonical request from it, so a mismatch fails
  every artifact write with `SignatureDoesNotMatch`.
- **Kong is not on the MLflow path.** MLflow talks to storage-api directly, so
  the admin gateway being down can't stop a training run from logging.
