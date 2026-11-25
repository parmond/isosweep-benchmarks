"""Spec loading, strict validation and hashing.

The spec (bench/framework/spec/) is the single definition of every benchmark.
Validation is strict on purpose: unknown keys, dangling references and
missing fields are errors, so a typo cannot silently change what is measured.
"""
import glob
import hashlib
import os

import yaml

FRAMEWORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_DIR = os.path.join(FRAMEWORK, "spec")


class SpecError(Exception):
    pass


def _need(d, keys, where):
    for k in keys:
        if k not in d:
            raise SpecError(f"{where}: missing required key '{k}'")


def _only(d, keys, where):
    extra = set(d) - set(keys)
    if extra:
        raise SpecError(f"{where}: unknown key(s) {sorted(extra)}")


def spec_files(spec_dir=SPEC_DIR):
    files = [os.path.join(spec_dir, "global.yaml"), os.path.join(spec_dir, "tools.yaml")]
    files += sorted(glob.glob(os.path.join(spec_dir, "benchmarks", "*.yaml")))
    return files


def spec_hash(spec_dir=SPEC_DIR):
    """sha256 over (relative path, content) of every spec file."""
    h = hashlib.sha256()
    for f in spec_files(spec_dir):
        h.update(os.path.relpath(f, spec_dir).encode() + b"\0")
        with open(f, "rb") as fh:
            h.update(fh.read() + b"\0")
    return h.hexdigest()


def load(spec_dir=SPEC_DIR):
    g = yaml.safe_load(open(os.path.join(spec_dir, "global.yaml")))
    t = yaml.safe_load(open(os.path.join(spec_dir, "tools.yaml")))
    benches = {}
    for f in sorted(glob.glob(os.path.join(spec_dir, "benchmarks", "*.yaml"))):
        b = yaml.safe_load(open(f))
        if b["id"] != os.path.splitext(os.path.basename(f))[0]:
            raise SpecError(f"{f}: id '{b['id']}' does not match file name")
        benches[b["id"]] = b
    spec = {"global": g, "tools": t, "benchmarks": benches,
            "hash": spec_hash(spec_dir), "dir": spec_dir}
    validate(spec)
    return spec


ADAPTER_KEYS = {"kind", "impl", "product_surface", "version", "artifacts", "compute_allowlist",
                "output_class", "evidence", "notes", "family", "coords", "precision", "work",
                "threads_reported"}
WORK_KEYS = {"input_dtype", "timed_conversions", "result", "extra_outputs", "assembly", "differences"}
DIFF_KEYS = {"what", "direction", "measured_by", "bound"}
REPORT_KEYS = {"label", "adapter", "variants", "select", "waivers"}
VARIANT_KEYS = {"id", "params", "engine", "max_threads"}
WAIVER_KEYS = {"check", "direction", "justification", "magnitude", "max_rel", "bound"}
WAIVER_BOUNDS = {"border_perimeter_x_levels", "border_perimeter"}
WAIVER_CHECKS = {"W1_total_length", "W1_total_points", "W1_level_length", "W1_level_points",
                 "W2_feature_count", "W2_unjoined_ends", "W3_accuracy",
                 "B1_total_area", "B1_all_bands", "B1_band_area", "B1_band_points", "B2_band_polygons", "B3_accuracy", "triangle_count", "extra_output"}
WAIVER_DIRECTIONS = {"favours_competitor", "favours_isosweep", "neutral"}
BENCH_KEYS = {"id", "title", "kind", "use_case", "also_use_cases", "published", "data", "levels",
              "tools", "rationale", "validation", "levels_label", "data_label"}
KINDS = {"contour2d": "polylines.assembled", "bands2d": "polygons.bands", "surface3d": "mesh.triangles"}


def validate(spec):
    g, t, benches = spec["global"], spec["tools"], spec["benchmarks"]
    _need(g, ["schema", "timed_region", "static_denylist", "syscall_audit", "stats", "sweeps",
              "validation_defaults", "environments", "publication", "profiles", "process_env",
              "preflight_only_benchmarks"], "global.yaml")
    adapters, reports = t["adapters"], t["report_tools"]
    for name, a in adapters.items():
        where = f"tools.yaml adapters.{name}"
        _only(a, ADAPTER_KEYS, where)
        _need(a, ["kind", "impl", "version", "compute_allowlist", "output_class"], where)
        if a["kind"] not in ("ours", "competitor"):
            raise SpecError(f"{where}: kind must be ours|competitor")
        if a["kind"] == "competitor" and not a.get("evidence") and a["output_class"] == "polylines.assembled" \
                and name not in ("skimage_find_contours",):
            raise SpecError(f"{where}: competitor needs 'evidence' for its configuration")
        if not a["compute_allowlist"]:
            raise SpecError(f"{where}: empty compute_allowlist")
        if a["output_class"] in ("polylines.assembled", "polygons.bands"):
            _need(a, ["family", "coords", "precision", "work"], where)
            from .geometry import FRAMES
            if a["coords"] not in FRAMES:
                raise SpecError(f"{where}: coords must be one of {sorted(FRAMES)}")
            if a["precision"] not in ("float32", "float64"):
                raise SpecError(f"{where}: precision must be float32|float64")
            _only(a["work"], WORK_KEYS, f"{where}.work")
            _need(a["work"], ["input_dtype", "result", "assembly"], f"{where}.work")
            for d in a["work"].get("differences", []):
                _only(d, DIFF_KEYS, f"{where}.work.differences")
                _need(d, ["what", "direction", "measured_by"], f"{where}.work.differences")
                if d["direction"] not in WAIVER_DIRECTIONS:
                    raise SpecError(f"{where}: bad difference direction {d['direction']}")
                if d["direction"] == "favours_isosweep" and "bound" not in d:
                    raise SpecError(f"{where}: a difference favouring isosweep must state its bound")
    for name, r in reports.items():
        where = f"tools.yaml report_tools.{name}"
        _only(r, REPORT_KEYS, where)
        _need(r, ["label", "adapter", "variants"], where)
        if r["adapter"] not in adapters:
            raise SpecError(f"{where}: unknown adapter '{r['adapter']}'")
        ours = adapters[r["adapter"]]["kind"] == "ours"
        ids = set()
        for v in r["variants"]:
            _only(v, VARIANT_KEYS, f"{where}.variants")
            _need(v, ["id", "params"], f"{where}.variants")
            if v["id"] in ids:
                raise SpecError(f"{where}: duplicate variant id {v['id']}")
            ids.add(v["id"])
            if ours and "engine" not in v:
                raise SpecError(f"{where}.{v['id']}: our variants must declare the expected engine")
        if len(r["variants"]) > 1 and r.get("select") != "fastest_variant":
            raise SpecError(f"{where}: several variants need select: fastest_variant")
        for w in r.get("waivers", []):
            _only(w, WAIVER_KEYS, f"{where}.waivers")
            _need(w, ["check", "direction", "justification"], f"{where}.waivers")
            if w["check"] not in WAIVER_CHECKS or w["direction"] not in WAIVER_DIRECTIONS:
                raise SpecError(f"{where}: bad waiver {w}")
            if w["direction"] == "favours_isosweep":
                raise SpecError(f"{where}: a difference favouring isosweep can never be waived (fix or exclude)")
            if "max_rel" not in w:
                raise SpecError(f"{where}: every waiver must be bounded (max_rel) (R4)")
    for bid, b in benches.items():
        where = f"benchmarks/{bid}.yaml"
        _only(b, BENCH_KEYS, where)
        _need(b, ["id", "title", "kind", "use_case", "published", "data", "levels", "tools", "rationale",
                  "levels_label", "data_label"], where)
        if b["kind"] not in KINDS:
            raise SpecError(f"{where}: kind must be one of {list(KINDS)}")
        for tool in b["tools"]:
            if tool not in reports:
                raise SpecError(f"{where}: unknown report tool '{tool}'")
            oc = adapters[reports[tool]["adapter"]]["output_class"]
            if oc != KINDS[b["kind"]]:
                raise SpecError(f"{where}: tool {tool} output_class {oc} != {KINDS[b['kind']]} (not like-for-like)")
        d = b["data"]
        if "dataset" in d and "sha256" not in d:
            raise SpecError(f"{where}: real datasets must pin sha256")
        if not (("generator" in d) ^ ("dataset" in d)):
            raise SpecError(f"{where}: data needs exactly one of generator|dataset")
        lv = b["levels"]
        if len(set(lv) & {"count", "interval", "values"}) != 1:
            raise SpecError(f"{where}: levels needs exactly one of count|interval|values")
    for prof, p in g["profiles"].items():
        bl = p["benchmarks"]
        if bl != "all":
            for bid in bl:
                if bid not in benches:
                    raise SpecError(f"global.yaml profiles.{prof}: unknown benchmark {bid}")
    return True


def expand_sweeps(params, spec, ncpu, smp_backends):
    """{"nthreads": {"sweep": "threads"}} → (list of concrete param dicts, swept?).
    Shared by the harness and the auditor (C14 re-derives every candidate)."""
    import itertools
    cands = [c for c in spec["global"]["sweeps"]["thread_candidates"] if c <= ncpu]
    keys, choices = [], []
    for k, v in params.items():
        if isinstance(v, dict) and "sweep" in v:
            s = v["sweep"]
            if s == "threads":
                vals = cands
            elif s == "threads_min2":
                vals = [c for c in cands if c >= 2]
            elif s == "smp_backends":
                vals = list(smp_backends)
            elif isinstance(s, list):
                vals = s
            else:
                raise ValueError(f"unknown sweep {s}")
            keys.append(k)
            choices.append(vals)
    if not keys:
        return [dict(params)], False
    out = []
    for combo in itertools.product(*choices):
        p = dict(params)
        p.update(dict(zip(keys, combo)))
        out.append(p)
    return out, True


def profile_benchmarks(spec, profile):
    """The benchmark set a profile runs (R5: reference and preflight always run
    exactly this set; only indicative runs may take a subset)."""
    prof = spec["global"]["profiles"][profile]
    if prof["benchmarks"] == "all":
        return [b for b in sorted(spec["benchmarks"]) if b not in spec["global"]["preflight_only_benchmarks"]]
    return list(prof["benchmarks"])


def configs_for(spec, bench_id):
    """Expand a benchmark into measured configurations: (report_tool, variant)."""
    t = spec["tools"]
    out = []
    for rt in spec["benchmarks"][bench_id]["tools"]:
        r = t["report_tools"][rt]
        for v in r["variants"]:
            out.append({"report_tool": rt, "variant": v["id"], "adapter": r["adapter"],
                        "params": v["params"], "engine": v.get("engine"),
                        "config_id": f"{rt}/{v['id']}"})
    return out
