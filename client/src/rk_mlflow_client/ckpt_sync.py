"""Ship a checkpoint to the coordination box and link it to its MLflow run.

Checkpoints are gigabytes and the tunnel proxies at 100 MB, so the bytes go
over SSH via rsync on the private network while only a pointer goes to
MLflow. That split is the whole design; this script is the seam.

Usage on a verl node, from a save hook or after a run:

    rk-ckpt-sync --local-dir /scratch/ckpts/grpo/global_step_50 --step 50

Reads RK_CKPT_HOST / RK_CKPT_PORT / RK_CKPT_ROOT and the usual MLflow env.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import subprocess
import sys
from pathlib import Path

import mlflow


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(f"command failed ({result.returncode}): {shlex.join(cmd)}")


def dir_size_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def config_hash(local_dir: Path) -> str:
    """Fingerprint whatever config verl wrote next to the checkpoint.

    Resuming a sharded checkpoint requires the exact config it was written
    with, so a mismatch here is the difference between a resumable artifact
    and a directory of unusable tensors.
    """
    digest = hashlib.sha256()
    for name in sorted(("config.json", "config.yaml", "params.json")):
        candidate = local_dir / name
        if candidate.is_file():
            digest.update(candidate.read_bytes())
    return digest.hexdigest()[:16] if digest.hexdigest() else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-dir", required=True, type=Path,
                       help="checkpoint directory on this node")
    parser.add_argument("--step", required=True, type=int,
                       help="global_step this checkpoint corresponds to")
    parser.add_argument("--run-id", default=os.environ.get("MLFLOW_RUN_ID"),
                       help="MLflow run to attach the pointer to")
    parser.add_argument("--experiment", default=os.environ.get("MLFLOW_EXPERIMENT_NAME", "default"))
    parser.add_argument("--kind", default="sharded", choices=["sharded", "hf"],
                       help="sharded = resumable, tied to topology; hf = exported weights")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    local_dir: Path = args.local_dir.resolve()
    if not local_dir.is_dir():
        sys.exit(f"not a directory: {local_dir}")

    host = os.environ.get("RK_CKPT_HOST")
    root = os.environ.get("RK_CKPT_ROOT")
    port = os.environ.get("RK_CKPT_PORT", "22")
    if not host or not root:
        sys.exit("set RK_CKPT_HOST and RK_CKPT_ROOT — source your node bundle first")

    run_id = args.run_id
    if not run_id:
        active = mlflow.active_run()
        run_id = active.info.run_id if active else None
    if not run_id:
        sys.exit("no run id — pass --run-id or set MLFLOW_RUN_ID")

    user_host, _, _ = host.partition(":")
    remote_run_dir = f"{root}/{args.experiment}/{run_id}"
    final = f"{remote_run_dir}/step_{args.step}"
    # Land in .incoming and rename only on success: the retention sweep on the
    # server skips *.incoming, so a half-transferred checkpoint is never
    # mistaken for a complete one, and never reaped mid-flight.
    staging = f"{final}.incoming"

    size_gb = dir_size_bytes(local_dir) / 1e9
    print(f"syncing {size_gb:.2f} GB  ->  {user_host}:{final}")

    if args.dry_run:
        print("(dry run — nothing transferred, nothing logged)")
        return 0

    # The bundle ships a dedicated key rather than relying on the node's
    # default identity, so it has to be passed explicitly — every ssh here and
    # the one rsync tunnels over. IdentitiesOnly stops ssh offering every
    # agent key first and tripping the server's MaxAuthTries.
    ssh_opts = ["-p", port, "-o", "StrictHostKeyChecking=accept-new"]
    key = os.environ.get("RK_CKPT_KEY")
    if key:
        ssh_opts += ["-i", os.path.expanduser(key), "-o", "IdentitiesOnly=yes"]
    ssh_cmd = ["ssh", *ssh_opts]

    run([*ssh_cmd, user_host, f"mkdir -p {shlex.quote(remote_run_dir)}"])
    run([
        "rsync", "-a", "--partial", "--inplace", "--compress-level=0",
        "--info=progress2", "-e", shlex.join(ssh_cmd),
        f"{local_dir}/", f"{user_host}:{staging}/",
    ])
    # Atomic-enough promotion; rsync has already fsynced the contents.
    run([*ssh_cmd, user_host,
         f"rm -rf {shlex.quote(final)} && mv {shlex.quote(staging)} {shlex.quote(final)}"])

    client = mlflow.MlflowClient()
    client.set_tag(run_id, f"ckpt.step_{args.step}.uri", f"{user_host}:{final}")
    client.set_tag(run_id, f"ckpt.step_{args.step}.kind", args.kind)
    client.set_tag(run_id, f"ckpt.step_{args.step}.size_gb", f"{size_gb:.2f}")
    client.set_tag(run_id, f"ckpt.step_{args.step}.config_hash", config_hash(local_dir))
    client.set_tag(run_id, "ckpt.latest_step", str(args.step))
    client.log_metric(run_id, "ckpt_size_gb", size_gb, step=args.step)

    print(f"logged pointer to run {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
