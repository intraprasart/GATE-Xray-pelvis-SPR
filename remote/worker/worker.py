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
# utf-8-sig: เผื่อไฟล์ถูกเซฟจาก Notepad เป็น UTF-8 with BOM
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))

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
SERVER_MAX_UPLOAD_BYTES = int(CFG.get("max_upload_bytes", 2_000_000_000))

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


def _kill_tree(proc: subprocess.Popen) -> None:
    """ฆ่าทั้ง process tree — สำคัญเพราะ compute จริงอยู่ในโปรเซสหลาน (_worker)"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()


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


def _post_complete(job_id: str, data: dict, zip_path: Path | None):
    if zip_path is not None:
        with zip_path.open("rb") as f:
            return SESSION.post(f"{SERVER}/api/worker/jobs/{job_id}/complete",
                                data=data, files={"file": (zip_path.name, f, "application/zip")},
                                timeout=1800)
    return SESSION.post(f"{SERVER}/api/worker/jobs/{job_id}/complete",
                        data=data, timeout=60)


def complete(job_id: str, status: str, metrics: dict | None,
             error: str, zip_path: Path | None) -> bool:
    """รายงานผลกลับ server. คืน True เมื่ออัปโหลดไฟล์ผลสำเร็จ.

    ยุทธศาสตร์: ถ้ามีไฟล์แต่ใหญ่เกิน/อัปไม่ขึ้น → ไม่ retry payload เดิมซ้ำ ๆ
    แต่ fallback ไปรายงานสถานะแบบไม่มีไฟล์ เพื่อให้ job ถึงสถานะสุดท้ายเสมอ
    (ไม่ค้าง running) พร้อมโน้ตว่าไฟล์ผลยังอยู่ที่เครื่อง worker.
    """
    base = {"status": status, "metrics": json.dumps(metrics or {}), "error": error[:2000]}
    have_file = zip_path is not None and zip_path.exists()
    too_big = have_file and zip_path.stat().st_size > SERVER_MAX_UPLOAD_BYTES

    if have_file and not too_big:
        for attempt in range(1, 4):
            try:
                r = _post_complete(job_id, base, zip_path)
                if r.status_code < 400:
                    return True
                if r.status_code == 429 or r.status_code >= 500:
                    log_local(f"complete {r.status_code} (attempt {attempt}) — retry")
                    time.sleep(10 * attempt)
                    continue
                log_local(f"server ปฏิเสธการอัปโหลด ({r.status_code}) — fallback ไม่มีไฟล์")
                break  # 4xx อื่น ๆ: อย่า retry payload เดิม
            except requests.RequestException as e:
                log_local(f"upload attempt {attempt} failed: {e}")
                time.sleep(15 * attempt)

    # fallback: รายงานสถานะแบบไม่มีไฟล์ ให้ job ถึงสถานะสุดท้ายเสมอ
    note = error
    if have_file:
        reason = "ไฟล์ใหญ่เกินขีดจำกัด server" if too_big else "อัปโหลดผลไม่สำเร็จ"
        note = (f"{error} | {reason}; ผลยังอยู่ที่ {zip_path}").strip(" |")
    fb = {"status": status, "metrics": json.dumps(metrics or {}), "error": note[:2000]}
    for attempt in range(1, 4):
        try:
            r = _post_complete(job_id, fb, None)
            if r.status_code < 400:
                if have_file:
                    log_local(f"รายงานสถานะ {status} แล้ว (ไม่มีไฟล์) — ผลอยู่ที่ {zip_path}")
                return False
        except requests.RequestException as e:
            log_local(f"complete(no-file) attempt {attempt} failed: {e}")
        time.sleep(10 * attempt)
    log_local(f"ERROR: รายงานผล job {job_id} ไม่สำเร็จเลย — ผลอยู่ที่ {zip_path}")
    return False


# ----------------------------------------------------------------------
# แปลง job spec → คำสั่ง CLI (มี whitelist กันคำสั่งแปลกปลอม)
# ----------------------------------------------------------------------

def _safe_segment(name: str, what: str) -> str:
    """อนุญาตเฉพาะชื่อชั้นเดียว ไม่มี / \\ หรือ .. (กัน path traversal)"""
    name = str(name)
    if not name or any(c in name for c in "/\\") or ".." in name:
        raise ValueError(f"{what} ไม่ถูกต้อง: {name!r}")
    return name


def resolve_stl(name: str) -> Path:
    p = MODELS_DIR / _safe_segment(name, "ชื่อ STL")
    if not p.is_file():
        raise ValueError(f"ไม่พบโมเดล {name!r} ใน {MODELS_DIR}")
    return p


def resolve_job_subdir(job_name: str, sub_name: str) -> Path:
    """โฟลเดอร์ผลรันเดิมบนเครื่องนี้ — ต้องอยู่ใน JOBS_DIR เท่านั้น"""
    job = _safe_segment(job_name, "control_job/fracture_job")
    sub = _safe_segment(sub_name or "run", "control_sub/fracture_sub")
    d = (JOBS_DIR / job / sub).resolve()
    if not d.is_relative_to(JOBS_DIR.resolve()):
        raise ValueError(f"path ออกนอก JOBS_DIR: {d}")
    return d


def cli_args(params: dict, stl: Path, out_dir: Path, allow_no_phsp: bool = True) -> list[str]:
    args = [PYTHON, "-u", "-m", "gate_pelvis.cli", "run",
            "--stl", str(stl), "--out", str(out_dir), "--clean"]
    for key, (lo, hi) in INT_PARAMS.items():
        if key in params:
            args += [f"--{key}", str(max(lo, min(hi, int(params[key]))))]
    for key, (lo, hi) in FLOAT_PARAMS.items():
        if key in params:
            args += [f"--{key}", str(max(lo, min(hi, float(params[key]))))]
    # โหมดการรัน (ใช้ CPU/RAM เต็มเครื่อง): single | balanced | max
    # หรือระบุจำนวนโปรเซสตรง ๆ ด้วย n_procs (pipeline จะ cap ตาม RAM ให้อีกที)
    if params.get("mode") in ("single", "balanced", "max"):
        args += ["--mode", params["mode"]]
    if "n_procs" in params:
        try:
            np_ = int(params["n_procs"])
        except (TypeError, ValueError):
            np_ = 1
        args += ["--n_procs", str(max(1, min(np_, os.cpu_count() or 1)))]
    # no_phsp ตัด phase-space ทิ้ง → ไม่มี SPR.mhd; ใช้ได้เฉพาะ run เดี่ยวเท่านั้น
    # (run_pair ต้องมี SPR.mhd ไปทำ compare)
    if allow_no_phsp and params.get("no_phsp"):
        args.append("--no_phsp")
    return args


def run_cmd(args: list[str], job_id: str, label: str, deadline: float) -> None:
    """รัน subprocess, stream stdout ไป server. deadline = time.monotonic() ที่ต้องจบก่อน"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{label}: เกินเวลารวมของงานก่อนเริ่ม")
    send_log(job_id, f"\n===== {label} =====\n$ {' '.join(args)}\n")
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    kwargs = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True     # ให้ฆ่าทั้งกลุ่มได้บน POSIX
    proc = subprocess.Popen(args, cwd=str(REPO), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)
    timed_out = {"v": False}

    def _on_timeout():
        timed_out["v"] = True
        _kill_tree(proc)

    killer = threading.Timer(remaining, _on_timeout)
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
    if timed_out["v"]:
        raise TimeoutError(f"{label}: เกินเวลา {MAX_JOB_SECONDS}s — ถูกยกเลิกและฆ่าโปรเซสทั้งกลุ่ม")
    if code != 0:
        raise RuntimeError(f"{label} ล้มเหลว (exit code {code})")


# ----------------------------------------------------------------------
# เก็บผล + zip
# ----------------------------------------------------------------------

def collect_zip(job_dir: Path, zip_path: Path, manifest: dict) -> tuple[int, int]:
    """zip เฉพาะไฟล์ผลวิเคราะห์/ภาพ. คืน (จำนวนไฟล์, ขนาด zip). หยุดถ้ารวมใหญ่เกิน server."""
    n, skipped_big = 0, 0
    budget = SERVER_MAX_UPLOAD_BYTES
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(job_dir.rglob("*")):
            if not p.is_file() or p == zip_path:
                continue
            if p.suffix.lower() not in RESULT_EXTS:
                continue
            if p.stat().st_size > MAX_RESULT_FILE_BYTES:
                skipped_big += 1
                continue
            zf.write(p, p.relative_to(job_dir).as_posix())
            n += 1
        manifest = dict(manifest, files_included=n, files_skipped_large=skipped_big)
        zf.writestr("job_manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    return n, zip_path.stat().st_size


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
    job_dir = JOBS_DIR / _safe_segment(job_id, "job id")
    job_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    deadline = t0 + MAX_JOB_SECONDS      # เพดานเวลารวมของทั้ง job (ไม่ใช่ต่อ subprocess)
    metrics: dict = {}
    send_log(job_id, f"worker {WORKER_ID} รับงาน {job_id} ({jtype})\n"
                     f"params: {json.dumps(params, ensure_ascii=False)}\n")

    if jtype == "run":
        stl = resolve_stl(params.get("stl", ""))
        run_cmd(cli_args(params, stl, job_dir / "run"), job_id, f"run {stl.name}", deadline)

    elif jtype == "run_pair":
        if params.get("no_phsp"):
            send_log(job_id, "หมายเหตุ: run_pair ต้องใช้ phase-space (SPR) — ไม่สน no_phsp\n")
        c_stl = resolve_stl(params.get("control_stl", ""))
        f_stl = resolve_stl(params.get("fracture_stl", ""))
        run_cmd(cli_args(params, c_stl, job_dir / "control", allow_no_phsp=False),
                job_id, f"control: {c_stl.name}", deadline)
        run_cmd(cli_args(params, f_stl, job_dir / "fracture", allow_no_phsp=False),
                job_id, f"fracture: {f_stl.name}", deadline)
        run_cmd([PYTHON, "-u", "-m", "gate_pelvis.cli", "compare",
                 "--control", str(job_dir / "control"),
                 "--fracture", str(job_dir / "fracture"),
                 "--out", str(job_dir / "roi_analysis")],
                job_id, "compare control vs fracture", deadline)
        metrics.update(read_summary_csv(job_dir / "roi_analysis" / "summary.csv"))

    elif jtype == "compare":
        c_dir = resolve_job_subdir(params.get("control_job", ""), params.get("control_sub", "run"))
        f_dir = resolve_job_subdir(params.get("fracture_job", ""), params.get("fracture_sub", "run"))
        for d in (c_dir, f_dir):
            if not (d / "object" / "SPR.mhd").exists():
                raise RuntimeError(f"ไม่พบผลรันเดิมที่ {d} บนเครื่อง worker นี้")
        run_cmd([PYTHON, "-u", "-m", "gate_pelvis.cli", "compare",
                 "--control", str(c_dir), "--fracture", str(f_dir),
                 "--out", str(job_dir / "roi_analysis")],
                job_id, "compare (จากผลรันเดิม)", deadline)
        metrics.update(read_summary_csv(job_dir / "roi_analysis" / "summary.csv"))
    else:
        raise RuntimeError(f"ไม่รู้จักประเภทงาน {jtype!r}")

    duration = round(time.monotonic() - t0, 1)
    metrics["duration_seconds"] = duration
    manifest = {"job_id": job_id, "type": jtype, "params": params,
                "worker": WORKER_ID, "duration_seconds": duration,
                "finished_at": datetime.now(timezone.utc).isoformat()}
    zip_path = job_dir / "results.zip"
    n, zsize = collect_zip(job_dir, zip_path, manifest)
    send_log(job_id, f"\nเสร็จใน {duration}s — ส่งผล {n} ไฟล์ ({zsize / 1e6:.1f} MB) กลับ server\n")
    uploaded = complete(job_id, "done", metrics, "", zip_path)
    if not KEEP_LOCAL_RUNS and uploaded:
        shutil.rmtree(job_dir, ignore_errors=True)


def main() -> None:
    log_local(f"SPR worker '{WORKER_ID}' เริ่มทำงาน -> {SERVER}")
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
