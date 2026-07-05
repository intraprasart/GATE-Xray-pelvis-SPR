"""SPR Worker — ให้เครื่องนี้รับงาน simulation จาก job server แล้วส่งผลกลับ

วน loop: claim job จาก server → รัน gate_pelvis CLI → เก็บผลวิเคราะห์+ภาพ
(ไม่รวมไฟล์ raw phase-space .root) → zip อัปโหลดกลับ server

ตั้งค่าใน config.json ข้างไฟล์นี้ (ดู config.example.json)
รัน:  python worker.py
"""

from __future__ import annotations

import csv
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

# คอนโซล Windows อาจเป็น codepage cp874 ซึ่งพิมพ์ UTF-8 ไม่ได้
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))

SERVER = CFG["server_url"].rstrip("/")
KEY = CFG["worker_key"]
WORKER_ID = CFG.get("worker_id") or socket.gethostname()
REPO = Path(CFG.get("repo_dir", HERE.parent.parent))
PYTHON = CFG.get("python", sys.executable)
JOBS_DIR = Path(CFG.get("jobs_dir", REPO / "runs" / "remote_jobs"))
MODELS_DIR = Path(CFG.get("models_dir", REPO / "models"))
POLL_SECONDS = int(CFG.get("poll_seconds", 10))
MAX_PHOTONS = int(CFG.get("max_photons", 100_000_000))
MAX_JOB_SECONDS = int(CFG.get("max_job_seconds", 24 * 3600))
KEEP_LOCAL_RUNS = bool(CFG.get("keep_local_runs", True))

HEADERS = {"X-API-Key": KEY}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ไฟล์ผลลัพธ์ที่ส่งกลับ (ไม่รวม .root ซึ่งใหญ่ระดับ GB)
RESULT_EXTS = {".png", ".mhd", ".raw", ".zraw", ".csv", ".json", ".txt"}
MAX_RESULT_FILE_BYTES = 300 * 1024 * 1024

# ขอบเขตพารามิเตอร์ที่ยอมรับจาก server (กันค่าที่พังเครื่อง)
INT_PARAMS = {"photons": (1, MAX_PHOTONS), "pix": (16, 2048),
              "threads": (1, os.cpu_count() or 1)}
FLOAT_PARAMS = {"energy_keV": (1.0, 1000.0), "sod": (10.0, 5000.0),
                "odd": (0.0, 5000.0), "film_xy": (10.0, 2000.0),
                "film_thickness": (0.1, 100.0),
                "rot_x": (-360.0, 360.0), "rot_y": (-360.0, 360.0),
                "rot_z": (-360.0, 360.0),
                "primary_theta_deg": (0.01, 90.0), "primary_dE_keV": (0.01, 100.0)}


def log_local(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with (HERE / "worker.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ----------------------------------------------------------------------
# คุยกับ server
# ----------------------------------------------------------------------

def register() -> None:
    models = sorted(p.name for p in MODELS_DIR.glob("*.stl"))
    SESSION.post(f"{SERVER}/api/worker/register", json={
        "worker_id": WORKER_ID,
        "models": models,
        "machine": {"platform": platform.platform(), "cpu_count": os.cpu_count(),
                    "python": platform.python_version()},
    }, timeout=30)


def claim() -> dict | None:
    r = SESSION.post(f"{SERVER}/api/worker/claim",
                     json={"worker_id": WORKER_ID}, timeout=30)
    if r.status_code == 204:
        return None
    r.raise_for_status()
    return r.json()


def send_log(job_id: str, text: str) -> None:
    if not text:
        return
    try:
        SESSION.post(f"{SERVER}/api/worker/jobs/{job_id}/log",
                     data=text.encode("utf-8"), timeout=30)
    except requests.RequestException:
        pass  # log หายบ้างไม่เป็นไร งานหลักต้องเดินต่อ


def complete(job_id: str, status: str, metrics: dict | None,
             error: str, zip_path: Path | None) -> None:
    data = {"status": status, "metrics": json.dumps(metrics or {}), "error": error[:2000]}
    for attempt in range(1, 4):
        try:
            if zip_path is not None and zip_path.exists():
                with zip_path.open("rb") as f:
                    r = SESSION.post(f"{SERVER}/api/worker/jobs/{job_id}/complete",
                                     data=data, files={"file": (zip_path.name, f, "application/zip")},
                                     timeout=1800)
            else:
                r = SESSION.post(f"{SERVER}/api/worker/jobs/{job_id}/complete",
                                 data=data, timeout=60)
            r.raise_for_status()
            return
        except requests.RequestException as e:
            log_local(f"upload attempt {attempt} failed: {e}")
            time.sleep(15 * attempt)
    log_local(f"ERROR: ส่งผล job {job_id} ไม่สำเร็จหลัง retry — ผลยังอยู่ที่ {zip_path}")


# ----------------------------------------------------------------------
# แปลง job spec → คำสั่ง CLI (มี whitelist กันคำสั่งแปลกปลอม)
# ----------------------------------------------------------------------

def resolve_stl(name: str) -> Path:
    if not name or any(c in name for c in "/\\") or ".." in name:
        raise ValueError(f"ชื่อ STL ไม่ถูกต้อง: {name!r}")
    p = MODELS_DIR / name
    if not p.is_file():
        raise ValueError(f"ไม่พบโมเดล {name!r} ใน {MODELS_DIR}")
    return p


def cli_args(params: dict, stl: Path, out_dir: Path) -> list[str]:
    args = [PYTHON, "-u", "-m", "gate_pelvis.cli", "run",
            "--stl", str(stl), "--out", str(out_dir), "--clean"]
    for key, (lo, hi) in INT_PARAMS.items():
        if key in params:
            args += [f"--{key}", str(max(lo, min(hi, int(params[key]))))]
    for key, (lo, hi) in FLOAT_PARAMS.items():
        if key in params:
            args += [f"--{key}", str(max(lo, min(hi, float(params[key]))))]
    if params.get("no_phsp"):
        args.append("--no_phsp")
    return args


def run_cmd(args: list[str], job_id: str, label: str) -> None:
    """รัน subprocess, stream stdout ไป server เป็นช่วง ๆ"""
    send_log(job_id, f"\n===== {label} =====\n$ {' '.join(args)}\n")
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.Popen(args, cwd=str(REPO), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    killer = threading.Timer(MAX_JOB_SECONDS, proc.kill)
    killer.start()
    buf, last_flush = [], time.monotonic()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            buf.append(line)
            if len(buf) >= 50 or time.monotonic() - last_flush > 3:
                send_log(job_id, "".join(buf))
                buf, last_flush = [], time.monotonic()
        code = proc.wait()
    finally:
        killer.cancel()
    send_log(job_id, "".join(buf))
    if code != 0:
        raise RuntimeError(f"{label} ล้มเหลว (exit code {code})")


# ----------------------------------------------------------------------
# เก็บผล + zip
# ----------------------------------------------------------------------

def collect_zip(job_dir: Path, zip_path: Path, manifest: dict) -> int:
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(job_dir.rglob("*")):
            if not p.is_file() or p == zip_path:
                continue
            if p.suffix.lower() not in RESULT_EXTS:
                continue
            if p.stat().st_size > MAX_RESULT_FILE_BYTES:
                continue
            zf.write(p, p.relative_to(job_dir).as_posix())
            n += 1
        zf.writestr("job_manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    return n


def read_summary_csv(path: Path) -> dict:
    out = {}
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                k, v = row.get("metric"), row.get("value")
                if k is None:
                    continue
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    out[k] = v
    return out


# ----------------------------------------------------------------------
# จัดการ job แต่ละประเภท
# ----------------------------------------------------------------------

def handle_job(job: dict) -> None:
    job_id, jtype, params = job["id"], job["type"], job.get("params", {})
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    metrics: dict = {}
    send_log(job_id, f"worker {WORKER_ID} รับงาน {job_id} ({jtype})\n"
                     f"params: {json.dumps(params, ensure_ascii=False)}\n")

    if jtype == "run":
        stl = resolve_stl(params.get("stl", ""))
        run_cmd(cli_args(params, stl, job_dir / "run"), job_id, f"run {stl.name}")

    elif jtype == "run_pair":
        c_stl = resolve_stl(params.get("control_stl", ""))
        f_stl = resolve_stl(params.get("fracture_stl", ""))
        run_cmd(cli_args(params, c_stl, job_dir / "control"), job_id, f"control: {c_stl.name}")
        run_cmd(cli_args(params, f_stl, job_dir / "fracture"), job_id, f"fracture: {f_stl.name}")
        run_cmd([PYTHON, "-u", "-m", "gate_pelvis.cli", "compare",
                 "--control", str(job_dir / "control"),
                 "--fracture", str(job_dir / "fracture"),
                 "--out", str(job_dir / "roi_analysis")],
                job_id, "compare control vs fracture")
        metrics.update(read_summary_csv(job_dir / "roi_analysis" / "summary.csv"))

    elif jtype == "compare":
        c_dir = JOBS_DIR / str(params.get("control_job", "")) / str(params.get("control_sub", "run"))
        f_dir = JOBS_DIR / str(params.get("fracture_job", "")) / str(params.get("fracture_sub", "run"))
        for d in (c_dir, f_dir):
            if not (d / "object" / "SPR.mhd").exists():
                raise RuntimeError(f"ไม่พบผลรันเดิมที่ {d} บนเครื่อง worker นี้")
        run_cmd([PYTHON, "-u", "-m", "gate_pelvis.cli", "compare",
                 "--control", str(c_dir), "--fracture", str(f_dir),
                 "--out", str(job_dir / "roi_analysis")],
                job_id, "compare (จากผลรันเดิม)")
        metrics.update(read_summary_csv(job_dir / "roi_analysis" / "summary.csv"))
    else:
        raise RuntimeError(f"ไม่รู้จักประเภทงาน {jtype!r}")

    duration = round(time.monotonic() - t0, 1)
    metrics["duration_seconds"] = duration
    manifest = {"job_id": job_id, "type": jtype, "params": params,
                "worker": WORKER_ID, "duration_seconds": duration,
                "finished_at": datetime.now(timezone.utc).isoformat()}
    zip_path = job_dir / "results.zip"
    n = collect_zip(job_dir, zip_path, manifest)
    send_log(job_id, f"\nเสร็จใน {duration}s — ส่งผล {n} ไฟล์ "
                     f"({zip_path.stat().st_size / 1e6:.1f} MB) กลับ server\n")
    complete(job_id, "done", metrics, "", zip_path)
    if not KEEP_LOCAL_RUNS:
        shutil.rmtree(job_dir, ignore_errors=True)


def main() -> None:
    log_local(f"SPR worker '{WORKER_ID}' เริ่มทำงาน → {SERVER}")
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    last_register = 0.0
    while True:
        try:
            if time.monotonic() - last_register > 300:
                register()
                last_register = time.monotonic()
            job = claim()
        except requests.RequestException as e:
            log_local(f"ติดต่อ server ไม่ได้: {e} — รอ 30s")
            time.sleep(30)
            continue
        if job is None:
            time.sleep(POLL_SECONDS)
            continue
        log_local(f"ได้งาน {job['id']} ({job['type']})")
        try:
            handle_job(job)
            log_local(f"งาน {job['id']} เสร็จสมบูรณ์")
        except Exception as e:  # noqa: BLE001 — worker ต้องไม่ตายเพราะงานเดียว
            log_local(f"งาน {job['id']} ล้มเหลว: {e}")
            send_log(job["id"], f"\nERROR: {e}\n")
            try:
                complete(job["id"], "failed", None, str(e), None)
            except Exception as e2:  # noqa: BLE001
                log_local(f"รายงานความล้มเหลวไม่สำเร็จ: {e2}")


if __name__ == "__main__":
    main()
