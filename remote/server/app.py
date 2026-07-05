"""SPR Job Server — สั่งงาน simulation จากภายนอก + เก็บผลลัพธ์

FastAPI app เดียวจบ: job queue (SQLite) + รับอัปโหลดผล + dashboard
รันบน VPS:  uvicorn app:app --host 0.0.0.0 --port 8642

Environment variables (จำเป็น):
    SPR_ADMIN_KEY   คีย์สำหรับคน (ส่ง job / ดูผล)
    SPR_WORKER_KEY  คีย์สำหรับเครื่อง worker (รับ job / ส่งผล)
    SPR_DATA_DIR    โฟลเดอร์เก็บ DB + ผลลัพธ์ (default: ./data)
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

DATA_DIR = Path(os.environ.get("SPR_DATA_DIR", "./data")).resolve()
ADMIN_KEY = os.environ.get("SPR_ADMIN_KEY", "")
WORKER_KEY = os.environ.get("SPR_WORKER_KEY", "")
MAX_LOG_CHARS = 800_000          # เก็บ log ท้ายสุดไม่เกินนี้ต่อ job
MAX_UPLOAD_BYTES = 2_000_000_000

DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "results").mkdir(exist_ok=True)

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
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job_row(row: sqlite3.Row, with_log: bool = False) -> dict:
    d = {k: row[k] for k in row.keys() if k != "log"}
    d["params"] = json.loads(d["params"])
    d["metrics"] = json.loads(d["metrics"]) if d["metrics"] else None
    if with_log:
        d["log"] = row["log"]
    return d


# ----------------------------------------------------------------------
# Auth — รับคีย์ทาง header X-API-Key หรือ query ?key=
# ----------------------------------------------------------------------

def _get_key(request: Request, key: str | None) -> str:
    return request.headers.get("X-API-Key") or key or ""


def require_admin(request: Request, key: str | None = Query(default=None)):
    if _get_key(request, key) != ADMIN_KEY:
        raise HTTPException(401, "invalid admin key")


def require_worker(request: Request, key: str | None = Query(default=None)):
    if _get_key(request, key) != WORKER_KEY:
        raise HTTPException(401, "invalid worker key")


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
        _db.execute(
            "INSERT INTO jobs (id, created_at, type, params, status) VALUES (?,?,?,?,?)",
            (job_id, _now(), jtype, json.dumps(params), "pending"))
        _db.commit()
    return {"id": job_id, "status": "pending"}


@app.get("/api/jobs", dependencies=[Depends(require_admin)])
def list_jobs(limit: int = 100):
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


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_admin)])
def delete_job(job_id: str):
    with _lock:
        cur = _db.execute(
            "DELETE FROM jobs WHERE id=? AND status != 'running'", (job_id,))
        _db.commit()
    if cur.rowcount == 0:
        raise HTTPException(409, "ลบไม่ได้ (ไม่พบ หรือกำลังรันอยู่)")
    zip_path = DATA_DIR / "results" / f"{job_id}.zip"
    dir_path = DATA_DIR / "results" / job_id
    if zip_path.exists():
        zip_path.unlink()
    if dir_path.exists():
        shutil.rmtree(dir_path, ignore_errors=True)
    return {"id": job_id, "deleted": True}


@app.get("/api/jobs/{job_id}/results.zip", dependencies=[Depends(require_admin)])
def download_results(job_id: str):
    zip_path = DATA_DIR / "results" / f"{job_id}.zip"
    if not zip_path.exists():
        raise HTTPException(404, "ยังไม่มีผลลัพธ์ของ job นี้")
    return FileResponse(zip_path, filename=f"{job_id}.zip")


@app.get("/api/jobs/{job_id}/files", dependencies=[Depends(require_admin)])
def list_result_files(job_id: str):
    base = DATA_DIR / "results" / job_id
    if not base.exists():
        return []
    return sorted(str(p.relative_to(base)).replace("\\", "/")
                  for p in base.rglob("*") if p.is_file())


@app.get("/api/jobs/{job_id}/file", dependencies=[Depends(require_admin)])
def get_result_file(job_id: str, path: str):
    base = (DATA_DIR / "results" / job_id).resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(target)


@app.get("/api/models", dependencies=[Depends(require_admin)])
def list_models():
    """รายชื่อ STL + สถานะ worker ที่เคยลงทะเบียน"""
    with _lock:
        rows = _db.execute("SELECT * FROM workers").fetchall()
    out = []
    for r in rows:
        info = json.loads(r["info"] or "{}")
        out.append({"worker_id": r["worker_id"], "last_seen": r["last_seen"],
                    "models": info.get("models", []), "machine": info.get("machine", {})})
    return out


# ----------------------------------------------------------------------
# Worker API — เครื่อง simulation เรียกเข้ามา
# ----------------------------------------------------------------------

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
        row = _db.execute(
            "SELECT * FROM jobs WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            _db.commit()
            return Response(status_code=204)
        _db.execute(
            "UPDATE jobs SET status='running', claimed_at=?, worker_id=? WHERE id=?",
            (_now(), worker_id, row["id"]))
        _db.commit()
    return {"id": row["id"], "type": row["type"], "params": json.loads(row["params"])}


@app.post("/api/worker/jobs/{job_id}/log", dependencies=[Depends(require_worker)])
async def append_log(job_id: str, request: Request):
    text = (await request.body()).decode("utf-8", errors="replace")
    with _lock:
        row = _db.execute("SELECT log FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "job not found")
        new_log = (row["log"] + text)[-MAX_LOG_CHARS:]
        _db.execute("UPDATE jobs SET log=? WHERE id=?", (new_log, job_id))
        _db.commit()
    return {"ok": True}


@app.post("/api/worker/jobs/{job_id}/complete", dependencies=[Depends(require_worker)])
async def complete_job(job_id: str,
                       status: str = Form(...),
                       metrics: str = Form(default=""),
                       error: str = Form(default=""),
                       file: UploadFile | None = None):
    if status not in ("done", "failed"):
        raise HTTPException(400, "status ต้องเป็น done หรือ failed")
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
        # แตก zip ไว้ให้ dashboard เปิดดูภาพได้ (กัน zip-slip ด้วยการเช็ค path)
        extract_dir = DATA_DIR / "results" / job_id
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)
        extract_dir.mkdir(parents=True)
        with zipfile.ZipFile(zip_path) as zf:
            for m in zf.infolist():
                dest = (extract_dir / m.filename).resolve()
                if not str(dest).startswith(str(extract_dir.resolve())):
                    continue
                if m.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(m) as src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out)

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
<title>SPR Job Server</title>
<style>
  :root { font-family: system-ui, 'Segoe UI', sans-serif; }
  body { margin: 0; background: #f5f6f8; color: #1c2330; }
  header { background: #14213d; color: #fff; padding: 12px 20px; display: flex;
           justify-content: space-between; align-items: center; }
  header h1 { font-size: 18px; margin: 0; }
  main { max-width: 1100px; margin: 20px auto; padding: 0 16px; }
  .card { background: #fff; border-radius: 10px; padding: 16px 20px; margin-bottom: 18px;
          box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  h2 { font-size: 15px; margin: 0 0 12px; }
  label { font-size: 13px; display: block; margin: 8px 0 2px; color: #444; }
  input, select { padding: 6px 8px; border: 1px solid #ccc; border-radius: 6px; width: 100%;
                  box-sizing: border-box; font-size: 14px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
  button { background: #14213d; color: #fff; border: 0; border-radius: 6px; padding: 8px 16px;
           cursor: pointer; font-size: 14px; }
  button.small { padding: 3px 10px; font-size: 12px; }
  button.ghost { background: #e5e7eb; color: #1c2330; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid #eee; }
  .st { padding: 2px 9px; border-radius: 10px; font-size: 12px; font-weight: 600; }
  .st.pending { background:#fef3c7; color:#92400e; } .st.running { background:#dbeafe; color:#1d4ed8; }
  .st.done { background:#d1fae5; color:#065f46; } .st.failed { background:#fee2e2; color:#991b1b; }
  .st.cancelled { background:#e5e7eb; color:#4b5563; }
  pre { background: #0f172a; color: #cbd5e1; padding: 12px; border-radius: 8px; font-size: 12px;
        max-height: 320px; overflow: auto; white-space: pre-wrap; }
  .imgs { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }
  .imgs figure { margin: 0; } .imgs img { width: 100%; border-radius: 6px; border: 1px solid #ddd; }
  .imgs figcaption { font-size: 11px; color: #555; word-break: break-all; }
  #workerline { font-size: 12px; color: #cbd5e1; }
  dialog { border: 0; border-radius: 12px; width: min(950px, 94vw); max-height: 90vh; }
  .metrics td:first-child { color: #555; }
</style></head><body>
<header><h1>🩻 SPR Job Server — GATE X-ray pelvis</h1><div id="workerline">…</div></header>
<main>
  <div class="card"><h2>ส่งงานใหม่ (run_pair: control + fracture + เปรียบเทียบ)</h2>
    <div class="grid">
      <div><label>ประเภทงาน</label>
        <select id="jtype"><option value="run_pair">run_pair (control vs fracture)</option>
        <option value="run">run (โมเดลเดียว)</option></select></div>
      <div><label>Control STL</label><select id="control_stl"></select></div>
      <div id="fr_wrap"><label>Fracture STL</label><select id="fracture_stl"></select></div>
      <div><label>Photons</label><input id="photons" type="number" value="1000000"></div>
      <div><label>Energy (keV)</label><input id="energy" type="number" value="80"></div>
      <div><label>Pixels</label><input id="pix" type="number" value="512"></div>
      <div><label>Threads</label><input id="threads" type="number" value="1"></div>
    </div>
    <p><button onclick="submitJob()">🚀 ส่งงาน</button> <span id="submitmsg"></span></p>
  </div>
  <div class="card"><h2>รายการงาน <button class="small ghost" onclick="refresh()">รีเฟรช</button></h2>
    <table><thead><tr><th>ID</th><th>ประเภท</th><th>สถานะ</th><th>สร้างเมื่อ</th><th>เครื่อง</th><th></th></tr></thead>
    <tbody id="jobs"></tbody></table>
  </div>
</main>
<dialog id="dlg"><div style="padding:18px">
  <h2 id="dlg_title" style="font-size:15px"></h2>
  <div id="dlg_body"></div>
  <p style="text-align:right"><button class="ghost" onclick="dlg.close()">ปิด</button></p>
</div></dialog>
<script>
const KEY = localStorage.getItem('spr_key') || prompt('ใส่ Admin API key:');
localStorage.setItem('spr_key', KEY);
const H = { 'X-API-Key': KEY };
const dlg = document.getElementById('dlg');
let watching = null;

document.getElementById('jtype').onchange = e =>
  document.getElementById('fr_wrap').style.display = e.target.value === 'run_pair' ? '' : 'none';

async function loadModels() {
  try {
    const ws = await (await fetch('/api/models', { headers: H })).json();
    const models = [...new Set(ws.flatMap(w => w.models))];
    for (const id of ['control_stl', 'fracture_stl']) {
      const sel = document.getElementById(id);
      sel.innerHTML = models.map(m => `<option>${m}</option>`).join('') ||
                      '<option value="">(ยังไม่มี worker ลงทะเบียน)</option>';
    }
    document.getElementById('fracture_stl').value =
      models.find(m => m !== document.getElementById('control_stl').value) || models[0] || '';
    const online = ws.filter(w => Date.now() - Date.parse(w.last_seen) < 90000);
    document.getElementById('workerline').textContent = online.length
      ? `🟢 worker ออนไลน์: ${online.map(w => w.worker_id).join(', ')}`
      : '🔴 ไม่มี worker ออนไลน์';
  } catch (e) { document.getElementById('workerline').textContent = '⚠ โหลดข้อมูล worker ไม่ได้'; }
}

async function submitJob() {
  const t = document.getElementById('jtype').value;
  const p = {
    photons: +document.getElementById('photons').value,
    energy_keV: +document.getElementById('energy').value,
    pix: +document.getElementById('pix').value,
    threads: +document.getElementById('threads').value,
  };
  if (t === 'run_pair') {
    p.control_stl = document.getElementById('control_stl').value;
    p.fracture_stl = document.getElementById('fracture_stl').value;
  } else { p.stl = document.getElementById('control_stl').value; }
  const r = await fetch('/api/jobs', { method: 'POST', headers: { ...H, 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: t, params: p }) });
  document.getElementById('submitmsg').textContent = r.ok ? '✅ ส่งแล้ว' : '❌ ' + (await r.text());
  refresh();
}

async function refresh() {
  const jobs = await (await fetch('/api/jobs', { headers: H })).json();
  document.getElementById('jobs').innerHTML = jobs.map(j => `<tr>
    <td style="font-family:monospace">${j.id}</td><td>${j.type}</td>
    <td><span class="st ${j.status}">${j.status}</span></td>
    <td>${j.created_at.replace('T', ' ').replace('Z', '')}</td><td>${j.worker_id || '-'}</td>
    <td><button class="small" onclick="showJob('${j.id}')">ดู</button>
      ${j.status === 'done' ? `<a href="/api/jobs/${j.id}/results.zip?key=${KEY}"><button class="small ghost">zip</button></a>` : ''}
      ${j.status === 'pending' ? `<button class="small ghost" onclick="cancelJob('${j.id}')">ยกเลิก</button>` : ''}
      ${j.status !== 'running' ? `<button class="small ghost" onclick="delJob('${j.id}')">ลบ</button>` : ''}
    </td></tr>`).join('');
}

async function cancelJob(id) { await fetch(`/api/jobs/${id}/cancel`, { method: 'POST', headers: H }); refresh(); }
async function delJob(id) { if (confirm('ลบ job ' + id + '?')) { await fetch(`/api/jobs/${id}`, { method: 'DELETE', headers: H }); refresh(); } }

async function showJob(id) {
  watching = id;
  const j = await (await fetch(`/api/jobs/${id}`, { headers: H })).json();
  const files = await (await fetch(`/api/jobs/${id}/files`, { headers: H })).json();
  document.getElementById('dlg_title').textContent = `${j.id} — ${j.type} [${j.status}]`;
  let html = `<table class="metrics"><tr><td>พารามิเตอร์</td><td><code>${JSON.stringify(j.params)}</code></td></tr>`;
  if (j.metrics) for (const [k, v] of Object.entries(j.metrics))
    html += `<tr><td>${k}</td><td>${typeof v === 'number' ? v.toPrecision(6) : v}</td></tr>`;
  if (j.error) html += `<tr><td>error</td><td style="color:#b91c1c">${j.error}</td></tr>`;
  html += '</table>';
  const pngs = files.filter(f => f.endsWith('.png'));
  if (pngs.length) html += '<div class="imgs">' + pngs.map(f =>
    `<figure><a href="/api/jobs/${id}/file?path=${encodeURIComponent(f)}&key=${KEY}" target="_blank">
     <img loading="lazy" src="/api/jobs/${id}/file?path=${encodeURIComponent(f)}&key=${KEY}"></a>
     <figcaption>${f}</figcaption></figure>`).join('') + '</div>';
  html += `<h2 style="margin-top:14px">Log</h2><pre id="dlg_log">${(j.log || '(ว่าง)')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')}</pre>`;
  document.getElementById('dlg_body').innerHTML = html;
  if (!dlg.open) dlg.showModal();
  const pre = document.getElementById('dlg_log'); pre.scrollTop = pre.scrollHeight;
}
dlg.addEventListener('close', () => watching = null);

setInterval(() => { refresh(); loadModels(); if (watching) showJob(watching); }, 5000);
loadModels(); refresh();
</script></body></html>
"""
