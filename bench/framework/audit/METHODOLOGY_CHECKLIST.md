# Audit 2 — methodology vs industry standards and practice

**Scope:** the benchmark specification (`spec/`), harness (`run.py`,
`isobench/`), conformance audit (`audit/conformance.py`), claims audit
(`audit/claims.py`) and how results are presented on isosweep.com.
**Status values:** MET · PARTIAL (met with a stated limitation) · GAP (open).
**Review log:** `REVIEW_LOG.md` (independent agent review, findings, resolutions).

## Sources

- **SIGPLAN** — Berger, Blackburn, Hauswirth, Hicks, *SIGPLAN Empirical Evaluation Checklist* (2018).
- **H&B** — Hoefler & Belli, *Scientific Benchmarking of Parallel Computing Systems: Twelve ways to tell the masses when reporting performance results*, SC'15.
- **Heiser** — G. Heiser, *Systems Benchmarking Crimes* (UNSW, maintained list).
- **SPEC/TPC** — SPEC run & reporting rules (full disclosure, base vs peak); TPC full-disclosure + independent audit principle.
- **MLPerf** — MLCommons rules: fixed reference model ("closed division"), automated submission checker, reproducibility.
- **EU 2006/114/EC** — Directive on misleading and comparative advertising, Art. 4; Sweden: Marknadsföringslagen (2008:486) §18. **FTC** — US substantiation doctrine (reasonable basis before making objective claims).

## Checklist

| # | Requirement (source) | How the framework meets it | Status |
|---|---|---|---|
| 1 | Claims explicit and scoped (SIGPLAN "clearly stated claims") | Every number rendered from attested data; claims registry scopes qualitative claims (e.g. speed claims are 2D contour lines only; 3D/polygons excluded by rule) | MET |
| 2 | Appropriate baseline (SIGPLAN, Heiser "improper baseline") | Baselines are the tools buyers use today (GDAL incl. unreleased master, VTK, ContourPy, scikit-image, contour-rs), each at its documented fastest configuration with evidence links | MET |
| 3 | Fair comparison / competitor not misconfigured (SIGPLAN, Heiser) | Best-config sweeps applied to every multi-threaded tool incl. ours; GDAL best of two APIs; disclosure of all sweep candidates (C14); input-dtype rule documented (favours competitors where asymmetric) | MET |
| 4 | Same task / like-for-like output (MLPerf closed division) | Output class declared per adapter; spec rejects mixed classes; geometry consensus check (C08); waivers explicit, bounded, direction-labelled | MET |
| 5 | Measure the thing claimed (Heiser "measuring the wrong thing") | Computation-only timed region; static allowlist (C06) + strace window audit (C07); engine identity reported by the product and asserted (C05) | MET |
| 6 | Principled benchmark choice; no selective benchmarking (SIGPLAN, Heiser) | Benchmarks pre-registered in spec before reference runs; completeness check (C03); every published case reported on /benchmarks; real third-party data where available | MET |
| 7 | Report subsets with reasons (H&B R2) | Unpublished: 3D isosurfaces (not competitive vs VTK multi-threaded); excluded tools listed with reasons in global.yaml and on /benchmarks | PARTIAL — 3D omission should be stated on /benchmarks (see review) |
| 8 | Speed-up base case and absolute times (H&B R1) | Tables show absolute medians for every tool + ratios; "1-thread" vs "fastest overall" columns state the base case | MET |
| 9 | Do not summarise ratios with arithmetic means (H&B R3/R4) | No averages of ratios anywhere; ranges are min–max over a declared case set | MET |
| 10 | Report variability / CIs; non-parametric stats (H&B R5–R8, SIGPLAN) | Medians; CV gate 5% (C09) with adaptive extra rounds; bootstrap 95% CI of every rendered ratio must exclude 1 (K02); min/max/IQR stored | PARTIAL — CIs/IQR verified but not displayed on the site |
| 11 | Sufficient trials, warm-up, no discarded data (SIGPLAN, Heiser) | ≥1 warm-up, 7 rounds (adaptive to 21), loop calibration to ≥100 ms per sample, no outlier removal, randomized interleaving | MET |
| 12 | Full experimental setup for reproducibility (H&B R9, SPEC full disclosure) | Instance type, CPU, cores, kernel, governor, turbo, THP, pinned versions + commits, artifact hashes, dataset hashes, source snapshot, spec hash per run | MET |
| 13 | Timing/measurement technique documented (H&B R10) | Harness owns the clock (perf_counter_ns / CLOCK_MONOTONIC in workers around BEGIN_TIMED); per-call = batch/loops; documented in global.yaml | MET |
| 14 | Isolated, stable platform (SIGPLAN "reasonable platform") | Dedicated bare metal (no hypervisor flag), turbo off, performance governor, THP off, load check — enforced (C02) | MET |
| 15 | Independent verification (TPC independent auditor; MLPerf submission checker) | Automated conformance + claims audits; auditor self-test with known-bad cases; independent agent review; preflight gate | PARTIAL — no external human auditor (owner decision 2026-11-19) |
| 16 | Results reproducible by others (SIGPLAN, ACM artifacts) | Harness, spec, datasets (public + hashes) available to evaluators; deterministic synthetic fields | PARTIAL — harness not public (closed source); evaluators only |
| 17 | Comparative advertising: same need, objective, verifiable, representative (EU 2006/114/EC Art. 4) | Same task and output; measured on public data; method published; claims registry forbids unsupported superlatives ("fastest") and scope creep (3D, polygons) | MET |
| 18 | Substantiation before claim (FTC) | Publication gated on PASS attestations (conformance + claims); claims registry requires evidence | MET |
| 19 | Licence compliance for competitor benchmarking | Closed tools with restrictive licences excluded (MathWorks SLA §3.4 verified); open-source licences permit benchmarking | MET |
| 20 | Trademark / nominative use | Competitor names used only to identify the software measured (footer notice) | MET |
| 21 | Warm vs cold start disclosed (Heiser "measuring warm only") | Warm, in-process measurements by design (server/library use); cold start not measured — must be stated | PARTIAL — state on /benchmarks |
| 22 | Platform generality (SIGPLAN "explores key parameters") | One reference architecture (x86 Sapphire Rapids); ARM (c7g.metal) allowed in spec but not yet run | PARTIAL |
| 23 | Absolute times relative to turbo-disabled clocks | Turbo off makes absolute times slower than on typical turbo-enabled machines (all tools alike); ratios are the robust quantity — must be stated | PARTIAL — state on /benchmarks |
