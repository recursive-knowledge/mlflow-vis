"""Generate every secret the stack needs, idempotently, into .env.

Only fills blanks — an existing value is never overwritten, so re-running
after adding a variable is safe and never invalidates a live database
password.

The Supabase ANON_KEY / SERVICE_ROLE_KEY are JWTs signed with JWT_SECRET, so
they must be regenerated together with it; this script keeps them consistent.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
import string
import sys
from pathlib import Path

import jwt

ENV_PATH = Path(__file__).resolve().parent.parent / "env" / "server.env"

ALPHABET = string.ascii_letters + string.digits


def token(n: int) -> str:
    # Alphanumeric only: these land in URLs, psql connection strings, and a
    # Makefile, and shell-special characters have burned every one of those.
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text().splitlines()
    values: dict[str, str] = {}
    for line in lines:
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if m:
            values[m.group(1)] = m.group(2)
    return lines, values


def main() -> int:
    if not ENV_PATH.exists():
        print("no .env — run: cp env/server.env.example env/server.env", file=sys.stderr)
        return 1

    lines, values = read_env(ENV_PATH)
    filled: list[str] = []

    def ensure(key: str, make: callable) -> str:
        current = values.get(key, "")
        if current:
            return current
        value = make()
        values[key] = value
        filled.append(key)
        return value

    ensure("POSTGRES_PASSWORD", lambda: token(32))
    ensure("MLFLOW_DB_PASSWORD", lambda: token(32))
    ensure("PG_META_CRYPTO_KEY", lambda: token(32))
    ensure("DASHBOARD_PASSWORD", lambda: token(24))
    ensure("S3_PROTOCOL_ACCESS_KEY_ID", lambda: token(20))
    ensure("S3_PROTOCOL_ACCESS_KEY_SECRET", lambda: token(40))

    jwt_secret = ensure("JWT_SECRET", lambda: token(48))

    # The service keys are derived, not random: regenerating JWT_SECRET
    # without regenerating these would silently break storage-api auth.
    if "JWT_SECRET" in filled or not values.get("ANON_KEY"):
        issued = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        expires = issued + dt.timedelta(days=3650)
        for key, role in (("ANON_KEY", "anon"), ("SERVICE_ROLE_KEY", "service_role")):
            claims = {
                "role": role,
                "iss": "supabase",
                "iat": int(issued.timestamp()),
                "exp": int(expires.timestamp()),
            }
            values[key] = jwt.encode(claims, jwt_secret, algorithm="HS256")
            if key not in filled:
                filled.append(key)

    if not filled:
        print("all secrets already set — nothing to do")
        return 0

    # Rewrite in place, preserving comments and ordering.
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if m and m.group(1) in values:
            key = m.group(1)
            seen.add(key)
            out.append(f"{key}={values[key]}")
        else:
            out.append(line)
    for key in filled:
        if key not in seen:
            out.append(f"{key}={values[key]}")

    ENV_PATH.write_text("\n".join(out) + "\n")
    ENV_PATH.chmod(0o600)
    print(f"generated {len(filled)} secret(s): {', '.join(sorted(filled))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
