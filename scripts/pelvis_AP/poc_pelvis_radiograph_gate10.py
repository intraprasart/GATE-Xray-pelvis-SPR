#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
poc_pelvis_radiograph_gate10.py

Radiography-like simulation in OpenGATE (GATE10 / opengate):
- Run "flat" (no object) to get I0
- Run "object" (with STL) to get I
- Compute attenuation image: A = -ln((I+eps)/(I0+eps))
IMPORTANT: OpenGATE allows only ONE SimulationEngine per PROCESS.
So --run both will spawn TWO subprocesses (flat + object) and then postprocess.

Usage examples:
  python poc_pelvis_radiograph_gate10.py --stl control_mm.stl --run both --center_mesh --make_png
  python poc_pelvis_radiograph_gate10.py --stl control_mm.stl --run flat
  python poc_pelvis_radiograph_gate10.py --stl control_mm.stl --run object

Outputs (default out dir: poc_radiograph_out/):
  flat/I.mhd (+ .raw)
  object/I.mhd (+ .raw)
  attenuation.mhd (+ .raw)
  attenuation.png (optional)
"""

import os
import sys
import math
import shutil
import argparse
import subprocess
from pathlib import Path

import numpy as np

# Optional deps for image IO/PNG
try:
    import SimpleITK as sitk
except Exception:
    sitk = None

# Optional plotting
try:
    import matplotlib.pyplot as plt
except Exception:
    plt = None


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def rm_dir(p: Path):
    if p.exists():
        shutil.rmtree(p)


def read_mhd_as_array(mhd_path: Path):
    if sitk is None:
        raise RuntimeError("SimpleITK is required to read .mhd files. Install: pip install SimpleITK")
    img = sitk.ReadImage(str(mhd_path))
    arr = sitk.GetArrayFromImage(img)  # [z,y,x]
    return img, arr


def write_array_as_mhd_like(ref_img, arr_zyx: np.ndarray, out_mhd: Path):
    if sitk is None:
        raise RuntimeError("SimpleITK is required to write .mhd files. Install: pip install SimpleITK")
    out = sitk.GetImageFromArray(arr_zyx.astype(np.float32))
    out.CopyInformation(ref_img)
    sitk.WriteImage(out, str(out_mhd), useCompression=True)


def save_png(img2d: np.ndarray, out_png: Path, title: str = None, use_log=False):
    if plt is None:
        print("WARNING: matplotlib not available -> skip PNG output")
        return
    a = img2d.copy()
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


def try_center_translation_from_stl(stl_path: Path):
    """
    Returns translation vector (x,y,z) that moves STL bounding box center to origin.
    Uses trimesh if available; otherwise returns None.
    """
    try:
        import trimesh
    except Exception:
        print("WARNING: trimesh not installed; cannot auto-center STL. Install: pip install trimesh")
        return None

    mesh = trimesh.load(str(stl_path), force="mesh")
    if mesh.is_empty:
        print("WARNING: STL mesh is empty; cannot center.")
        return None
    bounds = mesh.bounds  # [[minx,miny,minz],[maxx,maxy,maxz]]
    center = (bounds[0] + bounds[1]) / 2.0
    # translation to move center -> (0,0,0)
    return (-float(center[0]), -float(center[1]), -float(center[2]))


def build_simulation(args, out_dir: Path, with_object: bool):
    """
    Create and configure a single OpenGATE simulation.
    IMPORTANT: this function must be called/run only ONCE per python process (one sim.run()).
    """
    import opengate as gate

    mm = gate.g4_units.mm
    cm = gate.g4_units.cm
    m = gate.g4_units.m
    keV = gate.g4_units.keV
    deg = gate.g4_units.deg

    # -----------------------------
    # Simulation base
    # -----------------------------
    sim = gate.Simulation()
    sim.output_dir = str(out_dir)

    # Visualization off by default (keeps it lighter)
    sim.visu = False

    # Multithreading (optional)
    # If you want MT, set e.g. --threads 4. Default 1.
    sim.number_of_threads = int(args.threads)

    # -----------------------------
    # World
    # -----------------------------
    world = sim.world
    world.size = [2.0 * m, 2.0 * m, 2.0 * m]
    world.material = "G4_AIR"

    # -----------------------------
    # Film / detector plane
    # -----------------------------
    film = sim.add_volume("Box", "film")
    film.mother = world.name
    film.size = [args.film_xy * mm, args.film_xy * mm, args.film_thickness * mm]
    film.translation = [0.0 * mm, 0.0 * mm, float(args.odd) * mm]
    film.material = "G4_AIR"  # counts/fluence doesn't need absorbing material
    # You can set a real detector material later if you want edep-based imaging.

    # -----------------------------
    # Object mesh (STL) via Tesselated volume  ✅
    # -----------------------------
    if with_object:
        stl_abs = str(Path(args.stl).resolve())

        # some opengate versions use "Tesselated" (doc), some accept "TesselatedVolume"
        try:
            pelvis = sim.add_volume("Tesselated", "pelvis")
        except Exception:
            pelvis = sim.add_volume("TesselatedVolume", "pelvis")

        pelvis.mother = world.name
        pelvis.material = args.object_material
        pelvis.translation = [0.0 * mm, 0.0 * mm, 0.0 * mm]

        # IMPORTANT: attribute name is file_name (per OpenGATE docs)
        pelvis.file_name = stl_abs

        if args.center_mesh:
            t = try_center_translation_from_stl(Path(args.stl))
            if t is not None:
                pelvis.translation = [t[0] * mm, t[1] * mm, t[2] * mm]
                print(f"Centering mesh: translation = {pelvis.translation}")



    # -----------------------------
    # Physics
    # -----------------------------
    # Good low-energy EM physics for X-ray energies:
    sim.physics_manager.physics_list_name = "G4EmLivermorePhysics"
    # You can further tune production cuts if needed.

    # -----------------------------
    # Source: cone-beam diverging from point focal spot
    # -----------------------------
    # Geometry convention:
    # - Source at z = -SOD
    # - Film at z = +ODD
    # - Beam direction toward +Z (theta close to 180 deg in Geant4 convention)
    src = sim.add_source("GenericSource", "src")
    src.particle = "gamma"
    src.n = int(args.photons)

    src.position.type = "point"
    src.position.translation = [0.0, 0.0, -float(args.sod) * mm]

    # half-angle that covers film half diagonal
    half_diag = 0.5 * float(args.film_xy) * math.sqrt(2.0) * mm
    sid = (float(args.sod) + float(args.odd)) * mm  # source-to-image distance
    alpha = math.degrees(math.atan(float(half_diag / sid)))  # note: half_diag and sid have same unit

    src.direction.type = "iso"
    src.direction.phi = [0 * deg, 360 * deg]
    # toward +Z -> theta near 180 deg
    src.direction.theta = [(180.0 - alpha) * deg, 180.0 * deg]

    # Only generate directions that can hit the film (faster)
    src.direction.angle_acceptance_volume = "film"

    # Energy
    src.energy.type = "mono"
    src.energy.mono = float(args.energy_keV) * keV

    # -----------------------------
    # Actors: count/fluence image on film
    # -----------------------------
    # FluenceActor creates an image (mhd/raw) where values represent fluence/count-like quantities
    fl = sim.add_actor("FluenceActor", "I")
    fl.attached_to = film.name
    fl.output_filename = "I.mhd"
    fl.spacing = [
        (float(args.film_xy) / float(args.pix)) * mm,
        (float(args.film_xy) / float(args.pix)) * mm,
        float(args.film_thickness) * mm,
    ]
    fl.size = [int(args.pix), int(args.pix), 1]
    fl.translation = [0.0, 0.0, 0.0]
    fl.hit_type = "random"  # ok for imaging
    # If you want strictly number of hits, you can later switch actor chain to digitizer/projection.

    # Optional: save phase space on film (for later primary/scatter separation)
    if args.write_phsp:
        ph = sim.add_actor("PhaseSpaceActor", "phsp")
        ph.attached_to = film.name
        ph.output_filename = "phsp.root"
        # keep it lean (you can add more fields if needed)
        ph.attributes = ["KineticEnergy", "Position", "Direction", "ParticleName", "EventID", "TrackID"]
        ph.steps_to_store = "first"  # or "all" depending on your need

    # Stats
    st = sim.add_actor("SimulationStatisticsActor", "stats")
    st.output_filename = "stats.txt"

    return sim


def run_one(args, base_out: Path, tag: str, with_object: bool):
    out_dir = base_out / tag
    ensure_dir(out_dir)
    sim = build_simulation(args, out_dir, with_object=with_object)
    print(f"\n=== Running: {tag} (with_object={with_object}) ===")
    sim.run()
    print(f"=== Done: {tag} ===\n")


def do_postprocess(args, base_out: Path):
    flat_mhd = base_out / "flat" / "I.mhd"
    obj_mhd = base_out / "object" / "I.mhd"
    if not flat_mhd.exists() or not obj_mhd.exists():
        print("Postprocess skipped: missing flat/object outputs.")
        print(f"  flat exists? {flat_mhd.exists()} -> {flat_mhd}")
        print(f"  obj  exists? {obj_mhd.exists()} -> {obj_mhd}")
        return

    if sitk is None:
        print("Postprocess skipped: SimpleITK not installed (needed to read/write .mhd).")
        return

    ref_img0, I0 = read_mhd_as_array(flat_mhd)
    ref_img, I = read_mhd_as_array(obj_mhd)

    # Expect shape [1, y, x]
    I0_2d = I0[0].astype(np.float64)
    I_2d = I[0].astype(np.float64)

    eps = 1e-12
    A_2d = -np.log((I_2d + eps) / (I0_2d + eps))
    A_2d = np.nan_to_num(A_2d, nan=0.0, posinf=0.0, neginf=0.0)

    # Write attenuation as mhd (keep same geometry as object)
    out_mhd = base_out / "attenuation.mhd"
    A_zyx = A_2d[np.newaxis, :, :]
    write_array_as_mhd_like(ref_img, A_zyx, out_mhd)
    print(f"Saved attenuation: {out_mhd}")

    # Optional PNGs
    if args.make_png:
        # quick preview PNGs
        save_png(I0_2d, base_out / "flat_log10I0.png", title="flat log1p(I0)", use_log=True)
        save_png(I_2d, base_out / "object_log10I.png", title="object log1p(I)", use_log=True)
        save_png(A_2d, base_out / "attenuation.png", title="attenuation = -ln(I/I0)", use_log=False)
        print(f"Saved PNG previews in: {base_out}")


def main():
    p = argparse.ArgumentParser()

    p.add_argument("--stl", type=str, required=True, help="Path to STL file (mm units recommended)")
    p.add_argument("--out", type=str, default="poc_radiograph_out", help="Output directory")

    p.add_argument("--run", choices=["flat", "object", "both"], default="both",
                   help="flat: no object, object: with STL, both: run flat+object then postprocess")

    p.add_argument("--clean", action="store_true", help="Delete output directory before running")
    p.add_argument("--center_mesh", action="store_true", help="Center mesh bbox at origin (requires trimesh)")

    p.add_argument("--photons", type=int, default=2_000_000, help="Number of photons (events)")
    p.add_argument("--threads", type=int, default=1, help="Number of threads")

    p.add_argument("--sod", type=float, default=800.0, help="Source-to-object distance (mm), source at z=-SOD")
    p.add_argument("--odd", type=float, default=400.0, help="Object-to-detector distance (mm), film at z=+ODD")

    p.add_argument("--film_xy", type=float, default=400.0, help="Detector size in x/y (mm)")
    p.add_argument("--film_thickness", type=float, default=1.0, help="Detector thickness (mm)")
    p.add_argument("--pix", type=int, default=512, help="Detector pixels in x/y (square)")

    p.add_argument("--energy_keV", type=float, default=80.0, help="Mono energy (keV)")
    p.add_argument("--object_material", type=str, default="G4_BONE_COMPACT_ICRU",
                   help="Material for STL mesh volume")

    p.add_argument("--make_png", action="store_true", help="Save PNG previews (requires matplotlib)")
    p.add_argument("--write_phsp", action="store_true", help="Write phase space ROOT at film")

    # internal: prevent postprocess during subprocess runs
    p.add_argument("--no_post", action="store_true", help=argparse.SUPPRESS)

    args = p.parse_args()

    stl_path = Path(args.stl)
    if not stl_path.exists():
        raise FileNotFoundError(f"STL not found: {stl_path}")

    base_out = Path(args.out).resolve()
    if args.clean and base_out.exists():
        rm_dir(base_out)
    ensure_dir(base_out)

    # IMPORTANT: OpenGATE only allows 1 SimulationEngine per process.
    # So if args.run == "both", we must spawn two separate python processes.
    if args.run == "both":
        script_path = Path(__file__).resolve()

        cmd_base = [
            sys.executable, str(script_path),
            "--stl", str(stl_path.resolve()),
            "--out", str(base_out),
            "--pix", str(args.pix),
            "--photons", str(args.photons),
            "--threads", str(args.threads),
            "--sod", str(args.sod),
            "--odd", str(args.odd),
            "--film_xy", str(args.film_xy),
            "--film_thickness", str(args.film_thickness),
            "--energy_keV", str(args.energy_keV),
            "--object_material", str(args.object_material),
            "--no_post",
        ]

        if args.center_mesh:
            cmd_base.append("--center_mesh")
        if args.write_phsp:
            cmd_base.append("--write_phsp")

        # clean only once (before flat)
        if args.clean:
            cmd_base.append("--clean")

        # Run flat in a new process
        subprocess.run(cmd_base + ["--run", "flat"], check=True)

        # Run object in a new process (must NOT clean)
        cmd_obj = [c for c in cmd_base if c != "--clean"]
        subprocess.run(cmd_obj + ["--run", "object"], check=True)

        # Postprocess in current process
        if not args.no_post:
            do_postprocess(args, base_out)
        return

    # Single-run modes (safe: only one sim.run in this process)
    if args.run == "flat":
        run_one(args, base_out, tag="flat", with_object=False)
    elif args.run == "object":
        run_one(args, base_out, tag="object", with_object=True)

    # Postprocess only if user asked run==both (handled above) OR they explicitly want it
    # Here we keep it minimal: don't auto-run postprocess for flat/object single runs.
    # But you can uncomment if you want:
    # if not args.no_post:
    #     do_postprocess(args, base_out)


if __name__ == "__main__":
    main()
