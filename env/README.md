# env/

Every secret in the project lives here. Nothing in this directory except the
`.example` template is tracked by git.

```
env/
├── server.env.example    tracked — the template
├── server.env            IGNORED — this box's secrets. Never share.
└── nodes/
    ├── julius.env             IGNORED — shareable with that node's owner
    └── julius_id_ed25519      IGNORED — that node's SSH key
```

## The split, and why it matters

`server.env` holds the Postgres password, the Supabase service-role key (which
bypasses row-level security), and the Cloudflare tunnel token. Handing it to
someone gives them the whole box.

A node needs far less: permission to log telemetry, and permission to rsync
checkpoints. That is what `make node-provision NODE=<name>` produces — a bundle
with no database password and no tunnel token, revocable one node at a time.

**If someone asks for access, send them a node bundle, never `server.env`.**

## Generating

```bash
make install                      # fills every blank secret in server.env
make node-provision NODE=julius   # builds a shareable bundle + SSH key
```

`make install` only fills blanks. It will never overwrite a value that is
already set, so re-running it after adding a variable is safe and cannot
invalidate a live database password.

## Rotating

| Secret | How | Blast radius |
|---|---|---|
| A node's access | `sed -i '/rk-mlflow-<node>/d' ~/.ssh/authorized_keys`, delete its Cloudflare service token | that node only |
| `CLOUDFLARE_TUNNEL_TOKEN` | new token in the dashboard, then `make up-tunnel` | tunnel restarts |
| `S3_PROTOCOL_*` | edit, `make up` | brief artifact-write failures |
| `JWT_SECRET` | blank it *and* `ANON_KEY`/`SERVICE_ROLE_KEY`, then `make install` | all three are regenerated together — they must stay consistent |
| `POSTGRES_PASSWORD` | **not** a simple edit — it is baked into the cluster's roles at init; change it with `ALTER USER` inside `make psql` and update the file to match | everything |
