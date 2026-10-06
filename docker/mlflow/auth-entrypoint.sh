#!/bin/sh
# Render basic_auth.ini from the environment, migrate the auth schema, then
# exec the server.
#
# Only the mlflow-auth service uses this entrypoint; the plain `mlflow`
# service is unaffected by its presence in the image.
set -eu

: "${MLFLOW_DB_PASSWORD:?must be set (see env/server.env)}"
: "${MLFLOW_AUTH_ADMIN_PASSWORD:?must be set (see env/server.env)}"
: "${MLFLOW_AUTH_CONFIG_PATH:?must be set by the compose service}"

# --- 1. render the config ---------------------------------------------------
# MLflow reads admin_password and the full auth database_uri only from this
# file, with a plain configparser that does no environment interpolation, and
# with required-key access for both. There is no MLFLOW_AUTH_ADMIN_PASSWORD
# variable in MLflow 3.14. So the secrets have to reach a file somehow; doing
# it here keeps them out of git and out of `docker history`, and the
# destination is tmpfs, so they never touch a writable image layer.
python - "$MLFLOW_AUTH_CONFIG_PATH" <<'PY'
import os, pathlib, string, sys

dest = pathlib.Path(sys.argv[1])
template = pathlib.Path("/etc/mlflow/basic_auth.ini.template").read_text()
dest.parent.mkdir(parents=True, exist_ok=True)
# substitute(), not safe_substitute(): a missing variable must fail loudly here
# rather than leave a literal ${...} inside a database URI.
dest.write_text(string.Template(template).substitute(os.environ))
dest.chmod(0o600)
PY

# --- 2. migrate the auth schema, single-threaded ----------------------------
# Each uvicorn worker calls store.init_db() -> alembic migrate_if_needed() on
# startup, and alembic is not concurrency-safe: on a fresh database two workers
# race and one dies with "table users already exists" before uvicorn restarts
# it. Running the same migration once here, before uvicorn forks, removes the
# race — the workers then find the schema already at head. Idempotent, so it is
# a no-op on every subsequent start.
python -c '
import os
from mlflow.server.auth.config import read_auth_config
from mlflow.server.auth.sqlalchemy_store import SqlAlchemyStore
SqlAlchemyStore().init_db(read_auth_config().database_uri)
print("auth schema at head", flush=True)
'

exec "$@"
