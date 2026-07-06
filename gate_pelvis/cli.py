"""Command-line entry point (a thin, friendly wrapper over the pipeline).

Examples:
    python -m gate_pelvis.cli run --stl models/control_mm.stl --out out_control --photons 1000000
    python -m gate_pelvis.cli compare --control out_control --fracture out_fracture --out roi_analysis
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import fields

# Windows consoles default to a legacy codepage (e.g. cp874) that cannot encode
# the Thai progress messages; force UTF-8 so direct CLI runs never crash on print.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from .config import SimConfig
from .pipeline import run_simulation
from .analysis import compare_runs, aggregate_seeds, ROI


def _add_config_args(p: argparse.ArgumentParser) -> None:
    defaults = SimConfig(stl="", out="")
    p.add_argument("--stl", required=True, help="Path to STL mesh (mm)")
    p.add_argument("--out", default="poc_radiograph_out", help="Output directory")
    p.add_argument("--clean", action="store_true", help="Delete output dir first")
    for name in ("photons", "threads", "pix", "n_procs", "random_seed"):
        p.add_argument(f"--{name}", type=int, default=getattr(defaults, name))
    p.add_argument("--mode", choices=("single", "balanced", "max"), default=defaults.mode,
                   help="parallelism: single=1 proc, balanced=cpu-2, max=all cpu "
                        "(RAM-capped; --n_procs overrides)")
    for name in ("sod", "odd", "film_xy", "film_thickness", "energy_keV",
                 "rot_x", "rot_y", "rot_z", "primary_theta_deg", "primary_dE_keV",
                 "field_mm", "obj_dx", "obj_dy"):
        p.add_argument(f"--{name}", type=float, default=getattr(defaults, name))
    for name in ("src_x", "src_y", "src_z"):     # ตำแหน่ง source 3D (default: 0,0,-sod)
        p.add_argument(f"--{name}", type=float, default=None)
    p.add_argument("--object_material", default=defaults.object_material)
    p.add_argument("--no_center_mesh", action="store_true", help="Do not auto-center STL")
    p.add_argument("--no_phsp", action="store_true", help="Skip phase-space / SPR")


def _cfg_from_args(a) -> SimConfig:
    valid = {f.name for f in fields(SimConfig)}
    kw = {k: v for k, v in vars(a).items() if k in valid}
    kw["center_mesh"] = not a.no_center_mesh
    kw["separate_primary_scatter"] = not a.no_phsp
    kw["write_phsp"] = not a.no_phsp
    return SimConfig(**kw)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="gate_pelvis", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run a full flat+object simulation")
    _add_config_args(run_p)

    cmp_p = sub.add_parser("compare", help="ROI comparison of two runs")
    cmp_p.add_argument("--control", required=True)
    cmp_p.add_argument("--fracture", required=True)
    cmp_p.add_argument("--out", default="roi_analysis")
    cmp_p.add_argument("--roi", nargs=4, type=int, metavar=("X0", "Y0", "W", "H"),
                       help="Explicit ROI; omit to auto-detect")

    agg_p = sub.add_parser("aggregate-seeds",
                           help="Combine N seed runs into mean/std/significance maps")
    agg_p.add_argument("--root", required=True,
                       help="job dir containing seed_0/, seed_1/, ... each with control/ fracture/")
    agg_p.add_argument("--n", type=int, required=True, help="number of seeds")
    agg_p.add_argument("--out", default="seed_analysis")

    a = parser.parse_args(argv)

    if a.cmd == "run":
        srcs = [a.src_x, a.src_y, a.src_z]
        if any(s is not None for s in srcs) and any(s is None for s in srcs):
            parser.error("ต้องระบุ --src_x --src_y --src_z ครบทั้งสามค่าพร้อมกัน "
                         "(ไม่งั้นจะย้อนไปใช้ตำแหน่ง default เงียบ ๆ)")
        cfg = _cfg_from_args(a)
        run_simulation(cfg, clean=a.clean)
    elif a.cmd == "compare":
        roi = ROI(*a.roi) if a.roi else None
        res = compare_runs(a.control, a.fracture, roi=roi)
        res.save(a.out)
        print("Summary:")
        for k, v in res.summary.items():
            print(f"  {k}: {v}")
    elif a.cmd == "aggregate-seeds":
        from pathlib import Path
        root = Path(a.root)
        pairs = [(root / f"seed_{r}" / "control", root / f"seed_{r}" / "fracture")
                 for r in range(a.n)]
        summary = aggregate_seeds(pairs, a.out)
        print("Multi-seed summary:")
        for k, v in summary.items():
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
