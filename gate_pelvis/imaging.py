"""Image I/O and rendering helpers (.mhd volumes + PNG previews).

Heavy/optional deps (SimpleITK, matplotlib) are imported lazily so the rest of
the package — and the UI — still load when only post-processing is needed.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

try:
    import SimpleITK as sitk
except Exception:  # pragma: no cover
    sitk = None

try:
    import matplotlib
    matplotlib.use("Agg")  # headless: safe inside Streamlit / subprocess
    import matplotlib.pyplot as plt
except Exception:  # pragma: no cover
    plt = None


def require_sitk():
    if sitk is None:
        raise RuntimeError("SimpleITK is required for .mhd I/O. Install: pip install SimpleITK")
    return sitk


# ----------------------------------------------------------------------
# .mhd volume I/O
# ----------------------------------------------------------------------

def read_mhd(path: str | Path):
    """Return (sitk_image, numpy_array[z, y, x])."""
    img = require_sitk().ReadImage(str(path))
    return img, sitk.GetArrayFromImage(img)


def read_mhd_2d(path: str | Path) -> np.ndarray:
    """Read a single-slice .mhd and return the 2D array."""
    _, arr = read_mhd(path)
    return np.asarray(arr[0], dtype=np.float64)


def write_mhd_like(ref_img, arr_zyx: np.ndarray, out_path: str | Path) -> Path:
    """Write a numpy array as .mhd copying geometry from a reference image."""
    sitk_ = require_sitk()
    out = sitk_.GetImageFromArray(np.asarray(arr_zyx, dtype=np.float32))
    out.CopyInformation(ref_img)
    sitk_.WriteImage(out, str(out_path), useCompression=True)
    return Path(out_path)


def make_reference_image(pix: int, pixel_size_mm: float):
    """A blank single-slice reference for film-plane images."""
    sitk_ = require_sitk()
    ref = sitk_.GetImageFromArray(np.zeros((1, pix, pix), dtype=np.float32))
    half = 0.5 * pix * pixel_size_mm
    ref.SetSpacing((pixel_size_mm, pixel_size_mm, 1.0))
    ref.SetOrigin((-half, -half, 0.0))
    return ref


# ----------------------------------------------------------------------
# PNG previews
# ----------------------------------------------------------------------

def save_png(img2d: np.ndarray, out_png: str | Path, title: str | None = None,
             use_log: bool = False) -> Path | None:
    """Grayscale PNG of a 2D array (optionally log1p-scaled)."""
    if plt is None:
        print("WARNING: matplotlib not available -> skip PNG output")
        return None
    a = np.asarray(img2d, dtype=np.float64)
    if use_log:
        a = np.log1p(np.clip(a, 0, None))
    plt.figure()
    plt.imshow(a, cmap="gray")
    plt.axis("off")
    if title:
        plt.title(title)
    plt.tight_layout()
    plt.savefig(str(out_png), dpi=200, bbox_inches="tight", pad_inches=0)
    plt.close()
    return Path(out_png)


def _box_blur_3x3(img: np.ndarray) -> np.ndarray:
    return (img +
            np.roll(img, 1, 0) + np.roll(img, -1, 0) +
            np.roll(img, 1, 1) + np.roll(img, -1, 1) +
            np.roll(np.roll(img, 1, 0), 1, 1) +
            np.roll(np.roll(img, 1, 0), -1, 1) +
            np.roll(np.roll(img, -1, 0), 1, 1) +
            np.roll(np.roll(img, -1, 0), -1, 1)) / 9.0


def save_film_like(ratio2d: np.ndarray, out_png: str | Path,
                   clip_lo: float = 0.5, clip_hi: float = 99.5,
                   gamma: float = 0.85, blur_passes: int = 2,
                   invert: bool = False) -> Path | None:
    """Render a transmission ratio as a clinical-looking radiograph PNG."""
    if plt is None:
        print("WARNING: matplotlib not available -> skip film-like PNG output")
        return None

    T = np.asarray(ratio2d, dtype=np.float32)
    T = np.where(np.isfinite(T), T, 1.0)
    T = np.clip(T, 0.0, 1.5)
    img = 1.0 - T

    for _ in range(max(0, int(blur_passes))):
        img = _box_blur_3x3(img)

    v = img[np.isfinite(img)]
    lo = np.percentile(v, float(clip_lo))
    hi = np.percentile(v, float(clip_hi))
    img = np.clip(img, lo, hi)
    img = (img - lo) / (hi - lo + 1e-12)
    img = np.power(img, float(gamma))
    if invert:
        img = 1.0 - img

    plt.figure()
    plt.imshow(img, cmap="gray", vmin=0, vmax=1)
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(str(out_png), dpi=200, bbox_inches="tight", pad_inches=0)
    plt.close()
    return Path(out_png)
