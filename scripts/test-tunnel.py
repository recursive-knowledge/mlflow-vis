"""Verify a node bundle can log to MLflow through the Cloudflare tunnel.

This is the test that matters before handing a bundle to someone: `make
smoke` proves the server works over loopback, which says nothing about
whether the tunnel is up, Access is configured, or the service token is
valid. This exercises the exact path a verl node takes.

    make test-tunnel NODE=julius
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

import mlflow
import requests

REPO = Path(__file__).resolve().parent.parent


def load_bundle(node: str) -> dict[str, str]:
    path = REPO / "env" / "nodes" / f"{node}.env"
    if not path.exists():
        sys.exit(f"no bundle for '{node}' — run: make node-provision NODE={node}")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        m = re.match(r'^\s*export\s+([A-Z0-9_]+)="?([^"]*)"?\s*$', line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def fail(msg: str, hint: str = "") -> int:
    print(f"\n  \033[31m✗\033[0m {msg}")
    if hint:
        print(f"    {hint}")
    return 1


def main() -> int:
    node = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NODE", "")
    if not node:
        sys.exit("usage: test-tunnel.py <node-name>")

    bundle = load_bundle(node)
    uri = bundle.get("MLFLOW_TRACKING_URI", "")
    client_id = bundle.get("CF_ACCESS_CLIENT_ID", "")
    client_secret = bundle.get("CF_ACCESS_CLIENT_SECRET", "")

    print(f"\ntesting '{node}' through {uri}\n")

    if not uri or "example.com" in uri:
        return fail("MLFLOW_TRACKING_URI is unset or still the placeholder",
                    "set MLFLOW_HOSTNAME in env/server.env, then reprovision")
    if not client_id or not client_secret:
        return fail("service token missing from the bundle",
                    f"paste CF_ACCESS_CLIENT_ID/SECRET into env/nodes/{node}.env "
                    "(Zero Trust > Access > Service Auth)")

    headers = {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret}

    # 1. Access must reject an unauthenticated request. If it does not, the
    #    tracking server is exposed to the internet and the rest of this test
    #    is measuring the wrong thing.
    try:
        bare = requests.get(f"{uri}/health", timeout=15, allow_redirects=False)
    except requests.RequestException as exc:
        return fail(f"cannot reach {uri}: {exc}",
                    "is the tunnel up? make up-tunnel && make health")
    if bare.status_code == 200:
        return fail("unauthenticated request returned 200 — Access is NOT in front of MLflow",
                    "add a self-hosted Access application for this hostname")
    print(f"  \033[32m✓\033[0m unauthenticated request blocked (HTTP {bare.status_code})")

    # 2. The service token must get through.
    authed = requests.get(f"{uri}/health", headers=headers, timeout=15)
    if authed.status_code != 200:
        return fail(f"service token rejected (HTTP {authed.status_code})",
                    "is the token added to the application's Service Auth policy?")
    print("  \033[32m✓\033[0m service token accepted")

    # 3. A real run, over the tunnel, exactly as verl would log it.
    os.environ["MLFLOW_TRACKING_URI"] = uri
    os.environ["CF_ACCESS_CLIENT_ID"] = client_id
    os.environ["CF_ACCESS_CLIENT_SECRET"] = client_secret

    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment(f"rk-tunnel-test-{node}")

    with mlflow.start_run(run_name=f"tunnel-check-{node}") as run:
        for step in range(3):
            mlflow.log_metric("probe_loss", 1.0 / (step + 1), step=step)
        mlflow.log_param("node", node)
        print("  \033[32m✓\033[0m metrics logged through the tunnel")

        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.txt"
            probe.write_text(f"tunnel test from {node}\n")
            mlflow.log_artifact(str(probe))
        print("  \033[32m✓\033[0m artifact uploaded through the tunnel")

        run_id = run.info.run_id

    fetched = mlflow.MlflowClient().get_run(run_id)
    if fetched.data.metrics.get("probe_loss") is None:
        return fail("metric did not persist", "check: make logs S=mlflow")
    print("  \033[32m✓\033[0m run read back from the server")

    print(f"\n'{node}' is good to go — {uri}/#/experiments\n")
    print("note: this bundle logs telemetry only. Verify rsync separately:")
    print(f"  ssh -i env/nodes/{node}_id_ed25519 {bundle.get('RK_CKPT_HOST')} true\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
