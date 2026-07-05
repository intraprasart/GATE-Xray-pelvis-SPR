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
    threads: int = 1                      # Geant4 threads (Windows: forced to 1 — MT unsupported)

    # --- Parallelism (Windows has no Geant4 MT, so we shard across processes) ---
    mode: str = "single"                  # "single" | "balanced" | "max" — resolved to n_procs
    n_procs: int = 1                      # explicit process count (overrides mode when > 1)
    ram_per_proc_mb: int = 1500           # RAM budget per shard (~800MB measured + headroom); caps n_procs
    random_seed: int = 1234567            # base seed; shard k uses random_seed + k

    # --- Geometry (AP projection along +z) ---
    sod: float = 800.0                    # source-to-object distance (mm); source at z=-sod
    odd: float = 400.0                    # object-to-detector distance (mm); film at z=+odd
    film_xy: float = 400.0                # detector size in x and y (mm)
    film_thickness: float = 1.0           # detector thickness (mm)
    pix: int = 512                        # detector pixels per side (square)

    # --- Physics ---
    energy_keV: float = 80.0              # mono-energetic beam (keV)
    object_material: str = "G4_BONE_COMPACT_ICRU"

    # --- Source position (3D) + collimation (v1.1) ---
    # ตำแหน่ง source ใน world frame (mm) — None = ค่าเดิม (0, 0, -sod)
    # ลำแสงเล็งเข้าศูนย์กลางวัตถุ (origin) เสมอ และฉากรับตั้งฉากกับแนวลำแสง
    # อยู่หลังวัตถุที่ระยะ odd โดยอัตโนมัติ (ผ่าน beam-frame transform ใน engine)
    src_x: float | None = None
    src_y: float | None = None
    src_z: float | None = None
    field_mm: float = 0.0                 # เส้นผ่านศูนย์กลางลำแสงที่ระนาบฉากรับ; <=0 = เต็มฟิล์ม

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
    def source_world_mm(self) -> np.ndarray:
        """ตำแหน่ง source ใน world frame ตามที่ผู้ใช้กำหนด (default: บนแกน -z)"""
        if self.src_x is None or self.src_y is None or self.src_z is None:
            return np.array([0.0, 0.0, -float(self.sod)], dtype=np.float64)
        return np.array([float(self.src_x), float(self.src_y), float(self.src_z)],
                        dtype=np.float64)

    @property
    def sod_eff(self) -> float:
        """ระยะ source→ศูนย์กลางวัตถุจริง (mm) — ขึ้นกับตำแหน่ง source 3D"""
        return float(np.linalg.norm(self.source_world_mm))

    @property
    def sid(self) -> float:
        """Source-to-image distance (mm)."""
        return self.sod_eff + self.odd

    @property
    def pixel_size_mm(self) -> float:
        return self.film_xy / self.pix

    @property
    def source_pos_mm(self) -> np.ndarray:
        """ตำแหน่ง source ใน BEAM frame (ที่ engine/phasespace ใช้จริง):
        แกนลำแสงถูกหมุนให้เป็น +z เสมอ → source อยู่ที่ (0,0,-sod_eff)"""
        return np.array([0.0, 0.0, -self.sod_eff], dtype=np.float64)

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


def available_ram_mb() -> float:
    """Physical RAM available now (MB), cross-platform, no hard deps."""
    try:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = _MS()
        st.dwLength = ctypes.sizeof(_MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
            return st.ullAvailPhys / (1024 * 1024)
    except Exception:
        pass
    try:  # POSIX
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 4096.0  # unknown → assume a modest 4 GB


def resolve_n_procs(cfg: "SimConfig", cpu: int | None = None,
                    avail_mb: float | None = None) -> int:
    """Turn (mode / n_procs) + live RAM into a safe concrete process count."""
    import os as _os
    cpu = cpu or _os.cpu_count() or 1
    avail_mb = available_ram_mb() if avail_mb is None else avail_mb

    if int(cfg.n_procs) > 1:
        want = int(cfg.n_procs)
    else:
        want = {"single": 1, "balanced": max(1, cpu - 2), "max": cpu}.get(cfg.mode, 1)

    ram_cap = max(1, int(avail_mb * 0.85 / max(256, int(cfg.ram_per_proc_mb))))
    return max(1, min(want, cpu, ram_cap))
