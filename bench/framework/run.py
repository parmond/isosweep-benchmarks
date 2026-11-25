#!/usr/bin/env python3
"""isobench harness — runs the benchmarks exactly as the spec defines them.

    run.py --profile reference|preflight|indicative --out DIR
           [--data-dir D] [--bin-dir B] [--benchmarks a,b] [--provenance-file F]

Only this file touches the clock. Adapters supply prepare/activate/compute/
collect; the harness decides what is timed. Every run writes:
  run.json            spec hash, environment + checks, provenance, artifacts, static checks
  measurements.jsonl  one record per (benchmark, configuration): samples, loops, engine, effective config
  sweeps.jsonl        every candidate tried when selecting a multi-threaded configuration
  geometry.jsonl      output summaries (like-for-like checks)
  syscheck.jsonl      strace validation results per (benchmark, configuration)
  source_snapshot.tar.gz  the exact source tree the run was built from
The run is then audited by audit/conformance.py (attestation.json).
"""
import argparse
import gc
import importlib
import json
import math
import os
import platform
import random
import resource
import subprocess
import sys
import time

FRAMEWORK = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FRAMEWORK)

from isobench import store, data, env, geometry, polygons, provenance, spec as specmod, static_check, stats, syscheck  # noqa: E402
from isobench.data import sha256_file  # noqa: E402


def adapter_class(spec, adapter_name):
    a = spec["tools"]["adapters"][adapter_name]
    from isobench import adapters
    if "python" in a["impl"]:
        mod, cls = a["impl"]["python"].split(":")
        return getattr(importlib.import_module(mod), cls)
    return adapters.ADAPTER_CLASSES[adapter_name]


def static_checks(spec, adapter_names):
    g = spec["global"]
    out = {}
    for name in sorted(adapter_names):
        a = spec["tools"]["adapters"][name]
        if "python" in a["impl"]:
            calls = static_check.python_calls(adapter_class(spec, name))
            ok, bad = static_check.check(calls, a["compute_allowlist"], g["static_denylist"]["python"])
            where = a["impl"]["python"]
        else:
            path = os.path.join(FRAMEWORK, a["impl"]["worker"])
            calls = static_check.native_calls(path)
            ok, bad = static_check.check_native(path, a["compute_allowlist"], g["static_denylist"]["native"])
            where = a["impl"]["worker"]
        out[name] = {"ok": ok, "violations": bad, "calls": sorted(set(calls or [])), "source": where}
    return out


def _smp_backends():
    from isobench.adapters import vtk_backends
    return vtk_backends() or ["STDThread"]


def expand_sweeps(params, spec, ncpu):
    return specmod.expand_sweeps(params, spec, ncpu, _smp_backends())


def measure(inst, loops):
    """One sample of `loops` calls. Each call is timed on its own; the previous
    call's result is released outside the clock and the cyclic garbage
    collector is off inside the sample (as timeit does), so freeing and
    collection never land in the timed region (R9/R10). Returns (sample, last
    result): sample = per-call wall ms (mean over loops), per-call CPU ms
    (all threads of the process: W4), major page faults (must be 0: R6),
    fastest/slowest single call. Native workers time themselves the same way."""
    inst.activate()
    gc.collect()
    if inst.native:
        r = inst.run_native(loops)
        return {"ms": r["wall_ns"] / 1e6 / loops, "cpu_ms": r["cpu_ns"] / 1e6 / loops,
                "majflt": r["majflt"], "call_min_ms": r["min_ns"] / 1e6,
                "call_max_ms": r["max_ns"] / 1e6}, None
    wall = cpu = 0
    mn, mx = None, 0
    res = None
    ru0 = resource.getrusage(resource.RUSAGE_SELF).ru_majflt
    gc.disable()
    try:
        for i in range(loops):
            c0 = time.process_time_ns()
            t0 = time.perf_counter_ns()
            res = inst.compute()
            t1 = time.perf_counter_ns()
            c1 = time.process_time_ns()
            d = t1 - t0
            wall += d
            cpu += c1 - c0
            mn = d if mn is None else min(mn, d)
            mx = max(mx, d)
            if i < loops - 1:
                res = None                      # released untimed
    finally:
        gc.enable()
    majflt = resource.getrusage(resource.RUSAGE_SELF).ru_majflt - ru0
    return {"ms": wall / 1e6 / loops, "cpu_ms": cpu / 1e6 / loops, "majflt": majflt,
            "call_min_ms": mn / 1e6, "call_max_ms": mx / 1e6}, res


def calibrate(inst, st):
    """pyperf-style: loops so that one sample lasts >= min_sample_ms."""
    t = measure(inst, 1)[0]["ms"]
    loops = max(1, math.ceil(st["min_sample_ms"] / max(t, 1e-6)))
    return min(loops, st["max_loops"]), t


def new_instance(spec, cfg, params, ctx, z, levels, field_path):
    inst = adapter_class(spec, cfg["adapter"])(params, ctx)
    inst.prepare(z, levels, field_path)
    return inst


class AnchorDrift(Exception):
    """An anchor differs from its stored result beyond reuse.max_drift."""


def run_benchmark(spec, bid, ctx, args, out, rng):
    g = spec["global"]
    st = dict(g["stats"])
    prof = g["profiles"][args.profile]
    if "rounds" in prof:
        st["rounds"] = prof["rounds"]
    b = spec["benchmarks"][bid]
    z, field_path, dinfo = data.load_field(b, args.data_dir, ctx["cache_dir"])
    levels = data.make_levels(b, z)
    print(f">>> {bid}  shape={list(z.shape)}  levels={len(levels)}", flush=True)
    configs = [c for c in specmod.configs_for(spec, bid)
               if not (ctx.get("exclude_ours") and store.is_ours(spec, c["adapter"]))]
    ncpu = os.cpu_count()
    # ---- reuse (R13): a configuration measured before with the same code, data,
    # method and machine is taken from the store, not measured again; anchors
    # are always measured (they tie the stored results to this machine)
    reused, probes, anchor_entries = {}, {}, {}
    for cfg in configs:
        cands, _ = expand_sweeps(cfg["params"], spec, ncpu)
        probe = new_instance(spec, cfg, cands[0], ctx, z, levels, field_path)
        shas = {k: sha256_file(p) for k, p in probe.artifacts().items() if p and os.path.exists(p)}
        paths = {k: p for k, p in probe.artifacts().items() if p and os.path.exists(p)}
        ver = probe.version()
        probe.close()
        tk, ok_, _parts = store.keys(spec, bid, cfg, dinfo["field_sha256"], shas, ver, ctx["environment"],
                                     ctx["toolchain"], ctx["sweep_inputs"])
        probes[cfg["config_id"]] = {"timing_key": tk, "output_key": ok_, "paths": paths}
        if ctx.get("store"):
            e = store.lookup(ctx["store"], ctx["env_fp"], tk)
            if e and (bid, cfg["config_id"]) in ctx["anchors"]:
                anchor_entries[cfg["config_id"]] = e      # re-measured, compared below
            elif e:
                reused[cfg["config_id"]] = e
    fresh = [c for c in configs if c["config_id"] not in reused]
    if reused:
        print(f"    reused from the store: {len(reused)} of {len(configs)} configurations", flush=True)
    final = []
    # ---- sweeps: pick each multi-threaded configuration's best setting (all tools alike)
    for cfg in fresh:
        cands, swept = expand_sweeps(cfg["params"], spec, ncpu)
        ae = anchor_entries.get(cfg["config_id"])
        if swept and ae and ae.get("sweep"):
            # an anchor is re-measured in exactly the stored configuration (its
            # sweep's choice), so the comparison is like for like
            sw = ae["sweep"]
            out["sweeps"].write(json.dumps(dict(sw, anchor_from={"timing_key": ae["timing_key"],
                                                                 "source": ae["source"]})) + "\n")
            final.append((cfg, sw["chosen"], sw["trials"]))
            continue
        if not swept:
            final.append((cfg, cands[0], None))
            continue
        trials = []
        for p in cands:
            inst = new_instance(spec, cfg, p, ctx, z, levels, field_path)
            measure(inst, 1)                                   # warm-up
            loops, _ = calibrate(inst, st)
            samples = [measure(inst, loops)[0]["ms"] for _ in range(g["sweeps"]["repeats"])]
            trials.append({"params": p, "median_ms": stats.summarize(samples)["median"], "loops": loops})
            inst.close()
        best = min(trials, key=lambda t: t["median_ms"])
        out["sweeps"].write(json.dumps({"benchmark": bid, "config_id": cfg["config_id"], "trials": trials,
                                        "chosen": best["params"]}) + "\n")
        final.append((cfg, best["params"], trials))
    # ---- final instances: prepare, warm-up, engine + effective config, calibration
    insts = []
    for cfg, params, _ in final:
        inst = new_instance(spec, cfg, params, ctx, z, levels, field_path)
        eff = inst.activate()
        for _ in range(st["warmup_runs"]):
            _, res = measure(inst, 1)
        loops, _ = calibrate(inst, st)
        insts.append({"cfg": cfg, "params": params, "inst": inst, "loops": loops, "samples": [],
                      "cpu": [], "majflt": [], "call_min": [], "call_max": [],
                      "engines": set(), "threads_used": set(), "effective": eff, "last": None})
    # ---- timed rounds: a fixed number (no optional stopping: R9), randomized interleaving
    orders = []
    for r in range(st["rounds"]):
        order = list(range(len(insts)))
        rng.shuffle(order)
        orders.append(order)
        for i in order:
            it = insts[i]
            smp, res = measure(it["inst"], it["loops"])
            it["samples"].append(smp["ms"])
            it["cpu"].append(smp["cpu_ms"])
            it["majflt"].append(smp["majflt"])
            it["call_min"].append(smp["call_min_ms"])
            it["call_max"].append(smp["call_max_ms"])
            if res is not None:
                it["last"] = res
                e = it["inst"].engine(res)
                if e is not None:
                    it["engines"].add(e)
                tu = it["inst"].threads_used(res)
                if tu is not None:
                    it["threads_used"].add(tu)
            elif it["inst"].native:
                it["last"] = None
                last = it["inst"].native_last()          # ours: the library reports what ran
                if last is not None:
                    e, tu = it["inst"].engine(last), it["inst"].threads_used(last)
                    if e is not None:
                        it["engines"].add(e)
                    if tu is not None:
                        it["threads_used"].add(tu)
    # ---- geometry + records
    kind = b["kind"]
    if kind == "contour2d":     # what saddle and tie conventions alone can explain (C08), from the field
        out["geometry"].write(json.dumps({"benchmark": bid, "saddle_budget": geometry.saddle_budget(z, levels),
                                          "tie_budget": geometry.tie_budget(z, levels)}) + "\n")
    elif kind == "bands2d":
        out["geometry"].write(json.dumps({"benchmark": bid, "saddle_area_budget":
                                          polygons.saddle_area_budget(z, levels),
                                          "tie_area_budget": polygons.tie_area_budget(z, levels)}) + "\n")
    for it in insts:
        inst, cfg = it["inst"], it["cfg"]
        raw = inst.collect(it["last"])
        if kind == "contour2d":
            frame = spec["tools"]["adapters"][cfg["adapter"]]["coords"]
            geo = geometry.summarize_lines(raw, frame, z, levels)
        elif kind == "bands2d":
            frame = spec["tools"]["adapters"][cfg["adapter"]]["coords"]
            geo = polygons.summarize_bands(raw, frame, z, levels)
        else:
            geo = raw
        out["geometry"].write(json.dumps({"benchmark": bid, "config_id": cfg["config_id"],
                                          "report_tool": cfg["report_tool"], "summary": geo}) + "\n")
        rec = {"benchmark": bid, "config_id": cfg["config_id"], "report_tool": cfg["report_tool"],
               "variant": cfg["variant"], "adapter": cfg["adapter"], "params": it["params"],
               "effective": it["effective"], "expected_engine": cfg["engine"],
               "observed_engines": sorted(it["engines"]), "observed_threads": sorted(it["threads_used"]),
               "version": inst.version(),
               "loops": it["loops"], "samples_ms": it["samples"], "summary": stats.summarize(it["samples"]),
               "cpu_ms": it["cpu"], "majflt": it["majflt"], "call_min_ms": it["call_min"],
               "call_max_ms": it["call_max"], "breakdown": inst.breakdown(),
               "work": inst.work_observed(),
               "warmup_runs": st["warmup_runs"], "rounds": st["rounds"], "data": dinfo,
               "levels": len(levels), "native_timing": inst.native}
        out["measurements"].write(json.dumps(rec) + "\n")
        out["artifacts"].update({f"{cfg['config_id']}::{k}": v for k, v in inst.artifacts().items() if v})
        inst.close()
    for cid, e in reused.items():
        src = {"timing_key": e["timing_key"], "source": e["source"], "admitted_at": e.get("admitted_at")}
        out["measurements"].write(json.dumps(dict(e["measurement"], reused=src)) + "\n")
        if e.get("sweep"):
            out["sweeps"].write(json.dumps(dict(e["sweep"], reused=src)) + "\n")
        if e.get("geometry"):
            out["geometry"].write(json.dumps(dict(e["geometry"], reused=src)) + "\n")
        out["artifacts"].update({f"{cid}::{k}": p for k, p in probes[cid]["paths"].items()})
        if e.get("syscheck") and ctx["syscheck"]:
            sy = dict(e["syscheck"], reused=src)
            if e.get("syscheck_log"):
                dst = os.path.join(ctx["syscheck_dir"], os.path.basename(e["syscheck"]["log"]))
                import shutil as _sh
                _sh.copyfile(os.path.join(ctx["store"], e["syscheck_log"]), dst)
                sy["log"] = os.path.relpath(dst, args.out)
            out["syscheck"].write(json.dumps(sy) + "\n")
    out["keys"][bid] = {cid: {k: v for k, v in p.items() if k != "paths"} for cid, p in probes.items()}
    out["orders"][bid] = orders
    # ---- dynamic syscall validation (Linux, strace) per configuration
    if ctx["syscheck"]:
        for cfg, params, _ in final:
            tag = f"{bid}__{cfg['config_id'].replace('/', '__')}"
            log = os.path.join(ctx["syscheck_dir"], tag + ".strace")
            loops = next(it["loops"] for it in insts if it["cfg"] is cfg)
            cmd = [sys.executable, os.path.join(FRAMEWORK, "validate_one.py"), "--benchmark", bid,
                   "--adapter", cfg["adapter"], "--params", json.dumps(params), "--data-dir", args.data_dir,
                   "--bin-dir", ctx["bin_dir"], "--cache-dir", ctx["cache_dir"], "--loops", str(loops)]
            rc, so, se = syscheck.run_traced(cmd, log, g)
            res = syscheck.analyze(log, g)
            # every log is kept (gzip) for the auditor to re-analyse (R6, C07)
            import gzip
            import shutil as _sh
            with open(log, "rb") as fi, gzip.open(log + ".gz", "wb", compresslevel=6) as fo:
                _sh.copyfileobj(fi, fo)
            os.unlink(log)
            res.update({"benchmark": bid, "config_id": cfg["config_id"], "returncode": rc,
                        "stderr_tail": se[-400:] if rc else "",
                        "log": os.path.relpath(log + ".gz", args.out), "log_sha256": sha256_file(log + ".gz")})
            out["syscheck"].write(json.dumps(res) + "\n")
            print(f"    syscheck {cfg['config_id']:32s} {'ok' if res['ok'] else 'FAIL'} "
                  f"({res['window_syscalls']} syscalls in window)", flush=True)
    for it in insts:
        s = stats.summarize(it["samples"])
        print(f"    {it['cfg']['config_id']:32s} {s['median']:11.3f} ms  cv={s['cv']:.3f} loops={it['loops']}"
              f"  engine={sorted(it['engines']) or '-'}", flush=True)
        cid = it["cfg"]["config_id"]
        if ctx.get("store") and (bid, cid) in ctx["anchors"]:
            e = anchor_entries.get(cid)
            if e:
                old = e["measurement"]["summary"]["median"]
                drift = s["median"] / old - 1
                out["anchors"].append({"benchmark": bid, "config_id": cid, "timing_key": probes[cid]["timing_key"],
                                       "median_ms": s["median"], "stored_median_ms": old, "drift": drift,
                                       "stored_source": e["source"]})
                print(f"    anchor {cid}: {s['median']:.3f} ms vs stored {old:.3f} ms ({100 * drift:+.2f}%)", flush=True)


def _finish(run, out, args):
    for f in ("measurements", "sweeps", "geometry", "syscheck"):
        out[f].flush()
    hashed = {}
    for k, p in out["artifacts"].items():
        if p and os.path.exists(p):
            hashed[k] = {"path": p, "sha256": sha256_file(p)}
    run.update({"finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "artifacts": hashed,
                "round_orders": out["orders"], "keys": out["keys"], "anchors": out["anchors"]})
    json.dump(run, open(os.path.join(args.out, "run.json"), "w"), indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=["reference", "preflight", "indicative"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--data-dir", default=os.path.expanduser("~/landscape_data"))
    ap.add_argument("--bin-dir", default=os.path.expanduser("~/bin"))
    ap.add_argument("--benchmarks", default="")
    ap.add_argument("--provenance-file", default="", help="orchestrator's provenance.json (tree hash at launch)")
    ap.add_argument("--no-syscheck", action="store_true", help="only allowed for the indicative profile")
    ap.add_argument("--artifact-manifest", default="",
                    help="build-once artifact manifest written by cloud/setup_ubuntu.sh (R8)")
    ap.add_argument("--store", default="", help="result store (R13): reuse configurations already measured "
                                                "with the same code, data, method and machine")
    ap.add_argument("--exclude-ours", action="store_true",
                    help="leave out isosweep's configurations (no evaluation build installed): "
                         "reproduces every other library's results; C03 then lists isosweep as missing")
    args = ap.parse_args()
    spec = specmod.load()
    g = spec["global"]
    if args.no_syscheck and args.profile != "indicative":
        sys.exit("--no-syscheck is only allowed for indicative runs")
    if args.benchmarks and args.profile != "indicative":
        sys.exit("--benchmarks (a subset) is only allowed for indicative runs (R5)")
    os.makedirs(args.out, exist_ok=True)
    e = env.collect()
    checks = env.check(e, args.profile, g)
    failed = [c for c in checks if not c[1]]
    if failed and args.profile == "reference":
        json.dump({"aborted": "environment requirements not met", "checks": checks, "environment": e},
                  open(os.path.join(args.out, "run.json"), "w"), indent=1)
        sys.exit(f"reference environment requirements not met: {failed}")
    files = provenance.tree_files()
    th = provenance.tree_hash(files=files)
    launch = json.load(open(args.provenance_file)) if args.provenance_file else None
    snap_sha = provenance.snapshot(os.path.join(args.out, "source_snapshot.tar.gz"), files=files)
    benches = args.benchmarks.split(",") if args.benchmarks else specmod.profile_benchmarks(spec, args.profile)
    for b in benches:
        if b not in spec["benchmarks"]:
            sys.exit(f"unknown benchmark {b}")
    adapters_used = {spec["tools"]["report_tools"][t]["adapter"] for b in benches
                     for t in spec["benchmarks"][b]["tools"]}
    sc = static_checks(spec, adapters_used)
    tc = provenance.toolchain()
    si = {"ncpu": os.cpu_count(), "smp_backends": _smp_backends()}
    ru = g.get("reuse") or {}
    anchor_benches = list(ru.get("anchor_benchmarks", [])) if args.store else []
    anchors = set(store.anchor_configs(spec)) if args.store else set()
    for b_ in reversed(anchor_benches):
        if b_ not in benches:
            print(f"note: anchor benchmark {b_} added (reuse needs the anchors)", flush=True)
            benches.insert(0, b_)
    # anchor benchmarks first: a machine unlike the store's stops the run early
    benches = sorted(benches, key=lambda b: (b not in anchor_benches, benches.index(b)))
    last_anchor = next((b_ for b_ in reversed(benches) if b_ in anchor_benches), None)
    ctx = {"bin_dir": args.bin_dir, "framework_dir": FRAMEWORK, "cache_dir": os.path.join(args.out, "_fields"),
           "syscheck": (not args.no_syscheck) and syscheck.available(),
           "syscheck_dir": os.path.join(args.out, "syscheck"),
           "store": os.path.abspath(args.store) if args.store else "", "environment": e, "toolchain": tc,
           "sweep_inputs": si, "env_fp": store.env_fingerprint(e, tc, si), "anchors": anchors,
           "max_drift": float(ru.get("max_drift", 0.03))}
    ctx["exclude_ours"] = args.exclude_ours
    os.makedirs(ctx["syscheck_dir"], exist_ok=True)
    run = {"run_id": f"{platform.node()}-{time.strftime('%Y%m%dT%H%M%S')}", "profile": args.profile,
           "spec_hash": spec["hash"], "policy_version": g["policy_version"], "environment": e,
           "environment_checks": checks, "source_tree_hash": th, "source_files": len(files),
           "launch_provenance": launch, "git": provenance.git_state(), "snapshot_sha256": snap_sha,
           "static_checks": sc, "benchmarks": benches, "syscheck_enabled": ctx["syscheck"],
           "toolchain": tc, "sweep_inputs": si, "store_used": bool(args.store),
           "exclude_ours": args.exclude_ours,
           "artifact_manifest": json.load(open(args.artifact_manifest)) if args.artifact_manifest else None,
           "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    out = {k: open(os.path.join(args.out, f"{k}.jsonl"), "w") for k in ("measurements", "sweeps", "geometry", "syscheck")}
    out["artifacts"], out["orders"], out["keys"], out["anchors"] = {}, {}, {}, []
    rng = random.Random(g["stats"]["rng_seed"])
    for bid in benches:
        try:
            run_benchmark(spec, bid, ctx, args, out, rng)
            if bid == last_anchor and out["anchors"]:
                med = stats.summarize([a_["drift"] for a_ in out["anchors"]])["median"]
                print(f"    anchors: median drift {100 * med:+.2f}% over {len(out['anchors'])} configurations "
                      f"(max {100 * ctx['max_drift']:.1f}%)", flush=True)
                if abs(med) > ctx["max_drift"]:
                    raise AnchorDrift(f"anchor configurations differ from the store by {100 * med:+.2f}% (median "
                                      f"over {len(out['anchors'])}; max {100 * ctx['max_drift']:.1f}%): this machine "
                                      f"is not like the one the store was measured on; rerun without --store")
        except AnchorDrift as ex:
            run["aborted"] = str(ex)          # the record is complete up to here; the auditor says what is missing
            _finish(run, out, args)
            sys.exit(str(ex))
        for f in ("measurements", "sweeps", "geometry", "syscheck"):
            out[f].flush()
    _finish(run, out, args)
    import shutil
    shutil.rmtree(ctx["cache_dir"], ignore_errors=True)
    print(f"=== done: {args.out}")


if __name__ == "__main__":
    main()
