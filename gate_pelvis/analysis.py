"""ROI comparison of two SPR maps (e.g. control vs fracture).

Reproduces the metrics from the original interactive analysis:
  - dSPR          : mean SPR difference inside the ROI
  - dSTD_local    : mean difference of local standard deviation on high-pass SPR
  - dEntropy_local: mean difference of local rank-entropy on high-pass SPR

High-pass = SPR minus a Gaussian-blurred SPR (removes smooth anatomy, keeps
fine texture introduced by a fracture). Local windows default to 15 px.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import imaging

try:
    from scipy.ndimage import gaussian_filter, uniform_filter
except Exception:  # pragma: no cover
    gaussian_filter = uniform_filter = None

try:
    from skimage.filters.rank import entropy as _rank_entropy
    from skimage.morphology import disk as _disk
except Exception:  # pragma: no cover
    _rank_entropy = _disk = None


@dataclass
class ROI:
    x0: int
    y0: int
    w: int
    h: int

    def slices(self):
        return (slice(self.y0, self.y0 + self.h), slice(self.x0, self.x0 + self.w))


@dataclass
class CompareResult:
    roi: ROI
    summary: dict
    maps: dict[str, np.ndarray] = field(default_factory=dict)  # label -> 2D array

    def save(self, out_dir: str | Path) -> Path:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for label, arr in self.maps.items():
            imaging.save_png(arr, out_dir / f"{label}.png", title=label)
        with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["metric", "value"])
            for k, v in self.summary.items():
                w.writerow([k, v])
        return out_dir


def _require_deps():
    if gaussian_filter is None or uniform_filter is None:
        raise RuntimeError("scipy is required for analysis. Install: pip install scipy")
    if _rank_entropy is None:
        raise RuntimeError("scikit-image is required for entropy. Install: pip install scikit-image")


def _highpass(spr: np.ndarray, sigma: float = 3.0) -> np.ndarray:
    return spr - gaussian_filter(spr, sigma=sigma)


def _local_std(hp: np.ndarray, win: int = 15) -> np.ndarray:
    mean = uniform_filter(hp, size=win)
    mean_sq = uniform_filter(hp * hp, size=win)
    return np.sqrt(np.clip(mean_sq - mean * mean, 0, None))


def _local_entropy(hp: np.ndarray, win: int = 15) -> np.ndarray:
    v = hp - hp.min()
    vmax = v.max()
    u8 = (v / vmax * 255.0).astype(np.uint8) if vmax > 0 else v.astype(np.uint8)
    return _rank_entropy(u8, _disk(win // 2))


def _auto_roi(diff: np.ndarray, w: int, h: int) -> ROI:
    """Centre a w*h box on the strongest smoothed |difference|."""
    smoothed = gaussian_filter(np.abs(diff), sigma=max(2.0, min(w, h) / 4.0))
    cy, cx = np.unravel_index(int(np.argmax(smoothed)), smoothed.shape)
    H, W = diff.shape
    x0 = int(np.clip(cx - w // 2, 0, W - w))
    y0 = int(np.clip(cy - h // 2, 0, H - h))
    return ROI(x0, y0, w, h)


def compare_runs(control_dir: str | Path, fracture_dir: str | Path,
                 roi: ROI | None = None, roi_size: tuple[int, int] = (53, 52),
                 hp_sigma: float = 3.0, win: int = 15) -> CompareResult:
    """Compare object/SPR.mhd of two runs and return metrics + maps."""
    _require_deps()
    spr_c = imaging.read_mhd_2d(Path(control_dir) / "object" / "SPR.mhd")
    spr_f = imaging.read_mhd_2d(Path(fracture_dir) / "object" / "SPR.mhd")
    if spr_c.shape != spr_f.shape:
        raise ValueError(f"SPR shapes differ: control {spr_c.shape} vs fracture {spr_f.shape}")

    dspr_map = spr_f - spr_c
    if roi is None:
        roi = _auto_roi(dspr_map, *roi_size)
    sl = roi.slices()

    hp_c, hp_f = _highpass(spr_c, hp_sigma), _highpass(spr_f, hp_sigma)
    std_c, std_f = _local_std(hp_c, win), _local_std(hp_f, win)
    ent_c, ent_f = _local_entropy(hp_c, win), _local_entropy(hp_f, win)

    summary = {
        "dSPR_mean_in_ROI": float(np.mean(dspr_map[sl])),
        "dSTD_local_mean_in_ROI": float(np.mean((std_f - std_c)[sl])),
        "dEntropy_local_mean_in_ROI": float(np.mean((ent_f - ent_c)[sl])),
        "ROI_x0": roi.x0, "ROI_y0": roi.y0, "ROI_w": roi.w, "ROI_h": roi.h,
        "highpass_sigma": hp_sigma, "local_window": win,
        "note": "single-seed deltas; run multiple seeds for significance (mean/std)",
    }
    maps = {
        "SPR_control": spr_c, "SPR_fracture": spr_f,
        "dSPR_fracture_minus_control": dspr_map,
        "localSTD_control": std_c, "localSTD_fracture": std_f,
        "dSTD_fracture_minus_control": std_f - std_c,
        "entropy_control": ent_c, "entropy_fracture": ent_f,
        "dEntropy_fracture_minus_control": ent_f - ent_c,
    }
    return CompareResult(roi=roi, summary=summary, maps=maps)
