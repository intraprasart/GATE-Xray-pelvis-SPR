# คำสั่งรัน — จัดหมวด "ต้องโหลดไฟล์ 3D" vs "ไม่ต้องโหลด"

นิยาม:
- **ต้องโหลดไฟล์ 3D** = คำสั่งที่อ่านเนื้อ STL จริง (วาง object ในฉาก) → ไฟล์ใน `models/` ต้องมีเนื้อจริง
  (ถ้าไฟล์ถูก offload ขึ้น iCloud ต้อง `brctl download` ก่อน — ในเครื่องใหม่ที่ git clone มาจะมีเนื้อครบอยู่แล้ว)
- **ไม่ต้องโหลด** = คำสั่งที่ไม่อ่านเนื้อ STL (flat-field) หรือ post-processing บนไฟล์ `.mhd` ที่มีอยู่แล้ว

หลักการในโค้ด: object mesh ถูกเพิ่ม `if with_object:` เท่านั้น → `--run flat` จะไม่แตะไฟล์ STL เลย

---

## 🟩 หมวด A — ไม่ต้องโหลดไฟล์ 3D

### A1. Flat field (I0 reference, ไม่มี object)
ใช้ทำภาพอ้างอิงไม่มีวัตถุ — ต้องส่ง `--stl` ให้ argparse ผ่าน แต่โค้ดไม่อ่านเนื้อไฟล์ (อย่าใส่ `--center_mesh`)
```bash
cd scripts/pelvis_AP_primary_scatter
python poc_pelvis_radiograph_gate10_AP_primary_scatter_v2.py \
    --stl ../../models/control_mm.stl --out out_flat_only \
    --run flat --photons 40000000 --make_png \
    --write_phsp --separate_primary_scatter
```

### A2. Post-processing / วิเคราะห์ (ทำงานบน .mhd ที่จำลองไว้แล้ว)
```bash
cd scripts/cone_film
python make_spr_v3.py        # สร้าง SPR map จาก I / I0 (.mhd)
python rebin_maps.py         # rebin/ลด noise ของ map
python make_spr.py           # เวอร์ชันแรก (อ้างอิง)
```

### A3. แปลง intensity → ภาพคล้ายฟิล์ม
```bash
cd scripts/pelvis_AP
python Make_film.py          # อ่าน .mhd ที่มีอยู่ → ภาพ film-like PNG
```

---

## 🟥 หมวด B — ต้องโหลดไฟล์ 3D (วาง object จริง)

### B1. ★ Pipeline ล่าสุด — Primary/Scatter + SPR (test8)
```bash
cd scripts/pelvis_AP_primary_scatter

# --- 1 ล้านโฟตอน (เร็ว ~1.5 นาที/ชุด) ---
python poc_pelvis_radiograph_gate10_AP_primary_scatter_v2.py \
    --stl ../../models/control_mm.stl --out out_control \
    --run both --photons 1000000 --make_png --center_mesh \
    --write_phsp --separate_primary_scatter \
    --primary_theta_deg 0.5 --primary_dE_keV 1.0

python poc_pelvis_radiograph_gate10_AP_primary_scatter_v2.py \
    --stl ../../models/0_54x1000.stl --out out_fracture \
    --run both --photons 1000000 --make_png --center_mesh \
    --write_phsp --separate_primary_scatter \
    --primary_theta_deg 0.5 --primary_dE_keV 1.0

# --- 40 ล้านโฟตอน (สถิติสูง ~56–58 นาที/ชุด, single-thread) ---
python poc_pelvis_radiograph_gate10_AP_primary_scatter_v2.py \
    --stl ../../models/control_mm.stl --out out_control_4E7 \
    --run both --photons 40000000 --make_png --center_mesh \
    --write_phsp --separate_primary_scatter \
    --primary_theta_deg 0.5 --primary_dE_keV 1.0

python poc_pelvis_radiograph_gate10_AP_primary_scatter_v2.py \
    --stl ../../models/0_54x1000.stl --out out_fracture_4E7 \
    --run both --photons 40000000 --make_png --center_mesh \
    --write_phsp --separate_primary_scatter \
    --primary_theta_deg 0.5 --primary_dE_keV 1.0
```
> เร่งความเร็วได้ด้วย `--threads N` (โค้ดรองรับ — การรันเดิมใช้ thread เดียว)

### B2. ภาพรังสี pelvis AP พื้นฐาน (test6/7) + มุมหมุน
```bash
cd scripts/pelvis_AP
python poc_pelvis_radiograph_gate10_AP.py --stl ../../models/control_mm.stl --run both --make_png --center_mesh
python poc_pelvis_radiograph_gate10_AP.py --stl ../../models/control_mm.stl --run both --make_png --center_mesh --rot_x 90
python poc_pelvis_radiograph_gate10_AP.py --stl ../../models/control_mm.stl --run both --make_png --center_mesh --rot_x -90
python poc_pelvis_radiograph_gate10_AP.py --stl ../../models/control_mm.stl --run both --make_png --center_mesh --rot_y 90
python poc_pelvis_radiograph_gate10_AP.py --stl ../../models/control_mm.stl --run both --make_png --center_mesh --rot_y -90
```

### B3. Cone-beam film ช่วงแรก (test4/5 — water phantom, default ไม่ใช้ pelvis)
```bash
cd scripts/cone_film
python poc_cone_film_gate10_v3.py --photons 1000000      # default: SOD800 ODD800 pix128 G4_WATER 80keV
python poc_cone_film_gate10.py    --photons 1000000      # เวอร์ชันแรก
```

### B4. Utilities ที่อ่าน STL
```bash
cd scripts/utils
python scale_stl.py            # scale control.stl → control_mm.stl (หน่วย mm)
python bench_stl_speed_v2.py   # benchmark ความเร็ว tessellation ของ STL
```

---

## ตารางจับคู่ run → ไฟล์ 3D ที่ใช้ (อ้างอิงผลเดิม test8)

| Output เดิม | STL ที่ใช้ | โฟตอน | หมายเหตุ |
|---|---|---|---|
| `out_control`        | control_mm.stl | 1M  | ปกติ |
| `out_fracture`       | 0_54x1000.stl  | 1M  | แตก 0.54mm |
| `out_0_54_fracture`  | 0_54x1000.stl  | 1M  | แตก 0.54mm (ซ้ำ) |
| `out_control_4E7`    | control_mm.stl | 40M | ปกติ สถิติสูง |
| `out_fracture_4E7`   | 0_54x1000.stl  | 40M | แตก สถิติสูง |

> ผลลัพธ์เหล่านี้ถูกใส่ใน `.gitignore` (จำลองใหม่ได้) — repo เก็บเฉพาะ input + โค้ด
