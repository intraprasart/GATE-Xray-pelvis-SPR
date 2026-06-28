import struct
import argparse
import os

def is_binary_stl(path: str) -> bool:
    # heuristic: binary STL file size should match 84 + 50*n
    size = os.path.getsize(path)
    if size < 84:
        return False
    with open(path, "rb") as f:
        header = f.read(80)
        tri_count = struct.unpack("<I", f.read(4))[0]
    expected = 84 + 50 * tri_count
    return expected == size

def scale_binary_stl(in_path: str, out_path: str, scale: float):
    with open(in_path, "rb") as f:
        header = f.read(80)
        tri_count = struct.unpack("<I", f.read(4))[0]
        data = f.read()

    out = bytearray()
    out.extend(header)
    out.extend(struct.pack("<I", tri_count))

    # Each triangle = 50 bytes:
    # normal (3 floats) + v1(3) + v2(3) + v3(3) + attr (2 bytes)
    offset = 0
    for _ in range(tri_count):
        chunk = data[offset:offset+50]
        # unpack 12 floats + uint16
        vals = struct.unpack("<12fH", chunk)
        # normal unchanged, vertices scaled
        n = vals[0:3]
        v1 = [vals[3]*scale, vals[4]*scale, vals[5]*scale]
        v2 = [vals[6]*scale, vals[7]*scale, vals[8]*scale]
        v3 = [vals[9]*scale, vals[10]*scale, vals[11]*scale]
        attr = vals[12]
        out.extend(struct.pack("<3f", *n))
        out.extend(struct.pack("<3f", *v1))
        out.extend(struct.pack("<3f", *v2))
        out.extend(struct.pack("<3f", *v3))
        out.extend(struct.pack("<H", attr))
        offset += 50

    with open(out_path, "wb") as f:
        f.write(out)

def scale_ascii_stl(in_path: str, out_path: str, scale: float):
    # minimal ASCII scaler: scales "vertex x y z" lines
    with open(in_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    out_lines = []
    for line in lines:
        s = line.strip()
        if s.lower().startswith("vertex "):
            parts = s.split()
            x, y, z = map(float, parts[1:4])
            x *= scale; y *= scale; z *= scale
            # keep indentation roughly
            indent = line[:len(line) - len(line.lstrip())]
            out_lines.append(f"{indent}vertex {x:.6f} {y:.6f} {z:.6f}\n")
        else:
            out_lines.append(line)

    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(out_lines)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--out", dest="out_path", required=True)
    ap.add_argument("--scale", type=float, required=True)
    args = ap.parse_args()

    if is_binary_stl(args.in_path):
        scale_binary_stl(args.in_path, args.out_path, args.scale)
    else:
        scale_ascii_stl(args.in_path, args.out_path, args.scale)

    print(f"Scaled STL written: {args.out_path}")

if __name__ == "__main__":
    main()
