#!/usr/bin/env python3
# Copyright 2026 Parmond AB
# SPDX-License-Identifier: Apache-2.0
"""Run ONE configuration's compute() between ISOBENCH markers (for strace).

Started by run.py under `strace -f -y`. Everything before the first marker
(imports, loading the field, prepare, activate, a warm-up compute) is outside
the audited window; exactly one compute() happens between the markers.
"""
import argparse
import gc
import json
import os
import sys

FRAMEWORK = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FRAMEWORK)

from isobench import data, spec as specmod  # noqa: E402
from run import adapter_class  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--params", required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--bin-dir", required=True)
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--spec-dir", default=specmod.SPEC_DIR)
    ap.add_argument("--loops", type=int, default=1, help="a full calibrated sample (R6)")
    a = ap.parse_args()
    spec = specmod.load(a.spec_dir)
    b = spec["benchmarks"][a.benchmark]
    z, field_path, _ = data.load_field(b, a.data_dir, a.cache_dir)
    levels = data.make_levels(b, z)
    ctx = {"bin_dir": a.bin_dir, "framework_dir": FRAMEWORK}
    inst = adapter_class(spec, a.adapter)(json.loads(a.params), ctx)
    inst.prepare(z, levels, field_path)
    inst.activate()
    if inst.native:
        inst.run_native(1)                      # warm-up, outside the window
        inst.run_native(a.loops, markers=True)  # markers emitted by the worker itself
    else:
        inst.compute()                          # warm-up (also triggers any lazy imports)
        gc.collect()                            # so no finalizer runs inside the window
        gc.disable()                            # as in the measured samples
        res = None
        os.write(2, b"ISOBENCH_T0\n")
        for i in range(a.loops):
            res = inst.compute()
            if i < a.loops - 1:
                res = None
        os.write(2, b"ISOBENCH_T1\n")
        gc.enable()
    inst.close()


if __name__ == "__main__":
    main()
