# November 2026 reference run

AWS EC2 `c7i.metal-24xl` (dedicated bare-metal server, Intel Xeon Platinum
8488C, 48 cores / 96 threads), Ubuntu 24.04, turbo disabled, performance
governor; 30 benchmarks, 394 configurations. The conformance auditor's
verdict is PASS (`attestation.json`).

| File | Contents |
|---|---|
| `measurements.jsonl` | one record per configuration: all 30 timed samples, summary statistics, versions, parameters |
| `geometry.jsonl` | summaries of each library's output (contour length, vertex counts per level; band areas) |
| `sweeps.jsonl` | thread-count sweeps of the multi-threaded configurations |
| `syscheck.jsonl` | syscall-trace verdicts per configuration (the traces themselves are not published) |
| `run.json` | environment, toolchain, benchmark list, round orders, result-store keys |
| `artifact_manifest.json` | sha256 of every binary and library that ran |
| `attestation.json` | the auditor's checks C01–C17 and their findings |
| `run.log` | the console log of the run |

33 configurations were measured in this run; 361 were reused from an
earlier November 2026 run on the same machine type, with the same code, data and method
(their records carry `reused` with the original run). Before reusing, the run
re-measured every configuration of the other libraries on three small
benchmarks; their median differed by −0.30% from the kept results.

Configurations whose run-to-run variation exceeded 5% are listed in the
attestation (check C09, `variability`); they are used as measured.

The source snapshot of the run is not published: it contains isosweep's
source code.
