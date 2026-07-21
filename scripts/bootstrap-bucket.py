"""Create the MLflow artifact bucket in Supabase Storage, idempotently.

MLflow will not create its own bucket — it assumes the artifact root already
exists — so a fresh stack logs artifacts into a 404 until this has run.
Invoked by `make install` and safe to re-run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parent.parent


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    # Overridable because this runs inside a one-off container on the compose
    # network (storage-api is not published to the host), where .env is
    # bind-mounted rather than sitting next to the script.
    path = Path(os.environ.get("RK_ENV_FILE", REPO / "env" / "server.env"))
    if not path.exists():
        sys.exit("no env/server.env — run: make install")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def main() -> int:
    env = load_env()
    bucket = env.get("MLFLOW_BUCKET", "mlflow")
    endpoint = os.environ.get("S3_ENDPOINT", "http://storage:5000/s3")

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=env.get("S3_PROTOCOL_ACCESS_KEY_ID"),
        aws_secret_access_key=env.get("S3_PROTOCOL_ACCESS_KEY_SECRET"),
        region_name=env.get("REGION", "local"),
        # Supabase Storage speaks path-style only; virtual-host style would
        # resolve to a hostname that does not exist.
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )

    try:
        s3.head_bucket(Bucket=bucket)
        print(f"bucket '{bucket}' already exists")
        return 0
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("404", "NoSuchBucket", "403"):
            print(f"could not reach storage at {endpoint}: {exc}", file=sys.stderr)
            return 1

    try:
        s3.create_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "BucketAlreadyOwnedByYou":
            print(f"bucket '{bucket}' already exists")
            return 0
        print(f"failed to create bucket '{bucket}': {exc}", file=sys.stderr)
        return 1

    print(f"created bucket '{bucket}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
