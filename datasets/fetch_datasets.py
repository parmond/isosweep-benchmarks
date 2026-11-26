#!/usr/bin/env python3
# Copyright 2026 Parmond AB
# SPDX-License-Identifier: Apache-2.0
"""Fetch and prepare the real datasets of the benchmark suite, then verify them.

    python3 datasets/fetch_datasets.py [--out data] [--only NAME,...] [--keep-raw]

Every file is rebuilt from its public source exactly as recorded in
datasets/DATASETS.md and checked against datasets/MANIFEST.sha256 (the
checksums the benchmark specifications and the published results use).

Needs: Python 3.10+, numpy, GDAL command-line tools (gdal_translate,
gdalbuildvrt; the reference used GDAL 3.13.3), and scipy for the GMRT grid.

Reproducibility notes:
- cop_N46E007.tif is used as downloaded, and the four .f32 fields are raw
  float32 values decoded from NOAA's archived GRIB2 files: these should match
  byte for byte.
- alps_7200.tif and gmrt_mar_filled.tif are written as GeoTIFFs; a different
  GDAL/zlib version can write different bytes for identical values, and the
  GMRT server may serve updated data. A mismatch there is reported with the
  checksum of the decoded values, so you can tell a byte-level difference from
  a data difference.
"""
import argparse
import hashlib
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
COP = "https://copernicus-dem-30m.s3.amazonaws.com/{t}/{t}.tif"
COP_TILE = "Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM"
GMRT = ("https://www.gmrt.org/services/GridServer?minlongitude=-46&maxlongitude=-42&minlatitude=26"
        "&maxlatitude=30&format=geotiff&resolution=high&layer=topo")
GFS = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261118/00/atmos/gfs.t00z.pgrb2.0p25.f000"
HRRR = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20261118/conus/hrrr.t00z.wrfsfcf00.grib2"
# sha256 of the decoded field (float64, row-major) as recorded in the published
# results (measurements.jsonl, data.field_sha256), for files whose bytes depend
# on the server's encoding: same values = the same benchmark input
FIELD_SHA = {"gmrt_mar_filled.tif": "0afdb1ffceeed8dc9b58c24b74b0bf4a1c441ca85531a2c9f63ed872b0c9dadf"}


def manifest():
    out = {}
    for line in open(os.path.join(HERE, "MANIFEST.sha256")):
        if line.strip() and not line.startswith("#"):
            sha, name = line.split()
            out[name] = sha
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def get(url, dest, byte_range=None):
    if os.path.exists(dest) and os.path.getsize(dest) > 0 and not byte_range:
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": "isosweep-benchmarks/1.0"})
    if byte_range:
        req.add_header("Range", f"bytes={byte_range[0]}-{byte_range[1] if byte_range[1] is not None else ''}")
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    if os.path.getsize(tmp) == 0:
        os.remove(tmp)
        raise SystemExit(f"empty response from {url}; the server may be busy, try again later")
    os.replace(tmp, dest)
    return dest


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)


def grib_message(url, match, dest, raw):
    """Download one GRIB2 message by its byte range (from the .idx index)."""
    idx = urllib.request.urlopen(urllib.request.Request(url + ".idx", headers={"User-Agent": "isosweep-benchmarks/1.0"}),
                                 timeout=120).read().decode().splitlines()
    rows = [l.split(":") for l in idx if l.strip()]
    for i, r in enumerate(rows):
        if f"{r[3]}:{r[4]}" == match:
            start = int(r[1])
            end = int(rows[i + 1][1]) - 1 if i + 1 < len(rows) else None
            return get(url, os.path.join(raw, dest), (start, end))
    raise SystemExit(f"{match} not found in {url}.idx")


def to_f32(grib, dest):
    """Decode a GRIB2 message with GDAL to raw little-endian float32 (rows north to south)."""
    envi = dest + ".envi"
    env = dict(os.environ, GRIB_NORMALIZE_UNITS="YES")
    subprocess.run(["gdal_translate", "-q", "-ot", "Float32", "-of", "ENVI", grib, envi], check=True, env=env)
    os.replace(envi, dest)
    for ext in (".hdr", ".aux.xml"):
        for p in (envi + ext, dest + ".envi" + ext, os.path.splitext(envi)[0] + ext):
            if os.path.exists(p):
                os.remove(p)


def values_sha(path):
    """sha256 of a raster's decoded float32 values (to separate data from file-format differences)."""
    import numpy as np
    if path.endswith(".f32"):
        return hashlib.sha256(np.fromfile(path, dtype="<f4").tobytes()).hexdigest()
    tmp = path + ".values.f32"
    run(["gdal_translate", "-q", "-ot", "Float32", "-of", "ENVI", path, tmp])
    h = hashlib.sha256(np.fromfile(tmp, dtype="<f4").tobytes()).hexdigest()
    for p in (tmp, tmp + ".hdr", os.path.splitext(tmp)[0] + ".hdr", tmp + ".aux.xml"):
        if os.path.exists(p):
            os.remove(p)
    return h


def field_sha(path):
    """sha256 of the raster's values as float64 (the harness's field fingerprint)."""
    import numpy as np
    tmp = path + ".field.f64"
    run(["gdal_translate", "-q", "-ot", "Float64", "-of", "ENVI", path, tmp])
    h = hashlib.sha256(np.fromfile(tmp, dtype="<f8").tobytes()).hexdigest()
    for p in (tmp, tmp + ".hdr", os.path.splitext(tmp)[0] + ".hdr", tmp + ".aux.xml"):
        if os.path.exists(p):
            os.remove(p)
    return h


def build(name, out, raw):
    dest = os.path.join(out, name)
    if name == "cop_N46E007.tif":
        t = COP_TILE.format(lat="N46", lon="E007")
        get(COP.format(t=t), dest)
    elif name == "alps_7200.tif":
        tiles = []
        for lat in ("N45", "N46"):
            for lon in ("E007", "E008"):
                t = COP_TILE.format(lat=lat, lon=lon)
                tiles.append(get(COP.format(t=t), os.path.join(raw, t + ".tif")))
        vrt = os.path.join(raw, "alps.vrt")
        run(["gdalbuildvrt", "-q", vrt] + tiles)
        run(["gdal_translate", "-q", "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", vrt, dest])
    elif name == "gmrt_mar_filled.tif":
        import numpy as np
        from scipy.ndimage import distance_transform_edt
        src = get(GMRT, os.path.join(raw, "gmrt_mar.tif"))
        tmp = os.path.join(raw, "gmrt_mar.f32")
        run(["gdal_translate", "-q", "-ot", "Float32", "-of", "ENVI", src, tmp])
        info = subprocess.run(["gdalinfo", src], capture_output=True, text=True, check=True).stdout
        size = next(l for l in info.splitlines() if l.startswith("Size is")).split("is")[1]
        nx, ny = (int(v) for v in size.split(","))
        z = np.fromfile(tmp, dtype="<f4").reshape(ny, nx)
        nd = [l for l in info.splitlines() if "NoData Value=" in l]
        bad = ~np.isfinite(z)
        if nd:
            bad |= z == np.float32(float(nd[0].split("=")[1]))
        idx = distance_transform_edt(bad, return_distances=False, return_indices=True)
        z = z[tuple(idx)]                               # nearest valid cell
        z.astype("<f4").tofile(tmp)
        run(["gdal_translate", "-q", "-a_nodata", "none", "-of", "GTiff", tmp, dest])
    elif name in ("gfs_prmsl.f32", "gfs_t2m.f32"):
        match = "PRMSL:mean sea level" if "prmsl" in name else "TMP:2 m above ground"
        to_f32(grib_message(GFS, match, name + ".grib2", raw), dest)
    elif name in ("hrrr_mslp.f32", "hrrr_t2m.f32"):
        match = "MSLMA:mean sea level" if "mslp" in name else "TMP:2 m above ground"
        to_f32(grib_message(HRRR, match, name + ".grib2", raw), dest)
    else:
        raise SystemExit(f"unknown dataset {name}")
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--only", default="")
    ap.add_argument("--keep-raw", action="store_true")
    a = ap.parse_args()
    want = manifest()
    names = a.only.split(",") if a.only else list(want)
    raw = os.path.join(a.out, "raw")
    os.makedirs(raw, exist_ok=True)
    bad = 0
    for name in names:
        print(f"{name}: fetching ...", flush=True)
        path = build(name, a.out, raw)
        got = sha256(path)
        if got == want[name]:
            print(f"  ok        {got}")
        elif name in FIELD_SHA and field_sha(path) == FIELD_SHA[name]:
            bad += 1
            print(f"  VALUES OK, BYTES DIFFER  {got}\n"
                  f"            the values are identical to the published benchmark field (fingerprint\n"
                  f"            {FIELD_SHA[name][:16]}...), but the server now encodes the file differently;\n"
                  f"            the harness checks file bytes, so this dataset's benchmarks will refuse it")
        else:
            bad += 1
            print(f"  MISMATCH  {got} (expected {want[name]}); decoded values sha256 {values_sha(path)}")
    with open(os.path.join(a.out, "MANIFEST.sha256"), "w") as f:
        for name in names:
            f.write(f"{want[name]}  {name}\n")
    if not a.keep_raw:
        import shutil
        shutil.rmtree(raw, ignore_errors=True)
    print("all datasets verified" if not bad else f"{bad} dataset(s) differ from the published checksums")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
