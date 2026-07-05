"""GATE 10 simulation builder — the ONLY module that imports opengate.

OpenGATE allows a single SimulationEngine per process, so this module builds and
runs exactly one phase ("flat" = no object, or "object" = with the STL mesh).
The orchestration of flat+object lives in pipeline.py, which launches this in a
fresh subprocess for each phase (see _worker.py).
"""

from __future__ import annotations

import math
from pathlib import Path

from .config import SimConfig


def _center_translation_from_stl(stl_path: Path):
    """Translation that moves the mesh bounding-box centre to the origin."""
    try:
        import trimesh
    except Exception:
        print("WARNING: trimesh not installed; cannot auto-center STL (pip install trimesh).")
        return None
    mesh = trimesh.load(str(stl_path), force="mesh")
    if mesh.is_empty:
        print("WARNING: STL mesh is empty; cannot center.")
        return None
    lo, hi = mesh.bounds
    c = (lo + hi) / 2.0
    return (-float(c[0]), -float(c[1]), -float(c[2]))


def _beam_rotation(src_world) -> "np.ndarray":
    """Rotation ที่หมุน world frame → beam frame (แกนลำแสงกลายเป็น +z).

    ลำแสงวิ่งจาก source เข้าหา origin: แกน u = -src/|src|. คืนเมทริกซ์ R ที่
    R @ u = ez (Rodrigues). การย้าย source รอบวัตถุจึงเทียบเท่าการหมุนวัตถุ
    ในทางฟิสิกส์ทุกประการ — engine คงยิงตาม +z และฉากรับอยู่หลังวัตถุเสมอ.
    """
    import numpy as np
    src = np.asarray(src_world, dtype=np.float64)
    norm = float(np.linalg.norm(src))
    if norm < 50.0:
        raise ValueError(f"source ใกล้ศูนย์กลางวัตถุเกินไป ({norm:.1f} mm < 50 mm)")
    u = -src / norm                      # ทิศลำแสง (source → origin)
    e = np.array([0.0, 0.0, 1.0])
    c = float(np.dot(u, e))
    if c > 1.0 - 1e-12:                  # ตรงแนว +z อยู่แล้ว
        return np.eye(3)
    if c < -1.0 + 1e-12:                 # สวนแนวพอดี → หมุน 180° รอบแกน x
        return np.diag([1.0, -1.0, -1.0])
    v = np.cross(u, e)
    s2 = float(np.dot(v, v))
    K = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])
    return np.eye(3) + K + K @ K * ((1.0 - c) / s2)


def build_simulation(cfg: SimConfig, out_dir: Path, with_object: bool,
                     photons: int | None = None, seed: int | None = None):
    """Construct (but do not run) a GATE Simulation for one phase.

    photons/seed override cfg when running a shard (process-level parallelism).
    """
    import os
    import opengate as gate

    mm = gate.g4_units.mm
    m = gate.g4_units.m
    keV = gate.g4_units.keV
    deg = gate.g4_units.deg

    sim = gate.Simulation()
    sim.output_dir = str(out_dir)
    sim.visu = False
    # Geant4 multithreading is unavailable on Windows; parallelism comes from
    # running multiple shard PROCESSES instead. Keep each engine single-threaded.
    sim.number_of_threads = 1 if os.name == "nt" else int(cfg.threads)
    if seed is not None:
        sim.random_seed = int(seed)

    # World
    sim.world.size = [2.0 * m, 2.0 * m, 2.0 * m]
    sim.world.material = "G4_AIR"

    # Detector / film plane at z = +odd
    film = sim.add_volume("Box", "film")
    film.mother = sim.world.name
    film.size = [cfg.film_xy * mm, cfg.film_xy * mm, cfg.film_thickness * mm]
    film.translation = [0.0, 0.0, float(cfg.odd) * mm]
    film.material = "G4_AIR"

    # Beam-frame transform: หมุน world → beam frame ตามตำแหน่ง source 3 มิติ
    # (identity ถ้า source อยู่บนแกน -z แบบเดิม)
    import numpy as np
    R_beam = _beam_rotation(cfg.source_world_mm)

    # Object mesh (only for the "object" phase)
    if with_object:
        stl_abs = str(Path(cfg.stl).resolve())
        try:
            pelvis = sim.add_volume("Tesselated", "pelvis")
        except Exception:
            pelvis = sim.add_volume("TesselatedVolume", "pelvis")
        pelvis.mother = sim.world.name
        pelvis.material = cfg.object_material
        pelvis.translation = [0.0, 0.0, 0.0]
        pelvis.file_name = stl_abs

        # การวางใน GATE: world_p = R @ local_p + T
        # ต้องการ: จัดกลาง (local_p + t_center) → หมุนของผู้ใช้ → หมุนเข้า beam frame
        # ⇒ R_total = R_beam @ R_user, T = R_total @ t_center
        R_user = _rotation_matrix(cfg.rot_x, cfg.rot_y, cfg.rot_z) \
            if any(abs(r) > 1e-9 for r in (cfg.rot_x, cfg.rot_y, cfg.rot_z)) else np.eye(3)
        R_total = R_beam @ R_user
        if not np.allclose(R_total, np.eye(3)):
            pelvis.rotation = R_total

        if cfg.center_mesh:
            t = _center_translation_from_stl(Path(cfg.stl))
            if t is not None:
                t_rot = R_total @ np.asarray(t, dtype=np.float64)
                pelvis.translation = [t_rot[0] * mm, t_rot[1] * mm, t_rot[2] * mm]
                print(f"Centering mesh (beam frame): translation = {pelvis.translation}")

    # Physics
    sim.physics_manager.physics_list_name = "G4EmLivermorePhysics"

    # Point source aimed at the film, mono-energetic
    # (ใน beam frame: source อยู่บนแกน -z ที่ระยะ sod_eff เสมอ)
    src = sim.add_source("GenericSource", "src")
    src.particle = "gamma"
    src.n = int(cfg.photons if photons is None else photons)
    src.position.type = "point"
    src.position.translation = [0.0, 0.0, -cfg.sod_eff * mm]

    # Collimation: จำกัดกรวยลำแสงตามขนาดสนามที่ระนาบฉากรับ
    # field_mm <= 0 → เต็มฟิล์ม (ครอบมุมฟิล์มเหมือนเดิม)
    half_diag = 0.5 * float(cfg.film_xy) * math.sqrt(2.0)
    half_field = half_diag if float(cfg.field_mm) <= 0 else \
        min(max(0.5 * float(cfg.field_mm), 0.5), half_diag)
    alpha = math.degrees(math.atan(half_field / float(cfg.sid)))
    src.direction.type = "iso"
    src.direction.phi = [0 * deg, 360 * deg]
    src.direction.theta = [(180.0 - alpha) * deg, 180.0 * deg]
    # opengate >= 10.1 renamed angle_acceptance_volume -> angular_acceptance
    if hasattr(src.direction, "angular_acceptance"):
        src.direction.angular_acceptance.target_volumes = ["film"]
        src.direction.angular_acceptance.enable_intersection_check = True
    else:
        src.direction.angle_acceptance_volume = "film"
    src.energy.type = "mono"
    src.energy.mono = float(cfg.energy_keV) * keV

    # Total fluence image -> I.mhd
    fl = sim.add_actor("FluenceActor", "I")
    fl.attached_to = film.name
    fl.output_filename = "I.mhd"
    fl.spacing = [cfg.pixel_size_mm * mm, cfg.pixel_size_mm * mm, cfg.film_thickness * mm]
    fl.size = [int(cfg.pix), int(cfg.pix), 1]
    fl.translation = [0.0, 0.0, 0.0]
    fl.hit_type = "random"

    # Phase space at the film (for primary/scatter separation)
    if cfg.write_phsp or cfg.separate_primary_scatter:
        ph = sim.add_actor("PhaseSpaceActor", "phsp")
        ph.attached_to = film.name
        ph.output_filename = "phsp.root"
        ph.attributes = ["KineticEnergy", "Position", "Direction",
                         "ParticleName", "EventID", "TrackID"]
        ph.steps_to_store = "first"

    st = sim.add_actor("SimulationStatisticsActor", "stats")
    st.output_filename = "stats.txt"
    return sim


def _split_photons(total: int, n_shards: int, shard: int) -> int:
    """Distribute `total` photons across shards as evenly as possible."""
    base, extra = divmod(int(total), int(n_shards))
    return base + (1 if shard < extra else 0)


def run_phase(cfg: SimConfig, phase: str, shard: int = 0, n_shards: int = 1,
              out_dir: Path | None = None) -> Path:
    """Build and run one phase ('flat' or 'object'), optionally as one shard.

    When n_shards > 1 the caller runs several of these as separate processes,
    each with a slice of the photons and a distinct seed, then merges the output.
    """
    if phase not in ("flat", "object"):
        raise ValueError(f"phase must be 'flat' or 'object', got {phase!r}")
    if out_dir is None:
        out_dir = cfg.out_path / phase
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    photons = _split_photons(cfg.photons, n_shards, shard) if n_shards > 1 else int(cfg.photons)
    seed = int(cfg.random_seed) + int(shard)
    sim = build_simulation(cfg, out_dir, with_object=(phase == "object"),
                           photons=photons, seed=seed)
    tag = f"{phase}" if n_shards == 1 else f"{phase} shard {shard + 1}/{n_shards}"
    print(f"\n=== Running {tag} ({photons} photons, seed {seed}) ===")
    sim.run()
    print(f"=== Done {tag} ===\n")
    return out_dir


def _rotation_matrix(rx_deg: float, ry_deg: float, rz_deg: float):
    """3x3 rotation = Rz @ Ry @ Rx (degrees)."""
    import numpy as np
    rx, ry, rz = map(math.radians, (rx_deg, ry_deg, rz_deg))
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx
