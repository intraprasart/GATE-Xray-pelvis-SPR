"""SPR Job Server — สั่งงาน simulation จากภายนอก + เก็บผลลัพธ์

FastAPI app เดียวจบ: job queue (SQLite) + รับอัปโหลดผล + dashboard
รันบน VPS:  uvicorn app:app --host 0.0.0.0 --port 8642 --proxy-headers

Environment variables (จำเป็น):
    SPR_ADMIN_KEY   คีย์สำหรับคน (ส่ง job / ดูผล)
    SPR_WORKER_KEY  คีย์สำหรับเครื่อง worker (รับ job / ส่งผล)
    SPR_DATA_DIR    โฟลเดอร์เก็บ DB + ผลลัพธ์ (default: ./data)
    SPR_STALE_RUNNING_SECONDS  ถือว่า worker หายถ้าไม่ส่งสัญญาณเกินนี้ (default 600)
"""

from __future__ import annotations

import hmac
import json
import os
import shutil
import sqlite3
import threading
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

DATA_DIR = Path(os.environ.get("SPR_DATA_DIR", "./data")).resolve()
ADMIN_KEY = os.environ.get("SPR_ADMIN_KEY", "")
WORKER_KEY = os.environ.get("SPR_WORKER_KEY", "")
STALE_RUNNING_SECONDS = int(os.environ.get("SPR_STALE_RUNNING_SECONDS", "600"))

MAX_LOG_CHARS = 800_000            # เก็บ log ท้ายสุดไม่เกินนี้ต่อ job
MAX_LOG_POST_BYTES = 2_000_000     # ขนาด body สูงสุดต่อการ POST /log หนึ่งครั้ง
MAX_UPLOAD_BYTES = 2_000_000_000   # ขนาด zip (บีบอัด) สูงสุดที่รับ
MAX_EXTRACT_BYTES = 6_000_000_000  # ขนาดรวมหลังคลายบีบอัด (กัน zip bomb)
MAX_PENDING_JOBS = 200             # กันคิวถูกถล่ม
TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

MESH_DIR = DATA_DIR / "meshes"
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "results").mkdir(exist_ok=True)
MESH_DIR.mkdir(exist_ok=True)
MAX_MESH_BYTES = 4_000_000        # point cloud ต่อโมเดลไม่ควรเกินนี้

if not ADMIN_KEY or not WORKER_KEY:
    raise RuntimeError("ต้องตั้ง SPR_ADMIN_KEY และ SPR_WORKER_KEY ก่อนรัน server")

app = FastAPI(title="SPR Job Server", docs_url=None, redoc_url=None)

# ----------------------------------------------------------------------
# DB (SQLite + global lock ก็พอสำหรับงานคิวเดียว)
# ----------------------------------------------------------------------

_db = sqlite3.connect(DATA_DIR / "jobs.db", check_same_thread=False)
_db.row_factory = sqlite3.Row
_lock = threading.Lock()

_db.executescript("""
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  type TEXT NOT NULL,
  params TEXT NOT NULL,
  status TEXT NOT NULL,
  claimed_at TEXT,
  finished_at TEXT,
  worker_id TEXT,
  log TEXT NOT NULL DEFAULT '',
  metrics TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS workers (
  worker_id TEXT PRIMARY KEY,
  last_seen TEXT,
  info TEXT
);
""")
_db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FMT)


def _cutoff_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(TIMESTAMP_FMT)


def _safe_json(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"_unparsed": str(text)[:500]}


def _job_row(row: sqlite3.Row, with_log: bool = False) -> dict:
    d = {k: row[k] for k in row.keys() if k != "log"}
    d["params"] = _safe_json(d["params"]) or {}
    d["metrics"] = _safe_json(d["metrics"])
    if with_log:
        d["log"] = row["log"]
    return d


def _requeue_stale_running() -> None:
    """Mark jobs whose worker went silent as failed (called under _lock)."""
    cutoff = _cutoff_iso(STALE_RUNNING_SECONDS)
    _db.execute(
        "UPDATE jobs SET status='failed', finished_at=?, "
        "error='worker หายระหว่างรัน (ไม่มีสัญญาณเกินกำหนด)' "
        "WHERE status='running' AND claimed_at < ? AND NOT EXISTS "
        "  (SELECT 1 FROM workers w WHERE w.worker_id = jobs.worker_id AND w.last_seen >= ?)",
        (_now(), cutoff, cutoff))


# ----------------------------------------------------------------------
# Auth — รับคีย์ทาง header X-API-Key หรือ cookie (dashboard) เท่านั้น
#         ไม่รับทาง query string อีกต่อไป (กันคีย์หลุดลง access log / history)
# ----------------------------------------------------------------------

def _key_matches(candidate: str | None, expected: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, expected)


def require_admin(request: Request):
    if _key_matches(request.headers.get("X-API-Key"), ADMIN_KEY):
        return
    if _key_matches(request.cookies.get("spr_admin"), ADMIN_KEY):
        return
    raise HTTPException(401, "invalid admin key")


def require_worker(request: Request):
    if _key_matches(request.headers.get("X-API-Key"), WORKER_KEY):
        return
    raise HTTPException(401, "invalid worker key")


@app.post("/api/login")
async def login(request: Request):
    """แลกคีย์เป็น cookie (HttpOnly) ให้ dashboard ใช้ — คีย์ไม่โผล่ใน URL อีก"""
    body = await request.json()
    key = body.get("key", "")
    if not _key_matches(key, ADMIN_KEY):
        raise HTTPException(401, "invalid key")
    secure = (request.headers.get("x-forwarded-proto", request.url.scheme) == "https")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("spr_admin", key, httponly=True, samesite="lax",
                    secure=secure, max_age=30 * 24 * 3600)
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("spr_admin")
    return resp


# ----------------------------------------------------------------------
# Admin API — ส่ง job / ดูสถานะ / เอาผล
# ----------------------------------------------------------------------

VALID_TYPES = {"run", "run_pair", "compare"}


@app.post("/api/jobs", dependencies=[Depends(require_admin)])
async def submit_job(request: Request):
    body = await request.json()
    jtype = body.get("type")
    params = body.get("params", {})
    if jtype not in VALID_TYPES:
        raise HTTPException(400, f"type ต้องเป็นหนึ่งใน {sorted(VALID_TYPES)}")
    if not isinstance(params, dict):
        raise HTTPException(400, "params ต้องเป็น object")
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    with _lock:
        n = _db.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]
        if n >= MAX_PENDING_JOBS:
            raise HTTPException(429, f"คิวเต็ม (pending {n} งาน) — รอให้ประมวลผลก่อน")
        _db.execute(
            "INSERT INTO jobs (id, created_at, type, params, status) VALUES (?,?,?,?,?)",
            (job_id, _now(), jtype, json.dumps(params), "pending"))
        _db.commit()
    return {"id": job_id, "status": "pending"}


@app.get("/api/jobs", dependencies=[Depends(require_admin)])
def list_jobs(limit: int = 100):
    limit = max(1, min(int(limit), 1000))
    with _lock:
        rows = _db.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_job_row(r) for r in rows]


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_admin)])
def get_job(job_id: str):
    with _lock:
        row = _db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "job not found")
    return _job_row(row, with_log=True)


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(require_admin)])
def cancel_job(job_id: str):
    with _lock:
        cur = _db.execute(
            "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='pending'",
            (_now(), job_id))
        _db.commit()
    if cur.rowcount == 0:
        raise HTTPException(409, "ยกเลิกได้เฉพาะ job ที่ยัง pending")
    return {"id": job_id, "status": "cancelled"}


@app.post("/api/jobs/{job_id}/force-fail", dependencies=[Depends(require_admin)])
def force_fail_job(job_id: str):
    """บังคับปิด job ที่ค้าง running (เช่น worker ตายไปแล้ว)"""
    with _lock:
        cur = _db.execute(
            "UPDATE jobs SET status='failed', finished_at=?, "
            "error='ปิดโดยผู้ดูแล (force-fail)' WHERE id=? AND status IN ('pending','running')",
            (_now(), job_id))
        _db.commit()
    if cur.rowcount == 0:
        raise HTTPException(409, "บังคับปิดได้เฉพาะ job ที่ยัง pending/running")
    return {"id": job_id, "status": "failed"}


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_admin)])
def delete_job(job_id: str):
    with _lock:
        cur = _db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        _db.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "job not found")
    zip_path = DATA_DIR / "results" / f"{job_id}.zip"
    dir_path = DATA_DIR / "results" / job_id
    zip_path.unlink(missing_ok=True)
    if dir_path.exists():
        shutil.rmtree(dir_path, ignore_errors=True)
    return {"id": job_id, "deleted": True}


@app.get("/api/jobs/{job_id}/results.zip", dependencies=[Depends(require_admin)])
def download_results(job_id: str):
    zip_path = DATA_DIR / "results" / f"{job_id}.zip"
    if not zip_path.exists():
        raise HTTPException(404, "ยังไม่มีผลลัพธ์ของ job นี้")
    return FileResponse(zip_path, filename=f"{job_id}.zip")


def _results_base(job_id: str) -> Path:
    return (DATA_DIR / "results" / job_id).resolve()


@app.get("/api/jobs/{job_id}/files", dependencies=[Depends(require_admin)])
def list_result_files(job_id: str):
    base = _results_base(job_id)
    if not base.exists():
        return []
    return sorted(str(p.relative_to(base)).replace("\\", "/")
                  for p in base.rglob("*") if p.is_file())


@app.get("/api/jobs/{job_id}/file", dependencies=[Depends(require_admin)])
def get_result_file(job_id: str, path: str):
    base = _results_base(job_id)
    target = (base / path).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(target)


@app.get("/api/models", dependencies=[Depends(require_admin)])
def list_models():
    """รายชื่อ STL + สถานะ worker ที่เคยลงทะเบียน"""
    with _lock:
        rows = _db.execute("SELECT * FROM workers").fetchall()
    out = []
    for r in rows:
        info = _safe_json(r["info"]) or {}
        out.append({"worker_id": r["worker_id"], "last_seen": r["last_seen"],
                    "models": info.get("models", []), "machine": info.get("machine", {})})
    return out


# ----------------------------------------------------------------------
# Worker API — เครื่อง simulation เรียกเข้ามา
# ----------------------------------------------------------------------

def _safe_mesh_name(name: str) -> str:
    if not name or any(c in name for c in "/\\") or ".." in name or not name.endswith(".stl"):
        raise HTTPException(400, "ชื่อโมเดลไม่ถูกต้อง")
    return name


@app.get("/api/worker/mesh-manifest", dependencies=[Depends(require_worker)])
def mesh_manifest():
    """worker เช็คว่ามี mesh ตัวไหน (hash อะไร) แล้วบ้าง → ส่งเฉพาะที่เปลี่ยน"""
    out = {}
    for p in MESH_DIR.glob("*.json"):
        try:
            out[p.stem + ".stl"] = json.loads(p.read_text(encoding="utf-8")).get("hash")
        except (OSError, ValueError):
            continue
    return out


@app.post("/api/worker/meshes", dependencies=[Depends(require_worker)])
async def upload_mesh(request: Request):
    raw = await request.body()
    if len(raw) > MAX_MESH_BYTES:
        raise HTTPException(413, "mesh preview ใหญ่เกินไป")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    name = _safe_mesh_name(str(body.get("name", "")))
    rec = {"name": name, "hash": body.get("hash"),
           "points": body.get("points", []), "bbox": body.get("bbox")}
    if not isinstance(rec["points"], list):
        raise HTTPException(400, "points ต้องเป็น array")
    (MESH_DIR / (name[:-4] + ".json")).write_text(
        json.dumps(rec), encoding="utf-8")
    return {"ok": True, "name": name, "points": len(rec["points"])}


@app.get("/api/meshes/{name}", dependencies=[Depends(require_admin)])
def get_mesh(name: str):
    p = MESH_DIR / (_safe_mesh_name(name)[:-4] + ".json")
    if not p.exists():
        raise HTTPException(404, "ยังไม่มี preview ของโมเดลนี้ (รอ worker อัปโหลด)")
    return json.loads(p.read_text(encoding="utf-8"))


@app.post("/api/worker/register", dependencies=[Depends(require_worker)])
async def register_worker(request: Request):
    body = await request.json()
    worker_id = str(body.get("worker_id", "unknown"))[:64]
    info = json.dumps({"models": body.get("models", []), "machine": body.get("machine", {})})
    with _lock:
        _db.execute(
            "INSERT INTO workers (worker_id, last_seen, info) VALUES (?,?,?) "
            "ON CONFLICT(worker_id) DO UPDATE SET last_seen=excluded.last_seen, info=excluded.info",
            (worker_id, _now(), info))
        _db.commit()
    return {"ok": True}


@app.post("/api/worker/claim", dependencies=[Depends(require_worker)])
async def claim_job(request: Request):
    body = await request.json()
    worker_id = str(body.get("worker_id", "unknown"))[:64]
    with _lock:
        _db.execute("UPDATE workers SET last_seen=? WHERE worker_id=?", (_now(), worker_id))
        _requeue_stale_running()
        row = _db.execute(
            "SELECT * FROM jobs WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            _db.commit()
            return Response(status_code=204)
        _db.execute(
            "UPDATE jobs SET status='running', claimed_at=?, worker_id=? WHERE id=?",
            (_now(), worker_id, row["id"]))
        _db.commit()
    return {"id": row["id"], "type": row["type"], "params": _safe_json(row["params"]) or {}}


@app.post("/api/worker/jobs/{job_id}/log", dependencies=[Depends(require_worker)])
async def append_log(job_id: str, request: Request):
    raw = await request.body()
    if len(raw) > MAX_LOG_POST_BYTES:
        raw = raw[-MAX_LOG_POST_BYTES:]
    text = raw.decode("utf-8", errors="replace")
    with _lock:
        row = _db.execute("SELECT log, worker_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "job not found")
        new_log = (row["log"] + text)[-MAX_LOG_CHARS:]
        _db.execute("UPDATE jobs SET log=? WHERE id=?", (new_log, job_id))
        # log ที่ไหลเข้ามา = worker ยังมีชีวิต → กัน stale-requeue ระหว่างงานยาว
        if row["worker_id"]:
            _db.execute("UPDATE workers SET last_seen=? WHERE worker_id=?",
                        (_now(), row["worker_id"]))
        _db.commit()
    return {"ok": True}


def _extract_zip(zip_path: Path, extract_dir: Path) -> None:
    """แตก zip อย่างปลอดภัย: กัน zip-slip + จำกัดขนาดรวมหลังคลาย (blocking)."""
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True)
    base = extract_dir.resolve()
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        for m in zf.infolist():
            dest = (extract_dir / m.filename).resolve()
            if not dest.is_relative_to(base):
                continue  # zip-slip — ข้าม
            if m.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, dest.open("wb") as out:
                while chunk := src.read(1 << 20):
                    total += len(chunk)
                    if total > MAX_EXTRACT_BYTES:
                        raise HTTPException(413, "ผลลัพธ์ใหญ่เกินไปหลังคลายบีบอัด")
                    out.write(chunk)


@app.post("/api/worker/jobs/{job_id}/complete", dependencies=[Depends(require_worker)])
async def complete_job(job_id: str,
                       status: str = Form(...),
                       metrics: str = Form(default=""),
                       error: str = Form(default=""),
                       file: UploadFile | None = None):
    if status not in ("done", "failed"):
        raise HTTPException(400, "status ต้องเป็น done หรือ failed")
    if metrics:
        try:
            json.loads(metrics)
        except ValueError:
            raise HTTPException(400, "metrics ต้องเป็น JSON ที่ถูกต้อง")
    with _lock:
        row = _db.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "job not found")

    if file is not None:
        zip_path = DATA_DIR / "results" / f"{job_id}.zip"
        size = 0
        with zip_path.open("wb") as f:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    f.close()
                    zip_path.unlink(missing_ok=True)
                    raise HTTPException(413, "ไฟล์ใหญ่เกินไป")
                f.write(chunk)
        try:
            await run_in_threadpool(_extract_zip, zip_path, DATA_DIR / "results" / job_id)
        except zipfile.BadZipFile:
            raise HTTPException(400, "ไฟล์ zip เสียหาย")

    with _lock:
        _db.execute(
            "UPDATE jobs SET status=?, finished_at=?, metrics=?, error=? WHERE id=?",
            (status, _now(), metrics or None, error or None, job_id))
        _db.commit()
    return {"id": job_id, "status": status}


# ----------------------------------------------------------------------
# Dashboard (หน้าเดียว ไม่มี build step)
# ----------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>SPR Job Server</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600&family=Fira+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#f3f4f6; --surface:#ffffff; --surface-alt:#fbfafb;
    --fg:#191c22; --fg-muted:#5b6472; --fg-soft:#64748b;
    --border:#e7e9ee; --border-strong:#d4d8e0;
    --primary:#c1121f; --primary-hover:#9a0e18; --primary-active:#7d0b13;
    --primary-tint:#fef2f3; --primary-tint-2:#fbdfe2;
    --ring:rgba(193,18,31,.35);
    --ok:#16a34a; --ok-text:#15803d; --off:#d92b38;
    --font-sans:'Fira Sans',system-ui,-apple-system,'Segoe UI',sans-serif;
    --font-mono:'Fira Code',ui-monospace,'SF Mono','Cascadia Code',monospace;
    --shadow-sm:0 1px 2px rgba(16,20,28,.05),0 1px 3px rgba(16,20,28,.05);
    --shadow-md:0 6px 24px rgba(16,20,28,.10);
    --radius:14px; --radius-sm:9px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--font-sans);
       font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased;}

  /* ---------- header ---------- */
  header{position:sticky;top:0;z-index:40;
    background:linear-gradient(135deg,#c1121f 0%,#8b0d16 60%,#6d0a11 100%);
    color:#fff;padding:14px 22px;display:flex;flex-wrap:wrap;gap:12px;
    justify-content:space-between;align-items:center;
    box-shadow:0 2px 12px rgba(109,10,17,.30);}
  .brand{display:flex;align-items:center;gap:13px;min-width:0;}
  .brand-icon{width:40px;height:40px;flex:none;display:grid;place-items:center;
    border-radius:11px;background:rgba(255,255,255,.14);
    box-shadow:inset 0 0 0 1px rgba(255,255,255,.22);}
  .brand-icon svg{width:23px;height:23px;stroke:#fff;}
  .brand h1{font-size:18px;line-height:1.15;margin:0;font-weight:700;letter-spacing:.2px;}
  .brand-sub{margin:1px 0 0;font-family:var(--font-mono);font-size:11.5px;
    color:#ffd9dc;letter-spacing:.3px;}
  .worker-status{display:inline-flex;align-items:center;gap:8px;
    font-size:12.5px;font-weight:500;color:#ffe7e9;
    background:rgba(0,0,0,.16);padding:6px 12px;border-radius:999px;
    box-shadow:inset 0 0 0 1px rgba(255,255,255,.14);}
  .wdot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--fg-soft);}
  .wdot.on{background:var(--ok);box-shadow:0 0 0 3px rgba(34,197,94,.30);}
  .wdot.off{background:var(--off);box-shadow:0 0 0 3px rgba(217,43,56,.35);}

  /* ---------- layout ---------- */
  main{max-width:1120px;margin:22px auto;padding:0 16px;}
  .card{background:var(--surface);border-radius:var(--radius);
    border:1px solid var(--border);padding:20px 22px;margin-bottom:18px;
    box-shadow:var(--shadow-sm);}
  .card-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
    margin:0 0 16px;padding-bottom:12px;border-bottom:1px solid var(--border);}
  .card-head h2{position:relative;font-size:15px;margin:0;font-weight:600;
    padding-left:12px;color:var(--fg);}
  .card-head h2::before{content:"";position:absolute;left:0;top:2px;bottom:2px;
    width:4px;border-radius:3px;background:var(--primary);}
  .card-hint{font-family:var(--font-mono);font-size:11.5px;color:var(--fg-soft);}
  .card-head .btn{margin-left:auto;}

  /* ---------- form ---------- */
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;}
  .field{display:flex;flex-direction:column;gap:5px;min-width:0;}
  label{font-size:12.5px;font-weight:500;color:var(--fg-muted);}
  input,select{padding:9px 11px;border:1px solid var(--border-strong);
    border-radius:var(--radius-sm);width:100%;font-size:14px;font-family:inherit;
    color:var(--fg);background:var(--surface);min-height:40px;
    transition:border-color .15s ease,box-shadow .15s ease;}
  input[type=number]{font-family:var(--font-mono);}
  input:hover,select:hover{border-color:#b9bfca;}
  input:focus,select:focus{outline:none;border-color:var(--primary);
    box-shadow:0 0 0 3px var(--ring);}
  select{cursor:pointer;appearance:none;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%235b6472' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 10px center;padding-right:34px;}
  .form-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
    margin:18px 0 0;padding-top:16px;border-top:1px solid var(--border);}
  .submit-msg{font-size:13px;font-weight:500;}
  .submit-msg.ok{color:var(--ok-text);} .submit-msg.err{color:var(--primary);}

  /* ---------- buttons ---------- */
  .btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;
    border:1px solid transparent;border-radius:var(--radius-sm);cursor:pointer;text-decoration:none;
    font-family:inherit;font-size:14px;font-weight:600;padding:0 18px;min-height:42px;
    transition:background .15s ease,border-color .15s ease,transform .06s ease,box-shadow .15s ease;
    white-space:nowrap;}
  .btn svg{width:16px;height:16px;stroke:currentColor;flex:none;}
  .btn:focus-visible{outline:none;box-shadow:0 0 0 3px var(--ring);}
  .btn:active{transform:translateY(1px);}
  .btn-primary{background:var(--primary);color:#fff;}
  .btn-primary:hover{background:var(--primary-hover);}
  .btn-primary:active{background:var(--primary-active);}
  .btn-primary:disabled{opacity:.85;cursor:progress;}
  .btn-ghost{background:var(--surface);color:var(--fg);border-color:var(--border-strong);}
  .btn-ghost:hover{background:var(--primary-tint);border-color:var(--primary-tint-2);color:var(--primary-hover);}
  .btn-sm{min-height:32px;padding:0 12px;font-size:12.5px;font-weight:500;border-radius:7px;gap:5px;}
  .btn-sm svg{width:14px;height:14px;}
  .spinner{width:15px;height:15px;border:2px solid rgba(255,255,255,.45);
    border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;}
  @keyframes spin{to{transform:rotate(360deg)}}

  /* ---------- table ---------- */
  .table-wrap{overflow-x:auto;margin:0 -4px;}
  table{width:100%;border-collapse:collapse;font-size:13px;min-width:560px;}
  thead th{text-align:left;padding:9px 10px;font-size:11px;font-weight:600;
    text-transform:uppercase;letter-spacing:.5px;color:var(--fg-soft);
    border-bottom:2px solid var(--border);white-space:nowrap;}
  tbody td{padding:11px 10px;border-bottom:1px solid var(--border);vertical-align:middle;}
  tbody tr{transition:background .12s ease;}
  tbody tr:hover{background:var(--primary-tint);}
  td.jid{font-family:var(--font-mono);font-size:12px;color:var(--fg-muted);}
  td.actions{white-space:nowrap;text-align:right;}
  td.actions .btn{margin-left:6px;}
  .empty{padding:34px 10px;text-align:center;color:var(--fg-soft);}
  .empty svg{width:34px;height:34px;stroke:var(--border-strong);margin-bottom:8px;}

  /* ---------- status pills ---------- */
  .st{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;
    font-size:11.5px;font-weight:600;line-height:1.4;letter-spacing:.2px;}
  .st::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;flex:none;}
  .st.pending{background:#fef3c7;color:#92400e;} .st.running{background:#ffe8d1;color:#c2410c;}
  .st.done{background:#dcfce7;color:#15803d;} .st.failed{background:var(--primary-tint-2);color:var(--primary-active);}
  .st.cancelled{background:#eef0f3;color:#4b5563;}

  /* ---------- dialog ---------- */
  dialog{border:0;border-radius:16px;width:min(950px,94vw);max-height:90vh;padding:0;
    box-shadow:var(--shadow-md);color:var(--fg);}
  dialog::backdrop{background:rgba(24,10,12,.45);backdrop-filter:blur(2px);}
  .dlg-inner{padding:22px 24px;}
  #dlg_title{font-family:var(--font-mono);font-size:14.5px;font-weight:600;
    margin:0 0 16px;padding-bottom:12px;border-bottom:1px solid var(--border);color:var(--fg);
    word-break:break-word;overflow-wrap:anywhere;}
  .metrics{width:100%;border-collapse:collapse;font-size:13px;}
  .metrics td{padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:top;}
  .metrics td:first-child{color:var(--fg-muted);width:34%;font-weight:500;}
  .metrics code{font-family:var(--font-mono);font-size:12px;color:var(--fg);
    background:var(--surface-alt);padding:1px 5px;border-radius:5px;
    word-break:break-all;border:1px solid var(--border);}
  .imgs{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-top:14px;}
  .imgs figure{margin:0;}
  .imgs img{width:100%;border-radius:9px;border:1px solid var(--border-strong);display:block;
    transition:box-shadow .15s ease;}
  .imgs a:hover img{box-shadow:0 0 0 3px var(--primary-tint-2);}
  .imgs figcaption{font-family:var(--font-mono);font-size:10.5px;color:var(--fg-soft);
    word-break:break-all;margin-top:4px;}
  pre{background:#161b22;color:#d7dde5;padding:14px;border-radius:10px;font-size:12px;
    font-family:var(--font-mono);max-height:320px;overflow:auto;white-space:pre-wrap;
    border:1px solid #23303f;}
  .dlg-foot{text-align:right;margin:16px 0 0;padding-top:14px;border-top:1px solid var(--border);}
  .dlg-log-title{font-size:13px;font-weight:600;color:var(--fg-muted);margin:16px 0 8px;}

  @media (max-width:560px){
    header{padding:12px 16px;} .brand h1{font-size:16px;}
    main{margin:16px auto;} .card{padding:16px;}
  }
  @media (prefers-reduced-motion:reduce){
    *{transition:none!important;animation:none!important;}
  }
</style></head><body>
<header>
  <div class="brand">
    <span class="brand-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M7 12h10"/><path d="M9 8v8"/><path d="M15 8v8"/></svg></span>
    <div class="brand-text">
      <h1>SPR Job Server</h1>
      <p class="brand-sub">GATE · Monte Carlo X-ray pelvis</p>
    </div>
  </div>
  <div id="workerline" class="worker-status"><span class="wdot"></span>กำลังเชื่อมต่อ…</div>
</header>
<main>
  <section class="card">
    <div class="card-head">
      <h2>ส่งงานใหม่</h2>
      <span class="card-hint">run_pair: control + fracture + เปรียบเทียบ</span>
    </div>
    <div class="grid">
      <div class="field"><label for="jtype">ประเภทงาน</label>
        <select id="jtype"><option value="run_pair">run_pair (control vs fracture)</option>
        <option value="run">run (โมเดลเดียว)</option></select></div>
      <div class="field"><label for="control_stl">Control STL</label><select id="control_stl"></select></div>
      <div class="field" id="fr_wrap"><label for="fracture_stl">Fracture STL</label><select id="fracture_stl"></select></div>
      <div class="field"><label for="photons">Photons</label><input id="photons" type="number" value="1000000"></div>
      <div class="field"><label for="energy">Energy (keV)</label><input id="energy" type="number" value="80"></div>
      <div class="field"><label for="pix">Pixels</label><input id="pix" type="number" value="512"></div>
      <div class="field"><label for="mode">โหมดการรัน (CPU)</label>
        <select id="mode">
          <option value="single">single — 1 คอร์ (เบาที่สุด)</option>
          <option value="balanced" selected>balanced — เว้น 2 คอร์ให้เครื่อง</option>
          <option value="max">max — ใช้ CPU/RAM เต็มเครื่อง</option>
        </select></div>
    </div>
    <div class="form-actions">
      <button id="submitBtn" class="btn btn-primary" onclick="submitJob()">
        <svg viewBox="0 0 24 24" fill="none" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.9" aria-hidden="true" focusable="false"><path d="M22 2 11 13"/><path d="m22 2-7 20-4-9-9-4Z"/></svg>
        ส่งงาน</button>
      <span id="submitmsg" class="submit-msg"></span>
    </div>
  </section>
  <section class="card">
    <div class="card-head">
      <h2>🔭 Preview ตำแหน่ง (ก่อนรันจริง)</h2>
      <span class="card-hint">ดูวัตถุ · source · ลำแสง · ฉากรับ แบบเรขาคณิต (ไม่ใช่ Monte Carlo)</span>
    </div>
    <div class="grid">
      <div class="field"><label for="pv_model">โมเดลที่ดู</label><select id="pv_model"></select></div>
      <div class="field"><label for="pv_sx">Source X (มม.)</label><input id="pv_sx" type="number" value="0"></div>
      <div class="field"><label for="pv_sy">Source Y (มม.)</label><input id="pv_sy" type="number" value="0"></div>
      <div class="field"><label for="pv_sz">Source Z (มม.)</label><input id="pv_sz" type="number" value="-800"></div>
      <div class="field"><label for="pv_odd">ระยะวัตถุ→ฉากรับ ODD (มม.)</label><input id="pv_odd" type="number" value="400"></div>
      <div class="field"><label for="pv_field">ขนาดลำแสง ⌀ (มม., 0=เต็มฟิล์ม)</label><input id="pv_field" type="number" value="0"></div>
    </div>
    <div id="pv_info" style="font-family:var(--font-mono);font-size:12px;color:var(--fg-soft);margin:2px 0 10px"></div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px">
      <figure style="margin:0"><canvas id="pv_plan" width="360" height="260" style="width:100%;border:1px solid var(--line);border-radius:8px;background:#fbfafb"></canvas>
        <figcaption style="font-family:var(--font-mono);font-size:11px;color:var(--fg-soft);margin-top:3px">Plan — มองจากด้านบน (X↔, Z↕)</figcaption></figure>
      <figure style="margin:0"><canvas id="pv_front" width="360" height="260" style="width:100%;border:1px solid var(--line);border-radius:8px;background:#fbfafb"></canvas>
        <figcaption style="font-family:var(--font-mono);font-size:11px;color:var(--fg-soft);margin-top:3px">Front — มองตามแนวลำแสง (X↔, Y↕)</figcaption></figure>
      <figure style="margin:0"><canvas id="pv_side" width="360" height="260" style="width:100%;border:1px solid var(--line);border-radius:8px;background:#fbfafb"></canvas>
        <figcaption style="font-family:var(--font-mono);font-size:11px;color:var(--fg-soft);margin-top:3px">Side — มองจากด้านข้าง (Z↔, Y↕)</figcaption></figure>
      <figure style="margin:0"><canvas id="pv_iso" width="360" height="260" style="width:100%;border:1px solid var(--line);border-radius:8px;background:#fbfafb"></canvas>
        <figcaption style="font-family:var(--font-mono);font-size:11px;color:var(--fg-soft);margin-top:3px">3D — ไอโซเมตริก</figcaption></figure>
    </div>
    <div style="margin-top:12px;display:flex;gap:14px;align-items:center;flex-wrap:wrap">
      <button class="btn btn-ghost btn-sm" onclick="pvReset()">รีเซ็ตตำแหน่งเริ่มต้น</button>
      <label style="font-size:12.5px;display:flex;gap:7px;align-items:center;cursor:pointer">
        <input type="checkbox" id="pv_apply" checked style="width:auto"> ใช้ตำแหน่ง/ลำแสงนี้ตอนกดส่งงาน</label>
      <span style="font-family:var(--font-mono);font-size:11px;color:var(--fg-soft)">
        <span style="color:#c1121f">●</span> source/ลำแสง &nbsp; <span style="color:#2563eb">▭</span> ฉากรับ &nbsp; <span style="color:#3c4655">·</span> วัตถุ</span>
    </div>
  </section>
  <section class="card">
    <div class="card-head">
      <h2>รายการงาน</h2>
      <button class="btn btn-ghost btn-sm" onclick="refresh()">
        <svg viewBox="0 0 24 24" fill="none" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" aria-hidden="true" focusable="false"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M3 21v-5h5"/></svg>
        รีเฟรช</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>ประเภท</th><th>สถานะ</th><th>สร้างเมื่อ</th><th>เครื่อง</th><th></th></tr></thead>
        <tbody id="jobs"></tbody>
      </table>
    </div>
  </section>
</main>
<dialog id="dlg" aria-labelledby="dlg_title"><div class="dlg-inner">
  <h2 id="dlg_title"></h2>
  <div id="dlg_body"></div>
  <div class="dlg-foot"><button class="btn btn-ghost btn-sm" onclick="dlg.close()">ปิด</button></div>
</div></dialog>
<script>
const dlg = document.getElementById('dlg');
let watching = null;
let modelsCache = '';

// escape ทุกค่าที่มาจาก worker/job ก่อนยัดลง innerHTML (กัน XSS)
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));

// server เก็บเวลาเป็น UTC (ลงท้าย Z) — แปลงเป็นเวลาไทย (UTC+7, ไม่มี DST) ให้ดู
const fmtTH = iso => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return new Date(d.getTime() + 7 * 3600 * 1000).toISOString().slice(0, 19).replace('T', ' ');
};

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts });
  if (r.status === 401) { await ensureLogin(true); throw new Error('unauthorized'); }
  return r;
}

async function ensureLogin(force) {
  // ถ้า cookie ยังใช้ได้ ก็ผ่าน ไม่ต้องถาม
  if (!force) {
    const r = await fetch('/api/models', { credentials: 'same-origin' });
    if (r.ok) return true;
  }
  for (;;) {
    const key = prompt('ใส่ Admin API key:');
    if (key === null) return false;
    const r = await fetch('/api/login', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
    if (r.ok) return true;
    alert('คีย์ไม่ถูกต้อง ลองใหม่');
  }
}

document.getElementById('jtype').onchange = e =>
  document.getElementById('fr_wrap').style.display = e.target.value === 'run_pair' ? '' : 'none';

async function loadModels() {
  try {
    const r = await api('/api/models'); if (!r.ok) return;
    const ws = await r.json();
    const models = [...new Set(ws.flatMap(w => w.models))];
    const sig = models.join('|');
    if (sig !== modelsCache) {            // สร้าง option ใหม่เฉพาะตอนรายชื่อเปลี่ยน (กันรีเซ็ตที่เลือกไว้)
      modelsCache = sig;
      for (const id of ['control_stl', 'fracture_stl']) {
        const sel = document.getElementById(id);
        const cur = sel.value;
        sel.innerHTML = models.map(m => `<option>${esc(m)}</option>`).join('') ||
                        '<option value="">(ยังไม่มี worker ลงทะเบียน)</option>';
        if (models.includes(cur)) sel.value = cur;
        else if (id === 'fracture_stl') sel.value = models.find(m => m !== document.getElementById('control_stl').value) || models[0] || '';
      }
      const pvsel = document.getElementById('pv_model');
      const pvcur = pvsel.value;
      pvsel.innerHTML = models.map(m => `<option>${esc(m)}</option>`).join('') ||
                        '<option value="">(ยังไม่มีโมเดล)</option>';
      pvsel.value = models.includes(pvcur) ? pvcur : (models[0] || '');
      pvLoadMesh(pvsel.value);
    }
    const online = ws.filter(w => Date.now() - Date.parse(w.last_seen) < 90000);
    const line = document.getElementById('workerline');
    line.innerHTML = online.length
      ? `<span class="wdot on"></span>worker ออนไลน์: ${esc(online.map(w => w.worker_id).join(', '))}`
      : '<span class="wdot off"></span>ไม่มี worker ออนไลน์';
  } catch (e) { /* 401 handled in api() */ }
}

async function submitJob() {
  const btn = document.getElementById('submitBtn');
  const msg = document.getElementById('submitmsg');
  const t = document.getElementById('jtype').value;
  const p = {
    photons: +document.getElementById('photons').value,
    energy_keV: +document.getElementById('energy').value,
    pix: +document.getElementById('pix').value,
    mode: document.getElementById('mode').value,
  };
  if (t === 'run_pair') {
    p.control_stl = document.getElementById('control_stl').value;
    p.fracture_stl = document.getElementById('fracture_stl').value;
  } else { p.stl = document.getElementById('control_stl').value; }
  if (document.getElementById('pv_apply').checked) {   // แนบตำแหน่ง source 3D + ลำแสงจาก Preview
    const v = pvVals();
    p.src_x = v.sx; p.src_y = v.sy; p.src_z = v.sz;
    p.odd = v.odd; p.field_mm = v.field;
  }
  btn.disabled = true;
  const html0 = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span>กำลังส่ง…';
  msg.textContent = '';
  try {
    const r = await api('/api/jobs', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ type: t, params: p }) });
    if (r.ok) { msg.className = 'submit-msg ok'; msg.textContent = 'ส่งงานสำเร็จ'; }
    else { msg.className = 'submit-msg err'; msg.textContent = 'ส่งไม่สำเร็จ: ' + (await r.text()); }
  } catch (e) {
    msg.className = 'submit-msg err'; msg.textContent = 'ส่งไม่สำเร็จ';
  } finally {
    btn.disabled = false; btn.innerHTML = html0; refresh();
  }
}

async function refresh() {
  let jobs;
  try { const r = await api('/api/jobs'); if (!r.ok) return; jobs = await r.json(); }
  catch (e) { return; }
  const tb = document.getElementById('jobs');
  if (!jobs.length) {
    tb.innerHTML = `<tr><td colspan="6"><div class="empty">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
      <div>ยังไม่มีงานในคิว</div></div></td></tr>`;
    return;
  }
  tb.innerHTML = jobs.map(j => `<tr>
    <td class="jid">${esc(j.id)}</td><td>${esc(j.type)}</td>
    <td><span class="st ${esc(j.status)}">${esc(j.status)}</span></td>
    <td>${esc(fmtTH(j.created_at))}</td><td>${esc(j.worker_id || '-')}</td>
    <td class="actions"><button class="btn btn-ghost btn-sm" onclick="showJob('${esc(j.id)}')">ดู</button>
      ${j.status === 'done' ? `<a class="btn btn-ghost btn-sm" href="/api/jobs/${encodeURIComponent(j.id)}/results.zip" download>zip</a>` : ''}
      ${j.status === 'pending' ? `<button class="btn btn-ghost btn-sm" onclick="cancelJob('${esc(j.id)}')">ยกเลิก</button>` : ''}
      ${j.status === 'running' ? `<button class="btn btn-ghost btn-sm" onclick="forceFail('${esc(j.id)}')">บังคับปิด</button>` : ''}
      ${j.status !== 'running' ? `<button class="btn btn-ghost btn-sm" onclick="delJob('${esc(j.id)}')">ลบ</button>` : ''}
    </td></tr>`).join('');
}

async function cancelJob(id) { await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }); refresh(); }
async function forceFail(id) { if (confirm('บังคับปิด job ' + id + ' ที่ค้างอยู่?')) { await api(`/api/jobs/${encodeURIComponent(id)}/force-fail`, { method: 'POST' }); refresh(); } }
async function delJob(id) { if (confirm('ลบ job ' + id + '?')) { await api(`/api/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' }); refresh(); } }

async function showJob(id) {
  watching = id;
  let j, files;
  try {
    j = await (await api(`/api/jobs/${encodeURIComponent(id)}`)).json();
    files = await (await api(`/api/jobs/${encodeURIComponent(id)}/files`)).json();
  } catch (e) { return; }
  document.getElementById('dlg_title').textContent = `${j.id} — ${j.type} [${j.status}]`;
  let html = `<table class="metrics"><tr><td>พารามิเตอร์</td><td><code>${esc(JSON.stringify(j.params))}</code></td></tr>`;
  if (j.metrics) for (const [k, v] of Object.entries(j.metrics))
    html += `<tr><td>${esc(k)}</td><td>${esc(typeof v === 'number' ? v.toPrecision(6) : v)}</td></tr>`;
  if (j.error) html += `<tr><td>error</td><td style="color:var(--primary)">${esc(j.error)}</td></tr>`;
  html += '</table>';
  const pngs = files.filter(f => f.endsWith('.png'));
  if (pngs.length) html += '<div class="imgs">' + pngs.map(f => {
    const u = `/api/jobs/${encodeURIComponent(id)}/file?path=${encodeURIComponent(f)}`;
    return `<figure><a href="${u}" target="_blank"><img loading="lazy" src="${u}"></a>
            <figcaption>${esc(f)}</figcaption></figure>`;
  }).join('') + '</div>';
  html += `<div class="dlg-log-title">Log</div><pre>${esc(j.log || '(ว่าง)')}</pre>`;
  document.getElementById('dlg_body').innerHTML = html;
  if (!dlg.open) dlg.showModal();
  const pre = document.querySelector('#dlg_body pre'); if (pre) pre.scrollTop = pre.scrollHeight;
}
dlg.addEventListener('close', () => watching = null);

// ---------------- Preview ตำแหน่ง (เรขาคณิต, ไม่ใช่ Monte Carlo) ----------------
const PV = { pts: null, name: null };
const FILM_MM = 400;                       // ขนาดฟิล์มดีฟอลต์ (film_xy) สำหรับวาดฉากรับ
const PV_IDS = ['pv_sx','pv_sy','pv_sz','pv_odd','pv_field'];

function pvVals() {
  const g = id => +document.getElementById(id).value;
  return { sx: g('pv_sx'), sy: g('pv_sy'), sz: g('pv_sz'),
           odd: Math.max(50, g('pv_odd')), field: Math.max(0, g('pv_field')) };
}
function pvReset() {
  const set = (id,v)=>document.getElementById(id).value=v;
  set('pv_sx',0); set('pv_sy',0); set('pv_sz',-800); set('pv_odd',400); set('pv_field',0);
  pvDraw();
}
async function pvLoadMesh(name) {
  if (!name) { PV.pts = null; pvDraw(); return; }
  if (PV.name === name && PV.pts) { pvDraw(); return; }
  try {
    const r = await api('/api/meshes/' + encodeURIComponent(name));
    if (!r.ok) { PV.pts = null; PV.name = name; pvDraw('(ยังไม่มี preview ของโมเดลนี้ — รอ worker อัปโหลด)'); return; }
    const d = await r.json();
    PV.pts = d.points || []; PV.name = name; pvDraw();
  } catch (e) { PV.pts = null; pvDraw(); }
}

const V3 = {
  sub:(a,b)=>[a[0]-b[0],a[1]-b[1],a[2]-b[2]],
  add:(a,b)=>[a[0]+b[0],a[1]+b[1],a[2]+b[2]],
  mul:(a,s)=>[a[0]*s,a[1]*s,a[2]*s],
  cross:(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]],
  norm:a=>{const n=Math.hypot(a[0],a[1],a[2])||1;return [a[0]/n,a[1]/n,a[2]/n];},
};
// projection แต่ละมุมมอง: world[x,y,z] -> screen[u,v] (v ชี้ลงบน canvas)
const PROJ = {
  plan: p => [p[0], p[2]],          // มองบน: X แนวนอน, Z แนวตั้ง (source z<0 อยู่บน)
  front:p => [p[0], -p[1]],         // มองตามลำแสง: X, Y(ขึ้น)
  side: p => [p[2], -p[1]],         // มองข้าง: Z, Y(ขึ้น)
  iso:  p => { const c=0.8660254, s=0.5;
               return [(p[0]-p[2])*c, (p[0]+p[2])*s - p[1]]; },
};

function pvScene() {
  const v = pvVals();
  const S = [v.sx, v.sy, v.sz];
  const sod = Math.hypot(v.sx, v.sy, v.sz) || 1;
  const u = [-S[0]/sod, -S[1]/sod, -S[2]/sod];       // ทิศลำแสง source->วัตถุ
  const Dc = V3.mul(u, v.odd);                       // ศูนย์กลางฉากรับ (หลังวัตถุ)
  let up = Math.abs(u[1]) > 0.99 ? [1,0,0] : [0,1,0];
  const a = V3.norm(V3.cross(u, up)), b = V3.cross(u, a);   // แกนในระนาบฉากรับ
  const hf = v.field > 0 ? v.field/2 : FILM_MM*Math.SQRT2/2;
  const cor = (r, sa, sb) => V3.add(Dc, V3.add(V3.mul(a, sa*r), V3.mul(b, sb*r)));
  const det = [cor(FILM_MM/2,1,1), cor(FILM_MM/2,1,-1), cor(FILM_MM/2,-1,-1), cor(FILM_MM/2,-1,1)];
  const fld = [cor(hf,1,1), cor(hf,1,-1), cor(hf,-1,-1), cor(hf,-1,1)];
  return { S, Dc, det, fld, sod, sid: sod + v.odd, v };
}

function pvView(cid, projKey) {
  const cv = document.getElementById(cid); if (!cv) return;
  const ctx = cv.getContext('2d'); const W = cv.width, H = cv.height;
  ctx.clearRect(0,0,W,H);
  const proj = PROJ[projKey];
  const sc = pvScene();
  const obj = PV.pts || [];
  const world = [...obj, sc.S, sc.Dc, ...sc.det, ...sc.fld];
  const P = world.map(proj);
  let mnx=1e9,mny=1e9,mxx=-1e9,mxy=-1e9;
  for (const q of P){ mnx=Math.min(mnx,q[0]);mxx=Math.max(mxx,q[0]);mny=Math.min(mny,q[1]);mxy=Math.max(mxy,q[1]); }
  const pad=18, sw=(mxx-mnx)||1, sh=(mxy-mny)||1;
  const s = Math.min((W-2*pad)/sw, (H-2*pad)/sh);
  const ox = (W - sw*s)/2 - mnx*s, oy = (H - sh*s)/2 - mny*s;
  const T = w => { const q = proj(w); return [q[0]*s+ox, q[1]*s+oy]; };
  const poly = (pts, close) => { ctx.beginPath(); pts.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1])); if(close)ctx.closePath(); };
  // ฉากรับ (น้ำเงิน)
  ctx.lineWidth=1.4; ctx.strokeStyle='#2563eb'; ctx.fillStyle='rgba(37,99,235,.07)';
  poly(sc.det.map(T), true); ctx.fill(); ctx.stroke();
  // ลำแสงจาก source ไปมุมสนาม (แดงจาง) + แกนลำแสง
  const sp = T(sc.S);
  ctx.strokeStyle='rgba(193,18,31,.45)'; ctx.lineWidth=1;
  for (const c of sc.fld){ const cp=T(c); ctx.beginPath(); ctx.moveTo(sp[0],sp[1]); ctx.lineTo(cp[0],cp[1]); ctx.stroke(); }
  ctx.strokeStyle='rgba(193,18,31,.3)'; const dp=T(sc.Dc); ctx.beginPath(); ctx.moveTo(sp[0],sp[1]); ctx.lineTo(dp[0],dp[1]); ctx.stroke();
  // กรอบสนามลำแสงที่ฉากรับ (แดงประ)
  ctx.setLineDash([4,3]); ctx.strokeStyle='rgba(193,18,31,.8)'; poly(sc.fld.map(T), true); ctx.stroke(); ctx.setLineDash([]);
  // วัตถุ (จุดเทา)
  ctx.fillStyle='rgba(60,70,85,.5)';
  for (const w of obj){ const q=T(w); ctx.fillRect(q[0]-0.6, q[1]-0.6, 1.5, 1.5); }
  // source (จุดแดง)
  ctx.fillStyle='#c1121f'; ctx.beginPath(); ctx.arc(sp[0],sp[1],4.5,0,7); ctx.fill();
  ctx.fillStyle='#fff'; ctx.beginPath(); ctx.arc(sp[0],sp[1],1.7,0,7); ctx.fill();
}

let pvMsg = '';
function pvDraw(msg) {
  if (msg !== undefined) pvMsg = msg;
  pvView('pv_plan','plan'); pvView('pv_front','front'); pvView('pv_side','side'); pvView('pv_iso','iso');
  const sc = pvScene();
  const ang = Math.atan((sc.v.field>0? sc.v.field/2 : FILM_MM*Math.SQRT2/2)/sc.sid)*180/Math.PI;
  document.getElementById('pv_info').textContent =
    `SOD ${sc.sod.toFixed(0)} มม. · SID ${sc.sid.toFixed(0)} มม. · ครึ่งมุมลำแสง ${ang.toFixed(1)}° · `
    + `ลำแสง ${sc.v.field>0? sc.v.field+' มม.' : 'เต็มฟิล์ม'}` + (pvMsg? '  — '+pvMsg : '');
}
function pvInit() {
  PV_IDS.forEach(id => document.getElementById(id).addEventListener('input', () => pvDraw('')));
  document.getElementById('pv_model').addEventListener('change', e => pvLoadMesh(e.target.value));
  pvDraw('');
}

setInterval(() => { refresh(); loadModels(); if (watching) showJob(watching); }, 5000);
(async () => { if (await ensureLogin(false)) { pvInit(); loadModels(); refresh(); } })();
</script></body></html>
"""
