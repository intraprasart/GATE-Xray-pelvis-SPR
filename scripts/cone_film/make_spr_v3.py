#!/usr/bin/env python3
import os
import argparse
import numpy as np
import uproot
import awkward as ak
import matplotlib.pyplot as plt

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
    vmin = np.min(img)
    plt.imshow(img, origin="lower", vmin=vmin, vmax=vmax)
    plt.title(title)
    plt.colorbar()
    plt.tight_layout()
    plt.savefig(fname, dpi=200)
    plt.close()

def box_filter_sum(img, r):
    H, W = img.shape
    pad = r
    A = np.pad(img, ((pad, pad), (pad, pad)), mode="constant", constant_values=0)
    S = A.cumsum(axis=0).cumsum(axis=1)
    y0 = np.arange(0, H)
    y1 = y0 + 2*pad + 1
    x0 = np.arange(0, W)
    x1 = x0 + 2*pad + 1
    return (
        S[np.ix_(y1, x1)]
        - S[np.ix_(y0, x1)]
        - S[np.ix_(y1, x0)]
        + S[np.ix_(y0, x0)]
    )

def local_mean_var(img, r):
    k = (2*r+1)**2
    s1 = box_filter_sum(img, r)
    s2 = box_filter_sum(img*img, r)
    mean = s1 / k
    var = s2 / k - mean*mean
    var = np.maximum(var, 0)
    return mean, var

def local_entropy_counts(counts, r, bins=16):
    H, W = counts.shape
    eps = 1e-12
    x = np.log1p(counts.astype(np.float64))
    lo, hi = np.percentile(x, 1), np.percentile(x, 99)
    if hi <= lo:
        return np.zeros_like(x)

    edges = np.linspace(lo, hi, bins+1)
    ent = np.zeros((H, W), dtype=np.float64)
    k = (2*r+1)**2

    for b in range(bins):
        m = ((x >= edges[b]) & (x < edges[b+1])).astype(np.float64)
        pb = box_filter_sum(m, r) / k
        ent -= pb * np.log2(pb + eps)

    return ent

def read_xy(tree):
    keys = set(tree.keys())

    # Case 1: split components (your current ROOT)
    if "Position_X" in keys and "Position_Y" in keys:
        x = tree["Position_X"].array(library="np").astype(np.float64)
        y = tree["Position_Y"].array(library="np").astype(np.float64)
        return x, y, "split(Position_X/Y)"

    # Case 2: vector branch
    if "Position" in keys:
        pos = tree["Position"].array(library="ak")
        pos_np = ak.to_numpy(pos)
        if pos_np.ndim != 2 or pos_np.shape[1] != 3:
            raise RuntimeError(f"Unexpected Position shape: {pos_np.shape}")
        return pos_np[:, 0].astype(np.float64), pos_np[:, 1].astype(np.float64), "vector(Position)"

    raise KeyError("Cannot find Position_X/Y or Position in ROOT tree.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=str, default="phsp_film.root")
    ap.add_argument("--tree", type=str, default="phsp_film")
    ap.add_argument("--pix", type=int, default=128)
    ap.add_argument("--film_xy", type=float, default=400.0)
    ap.add_argument("--rebin", type=int, default=2)
    ap.add_argument("--clip", type=float, default=99.5)
    ap.add_argument("--radius", type=int, default=2)
    ap.add_argument("--bins", type=int, default=16)
    args = ap.parse_args()

    if not os.path.exists(args.root):
        raise FileNotFoundError(args.root)

    t = uproot.open(args.root)[args.tree]
    keys = set(t.keys())

    # Read x,y robustly
    x, y, mode_xy = read_xy(t)
    print("XY mode:", mode_xy)

    # Flags + weights
    if "UnscatteredPrimaryFlag" not in keys:
        raise KeyError("ROOT does not contain UnscatteredPrimaryFlag. Please ensure PhaseSpaceActor saves it.")

    flag = t["UnscatteredPrimaryFlag"].array(library="np")
    if "Weight" in keys:
        w = t["Weight"].array(library="np").astype(np.float64)
    else:
        w = np.ones_like(x, dtype=np.float64)

    pix = args.pix
    half = args.film_xy * 0.5
    edges = np.linspace(-half, half, pix+1)

    def hist2(xv, yv, wv):
        H2, _, _ = np.histogram2d(xv, yv, bins=[edges, edges], weights=wv)
        return H2.T

    isP = (flag == 1)
    isS = ~isP

    total = hist2(x, y, w)
    P = hist2(x[isP], y[isP], w[isP])
    S = hist2(x[isS], y[isS], w[isS])

    print("=== SANITY ===")
    tot_sum = float(total.sum())
    p_sum = float(P.sum())
    s_sum = float(S.sum())
    print("total hits  :", int(tot_sum))
    print("primary hits:", int(p_sum), f"({100.0*p_sum/max(tot_sum,1):.2f}%)")
    print("scatter hits:", int(s_sum), f"({100.0*s_sum/max(tot_sum,1):.2f}%)")

    SPR = np.zeros_like(P, dtype=np.float64)
    m = P > 0
    SPR[m] = S[m] / P[m]

    np.save("TOTAL.npy", total)
    np.save("P.npy", P)
    np.save("S.npy", S)
    np.save("SPR.npy", SPR)

    save_img(total, "TOTAL log10(1+counts)", "total_log.png", log=True, clip=args.clip)
    save_img(P,     "Primary log10(1+P)",    "primary_log.png", log=True, clip=args.clip)
    save_img(S,     "Scatter log10(1+S)",    "scatter_log.png", log=True, clip=args.clip)
    save_img(SPR,   "SPR=S/P",               "spr.png", log=False, clip=args.clip)

    rbf = args.rebin
    if rbf > 1:
        total_r = rebin2d(total, rbf, "sum")
        P_r     = rebin2d(P,     rbf, "sum")
        S_r     = rebin2d(S,     rbf, "sum")
        SPR_r   = np.zeros_like(P_r, dtype=np.float64)
        mm = P_r > 0
        SPR_r[mm] = S_r[mm] / P_r[mm]

        np.save(f"TOTAL_rebin{rbf}.npy", total_r)
        np.save(f"P_rebin{rbf}.npy", P_r)
        np.save(f"S_rebin{rbf}.npy", S_r)
        np.save(f"SPR_rebin{rbf}.npy", SPR_r)

        save_img(total_r, f"TOTAL log10(1+counts) rebin x{rbf}", f"total_rebin{rbf}_log.png", log=True, clip=args.clip)
        save_img(P_r,     f"Primary log10(1+P) rebin x{rbf}",    f"primary_rebin{rbf}_log.png", log=True, clip=args.clip)
        save_img(S_r,     f"Scatter log10(1+S) rebin x{rbf}",    f"scatter_rebin{rbf}_log.png", log=True, clip=args.clip)
        save_img(SPR_r,   f"SPR=S/P rebin x{rbf}",               f"spr_rebin{rbf}.png", log=False, clip=args.clip)

        img_for_tex = total_r
    else:
        img_for_tex = total

    rr = args.radius
    mean, var = local_mean_var(img_for_tex.astype(np.float64), rr)
    ent = local_entropy_counts(img_for_tex, rr, bins=args.bins)

    np.save("local_mean.npy", mean)
    np.save("local_var.npy", var)
    np.save("local_entropy.npy", ent)

    save_img(var, "Local variance (TOTAL)", "local_var.png", log=False, clip=args.clip)
    save_img(ent, "Local entropy (TOTAL)", "local_entropy.png", log=False, clip=args.clip)

    print("\nDONE: wrote maps + texture (variance/entropy).")

if __name__ == "__main__":
    main()
