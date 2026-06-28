import uproot
import numpy as np
import matplotlib.pyplot as plt

root_path = "phsp_film.root"

# ต้องตรงกับตอน simulation
pix = 256
film_xy_mm = 400.0
half = film_xy_mm / 2.0

with uproot.open(root_path) as f:
    tree = f["phsp_film"]  # จาก error ของคุณมันคือ /phsp_film;1

    x = tree["Position_X"].array(library="np").astype(np.float64)
    y = tree["Position_Y"].array(library="np").astype(np.float64)

    # ถ้ามี UnscatteredPrimaryFlag จะดีที่สุด
    has_flag = "UnscatteredPrimaryFlag" in tree.keys()
    if has_flag:
        flag = tree["UnscatteredPrimaryFlag"].array(library="np")
        is_primary = (flag == 1)
    else:
        # fallback: ใช้ ProcessDefinedStep เป็นตัวแบ่งคร่าว ๆ
        # primary มักเป็น 'Transportation' (ชื่อจริงอาจต่างเล็กน้อย)
        proc = tree["ProcessDefinedStep"].array(library="np")
        # proc อาจเป็น bytes/str; แปลงให้เทียบง่าย
        proc = np.array([p.decode() if isinstance(p, (bytes, bytearray)) else str(p) for p in proc])
        is_primary = (proc == "Transportation")

def hist2(xv, yv, mask):
    H, _, _ = np.histogram2d(
        xv[mask], yv[mask],
        bins=pix,
        range=[[-half, half], [-half, half]]
    )
    return H.T  # transpose ให้ตรงกับ imshow(origin='lower')

P = hist2(x, y, is_primary)
S = hist2(x, y, ~is_primary)

SPR = np.zeros_like(P, dtype=np.float64)
SPR[P > 0] = S[P > 0] / P[P > 0]

def save_png(arr, title, fname, log=False):
    plt.figure()
    img = np.log10(1 + arr) if log else arr
    plt.imshow(img, origin="lower")
    plt.title(title)
    plt.colorbar()
    plt.tight_layout()
    plt.savefig(fname, dpi=200)
    plt.close()

save_png(P, "Primary log10(1+P)", "primary_log.png", log=True)
save_png(S, "Scatter log10(1+S)", "scatter_log.png", log=True)
save_png(SPR, "SPR = S/P", "spr.png", log=False)

np.save("P.npy", P)
np.save("S.npy", S)
np.save("SPR.npy", SPR)

print("DONE -> primary_log.png, scatter_log.png, spr.png + P/S/SPR .npy")
print("Used UnscatteredPrimaryFlag?" , has_flag)
