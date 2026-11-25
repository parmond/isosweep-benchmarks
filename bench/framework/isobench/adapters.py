# Copyright 2026 Parmond AB
# SPDX-License-Identifier: Apache-2.0
"""Tool adapters — the three-phase contract every measured tool implements.

  prepare(z, levels, field_path)  untimed: load / convert / wrap input, build O(1) objects
  activate()                      untimed, before EVERY sample: (re)apply process-global
                                  state (e.g. VTK's SMP backend) and report it
  compute()                       TIMED by the harness; statically audited against the
                                  tool's compute_allowlist; dynamically audited with strace
  collect(result)                 untimed: raw output for the like-for-like checks — contour
                                  tools return [(level or None, (n,2) points in the tool's own
                                  frame)], normalised and summarised by geometry.summarize_lines
                                  (one code path for every tool); 3D tools return a mesh summary
  engine(result)                  for our tools: the engine the product reports it ran

Native tools run as worker processes (workers/); their clock lives inside the
worker around the BEGIN_TIMED block (adapter.native = True).
"""
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time

import numpy as np

from . import geometry

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "python"))


class Adapter:
    native = False

    def __init__(self, params, ctx):
        self.p = dict(params)
        self.ctx = ctx

    def version(self):
        raise NotImplementedError

    def artifacts(self):
        return {}

    def prepare(self, z, levels, field_path):
        raise NotImplementedError

    def activate(self):
        return {}

    def compute(self):
        raise NotImplementedError

    def collect(self, result):
        raise NotImplementedError

    def engine(self, result):
        return None

    def threads_used(self, result):
        """Threads the product reports it actually used (ours), else None."""
        return None

    def breakdown(self):
        """Untimed diagnostic: where our binding spends a call (W6), else None."""
        return None

    def received(self, arr, source=None):
        """Record the array compute() will be given (R11/W5: observed input) and
        the hash of the shared field it was made from (source, default arr)."""
        a = np.ascontiguousarray(arr)
        src = a if source is None else np.ascontiguousarray(source)
        self._recv = {"dtype": str(a.dtype), "shape": list(a.shape),
                      "sha256": hashlib.sha256(a.tobytes()).hexdigest(),
                      "source_sha256": hashlib.sha256(src.tobytes()).hexdigest()}
        return arr

    def work_observed(self):
        return getattr(self, "_recv", None)

    def close(self):
        pass


def _module_file(mod):
    return getattr(mod, "__file__", None)


# ---------------------------------------------------------------- isosweep (ours)
try:
    import isocurves  # noqa: E402
except ImportError:  # no isosweep build installed: run the other libraries with run.py --exclude-ours
    isocurves = None


class IsosweepContour(Adapter):
    def version(self):
        return isocurves.__version__

    def artifacts(self):
        return {"isocurves._core": _module_file(isocurves._core)}

    def prepare(self, z, levels, field_path):
        # isosweep's native input is float32: converted here, untimed, as every
        # tool gets its input in its own format (W table); accuracy: W3
        self.z = self.received(np.ascontiguousarray(z, dtype=np.float32), source=z)
        self.levels, self.nthreads = levels, int(self.p["nthreads"])
        self.coord_dtype = np.float64 if self.p.get("coords") == "float64" else np.float32

    def activate(self):
        return {"nthreads": self.nthreads, "coords": np.dtype(self.coord_dtype).name}

    def compute(self):
        return isocurves.contour(self.z, self.levels, nthreads=self.nthreads, dtype=self.coord_dtype)

    def engine(self, result):
        return result.engine

    def threads_used(self, result):
        return result.threads_used

    def breakdown(self):
        t0 = time.perf_counter_ns()
        r = self.compute()
        total = time.perf_counter_ns() - t0
        run = isocurves._core._last_run()
        c = run["convert_ns"] + run["engine_ns"] + run["build_ns"]
        del r
        return {"total_ms": total / 1e6, "input_ms": run["convert_ns"] / 1e6,
                "engine_ms": run["engine_ns"] / 1e6, "c_result_ms": run["build_ns"] / 1e6,
                "python_result_ms": max(0, total - c) / 1e6}

    def collect(self, result):
        # closed rings are returned without a repeated first vertex; repeat it
        # (formatting only) so the closing segment counts like every tool's
        out = []
        for c in result:
            a = np.column_stack([c.x, c.y])
            if c.is_closed and len(a) > 1:
                a = np.vstack([a, a[:1]])
            out.append((c.level_value, a))
        return out


class IsosweepIsosurface(Adapter):
    def version(self):
        return isocurves.__version__

    def artifacts(self):
        return {"isocurves._core": _module_file(isocurves._core)}

    def prepare(self, v, levels, field_path):
        self.v = self.received(v)
        self.levels, self.nthreads = [float(x) for x in levels], int(self.p["nthreads"])

    def activate(self):
        return {"nthreads": self.nthreads}

    def compute(self):
        return isocurves.isosurface(self.v, self.levels, nthreads=self.nthreads,
                                    split_components=False, normals=False)

    def engine(self, result):
        return result.engine

    def collect(self, result):
        return geometry.mesh_summary(sum(s.triangles.shape[0] for s in result.surfaces),
                                     sum(s.vertices.shape[0] for s in result.surfaces))


# ---------------------------------------------------------------- ContourPy
import contourpy  # noqa: E402


class ContourPy(Adapter):
    def version(self):
        return contourpy.__version__

    def artifacts(self):
        return {"contourpy._contourpy": _module_file(contourpy._contourpy)}

    def prepare(self, z, levels, field_path):
        self.z, self.levels = self.received(z), levels
        self.kw = {"name": self.p["name"], "line_type": getattr(contourpy.LineType, self.p["line_type"])}
        if self.p["name"] == "threaded":
            self.kw["thread_count"] = int(self.p["thread_count"])
            cc = int(self.p["chunk_count"])
            self.kw["chunk_count"] = (cc, cc)
        # reuse: the generator (O(n) setup) is built once, untimed — ContourPy's
        # documented pattern for repeated calls; favours ContourPy (disclosed)
        self.gen = contourpy.contour_generator(z=self.z, **self.kw) if self.p.get("reuse_generator") else None

    def activate(self):
        e = {k: (str(v) if k == "line_type" else v) for k, v in self.kw.items()}
        e["reuse_generator"] = self.gen is not None
        return e

    def compute(self):
        if self.gen is not None:
            return self.gen.multi_lines(self.levels)
        return contourpy.contour_generator(z=self.z, **self.kw).multi_lines(self.levels)

    def collect(self, result):
        out = []
        for lev, (pts_chunks, off_chunks) in zip(self.levels, result):
            for pts, off in zip(pts_chunks, off_chunks):
                if pts is None:
                    continue
                for i in range(len(off) - 1):
                    out.append((lev, pts[off[i]:off[i + 1]]))
        return out


# ---------------------------------------------------------------- filled bands (class polygons.bands)
class IsosweepFilled(Adapter):
    """isocurves.filled(): every band between consecutive levels plus the two
    outer bands, as polygons with holes. The result holds flat arrays; Band
    objects (exterior + holes) are made on access, in collect()."""
    def version(self):
        return isocurves.__version__

    def artifacts(self):
        return {"isocurves._core": _module_file(isocurves._core)}

    def prepare(self, z, levels, field_path):
        self.z = self.received(np.ascontiguousarray(z, dtype=np.float32), source=z)
        self.levels, self.nthreads = levels, int(self.p["nthreads"])

    def activate(self):
        return {"nthreads": self.nthreads}

    def compute(self):
        return isocurves.filled(self.z, self.levels, nthreads=self.nthreads)

    def engine(self, result):
        return result.engine

    def threads_used(self, result):
        return result.threads_used

    def collect(self, result):
        out = []
        for b in result:
            rings = [np.column_stack([b.exterior.x, b.exterior.y])]
            rings += [np.column_stack([h.x, h.y]) for h in b.holes]
            out.append((b.lower_value, b.upper_value, rings))
        return out


class ContourPyFilled(Adapter):
    """ContourPy multi_filled over [-inf, l0, ..., lN, +inf]: the same bands,
    outer bands included."""
    def version(self):
        return contourpy.__version__

    def artifacts(self):
        return {"contourpy._contourpy": _module_file(contourpy._contourpy)}

    def prepare(self, z, levels, field_path):
        self.z = self.received(z)
        self.bounds = [-np.inf, *[float(v) for v in levels], np.inf]
        self.kw = {"name": self.p["name"], "fill_type": getattr(contourpy.FillType, self.p["fill_type"])}
        if self.p["name"] == "threaded":
            self.kw["thread_count"] = int(self.p["thread_count"])
            cc = int(self.p["chunk_count"])
            self.kw["chunk_count"] = (cc, cc)
        self.gen = contourpy.contour_generator(z=self.z, **self.kw) if self.p.get("reuse_generator") else None

    def activate(self):
        e = {k: (str(v) if k == "fill_type" else v) for k, v in self.kw.items()}
        e["reuse_generator"] = self.gen is not None
        return e

    def compute(self):
        if self.gen is not None:
            return self.gen.multi_filled(self.bounds)
        return contourpy.contour_generator(z=self.z, **self.kw).multi_filled(self.bounds)

    def collect(self, result):
        out = []
        for k, band in enumerate(result):
            lo, hi = self.bounds[k], self.bounds[k + 1]
            if self.p["fill_type"] == "OuterOffset":
                pts_l, off_l = band
                for pts, off in zip(pts_l, off_l):
                    out.append((lo, hi, [pts[off[i]:off[i + 1]] for i in range(len(off) - 1)]))
            else:                                   # ChunkCombinedOffsetOffset
                pts_c, off_c, outer_c = band
                for pts, off, outer in zip(pts_c, off_c, outer_c):
                    if pts is None:
                        continue
                    for p in range(len(outer) - 1):
                        rs = range(outer[p], outer[p + 1])
                        out.append((lo, hi, [pts[off[i]:off[i + 1]] for i in rs]))
        return out


# ---------------------------------------------------------------- VTK Flying Edges
from vtkmodules.vtkCommonCore import vtkSMPTools, vtkVersion  # noqa: E402
from vtkmodules.vtkCommonDataModel import vtkImageData  # noqa: E402
from vtkmodules.vtkFiltersCore import vtkFlyingEdges2D, vtkFlyingEdges3D, vtkStripper  # noqa: E402
from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy  # noqa: E402


def vtk_backends():
    have = []
    for b in ("TBB", "OpenMP", "STDThread"):
        vtkSMPTools.SetBackend(b)
        if vtkSMPTools.GetBackend() == b:
            have.append(b)
    vtkSMPTools.SetBackend("Sequential")
    return have


class _VtkBase(Adapter):
    def version(self):
        return vtkVersion.GetVTKVersion()

    def artifacts(self):
        import vtkmodules.vtkFiltersCore as m
        return {"vtkFiltersCore": _module_file(m)}

    def activate(self):
        be = self.p["backend"]
        vtkSMPTools.SetBackend(be)
        if be != "Sequential":
            vtkSMPTools.Initialize(int(self.p["threads"]))
        return {"backend": vtkSMPTools.GetBackend(),
                "threads": vtkSMPTools.GetEstimatedNumberOfThreads() if be != "Sequential" else 1}


class VtkFlyingEdges2D(_VtkBase):
    def prepare(self, z, levels, field_path):
        ny, nx = z.shape
        self.received(z)
        self.img = vtkImageData()
        self.img.SetDimensions(nx, ny, 1)
        self.arr = numpy_to_vtk(z.ravel(), deep=0)       # zero-copy wrap
        self.img.GetPointData().SetScalars(self.arr)
        self.fe = vtkFlyingEdges2D()
        self.fe.SetInputData(self.img)
        self.fe.SetNumberOfContours(len(levels))
        for i, v in enumerate(levels):
            self.fe.SetValue(i, float(v))
        self.fe.ComputeScalarsOff()
        self.stripper = vtkStripper()
        self.stripper.SetInputConnection(self.fe.GetOutputPort())
        self.stripper.JoinContiguousSegmentsOn()
        # default MaximumLength (1000 points) leaves long lines unjoined (less
        # work than the other tools: R10); join fully, as every other tool does
        self.stripper.SetMaximumLength(1 << 30)

    def compute(self):
        self.fe.Modified()
        self.stripper.Update()
        return self.stripper.GetOutput()

    def collect(self, out):
        pts = vtk_to_numpy(out.GetPoints().GetData())[:, :2]
        lines = out.GetLines()
        off = vtk_to_numpy(lines.GetOffsetsArray())
        con = vtk_to_numpy(lines.GetConnectivityArray())
        return [(None, pts[con[off[i]:off[i + 1]]]) for i in range(len(off) - 1)]


class VtkFlyingEdges3D(_VtkBase):
    def prepare(self, v, levels, field_path):
        nz, ny, nx = v.shape
        self.received(v)
        self.img = vtkImageData()
        self.img.SetDimensions(nx, ny, nz)
        self.arr = numpy_to_vtk(v.ravel(), deep=0)
        self.img.GetPointData().SetScalars(self.arr)
        self.fe = vtkFlyingEdges3D()
        self.fe.SetInputData(self.img)
        self.fe.SetNumberOfContours(len(levels))
        for i, lv in enumerate(levels):
            self.fe.SetValue(i, float(lv))
        self.fe.ComputeNormalsOff()
        self.fe.ComputeGradientsOff()
        self.fe.ComputeScalarsOff()

    def compute(self):
        self.fe.Modified()
        self.fe.Update()
        return self.fe.GetOutput()

    def collect(self, out):
        return geometry.mesh_summary(out.GetNumberOfCells(), out.GetNumberOfPoints())


# ---------------------------------------------------------------- scikit-image
import skimage  # noqa: E402
from skimage.measure import find_contours, marching_cubes  # noqa: E402


class SkimageFindContours(Adapter):
    def version(self):
        return skimage.__version__

    def prepare(self, z, levels, field_path):
        self.z, self.levels = self.received(z), [float(x) for x in levels]

    def compute(self):
        return [find_contours(self.z, lev) for lev in self.levels]

    def collect(self, result):
        return [(lev, c) for lev, cs in zip(self.levels, result) for c in cs]


class SkimageMarchingCubes(Adapter):
    def version(self):
        return skimage.__version__

    def prepare(self, v, levels, field_path):
        self.v, self.lev = self.received(v), float(levels[0])

    def compute(self):
        return marching_cubes(self.v, level=self.lev)

    def collect(self, r):
        return geometry.mesh_summary(len(r[1]), len(r[0]))


# ---------------------------------------------------------------- PyMCubes
import mcubes  # noqa: E402


class PyMCubes(Adapter):
    def version(self):
        return importlib.metadata.version("PyMCubes")

    def prepare(self, v, levels, field_path):
        self.v64 = self.received(np.ascontiguousarray(v, dtype=np.float64), source=v)   # required dtype; untimed
        self.lev = float(levels[0])

    def compute(self):
        return mcubes.marching_cubes(self.v64, self.lev)

    def collect(self, r):
        return geometry.mesh_summary(len(r[1]), len(r[0]))


# ---------------------------------------------------------------- native workers
class WorkerAdapter(Adapter):
    """Client for workers/*: line protocol LOAD / RUN / GEOM / INFO / QUIT."""
    native = True

    def _binary(self):
        raise NotImplementedError

    def _mode(self):
        raise NotImplementedError

    def artifacts(self):
        a = {"worker": self._binary()}
        lib = (getattr(self, "info", None) or {}).get("lib")
        if lib:                                  # the shared library the worker really loaded (R8)
            a["lib"] = os.path.realpath(lib)
        return a

    def prepare(self, z, levels, field_path):
        ny, nx = z.shape
        self.received(z)              # the worker reads these exact float64 bytes from field_path
        self.proc = subprocess.Popen([self._binary()], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=None, text=True, bufsize=1)
        lev = ",".join(repr(float(v)) for v in levels)
        r = self._cmd(f"LOAD {field_path} {nx} {ny} {lev} {self._mode()}")
        if r != "OK":
            raise RuntimeError(f"{self._binary()}: LOAD failed: {r}")
        self.info = json.loads(self._cmd("INFO"))
        # the field the worker actually loaded (R11): its checksums vs the array handed over
        w = np.ascontiguousarray(z, dtype="<f8").view("<u8").ravel()
        with np.errstate(over="ignore"):
            s1 = int(w.sum(dtype=np.uint64))
            s2 = int((w * np.arange(1, len(w) + 1, dtype=np.uint64)).sum(dtype=np.uint64))
        self._recv["worker_loaded_matches"] = (self.info.get("field_sum1") == str(s1)
                                               and self.info.get("field_sum2") == str(s2))

    def _cmd(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return self.proc.stdout.readline().strip()

    def _polygons(self):
        return self._mode() in ("polygonize", "isobands")

    def native_last(self):
        """Engine identity reported by the product itself after a sample (ours), else None."""
        return None

    def run_native(self, loops, markers=False):
        """→ {wall_ns, cpu_ns, majflt, min_ns, max_ns} for `loops` individually timed calls."""
        r = self._cmd(f"RUN {int(loops)} {1 if markers else 0}")
        f = r.split()
        if len(f) != 6 or f[0] != "TIME":
            raise RuntimeError(f"worker RUN failed: {r}")
        return dict(zip(("wall_ns", "cpu_ns", "majflt", "min_ns", "max_ns"), map(int, f[1:])))

    def collect(self, result=None):
        """The worker writes its last result (untimed) as binary polylines."""
        import struct
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".lines", dir=self.ctx.get("cache_dir"))
        os.close(fd)
        try:
            r = self._cmd(f"DUMP {path}")
            if r != "OK":
                raise RuntimeError(f"worker DUMP failed: {r}")
            b = open(path, "rb").read()
        finally:
            os.unlink(path)
        if self._polygons():                                  # polygons with holes per band
            n = struct.unpack_from("<q", b, 0)[0]
            o, out = 8, []
            for _ in range(n):
                lo, hi, nr = struct.unpack_from("<ddq", b, o)
                o += 24
                rings = []
                for _ in range(nr):
                    m = struct.unpack_from("<q", b, o)[0]
                    o += 8
                    rings.append(np.frombuffer(b, "<f8", 2 * m, o).reshape(m, 2))
                    o += 16 * m
                out.append((lo, hi, rings))
            return out
        nl = struct.unpack_from("<q", b, 0)[0]
        o, out = 8, []
        for _ in range(nl):
            lev, n = struct.unpack_from("<dq", b, o)
            o += 16
            out.append((lev, np.frombuffer(b, "<f8", 2 * n, o).reshape(n, 2)))
            o += 16 * n
        return out

    def close(self):
        try:
            self._cmd("QUIT")
        except Exception:  # noqa: BLE001
            pass
        self.proc.wait(timeout=30)


class GdalWorker(WorkerAdapter):
    def _binary(self):
        return os.path.join(self.ctx["bin_dir"], f"gdal_worker_{self.p['lib']}")

    def _mode(self):
        return self.p["api"]

    def version(self):
        return self.info["version"]

    def activate(self):
        return {"api": self.info["api"]}


class ContourRsWorker(WorkerAdapter):
    def _binary(self):
        return os.path.join(self.ctx["bin_dir"], "rs_worker")

    def _mode(self):
        return self.p["mode"]

    def version(self):
        lock = os.path.join(self.ctx["framework_dir"], "workers", "rs_worker", "Cargo.lock")
        lines = open(lock).read().split("\n")
        for i, l in enumerate(lines):
            if l.strip() == 'name = "contour"':
                return lines[i + 1].split('"')[1]
        return None


class IsosweepCWorker(WorkerAdapter):
    """libisosweep's C API (isosweep_contour) in a native worker: the library a
    C licensee links, measured C to C like GDAL's C API (design B10)."""
    def _binary(self):
        return os.path.join(self.ctx["bin_dir"], "isosweep_worker")

    def _mode(self):
        return f"{self.p['mode']}/{int(self.p['nthreads'])}"

    def _polygons(self):
        return self.p["mode"] == "bands"

    def version(self):
        return self.info["version"]

    def activate(self):
        return {"nthreads": int(self.p["nthreads"]), "api": self.info["api"]}

    def native_last(self):
        return json.loads(self._cmd("LAST"))

    def engine(self, result):
        return result.get("engine") if isinstance(result, dict) else None

    def threads_used(self, result):
        return result.get("threads_used") if isinstance(result, dict) else None


ADAPTER_CLASSES = {
    "isosweep_contour": IsosweepContour, "isosweep_isosurface": IsosweepIsosurface,
    "contourpy": ContourPy, "vtk_flying_edges_2d": VtkFlyingEdges2D, "vtk_flying_edges_3d": VtkFlyingEdges3D,
    "skimage_find_contours": SkimageFindContours, "skimage_marching_cubes": SkimageMarchingCubes,
    "pymcubes": PyMCubes, "gdal_worker": GdalWorker, "contour_rs_worker": ContourRsWorker,
    "isosweep_filled": IsosweepFilled, "contourpy_filled": ContourPyFilled,
    "gdal_worker_polygonize": GdalWorker, "contour_rs_bands_worker": ContourRsWorker,
    "isosweep_c_worker": IsosweepCWorker, "isosweep_c_bands_worker": IsosweepCWorker,
}
