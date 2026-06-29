"""Simulation configuration — a single, serializable source of truth.

All run parameters live in one dataclass so they can be passed around the
package, written to JSON, and handed to the subprocess worker unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np


@dataclass
class SimConfig:
    """Parameters for one GATE 10 pelvis-radiograph simulation."""

    # --- Required input ---
    stl: str                              # path to STL mesh (mm units)
    out: str                              # output directory

    # --- Statistics ---
    photons: int = 1_000_000              # number of primary photons (events)
    threads: int = 1                      # Geant4 threads

    # --- Geometry (AP projection along +z) ---
    sod: float = 800.0                    # source-to-object distance (mm); source at z=-sod
    odd: float = 400.0                    # object-to-detector distance (mm); film at z=+odd
    film_xy: float = 400.0                # detector size in x and y (mm)
    film_thickness: float = 1.0           # detector thickness (mm)
    pix: int = 512                        # detector pixels per side (square)

    # --- Physics ---
    energy_keV: float = 80.0              # mono-energetic beam (keV)
    object_material: str = "G4_BONE_COMPACT_ICRU"

    # --- Mesh placement ---
    center_mesh: bool = True              # auto-center STL bbox at origin (needs trimesh)
    rot_x: float = 0.0
    rot_y: float = 0.0
    rot_z: float = 0.0

    # --- Outputs ---
    make_png: bool = True                 # write PNG previews
    write_phsp: bool = True               # write phase-space ROOT at film
    separate_primary_scatter: bool = True # post-process phsp into primary/scatter/SPR
    primary_theta_deg: float = 0.5        # primary-like: max angle vs source->hit ray
    primary_dE_keV: float = 1.0           # primary-like: max |E - E0|

    # --- Film-like rendering ---
    film_clip_lo: float = 0.5
    film_clip_hi: float = 99.5
    film_gamma: float = 0.85
    film_blur_passes: int = 2

    # ----- Derived geometry helpers -----
    @property
    def sid(self) -> float:
        """Source-to-image distance (mm)."""
        return self.sod + self.odd

    @property
    def pixel_size_mm(self) -> float:
        return self.film_xy / self.pix

    @property
    def source_pos_mm(self) -> np.ndarray:
        return np.array([0.0, 0.0, -float(self.sod)], dtype=np.float64)

    @property
    def out_path(self) -> Path:
        return Path(self.out).resolve()

    # ----- Serialization -----
    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, d: dict) -> "SimConfig":
        # ignore unknown keys so old/new configs stay compatible
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def from_json(cls, path: str | Path) -> "SimConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
