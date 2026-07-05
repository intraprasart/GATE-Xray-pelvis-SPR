@echo off
rem SPR worker launcher — วน restart อัตโนมัติถ้า worker ตาย
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
:loop
"D:\GATE-Xray-pelvis-SPR\.venv\Scripts\python.exe" -u worker.py
echo worker exited, restarting in 15s...
timeout /t 15 /nobreak >nul
goto loop
