"""Orchestration: run flat + object phases, then post-process to SPR.

`run_simulation` is the single entry point used by both the CLI and the UI. It
launches each GATE phase in its own subprocess (one engine per process), then
derives transmission / attenuation / primary-scatter / SPR images in-process.
"""

from __future__ import annotations

import os
import sys
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .config import SimConfig
from . import imaging, phasespace

# Root that must be importable as a package for `python -m gate_pelvis._worker`.
_PKG_PARENT = str(Path(__file__).resolve().parent.parent)

ProgressFn = Callable[[str], None]


@dataclass
class RunResult:
    out_dir: Path
    images: dict[str, Path] = field(default_factory=dict)   # label -> PNG path
    primary_fraction: float | None = None


def _log(progress: ProgressFn | None, msg: str) -> None:
    if progress is not None:
        progress(msg)
    else:
        print(msg)


def _run_worker(config_path: Path, phase: str, progress: ProgressFn | None) -> None:
    """Run one GATE phase in a subprocess, streaming its stdout."""
    env = dict(os.environ)
    env["PYTHONPATH"] = _PKG_PARENT + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-u", "-m", "gate_pelvis._worker", str(config_path), phase]
    _log(progress, f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd, cwd=_PKG_PARENT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        _log(progress, line.rstrip("\n"))
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"GATE phase '{phase}' failed (exit code {code}). See log above.")


def run_simulation(cfg: SimConfig, progress: ProgressFn | None = None,
                   clean: bool = False) -> RunResult:
    """Run flat + object, then post-process. Returns produced PNG paths."""
    if not Path(cfg.stl).exists():
        raise FileNotFoundError(f"STL not found: {cfg.stl}")

    out = cfg.out_path
    if clean and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    config_path = cfg.to_json(out / "config.json")

    _log(progress, "[1/3] flat field (no object) ...")
    _run_worker(config_path, "flat", progress)

    _log(progress, "[2/3] object (with STL) ...")
    _run_worker(config_path, "object", progress)

    _log(progress, "[3/3] post-processing ...")
    return postprocess(cfg, progress)


# ----------------------------------------------------------------------
# Post-processing (no GATE engine needed)
# ----------------------------------------------------------------------

def postprocess(cfg: SimConfig, progress: ProgressFn | None = None) -> RunResult:
    out = cfg.out_path
    flat_mhd = out / "flat" / "I.mhd"
    obj_mhd = out / "object" / "I.mhd"
    if not flat_mhd.exists() or not obj_mhd.exists():
        raise FileNotFoundError(
            f"Missing fluence outputs.\n  flat: {flat_mhd} (exists={flat_mhd.exists()})"
            f"\n  object: {obj_mhd} (exists={obj_mhd.exists()})")

    ref_img, _ = imaging.read_mhd(obj_mhd)
    I0 = imaging.read_mhd_2d(flat_mhd)
    I = imaging.read_mhd_2d(obj_mhd)

    eps = 1e-12
    T = (I + eps) / (I0 + eps)
    A = np.nan_to_num(-np.log(T), nan=0.0, posinf=0.0, neginf=0.0)

    imaging.write_mhd_like(ref_img, T[None], out / "transmission.mhd")
    imaging.write_mhd_like(ref_img, A[None], out / "attenuation.mhd")

    result = RunResult(out_dir=out)
    if cfg.make_png:
        result.images["flat (log I0)"] = imaging.save_png(I0, out / "flat_log1pI0.png", "flat log1p(I0)", use_log=True)
        result.images["object (log I)"] = imaging.save_png(I, out / "object_log1pI.png", "object log1p(I)", use_log=True)
        result.images["attenuation"] = imaging.save_png(A, out / "attenuation.png", "attenuation = -ln(I/I0)")
        result.images["film-like"] = imaging.save_film_like(
            T, out / "film_like_AP.png",
            clip_lo=cfg.film_clip_lo, clip_hi=cfg.film_clip_hi,
            gamma=cfg.film_gamma, blur_passes=cfg.film_blur_passes, invert=False)

    if cfg.separate_primary_scatter:
        _postprocess_primary_scatter(cfg, ref_img, eps, result, progress)

    _log(progress, f"Done. Outputs in {out}")
    return result


def _postprocess_primary_scatter(cfg, ref_img, eps, result: RunResult,
                                 progress: ProgressFn | None) -> None:
    out = cfg.out_path
    ph_flat = out / "flat" / "phsp.root"
    ph_obj = out / "object" / "phsp.root"
    if not ph_flat.exists() or not ph_obj.exists():
        raise FileNotFoundError("Missing phsp.root; re-run with write_phsp enabled.")

    _log(progress, "  separating primary / scatter (flat) ...")
    flat = phasespace.compute(ph_flat, out / "flat", cfg, write=True)
    _log(progress, "  separating primary / scatter (object) ...")
    obj = phasespace.compute(ph_obj, out / "object", cfg, write=True)
    result.primary_fraction = obj.primary_fraction

    Tp = (obj.I_primary + eps) / (flat.I_primary + eps)
    Ap = np.nan_to_num(-np.log(Tp), nan=0.0, posinf=0.0, neginf=0.0)
    Itot, I0tot = obj.I_primary + obj.I_scatter, flat.I_primary + flat.I_scatter
    Ttot = (Itot + eps) / (I0tot + eps)
    Atot = np.nan_to_num(-np.log(Ttot), nan=0.0, posinf=0.0, neginf=0.0)

    imaging.write_mhd_like(ref_img, Tp[None], out / "transmission_primary.mhd")
    imaging.write_mhd_like(ref_img, Ap[None], out / "attenuation_primary.mhd")
    imaging.write_mhd_like(ref_img, Ttot[None], out / "transmission_total.mhd")
    imaging.write_mhd_like(ref_img, Atot[None], out / "attenuation_total.mhd")

    if cfg.make_png:
        result.images["SPR (object)"] = imaging.save_png(obj.SPR, out / "SPR_object.png", "SPR (object)")
        result.images["attenuation (primary)"] = imaging.save_png(Ap, out / "attenuation_primary.png", "attenuation primary-like")
        result.images["attenuation (total)"] = imaging.save_png(Atot, out / "attenuation_total.png", "attenuation total")
