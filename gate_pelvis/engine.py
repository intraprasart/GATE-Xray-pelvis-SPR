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


def build_simulation(cfg: SimConfig, out_dir: Path, with_object: bool):
    """Construct (but do not run) a GATE Simulation for one phase."""
    import opengate as gate

    mm = gate.g4_units.mm
    m = gate.g4_units.m
    keV = gate.g4_units.keV
    deg = gate.g4_units.deg

    sim = gate.Simulation()
    sim.output_dir = str(out_dir)
    sim.visu = False
    sim.number_of_threads = int(cfg.threads)

    # World
    sim.world.size = [2.0 * m, 2.0 * m, 2.0 * m]
    sim.world.material = "G4_AIR"

    # Detector / film plane at z = +odd
    film = sim.add_volume("Box", "film")
    film.mother = sim.world.name
    film.size = [cfg.film_xy * mm, cfg.film_xy * mm, cfg.film_thickness * mm]
    film.translation = [0.0, 0.0, float(cfg.odd) * mm]
    film.material = "G4_AIR"

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

        if any(abs(r) > 1e-9 for r in (cfg.rot_x, cfg.rot_y, cfg.rot_z)):
            pelvis.rotation = _rotation_matrix(cfg.rot_x, cfg.rot_y, cfg.rot_z)

        if cfg.center_mesh:
            t = _center_translation_from_stl(Path(cfg.stl))
            if t is not None:
                pelvis.translation = [t[0] * mm, t[1] * mm, t[2] * mm]
                print(f"Centering mesh: translation = {pelvis.translation}")

    # Physics
    sim.physics_manager.physics_list_name = "G4EmLivermorePhysics"

    # Point source aimed at the film, mono-energetic
    src = sim.add_source("GenericSource", "src")
    src.particle = "gamma"
    src.n = int(cfg.photons)
    src.position.type = "point"
    src.position.translation = [0.0, 0.0, -float(cfg.sod) * mm]

    half_diag = 0.5 * float(cfg.film_xy) * math.sqrt(2.0)
    alpha = math.degrees(math.atan(half_diag / float(cfg.sid)))
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


def run_phase(cfg: SimConfig, phase: str) -> Path:
    """Build and run one phase ('flat' or 'object'). Returns its output dir."""
    if phase not in ("flat", "object"):
        raise ValueError(f"phase must be 'flat' or 'object', got {phase!r}")
    out_dir = cfg.out_path / phase
    out_dir.mkdir(parents=True, exist_ok=True)
    sim = build_simulation(cfg, out_dir, with_object=(phase == "object"))
    print(f"\n=== Running phase: {phase} ===")
    sim.run()
    print(f"=== Done phase: {phase} ===\n")
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
