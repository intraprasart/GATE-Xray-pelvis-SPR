#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
poc_pelvis_radiograph_gate10_AP_primary_scatter.py

Fork of your poc_pelvis_radiograph_gate10_AP.py with an **add-on** ability to
separate PRIMARY-like vs SCATTER-like contributions at the film using a phase-space
(ROOT) file recorded at the film.

Why "PRIMARY-like"?
- In Geant4, a photon that scatters (Compton/Rayleigh) usually keeps the same TrackID,
  so ParentID/TrackID alone cannot truth-tag "unscattered".
- Here we classify a hit as PRIMARY-like if, at the film:
    (1) direction is consistent with a straight line from source to that hit position
        (angle < theta_primary_deg)
    AND
    (2) kinetic energy is close to the source mono-energy (|E-E0| < dE_primary_keV)
  Otherwise the hit is SCATTER-like.

This is a practical separation that works well for mono-energy beams and a point source.
You can tighten/loosen thresholds.

Also note: classical GATE (macro) docs mention the FluenceActor option enableScatter
and dedicated CT actors that can output primary/secondary images. In GATE10 python,
the exact API names may differ, so this script relies on PhaseSpaceActor + postprocess.

Outputs (when --separate_primary_scatter):
  object/I_primary.mhd, object/I_scatter.mhd, object/SPR.mhd
  flat/I0_primary.mhd,  flat/I0_scatter.mhd
  transmission_primary.mhd, transmission_total.mhd
  attenuation_primary.mhd, attenuation_total.mhd

Usage example:
  python poc_pelvis_radiograph_gate10_AP_primary_scatter.py \
      --stl control_mm.stl --run both --make_png --center_mesh \
      --write_phsp --separate_primary_scatter \
      --primary_theta_deg 0.5 --primary_dE_keV 1.0

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

# Optional ROOT IO for phase space
try:
    import uproot
except Exception:
    uproot = None


def rot_matrix_xyz(rx_deg: float, ry_deg: float, rz_deg: float):
    """Return 3x3 rotation matrix for rotations around X then Y then Z (degrees)."""
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
    if plt is None:
        print("WARNING: matplotlib not available -> skip film-like PNG output")
        return

    T = ratio2d.astype(np.float32)
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
    print(f"Saved film-like PNG: {out_png}")


def try_center_translation_from_stl(stl_path: Path):
    try:
        import trimesh
    except Exception:
        print("WARNING: trimesh not installed; cannot auto-center STL. Install: pip install trimesh")
        return None

    mesh = trimesh.load(str(stl_path), force="mesh")
    if mesh.is_empty:
        print("WARNING: STL mesh is empty; cannot center.")
        return None
    bounds = mesh.bounds
    center = (bounds[0] + bounds[1]) / 2.0
    return (-float(center[0]), -float(center[1]), -float(center[2]))


# -------------------------
# Phase space postprocess
# -------------------------

def _open_first_tree(root_path: Path):
    if uproot is None:
        raise RuntimeError("uproot is required to read phsp.root. Install: pip install uproot")
    f = uproot.open(str(root_path))
    # Find the first TTree-like object
    for k in f.keys():
        try:
            obj = f[k]
            # uproot identifies TTrees with 'classnames' that include 'TTree'
            if hasattr(obj, "keys") and hasattr(obj, "arrays"):
                return obj
        except Exception:
            continue
    raise RuntimeError(f"No readable tree found in {root_path}")


def _get_vec3(tree, base: str):
    """Return (x,y,z) arrays from either vector branch 'base' or split branches base_X etc."""
    keys = set(tree.keys())
    # split
    if f"{base}_X" in keys and f"{base}_Y" in keys and f"{base}_Z" in keys:
        ax = tree[f"{base}_X"].array(library="np")
        ay = tree[f"{base}_Y"].array(library="np")
        az = tree[f"{base}_Z"].array(library="np")
        return ax, ay, az
    # vector (might be a fixed-length array)
    if base in keys:
        a = tree[base].array(library="np")
        a = np.asarray(a)
        if a.ndim == 2 and a.shape[1] == 3:
            return a[:, 0], a[:, 1], a[:, 2]
    raise KeyError(f"Cannot find {base} or {base}_X/Y/Z in tree branches: {sorted(list(keys))[:30]}...")


def phsp_to_primary_scatter_images(
    phsp_root: Path,
    out_dir: Path,
    film_xy_mm: float,
    pix: int,
    film_z_mm: float,
    film_thickness_mm: float,
    source_pos_mm: np.ndarray,
    energy0_keV: float,
    theta_primary_deg: float,
    dE_primary_keV: float,
):
    """Build I_primary, I_scatter, SPR images (2D) from a phase space file."""

    tree = _open_first_tree(phsp_root)
    keys = set(tree.keys())

    # Energy
    ekey = None
    for cand in ["KineticEnergy", "KineticEnergy_keV", "Energy", "TotalEnergy"]:
        if cand in keys:
            ekey = cand
            break
    if ekey is None:
        raise KeyError("Cannot find an energy branch (expected KineticEnergy)")

    E = tree[ekey].array(library="np").astype(np.float64)

    # --- Energy unit sanity ---
    # Gate/Geant4 often stores energy in MeV. If user provides energy0_keV (e.g. 80)
    # and we see values around ~0.08, we convert MeV->keV.
    try:
        med_E = float(np.nanmedian(E))
    except Exception:
        med_E = float(E[0]) if len(E) else 0.0

    if float(energy0_keV) > 10.0 and 0.001 < med_E < 10.0:
        # likely MeV
        E = E * 1000.0
        if len(E):
            print(f"[phsp] Detected energy in MeV (median {med_E:.6g}). Converted to keV.")
    else:
        if len(E):
            print(f"[phsp] Energy median = {med_E:.6g} (assuming keV).")

    # Position and direction
    px, py, pz = _get_vec3(tree, "Position")
    dx, dy, dz = _get_vec3(tree, "Direction")

    # Ensure numpy float
    px = np.asarray(px, dtype=np.float64)
    py = np.asarray(py, dtype=np.float64)
    pz = np.asarray(pz, dtype=np.float64)
    # --- Coordinate sanity ---
    # Depending on Gate/OpenGATE configuration, PhaseSpaceActor may store Position
    # either in WORLD coordinates (z ~ +ODD) or in LOCAL film coordinates (z ~ 0).
    # Our primary/separation logic assumes WORLD coordinates.
    # If we detect local coordinates, we shift z by film translation (+ODD).
    try:
        med_pz = float(np.nanmedian(pz))
    except Exception:
        med_pz = float(pz[0]) if len(pz) else 0.0

    z_tol = max(5.0 * float(film_thickness_mm), 2.0)  # at least 2 mm tolerance
    is_local = abs(med_pz) <= z_tol and abs(med_pz - float(film_z_mm)) > z_tol
    is_world = abs(med_pz - float(film_z_mm)) <= z_tol
    if is_local and not is_world:
        pz = pz + float(film_z_mm)
        if len(pz):
            print(f"[phsp] Detected LOCAL film coordinates (median z={med_pz:.3f} mm). Shifted to WORLD by +{film_z_mm:.3f} mm")
    else:
        if len(pz):
            mode = "WORLD" if is_world else "UNKNOWN"
            print(f"[phsp] Position z looks {mode} (median z={med_pz:.3f} mm, expected ~{film_z_mm:.3f} mm)")
    dx = np.asarray(dx, dtype=np.float64)
    dy = np.asarray(dy, dtype=np.float64)
    dz = np.asarray(dz, dtype=np.float64)

    # Normalize direction
    dnorm = np.sqrt(dx * dx + dy * dy + dz * dz) + 1e-30
    ux = dx / dnorm
    uy = dy / dnorm
    uz = dz / dnorm

    # Expected (unscattered) direction from source to hit
    sx, sy, sz = source_pos_mm
    vx = px - sx
    vy = py - sy
    vz = pz - sz
    vnorm = np.sqrt(vx * vx + vy * vy + vz * vz) + 1e-30
    v0x = vx / vnorm
    v0y = vy / vnorm
    v0z = vz / vnorm

    # Angle between actual direction and straight-line direction
    dot = ux * v0x + uy * v0y + uz * v0z
    dot = np.clip(dot, -1.0, 1.0)
    ang = np.degrees(np.arccos(dot))

    # Energy close to mono-energy
    dE = np.abs(E - float(energy0_keV))

    primary_mask = (ang <= float(theta_primary_deg)) & (dE <= float(dE_primary_keV))

    # Quick diagnostic
    if len(ang):
        frac = float(np.mean(primary_mask))
        print(f"[phsp] Primary-like fraction: {frac:.6f} (theta<= {theta_primary_deg} deg, |dE|<= {dE_primary_keV} keV)")

    # Map to pixel indices
    half = 0.5 * float(film_xy_mm)
    pix_size = float(film_xy_mm) / float(pix)

    ix = np.floor((px + half) / pix_size).astype(np.int64)
    iy = np.floor((py + half) / pix_size).astype(np.int64)

    inside = (ix >= 0) & (ix < pix) & (iy >= 0) & (iy < pix) & np.isfinite(E)

    # Accumulate
    I_primary = np.zeros((pix, pix), dtype=np.float64)
    I_scatter = np.zeros((pix, pix), dtype=np.float64)

    # Note: image axis: [y,x]
    ip = inside & primary_mask
    iscat = inside & (~primary_mask)

    # bincount trick for speed
    def _accum(img, m):
        if not np.any(m):
            return
        lin = iy[m] * pix + ix[m]
        bc = np.bincount(lin, minlength=pix * pix).astype(np.float64)
        img += bc.reshape((pix, pix))

    _accum(I_primary, ip)
    _accum(I_scatter, iscat)

    SPR = I_scatter / (I_primary + 1e-12)

    # Save as .mhd (use a dummy reference if none)
    if sitk is not None:
        ref = sitk.GetImageFromArray(np.zeros((1, pix, pix), dtype=np.float32))
        # spacing in mm
        ref.SetSpacing((pix_size, pix_size, 1.0))
        ref.SetOrigin((-half, -half, 0.0))
        write_array_as_mhd_like(ref, I_primary[np.newaxis, :, :], out_dir / "I_primary.mhd")
        write_array_as_mhd_like(ref, I_scatter[np.newaxis, :, :], out_dir / "I_scatter.mhd")
        write_array_as_mhd_like(ref, SPR[np.newaxis, :, :], out_dir / "SPR.mhd")

    # PNG quicklooks
    if plt is not None:
        save_png(I_primary, out_dir / "I_primary_log1p.png", title="primary-like log1p", use_log=True)
        save_png(I_scatter, out_dir / "I_scatter_log1p.png", title="scatter-like log1p", use_log=True)
        save_png(SPR, out_dir / "SPR.png", title="SPR=scatter/primary", use_log=False)

    return I_primary, I_scatter, SPR


# -------------------------
# Build simulation
# -------------------------

def build_simulation(args, out_dir: Path, with_object: bool):
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

    # Object mesh
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

        rx = float(args.rot_x)
        ry = float(args.rot_y)
        rz = float(args.rot_z)
        if abs(rx) > 1e-9 or abs(ry) > 1e-9 or abs(rz) > 1e-9:
            pelvis.rotation = rot_matrix_xyz(rx, ry, rz)

        if args.center_mesh:
            t = try_center_translation_from_stl(Path(args.stl))
            if t is not None:
                pelvis.translation = [t[0] * mm, t[1] * mm, t[2] * mm]
                print(f"Centering mesh: translation = {pelvis.translation}")

    # Physics
    sim.physics_manager.physics_list_name = "G4EmLivermorePhysics"

    # Source
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

    # Total fluence image (I.mhd)
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

    # Phase space at film
    if args.write_phsp or args.separate_primary_scatter:
        ph = sim.add_actor("PhaseSpaceActor", "phsp")
        ph.attached_to = film.name
        ph.output_filename = "phsp.root"
        # request high-level attributes (Gate may split Position/Direction into components internally)
        ph.attributes = ["KineticEnergy", "Position", "Direction", "ParticleName", "EventID", "TrackID"]
        # store first step in volume to represent what reaches the film
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

    # ---- Primary/scatter separation (phase space based) ----
    if args.separate_primary_scatter:
        if uproot is None:
            raise RuntimeError("--separate_primary_scatter requires uproot. Install: pip install uproot")

        ph0 = base_out / "flat" / "phsp.root"
        ph1 = base_out / "object" / "phsp.root"
        if not ph0.exists() or not ph1.exists():
            raise FileNotFoundError(
                "Missing phsp.root. Re-run with --write_phsp (or --separate_primary_scatter which enables it)."
            )

        source_pos = np.array([0.0, 0.0, -float(args.sod)], dtype=np.float64)

        # flat
        I0p, I0s, _ = phsp_to_primary_scatter_images(
            ph0,
            base_out / "flat",
            film_xy_mm=float(args.film_xy),
            pix=int(args.pix),
            film_z_mm=float(args.odd),
            film_thickness_mm=float(args.film_thickness),
            source_pos_mm=source_pos,
            energy0_keV=float(args.energy_keV),
            theta_primary_deg=float(args.primary_theta_deg),
            dE_primary_keV=float(args.primary_dE_keV),
        )

        # object
        Ip, Is, SPR = phsp_to_primary_scatter_images(
            ph1,
            base_out / "object",
            film_xy_mm=float(args.film_xy),
            pix=int(args.pix),
            film_z_mm=float(args.odd),
            film_thickness_mm=float(args.film_thickness),
            source_pos_mm=source_pos,
            energy0_keV=float(args.energy_keV),
            theta_primary_deg=float(args.primary_theta_deg),
            dE_primary_keV=float(args.primary_dE_keV),
        )

        # Save transmission/attenuation for primary-only and total (primary+scatter)
        Tp = (Ip + eps) / (I0p + eps)
        Ap = -np.log(Tp)
        Ap = np.nan_to_num(Ap, nan=0.0, posinf=0.0, neginf=0.0)

        Itot = Ip + Is
        I0tot = I0p + I0s
        Ttot = (Itot + eps) / (I0tot + eps)
        Atot = -np.log(Ttot)
        Atot = np.nan_to_num(Atot, nan=0.0, posinf=0.0, neginf=0.0)

        write_array_as_mhd_like(ref_img, Tp[np.newaxis, :, :], base_out / "transmission_primary.mhd")
        write_array_as_mhd_like(ref_img, Ap[np.newaxis, :, :], base_out / "attenuation_primary.mhd")
        write_array_as_mhd_like(ref_img, Ttot[np.newaxis, :, :], base_out / "transmission_total.mhd")
        write_array_as_mhd_like(ref_img, Atot[np.newaxis, :, :], base_out / "attenuation_total.mhd")

        if args.make_png:
            save_png(SPR, base_out / "SPR_object.png", title="SPR (object)", use_log=False)
            save_png(Ap, base_out / "attenuation_primary.png", title="attenuation primary-like", use_log=False)
            save_png(Atot, base_out / "attenuation_total.png", title="attenuation total", use_log=False)

        print("Saved primary/scatter-derived outputs in flat/ and object/ plus *_primary/total in base out.")


def main():
    p = argparse.ArgumentParser()

    p.add_argument("--stl", type=str, required=True, help="Path to STL file (mm units recommended)")
    p.add_argument("--out", type=str, default="poc_radiograph_out", help="Output directory")

    p.add_argument("--run", choices=["flat", "object", "both"], default="both",
                   help="flat: no object, object: with STL, both: run flat+object then postprocess")

    p.add_argument("--clean", action="store_true", help="Delete output directory before running")
    p.add_argument("--center_mesh", action="store_true", help="Center mesh bbox at origin (requires trimesh)")

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

    # Phase space outputs
    p.add_argument("--write_phsp", action="store_true", help="Write phase space ROOT at film")

    # Primary/scatter separation (phase space based)
    p.add_argument("--separate_primary_scatter", action="store_true",
                   help="Postprocess phsp.root into I_primary/I_scatter/SPR + primary/total attenuation")
    p.add_argument("--primary_theta_deg", type=float, default=0.5,
                   help="PRIMARY-like: max angle (deg) between actual dir and source->hit dir")
    p.add_argument("--primary_dE_keV", type=float, default=1.0,
                   help="PRIMARY-like: max |E-E0| in keV")

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
        if args.make_png:
            cmd_base.append("--make_png")
        if args.write_phsp:
            cmd_base.append("--write_phsp")
        if args.separate_primary_scatter:
            cmd_base.append("--separate_primary_scatter")
            cmd_base += ["--primary_theta_deg", str(args.primary_theta_deg),
                         "--primary_dE_keV", str(args.primary_dE_keV)]

        if args.clean:
            cmd_base.append("--clean")

        # Run flat
        subprocess.run(cmd_base + ["--run", "flat"], check=True)

        # Run object (must NOT clean)
        cmd_obj = [c for c in cmd_base if c != "--clean"]
        subprocess.run(cmd_obj + ["--run", "object"], check=True)

        if not args.no_post:
            do_postprocess(args, base_out)
        return

    if args.run == "flat":
        run_one(args, base_out, tag="flat", with_object=False)
    elif args.run == "object":
        run_one(args, base_out, tag="object", with_object=True)


if __name__ == "__main__":
    main()
