#!/usr/bin/env python3
# poc_cone_film_gate10_v3_fixed.py
# Corrected Version for OpenGATE 10 (GATE 10)

import os
import time
import math
import shutil
import argparse

# -----------------------------------------
# Helpers
# -----------------------------------------
def ensure_empty_dir(path: str, clean: bool):
    if clean and os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)


def build_and_run(args):
    try:
        import opengate as gate
    except Exception as e:
        raise SystemExit(
            "ERROR: Cannot import opengate.\n"
            "Please run in an environment where GATE10/OpenGATE is installed.\n"
            f"Details: {e}"
        )

    out_dir = os.path.abspath(args.out)
    ensure_empty_dir(out_dir, args.clean)

    # -----------------------------
    # Geometry (mm)
    # -----------------------------
    SOD = float(args.SOD)
    ODD = float(args.ODD)
    film_xy = float(args.det_xy)
    film_thick = float(args.det_thick)
    pix = int(args.pix)

    # World size
    z_extent = SOD + ODD
    margin = float(args.world_margin)
    world_z = z_extent + margin
    world_xy = max(film_xy, float(args.world_min_xy)) + margin
    world_size = [world_xy, world_xy, world_z]

    E_MeV = float(args.energy_keV) * 1e-3

    # -----------------------------
    # Simulation Setup
    # -----------------------------
    sim = gate.Simulation()
    sim.output_dir = out_dir
    sim.random_seed = int(args.seed)
    sim.g4_verbose = False
    sim.visu = False
    sim.number_of_threads = int(args.threads)

    if args.physics_list:
        sim.physics_manager.physics_list_name = args.physics_list

    # -----------------------------
    # World
    # -----------------------------
    world = sim.world
    world.size = world_size
    world.material = "G4_AIR"

    # -----------------------------
    # Object: STL
    # -----------------------------
    stl_path = os.path.abspath(args.stl)
    if not os.path.exists(stl_path):
        raise FileNotFoundError(f"STL not found: {stl_path}")

    obj = sim.add_volume("Tesselated", "object_mesh")
    obj.material = args.object_material
    obj.file_name = stl_path
    obj.translation = [0.0, 0.0, 0.0]
    
    if args.center_mesh:
        obj.translate_mesh_to_center = True

    # -----------------------------
    # Film / Detector
    # -----------------------------
    film = sim.add_volume("Box", "film")
    film.size = [film_xy, film_xy, film_thick]
    film.translation = [0.0, 0.0, ODD]
    film.material = args.film_material

    # -----------------------------
    # Source: Cone-beam
    # -----------------------------
    src = sim.add_source("GenericSource", "src")
    src.particle = "gamma"
    src.n = int(args.photons)
    src.position.type = "disc"
    src.position.translation = [0.0, 0.0, -SOD]

    if args.disc_radius > 0:
        disc_radius = float(args.disc_radius)
    else:
        # Auto-compute radius to cover the film diagonal
        half_diag = (film_xy * math.sqrt(2)) * 0.5
        alpha = math.atan(half_diag / (SOD + ODD))
        disc_radius = SOD * math.tan(alpha)
    
    src.position.radius = disc_radius
    src.direction.type = "focused"
    src.direction.focus_point = [0.0, 0.0, ODD]
    src.energy.type = "mono"
    src.energy.mono = E_MeV

    # -----------------------------
    # Actors
    # -----------------------------
    spacing_xy = film_xy / pix
    spacing = [spacing_xy, spacing_xy, film_thick]

    # 1. Fluence
    flu = sim.add_actor("FluenceActor", "fluence_film")
    flu.attached_to = "film"
    flu.size = [pix, pix, 1]
    flu.spacing = spacing
    flu.output_filename = "fluence_film.mhd"
    flu.write_to_disk = True

    # 2. Dose
    dose = sim.add_actor("DoseActor", "dose_film_edep")
    dose.attached_to = "film"
    dose.size = [pix, pix, 1]
    dose.spacing = spacing
    dose.output_filename = "dose_film_edep.mhd"
    dose.write_to_disk = True

    # 3. Phase Space (FIXED ATTRIBUTES HERE)
    phsp = sim.add_actor("PhaseSpaceActor", "phsp_film")
    phsp.attached_to = "film"
    phsp.attributes = [
        "Position",               # Automatically includes X, Y, Z
        "Direction",              # Automatically includes X, Y, Z
        "KineticEnergy", 
        "Weight",
        "UnscatteredPrimaryFlag", # Critical for Scatter-to-Primary Ratio (SPR)
        "ProcessDefinedStep",
        "TrackCreatorProcess",
        "TrackID", 
        "ParentID", 
        "EventID", 
        "RunID",
        "ParticleName",
    ]
    phsp.output_filename = "phsp_film.root"
    phsp.write_to_disk = True

    # 4. Stats
    stats = sim.add_actor("SimulationStatisticsActor", "stats")
    stats.track_types_flag = True

    # -----------------------------
    # Execution
    # -----------------------------
    print("\n=== POC cone-beam (GATE10 Fixed Version) ===")
    print(f"Output directory : {out_dir}")
    print(f"Photons          : {args.photons:,}")
    print(f"Energy           : {args.energy_keV} keV")
    print(f"Disc Radius      : {disc_radius:.3f} mm")
    print("-" * 40)

    t0 = time.time()
    sim.run()
    t1 = time.time()

    elapsed = t1 - t0
    print(f"\nDONE. Elapsed: {elapsed:.2f}s | PPS: {args.photons/elapsed:.1f}")
    print(f"Check results in: {out_dir}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stl", type=str, default="control_mm.stl")
    ap.add_argument("--out", type=str, default="poc_cone_film_out")
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--photons", type=int, default=1000000)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--SOD", type=float, default=800.0)
    ap.add_argument("--ODD", type=float, default=800.0)
    ap.add_argument("--det_xy", type=float, default=400.0)
    ap.add_argument("--det_thick", type=float, default=1.0)
    ap.add_argument("--pix", type=int, default=128)
    ap.add_argument("--disc_radius", type=float, default=25.0)
    ap.add_argument("--object_material", type=str, default="G4_WATER")
    ap.add_argument("--film_material", type=str, default="G4_AIR")
    ap.add_argument("--center_mesh", action="store_true")
    ap.add_argument("--energy_keV", type=float, default=80.0)
    ap.add_argument("--physics_list", type=str, default="G4EmStandardPhysics_option4")
    ap.add_argument("--world_margin", type=float, default=500.0)
    ap.add_argument("--world_min_xy", type=float, default=800.0)

    args = ap.parse_args()
    build_and_run(args)


if __name__ == "__main__":
    main()