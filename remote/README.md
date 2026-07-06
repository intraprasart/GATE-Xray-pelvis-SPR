# Remote simulation — สั่งงานจาก server ภายนอก

ระบบสั่งรัน GATE simulation บนเครื่อง Windows จากระยะไกล แล้วส่งผลกลับไปเก็บบน server

```
┌─────────────┐   1. ส่ง job (dashboard/API)   ┌──────────────────┐
│  ผู้ใช้      │ ─────────────────────────────▶ │  VPS: job server  │
│  (browser)  │ ◀───────────────────────────── │  FastAPI + SQLite │
└─────────────┘   4. ดูผล/ดาวน์โหลด             └──────────────────┘
                                                   ▲            │
                                    3. อัปโหลดผล   │            │ 2. worker poll รับงาน
                                    (zip: PNG/mhd/ │            ▼
                                     metrics)   ┌──────────────────────────┐
                                                │ เครื่อง Windows (worker)  │
                                                │ GATE 10 / Geant4 + venv  │
                                                └──────────────────────────┘
```

- **ทิศทางการเชื่อมต่อออกจากเครื่อง worker เสมอ** (polling) — ไม่ต้องเปิด port
  ที่บ้าน/โรงเรียน ไม่ต้องตั้ง port forwarding
- ผลที่ส่งกลับ: PNG previews, แผนที่ .mhd (SPR/attenuation/transmission),
  summary.csv, config.json, stats.txt — **ไม่รวม** phsp.root (ใหญ่ระดับ GB,
  เก็บไว้ที่เครื่อง worker ใน `runs/remote_jobs/<job_id>/`)

## โฟลเดอร์

| path | คืออะไร | รันที่ไหน |
|---|---|---|
| `server/` | job server (FastAPI) + dashboard + Docker | VPS |
| `worker/` | agent ที่ poll งานมารัน | เครื่อง Windows |

## ประเภทงาน (job types)

| type | params หลัก | ทำอะไร |
|---|---|---|
| `run` | `stl`, `photons`, `energy_keV`, `pix`, `mode`/`n_procs`, … | รัน simulation โมเดลเดียว (flat + object + SPR) |
| `run_pair` | `control_stl`, `fracture_stl` + ค่าเดียวกับ run | รัน control + fracture แล้ว compare อัตโนมัติ → dSPR / local-STD / entropy |
| `run_seeds` | `control_stl`, `fracture_stl`, `n_seeds` (2–30) + ค่าเดียวกับ run | รัน control+fracture **N รอบ** ด้วย seed อิสระ → **mean/std/significance(t) map + สรุป t/p ต่อ metric** (สำหรับวิเคราะห์นัยสำคัญ) |
| `compare` | `control_job`, `fracture_job` | เปรียบเทียบผลของ job เก่า 2 ตัวที่ยังอยู่บนเครื่อง worker |

### multi-seed (`run_seeds`) — วัดนัยสำคัญ

Monte Carlo run เดียวให้ 1 realization → บอกไม่ได้ว่าสัญญาณ fracture จริงหรือ noise
`run_seeds` รัน pipeline ซ้ำ N รอบ (base seed ต่างกัน; ในแต่ละรอบ control กับ fracture
ใช้ seed เดียวกัน = common-random-numbers ช่วยลด variance ของผลต่าง) แล้วรวมเป็น:
- **significance map ต่อพิกเซล** — `t = mean / (std/√N)` ของ dSPR/dSTD/dEntropy
- **สรุป ROI** — mean ± SD, t-statistic, p-value ต่อ metric

> ต่างจาก run mode (single/balanced/max) ที่แตกโฟตอนของ**การรันเดียว** — `run_seeds`
> ทำหลาย realization เพื่อประเมิน variance. แนะนำ 5–10 seed × โฟตอนปานกลาง (max mode)

### โหมดการรัน — ใช้ CPU/RAM เต็มเครื่อง

opengate **ไม่รองรับ multithread บน Windows** จึงใช้วิธี **แตกงานเป็นหลายโปรเซส** (sharding):
แต่ละโปรเซสยิง photon ส่วนหนึ่งด้วย seed ต่างกัน แล้วรวมผล (fluence บวกกัน,
primary/scatter counts บวกกัน) — เทียบเท่าการรันยาวครั้งเดียวแต่เร็วขึ้นตามจำนวนคอร์

| `mode` | จำนวนโปรเซส | เหมาะกับ |
|---|---|---|
| `single` | 1 | งานเล็ก / ใช้เครื่องทำอย่างอื่นไปด้วย |
| `balanced` | คอร์ − 2 | ค่าเริ่มต้น — เร็วแต่ยังเหลือคอร์ให้ระบบ |
| `max` | ทุกคอร์ | เร็วที่สุด ใช้เครื่องเต็มที่ |

จำนวนโปรเซสถูก **cap ตาม RAM ว่างอัตโนมัติ** (≈ RAM×0.85 ÷ 1.5GB ต่อโปรเซส) กัน OOM
หรือระบุ `n_procs` เป็นตัวเลขตรง ๆ ก็ได้ (จะ override `mode`)

### ตำแหน่ง source 3 มิติ + บีบลำแสง (v1.1)

ปรับตำแหน่ง source ได้อิสระใน 3 มิติ (`src_x`, `src_y`, `src_z` มม.; ดีฟอลต์ `(0,0,-800)`)
ลำแสงเล็งเข้าศูนย์กลางวัตถุเสมอ และ **ฉากรับอยู่หลังวัตถุตามแนวลำแสงโดยอัตโนมัติ**
(engine ใช้ beam-frame transform — การย้าย source รอบวัตถุเทียบเท่าการหมุนวัตถุในทางฟิสิกส์)

| param | ความหมาย |
|---|---|
| `src_x/y/z` | ตำแหน่ง source ใน world (มม.) |
| `odd` | ระยะวัตถุ→ฉากรับ ตามแนวลำแสง (มม.) |
| `field_mm` | เส้นผ่านศูนย์กลางลำแสงที่ฉากรับ (บีบเป็นจุด); `0` = เต็มฟิล์ม |

หน้า dashboard มีการ์ด **Preview** วาดวัตถุ/source/ลำแสง/ฉากรับ 4 มุมมอง (plan/front/side/3D)
แบบเรขาคณิต (ไม่ใช่ Monte Carlo) ให้เห็นตำแหน่งคร่าว ๆ ก่อนกดรันจริง

พารามิเตอร์ตัวเลขทั้งหมดถูก clamp อยู่ในช่วงปลอดภัยฝั่ง worker และชื่อ STL
ต้องเป็นไฟล์ใน `models/` เท่านั้น (กัน path traversal / คำสั่งแปลกปลอม)

## เริ่มใช้งาน

1. Deploy server: ดู [server/README.md](server/README.md)
2. ตั้ง worker บนเครื่อง Windows: ดู [worker/README.md](worker/README.md)
3. เปิด dashboard `http://<server>:8642` ใส่ admin key แล้วกดส่งงานได้เลย

### สั่งงานผ่าน API ตรง ๆ

```bash
curl -X POST http://<server>:8642/api/jobs \
  -H "X-API-Key: <ADMIN_KEY>" -H "Content-Type: application/json" \
  -d '{"type":"run_pair","params":{"control_stl":"control_mm.stl",
       "fracture_stl":"0_54x1000.stl","photons":1000000}}'

# เช็คสถานะ / ดึงผล
curl -H "X-API-Key: <ADMIN_KEY>" http://<server>:8642/api/jobs/<id>
curl -OJ -H "X-API-Key: <ADMIN_KEY>" http://<server>:8642/api/jobs/<id>/results.zip
```
