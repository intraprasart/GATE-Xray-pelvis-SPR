#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
poc_pelvis_radiograph_gate10_AP.py

Radiography-like simulation in OpenGATE (GATE10 / opengate) + film-like AP output

- Run "flat"   (no object) -> I0
- Run "object" (with STL)  -> I
- Postprocess:
    T(x,y) = (I+eps)/(I0+eps)                 (Transmission)
    A(x,y) = -ln(T)                           (Attenuation)
    film_like_AP.png from T with window/level + gamma + blur

IMPORTANT:
OpenGATE allows only ONE SimulationEngine per PROCESS.
So --run both will spawn TWO subprocesses (flat + object) then postprocess in parent.

Usage examples:
  # 1) Run flat+object and produce PNGs (including film-like)
  python poc_pelvis_radiograph_gate10_AP.py --stl control_mm.stl --run both --make_png --center_mesh

  # 2) Try AP rotations (you MUST tune these to make it truly AP for your STL)
  python poc_pelvis_radiograph_gate10_AP.py --stl control_mm.stl --run both --make_png --center_mesh --rot_x 90
  python poc_pelvis_radiograph_gate10_AP.py --stl control_mm.stl --run both --make_png --center_mesh --rot_x -90
  python poc_pelvis_radiograph_gate10_AP.py --stl control_mm.stl --run both --make_png --center_mesh --rot_y 90
  python poc_pelvis_radiograph_gate10_AP.py --stl control_mm.stl --run both --make_png --center_mesh --rot_y -90

Outputs (default out dir: poc_radiograph_out/):
  flat/I.mhd (+ .raw)
  object/I.mhd (+ .raw)
  transmission.mhd (+ .raw)
  attenuation.mhd (+ .raw)
  film_like_AP.png (if --make_png)
  object_log1pI.png, flat_log1pI0.png, attenuation.png (if --make_png)
"""

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

def rot_matrix_xyz(rx_deg: float, ry_deg: float, rz_deg: float):
    """
    Return 3x3 rotation matrix for rotations around X then Y then Z (degrees).
    OpenGATE expects rotation as a 3x3 matrix.
    """
    rx = math.radians(rx_deg)
    ry = math.radians(ry_deg)
    rz = math.radians(rz_deg)

    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)

    Rx = np.array([[1, 0, 0],
                   [0, cx, -sx],
                   [0, sx, cx]], dtype=float)

    Ry = np.array([[cy, 0, sy],
                   [0, 1, 0],
                   [-sy, 0, cy]], dtype=float)

    Rz = np.array([[cz, -sz, 0],
                   [sz,  cz, 0],
                   [0,   0,  1]], dtype=float)

    # Apply X then Y then Z  (R = Rz * Ry * Rx)
    R = Rz @ Ry @ Rx
    return R


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def rm_dir(p: Path):
    if p.exists():
        shutil.rmtree(p)


def read_mhd_as_array(mhd_path: Path):
    if sitk is None:
        raise RuntimeError("SimpleITK is required to read .mhd. Install: pip install SimpleITK")
    img = sitk.ReadImage(str(mhd_path))
    arr = sitk.GetArrayFromImage(img)  # [z,y,x]
    return img, arr


def write_array_as_mhd_like(ref_img, arr_zyx: np.ndarray, out_mhd: Path):
    if sitk is None:
        raise RuntimeError("SimpleITK is required to write .mhd. Install: pip install SimpleITK")
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


def _box_blur_3x3(img: np.ndarray) -> np.ndarray:
    # fast-ish 3x3 mean blur using roll (no scipy)
    return (img +
            np.roll(img, 1, 0) + np.roll(img, -1, 0) +
            np.roll(img, 1, 1) + np.roll(img, -1, 1) +
            np.roll(np.roll(img, 1, 0), 1, 1) +
            np.roll(np.roll(img, 1, 0), -1, 1) +
            np.roll(np.roll(img, -1, 0), 1, 1) +
            np.roll(np.roll(img, -1, 0), -1, 1)) / 9.0


def save_film_like_from_ratio(
    ratio2d: np.ndarray,
    out_png: Path,
    clip_lo: float = 0.5,
    clip_hi: float = 99.5,
    gamma: float = 0.85,
    blur_passes: int = 2,
    invert: bool = True,
):
    """
    Build a film-like radiograph from transmission ratio T=I/I0.

    Steps:
      img = 1 - clamp(T)         -> attenuation-like contrast
      blur (a few 3x3 passes)    -> reduces Monte-Carlo speckle
      percentile window/level
      gamma
      invert (optional)          -> classic film look (bone bright)
    """
    if plt is None:
        print("WARNING: matplotlib not available -> skip film-like PNG output")
        return

    T = ratio2d.astype(np.float32)
    T = np.where(np.isfinite(T), T, 1.0)

    # clamp transmission to avoid crazy values
    T = np.clip(T, 0.0, 1.5)

    # convert to contrast image: more attenuation -> larger value
    img = 1.0 - T

    # blur to reduce speckle
    for _ in range(max(0, int(blur_passes))):
        img = _box_blur_3x3(img)

    # robust window/level
    v = img[np.isfinite(img)]
    lo = np.percentile(v, float(clip_lo))
    hi = np.percentile(v, float(clip_hi))
    img = np.clip(img, lo, hi)
    img = (img - lo) / (hi - lo + 1e-12)

    # gamma
    img = np.power(img, float(gamma))

    # film convention (bone bright)
    if invert:
        img = 1.0 - img

    plt.figure()
    plt.imshow(img, cmap="gray", vmin=0, vmax=1)
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(str(out_png), dpi=200, bbox_inches="tight", pad_inches=0)
    plt.close()
    print(f"Saved film-like PNG: {out_png}")


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
    return (-float(center[0]), -float(center[1]), -float(center[2]))


def build_simulation(args, out_dir: Path, with_object: bool):
    """
    Create and configure a single OpenGATE simulation.
    IMPORTANT: this function must be called/run only ONCE per python process (one sim.run()).
    """
    import opengate as gate

    mm = gate.g4_units.mm
    m = gate.g4_units.m
    keV = gate.g4_units.keV
    deg = gate.g4_units.deg

    sim = gate.Simulation()
    sim.output_dir = str(out_dir)
    sim.visu = False
    sim.number_of_threads = int(args.threads)

    # World
    world = sim.world
    world.size = [2.0 * m, 2.0 * m, 2.0 * m]
    world.material = "G4_AIR"

    # Film / detector plane
    film = sim.add_volume("Box", "film")
    film.mother = world.name
    film.size = [args.film_xy * mm, args.film_xy * mm, args.film_thickness * mm]
    film.translation = [0.0 * mm, 0.0 * mm, float(args.odd) * mm]
    film.material = "G4_AIR"

    # Object mesh (STL) via Tesselated volume
    if with_object:
        stl_abs = str(Path(args.stl).resolve())

        try:
            pelvis = sim.add_volume("Tesselated", "pelvis")
        except Exception:
            pelvis = sim.add_volume("TesselatedVolume", "pelvis")

        pelvis.mother = world.name
        pelvis.material = args.object_material

        pelvis.translation = [0.0 * mm, 0.0 * mm, 0.0 * mm]
        pelvis.file_name = stl_abs

        # Set rotation ONLY if any angle is non-zero
        rx = float(args.rot_x)
        ry = float(args.rot_y)
        rz = float(args.rot_z)

        if abs(rx) > 1e-9 or abs(ry) > 1e-9 or abs(rz) > 1e-9:
            R = rot_matrix_xyz(rx, ry, rz)   # numpy 3x3
            pelvis.rotation = R              # <-- ส่งเป็น numpy array ห้าม .tolist()
        # else: don't set pelvis.rotation at all (default = identity)



        if args.center_mesh:
            t = try_center_translation_from_stl(Path(args.stl))
            if t is not None:
                pelvis.translation = [t[0] * mm, t[1] * mm, t[2] * mm]
                print(f"Centering mesh: translation = {pelvis.translation}")

    # Physics
    sim.physics_manager.physics_list_name = "G4EmLivermorePhysics"

    # Source: cone-beam from point focal spot
    src = sim.add_source("GenericSource", "src")
    src.particle = "gamma"
    src.n = int(args.photons)

    src.position.type = "point"
    src.position.translation = [0.0, 0.0, -float(args.sod) * mm]

    half_diag = 0.5 * float(args.film_xy) * math.sqrt(2.0) * mm
    sid = (float(args.sod) + float(args.odd)) * mm
    alpha = math.degrees(math.atan(float(half_diag / sid)))

    src.direction.type = "iso"
    src.direction.phi = [0 * deg, 360 * deg]
    src.direction.theta = [(180.0 - alpha) * deg, 180.0 * deg]
    src.direction.angle_acceptance_volume = "film"

    src.energy.type = "mono"
    src.energy.mono = float(args.energy_keV) * keV

    # FluenceActor (I.mhd)
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
    fl.hit_type = "random"

    # Optional phase space for later primary/scatter separation
    if args.write_phsp:
        ph = sim.add_actor("PhaseSpaceActor", "phsp")
        ph.attached_to = film.name
        ph.output_filename = "phsp.root"
        ph.attributes = ["KineticEnergy", "Position", "Direction", "ParticleName", "EventID", "TrackID"]
        ph.steps_to_store = "first"

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

    I0_2d = I0[0].astype(np.float64)
    I_2d = I[0].astype(np.float64)

    eps = 1e-12
    T_2d = (I_2d + eps) / (I0_2d + eps)
    A_2d = -np.log(T_2d)
    A_2d = np.nan_to_num(A_2d, nan=0.0, posinf=0.0, neginf=0.0)

    # Save transmission and attenuation as .mhd
    out_T_mhd = base_out / "transmission.mhd"
    write_array_as_mhd_like(ref_img, T_2d[np.newaxis, :, :], out_T_mhd)
    print(f"Saved transmission: {out_T_mhd}")

    out_A_mhd = base_out / "attenuation.mhd"
    write_array_as_mhd_like(ref_img, A_2d[np.newaxis, :, :], out_A_mhd)
    print(f"Saved attenuation: {out_A_mhd}")

    if args.make_png:
        save_png(I0_2d, base_out / "flat_log1pI0.png", title="flat log1p(I0)", use_log=True)
        save_png(I_2d, base_out / "object_log1pI.png", title="object log1p(I)", use_log=True)
        save_png(A_2d, base_out / "attenuation.png", title="attenuation = -ln(I/I0)", use_log=False)

        save_film_like_from_ratio(
            T_2d,
            base_out / "film_like_AP.png",
            clip_lo=args.film_clip_lo,
            clip_hi=args.film_clip_hi,
            gamma=args.film_gamma,
            blur_passes=args.film_blur_passes,
            invert=False,
        )
        print(f"Saved PNG previews in: {base_out}")


def main():
    p = argparse.ArgumentParser()

    p.add_argument("--stl", type=str, required=True, help="Path to STL file (mm units recommended)")
    p.add_argument("--out", type=str, default="poc_radiograph_out", help="Output directory")

    p.add_argument("--run", choices=["flat", "object", "both"], default="both",
                   help="flat: no object, object: with STL, both: run flat+object then postprocess")

    p.add_argument("--clean", action="store_true", help="Delete output directory before running")
    p.add_argument("--center_mesh", action="store_true", help="Center mesh bbox at origin (requires trimesh)")

    # Rotation to tune for AP view (YOU tune these)
    p.add_argument("--rot_x", type=float, default=0.0, help="Rotate STL around X (deg)")
    p.add_argument("--rot_y", type=float, default=0.0, help="Rotate STL around Y (deg)")
    p.add_argument("--rot_z", type=float, default=0.0, help="Rotate STL around Z (deg)")

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

    # Film-like tuning
    p.add_argument("--film_clip_lo", type=float, default=0.5, help="Film-like: low percentile (e.g., 0.5)")
    p.add_argument("--film_clip_hi", type=float, default=99.5, help="Film-like: high percentile (e.g., 99.5)")
    p.add_argument("--film_gamma", type=float, default=0.85, help="Film-like: gamma (0.7-1.3 typical)")
    p.add_argument("--film_blur_passes", type=int, default=2, help="Film-like: number of 3x3 blur passes (0-5)")

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
            "--rot_x", str(args.rot_x),
            "--rot_y", str(args.rot_y),
            "--rot_z", str(args.rot_z),
            "--no_post",
        ]

        if args.center_mesh:
            cmd_base.append("--center_mesh")
        if args.write_phsp:
            cmd_base.append("--write_phsp")

        # clean only once (before flat)
        if args.clean:
            cmd_base.append("--clean")

        # Run flat
        subprocess.run(cmd_base + ["--run", "flat"], check=True)

        # Run object (must NOT clean)
        cmd_obj = [c for c in cmd_base if c != "--clean"]
        subprocess.run(cmd_obj + ["--run", "object"], check=True)

        # Postprocess in this process
        if not args.no_post:
            do_postprocess(args, base_out)
        return

    # Single-run modes (safe: only one sim.run in this process)
    if args.run == "flat":
        run_one(args, base_out, tag="flat", with_object=False)
    elif args.run == "object":
        run_one(args, base_out, tag="object", with_object=True)


if __name__ == "__main__":
    main()
