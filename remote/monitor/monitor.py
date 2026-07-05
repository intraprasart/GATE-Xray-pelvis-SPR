"""SPR Monitor — จอเฝ้าดูคิวงานแบบ 24/7 สำหรับเครื่อง worker นี้

เปิดเว็บเซิร์ฟเวอร์เล็ก ๆ ที่ localhost แล้วรวมข้อมูล 2 ทาง:
  1) heartbeat ของ worker ในเครื่องนี้ (worker ยังมีชีวิต? กำลังรันงานไหน?)
  2) คิวงาน + worker ที่เชื่อมต่อ ดึงจาก job server (ผ่าน admin key)

admin key อยู่ในโปรเซสนี้เท่านั้น — ไม่ถูกส่งให้เบราว์เซอร์ (เบราว์เซอร์คุยกับ
localhost อย่างเดียว) จึงไม่มีปัญหา CORS และคีย์ไม่รั่วออกหน้าเว็บ

รัน:  python monitor.py     แล้วเปิด  http://localhost:8787
ตั้งค่าเพิ่มเติมได้ที่ env: SPR_MONITOR_PORT, SPR_ADMIN_KEY, SPR_SERVER_URL
"""

from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

HERE = Path(__file__).resolve().parent
WORKER_DIR = HERE.parent / "worker"
BOARD_HTML = (HERE / "board.html").read_text(encoding="utf-8")

PORT = int(os.environ.get("SPR_MONITOR_PORT", "8787"))
HEARTBEAT_FILE = WORKER_DIR / "worker.heartbeat"
HEARTBEAT_STALE_SEC = 45          # heartbeat เก่ากว่านี้ = worker น่าจะตาย
SERVER_POLL_SEC = 3               # เธรดเดียวดึงคิวจาก server ทุก N วิ (browser อ่านจาก cache)
SERVER_FRESH_SEC = 15             # ถ้าดึงสำเร็จล่าสุดภายในเวลานี้ ถือว่า server ยังออนไลน์
SERVER_TIMEOUT = 8                # timeout ต่อ request ไป server


def _load_worker_config() -> dict:
    try:
        return json.loads((WORKER_DIR / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


_CFG = _load_worker_config()
SERVER_URL = (os.environ.get("SPR_SERVER_URL") or _CFG.get("server_url", "")).rstrip("/")


def _admin_key() -> str:
    if os.environ.get("SPR_ADMIN_KEY"):
        return os.environ["SPR_ADMIN_KEY"]
    for p in (Path.home() / ".spr_admin_key", HERE / "admin_key.txt"):
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


ADMIN_KEY = _admin_key()
_SESSION = requests.Session()
_SESSION.headers.update({"X-API-Key": ADMIN_KEY})


def _read_heartbeat() -> dict | None:
    try:
        hb = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # heartbeat เก่าเกินไป → worker น่าจะตาย
    age = time.time() - HEARTBEAT_FILE.stat().st_mtime
    hb["age_sec"] = round(age, 1)
    hb["alive"] = age < HEARTBEAT_STALE_SEC
    return hb


def _fetch_server_state() -> dict:
    """ดึงคิว + worker จาก job server. คืน error field ถ้าติดต่อไม่ได้.

    เรียกจากเธรด poller เดียวเท่านั้น → ไม่มีการใช้ requests.Session ข้ามเธรด
    (ซึ่งไม่ปลอดภัยและเป็นเหตุให้ค้าง/timeout เวลา browser poll ทับกัน)
    """
    if not SERVER_URL or not ADMIN_KEY:
        return {"ok": False, "error": "ยังไม่ตั้ง server_url หรือ admin key"}
    try:
        jobs = _SESSION.get(f"{SERVER_URL}/api/jobs", params={"limit": 40},
                            timeout=SERVER_TIMEOUT).json()
        workers = _SESSION.get(f"{SERVER_URL}/api/models", timeout=SERVER_TIMEOUT).json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"ติดต่อ server ไม่ได้: {type(e).__name__}"}
    except ValueError:
        return {"ok": False, "error": "server ตอบไม่ใช่ JSON (คีย์ผิด?)"}

    running = next((j for j in jobs if j.get("status") == "running"), None)
    running_detail = None
    if running:
        try:
            d = _SESSION.get(f"{SERVER_URL}/api/jobs/{running['id']}",
                             timeout=SERVER_TIMEOUT).json()
            log = d.get("log", "") or ""
            running_detail = {**running, "log_tail": log[-4000:]}
        except (requests.RequestException, ValueError):
            running_detail = running

    counts = {}
    for j in jobs:
        counts[j.get("status", "?")] = counts.get(j.get("status", "?"), 0) + 1
    return {"ok": True, "jobs": jobs, "workers": workers,
            "running": running_detail, "counts": counts, "server_url": SERVER_URL}


# cache ที่เธรด poller เดียวเขียน, ทุก request อ่าน — ตัด internet ออกจาก browser
_CACHE = {"data": None, "last_ok": None, "last_try": None, "error": "กำลังเชื่อมต่อ server…"}
_CACHE_LOCK = threading.Lock()


def _poller() -> None:
    while True:
        s = _fetch_server_state()
        now = time.time()
        with _CACHE_LOCK:
            _CACHE["last_try"] = now
            if s.get("ok"):
                _CACHE["data"] = s          # เก็บชุดล่าสุดที่สำเร็จไว้เสมอ
                _CACHE["last_ok"] = now
                _CACHE["error"] = None
            else:
                _CACHE["error"] = s.get("error")   # ไม่ลบ data เดิม → board ไม่กระพริบ
        time.sleep(SERVER_POLL_SEC)


def build_state() -> dict:
    with _CACHE_LOCK:
        c = dict(_CACHE)
    now = time.time()
    age = (now - c["last_ok"]) if c["last_ok"] else None
    server = dict(c["data"]) if c["data"] else {"jobs": [], "workers": [], "counts": {}}
    # "ออนไลน์" = ดึงสำเร็จภายใน SERVER_FRESH_SEC (บลิปสั้น ๆ ไม่ทำให้หลุด)
    server["ok"] = age is not None and age < SERVER_FRESH_SEC
    server["age_sec"] = round(age, 1) if age is not None else None
    server["last_error"] = c["error"]
    server.setdefault("server_url", SERVER_URL)
    return {
        "now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "local_worker": _read_heartbeat(),
        "server": server,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, BOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            body = json.dumps(build_state(), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):  # เงียบ — ไม่ต้อง spam console
        pass


def main() -> None:
    if not SERVER_URL:
        print("คำเตือน: หา server_url ไม่พบ (worker/config.json) — จะเห็นแค่สถานะ local")
    if not ADMIN_KEY:
        print("คำเตือน: หา admin key ไม่พบ (~/.spr_admin_key) — จะดึงคิวจาก server ไม่ได้")
    url = f"http://localhost:{PORT}"
    print(f"SPR Monitor: {url}  (server: {SERVER_URL or '-'})")
    threading.Thread(target=_poller, daemon=True).start()   # ดึง server ในเธรดเดียว
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if os.environ.get("SPR_MONITOR_NOOPEN") != "1":   # run_monitor.bat เปิดเบราว์เซอร์เองครั้งเดียว
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
