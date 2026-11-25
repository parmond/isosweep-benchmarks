# Copyright 2026 Parmond AB
# SPDX-License-Identifier: Apache-2.0
"""Benchmark inputs: fields and levels, with integrity hashes.

All file reading happens here, before any timed region. Real datasets are
verified against the sha256 pinned in the benchmark spec; every field's own
sha256 is recorded in the results so a reviewer can confirm all tools
contoured identical data.
"""
import hashlib
import os
import subprocess

import numpy as np

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def sha256_file(path, bufsize=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_array(a):
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


def _analytic3d(field, n):
    lin = np.linspace(-0.5, 0.5, n, dtype=np.float32)
    x, y, z = np.meshgrid(lin, lin, lin, indexing="ij")
    if field == "sphere":
        v = np.sqrt(x * x + y * y + z * z) - np.float32(0.4)
    elif field == "torus":
        q = np.sqrt(x * x + y * y) - np.float32(0.30)
        v = np.sqrt(q * q + z * z) - np.float32(0.12)
    elif field == "gyroid":
        k = np.float32(4.0 * np.pi)
        x, y, z = x * k, y * k, z * k
        v = np.cos(x) * np.sin(y) + np.cos(y) * np.sin(z) + np.cos(z) * np.sin(x)
    elif field == "smooth":
        k = np.float32(3.0 * np.pi)
        v = np.sin(x * k) * np.sin(y * k) * np.sin(z * k)
    else:
        raise ValueError(f"unknown analytic3d field {field}")
    return np.ascontiguousarray(v.astype(np.float32))


def load_field(bench, data_dir, cache_dir):
    """Return (array, raw_f64_path_or_None, info). 2D → float64, 3D → float32."""
    d, bid = bench["data"], bench["id"]
    os.makedirs(cache_dir, exist_ok=True)
    info = {}
    if d.get("generator") == "analytic3d":
        z = _analytic3d(d["field"], int(d["size"]))
        info["source"] = f"analytic3d:{d['field']}:{d['size']}"
        info["field_sha256"] = sha256_array(z)
        return z, None, info
    path = os.path.join(cache_dir, f"{bid}.f64")
    if d.get("generator") == "fieldgen":
        fieldgen = os.path.join(REPO, "build", "fieldgen")      # make fieldgen (tools/fieldgen)
        subprocess.run([fieldgen, "--profile", d["profile"], "--seed", str(d["seed"]),
                        "--size", str(d["size"]), "--out", path],
                       check=True, capture_output=True)
        n = int(d["size"])
        z = np.fromfile(path, dtype="<f8").reshape(n, n)
        info["source"] = f"fieldgen:{d['profile']}:{d['size']}:seed{d['seed']}"
    else:
        src = os.path.join(data_dir, d["dataset"])
        got = sha256_file(src)
        if got != d["sha256"]:
            raise RuntimeError(f"{bid}: dataset {d['dataset']} sha256 {got} != spec {d['sha256']}")
        info["source"] = f"dataset:{d['dataset']}"
        info["dataset_sha256"] = got
        if "raw_shape" in d:
            ny, nx = d["raw_shape"]
            z = np.fromfile(src, dtype="<f4").astype(np.float64).reshape(ny, nx)
        else:
            helper = os.environ.get("TIF2F64", os.path.join(os.path.expanduser("~"), "bin", "tif2f64"))
            win = [str(v) for v in d.get("window", [])]
            out = subprocess.run([helper, src, path, "1", *win], check=True,
                                 capture_output=True, text=True).stdout.split()
            nx, ny = int(out[0]), int(out[1])
            z = np.fromfile(path, dtype="<f8").reshape(ny, nx)
    if not np.all(np.isfinite(z)):
        raise RuntimeError(f"{bid}: field contains NaN/Inf — benchmark fields must be void-free")
    # Every 2D benchmark field is float32-valued (as the real datasets — GFS, HRRR, DEMs,
    # bathymetry — are): synthetic fields are rounded once here, so every tool, whatever
    # its native dtype, receives exactly the same values (W3/W5: no input-precision asymmetry)
    z = np.ascontiguousarray(np.asarray(z, dtype=np.float32), dtype=np.float64)
    info["float32_exact"] = True
    z.astype("<f8").tofile(path)   # the exact bytes every worker reads
    info["field_sha256"] = sha256_array(z)
    info["shape"] = list(z.shape)
    return z, path, info


def nice_grid(lo, hi, count):
    """A binary-exact level grid off + k * s (s = m * 2^e, m in 1, 1.25, 1.5, 1.75;
    off = 0 or s/2) with neither extreme on the grid and about `count` levels
    strictly inside (lo, hi)."""
    raw = (hi - lo) / (count + 1)
    e = int(np.floor(np.log2(raw)))
    best = None
    for ee in (e - 1, e, e + 1):
        for m in (1.0, 1.25, 1.5, 1.75):
            for half in (False, True):
                s = m * 2.0 ** ee
                off = s / 2 if half else 0.0
                if any(((v - off) / s) == np.floor((v - off) / s) for v in (lo, hi)):
                    continue    # an extreme on the grid: GDAL's callback would contour it too
                n = len(_grid_inside(lo, hi, s, off))
                key = (abs(n - count), half, -s)
                if best is None or key < best[0]:
                    best = (key, s, off)
    return best[1], best[2]


def _grid_inside(lo, hi, s, off=0.0):
    k0, k1 = int(np.floor((lo - off) / s)) + 1, int(np.ceil((hi - off) / s)) - 1
    return off + np.arange(k0, k1 + 1, dtype=np.float64) * s


def make_levels(bench, z):
    """Levels are float32-exact and, for count / interval specs, ALL points of one
    exact grid strictly inside (min, max), with neither extreme on it: a tool that
    contours base + k * interval over the data range (GDAL's callback API) computes
    exactly these levels, no more."""
    lv = bench["levels"]
    lo, hi = float(z.min()), float(z.max())
    if "count" in lv:
        levels = _grid_inside(lo, hi, *nice_grid(lo, hi, int(lv["count"])))
    elif "interval" in lv:
        s = float(lv["interval"])
        levels = np.arange(np.floor(lo / s) * s + s, hi, s)
        levels = levels[(levels > lo) & (levels < hi)]
    else:
        levels = np.asarray(lv["values"], dtype=np.float64)
    levels = np.ascontiguousarray(levels, dtype=np.float64)
    if not np.all(levels.astype(np.float32).astype(np.float64) == levels):
        raise RuntimeError(f"{bench['id']}: levels are not float32-exact")
    return levels
