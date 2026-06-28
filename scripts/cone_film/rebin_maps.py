import numpy as np
import matplotlib.pyplot as plt

# ---------------------------
# rebin: factor f (เช่น 2 = 128->64, 4 = 128->32)
# mode="sum" เหมาะกับ counts (P,S)
# mode="mean" เหมาะกับ ratio (SPR) หรือค่าต่อพิกเซล
# ---------------------------
def rebin2d(A, f, mode="sum"):
    H, W = A.shape
    H2, W2 = (H // f) * f, (W // f) * f
    A = A[:H2, :W2]
    B = A.reshape(H2 // f, f, W2 // f, f)
    if mode == "sum":
        return B.sum(axis=(1, 3))
    elif mode == "mean":
        return B.mean(axis=(1, 3))
    else:
        raise ValueError("mode must be 'sum' or 'mean'")

def save_img(arr, title, fname, log=False, clip=99.5):
    plt.figure()
    img = np.log10(1 + arr) if log else arr
    vmax = np.percentile(img, clip)
    plt.imshow(img, origin="lower", vmin=np.min(img), vmax=vmax)
    plt.title(title)
    plt.colorbar()
    plt.tight_layout()
    plt.savefig(fname, dpi=200)
    plt.close()

def main():
    f = 2  # 128->64 (ลอง 2 ก่อน ถ้ายังหยาบค่อย 4)
    P = np.load("P.npy")
    S = np.load("S.npy")
    SPR = np.load("SPR.npy")

    # counts: sum
    P2 = rebin2d(P, f, mode="sum")
    S2 = rebin2d(S, f, mode="sum")

    # ratio: recompute SPR จาก rebinned counts (ดีกว่า rebin SPR ตรง ๆ)
    SPR2 = np.zeros_like(P2, dtype=np.float64)
    m = P2 > 0
    SPR2[m] = S2[m] / P2[m]

    np.save(f"P_rebin{f}.npy", P2)
    np.save(f"S_rebin{f}.npy", S2)
    np.save(f"SPR_rebin{f}.npy", SPR2)

    save_img(P2,  f"Primary log10(1+P) rebin x{f}", f"primary_rebin{f}_log.png", log=True)
    save_img(S2,  f"Scatter log10(1+S) rebin x{f}", f"scatter_rebin{f}_log.png", log=True)
    save_img(SPR2, f"SPR=S/P rebin x{f}",         f"spr_rebin{f}.png", log=False)

    print(f"DONE: rebin factor {f}")
    print(f"- primary_rebin{f}_log.png")
    print(f"- scatter_rebin{f}_log.png")
    print(f"- spr_rebin{f}.png")
    print(f"- P_rebin{f}.npy, S_rebin{f}.npy, SPR_rebin{f}.npy")

if __name__ == "__main__":
    main()
