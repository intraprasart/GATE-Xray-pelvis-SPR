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
| `run` | `stl`, `photons`, `energy_keV`, `pix`, `threads`, … | รัน simulation โมเดลเดียว (flat + object + SPR) |
| `run_pair` | `control_stl`, `fracture_stl` + ค่าเดียวกับ run | รัน control + fracture แล้ว compare อัตโนมัติ → dSPR / local-STD / entropy |
| `compare` | `control_job`, `fracture_job` | เปรียบเทียบผลของ job เก่า 2 ตัวที่ยังอยู่บนเครื่อง worker |

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
