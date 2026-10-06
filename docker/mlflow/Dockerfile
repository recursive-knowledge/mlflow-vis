# Reproducible tracking-server image: dependencies come from the committed
# uv.lock, never from a fresh resolve at build time.
#
# uv arrives as a pinned binary rather than via an uv-flavoured Python base
# image, because those tags float on the uv version.

# 3.13, not 3.14: MLflow 3.14.0's uvicorn app imports importlib.abc.Traversable,
# removed in 3.14. Uvicorn is not optional here — the --allowed-hosts flag the
# Cloudflare tunnel depends on is rejected under gunicorn. See pyproject.toml.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /uvx /usr/local/bin/

# Bytecode precompilation is deliberately off: uv's parallel compiler
# exhausts file descriptors on this host, and it only buys cold-start time on
# a server that starts once and runs for weeks.
ENV UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Only the lock inputs, so the dependency layer caches across code changes.
# .python-version is deliberately NOT copied — it pins the local ops venv to
# 3.14, which the server cannot run on.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python 3.13

# Last, so the dependency layer above stays cached. Both are inert for the
# plain `mlflow` service — only `mlflow-auth` sets this entrypoint, and only it
# reads the template. See compose.yaml.
COPY docker/mlflow/basic_auth.ini.template /etc/mlflow/basic_auth.ini.template
COPY docker/mlflow/auth-entrypoint.sh /usr/local/bin/auth-entrypoint.sh

EXPOSE 5000
