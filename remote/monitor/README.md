# SPR Monitor — จอเฝ้าดูคิวงาน 24/7 (เครื่อง worker นี้)

หน้าจอเดียวจบ ดูได้ตลอดเวลาว่า:
- **เครื่องนี้อยู่สถานะไหน** — worker ว่าง (idle) / กำลังรัน (busy) / หยุด (offline) + uptime
- **ใครเชื่อมต่อเข้ามา** — รายชื่อ worker ที่ลงทะเบียนกับ server + ออนไลน์/ออฟไลน์
- **คิวงาน** — pending / running / done ล่าสุด 40 รายการ อัปเดตทุก 2 วินาที
- **งานที่กำลังรัน** — พร้อม log สด (auto-scroll)

## ทำงานยังไง

```
เบราว์เซอร์ (localhost) ──▶ monitor.py (เครื่องนี้) ──┬──▶ อ่าน worker.heartbeat (สถานะ local)
     ▲  ดูอย่างเดียว                                  └──▶ ดึงคิวจาก job server (ใช้ admin key)
     └───────────── หน้า board (ไม่มี key ในเบราว์เซอร์) ◀──
```

admin key อยู่ใน `monitor.py` ฝั่งเครื่องเท่านั้น เบราว์เซอร์คุยกับ `localhost` อย่างเดียว
→ ไม่มีปัญหา CORS และคีย์ไม่รั่วออกหน้าเว็บ ถึง internet หลุด ก็ยังเห็นสถานะ worker ในเครื่อง

## เปิดใช้

```powershell
# ต้องมี worker รันอยู่ (worker.py เขียน worker.heartbeat ให้ monitor อ่าน)
D:\GATE-Xray-pelvis-SPR\remote\monitor\run_monitor.bat
```

เบราว์เซอร์จะเปิดที่ **http://localhost:8787** เอง — เปิดค้างไว้ทั้งวันได้เลย

### ให้เปิดเองตอน login (24/7)

```powershell
powershell -ExecutionPolicy Bypass -File install_monitor.ps1
```

## แหล่งข้อมูล / การตั้งค่า

monitor หา config อัตโนมัติ:
- **server_url** จาก `../worker/config.json`
- **admin key** จาก `~/.spr_admin_key` (หรือ `admin_key.txt` ข้างไฟล์ หรือ env `SPR_ADMIN_KEY`)
- พอร์ต localhost เปลี่ยนได้ด้วย env `SPR_MONITOR_PORT` (default 8787)

> monitor นี้ **ดูอย่างเดียว** ไม่ส่ง/ยกเลิกงาน — ใช้ dashboard บน server สำหรับสั่งงาน
