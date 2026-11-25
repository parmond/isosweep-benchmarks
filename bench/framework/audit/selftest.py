#!/usr/bin/env python3
# Copyright 2026 Parmond AB
# SPDX-License-Identifier: Apache-2.0
"""Auditor self-test: known-bad cases the conformance audit MUST reject.

    selftest.py --baseline RESULTS_DIR [--live] [--out selftest.json]

A test passes only if its target check is ok on the (normalised) baseline AND
fails on the mutated copy — so it proves the check works, not merely that
something failed. --live adds strace tests (Linux): forbidden actions inside
the marker window must be caught, and clean/multi-threaded computes must not
be (no false positives). Exit 0 iff every test passes.
"""
import argparse
import copy
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile

FRAMEWORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, FRAMEWORK)
sys.path.insert(0, os.path.join(FRAMEWORK, "audit"))

from isobench import spec as specmod, static_check, syscheck  # noqa: E402
import conformance  # noqa: E402

KB = os.path.join(FRAMEWORK, "audit", "known_bad")
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append({"test": name, "ok": bool(ok), "detail": detail})
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail if not ok else ''}")


# ------------------------------------------------------------------ static
def static_tests(spec):
    sys.path.insert(0, KB)
    import kb_adapters as kb
    g = spec["global"]
    deny = g["static_denylist"]["python"]
    for cls, allow, name in [
        (kb.ReadsFileInCompute, ["np.frombuffer"], "static: open()/read() in compute is rejected"),
        (kb.PlotsInCompute, ["*.contour"], "static: plotting objects in compute are rejected"),
        (kb.CallsUnlistedFunction, ["isocurves.contour"], "static: non-allowlisted call is rejected"),
    ]:
        ok, bad = static_check.check(static_check.python_calls(cls), allow, deny)
        record(name, not ok, bad)
    ok, bad = static_check.check(static_check.python_calls(kb.HiddenIoViaAllowlistedHelper), ["self.kernel"], deny)
    record("static: allowlisted helper passes static (documents why the dynamic check exists)", ok, bad)
    ok, bad = static_check.check(static_check.native_calls(os.path.join(KB, "kb_native_io.c")), ["fclose"],
                                 g["static_denylist"]["native"])
    record("static: fopen/fread inside a native BEGIN_TIMED block is rejected", not ok, bad)
    calls = static_check.native_calls(os.path.join(KB, "kb_comment_only.c"))
    record("static: marker words in prose are not a timed block", calls is None, calls)
    for f, name in [("kb_clock_wide.c", "clock started before untimed-looking file I/O"),
                    ("kb_clock_extra.c", "an extra clock read defines the reported region")]:
        path = os.path.join(KB, f)
        calls_ok, _ = static_check.check(static_check.native_calls(path), ["GDALContourGenerateEx"],
                                         g["static_denylist"]["native"])
        ok, bad = static_check.check_native(path, ["GDALContourGenerateEx"], g["static_denylist"]["native"])
        record(f"static: {name} → rejected (the calls alone pass: adjacency is what catches it)",
               calls_ok and not ok, bad)
    for w in ("workers/gdal_worker.c", "workers/rs_worker/src/main.rs"):
        probs = static_check.native_clock_adjacency(os.path.join(os.path.dirname(KB), "..", w))
        record(f"static: {w} clock brackets exactly its timed blocks", not probs, probs)


# ------------------------------------------------------------------ tamper
def _load(d):
    j = lambda f: [json.loads(l) for l in open(os.path.join(d, f))] if os.path.exists(os.path.join(d, f)) else []  # noqa: E731
    return json.load(open(os.path.join(d, "run.json"))), j("measurements.jsonl"), j("geometry.jsonl"), \
        j("syscheck.jsonl"), j("sweeps.jsonl")


def _save(d, run, meas, geo, sysc, sweeps):
    json.dump(run, open(os.path.join(d, "run.json"), "w"))
    for name, rows in (("measurements", meas), ("geometry", geo), ("syscheck", sysc), ("sweeps", sweeps)):
        with open(os.path.join(d, f"{name}.jsonl"), "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")


def _normalise(run, meas, geo, sysc, sweeps):
    """Make the baseline pass statistics on any machine (laptop noise) so C09 tests are meaningful."""
    for m in meas:
        med = sorted(m["samples_ms"])[len(m["samples_ms"]) // 2]
        m["samples_ms"] = [med * (1 + 0.001 * ((i % 3) - 1)) for i in range(len(m["samples_ms"]))]
    return run, meas, geo, sysc, sweeps


def tamper_tests(spec, baseline):
    tmp = tempfile.mkdtemp(prefix="isobench-selftest-")
    base = os.path.join(tmp, "base")
    shutil.copytree(baseline, base)
    st = _normalise(*_load(base))
    _save(base, *st)
    a0 = conformance.audit(base, spec=spec)["checks"]

    def case(name, cid, mutate):
        d = os.path.join(tmp, name.replace(" ", "_").replace("/", "_")[:60])
        shutil.copytree(base, d)
        run, meas, geo, sysc, sweeps = copy.deepcopy(_load(d))
        try:
            mutate(run, meas, geo, sysc, sweeps, d)
        except StopIteration:      # the baseline has no record of that kind (e.g. run without isosweep)
            print(f"  SKIP  tamper[{cid}]: {name}  (no matching record in the baseline)")
            return
        _save(d, run, meas, geo, sysc, sweeps)
        a1 = conformance.audit(d, spec=spec)["checks"]
        ok = a0[cid]["ok"] and not a1[cid]["ok"]
        record(f"tamper[{cid}]: {name}", ok, f"baseline_ok={a0[cid]['ok']} mutated_ok={a1[cid]['ok']}")

    ours = lambda meas: next(m for m in meas if m.get("expected_engine"))  # noqa: E731

    def m_engine(run, meas, geo, sysc, sweeps, d):
        ours(meas)["observed_engines"] = ["sweep_stream"]
    case("our configuration ran a different engine", "C05_engine", m_engine)

    def m_engine_mixed(run, meas, geo, sysc, sweeps, d):
        ours(meas)["observed_engines"] = ["sweep4", "sweep_stream"]
    case("engine changed between samples", "C05_engine", m_engine_mixed)

    def m_version(run, meas, geo, sysc, sweeps, d):
        next(m for m in meas if m["adapter"] == "contourpy")["version"] = "1.3.3"
    case("competitor version differs from pin", "C04_versions", m_version)

    def m_polygons(run, meas, geo, sysc, sweeps, d):
        x = next(x for x in geo if x.get("report_tool") == "gdal_master")
        x["summary"]["total_length"] *= 1.6
    case("unwaived tool output 60% longer (polygon-like)", "C08_output", m_polygons)

    def m_segments(run, meas, geo, sysc, sweeps, d):
        x = next(x for x in geo if x.get("report_tool") == "skimage")
        x["summary"]["features"] *= 60
        x["summary"]["mean_points_per_feature"] = 2.0
    case("unassembled 2-point segments (W2)", "C08_output", m_segments)

    def m_fewer_vertices(run, meas, geo, sysc, sweeps, d):
        x = next(x for x in geo if x.get("report_tool") == "contourpy_serial")
        x["summary"]["points"] = int(x["summary"]["points"] * 0.98)
    case("2% fewer crossing vertices (less work, W1)", "C08_output", m_fewer_vertices)

    def m_unjoined(run, meas, geo, sysc, sweeps, d):
        x = next(x for x in geo if x.get("report_tool") == "contourpy_serial")
        x["summary"]["unjoined_ends"] = 4
    case("two contours left unjoined in the interior (W2, exact)", "C08_output", m_unjoined)

    def m_waived_beyond(run, meas, geo, sysc, sweeps, d):
        x = next(x for x in geo if x.get("report_tool") == "vtk_fe2d_1t")
        x["summary"]["unjoined_ends"] = int(2 * 0.9 * x["summary"]["features"])
    case("waived difference beyond its bound (VTK unjoined ends 90% > 50%)", "C08_output", m_waived_beyond)

    def _band_geo(geo, tool):
        return next(x for x in geo if x.get("report_tool") == tool)

    def m_band_hole_missing(run, meas, geo, sysc, sweeps, d):
        x = _band_geo(geo, "contourpy_filled_serial")       # a hole not subtracted: band area too large
        k = max(x["summary"]["per_band"], key=lambda k: x["summary"]["per_band"][k]["holes"])
        x["summary"]["per_band"][k]["area"] += 50.0
        x["summary"]["total_area"] += 50.0
    case("bands: a hole left unassigned (area > domain, B1)", "C08_output", m_band_hole_missing)

    def m_band_missing(run, meas, geo, sysc, sweeps, d):
        x = _band_geo(geo, "gdal_bands_3.13")                # outer bands not produced (less work)
        pb = x["summary"]["per_band"]
        last = str(max(int(k) for k in pb))
        x["summary"]["total_area"] -= pb[last]["area"]
        del pb[last]
        x["summary"]["bands"] -= 1
    case("bands: an outer band missing (B1)", "C08_output", m_band_missing)

    def m_band_points(run, meas, geo, sysc, sweeps, d):
        x = _band_geo(geo, "contourpy_filled_serial")
        k = max(x["summary"]["per_band"], key=lambda k: x["summary"]["per_band"][k]["points"])
        x["summary"]["per_band"][k]["points"] = int(x["summary"]["per_band"][k]["points"] * 0.9)
    case("bands: 10% fewer crossing vertices in one band (B1)", "C08_output", m_band_points)

    def m_band_clip(run, meas, geo, sysc, sweeps, d):
        x = _band_geo(geo, "gdal_bands_3.13")
        x["summary"]["clipped_area"] = 0.05 * x["summary"]["total_area"]
    case("bands: GDAL border extension above its bound (W5)", "C16_work", m_band_clip)

    def m_offedge(run, meas, geo, sysc, sweeps, d):
        x = next(x for x in geo if x.get("report_tool") == "skimage")
        x["summary"]["accuracy"]["off_edge_vertices"] = 3
    case("vertices off the cell edges (wrong frame / wrong geometry, W3)", "C08_output", m_offedge)

    def m_imprecise(run, meas, geo, sysc, sweeps, d):
        x = next(x for x in geo if x.get("report_tool") == "contourpy_serial")
        x["summary"]["accuracy"]["max_err_cells"] = 1e-3
    case("float64 tool with float32-sized vertex errors (W3)", "C08_output", m_imprecise)

    def m_majflt(run, meas, geo, sysc, sweeps, d):
        meas[0]["majflt"][0] = 3
    case("major page faults inside a sample (file-backed I/O)", "C15_resources", m_majflt)

    def m_cpu(run, meas, geo, sysc, sweeps, d):
        m = next(m for m in meas if m["config_id"] == "isosweep_1t/binding_1t")
        m["cpu_ms"] = [v * 2.0 for v in m["samples_ms"]]
    case("a 1-thread configuration used two cores (W4)", "C15_resources", m_cpu)

    def m_engine_threads(run, meas, geo, sysc, sweeps, d):
        m = next(m for m in meas if m["config_id"] == "isosweep_mt/binding_mt")
        m["observed_threads"] = [1]
    case("engine ran fewer threads than requested (R7)", "C10_effective", m_engine_threads)

    def m_dtype(run, meas, geo, sysc, sweeps, d):
        m = next(m for m in meas if m["adapter"] == "isosweep_contour")
        m["work"]["dtype"] = "float64"
    case("tool received a different input dtype than declared (W5)", "C16_work", m_dtype)

    def m_clip(run, meas, geo, sysc, sweeps, d):
        x = next(x for x in geo if x.get("report_tool") == "gdal_3.13")
        x["summary"]["clipped_length"] = 0.05 * x["summary"]["total_length"]
    case("GDAL border extension above its bound (W5)", "C16_work", m_clip)

    # isosweep never votes: corrupting our output must fail only us
    has_ours = any(m.get("expected_engine") for m in _load(base)[1])
    has_dataset = any(m["data"].get("dataset_sha256") for m in _load(base)[1])
    d = os.path.join(tmp, "ours_never_votes")
    shutil.copytree(base, d)
    run, meas, geo, sysc, sweeps = copy.deepcopy(_load(d))
    for x in geo:
        if x.get("report_tool", "").startswith("isosweep") and "total_length" in x.get("summary", {}):
            x["summary"]["total_length"] *= 1.6
            for v in x["summary"]["per_level"].values():
                v["length"] *= 1.6
    _save(d, run, meas, geo, sysc, sweeps)
    f0 = conformance.audit(base, spec=spec)["geometry_findings"]
    f1 = conformance.audit(d, spec=spec)["geometry_findings"]
    comp = lambda f: sorted((b, x["tool"], x["check"], x["ok"]) for b, xs in f.items()  # noqa: E731
                            for x in xs if not str(x["tool"]).startswith("isosweep"))
    ours_fail = any(not x["ok"] for xs in f1.values() for x in xs if str(x["tool"]).startswith("isosweep"))
    if has_ours:
        record("leave-one-out: corrupting isosweep's output fails isosweep and changes no competitor finding",
               ours_fail and comp(f0) == comp(f1), f"ours_fail={ours_fail}")
    else:
        print("  SKIP  leave-one-out: corrupting isosweep's output  (no isosweep records in the baseline)")

    def m_missing(run, meas, geo, sysc, sweeps, d):
        meas.pop(0)
    case("a configuration's measurement is missing", "C03_completeness", m_missing)

    def m_noise(run, meas, geo, sysc, sweeps, d):
        # the CV gate applies to reference runs: make the copy a full-size reference run
        n = spec["global"]["stats"]["rounds"]
        run["profile"] = "reference"
        for m in meas:
            m["samples_ms"] = [m["samples_ms"][i % len(m["samples_ms"])] for i in range(n)]
        for b, o in run["round_orders"].items():
            run["round_orders"][b] = [o[i % len(o)] for i in range(n)]
        meas[0]["samples_ms"] = [v * (1 + 0.4 * (i % 2)) for i, v in enumerate(meas[0]["samples_ms"])]
    # 2026-11-22 policy: variation is disclosed, not failed (global.yaml cv_gate_on: [])
    d_ = os.path.join(tmp, "noise_disclosed")
    shutil.copytree(base, d_)
    st_ = copy.deepcopy(_load(d_))
    m_noise(*st_, d_)
    _save(d_, *st_)
    c9 = conformance.audit(d_, spec=spec)["checks"]["C09_statistics"]
    noisy = st_[1][0]
    record("variation: noisy samples on a reference run are disclosed (not failed)",
           c9["ok"] and any(v[0] == noisy["benchmark"] and v[1] == noisy["config_id"] for v in c9["variability"]),
           f"ok={c9['ok']} variability={c9.get('variability')}")

    def m_order(run, meas, geo, sysc, sweeps, d):
        for b, o in run["round_orders"].items():
            run["round_orders"][b] = [sorted(o[0])] * len(o)
    case("rounds not randomized", "C09_statistics", m_order)

    def m_extra(run, meas, geo, sysc, sweeps, d):
        meas[0]["samples_ms"] = meas[0]["samples_ms"] + meas[0]["samples_ms"][:2]
    case("more samples than the fixed count (optional stopping)", "C09_statistics", m_extra)

    def m_fewer(run, meas, geo, sysc, sweeps, d):
        meas[0]["samples_ms"] = meas[0]["samples_ms"][:1]
    case("fewer rounds than specified", "C09_statistics", m_fewer)

    def _as_preflight_env(run, extra_names):
        # a preflight-class environment that meets every other requirement
        run["profile"] = "preflight"
        e = run["environment"]
        e["instance_type"], e["strace"] = "c7i.xlarge", "/usr/bin/strace"
        e["process_env_names"] = ["HOME", "LANG", "PATH", "TIF2F64"] + extra_names
        run["environment_checks"] = [["strace", True, "/usr/bin/strace"]]
    ctl = os.path.join(tmp, "env_control")
    shutil.copytree(base, ctl)
    st = copy.deepcopy(_load(ctl))
    _as_preflight_env(st[0], [])
    _save(ctl, *st)
    a_ctl = conformance.audit(ctl, spec=spec)["checks"]["C02_environment"]
    record("control: clean preflight environment passes C02", a_ctl["ok"], str(a_ctl.get("failed")))

    def m_env_var(run, meas, geo, sysc, sweeps, d):
        _as_preflight_env(run, ["OMP_NUM_THREADS"])
        run["environment_checks"] = [["process_env_clean", True, "harness said clean"]]
    case("thread-count variable set on a preflight (harness verdict forged)", "C02_environment", m_env_var)

    def m_env(run, meas, geo, sysc, sweeps, d):
        run["environment_checks"] = (run.get("environment_checks") or []) + [["turbo_disabled", False, "False"]]
    case("environment requirement not met", "C02_environment", m_env)

    def m_spec(run, meas, geo, sysc, sweeps, d):
        run["spec_hash"] = "0" * 64
    case("run used a different spec", "C01_spec_hash", m_spec)

    def m_snapshot(run, meas, geo, sysc, sweeps, d):
        run["source_tree_hash"] = "f" * 64
    case("recorded source tree does not match snapshot", "C11_provenance", m_snapshot)

    def m_dataset(run, meas, geo, sysc, sweeps, d):
        for m in meas:
            if m["data"].get("dataset_sha256"):
                m["data"]["dataset_sha256"] = "e" * 64
    if has_dataset:
        case("real dataset hash differs from pin", "C12_data", m_dataset)
    else:
        print("  SKIP  tamper[C12_data]: real dataset hash differs from pin  (no real-dataset benchmark in the baseline)")

    def m_field(run, meas, geo, sysc, sweeps, d):
        meas[1]["data"]["field_sha256"] = "d" * 64
    case("one configuration saw a different field", "C12_data", m_field)

    def m_threads(run, meas, geo, sysc, sweeps, d):
        m = next(m for m in meas if m["params"].get("backend") not in (None, "Sequential"))
        m["effective"]["threads"] = 1
    case("effective threads differ from requested", "C10_effective", m_threads)

    def m_api(run, meas, geo, sysc, sweeps, d):
        m = next(m for m in meas if m["params"].get("api") == "callback")
        m["effective"]["api"] = "GDALContourGenerateEx (in-memory layer)"
    case("GDAL ran a different API than recorded", "C10_effective", m_api)

    def m_sweep(run, meas, geo, sysc, sweeps, d):
        s = sweeps[0]
        worst = max(s["trials"], key=lambda t: t["median_ms"])["params"]
        s["chosen"] = worst
        for m in meas:
            if (m["benchmark"], m["config_id"]) == (s["benchmark"], s["config_id"]):
                m["params"] = worst
    case("swept configuration is not the fastest candidate", "C14_sweeps", m_sweep)

    def m_subset(run, meas, geo, sysc, sweeps, d):
        # a reference run of a hand-picked subset (R5): otherwise complete
        # (syscheck records present), so only the benchmark-set rule can fire
        run["profile"], run["syscheck_enabled"] = "reference", True
        drop = run["benchmarks"][-1]
        run["benchmarks"] = run["benchmarks"][:-1]
        meas[:] = [m for m in meas if m["benchmark"] != drop]
        geo[:] = [x for x in geo if x["benchmark"] != drop]
        sweeps[:] = [x for x in sweeps if x["benchmark"] != drop]
        sysc.clear()
        for m in meas:
            sysc.append({"benchmark": m["benchmark"], "config_id": m["config_id"], "ok": True,
                         "window_found": True, "violations": [], "n_violations": 0})
    case("reference run of a benchmark subset", "C03_completeness", m_subset)

    def m_sweep_incomplete(run, meas, geo, sysc, sweeps, d):
        s = next(s for s in sweeps if len(s["trials"]) > 1)
        s["trials"] = [t for t in s["trials"] if t["params"] == s["chosen"]] + \
            [t for t in s["trials"] if t["params"] != s["chosen"]][1:]
    case("a sweep skipped one candidate (fastest of the rest still chosen)", "C14_sweeps", m_sweep_incomplete)

    def m_worker_field(run, meas, geo, sysc, sweeps, d):
        m = next(m for m in meas if m["adapter"] == "gdal_worker")
        m["work"]["worker_loaded_matches"] = False
    case("native worker loaded a different field than handed over", "C12_data", m_worker_field)

    def m_transposed(run, meas, geo, sysc, sweeps, d):
        m = next(m for m in meas if m["adapter"] == "skimage_find_contours")
        m["work"]["sha256"] = "0" * 64          # e.g. the adapter transposed z before compute
    case("one float64 tool was handed a different array than the others", "C12_data", m_transposed)

    def m_no_launch(run, meas, geo, sysc, sweeps, d):
        run["profile"], run["launch_provenance"] = "preflight", None
    case("preflight without the orchestrator's launch provenance", "C11_provenance", m_no_launch)

    def m_reference(run, meas, geo, sysc, sweeps, d):
        run["profile"] = "reference"
    case("reference run without a matching preflight", "C13_preflight", m_reference)

    CLEAN = ('100 write(2<pipe:[1]>, "ISOBENCH_T0\\n", 12) = 12\n'
             '100 futex(0x7f00, FUTEX_WAKE_PRIVATE, 1) = 0\n'
             '101 mmap(NULL, 8392704, PROT_NONE, MAP_PRIVATE|MAP_ANONYMOUS|MAP_STACK, -1, 0) = 0x7f01\n'
             '100 write(2<pipe:[1]>, "ISOBENCH_T1\\n", 12) = 12\n')
    OPEN = '100 openat(AT_FDCWD, "/data/field.f64", O_RDONLY) = 3</data/field.f64>\n'

    def _logs(run, meas, sysc, d, bad_body=None, recorded_ok=None):
        import gzip
        import hashlib
        run["profile"], run["syscheck_enabled"] = "preflight", True
        sysc.clear()
        os.makedirs(os.path.join(d, "syscheck"), exist_ok=True)
        for k, m in enumerate(meas):
            body = CLEAN if (k or bad_body is None) else CLEAN.replace("100 write(2<pipe:[1]>, \"ISOBENCH_T1",
                                                                       bad_body + "100 write(2<pipe:[1]>, \"ISOBENCH_T1")
            rel = os.path.join("syscheck", f"t{k}.strace.gz")
            with gzip.open(os.path.join(d, rel), "wt") as fh:
                fh.write(body)
            sha = hashlib.sha256(open(os.path.join(d, rel), "rb").read()).hexdigest()
            ok = True if (k or recorded_ok is None) else recorded_ok
            sysc.append({"benchmark": m["benchmark"], "config_id": m["config_id"], "ok": ok,
                         "window_found": True, "violations": [], "n_violations": 0, "log": rel, "log_sha256": sha})

    ctl = os.path.join(tmp, "sys_control")
    shutil.copytree(base, ctl)
    st = copy.deepcopy(_load(ctl))
    _logs(st[0], st[1], st[3], ctl)
    _save(ctl, *st)
    a_ctl = conformance.audit(ctl, spec=spec)["checks"]["C07_syscalls"]
    record("control: clean kept strace logs pass C07 (re-analysed)", a_ctl["ok"], str(a_ctl.get("failures"))[:200])

    def m_sys(run, meas, geo, sysc, sweeps, d):
        _logs(run, meas, sysc, d, bad_body=OPEN, recorded_ok=False)
    case("syscall violation recorded for one configuration", "C07_syscalls", m_sys)

    def m_sys_forged(run, meas, geo, sysc, sweeps, d):
        _logs(run, meas, sysc, d, bad_body=OPEN, recorded_ok=True)
    case("harness recorded 'clean' but the kept log shows a file open", "C07_syscalls", m_sys_forged)

    def m_sys_unlisted(run, meas, geo, sysc, sweeps, d):
        _logs(run, meas, sysc, d, bad_body="100 io_uring_enter(4, 1, 0, 0, NULL, 8) = 1\n", recorded_ok=True)
    case("a syscall not on the allowlist (io_uring) inside the window", "C07_syscalls", m_sys_unlisted)

    def m_sys_tampered_log(run, meas, geo, sysc, sweeps, d):
        _logs(run, meas, sysc, d)
        open(os.path.join(d, sysc[0]["log"]), "ab").write(b"\0")
    case("strace log edited after the run", "C07_syscalls", m_sys_tampered_log)

    def m_sys_allowed_read(run, meas, geo, sysc, sweeps, d):
        _logs(run, meas, sysc, d, bad_body='100 openat(AT_FDCWD, "/sys/devices/system/cpu/online", '
                                           'O_WRONLY) = 3</sys/devices/system/cpu/online>\n', recorded_ok=True)
    case("an allowlisted sysfs path opened for writing", "C07_syscalls", m_sys_allowed_read)

    def m_code(run, meas, geo, sysc, sweeps, d):
        """Snapshot whose adapter compute() opens a file, with self-consistent recorded hashes:
        provenance passes, the re-derived static check must not."""
        import tarfile
        from isobench import provenance
        x = os.path.join(d, "_x")
        with tarfile.open(os.path.join(d, "source_snapshot.tar.gz")) as t:
            t.extractall(x, filter="data")
            names = sorted(m.name for m in t.getmembers() if m.isfile())
        ap = os.path.join(x, "bench", "framework", "isobench", "adapters.py")
        line = "        return isocurves.contour(self.z, self.levels, nthreads=self.nthreads, dtype=self.coord_dtype)"
        src0 = open(ap).read()
        assert src0.count(line) == 1, "self-test mutation anchor not found in adapters.py"
        src = src0.replace(line, "        open('/tmp/x').read()\n" + line, 1)
        open(ap, "w").write(src)
        run["snapshot_sha256"] = provenance.snapshot(os.path.join(d, "source_snapshot.tar.gz"), root=x, files=names)
        run["source_tree_hash"] = provenance.tree_hash(root=x, files=names)
        run["launch_provenance"] = None
        shutil.rmtree(x)
    if has_ours:
        case("snapshot code reads a file in compute (hashes self-consistent)", "C06_static", m_code)
    else:
        print("  SKIP  tamper[C06_static]: snapshot code reads a file in compute  (no isosweep records in the baseline)")

    # R13: reused records must carry the key this run's code, data, method and
    # machine would produce, and the anchors must have matched the store
    def true_key(run, m, d):
        from isobench import store as storemod
        x = tempfile.mkdtemp(prefix="isobench-key-")
        with tarfile.open(os.path.join(d, "source_snapshot.tar.gz")) as tar:
            tar.extractall(x, filter="data")
        cfg = next(c for c in specmod.configs_for(spec, m["benchmark"]) if c["config_id"] == m["config_id"])
        shas = {n.split("::", 1)[1]: v["sha256"] for n, v in (run.get("artifacts") or {}).items()
                if n.startswith(m["config_id"] + "::")}
        tk, _, _ = storemod.keys(spec, m["benchmark"], cfg, m["data"]["field_sha256"], shas, m["version"],
                                 run.get("environment") or {}, run.get("toolchain"), run.get("sweep_inputs"), root=x)
        shutil.rmtree(x)
        return tk

    # the anchors of a spec whose anchor benchmarks this baseline contains (a
    # missing anchor would make every reuse case fail for that reason alone)
    from isobench import store as storemod_
    have = {m["benchmark"] for m in _load(base)[1]}
    spec_r = copy.deepcopy(spec)
    ru_ = spec_r["global"].setdefault("reuse", {})
    ru_["anchor_benchmarks"] = [b for b in ru_.get("anchor_benchmarks", []) if b in have] or sorted(have)[:1]

    def anchors_for(run, meas, drift, odd=None):
        run["anchors"] = []
        for b, cid in storemod_.anchor_configs(spec_r):
            m = next((m for m in meas if m["benchmark"] == b and m["config_id"] == cid), None)
            med = m["summary"]["median"] if m else 1.0
            dr = odd if odd is not None and not run["anchors"] else drift
            run["anchors"].append({"benchmark": b, "config_id": cid, "median_ms": med,
                                   "stored_median_ms": med / (1 + dr), "drift": dr})

    def reuse_case(name, mutate, expect_ok):
        d = os.path.join(tmp, "r17_" + name.replace(" ", "_").replace("/", "_")[:50])
        shutil.copytree(base, d)
        run, meas, geo, sysc, sweeps = copy.deepcopy(_load(d))
        m = next((m for m in meas if not m.get("expected_engine")
                  and (m["benchmark"], m["config_id"]) not in set(storemod_.anchor_configs(spec_r))), None)
        if m is None:
            print(f"  SKIP  reuse[C17_reuse]: {name}  (no non-anchor record in the baseline)")
            return
        mutate(run, meas, m, d)
        _save(d, run, meas, geo, sysc, sweeps)
        c = conformance.audit(d, spec=spec_r)["checks"]["C17_reuse"]
        record(f"reuse[C17_reuse]: {name}", c["ok"] == expect_ok,
               f"ok={c['ok']} failures={c.get('failures', [])[:3]}")

    def r_ok(run, meas, m, d):
        m["reused"] = {"timing_key": true_key(run, m, d), "source": {"run_id": "elsewhere"}}
        anchors_for(run, meas, 0.0)
    reuse_case("correct key, anchors as stored → passes", r_ok, True)

    def r_one_tool(run, meas, m, d):
        m["reused"] = {"timing_key": true_key(run, m, d), "source": {"run_id": "elsewhere"}}
        anchors_for(run, meas, 0.0, odd=0.08)
    reuse_case("one anchor 8% off, median as stored → passes (that tool's variation)", r_one_tool, True)

    def r_key(run, meas, m, d):
        m["reused"] = {"timing_key": "0" * 64, "source": {"run_id": "elsewhere"}}
        anchors_for(run, meas, 0.0)
    reuse_case("a reused record whose key this run would not produce → fails", r_key, False)

    def r_drift(run, meas, m, d):
        m["reused"] = {"timing_key": true_key(run, m, d), "source": {"run_id": "elsewhere"}}
        anchors_for(run, meas, 0.08)
    reuse_case("every anchor 8% off the store (another machine) → fails", r_drift, False)

    def r_none(run, meas, m, d):
        m["reused"] = {"timing_key": true_key(run, m, d), "source": {"run_id": "elsewhere"}}
        run["anchors"] = []
    reuse_case("reuse without the anchors re-measured → fails", r_none, False)
    shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ build once (R8)
def build_once_tests(baseline):
    run = json.load(open(os.path.join(baseline, "run.json")))
    run.setdefault("toolchain", {"machine": "x86_64", "os_release": "Ubuntu 24.04", "libc": "glibc-2.39",
                                 "python": "3.12.3", "pip_freeze": ["numpy==2.4.4"]})
    for k in ("machine", "os_release", "libc", "python", "pip_freeze"):
        run["toolchain"][k] = run["toolchain"].get(k) or "x"
    files = {"bin/gdal_worker_master": "a" * 64, "requirements.lock": "b" * 64}
    pre = copy.deepcopy(run)
    pre["artifact_manifest"] = {"mode": "built", "bundle_sha256": "c" * 64, "files": files}
    ref = copy.deepcopy(run)
    ref["artifact_manifest"] = {"mode": "installed", "bundle_sha256": "c" * 64, "files": dict(files)}
    record("build-once: identical preflight/reference → no differences",
           conformance.build_once_diffs(pre, ref) == [], str(conformance.build_once_diffs(pre, ref)[:3]))

    def flips(name, fn):
        r = copy.deepcopy(ref)
        fn(r)
        d = conformance.build_once_diffs(pre, r)
        record(f"build-once: {name} → detected", bool(d), str(d[:2]))
    flips("reference rebuilt instead of installing", lambda r: r["artifact_manifest"].update(mode="built"))
    flips("different bundle", lambda r: r["artifact_manifest"].update(bundle_sha256="d" * 64))
    flips("one bundle file changed after install",
          lambda r: r["artifact_manifest"]["files"].update({"bin/gdal_worker_master": "e" * 64}))
    flips("different pip freeze", lambda r: r["toolchain"].update(pip_freeze=["numpy==2.4.5"]))
    flips("different architecture", lambda r: r["toolchain"].update(machine="aarch64"))
    if run.get("artifacts"):
        def other_lib(r):
            k = next(iter(r["artifacts"]))
            r["artifacts"][k] = dict(r["artifacts"][k], sha256="f" * 64)
        flips("a tool loaded a different library", other_lib)
    flips("no manifest at all", lambda r: r.pop("artifact_manifest"))


# ------------------------------------------------------------------ gates (R11)
def gate_export_tests(spec, baseline):
    import hashlib
    from isobench import provenance
    root = tempfile.mkdtemp(prefix="isobench-gate-")
    d = os.path.join(root, "pre", "intel-test", "20261120")
    os.makedirs(d)
    bundle = os.path.join(d, "artifacts.tar.gz")
    open(bundle, "wb").write(b"bundle bytes")
    bsha = hashlib.sha256(b"bundle bytes").hexdigest()
    run = {"environment": {"instance_type": "c7i.xlarge"},
           "artifact_manifest": {"mode": "built", "bundle_sha256": bsha, "files": {}}}
    att = {"verdict": "PASS", "profile": "preflight", "auditor_selftest": {"ok": True, "live": True},
           "source_tree_hash": provenance.tree_hash(), "spec_hash": spec["hash"]}

    def gate(inst, att_=None, run_=None, bundle_bytes=None):
        json.dump(att_ or att, open(os.path.join(d, "attestation.json"), "w"))
        json.dump(run_ or run, open(os.path.join(d, "run.json"), "w"))
        open(bundle, "wb").write(bundle_bytes or b"bundle bytes")
        r = subprocess.run([sys.executable, os.path.join(FRAMEWORK, "gate.py"), "--instance-type", inst,
                            "--preflight-root", os.path.join(root, "pre")], capture_output=True, text=True)
        return r.returncode == 0
    record("gate: matching preflight opens the gate", gate("c7i.metal-24xl"))
    # R13: with --require-validation, no passing local validation keeps it closed
    json.dump(att, open(os.path.join(d, "attestation.json"), "w"))
    json.dump(run, open(os.path.join(d, "run.json"), "w"))
    open(bundle, "wb").write(b"bundle bytes")
    r_ = subprocess.run([sys.executable, os.path.join(FRAMEWORK, "gate.py"), "--instance-type", "c7i.metal-24xl",
                         "--preflight-root", os.path.join(root, "pre"), "--require-validation",
                         "--validation-dir", os.path.join(root, "no-validation")], capture_output=True, text=True)
    record("gate: no passing local validation → closed", r_.returncode != 0 and "validation" in r_.stderr)
    for name, kw in [("preflight on another instance family", {"inst": "c7g.metal"}),
                     ("auditor self-test not live", {"att_": dict(att, auditor_selftest={"ok": True, "live": False})}),
                     ("preflight of another spec", {"att_": dict(att, spec_hash="0" * 64)}),
                     ("preflight of another source tree", {"att_": dict(att, source_tree_hash="0" * 64)}),
                     ("preflight verdict FAIL", {"att_": dict(att, verdict="FAIL")}),
                     ("artifact bundle changed after the preflight", {"bundle_bytes": b"other bytes"}),
                     ("preflight that installed instead of building",
                      {"run_": dict(run, artifact_manifest=dict(run["artifact_manifest"], mode="installed"))})]:
        kw.setdefault("inst", "c7i.metal-24xl")
        record(f"gate: {name} → closed", not gate(**kw))
    shutil.rmtree(root, ignore_errors=True)
    # export refuses anything but a PASSing reference attestation of the current spec.
    # export_site.py is post-measurement tooling, not deployed to nodes (provenance
    # TREE_EXCLUDE): tested where it exists (locally), recorded as skipped on a node.
    exporter = os.path.join(FRAMEWORK, "export_site.py")
    if os.path.exists(exporter):
        r = subprocess.run([sys.executable, exporter, baseline], capture_output=True, text=True)
        record("export: a non-reference run is refused", r.returncode != 0 and "REFUSED" in (r.stderr + r.stdout),
               (r.stderr or r.stdout)[-200:])
    else:
        print("  SKIP  export: a non-reference run is refused  (export_site.py not deployed here)")


# ------------------------------------------------------------------ spec validation
def spec_tests():
    import yaml
    def mutate(name, fn):
        d = tempfile.mkdtemp(prefix="isobench-spec-")
        shutil.copytree(specmod.SPEC_DIR, os.path.join(d, "spec"))
        sd = os.path.join(d, "spec")
        t = yaml.safe_load(open(os.path.join(sd, "tools.yaml")))
        fn(t, sd)
        yaml.safe_dump(t, open(os.path.join(sd, "tools.yaml"), "w"), sort_keys=False)
        try:
            specmod.load(sd)
            record(f"spec: {name} is rejected", False, "loaded without error")
        except specmod.SpecError as e:
            record(f"spec: {name} is rejected", True, str(e))
        shutil.rmtree(d, ignore_errors=True)
    mutate("unknown key in a report tool", lambda t, sd: t["report_tools"]["skimage"].update(typo=1))
    mutate("our variant without an expected engine",
           lambda t, sd: t["report_tools"]["isosweep_1t"]["variants"][0].pop("engine"))
    mutate("unbounded waiver",
           lambda t, sd: t["report_tools"]["contourpy_threaded"]["waivers"][0].pop("max_rel"))
    mutate("waiver covering a difference that favours isosweep",
           lambda t, sd: t["report_tools"]["skimage"].update(waivers=[{
               "check": "W1_total_length", "direction": "favours_isosweep", "max_rel": 0.1,
               "justification": "x"}]))
    mutate("line tool without a declared work inventory", lambda t, sd: t["adapters"]["contourpy"].pop("work"))
    mutate("difference favouring isosweep without a bound",
           lambda t, sd: t["adapters"]["gdal_worker"]["work"]["differences"][0].pop("bound"))
    mutate("unknown coordinate frame", lambda t, sd: t["adapters"]["skimage_find_contours"].update(coords="rc"))
    mutate("polygon-class tool in a line benchmark",
           lambda t, sd: t["adapters"]["contourpy"].update(output_class="polygons.filled"))
    mutate("competitor without configuration evidence", lambda t, sd: t["adapters"]["contourpy"].pop("evidence"))


# ------------------------------------------------------------------ live (strace)
def live_tests(spec):
    if platform.system() != "Linux" or not syscheck.available():
        record("live strace tests", False, "require Linux with strace")
        return False
    g = spec["global"]
    tmpd = tempfile.mkdtemp(prefix="isobench-live-")
    data = os.path.join(tmpd, "data.bin")
    open(data, "wb").write(os.urandom(4096))
    expect = {"clean": True, "threads": True, "file_open_read": False, "read_preopened": False,
              "subprocess": False, "socket": False, "mmap_file": False, "stat": False}
    for case_name, want_ok in expect.items():
        log = os.path.join(tmpd, f"{case_name}.strace")
        syscheck.run_traced([sys.executable, os.path.join(KB, "kb_dynamic.py"), case_name, data], log, g)
        r = syscheck.analyze(log, g)
        ok = r["window_found"] and (r["ok"] == want_ok)
        record(f"live: {case_name} → {'clean' if want_ok else 'violation'}", ok,
               f"window={r['window_found']} n_violations={r['n_violations']} {r['violations'][:2]}")
    exe = os.path.join(tmpd, "kb_native_io")
    subprocess.run(["cc", "-O0", "-o", exe, os.path.join(KB, "kb_native_io.c")], check=True)
    log = os.path.join(tmpd, "native.strace")
    syscheck.run_traced([exe, data], log, g)
    r = syscheck.analyze(log, g)
    record("live: native fopen/fread between markers → violation", r["window_found"] and not r["ok"],
           f"n_violations={r['n_violations']}")
    shutil.rmtree(tmpd, ignore_errors=True)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    spec = specmod.load()
    print("== spec validation"); spec_tests()
    print("== static fixtures"); static_tests(spec)
    print("== tamper tests"); tamper_tests(spec, a.baseline)
    print("== build-once (R8)"); build_once_tests(a.baseline)
    print("== gates"); gate_export_tests(spec, a.baseline)
    live = False
    if a.live:
        print("== live strace tests"); live = live_tests(spec)
    ok = all(r["ok"] for r in RESULTS)
    res = {"ok": ok, "live": live, "tests": RESULTS, "n": len(RESULTS),
           "failed": [r for r in RESULTS if not r["ok"]], "auditor_hash": conformance.auditor_hash(),
           "spec_hash": spec["hash"]}
    if a.out:
        json.dump(res, open(a.out, "w"), indent=1)
    print(f"SELFTEST: {'PASS' if ok else 'FAIL'} ({len(RESULTS)} tests, live={live})")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
