"""Result store: every (benchmark, configuration) is measured once per code,
data, method and machine, and reused by later runs while none of these
changed (R13). Competitors do not change between our releases; re-measuring
them on the same data and machine adds nothing but cost.

Two keys, both content hashes:

- timing key: what produced a measurement. The data (benchmark data and
  levels spec, the field's sha256, the level-making code), the configuration
  (config id, variant, the adapter's definition without its descriptive
  fields), the tool's code (its adapter class and base classes, or its worker
  source), the tool's build (sha256 of the artifacts it loaded, its reported
  version, the pip freeze for Python tools), the measurement method (the
  sample-timing functions, the stats, syscheck and validate_one code, and of
  the spec's policy only what decides how samples are taken: rounds, warm-up,
  order, loop calibration, timing mode, the timed region, sweeps, the process
  environment, how the syscall trace is recorded)
  and the machine (instance type, CPU, cores, kernel, OS, libc, Python,
  turbo, governor, THP, ASLR, virtualization, sweep inputs).
- output key: what produced an output (no method, no machine): data,
  configuration, tool code and build. For our tools the build is identified
  by the library's sources (its output is bitwise the same on every platform:
  make oracle-portable), so a local validation can check our output against
  stored competitor outputs.

Layout under bench/results/framework/store/:
  timing/<env>/<timing key>.json   measurement, sweep, syscheck, geometry,
                                   the key's parts, the record's own audit
                                   verdict, provenance of the run it came from
  timing/<env>/logs/<key>.strace.gz
  outputs/<output key>.json        geometry summary (+ provenance)
  competitor_bundles/<build key>.tar.gz   competitor builds, reused byte for byte
A record enters only if it passed every check that applies to one record
(versions, engine, syscalls, statistics, effective config, data, sweeps,
resources, work); cross-record checks (output agreement, round order) are
re-evaluated over the assembled set by the auditor.
"""
import ast
import glob
import hashlib
import json
import os
import shutil

FW = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO = os.path.abspath(os.path.join(FW, "..", ".."))
STORE = os.path.join(REPO, "bench", "results", "framework", "store")
SCHEMA = 1
DESCRIPTIVE = ("notes", "evidence", "product_surface", "work")
OURS_SOURCES = ["Makefile", "include/*.h", "src/*.c", "src/*.h", "python/setup.py",
                "python/isocurves/*.py", "python/isocurves/*.c", "bench/framework/workers/isosweep_worker.c"]
METHOD_FILES = ["bench/framework/isobench/stats.py", "bench/framework/isobench/syscheck.py",
                "bench/framework/validate_one.py"]
METHOD_FUNCTIONS = ("measure", "calibrate")       # bench/framework/run.py: the sample-timing code
# Only what decides how samples are taken is part of a key; evaluation rules (CV
# disclosure, tolerances, allowlists, budgets, waivers) are re-applied by the
# auditor to stored records, so changing them never forces a re-measurement.
MEASURE_STATS = ("warmup_runs", "rounds", "order", "rng_seed", "min_sample_ms", "max_loops",
                 "per_call_timing", "gc_disabled_in_sample", "record_cpu_time")
MEASURE_TIMED = ("definition", "prepare_may", "forbidden_in_timed_region")
MEASURE_TRACE = ("tool", "marker")
ENV_KEYS = ("instance_type", "cpu", "logical_cpus", "physical_cores", "kernel", "turbo_disabled", "governors",
            "thp", "aslr", "virtualization", "hypervisor_cpu_flag")


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=str)


def h(o):
    return hashlib.sha256(canon(o).encode()).hexdigest()


def file_sha(path):
    if not os.path.exists(path):
        return None
    d = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            d.update(chunk)
    return d.hexdigest()


def _module(path):
    src = open(path).read()
    return src, ast.parse(src)


def defs_source(path, names):
    """Source text of the top-level functions / classes `names` in a file."""
    src, tree = _module(path)
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            out[node.name] = ast.get_source_segment(src, node)
    return out


def class_chain_source(path, cls):
    """A class's source and that of its base classes defined in the same file
    (the adapter base classes carry the worker protocol and the hooks)."""
    src, tree = _module(path)
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    out, todo = {}, [cls]
    while todo:
        c = todo.pop()
        if c in out or c not in classes:
            continue
        out[c] = ast.get_source_segment(src, classes[c])
        todo += [b.id for b in classes[c].bases if isinstance(b, ast.Name)]
    # module-level helpers the classes call (e.g. _module_file) are part of the code too
    helpers = {n.name: ast.get_source_segment(src, n) for n in tree.body if isinstance(n, ast.FunctionDef)}
    return {"classes": out, "helpers": helpers}


def adapter_code(spec, adapter, root=REPO):
    a = spec["tools"]["adapters"][adapter]
    if "python" in a["impl"]:
        mod, cls = a["impl"]["python"].split(":")
        path = os.path.join(root, "bench", "framework", *mod.split(".")) + ".py"
        return h(class_chain_source(path, cls))
    w = os.path.join(root, "bench", "framework", a["impl"]["worker"])
    files = [w]
    if w.endswith(".rs"):
        crate = os.path.dirname(os.path.dirname(w))
        files += [os.path.join(crate, "Cargo.toml"), os.path.join(crate, "Cargo.lock")]
    # the Python side of the worker protocol: the class ADAPTER_CLASSES maps it to
    ad_py = os.path.join(root, "bench", "framework", "isobench", "adapters.py")
    proto = class_chain_source(ad_py, _registry(ad_py).get(adapter, "WorkerAdapter"))
    return h({"files": {os.path.relpath(f, root): file_sha(f) for f in files}, "protocol": proto})


def _registry(path):
    """adapters.py's ADAPTER_CLASSES as {adapter name: class name}, read from the source."""
    _, tree = _module(path)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ADAPTER_CLASSES"
                                                for t in node.targets):
            return {k.value: v.id for k, v in zip(node.value.keys, node.value.values)
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Name)}
    return {}


def anchor_configs(spec):
    """The anchor configurations: every COMPETITOR configuration of the spec's
    reuse.anchor_benchmarks (ours change by design whenever our code does)."""
    from isobench import spec as specmod
    return [(b, c["config_id"]) for b in (spec["global"].get("reuse") or {}).get("anchor_benchmarks", [])
            for c in specmod.configs_for(spec, b) if not is_ours(spec, c["adapter"])]


def method_fingerprint(spec, root=REPO):
    g = spec["global"]
    policy = {"stats": {k: g["stats"].get(k) for k in MEASURE_STATS},
              "timed_region": {k: g["timed_region"].get(k) for k in MEASURE_TIMED},
              "syscall_trace": {k: g["syscall_audit"].get(k) for k in MEASURE_TRACE},
              "sweeps": g["sweeps"], "process_env": g["process_env"]}
    return h({"functions": defs_source(os.path.join(root, "bench", "framework", "run.py"), METHOD_FUNCTIONS),
              "files": {f: file_sha(os.path.join(root, f)) for f in METHOD_FILES}, "policy": policy})


def data_fingerprint(spec, bid, field_sha256, root=REPO):
    b = spec["benchmarks"][bid]
    return h({"data": b["data"], "levels": b["levels"], "kind": b["kind"], "field_sha256": field_sha256,
              "code": file_sha(os.path.join(root, "bench", "framework", "isobench", "data.py"))})


def config_fingerprint(spec, cfg):
    t = spec["tools"]
    rt = t["report_tools"][cfg["report_tool"]]
    var = next(v for v in rt["variants"] if v["id"] == cfg["variant"])
    ad = {k: v for k, v in t["adapters"][cfg["adapter"]].items() if k not in DESCRIPTIVE}
    return h({"config_id": cfg["config_id"], "adapter": cfg["adapter"], "variant": var, "adapter_def": ad})


def env_fingerprint(env, toolchain, sweep_inputs):
    toolchain = toolchain or {}
    return h({"env": {k: env.get(k) for k in ENV_KEYS}, "os": toolchain.get("os_release"),
              "libc": toolchain.get("libc"), "python": toolchain.get("python"), "sweep_inputs": sweep_inputs})


def ours_source_hash(root=REPO):
    files = sorted({os.path.relpath(p, root) for pat in OURS_SOURCES for p in glob.glob(os.path.join(root, pat))})
    return h({f: file_sha(os.path.join(root, f)) for f in files})


def is_ours(spec, adapter):
    return spec["tools"]["adapters"][adapter]["kind"] == "ours"


def build_identity(spec, cfg, artifact_shas, version, pip_freeze, root=REPO):
    """The tool's build: artifacts it loaded (sha256), its reported version,
    and for Python tools the exact package set."""
    python = "python" in spec["tools"]["adapters"][cfg["adapter"]]["impl"]
    return {"artifacts": dict(sorted(artifact_shas.items())), "version": version,
            "pip": sorted(pip_freeze or []) if python else None}


def keys(spec, bid, cfg, field_sha256, artifact_shas, version, env, toolchain, sweep_inputs, root=REPO):
    """(timing key, output key, parts) for one configuration of one benchmark."""
    toolchain = toolchain or {}
    parts = {
        "schema": SCHEMA,
        "data": data_fingerprint(spec, bid, field_sha256, root),
        "config": config_fingerprint(spec, cfg),
        "code": adapter_code(spec, cfg["adapter"], root),
        "build": h(build_identity(spec, cfg, artifact_shas, version, toolchain.get("pip_freeze"), root)),
        "method": method_fingerprint(spec, root),
        "env": env_fingerprint(env, toolchain, sweep_inputs),
    }
    out_build = ours_source_hash(root) if is_ours(spec, cfg["adapter"]) else parts["build"]
    output_key = h({"schema": SCHEMA, "data": parts["data"], "config": parts["config"], "code": parts["code"],
                    "build": out_build})
    timing_key = h(parts)
    return timing_key, output_key, parts


# ---- the store itself -------------------------------------------------------------
def _env_dir(store, env_fp):
    return os.path.join(store, "timing", env_fp[:16])


def lookup(store, env_fp, timing_key):
    p = os.path.join(_env_dir(store, env_fp), timing_key + ".json")
    return json.load(open(p)) if os.path.exists(p) else None


def lookup_output(store, output_key):
    p = os.path.join(store, "outputs", output_key + ".json")
    return json.load(open(p)) if os.path.exists(p) else None


def put(store, env_fp, entry, log_src=None):
    d = _env_dir(store, env_fp)
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    if log_src and os.path.exists(log_src):
        dst = os.path.join(d, "logs", entry["timing_key"] + ".strace.gz")
        shutil.copyfile(log_src, dst)
        entry["syscheck_log"] = os.path.relpath(dst, store)
    tmp = os.path.join(d, entry["timing_key"] + ".json.tmp")
    json.dump(entry, open(tmp, "w"), indent=1)
    os.replace(tmp, os.path.join(d, entry["timing_key"] + ".json"))


def put_output(store, output_key, entry):
    d = os.path.join(store, "outputs")
    os.makedirs(d, exist_ok=True)
    json.dump(entry, open(os.path.join(d, output_key + ".json"), "w"), indent=1)


def entries(store, env_fp=None):
    pat = os.path.join(store, "timing", env_fp[:16] if env_fp else "*", "*.json")
    for p in sorted(glob.glob(pat)):
        yield json.load(open(p))


# ---- competitor builds and the machine image, reused byte for byte -----------
COMPETITOR_SOURCES = ["bench/framework/cloud/competitors.env", "bench/framework/workers/gdal_worker.c",
                      "bench/framework/workers/tif2f64.c", "bench/framework/workers/rs_worker/src/main.rs",
                      "bench/framework/workers/rs_worker/Cargo.toml", "bench/framework/workers/rs_worker/Cargo.lock"]
COMPETITOR_MEMBERS = ("bin/gdal_worker_", "bin/rs_worker", "bin/tif2f64", "tools/gdal-", "requirements.lock")


def _build_gdal_text(root):
    """The GDAL build recipe (cmake options) inside cloud/setup_ubuntu.sh."""
    s = open(os.path.join(root, "bench", "framework", "cloud", "setup_ubuntu.sh")).read()
    i = s.find("build_gdal() {")
    j = s.find("\n}\n", i)
    return s[i:j] if i >= 0 else ""


def competitor_build_key(root=REPO):
    return h({"files": {f: file_sha(os.path.join(root, f)) for f in COMPETITOR_SOURCES},
              "gdal_recipe": _build_gdal_text(root)})


def competitor_bundle_from(artifacts_tar, store=STORE, root=REPO):
    """Keep the competitor part of a preflight's artifact bundle for later preflights."""
    import tarfile
    key = competitor_build_key(root)
    d = os.path.join(store, "competitor_bundles")
    out = os.path.join(d, key + ".tar.gz")
    if os.path.exists(out):
        return out, False
    os.makedirs(d, exist_ok=True)
    tmp = out + ".tmp"
    with tarfile.open(artifacts_tar) as src, tarfile.open(tmp, "w:gz") as dst:
        for m in sorted(src.getmembers(), key=lambda m: m.name):
            if m.name.startswith(COMPETITOR_MEMBERS):
                m.mtime = 0
                dst.addfile(m, src.extractfile(m) if m.isfile() else None)
    os.replace(tmp, out)
    return out, True


def ami_pin(arch, store=STORE, set_to=None):
    """The machine image results were measured on (its kernel is part of the
    machine key): pinned, so later runs boot the same one."""
    p = os.path.join(store, "ami.json")
    pins = json.load(open(p)) if os.path.exists(p) else {}
    if set_to:
        pins[arch] = set_to
        os.makedirs(store, exist_ok=True)
        json.dump(pins, open(p, "w"), indent=1)
    return pins.get(arch)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "competitor-key":
        print(competitor_build_key())
    elif cmd == "competitor-bundle":        # path of the stored bundle for the current pins, if any
        p = os.path.join(STORE, "competitor_bundles", competitor_build_key() + ".tar.gz")
        print(p if os.path.exists(p) else "")
    elif cmd == "competitor-bundle-from":
        out, new = competitor_bundle_from(sys.argv[2])
        print(f"{'stored' if new else 'already stored'}: {out}")
    elif cmd == "ami-pin":
        print(ami_pin(sys.argv[2], set_to=sys.argv[3] if len(sys.argv) > 3 else None) or "")
    else:
        sys.exit("usage: store.py competitor-key | competitor-bundle | competitor-bundle-from ARTIFACTS.tar.gz "
                 "| ami-pin ARCH [AMI]")
