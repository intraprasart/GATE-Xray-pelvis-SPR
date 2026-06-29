# GATE X-ray pelvis — SPR (Scatter-to-Primary) signature

Monte Carlo simulation (OpenGATE 10 / Geant4) ของภาพรังสี pelvis แบบ AP
เพื่อศึกษา **signature การกระเจิงรังสี (SPR = scatter / primary)** และดูว่า
สามารถใช้แยก "กระดูกแตก (fracture)" ออกจาก "กระดูกปกติ (control)" ได้หรือไม่

Repo นี้รวม **โค้ดเวอร์ชันสะอาด + UI + สคริปต์เดิม + ไฟล์ 3D ต้นฉบับ + คำสั่งรัน** ให้พกพาไปรันต่อเครื่องอื่นได้ครบ
(ไม่รวมผลลัพธ์ที่จำลองใหม่ได้ — ดู `.gitignore`)

---

## ⭐ เริ่มเร็ว — ใช้งานผ่าน UI (แนะนำ)

```bash
pip install -r requirements.txt
streamlit run app.py
```
เบราว์เซอร์จะเปิดหน้า UI ที่:
- **แท็บ "รันจำลอง"** — เลือกโมเดล STL, ปรับ photons/energy/geometry ด้วย slider, กดรัน แล้วดูภาพผล (film-like, SPR, attenuation) ในหน้าเดียว
- **แท็บ "เปรียบเทียบ"** — เลือก 2 การรัน (control vs fracture) คำนวณ dSPR / local-STD / entropy พร้อม ROI อัตโนมัติ

> UI สั่ง GATE ผ่าน subprocess ให้อัตโนมัติ (OpenGATE รันได้ 1 engine/process) — ไม่ต้องจำคำสั่ง

### หรือใช้ผ่าน command line
```bash
python -m gate_pelvis.cli run --stl models/control_mm.stl --out runs/control --photons 1000000
python -m gate_pelvis.cli run --stl models/0_54x1000.stl  --out runs/fracture --photons 1000000
python -m gate_pelvis.cli compare --control runs/control --fracture runs/fracture --out runs/roi_analysis
```

### หรือใช้เป็นไลบรารีใน Python
```python
from gate_pelvis import SimConfig, run_simulation, compare_runs
cfg = SimConfig(stl="models/control_mm.stl", out="runs/control", photons=1_000_000)
run_simulation(cfg)
res = compare_runs("runs/control", "runs/fracture")
print(res.summary)
```

---

## โครงสร้างโฟลเดอร์

```
.
├── app.py                        # ★ Streamlit UI  (streamlit run app.py)
├── gate_pelvis/                  # ★ แพ็กเกจเวอร์ชันสะอาด (refactor จากสคริปต์เดิม)
│   ├── config.py                 #   SimConfig — พารามิเตอร์ทั้งหมดในที่เดียว
│   ├── imaging.py                #   อ่าน/เขียน .mhd + PNG + ภาพ film-like
│   ├── phasespace.py             #   phsp.root → primary/scatter/SPR
│   ├── engine.py                 #   สร้าง+รัน GATE (ไฟล์เดียวที่ import opengate)
│   ├── _worker.py                #   subprocess worker (1 engine/process)
│   ├── pipeline.py               #   ออร์เคสเตรต flat+object + post-process
│   ├── analysis.py               #   เปรียบเทียบ ROI: dSPR / local-STD / entropy
│   └── cli.py                    #   command-line:  python -m gate_pelvis.cli ...
├── README.md
├── requirements.txt              # Python dependencies (รวม streamlit/scipy/skimage)
├── docs/
│   └── RUN_COMMANDS.md           # คำสั่งรันสคริปต์เดิม จัดหมวด "ต้องโหลด/ไม่ต้องโหลด"
├── models/                       # ไฟล์ 3D ต้นฉบับ (STL) ที่ใช้ทำการทดลองทั้งหมด
│   ├── control.stl               #   pelvis ต้นฉบับ (ก่อน scale)
│   ├── control_mm.stl            #   control ในหน่วย mm  ← ใช้เป็น "ปกติ" ในทุกการรัน
│   ├── 0_54x1000.stl             #   fracture ช่องว่าง 0.54 mm
│   └── 0.306x1000.stl            #   fracture ช่องว่าง 0.306 mm
└── scripts/                      # สคริปต์ต้นฉบับเดิม (เก็บไว้อ้างอิง)
    ├── pelvis_AP_primary_scatter/    # ★ pipeline ล่าสุด (แยก primary/scatter + SPR map)
    │   ├── poc_pelvis_radiograph_gate10_AP_primary_scatter_v2.py   # MAIN
    │   └── poc_pelvis_radiograph_gate10_AP_primary_scatter.py      # v1 (อ้างอิง)
    ├── pelvis_AP/                    # ภาพรังสี pelvis AP พื้นฐาน
    │   ├── poc_pelvis_radiograph_gate10_AP.py
    │   ├── poc_pelvis_radiograph_gate10.py
    │   └── Make_film.py              #   แปลง intensity → ภาพคล้ายฟิล์ม
    ├── cone_film/                    # การทดลอง cone-beam ช่วงแรก (water phantom)
    │   ├── poc_cone_film_gate10_v3.py
    │   ├── poc_cone_film_gate10.py
    │   ├── make_spr_v3.py            #   สร้าง SPR map จาก .mhd
    │   ├── make_spr.py
    │   └── rebin_maps.py
    └── utils/
        ├── scale_stl.py             #   scale STL → mm (สร้าง control_mm.stl จาก control.stl)
        └── bench_stl_speed_v2.py    #   benchmark ความเร็ว tessellation
```

---

## ติดตั้ง (เครื่องใหม่)

```bash
git clone <repo-url> "GATE-Xray-pelvis-SPR"
cd "GATE-Xray-pelvis-SPR"
python3.10 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

> GATE 10 จะดาวน์โหลด Geant4 + ฐานข้อมูลฟิสิกส์ครั้งแรกที่รัน (ต้องต่อเน็ต)

---

## พารามิเตอร์มาตรฐานของการทดลอง (pipeline ล่าสุด)

| พารามิเตอร์ | ค่า default |
|---|---|
| Engine | OpenGATE 10 (Geant4), physics list `G4EmLivermorePhysics` |
| Source | gamma, point, **80 keV mono** |
| Geometry | SOD 800 mm + ODD 400 mm (AP), film 400×400 mm, 512×512 px |
| วัสดุวัตถุ | `G4_BONE_COMPACT_ICRU` |
| แยก primary/scatter | มุม < 0.5° **และ** \|E−E₀\| < 1 keV → primary, นอกนั้น scatter |

---

## คำสั่งรันแบบย่อ (เต็มดูที่ [docs/RUN_COMMANDS.md](docs/RUN_COMMANDS.md))

รันจากในโฟลเดอร์สคริปต์ (อ้าง path ของ model แบบ relative):

```bash
cd scripts/pelvis_AP_primary_scatter

# control, สถิติสูง 40M โฟตอน
python poc_pelvis_radiograph_gate10_AP_primary_scatter_v2.py \
    --stl ../../models/control_mm.stl --out out_control_4E7 \
    --run both --photons 40000000 --make_png --center_mesh \
    --write_phsp --separate_primary_scatter \
    --primary_theta_deg 0.5 --primary_dE_keV 1.0

# fracture 0.54 mm, สถิติสูง 40M โฟตอน
python poc_pelvis_radiograph_gate10_AP_primary_scatter_v2.py \
    --stl ../../models/0_54x1000.stl --out out_fracture_4E7 \
    --run both --photons 40000000 --make_png --center_mesh \
    --write_phsp --separate_primary_scatter \
    --primary_theta_deg 0.5 --primary_dE_keV 1.0
```

---

## สถานะงาน / สิ่งที่ต้องทำต่อ (จากการตรวจผลล่าสุด test8)

- ✅ Pipeline ครบ: geometry → primary/scatter separation → SPR map → ROI analysis (dSPR, local-STD, entropy)
- ⚠️ ผลล่าสุดยัง **inconclusive**:
  1. รันแค่ **1 seed** → คำนวณนัยสำคัญ (across-seed std) ไม่ได้ → significance map ว่างเปล่า
  2. การวิเคราะห์ ROI ทำบนชุด **1M โฟตอน** ทั้งที่ชุด **40M** รันเสร็จแล้วแต่ยังไม่ได้วิเคราะห์
  3. สัญญาณ dSPR ที่จุด fracture ยังจมใน noise (entropy เป็น metric ที่มีแนวโน้มดีสุด)
- 🔜 ถัดไป: (1) รัน ROI analysis ซ้ำบนชุด 40M, (2) เพิ่ม multi-seed (≥5–10) เปิด significance,
  (3) เปิด `--threads`, (4) sweep ขนาด fracture หา detectability threshold

> หมายเหตุ: สคริปต์วิเคราะห์ ROI เดิมรันแบบ interactive และ **ไม่ถูกบันทึก** — ควรเขียนใหม่ให้ reproduce ได้
