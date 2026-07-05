"""Primary / scatter separation from a phase-space (ROOT) file.

A photon recorded at the film is classified as PRIMARY-like when, at the film,
its direction is consistent with a straight source->hit ray (angle <= theta)
AND its energy is close to the mono-energy (|E - E0| <= dE). Everything else is
SCATTER-like. SPR = scatter / primary.

This is a practical truth-free separation that works well for a point source and
a mono-energetic beam; tighten/loosen the thresholds in SimConfig.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import SimConfig
from . import imaging

try:
    import uproot
except Exception:  # pragma: no cover
    uproot = None


@dataclass
class PrimaryScatterResult:
    I_primary: np.ndarray
    I_scatter: np.ndarray
    SPR: np.ndarray
    primary_fraction: float


def _open_first_tree(root_path: Path):
    if uproot is None:
        raise RuntimeError("uproot is required to read phsp.root. Install: pip install uproot")
    f = uproot.open(str(root_path))
    for key in f.keys():
        try:
            obj = f[key]
            if hasattr(obj, "keys") and hasattr(obj, "arrays"):
                return obj
        except Exception:
            continue
    raise RuntimeError(f"No readable tree found in {root_path}")


def _get_vec3(tree, base: str):
    """Read a 3-vector branch, whether stored split (base_X/Y/Z) or packed."""
    keys = set(tree.keys())
    if all(f"{base}_{c}" in keys for c in "XYZ"):
        return tuple(tree[f"{base}_{c}"].array(library="np") for c in "XYZ")
    if base in keys:
        a = np.asarray(tree[base].array(library="np"))
        if a.ndim == 2 and a.shape[1] == 3:
            return a[:, 0], a[:, 1], a[:, 2]
    raise KeyError(f"Cannot find {base} or {base}_X/Y/Z. Branches: {sorted(keys)[:30]}...")


def _read_energy_keV(tree, energy0_keV: float) -> np.ndarray:
    keys = set(tree.keys())
    ekey = next((c for c in ("KineticEnergy", "KineticEnergy_keV", "Energy", "TotalEnergy")
                 if c in keys), None)
    if ekey is None:
        raise KeyError("Cannot find an energy branch (expected KineticEnergy)")
    E = tree[ekey].array(library="np").astype(np.float64)
    med = float(np.nanmedian(E)) if len(E) else 0.0
    # GATE/Geant4 often stores MeV; convert if values look like MeV.
    if energy0_keV > 10.0 and 0.001 < med < 10.0:
        E = E * 1000.0
        print(f"[phsp] Detected energy in MeV (median {med:.6g}); converted to keV.")
    else:
        print(f"[phsp] Energy median = {med:.6g} (assuming keV).")
    return E


def _counts_from_root(phsp_root: Path, cfg: SimConfig) -> tuple[np.ndarray, np.ndarray]:
    """Bin one phase-space file into (I_primary, I_scatter) count images."""
    pix = int(cfg.pix)
    film_xy = float(cfg.film_xy)
    film_z = float(cfg.odd)

    tree = _open_first_tree(phsp_root)
    E = _read_energy_keV(tree, float(cfg.energy_keV))

    px, py, pz = (np.asarray(a, dtype=np.float64) for a in _get_vec3(tree, "Position"))
    dx, dy, dz = (np.asarray(a, dtype=np.float64) for a in _get_vec3(tree, "Direction"))

    # Position may be stored in WORLD (z ~ +odd) or LOCAL film coords (z ~ 0).
    med_pz = float(np.nanmedian(pz)) if len(pz) else 0.0
    z_tol = max(5.0 * float(cfg.film_thickness), 2.0)
    is_local = abs(med_pz) <= z_tol and abs(med_pz - film_z) > z_tol
    is_world = abs(med_pz - film_z) <= z_tol
    if is_local and not is_world:
        pz = pz + film_z
        print(f"[phsp] LOCAL film coords (median z={med_pz:.3f}); shifted by +{film_z:.3f} mm.")
    else:
        mode = "WORLD" if is_world else "UNKNOWN"
        print(f"[phsp] Position z looks {mode} (median z={med_pz:.3f}, expected ~{film_z:.3f}).")

    # Unit direction of the recorded photon.
    dnorm = np.sqrt(dx * dx + dy * dy + dz * dz) + 1e-30
    ux, uy, uz = dx / dnorm, dy / dnorm, dz / dnorm

    # Ideal (unscattered) direction: source -> hit.
    sx, sy, sz = cfg.source_pos_mm
    vx, vy, vz = px - sx, py - sy, pz - sz
    vnorm = np.sqrt(vx * vx + vy * vy + vz * vz) + 1e-30
    cos_ang = np.clip((ux * vx + uy * vy + uz * vz) / vnorm, -1.0, 1.0)
    ang = np.degrees(np.arccos(cos_ang))

    primary_mask = (ang <= float(cfg.primary_theta_deg)) & \
                   (np.abs(E - float(cfg.energy_keV)) <= float(cfg.primary_dE_keV))
    primary_fraction = float(np.mean(primary_mask)) if len(ang) else 0.0
    print(f"[phsp] Primary-like fraction: {primary_fraction:.6f} "
          f"(theta<={cfg.primary_theta_deg} deg, |dE|<={cfg.primary_dE_keV} keV)")

    # Bin hits onto the detector grid.
    half = 0.5 * film_xy
    pix_size = film_xy / pix
    ix = np.floor((px + half) / pix_size).astype(np.int64)
    iy = np.floor((py + half) / pix_size).astype(np.int64)
    inside = (ix >= 0) & (ix < pix) & (iy >= 0) & (iy < pix) & np.isfinite(E)

    def _accumulate(mask) -> np.ndarray:
        img = np.zeros((pix, pix), dtype=np.float64)
        m = inside & mask
        if np.any(m):
            lin = iy[m] * pix + ix[m]
            img += np.bincount(lin, minlength=pix * pix).astype(np.float64).reshape((pix, pix))
        return img

    I_primary = _accumulate(primary_mask)
    I_scatter = _accumulate(~primary_mask)
    return I_primary, I_scatter


def _finalize(I_primary: np.ndarray, I_scatter: np.ndarray,
              out_dir: Path, cfg: SimConfig, write: bool) -> PrimaryScatterResult:
    pix = int(cfg.pix)
    pix_size = float(cfg.film_xy) / pix
    # SPR is undefined where no primary photons reached the pixel. Those zeros are
    # almost all low-statistics / off-field artefacts; dividing by a tiny epsilon
    # there produces ~1e12 spikes that swamp the analysis. Set SPR = 0 instead.
    SPR = np.divide(I_scatter, I_primary,
                    out=np.zeros((pix, pix), dtype=np.float64),
                    where=(I_primary > 0))
    tot = float(I_primary.sum() + I_scatter.sum())
    primary_fraction = float(I_primary.sum() / tot) if tot > 0 else 0.0
    print(f"[phsp] Primary-like fraction (in-field): {primary_fraction:.6f}")

    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        ref = imaging.make_reference_image(pix, pix_size)
        imaging.write_mhd_like(ref, I_primary[None], out_dir / "I_primary.mhd")
        imaging.write_mhd_like(ref, I_scatter[None], out_dir / "I_scatter.mhd")
        imaging.write_mhd_like(ref, SPR[None], out_dir / "SPR.mhd")
        if cfg.make_png:
            imaging.save_png(I_primary, out_dir / "I_primary_log1p.png", "primary-like log1p", use_log=True)
            imaging.save_png(I_scatter, out_dir / "I_scatter_log1p.png", "scatter-like log1p", use_log=True)
            imaging.save_png(SPR, out_dir / "SPR.png", "SPR = scatter/primary")

    return PrimaryScatterResult(I_primary, I_scatter, SPR, primary_fraction)


def compute(phsp_root: str | Path, out_dir: str | Path, cfg: SimConfig,
            write: bool = True) -> PrimaryScatterResult:
    """Build I_primary, I_scatter and SPR images from one phase-space file."""
    I_primary, I_scatter = _counts_from_root(Path(phsp_root), cfg)
    return _finalize(I_primary, I_scatter, Path(out_dir), cfg, write)


def compute_multi(phsp_roots, out_dir: str | Path, cfg: SimConfig,
                  write: bool = True) -> PrimaryScatterResult:
    """Same as compute() but sums counts across several shard phase-space files.

    Independent Monte-Carlo shards are additive, so summing primary/scatter
    counts is exactly equivalent to one long run with the combined statistics.
    """
    paths = [Path(p) for p in phsp_roots]
    if not paths:
        raise FileNotFoundError("no phase-space files to combine")
    if len(paths) == 1:
        return compute(paths[0], out_dir, cfg, write)
    pix = int(cfg.pix)
    I_primary = np.zeros((pix, pix), dtype=np.float64)
    I_scatter = np.zeros((pix, pix), dtype=np.float64)
    for p in paths:
        a, b = _counts_from_root(p, cfg)
        I_primary += a
        I_scatter += b
    return _finalize(I_primary, I_scatter, Path(out_dir), cfg, write)
