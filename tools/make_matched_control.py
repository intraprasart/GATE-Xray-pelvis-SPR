"""สร้าง "matched control" จากโมเดล fracture — control ที่ฐาน mesh เดียวกับ fracture
แต่ถมร่องรอยแตกด้วยกระดูก เพื่อให้ `fracture − control` แยกเฉพาะผลของรอยแตกจริง ๆ
(ตัด confound เรื่องความต่างของ mesh ทั้งก้อน ที่ทำให้ผลก่อนหน้า inconclusive)

วิธี:
  1. หาตำแหน่ง+ทิศทางร่อง จากคู่โมเดล backup ที่ tessellation เดียวกัน (vertex ตรงกัน)
     — เทียบ vertex ระหว่าง gap เล็กกับ gap ใหญ่ ที่ขยับ = ผนังร่อง; PCA ให้ระนาบ+normal
  2. union โมเดล fracture (watertight) กับกล่องบางวางตามระนาบร่อง (manifold3d)
     — กล่องบางตาม normal (คร่อมร่อง) + ขนาดระนาบพอดีร่อง → ถมกระดูกเฉพาะร่อง
  3. ตรวจว่า control ต่างจาก fracture "เฉพาะบริเวณร่อง" (นอกร่องผิวตรงกัน < 0.05mm)

ต้องมี: trimesh, manifold3d, scipy
ใช้:  python tools/make_matched_control.py <fracture.stl> [--backup-small B/0.306x1000.stl --backup-large B/1mmx1000.stl]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree

trimesh.util.log.setLevel(60)


def locate_gap(backup_small: Path, backup_large: Path):
    """คืน (center, Vt, inplane_extent) ของร่อง ในพิกัด bbox-centered ของโมเดล small.

    ต้องเป็นคู่ backup ที่ tessellation เดียวกัน (vertex index ตรงกัน) — ร่องคือ
    vertices ที่ขยับเมื่อร่องกว้างขึ้น.
    """
    ms = trimesh.load(str(backup_small), force="mesh")
    ml = trimesh.load(str(backup_large), force="mesh")
    if len(ms.vertices) != len(ml.vertices):
        raise SystemExit("backup ทั้งสองต้อง tessellation เดียวกัน (vertex count ต่างกัน)")
    c = (ms.bounds[0] + ms.bounds[1]) / 2
    moved = np.linalg.norm(ms.vertices - ml.vertices, axis=1) > 0.02
    pts = ms.vertices[moved] - c
    cen = pts.mean(0)
    _, _, Vt = np.linalg.svd(pts - cen, full_matrices=False)   # Vt[-1] = normal ของร่อง
    proj = (pts - cen) @ Vt.T
    return cen, Vt, (proj.max(0) - proj.min(0))


def make_control(fracture: Path, cen, Vt, inplane, gap_mm: float, out: Path) -> None:
    m = trimesh.load(str(fracture), force="mesh")
    c = (m.bounds[0] + m.bounds[1]) / 2
    box = trimesh.creation.box(extents=[inplane[0] + 1.0, inplane[1] + 1.0,
                                        max(1.6, gap_mm + 0.8)])
    R = np.eye(4)
    R[:3, :3] = Vt.T                       # หมุนกล่องให้ thin-axis ตรงกับ normal ของร่อง
    box.apply_transform(R)
    box.apply_translation(cen + c)         # ไปตำแหน่งร่องจริง (world ของ mesh)
    filled = trimesh.boolean.union([m, box], engine="manifold")

    d, _ = cKDTree(m.vertices).query(filled.vertices, k=1)
    far = np.linalg.norm(filled.vertices - (cen + c), axis=1) > 15
    ok = filled.is_watertight and filled.body_count == 1 and d[far].max() < 0.05
    print(f"{fracture.name}: watertight={filled.is_watertight} bodies={filled.body_count} "
          f"vol+={(filled.volume - m.volume) / 1000:.3f}cm3 "
          f"นอกร่องตรง(max {d[far].max():.4f}mm) -> {'OK' if ok else 'FAILED'}")
    if not ok:
        raise SystemExit("control ไม่ผ่านการตรวจ (ไม่ watertight / re-tessellate ทั้งก้อน)")
    filled.export(str(out))
    print(f"  saved {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fracture", help="โมเดล fracture (watertight) ใน models/")
    ap.add_argument("--backup-small", default="backup_model/0.306x1000.stl")
    ap.add_argument("--backup-large", default="backup_model/1mmx1000.stl")
    ap.add_argument("--gap-mm", type=float, default=1.0, help="ความกว้างร่องของ fracture (mm)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    frac = Path(a.fracture)
    out = Path(a.out) if a.out else frac.with_name(frac.stem + "_filled_control.stl")
    cen, Vt, inplane = locate_gap(Path(a.backup_small), Path(a.backup_large))
    print(f"gap center (bbox-centered) = {np.round(cen, 1)}  in-plane = {np.round(inplane, 1)} mm")
    make_control(frac, cen, Vt, inplane, a.gap_mm, out)


if __name__ == "__main__":
    main()
