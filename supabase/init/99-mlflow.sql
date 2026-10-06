-- MLflow gets its own database and its own least-privilege role, rather than
-- sharing Supabase's `postgres` database. Two reasons: `make backup` can dump
-- the run history on its own, and a compromised tracking server cannot read
-- Supabase's auth/storage tables.
--
-- Runs once, at first cluster init, as `supabase_admin` — the only superuser in
-- this image. Applying it by hand to a live cluster therefore needs
-- `psql -U supabase_admin`; the `postgres` role is not a member of `mlflow` and
-- fails CREATE DATABASE ... OWNER mlflow with "must be able to SET ROLE".
-- Uses the same psql env-substitution pattern as upstream's roles.sql / jwt.sql.

\set mlflow_pass `echo "$MLFLOW_DB_PASSWORD"`

-- Idempotent so re-running against an existing cluster is harmless.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mlflow') THEN
    CREATE ROLE mlflow WITH LOGIN;
  END IF;
END
$$;

ALTER ROLE mlflow WITH PASSWORD :'mlflow_pass';

-- CREATE DATABASE cannot run inside a transaction or a DO block, so it is
-- generated and executed via \gexec.
SELECT 'CREATE DATABASE mlflow OWNER mlflow'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mlflow')\gexec

-- MLflow creates its own schema on first start; it just needs the room to.
GRANT ALL PRIVILEGES ON DATABASE mlflow TO mlflow;

-- A second database for MLflow's basic-auth users and permissions, used only by
-- the mlflow-auth service (rk-mlflow-auth). Separate from `mlflow` because it is
-- a different concern with its own migration chain (alembic_version_auth) — and
-- because `make backup` dumping the whole cluster should not entangle the two.
-- It holds NO tracking data, so it can be dropped and rebuilt without loss.
SELECT 'CREATE DATABASE mlflow_auth OWNER mlflow'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mlflow_auth')\gexec

GRANT ALL PRIVILEGES ON DATABASE mlflow_auth TO mlflow;
