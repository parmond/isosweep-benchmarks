# isosweep benchmark harness

The harness, specifications and results behind every performance figure on
[isosweep.com](https://www.isosweep.com/benchmarks): head-to-head timings of
contour-line and filled-contour extraction against GDAL, VTK, ContourPy,
scikit-image and contour-rs on real and synthetic grids.

Everything needed to repeat the measurements of the other libraries is here.
isosweep itself is commercial software of Parmond AB and is not part of this
repository; its timings can be reproduced with an
[evaluation build](https://www.isosweep.com/evaluate).

## What is measured

- **Computation only.** Every library runs in-process on the same grid,
  already in memory, at the same levels. Start-up, file I/O and plotting are
  outside the timed region, for every library.
- **Every library at its best.** The fastest documented mode of each tool,
  plus thread-count sweeps for every multi-threaded one.
- **Like-for-like output, checked.** Each library's contours are compared with
  the consensus of the others (length, vertex counts, per level).
- **Statistics.** One warm-up, then 30 timed rounds per configuration, each
  round in a fresh random order; the median is reported and run-to-run
  variation is recorded.

The rules are in [`bench/framework/README.md`](bench/framework/README.md); the
policy itself is machine-readable in
[`bench/framework/spec/global.yaml`](bench/framework/spec/global.yaml), and
every benchmark is one file in `bench/framework/spec/benchmarks/`.

## Repository layout

| Path | Contents |
|---|---|
| `bench/framework/run.py` | the harness: runs a profile of benchmarks and writes JSON-lines records |
| `bench/framework/audit/` | the conformance auditor (checks C01–C17) and its self-test |
| `bench/framework/isobench/` | adapters, statistics, data loading, output comparison, result store |
| `bench/framework/spec/` | the benchmark specifications and the measurement policy |
| `bench/framework/workers/` | native workers for GDAL and contour-rs (and isosweep's C API, built only with an evaluation bundle) |
| `bench/framework/cloud/` | node setup (pinned GDAL builds, Python packages, Rust toolchain) and run scripts |
| `bench/aws_bench.sh` | the orchestrator used for the published runs (AWS bare metal) |
| `tools/fieldgen/` | the deterministic generator of the synthetic industry fields |
| `datasets/` | provenance, checksums and a fetch script for the seven real datasets |
| `results/` | the published, audited reference results |

## Reproduce it

On Ubuntu 24.04 (the reference platform), from a checkout in `~/isosweep-benchmarks`:

```bash
# 1. the datasets (needs GDAL, numpy, scipy)
python3 datasets/fetch_datasets.py --out ~/landscape_data

# 2. pinned GDAL 3.13.3 and GDAL development builds, the Python packages,
#    the Rust worker and the field generator
REPO_DIR=~/isosweep-benchmarks bash bench/framework/cloud/setup_ubuntu.sh

# 3. run and audit (isosweep is left out without an evaluation bundle)
REPO_DIR=~/isosweep-benchmarks bash bench/framework/cloud/run_node.sh indicative
```

Results land in `~/fw_results`: `measurements.jsonl` (every sample),
`geometry.jsonl` (output summaries), `run.json` (environment) and
`attestation.json` (the auditor's verdict). The `indicative` profile is
any machine; `reference` adds the controls of the published runs (bare metal,
fixed clocks, syscall tracing).

With an isosweep evaluation bundle, set `ISOSWEEP_SDK=/path/to/bundle` for
step 2 and its configurations are built and measured too.

To repeat the published runs exactly, `bench/aws_bench.sh` provisions an AWS
EC2 `c7i.metal-24xl` bare-metal server, runs a preflight on a small instance
first, and tears everything down afterwards (it needs your own AWS
credentials and costs about $5 per hour of bare metal).

## Published results

`results/2026-11-reference/` holds the November 2026 reference run on AWS EC2
`c7i.metal-24xl` (Intel Xeon Platinum 8488C, turbo disabled): every
measurement, the output summaries, the environment record and the auditor's
attestation (verdict PASS). A run reuses results measured earlier with the
same code, data, method and machine, so some records carry the earlier run's
date; see the `reused` field.

## Licence

Apache License 2.0, copyright Parmond AB; see [LICENSE](LICENSE) and
[NOTICE](NOTICE). The datasets and the libraries benchmarked keep their own
licences.

Questions and corrections are welcome as GitHub issues, or at
hello@parmond.com.
