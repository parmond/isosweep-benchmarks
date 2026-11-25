#!/usr/bin/env python3
"""Admit a finished run's records into the result store (isobench/store.py).

    store_admit.py RESULTS_DIR [--store DIR] [--preflight DIR] [--data-dir DIR]

Keys describe what the run executed: they are computed from ITS source
snapshot and spec. The checks apply TODAY's evaluation rules (tolerances,
budgets, waivers, variation disclosure) to its records, every failure listed.
A record is admitted only if the run passed every run-level check
(environment, provenance, completeness, preflight link; the spec parts that
shape a measurement are in each key, so the spec hash itself is not
required) and the record passed every check that applies to one record
(versions, engine, static, syscalls, statistics, effective configuration,
data, sweeps, resources, work). Variation above the disclosure threshold is
recorded with the record, not a reason to refuse it. Cross-record checks (output
agreement, round order) are re-evaluated by the auditor over each run that
assembles records. Prints what was admitted and why the rest was not.

Tie budgets (geometry.tie_budget) are added where this machine rebuilds the
run's field bit for bit (datasets; generated fields when libm agrees), for
runs made before the tie budget existed.
"""
import argparse
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time

FW = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FW)
sys.path.insert(0, os.path.join(FW, "audit"))
from isobench import data, geometry, polygons, spec as specmod, store  # noqa: E402
import conformance  # noqa: E402

RUN_LEVEL = ("C02_environment", "C03_completeness", "C11_provenance", "C13_preflight")
RECORD_LEVEL = ("C04_versions", "C05_engine", "C06_static", "C07_syscalls", "C09_statistics", "C10_effective",
                "C12_data", "C14_sweeps", "C15_resources", "C16_work")


def _jsonl(p):
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []


def record_failures(att, meas, spec):
    """{(benchmark, config_id): [check ids]} from the audit's full failure lists."""
    configs = {m["config_id"] for m in meas}
    by_bench = {}
    for m in meas:
        by_bench.setdefault(m["benchmark"], set()).add(m["config_id"])
    out = {}

    def mark(b, cid, check):
        out.setdefault((b, cid), set()).add(check)
    for check in RECORD_LEVEL:
        c = att["checks"].get(check, {})
        if c.get("ok"):
            continue
        entries = c.get("failures") or c.get("mismatches") or c.get("violations") or []
        if check == "C06_static":
            bad = {x[0] if isinstance(x, (list, tuple)) else x for x in entries}
            for m in meas:
                if m["adapter"] in bad:
                    mark(m["benchmark"], m["config_id"], check)
            continue
        for x in entries:
            if isinstance(x, (list, tuple)) and x and isinstance(x[0], (list, tuple)):
                mark(x[0][0], x[0][1], check)                       # C14: ((b, cid), ...)
            elif isinstance(x, (list, tuple)) and len(x) >= 2 and x[0] in by_bench and x[1] in configs:
                mark(x[0], x[1], check)
            elif isinstance(x, (list, tuple)) and x and x[0] in configs:
                for b, cs in by_bench.items():                     # C10, C05: per configuration
                    if x[0] in cs:
                        mark(b, x[0], check)
            elif isinstance(x, (list, tuple)) and x and x[0] in by_bench:
                if check == "C09_statistics":
                    continue                                        # round order: cross-record
                for cid in by_bench[x[0]]:                          # C12 benchmark-level: all of it
                    mark(x[0], cid, check)
            else:
                for b, cs in by_bench.items():                     # unattributable: reject everything
                    for cid in cs:
                        mark(b, cid, check + " (unattributed)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("--store", default=store.STORE)
    ap.add_argument("--preflight", default=None)
    ap.add_argument("--data-dir", default=os.path.expanduser("~/landscape_data"))
    a = ap.parse_args()
    src = os.path.abspath(a.results_dir)
    run = json.load(open(os.path.join(src, "run.json")))
    tmp = tempfile.mkdtemp(prefix="isobench-admit-")
    root = os.path.join(tmp, "tree")
    with tarfile.open(os.path.join(src, "source_snapshot.tar.gz")) as tar:
        tar.extractall(root, filter="data")
    spec = specmod.load(os.path.join(root, "bench", "framework", "spec"))     # what was run: keys
    now = specmod.load()                                                        # today's rules: checks
    # a working copy: the run's records plus tie budgets this machine can rebuild
    work = os.path.join(tmp, "run")
    os.makedirs(work)
    for f in os.listdir(src):
        if f.endswith((".json", ".jsonl", ".gz")):
            shutil.copy(os.path.join(src, f), work)
    os.symlink(os.path.join(src, "syscheck"), os.path.join(work, "syscheck"))
    meas = _jsonl(os.path.join(work, "measurements.jsonl"))
    geo = _jsonl(os.path.join(work, "geometry.jsonl"))
    have_ties = {x["benchmark"] for x in geo if "tie_budget" in x}
    added = []
    with open(os.path.join(work, "geometry.jsonl"), "a") as gf:
        for bid in run["benchmarks"]:
            b = now["benchmarks"][bid]
            if b["kind"] not in ("contour2d", "bands2d") or bid in have_ties:
                continue
            want = next((m["data"]["field_sha256"] for m in meas if m["benchmark"] == bid), None)
            try:
                z, _, dinfo = data.load_field(b, a.data_dir, os.path.join(tmp, "fields"))
            except Exception as e:  # noqa: BLE001
                print(f"  {bid}: field not rebuilt here ({e}); no tie budget")
                continue
            if dinfo.get("field_sha256") != want:
                print(f"  {bid}: this machine's field differs from the run's (libm); no tie budget")
                continue
            lv = data.make_levels(b, z)
            if b["kind"] == "contour2d":
                tb = geometry.tie_budget(z, lv)
                gf.write(json.dumps({"benchmark": bid, "tie_budget": tb}) + "\n")
                added.append((bid, sum(v["nodes"] for v in tb.values())))
            else:
                tb = polygons.tie_area_budget(z, lv)
                gf.write(json.dumps({"benchmark": bid, "tie_area_budget": tb}) + "\n")
                added.append((bid, sum(v["nodes"] for v in tb)))
    for bid, n in added:
        print(f"  {bid}: tie budget added ({n} tied nodes)")
    conformance.FULL = True
    att = conformance.audit(work, spec=now, preflight_dir=a.preflight)
    print(f"audit of {os.path.relpath(src)} under today's rules: {att['verdict']}"
          + ("" if att["checks"]["C01_spec_hash"]["ok"] else " (spec changed since the run; keys cover what matters)"))
    for cid, c in att["checks"].items():
        if not c["ok"]:
            print(f"  {cid} FAIL ({len(c.get('failures') or c.get('mismatches') or c.get('violations') or [])} finding(s))")
    run_bad = [c for c in RUN_LEVEL if not att["checks"].get(c, {}).get("ok")]
    if run_bad:
        sys.exit(f"not admitted: run-level checks failed: {run_bad}")
    fails = record_failures(att, meas, now)
    var = {(v[0], v[1]): v[2] for v in att["checks"].get("C09_statistics", {}).get("variability", [])}
    sweeps = {(s["benchmark"], s["config_id"]): s for s in _jsonl(os.path.join(work, "sweeps.jsonl"))}
    sysc = {(s["benchmark"], s["config_id"]): s for s in _jsonl(os.path.join(work, "syscheck.jsonl"))}
    geos = {(x["benchmark"], x["config_id"]): x for x in geo if "config_id" in x}
    env_fp = store.env_fingerprint(run["environment"], run.get("toolchain"), run.get("sweep_inputs"))
    source = {"results_dir": os.path.relpath(src, store.REPO), "run_id": run.get("run_id"),
              "profile": run.get("profile"), "started": run.get("started"),
              "snapshot_sha256": run.get("snapshot_sha256"), "source_tree_hash": run.get("source_tree_hash"),
              "spec_hash": run.get("spec_hash"), "instance_id": (run.get("environment") or {}).get("instance_id")}
    cfgs = {}
    for bid in run["benchmarks"]:
        for c in specmod.configs_for(spec, bid):
            cfgs[(bid, c["config_id"])] = c
    admitted, rejected = 0, []
    for m in meas:
        k = (m["benchmark"], m["config_id"])
        if k in fails:
            rejected.append((k, sorted(fails[k])))
            continue
        shas = {n.split("::", 1)[1]: v["sha256"] for n, v in (run.get("artifacts") or {}).items()
                if n.startswith(m["config_id"] + "::")}
        tk, ok_, parts = store.keys(spec, m["benchmark"], cfgs[k], m["data"]["field_sha256"], shas, m["version"],
                                    run["environment"], run.get("toolchain"), run.get("sweep_inputs"), root=root)
        sy = sysc.get(k)
        entry = {"timing_key": tk, "output_key": ok_, "key_parts": parts, "env": env_fp,
                 "benchmark": m["benchmark"], "config_id": m["config_id"], "measurement": m,
                 "sweep": sweeps.get(k), "syscheck": sy, "geometry": geos.get(k),
                 "record_checks": {c: "ok" for c in RECORD_LEVEL}, "source": source,
                 "variability": {"cv": m["summary"]["cv"], "disclosed": k in var},
                 "admitted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        store.put(a.store, env_fp, entry, log_src=os.path.join(src, sy["log"]) if sy and sy.get("log") else None)
        if geos.get(k):
            store.put_output(a.store, ok_, {"output_key": ok_, "benchmark": m["benchmark"],
                                            "config_id": m["config_id"], "summary": geos[k]["summary"],
                                            "source": source})
        admitted += 1
    print(f"admitted {admitted} of {len(meas)} records into {os.path.relpath(a.store, store.REPO)} "
          f"(machine {env_fp[:12]}); variation disclosed for {len(var)}")
    for k, why in rejected:
        print(f"  not admitted: {k[0]} / {k[1]}: {', '.join(why)}")
    c08 = att["checks"].get("C08_output", {})
    print(f"output agreement over this run (with tie budgets): {'ok' if c08.get('ok') else 'FAIL'}"
          + ("" if c08.get("ok") else f" {c08.get('failures')[:6]}"))
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
