-- MLflow gets its own database and its own least-privilege role, rather than
-- sharing Supabase's `postgres` database. Two reasons: `make backup` can dump
-- the run history on its own, and a compromised tracking server cannot read
-- Supabase's auth/storage tables.
--
-- Runs once, at first cluster init. Uses the same psql env-substitution
-- pattern as upstream's roles.sql / jwt.sql.

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
