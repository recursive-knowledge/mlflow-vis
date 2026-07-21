"""Prove the whole telemetry path works, end to end.

Exercises the two things that silently break: writing metrics to Postgres,
and writing an artifact through the tracking server into Supabase Storage.
An MLflow server with a misconfigured bucket accepts metrics happily and only
fails on the first artifact, so both halves are checked here.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import mlflow

REPO = Path(__file__).resolve().parent.parent


def env_value(key: str, default: str = "") -> str:
    path = REPO / "env" / "server.env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip().startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return default


def main() -> int:
    addr = env_value("MLFLOW_BIND_ADDR", "127.0.0.1")
    port = env_value("MLFLOW_PORT", "5000")
    uri = os.environ.get("MLFLOW_TRACKING_URI", f"http://{addr}:{port}")

    print(f"tracking uri: {uri}")
    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment("rk-smoke-test")

    with mlflow.start_run(run_name="smoke") as run:
        print(f"run id: {run.info.run_id}")

        for step in range(5):
            mlflow.log_metric("loss", 1.0 / (step + 1), step=step)
        mlflow.log_param("purpose", "install verification")
        print("  metrics + params -> postgres  OK")

        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.txt"
            probe.write_text("rk-mlflow smoke test\n")
            mlflow.log_artifact(str(probe))
        print("  artifact -> supabase storage  OK")

        # Reading it back is the only proof the bucket is actually wired up:
        # a write can succeed against a proxy that never persists.
        artifacts = mlflow.artifacts.list_artifacts(run_id=run.info.run_id)
        names = [a.path for a in artifacts]
        if "probe.txt" not in names:
            print(f"  artifact readback FAILED — bucket listing: {names}", file=sys.stderr)
            return 1
        print("  artifact readback            OK")

    print("\nsmoke test passed — telemetry path is live")
    print(f"view it: {uri}/#/experiments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
