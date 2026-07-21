"""Node-side helpers for reporting verl runs to the rk-mlflow coordination box.

Two transports, deliberately separate:
  auth       — Cloudflare Access headers so telemetry can cross the tunnel
  ckpt_sync  — rsync over SSH so checkpoint bytes never do
"""

__version__ = "0.1.0"
