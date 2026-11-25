#!/usr/bin/env python3
# Copyright 2026 Parmond AB
# SPDX-License-Identifier: Apache-2.0
"""Audit 1 — conformance: does a run do exactly what the spec says?

    conformance.py RESULTS_DIR [--preflight PREFLIGHT_DIR] [--out attestation.json]

Independent of the harness: it re-derives what it can (static checks from the
archived source snapshot, the snapshot's tree hash, geometry consensus,
statistics) instead of trusting run.json. Writes attestation.json with a
PASS/FAIL verdict and one entry per check:

  C01 spec_hash        run used the current spec, unchanged
  C02 environment      environment class requirements met (reference: bare metal, turbo off, …)
  C03 completeness     every benchmark × configuration measured, geometry + syscheck present;
                       reference/preflight ran exactly the profile's benchmark set (R5)
  C04 versions         runtime-reported versions equal spec pins
  C05 engine           our configurations ran the engine the spec declares (every sample)
  C06 static           compute()/BEGIN_TIMED blocks call only allowlisted functions (re-derived)
  C07 syscalls         strace window clean for every configuration (when required): every kept
                       log re-analysed by the auditor (allowlist model); allowed path reads disclosed
  C08 output           like-for-like geometry vs consensus, waivers only as declared
  C09 statistics       rounds/warm-up/loops as specified, CV within limit, randomized interleaving
  C10 effective        effective configuration equals requested (threads, backend, API)
  C11 provenance       snapshot hash + recomputed tree hash match the recorded source tree hash;
                       reference/preflight: the launch provenance is present and matches
  C12 data             real datasets match pinned sha256; one field per benchmark, as observed by
                       every adapter (input made from it; same array per dtype; native workers'
                       checksums of the field they loaded)
  C13 preflight        (reference) a PASSing preflight with the same source + spec + auditor self-test;
                       R8: the reference installed the preflight's artifact bundle unchanged
                       (bundle + per-file hashes), same pip freeze / OS / arch / Python, and every
                       artifact a tool loaded has the hash the preflight recorded
  C14 sweeps           every swept configuration tried exactly the spec's candidate grid
                       (re-derived); chosen = fastest
  C15 resources        no major page faults in any sample (R6); CPU time per call within the
                       variant's declared thread budget (W4: "1 thread" is one thread)
  C16 work             W5 declared work inventory vs observation: input dtype each tool received,
                       bounded differences within their bounds; differences listed for disclosure
"""
import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile

FRAMEWORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, FRAMEWORK)

from isobench import env, geometry, polygons, provenance, spec as specmod, static_check, stats, syscheck  # noqa: E402

AUDITOR_FILES = ["audit/conformance.py", "isobench/static_check.py", "isobench/syscheck.py",
                 "isobench/geometry.py", "isobench/stats.py", "isobench/spec.py"]


FULL = False          # store admission needs every failure, not the first few


def _cut(xs, n):
    return list(xs) if FULL else list(xs)[:n]


def _jsonl(path):
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


def auditor_hash():
    h = hashlib.sha256()
    for f in AUDITOR_FILES:
        h.update(open(os.path.join(FRAMEWORK, f), "rb").read())
    return h.hexdigest()


def build_once_diffs(prun, run):
    """R8: differences between a preflight run and the reference run that
    must have installed the preflight's artifacts unchanged. [] = identical."""
    d = []
    pm, rm = prun.get("artifact_manifest") or {}, run.get("artifact_manifest") or {}
    if pm.get("mode") != "built" or rm.get("mode") != "installed":
        d.append(["manifest mode", pm.get("mode"), rm.get("mode")])
    if not pm.get("bundle_sha256") or pm.get("bundle_sha256") != rm.get("bundle_sha256"):
        d.append(["bundle sha256", pm.get("bundle_sha256"), rm.get("bundle_sha256")])
    pf, rf = pm.get("files") or {}, rm.get("files") or {}
    for k in sorted(set(pf) | set(rf)):
        if pf.get(k) != rf.get(k):
            d.append(["bundle file", k, pf.get(k), rf.get(k)])
    pt, rt = prun.get("toolchain") or {}, run.get("toolchain") or {}
    for k in ("machine", "os_release", "libc", "python", "pip_freeze"):
        if pt.get(k) is None or pt.get(k) != rt.get(k):
            d.append(["toolchain", k])
    pa = {v["path"]: v["sha256"] for v in (prun.get("artifacts") or {}).values()}
    for k, v in (run.get("artifacts") or {}).items():
        if pa.get(v["path"]) != v["sha256"]:
            d.append(["loaded artifact", k, v["path"], pa.get(v["path"]), v["sha256"]])
    return d[:40]


def audit(results_dir, spec=None, preflight_dir=None):
    spec = spec or specmod.load()
    g = spec["global"]
    run = json.load(open(os.path.join(results_dir, "run.json")))
    meas = _jsonl(os.path.join(results_dir, "measurements.jsonl"))
    geo = _jsonl(os.path.join(results_dir, "geometry.jsonl"))
    sysc = _jsonl(os.path.join(results_dir, "syscheck.jsonl"))
    sweeps = _jsonl(os.path.join(results_dir, "sweeps.jsonl"))
    profile = run.get("profile")
    checks = {}

    def put(cid, ok, **detail):
        checks[cid] = {"ok": bool(ok), **detail}

    # C01 ----------------------------------------------------------------
    put("C01_spec_hash", run.get("spec_hash") == spec["hash"],
        run_spec_hash=run.get("spec_hash"), current_spec_hash=spec["hash"])

    # C02 ----------------------------------------------------------------
    envc = run.get("environment_checks") or []
    # re-derived from the recorded environment (not only the harness's verdicts)
    rederived = env.check(run.get("environment") or {}, profile, g) if profile in g["environments"] else []
    put("C02_environment", bool(envc or profile == "indicative") and all(c[1] for c in envc)
        and all(c[1] for c in rederived),
        failed=[c for c in envc if not c[1]] + [["rederived"] + list(c) for c in rederived if not c[1]],
        profile=profile)

    # C03 ----------------------------------------------------------------
    # a reproduction without an isosweep evaluation build (--exclude-ours) expects
    # every other library; a reference run never leaves isosweep out
    from isobench import store as storemod
    excl = bool(run.get("exclude_ours"))
    expected = {(b, c["config_id"]) for b in run.get("benchmarks", []) for c in specmod.configs_for(spec, b)
                if b in spec["benchmarks"] and not (excl and storemod.is_ours(spec, c["adapter"]))}
    got_m = {(m["benchmark"], m["config_id"]) for m in meas}
    got_g = {(x["benchmark"], x["config_id"]) for x in geo if "config_id" in x}
    need_sys = profile in g["syscall_audit"]["required_on"]
    got_s = {(x["benchmark"], x["config_id"]) for x in sysc}
    missing = sorted(expected - got_m) + [("geometry",) + k for k in sorted(expected - got_g)]
    if need_sys:
        missing += [("syscheck",) + k for k in sorted(expected - got_s)]
    unexpected = sorted(got_m - expected)
    subset = None
    if profile in ("reference", "preflight"):
        want = list(specmod.profile_benchmarks(spec, profile))
        if run.get("store_used"):          # reuse needs the anchors: the harness adds their benchmarks
            want += [b_ for b_ in (g.get("reuse") or {}).get("anchor_benchmarks", []) if b_ not in want]
        if sorted(run.get("benchmarks", [])) != sorted(want):
            subset = {"ran": sorted(run.get("benchmarks", [])), "profile_requires": sorted(want)}
    put("C03_completeness", not missing and not unexpected and bool(expected) and subset is None
        and not run.get("aborted") and not (excl and profile == "reference"), missing=_cut(missing, 20),
        unexpected=unexpected[:20], expected=len(expected), benchmark_set=subset, aborted=run.get("aborted"),
        exclude_ours=excl)

    # C04 ----------------------------------------------------------------
    bad = []
    for m in meas:
        a = spec["tools"]["adapters"][m["adapter"]]["version"]
        pin = a.get("pin") if "pin" in a else a["pin_by_lib"].get(m["params"].get("lib"))
        if m["version"] != pin:
            bad.append((m["benchmark"], m["config_id"], m["version"], pin))
    put("C04_versions", not bad, mismatches=_cut(bad, 20))

    # C05 ----------------------------------------------------------------
    bad = []
    for m in meas:
        exp = m.get("expected_engine")
        spec_exp = None
        rt = spec["tools"]["report_tools"][m["report_tool"]]
        for v in rt["variants"]:
            if v["id"] == m["variant"]:
                spec_exp = v.get("engine")
        if spec_exp != exp:
            bad.append((m["config_id"], "record/spec engine differ", exp, spec_exp))
        if spec_exp is not None and m.get("observed_engines") != [spec_exp]:
            bad.append((m["benchmark"], m["config_id"], m.get("observed_engines"), spec_exp))
        if spec_exp is None and m.get("observed_engines"):
            bad.append((m["config_id"], "competitor reported an engine", m["observed_engines"]))
    put("C05_engine", not bad, mismatches=_cut(bad, 20))

    # C11 (before C06: static checks are re-derived from the verified snapshot) ---
    snap = os.path.join(results_dir, "source_snapshot.tar.gz")
    prov_ok, prov = False, {}
    tmp = None
    if os.path.exists(snap):
        sha = hashlib.sha256(open(snap, "rb").read()).hexdigest()
        tmp = tempfile.mkdtemp(prefix="isobench-audit-")
        with tarfile.open(snap) as tar:
            tar.extractall(tmp, filter="data")
            names = sorted(m.name for m in tar.getmembers() if m.isfile())
        th = provenance.tree_hash(root=tmp, files=names)
        launch = run.get("launch_provenance") or {}
        prov = {"snapshot_sha256": sha, "recorded_snapshot_sha256": run.get("snapshot_sha256"),
                "recomputed_tree_hash": th, "recorded_tree_hash": run.get("source_tree_hash"),
                "launch_tree_hash": launch.get("source_tree_hash")}
        # R11: cloud runs must carry the orchestrator's launch provenance (tree hash at launch)
        need_launch = profile in ("reference", "preflight")
        prov_ok = (sha == run.get("snapshot_sha256") and th == run.get("source_tree_hash")
                   and ((launch.get("source_tree_hash") == th) if need_launch
                        else (not launch or launch.get("source_tree_hash") == th)))
        prov["launch_required"] = need_launch
    put("C11_provenance", prov_ok, **prov)

    # C06 ----------------------------------------------------------------
    bad, derived = [], {}
    if tmp:
        sys.path.insert(0, os.path.join(tmp, "bench", "framework"))
        for name in sorted({m["adapter"] for m in meas}):
            a = spec["tools"]["adapters"][name]
            if "python" in a["impl"]:
                src = os.path.join(tmp, "bench", "framework", "isobench", "adapters.py")
                cls = a["impl"]["python"].split(":")[1]
                calls = _python_calls_from_source(src, cls)
                ok, v = static_check.check(calls, a["compute_allowlist"], g["static_denylist"]["python"])
            else:
                ok, v = static_check.check_native(os.path.join(tmp, "bench", "framework", a["impl"]["worker"]),
                                                  a["compute_allowlist"], g["static_denylist"]["native"])
            derived[name] = ok
            if not ok:
                bad.append((name, v))
    put("C06_static", bool(derived) and not bad, violations=_cut(bad, 20), adapters=derived)

    # C07 ----------------------------------------------------------------
    if need_sys:
        badsys, path_reads = [], {}
        for x in sysc:
            # re-analysed here from the kept log with the auditor's own analyser (R6)
            lp = os.path.join(results_dir, x.get("log") or "")
            if not x.get("log") or not os.path.exists(lp):
                badsys.append((x["benchmark"], x["config_id"], "strace log missing"))
                continue
            if hashlib.sha256(open(lp, "rb").read()).hexdigest() != x.get("log_sha256"):
                badsys.append((x["benchmark"], x["config_id"], "strace log hash differs from the record"))
                continue
            r = syscheck.analyze(lp, g)
            if not r["ok"] or not r["window_found"] or r["ok"] != x.get("ok"):
                badsys.append((x["benchmark"], x["config_id"], r["n_violations"], r["violations"][:3]))
            if r["allowed_path_reads"]:
                path_reads[f"{x['benchmark']}/{x['config_id']}"] = r["allowed_path_reads"]
        put("C07_syscalls", run.get("syscheck_enabled") and not badsys and bool(sysc), failures=_cut(badsys, 20),
            checked=len(sysc), disclosed_path_reads=path_reads)
    else:
        put("C07_syscalls", True, note=f"not required for profile '{profile}'", checked=len(sysc))

    # C08 ----------------------------------------------------------------
    findings_all, bad = {}, []
    adapters = spec["tools"]["adapters"]
    for b in run.get("benchmarks", []):
        kind = spec["benchmarks"][b]["kind"]
        tol = dict(g["validation_defaults"][kind])
        tol.update(spec["benchmarks"][b].get("validation", {}))
        summaries, waivers, families, precision, budget, area_budget = {}, {}, {}, {}, None, None
        ties = None
        for x in geo:
            if x["benchmark"] != b:
                continue
            if "tie_budget" in x or "tie_area_budget" in x:
                ties = x.get("tie_budget", x.get("tie_area_budget"))
                if "saddle_budget" not in x and "saddle_area_budget" not in x:
                    continue
            if "saddle_budget" in x:
                budget = x["saddle_budget"]
                continue
            if "saddle_area_budget" in x:
                area_budget = x["saddle_area_budget"]
                continue
            cid = x["config_id"]
            summaries[cid] = x["summary"]
            rt = spec["tools"]["report_tools"][x["report_tool"]]
            waivers[cid] = {w["check"]: w for w in rt.get("waivers", [])}
            ad = adapters[rt["adapter"]]
            families[cid] = ad.get("family", rt["adapter"])
            precision[cid] = ad.get("precision", "float64")
        if not summaries:
            continue
        if kind == "contour2d":
            f = geometry.compare_lines(summaries, families, waivers, tol, precision=precision, budget=budget,
                                       ties=ties)
        elif kind == "bands2d":
            # the oracle: the bands partition the node domain (shape observed by the tools, not copied)
            shp = next(m["work"]["shape"] for m in meas if m["benchmark"] == b and m.get("work"))
            dom = float((shp[0] - 1) * (shp[1] - 1))
            f = polygons.compare_bands(summaries, families, waivers, tol, dom, area_budget or [],
                                       precision=precision, ties=ties)
        else:
            f = geometry.compare_surface3d(summaries, waivers, tol)
        findings_all[b] = f
        bad += [(b, x.get("tool"), x["check"], x.get("detail")) for x in f if not x["ok"]]
    put("C08_output", not bad, failures=_cut(bad, 30), benchmarks=len(findings_all))

    # C16 ----------------------------------------------------------------
    bad, disclosed = [], {}
    for m in meas:
        ad = adapters[m["adapter"]]
        wk = ad.get("work")
        if not wk:
            continue
        obs = m.get("work") or {}
        if obs.get("dtype") != wk["input_dtype"]:
            bad.append((m["benchmark"], m["config_id"], "input dtype", obs.get("dtype"), wk["input_dtype"]))
        disclosed[m["adapter"]] = wk.get("differences", [])
    for x in geo:
        if "config_id" not in x:
            continue
        ad = adapters[spec["tools"]["report_tools"][x["report_tool"]]["adapter"]]
        for d in (ad.get("work") or {}).get("differences", []):
            if d["measured_by"] == "clipped_length":
                s_ = x["summary"]
                rel = s_.get("clipped_length", 0.0) / max(s_.get("total_length", 0.0), 1e-12)
                if rel > 0.01:
                    bad.append((x["benchmark"], x["config_id"], "clipped border length above its 1% bound", rel))
            if d["measured_by"] == "clipped_area":
                s_ = x["summary"]
                rel = s_.get("clipped_area", 0.0) / max(s_.get("total_area", 0.0), 1e-12)
                if rel > 0.01:
                    bad.append((x["benchmark"], x["config_id"], "clipped border area above its 1% bound", rel))
    put("C16_work", not bad, failures=_cut(bad, 30), disclosed_differences=disclosed)

    # C09 ----------------------------------------------------------------
    st, prof = g["stats"], g["profiles"].get(profile, {})
    rounds_req = prof.get("rounds", st["rounds"])
    cv_gate = profile in st.get("cv_gate_on", [])
    bad, variability = [], []
    for m in meas:
        s = stats.summarize(m["samples_ms"])
        if len(m["samples_ms"]) != rounds_req or m["warmup_runs"] < st["warmup_runs"] or m["loops"] < 1:
            bad.append((m["benchmark"], m["config_id"], "rounds/warmup/loops", len(m["samples_ms"])))
        if s["cv"] > st["max_cv"]:
            variability.append((m["benchmark"], m["config_id"], round(s["cv"], 4)))
            if cv_gate:
                bad.append((m["benchmark"], m["config_id"], "cv", round(s["cv"], 4)))
    orders = run.get("round_orders", {})
    for b in run.get("benchmarks", []):
        o = orders.get(b)
        n = len([m for m in meas if m["benchmark"] == b and not m.get("reused")])
        if n == 0:
            continue                       # everything reused: ordered in the runs that measured it
        if not o or len(o) != rounds_req or any(sorted(r) != list(range(n)) for r in o):
            bad.append((b, "round orders missing, not full permutations, or not exactly the fixed count"))
        elif n > 2 and len(o) > 1 and len({tuple(r) for r in o}) == 1:
            bad.append((b, "round order not randomized"))
    put("C09_statistics", not bad, failures=_cut(bad, 30), cv_gate=cv_gate,
        variability_above=st["max_cv"], variability=variability)

    # C10 ----------------------------------------------------------------
    bad = []
    for m in meas:
        p, e = m["params"], m.get("effective", {})
        if "nthreads" in p and e.get("nthreads") != p["nthreads"]:
            bad.append((m["config_id"], "nthreads", p["nthreads"], e.get("nthreads")))
        if "backend" in p and e.get("backend") != p["backend"]:
            bad.append((m["config_id"], "backend", p["backend"], e.get("backend")))
        if p.get("backend") not in (None, "Sequential") and e.get("threads") != p.get("threads"):
            bad.append((m["config_id"], "threads", p.get("threads"), e.get("threads")))
        if "thread_count" in p and e.get("thread_count") != p["thread_count"]:
            bad.append((m["config_id"], "thread_count", p["thread_count"], e.get("thread_count")))
        if "api" in p and p["api"] == "callback" and "callback" not in str(e.get("api", "")):
            bad.append((m["config_id"], "api", p["api"], e.get("api")))
        if "reuse_generator" in p and bool(e.get("reuse_generator")) != bool(p["reuse_generator"]):
            bad.append((m["config_id"], "reuse_generator", p["reuse_generator"], e.get("reuse_generator")))
        if m.get("expected_engine") and "nthreads" in p and m.get("observed_threads") != [p["nthreads"]] \
                and spec["tools"]["adapters"][m["adapter"]].get("threads_reported", True):
            # the engine itself reports the threads it ran (R7), not the adapter
            bad.append((m["config_id"], "threads the engine used", p["nthreads"], m.get("observed_threads")))
    put("C10_effective", not bad, mismatches=_cut(bad, 20))

    # C15 ----------------------------------------------------------------
    bad = []
    for m in meas:
        flts = m.get("majflt")
        if flts is None or any(f > st.get("max_major_faults", 0) for f in flts):
            bad.append((m["benchmark"], m["config_id"], "major page faults in a sample", flts and max(flts)))
        rt = spec["tools"]["report_tools"][m["report_tool"]]
        var = next(v for v in rt["variants"] if v["id"] == m["variant"])
        budget = var.get("max_threads") or m["params"].get("nthreads")
        cpu = m.get("cpu_ms")
        if cpu is None:
            bad.append((m["benchmark"], m["config_id"], "no CPU time recorded"))
        elif budget:
            ratio = stats.summarize(cpu)["median"] / max(m["summary"]["median"], 1e-9)
            if ratio > budget + 0.15:
                bad.append((m["benchmark"], m["config_id"], "CPU/wall above thread budget", round(ratio, 2), budget))
    put("C15_resources", not bad, failures=_cut(bad, 30))

    # C12 ----------------------------------------------------------------
    bad = []
    for b in run.get("benchmarks", []):
        d = spec["benchmarks"][b]["data"]
        recs = [m for m in meas if m["benchmark"] == b]
        hashes = {m["data"].get("field_sha256") for m in recs}
        if len(hashes) != 1:
            bad.append((b, "configurations saw different fields", len(hashes)))
        # observed by each adapter (R11), not copied: what it was handed, and what it was made from
        by_dtype = {}
        for m in recs:
            w = m.get("work") or {}
            if w.get("source_sha256") not in hashes or len(hashes) != 1:
                bad.append((b, m["config_id"], "tool's input was not made from the benchmark field"))
            by_dtype.setdefault(w.get("dtype"), set()).add(w.get("sha256"))
            if "worker_loaded_matches" in w and w["worker_loaded_matches"] is not True:
                bad.append((b, m["config_id"], "worker loaded a different field than it was handed"))
            if "worker" in spec["tools"]["adapters"][m["adapter"]]["impl"] and "worker_loaded_matches" not in w:
                bad.append((b, m["config_id"], "worker did not report the field it loaded"))
        for dt, hs in by_dtype.items():
            if len(hs) != 1:
                bad.append((b, f"tools given {dt} input saw {len(hs)} different arrays"))
        if "sha256" in d and any(m["data"].get("dataset_sha256") != d["sha256"] for m in recs):
            bad.append((b, "dataset sha256 != spec pin"))
    put("C12_data", not bad, failures=_cut(bad, 20))

    # C13 ----------------------------------------------------------------
    if profile == "reference":
        ok, det = False, {"preflight_dir": preflight_dir}
        if preflight_dir and os.path.exists(os.path.join(preflight_dir, "attestation.json")):
            pa = json.load(open(os.path.join(preflight_dir, "attestation.json")))
            det.update(preflight_verdict=pa.get("verdict"), preflight_tree=pa.get("source_tree_hash"),
                       preflight_spec=pa.get("spec_hash"), selftest=pa.get("auditor_selftest", {}).get("ok"))
            ok = (pa.get("verdict") == "PASS" and pa.get("profile") == "preflight"
                  and pa.get("source_tree_hash") == run.get("source_tree_hash")
                  and pa.get("spec_hash") == run.get("spec_hash")
                  and pa.get("auditor_selftest", {}).get("ok") is True
                  and pa.get("auditor_selftest", {}).get("live") is True)
            prun_p = os.path.join(preflight_dir, "run.json")
            prun = json.load(open(prun_p)) if os.path.exists(prun_p) else {}
            det["build_once"] = same = build_once_diffs(prun, run)
            ok = ok and not same
        put("C13_preflight", ok, **det)
    else:
        put("C13_preflight", True, note="only required for reference runs")

    # C14 ----------------------------------------------------------------
    bad = []
    swept = {(m["benchmark"], m["config_id"]) for m in meas
             if any(isinstance(v, dict) and "sweep" in v for v in
                    next(v for v in spec["tools"]["report_tools"][m["report_tool"]]["variants"]
                         if v["id"] == m["variant"])["params"].values())}
    by_key = {(s["benchmark"], s["config_id"]): s for s in sweeps}
    si = run.get("sweep_inputs") or {}
    for k in swept:
        s = by_key.get(k)
        if not s or not s["trials"]:
            bad.append((k, "no sweep record"))
            continue
        # completeness (R11): every candidate of the spec's grid was tried, nothing else
        m = next(x for x in meas if (x["benchmark"], x["config_id"]) == k)
        vp = next(v for v in spec["tools"]["report_tools"][m["report_tool"]]["variants"]
                  if v["id"] == m["variant"])["params"]
        if not si:
            bad.append((k, "sweep inputs (ncpu, SMP backends) not recorded"))
        else:
            want, _ = specmod.expand_sweeps(vp, spec, si["ncpu"], si["smp_backends"])
            key = lambda p: json.dumps(p, sort_keys=True)  # noqa: E731
            if sorted(map(key, want)) != sorted(key(t["params"]) for t in s["trials"]):
                bad.append((k, "candidates tried != the spec's candidate grid",
                            len(want), len(s["trials"])))
        best = min(s["trials"], key=lambda t: t["median_ms"])["params"]
        m = next(x for x in meas if (x["benchmark"], x["config_id"]) == k)
        if best != s["chosen"] or m["params"] != s["chosen"]:
            bad.append((k, "chosen/measured configuration is not the fastest candidate"))
    put("C14_sweeps", not bad, failures=_cut(bad, 20), swept=len(swept))

    # C17 ----------------------------------------------------------------
    # R13: a record reused from the result store must have been made by the same
    # data, configuration, tool code, build, method and machine as this run would
    # use: its key, recomputed here from this run's verified snapshot (C11),
    # environment and artifact hashes, must equal the stored one; and the
    # anchors re-measured in this run must match their stored results.
    from isobench import store as storemod
    reused = [m for m in meas if m.get("reused")]
    bad = []
    if reused:
        if not tmp:
            bad.append(("no verified source snapshot to recompute keys from",))
        else:
            arts = run.get("artifacts") or {}
            for m in reused:
                cfg = next((c for c in specmod.configs_for(spec, m["benchmark"])
                            if c["config_id"] == m["config_id"]), None)
                if cfg is None:
                    bad.append((m["benchmark"], m["config_id"], "not a configuration of the spec"))
                    continue
                shas = {n.split("::", 1)[1]: v["sha256"] for n, v in arts.items()
                        if n.startswith(m["config_id"] + "::")}
                tk, _, _ = storemod.keys(spec, m["benchmark"], cfg, m["data"]["field_sha256"], shas, m["version"],
                                         run.get("environment") or {}, run.get("toolchain"),
                                         run.get("sweep_inputs"), root=tmp)
                if tk != m["reused"].get("timing_key"):
                    bad.append((m["benchmark"], m["config_id"], "key differs from the stored record's"))
        ru = g.get("reuse") or {}
        seen = {(x["benchmark"], x["config_id"]): x for x in run.get("anchors") or []}
        fresh = {(m["benchmark"], m["config_id"]): m for m in meas if not m.get("reused")}
        want = storemod.anchor_configs(spec)
        drifts = []
        for k in want:
            x, f_ = seen.get(k), fresh.get(k)
            if not x or not f_:
                bad.append((k[0], k[1], "anchor configuration not re-measured and compared"))
                continue
            if abs(f_["summary"]["median"] - x["median_ms"]) > 1e-9 * max(1.0, x["median_ms"]):
                bad.append((k[0], k[1], "anchor record differs from the comparison logged"))
            drifts.append(x["median_ms"] / x["stored_median_ms"] - 1)
        if not want:
            bad.append(("no anchor benchmarks in the spec",))
        elif drifts:
            med = stats.summarize(drifts)["median"]
            if abs(med) > ru.get("max_drift", 0.03):
                bad.append(("anchor configurations: median drift", round(med, 4)))
    put("C17_reuse", not bad, failures=_cut(bad, 20), reused=len(reused),
        anchors=run.get("anchors") or [])

    verdict = "PASS" if all(c["ok"] for c in checks.values()) else "FAIL"
    return {"verdict": verdict, "profile": profile, "run_id": run.get("run_id"),
            "spec_hash": run.get("spec_hash"), "source_tree_hash": run.get("source_tree_hash"),
            "auditor_hash": auditor_hash(), "checks": checks, "geometry_findings": findings_all}


def _python_calls_from_source(path, cls_name):
    import ast
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == "compute":
                    return [static_check._dotted(n.func) or "<dynamic>"
                            for n in ast.walk(fn) if isinstance(n, ast.Call)]
            # inherited compute: search bases defined in the same module
            for base in node.bases:
                if isinstance(base, ast.Name):
                    r = _python_calls_from_source(path, base.id)
                    if r is not None:
                        return r
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("--preflight", default=None)
    ap.add_argument("--selftest", default=None, help="selftest.json to embed (preflight runs)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    att = audit(a.results_dir, preflight_dir=a.preflight)
    if a.selftest:
        st = json.load(open(a.selftest))
        att["auditor_selftest"] = st
        if not st.get("ok"):
            att["verdict"] = "FAIL"
    out = a.out or os.path.join(a.results_dir, "attestation.json")
    json.dump(att, open(out, "w"), indent=1)
    for cid, c in att["checks"].items():
        print(f"  {cid:18s} {'ok  ' if c['ok'] else 'FAIL'}  " +
              ("" if c["ok"] else json.dumps({k: v for k, v in c.items() if k != 'ok'})[:300]))
    print(f"VERDICT: {att['verdict']}  → {out}")
    sys.exit(0 if att["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
