"""gate_pelvis — clean GATE 10 Monte Carlo pipeline for pelvis X-ray SPR.

Public API:
    SimConfig          - all run parameters (one dataclass)
    run_simulation     - run flat+object phases then post-process to SPR
    postprocess        - re-derive transmission/attenuation/SPR from existing runs
    compare_runs, ROI  - ROI comparison of two SPR maps (control vs fracture)
"""

from .config import SimConfig
from .pipeline import run_simulation, postprocess, RunResult
from .analysis import compare_runs, ROI, CompareResult

__all__ = [
    "SimConfig", "run_simulation", "postprocess", "RunResult",
    "compare_runs", "ROI", "CompareResult",
]

__version__ = "1.1.0"
