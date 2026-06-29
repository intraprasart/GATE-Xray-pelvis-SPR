"""GATE X-ray pelvis — Streamlit UI.

Run with:   streamlit run app.py
(from the repository root, with the gate_pelvis package alongside this file.)

Three things made easy:
  1. Configure & launch a Monte Carlo radiograph run (sliders / dropdowns).
  2. Browse the resulting images (film-like, SPR, attenuation ...).
  3. Compare two runs (control vs fracture) with ROI / SPR / entropy metrics.
"""

from __future__ import annotations

import importlib
import time
from pathlib import Path

import streamlit as st

from gate_pelvis import SimConfig, run_simulation, compare_runs, ROI

ROOT = Path(__file__).resolve().parent
MODELS_DIR = ROOT / "models"
RUNS_DIR = ROOT / "runs"
RUNS_DIR.mkdir(exist_ok=True)

st.set_page_config(page_title="GATE X-ray pelvis", page_icon="🦴", layout="wide")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def list_models() -> list[Path]:
    return sorted(MODELS_DIR.glob("*.stl")) if MODELS_DIR.exists() else []


def list_runs() -> list[Path]:
    """Directories that contain a finished run (object/SPR.mhd or object/I.mhd)."""
    found = []
    for base in (RUNS_DIR, ROOT):
        for p in base.glob("**/object/I.mhd"):
            found.append(p.parent.parent)
    return sorted(set(found))


def dep_status() -> dict[str, bool]:
    mods = ["opengate", "SimpleITK", "uproot", "scipy", "skimage", "trimesh", "matplotlib"]
    out = {}
    for m in mods:
        try:
            importlib.import_module(m)
            out[m] = True
        except Exception:
            out[m] = False
    return out


# ----------------------------------------------------------------------
# Sidebar — global info
# ----------------------------------------------------------------------

st.sidebar.title("🦴 GATE X-ray pelvis")
st.sidebar.caption("Monte Carlo (OpenGATE 10) · SPR scatter signature")

with st.sidebar.expander("สถานะ dependencies", expanded=False):
    for mod, ok in dep_status().items():
        st.write(("✅ " if ok else "❌ ") + mod)
    st.caption("opengate จำเป็นเฉพาะตอน 'รันจำลอง'; scipy/skimage จำเป็นตอน 'เปรียบเทียบ'")

tab_run, tab_compare = st.tabs(["▶️ รันจำลอง", "📊 เปรียบเทียบ control vs fracture"])


# ----------------------------------------------------------------------
# Tab 1 — Run a simulation
# ----------------------------------------------------------------------

with tab_run:
    st.subheader("ตั้งค่าและรันการจำลอง")

    models = list_models()
    if not models:
        st.warning(f"ไม่พบไฟล์ .stl ใน {MODELS_DIR} — วางไฟล์โมเดลไว้ในโฟลเดอร์ models/")

    col_l, col_r = st.columns(2)
    with col_l:
        model_names = [m.name for m in models]
        sel = st.selectbox("โมเดล 3D (STL)", model_names) if model_names else None
        run_name = st.text_input("ชื่อผลลัพธ์ (โฟลเดอร์ใน runs/)", value="run_control")
        photons = st.select_slider(
            "จำนวนโฟตอน", options=[100_000, 500_000, 1_000_000, 5_000_000,
                                   10_000_000, 40_000_000],
            value=1_000_000)
        threads = st.slider("Threads", 1, 16, 1,
                            help="เพิ่มเพื่อรันเร็วขึ้น (โค้ดเดิมใช้ 1)")
        energy = st.slider("พลังงาน (keV)", 20, 150, 80)
        material = st.text_input("วัสดุวัตถุ (Geant4)", value="G4_BONE_COMPACT_ICRU")

    with col_r:
        pix = st.select_slider("ความละเอียด detector (px)",
                               options=[128, 256, 512, 1024], value=512)
        sod = st.number_input("SOD source→object (mm)", value=800.0, step=50.0)
        odd = st.number_input("ODD object→detector (mm)", value=400.0, step=50.0)
        film_xy = st.number_input("ขนาด detector (mm)", value=400.0, step=50.0)
        sep = st.checkbox("แยก primary / scatter + สร้าง SPR map", value=True)
        c1, c2 = st.columns(2)
        theta = c1.number_input("primary θ (deg)", value=0.5, step=0.1, disabled=not sep)
        dE = c2.number_input("primary |dE| (keV)", value=1.0, step=0.5, disabled=not sep)
        center = st.checkbox("auto-center mesh (trimesh)", value=True)

    sid = sod + odd
    st.caption(f"SID = {sid:.0f} mm · pixel size = {film_xy/pix:.3f} mm/px · "
               f"กำลังขยาย = {sid/sod:.2f}×")

    if photons >= 10_000_000 and threads == 1:
        st.info("โฟตอนเยอะและใช้ thread เดียว อาจใช้เวลานาน (40M ≈ ~1 ชม.) — เพิ่ม Threads ได้")

    if st.button("▶️ เริ่มรันจำลอง", type="primary", disabled=not sel):
        cfg = SimConfig(
            stl=str(MODELS_DIR / sel), out=str(RUNS_DIR / run_name),
            photons=int(photons), threads=int(threads), energy_keV=float(energy),
            object_material=material.strip(), pix=int(pix),
            sod=float(sod), odd=float(odd), film_xy=float(film_xy),
            center_mesh=center, separate_primary_scatter=sep, write_phsp=sep,
            primary_theta_deg=float(theta), primary_dE_keV=float(dE), make_png=True,
        )
        log_lines: list[str] = []
        log_box = st.empty()
        t0 = time.time()

        def progress(msg: str):
            log_lines.append(msg)
            log_box.code("\n".join(log_lines[-40:]), language="text")

        try:
            with st.spinner("กำลังรัน GATE (flat → object → post-process) ..."):
                result = run_simulation(cfg, progress=progress, clean=True)
            st.success(f"เสร็จใน {time.time()-t0:.1f} วินาที · ผลลัพธ์: {result.out_dir}")
            if result.primary_fraction is not None:
                st.metric("Primary-like fraction", f"{result.primary_fraction:.4f}")
            st.session_state["last_run_images"] = {k: str(v) for k, v in result.images.items() if v}
        except Exception as e:  # show full error in UI
            st.error(f"รันไม่สำเร็จ: {e}")
            st.exception(e)

    imgs = st.session_state.get("last_run_images")
    if imgs:
        st.divider()
        st.subheader("ผลภาพล่าสุด")
        cols = st.columns(3)
        for i, (label, path) in enumerate(imgs.items()):
            if Path(path).exists():
                cols[i % 3].image(path, caption=label, use_container_width=True)


# ----------------------------------------------------------------------
# Tab 2 — Compare two runs
# ----------------------------------------------------------------------

with tab_compare:
    st.subheader("เปรียบเทียบ SPR signature ระหว่างสองการรัน")
    runs = list_runs()
    if len(runs) < 2:
        st.info("ต้องมีอย่างน้อย 2 การรันที่เสร็จแล้ว (มี object/SPR.mhd) — รันในแท็บแรกก่อน")
    else:
        names = [str(r.relative_to(ROOT)) for r in runs]
        c1, c2 = st.columns(2)
        ctrl = c1.selectbox("Control (ปกติ)", names, index=0)
        frac = c2.selectbox("Fracture (แตก)", names, index=min(1, len(names) - 1))

        st.markdown("**ROI** (ตำแหน่งที่สนใจ — เว้นว่างให้ auto-detect จากจุดต่างมากสุด)")
        auto = st.checkbox("auto-detect ROI", value=True)
        cc = st.columns(4)
        x0 = cc[0].number_input("x0", value=279, disabled=auto)
        y0 = cc[1].number_input("y0", value=107, disabled=auto)
        w = cc[2].number_input("w", value=53, disabled=auto)
        h = cc[3].number_input("h", value=52, disabled=auto)
        win = st.slider("local window (px)", 5, 31, 15, step=2)

        if st.button("📊 วิเคราะห์เปรียบเทียบ", type="primary"):
            try:
                roi = None if auto else ROI(int(x0), int(y0), int(w), int(h))
                with st.spinner("กำลังคำนวณ dSPR / local-STD / entropy ..."):
                    res = compare_runs(ROOT / ctrl, ROOT / frac, roi=roi, win=int(win))
                    out_dir = res.save(RUNS_DIR / "_compare_latest")
                st.success("เสร็จแล้ว")

                m = res.summary
                k1, k2, k3 = st.columns(3)
                k1.metric("dSPR (mean in ROI)", f"{m['dSPR_mean_in_ROI']:.5f}")
                k2.metric("dSTD_local", f"{m['dSTD_local_mean_in_ROI']:.5f}")
                k3.metric("dEntropy_local", f"{m['dEntropy_local_mean_in_ROI']:.5f}")
                st.caption(f"ROI = ({res.roi.x0}, {res.roi.y0}, {res.roi.w}×{res.roi.h}) · "
                           f"window={m['local_window']} · {m['note']}")

                st.divider()
                cols = st.columns(3)
                for i, label in enumerate([
                    "SPR_control", "SPR_fracture", "dSPR_fracture_minus_control",
                    "localSTD_control", "localSTD_fracture", "dSTD_fracture_minus_control",
                    "entropy_control", "entropy_fracture", "dEntropy_fracture_minus_control",
                ]):
                    png = out_dir / f"{label}.png"
                    if png.exists():
                        cols[i % 3].image(str(png), caption=label, use_container_width=True)
            except Exception as e:
                st.error(f"วิเคราะห์ไม่สำเร็จ: {e}")
                st.exception(e)
