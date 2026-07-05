@echo off
rem SPR Monitor launcher — เปิดจอเฝ้าดูคิว 24/7 (เปิดเบราว์เซอร์ครั้งเดียว, วน restart ถ้า crash)
set PYTHONIOENCODING=utf-8
set SPR_MONITOR_NOOPEN=1
cd /d "%~dp0"
start "" "http://localhost:8787"
:loop
"D:\GATE-Xray-pelvis-SPR\.venv\Scripts\python.exe" -u monitor.py
echo monitor exited, restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
