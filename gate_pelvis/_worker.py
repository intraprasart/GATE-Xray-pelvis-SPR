"""Subprocess worker: run ONE GATE phase from a JSON config.

Invoked by pipeline.py as a fresh process so each GATE SimulationEngine gets its
own interpreter (OpenGATE allows only one engine per process).

    python -m gate_pelvis._worker <config.json> <flat|object>
"""

from __future__ import annotations

import sys

from .config import SimConfig
from .engine import run_phase


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m gate_pelvis._worker <config.json> <flat|object>", file=sys.stderr)
        return 2
    config_path, phase = argv
    cfg = SimConfig.from_json(config_path)
    run_phase(cfg, phase)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
