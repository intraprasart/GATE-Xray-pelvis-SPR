# SPR Worker — ตั้งค่าบนเครื่อง Windows ที่รัน simulation

ต้องมี: repo นี้ + venv ที่ติดตั้ง opengate แล้ว (ดู README หลักของ repo)

## ตั้งค่า

```powershell
cd D:\GATE-Xray-pelvis-SPR\remote\worker
copy config.example.json config.json
notepad config.json     # ใส่ server_url และ worker_key ให้ตรงกับฝั่ง server
```

## ทดลองรันด้วยมือ

```powershell
D:\GATE-Xray-pelvis-SPR\.venv\Scripts\python.exe -u worker.py
```

เห็นบรรทัด `SPR worker '<ชื่อเครื่อง>' เริ่มทำงาน` แล้วลองส่ง job จาก dashboard

## ให้รันอัตโนมัติตอนเปิดเครื่อง

```powershell
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

จะได้ Task Scheduler ชื่อ **SPR-Worker** ที่:
- เริ่มเองตอน login (หน้าต่างย่อเป็น minimized)
- `run_worker.bat` วน restart worker ให้เองถ้าตาย

ดู log ได้ที่ `worker.log` ข้างไฟล์นี้

## หมายเหตุ

- ผลรันเต็ม (รวม phsp.root) เก็บไว้ที่ `runs/remote_jobs/<job_id>/`
  บนเครื่องนี้ ตั้ง `keep_local_runs: false` ใน config ถ้าไม่อยากเก็บ
- worker รับงานทีละงาน (เครื่องเดียว คิวเดียว) — งานที่เหลือค้างคิวบน server
