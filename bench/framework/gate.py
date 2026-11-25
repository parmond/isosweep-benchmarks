#!/usr/bin/env python3
# Copyright 2026 Parmond AB
# SPDX-License-Identifier: Apache-2.0
"""Pre-launch gate for reference runs (the pre-flight rule).

    gate.py [--preflight-root bench/results/framework/preflight]

Allows a bare-metal reference run only if a preflight run exists whose
attestation is PASS, whose live auditor self-test passed, and whose source
tree hash AND spec hash equal the CURRENT local tree and spec. Any change after
the preflight (code or spec) invalidates it. R8: the preflight must have run on
the same instance family (c7i / c7g) and its artifact bundle must be present
with the hash it recorded: the reference installs that bundle unchanged.
Prints the matching preflight dir.

R13 (correctness before cost): also requires a PASSing local validation
(validate_local.py) of the current sources of our code and the current spec:
our output on every benchmark agrees with the competitors' stored outputs.
"""
import hashlib
import argparse
import glob
import json
import os
import sys

FW = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FW)
from isobench import provenance, spec as specmod  # noqa: E402

REPO = os.path.abspath(os.path.join(FW, "..", ".."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preflight-root", default=os.path.join(REPO, "bench", "results", "framework", "preflight"))
    ap.add_argument("--instance-type", required=True, help="the reference instance type (e.g. c7i.metal-24xl)")
    ap.add_argument("--require-validation", action="store_true",
                    help="also require a PASSing local validation (validate_local.py) of the current sources "
                         "and spec (the orchestrator always passes this)")
    ap.add_argument("--validation-dir", default=os.path.join(REPO, "bench", "results", "framework", "validation"))
    a = ap.parse_args()
    family = a.instance_type.split(".")[0]
    tree, sh = provenance.tree_hash(), specmod.spec_hash()
    from isobench import store
    vp = os.path.join(a.validation_dir, store.ours_source_hash() + ".json")
    v = json.load(open(vp)) if os.path.exists(vp) else {}
    if a.require_validation and (v.get("verdict") != "PASS" or v.get("spec_hash") != sh):
        print(f"GATE CLOSED: no passing local validation of the current sources and spec "
              f"(run bench/framework/validate_local.py; found: {v.get('verdict') or 'none'})", file=sys.stderr)
        return 1
    cands = sorted(glob.glob(os.path.join(a.preflight_root, "*", "*", "attestation.json")), reverse=True)
    for att_path in cands:
        att = json.load(open(att_path))
        st = att.get("auditor_selftest", {})
        if (att.get("verdict") == "PASS" and att.get("profile") == "preflight" and st.get("ok") is True
                and st.get("live") is True and att.get("source_tree_hash") == tree and att.get("spec_hash") == sh):
            d = os.path.dirname(att_path)
            run = json.load(open(os.path.join(d, "run.json")))
            it = (run.get("environment") or {}).get("instance_type") or ""
            man = run.get("artifact_manifest") or {}
            bundle = os.path.join(d, "artifacts.tar.gz")
            if it.split(".")[0] != family or man.get("mode") != "built" or not os.path.exists(bundle):
                continue
            h = hashlib.sha256()
            with open(bundle, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != man.get("bundle_sha256"):
                continue
            print(d)
            return 0
    print(f"GATE CLOSED: no passing preflight (instance family {family}, artifact bundle present) "
          f"for current tree {tree[:12]} / spec {sh[:12]} "
          f"(checked {len(cands)} preflight attestation(s))", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
