import os
import time
import math
import shutil
import argparse
import subprocess
import sys

# -----------------------------
# Fixed geometry convention (mm)
# -----------------------------
# Object (STL) center: at (0,0,0)
# Source: at (0,0,-SOD)
# Detector plane: centered at (0,0,+ODD)

OUT_BASE = "bench_stl_out_v2"
SEED = 12345

# Energy (mono; for speed-only benchmark)
E_KEV = 80.0
E_MeV = E_KEV * 1e-3


def child_run_once(stl_path: str,
                   out_dir: str,
                   mode: str,
                   photons: int,
                   threads: int,
                   stl_scale: float,
                   SOD: float,
                   ODD: float,
                   det_xy: float,
                   det_thick: float):
    import opengate as gate

    # World size: cover source->detector with margin
    z_extent = SOD + ODD
    margin = 500.0
    world_z = z_extent + margin
    world_xy = max(det_xy, 800.0) + margin
    world_size = [world_xy, world_xy, world_z]

    sim = gate.Simulation()
    sim.output_dir = out_dir

    sim.random_seed = SEED
    sim.g4_verbose = False
    sim.visu = False
    sim.number_of_threads = threads

    # World
    world = sim.world
    world.size = world_size
    world.material = "G4_AIR"

    # STL mesh at origin
    if not os.path.exists(stl_path):
        raise FileNotFoundError(f"STL not found: {stl_path}")

    mesh = sim.add_volume("Tesselated", "control_mesh")
    mesh.material = "G4_WATER"
    mesh.file_name = stl_path
    mesh.translation = [0.0, 0.0, 0.0]


    # Detector plane at +ODD
    det = sim.add_volume("Box", "detector")
    det.size = [det_xy, det_xy, det_thick]
    det.translation = [0.0, 0.0, ODD]
    det.material = "G4_AIR"

    # Source at -SOD
    src = sim.add_source("GenericSource", "src")
    src.particle = "gamma"
    src.n = photons

    # --- Direction modes ---
    if mode == "cone":
        # cone-like: disc + focused to detector center (0,0,+ODD)
        # เลือก disc radius ให้ครอบ detector ด้วยมุมคร่าว ๆ
        # half-angle alpha ≈ arctan((det_xy/2) / (SOD + ODD))
        # disc_radius ≈ SOD * tan(alpha)
        alpha = math.atan((det_xy * 0.5) / (SOD + ODD))
        disc_radius = SOD * math.tan(alpha)

        src.position.type = "disc"
        src.position.radius = disc_radius
        src.position.translation = [0.0, 0.0, -SOD]

        src.direction.type = "focused"
        src.direction.focus_point = [0.0, 0.0, ODD]

    elif mode == "collimated":
        # collimated: point + fixed momentum (parallel)
        src.position.type = "point"
        src.position.translation = [0.0, 0.0, -SOD]

        src.direction.type = "momentum"
        src.direction.momentum = [0.0, 0.0, 1.0]

    else:
        raise ValueError("mode must be 'cone' or 'collimated'")

    src.energy.type = "mono"
    src.energy.mono = E_MeV

    # Stats only (เบาสุดสำหรับ speed benchmark)
    stats = sim.add_actor("SimulationStatisticsActor", "stats")
    stats.track_types_flag = True

    # Run + timing
    t0 = time.time()
    sim.run()
    t1 = time.time()

    elapsed = t1 - t0
    pps = photons / elapsed if elapsed > 0 else float("inf")

    print(
        f"RESULT mode={mode} threads={threads} photons={photons} "
        f"elapsed_s={elapsed:.6f} pps={pps:.3f} "
        f"SOD={SOD} ODD={ODD} det_xy={det_xy} stl_scale={stl_scale} "
        f"out_dir={os.path.abspath(out_dir)}"
    )


def parent_mode(args):
    os.makedirs(OUT_BASE, exist_ok=True)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    results = []

    print("=== STL Speed Test (position+scale locked, mm) ===")
    print(f"STL       : {os.path.abspath(args.stl)}")
    print(f"photons   : {args.photons:,}")
    print(f"threads   : {args.threads}  (แนะนำ 1 บน Mac env นี้ เพราะ MT ที่แล้วช้าลง)")
    print(f"energy    : {E_KEV} keV mono gamma")
    print(f"SOD/ODD   : {args.SOD} mm / {args.ODD} mm")
    print(f"detector  : {args.det_xy} x {args.det_xy} mm, thick={args.det_thick} mm @ z=+ODD")
    print(f"stl_scale : {args.stl_scale} (1000 ถ้า STL มาจาก Blender หน่วยเมตร)\n")

    for mode in modes:
        out_dir = os.path.join(OUT_BASE, f"{mode}_t{args.threads}_n{args.photons}")
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)
        os.makedirs(out_dir, exist_ok=True)

        cmd = [
            sys.executable, __file__,
            "--child",
            "--stl", args.stl,
            "--mode", mode,
            "--photons", str(args.photons),
            "--threads", str(args.threads),
            "--stl_scale", str(args.stl_scale),
            "--SOD", str(args.SOD),
            "--ODD", str(args.ODD),
            "--det_xy", str(args.det_xy),
            "--det_thick", str(args.det_thick),
        ]
        p = subprocess.run(cmd, capture_output=True, text=True)

        if p.returncode != 0:
            print(f"[{mode}] ERROR (returncode={p.returncode})")
            print(p.stdout)
            print(p.stderr)
            continue

        print(p.stdout.strip())

        for line in p.stdout.splitlines():
            if line.startswith("RESULT "):
                parts = dict(kv.split("=", 1) for kv in line.replace("RESULT ", "").split())
                results.append({
                    "mode": parts["mode"],
                    "elapsed_s": float(parts["elapsed_s"]),
                    "pps": float(parts["pps"]),
                })

    if results:
        fastest = min(results, key=lambda r: r["elapsed_s"])
        print("\n=== SUMMARY ===")
        for r in sorted(results, key=lambda x: x["elapsed_s"]):
            print(f"{r['mode']:<10} elapsed={r['elapsed_s']:.2f}s  pps={r['pps']:.1f}")
        print(f"\nFASTEST: {fastest['mode']} (elapsed {fastest['elapsed_s']:.2f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--stl", type=str, default="control.stl")
    ap.add_argument("--mode", type=str, default="cone", choices=["cone", "collimated"])
    ap.add_argument("--modes", type=str, default="cone,collimated")

    ap.add_argument("--photons", type=int, default=10000)
    ap.add_argument("--threads", type=int, default=1)

    # Geometry lock
    ap.add_argument("--SOD", type=float, default=800.0)
    ap.add_argument("--ODD", type=float, default=800.0)
    ap.add_argument("--det_xy", type=float, default=400.0)
    ap.add_argument("--det_thick", type=float, default=1.0)

    # Scale lock
    ap.add_argument("--stl_scale", type=float, default=1000.0,
                    help="1000 if STL units are meters (Blender typical). Use 1 if STL already in mm.")
    args = ap.parse_args()

    if args.child:
        out_dir = os.path.join(OUT_BASE, f"{args.mode}_t{args.threads}_n{args.photons}")
        child_run_once(
            stl_path=args.stl,
            out_dir=out_dir,
            mode=args.mode,
            photons=args.photons,
            threads=args.threads,
            stl_scale=args.stl_scale,
            SOD=args.SOD,
            ODD=args.ODD,
            det_xy=args.det_xy,
            det_thick=args.det_thick
        )
    else:
        parent_mode(args)


if __name__ == "__main__":
    main()
