"""Subprocess worker: run ONE GATE phase (or one shard of it) from a JSON config.

Invoked by pipeline.py as a fresh process so each GATE SimulationEngine gets its
own interpreter (OpenGATE allows only one engine per process, and on Windows has
no multithreading — so parallelism is achieved by running several of these).

    python -m gate_pelvis._worker <config.json> <flat|object> [shard n_shards out_dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import SimConfig
from .engine import run_phase


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 5):
        print("usage: python -m gate_pelvis._worker <config.json> <flat|object> "
              "[shard n_shards out_dir]", file=sys.stderr)
        return 2
    config_path, phase = argv[0], argv[1]
    cfg = SimConfig.from_json(config_path)
    if len(argv) == 5:
        shard, n_shards, out_dir = int(argv[2]), int(argv[3]), Path(argv[4])
        run_phase(cfg, phase, shard=shard, n_shards=n_shards, out_dir=out_dir)
    else:
        run_phase(cfg, phase)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
