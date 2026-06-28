import os
import time
import math
import argparse
import shutil

SEED = 12345


def attach(actor, volume_name: str):
    actor.attached_to = volume_name


def set_output(actor, filename: str, write: bool = True):
    # OpenGATE new API: output_filename is relative to sim.output_dir
    actor.output_filename = filename
    actor.write_to_disk = write


def build_and_run(args):
    import opengate as gate

    # --------- Output folder ----------
    out_dir = args.out
    if os.path.exists(out_dir) and args.clean:
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    # --------- Energy ----------
    E_MeV = args.kev * 1e-3  # keV -> MeV

    # --------- World size (mm) ----------
    z_extent = args.SOD + args.ODD
    margin = args.world_margin
    world_z = z_extent + margin
    world_xy = max(args.film_xy, 800.0) + margin
    world_size = [world_xy, world_xy, world_z]

    # --------- Simulation ----------
    sim = gate.Simulation()
    sim.output_dir = out_dir
    sim.random_seed = SEED
    sim.g4_verbose = False
    sim.visu = False
    sim.number_of_threads = args.threads

    # (optional) overlap check
    sim.check_volumes_overlap = args.check_overlap

    # World
    world = sim.world
    world.size = world_size
    world.material = "G4_AIR"

    # --------- Object (STL) ----------
    if not os.path.exists(args.stl):
        raise FileNotFoundError(f"STL not found: {args.stl}")

    obj = sim.add_volume("Tesselated", "control_mesh")
    obj.file_name = args.stl
    obj.translation = [0.0, 0.0, 0.0]
    obj.material = args.object_material

    # --------- Film detector ----------
    film = sim.add_volume("Box", "film")
    film.size = [args.film_xy, args.film_xy, args.film_thick]
    film.translation = [0.0, 0.0, args.ODD]
    film.material = args.film_material

    # --------- Source: cone-beam (disc + focused) ----------
    src = sim.add_source("GenericSource", "src")
    src.particle = "gamma"
    src.n = args.photons

    if args.disc_radius > 0:
        disc_radius = args.disc_radius
    else:
        alpha = math.atan((args.film_xy * 0.5) / (args.SOD + args.ODD))
        disc_radius = args.SOD * math.tan(alpha)

    src.position.type = "disc"
    src.position.radius = disc_radius
    src.position.translation = [0.0, 0.0, -args.SOD]

    src.direction.type = "focused"
    src.direction.focus_point = [0.0, 0.0, args.ODD]

    src.energy.type = "mono"
    src.energy.mono = E_MeV

    # --------- Actors ----------
    stats = sim.add_actor("SimulationStatisticsActor", "stats")
    attach(stats, "world")
    stats.track_types_flag = True
    # stats actor typically writes a json/txt automatically; we can leave output settings alone

    flu = sim.add_actor("FluenceActor", "fluence_film")
    attach(flu, "film")
    flu.size = [args.pix, args.pix, 1]
    flu.spacing = [args.film_xy / args.pix, args.film_xy / args.pix, args.film_thick]
    set_output(flu, "fluence_film.mhd", write=True)

    dose = sim.add_actor("DoseActor", "dose_film")
    attach(dose, "film")
    dose.size = [args.pix, args.pix, 1]
    dose.spacing = [args.film_xy / args.pix, args.film_xy / args.pix, args.film_thick]
    set_output(dose, "dose_film.mhd", write=True)

    phsp = sim.add_actor("PhaseSpaceActor", "phsp_film")
    attach(phsp, "film")
    phsp.attributes = [
        "EventID",
        "TrackID",
        "ParentID",
        "KineticEnergy",
        "Position",
        "Direction",
        "CreatorProcess",
        "ProcessDefinedStep",
    ]
    # PhaseSpace is a ROOT file in your env list; set output_filename accordingly
    set_output(phsp, "phsp_film.root", write=True)

    # --------- Sanity print ----------
    print("=== PoC Cone-beam -> Film (OpenGATE) ===")
    print(f"STL            : {os.path.abspath(args.stl)}")
    print(f"Object material: {args.object_material}")
    print(f"Film material  : {args.film_material}")
    print(f"SOD / ODD (mm) : {args.SOD} / {args.ODD}")
    print(f"Film size (mm) : {args.film_xy} x {args.film_xy} x {args.film_thick}")
    print(f"Pixels         : {args.pix} x {args.pix}")
    print(f"Energy         : {args.kev} keV mono")
    print(f"Photons        : {args.photons:,}")
    print(f"Threads        : {args.threads}")
    print(f"Disc radius    : {disc_radius:.3f} mm (cone-like)")
    print(f"Output dir     : {os.path.abspath(out_dir)}")
    print("Outputs (in output_dir): fluence_film.mhd/.raw, dose_film.mhd/.raw, phsp_film.root\n")

    # placement check
    zmin, zmax = -world_z/2.0, world_z/2.0
    print("[CHECK] placement")
    print(f"  source z = {-args.SOD}  (within [{zmin:.1f},{zmax:.1f}])")
    print(f"  object z = 0")
    print(f"  film   z = {args.ODD}   (within [{zmin:.1f},{zmax:.1f}])")
    print(f"  focus_point = (0,0,{args.ODD})\n")

    # --------- Run ----------
    t0 = time.time()
    sim.run()
    t1 = time.time()

    elapsed = t1 - t0
    pps = args.photons / elapsed if elapsed > 0 else float("inf")
    print(f"=== DONE === elapsed_s={elapsed:.2f}  pps={pps:.1f}")


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--stl", type=str, default="control_mm.stl")
    ap.add_argument("--out", type=str, default="poc_cone_film_out")
    ap.add_argument("--clean", action="store_true")

    ap.add_argument("--photons", type=int, default=100_000)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--kev", type=float, default=80.0)

    ap.add_argument("--SOD", type=float, default=800.0)
    ap.add_argument("--ODD", type=float, default=800.0)

    ap.add_argument("--film_xy", type=float, default=400.0)
    ap.add_argument("--film_thick", type=float, default=1.0)
    ap.add_argument("--pix", type=int, default=256)

    ap.add_argument("--disc_radius", type=float, default=-1.0)

    ap.add_argument("--object_material", type=str, default="G4_WATER")
    ap.add_argument("--film_material", type=str, default="G4_WATER")

    ap.add_argument("--world_margin", type=float, default=500.0)
    ap.add_argument("--check_overlap", action="store_true")

    args = ap.parse_args()
    build_and_run(args)


if __name__ == "__main__":
    main()
