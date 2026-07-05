# SPR Job Server — deploy บน VPS

## วิธีที่ 1: Docker (แนะนำ)

```bash
cd remote/server
cat > .env <<'EOF'
SPR_ADMIN_KEY=ใส่คีย์ยาวๆของคุณเอง
SPR_WORKER_KEY=อีกคีย์หนึ่งไม่ซ้ำกัน
EOF
docker compose up -d --build
```

เปิด `http://<server>:8642` → ใส่ admin key → ใช้งานได้เลย
ข้อมูลทั้งหมด (SQLite + ผลลัพธ์) อยู่ใน `./data`

## วิธีที่ 2: Python ตรง ๆ + systemd

```bash
cd remote/server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

sudo tee /etc/systemd/system/spr-jobserver.service <<EOF
[Unit]
Description=SPR Job Server
After=network.target

[Service]
User=$USER
WorkingDirectory=$(pwd)
Environment=SPR_ADMIN_KEY=ใส่คีย์ของคุณ
Environment=SPR_WORKER_KEY=อีกคีย์หนึ่ง
Environment=SPR_DATA_DIR=$(pwd)/data
ExecStart=$(pwd)/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8642
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now spr-jobserver
```

## เปิด firewall

```bash
sudo ufw allow 8642/tcp     # หรือเฉพาะ IP ที่ไว้ใจ
```

## แนะนำเพิ่ม (ถ้ามีโดเมน): ใส่ HTTPS ผ่าน reverse proxy

เช่น Caddy — สองบรรทัดจบ ได้ใบรับรองอัตโนมัติ:

```
sim.example.com {
    reverse_proxy localhost:8642
}
```

แล้วตั้ง `server_url` ฝั่ง worker เป็น `https://sim.example.com`
