import numpy as np
import SimpleITK as sitk
import matplotlib.pyplot as plt
from pathlib import Path

def gaussian_blur_2d(a, sigma=1.0):
    # Gaussian blur แบบ pure numpy (ไม่พึ่ง scipy)
    if sigma <= 0:
        return a
    radius = max(1, int(3 * sigma))
    x = np.arange(-radius, radius + 1, dtype=np.float32)
    k = np.exp(-(x * x) / (2 * sigma * sigma))
    k /= np.sum(k)

    # convolve separable: rows then cols
    # pad reflect
    ap = np.pad(a, ((radius, radius), (radius, radius)), mode="reflect")
    # along rows
    tmp = np.apply_along_axis(lambda m: np.convolve(m, k, mode="same"), axis=1, arr=ap)
    # along cols
    tmp = np.apply_along_axis(lambda m: np.convolve(m, k, mode="same"), axis=0, arr=tmp)
    # crop back
    return tmp[radius:-radius, radius:-radius]

def median_filter_3x3(a):
    # median 3x3 แบบ numpy (เร็วพอสำหรับภาพ 512/1024)
    ap = np.pad(a, ((1, 1), (1, 1)), mode="edge")
    w = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            w.append(ap[1+dy:1+dy+a.shape[0], 1+dx:1+dx+a.shape[1]])
    stack = np.stack(w, axis=0)
    return np.median(stack, axis=0)

def film_like_from_I_I0(mhd_I, mhd_I0, out_png="film_like.png",
                        mask_thr=5, clip_lo=1, clip_hi=99, gamma=0.8,
                        sigma=1.0):
    # read (3D image with z=1)
    I_img  = sitk.ReadImage(str(mhd_I))
    I0_img = sitk.ReadImage(str(mhd_I0))

    eps = 1e-6
    I  = sitk.GetArrayFromImage(sitk.Cast(I_img,  sitk.sitkFloat32))[0]   # 2D
    I0 = sitk.GetArrayFromImage(sitk.Cast(I0_img, sitk.sitkFloat32))[0]   # 2D

    # attenuation A = -ln((I+eps)/(I0+eps))
    ratio = (I + eps) / (I0 + eps)
    A = -np.log(ratio)

    # mask where I0 too low
    mask = I0 > float(mask_thr)
    A = np.where(mask, A, 0.0).astype(np.float32)

    # denoise 2D: median + gaussian
    A = median_filter_3x3(A)
    A = gaussian_blur_2d(A, sigma=sigma)

    # window/level: percentile clip (ignore zeros)
    valid = A[A > 0]
    if valid.size < 100:
        print("Warning: too few valid pixels after mask; lower mask_thr")
        valid = A.flatten()

    lo = np.percentile(valid, clip_lo)
    hi = np.percentile(valid, clip_hi)
    A = np.clip(A, lo, hi)
    A = (A - lo) / (hi - lo + 1e-12)  # 0..1

    # gamma + invert (radiograph look)
    A = np.power(A, gamma)
    #A = 1.0 - A

    plt.figure()
    plt.imshow(A, cmap="gray", vmin=0, vmax=1)
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(str(out_png), dpi=200, bbox_inches="tight", pad_inches=0)
    plt.close()
    print("Saved:", out_png)


if __name__ == "__main__":
    BASE = Path(__file__).resolve().parent
    film_like_from_I_I0(
       BASE / "object" / "I.mhd",
        BASE / "flat" / "I.mhd",
        out_png=BASE / "film_like.png",
        mask_thr=1,        # ลดลงมาก
        clip_lo=5,
        clip_hi=99.7,
        gamma=1.2,
        sigma=2.0,
    )

