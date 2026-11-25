# isobench — the isosweep benchmark framework

Every benchmark number we publish comes from here. The framework exists so the
two mistakes we made in November 2026 — timing file I/O and benchmarking the
wrong engine — and their relatives (unequal outputs, misconfigured
competitors, mixed machines, cherry-picked cases, unsupported copy) are caught
mechanically, not by vigilance.

## Layers

| Layer | File(s) | What it guarantees |
|---|---|---|
| Spec | `spec/global.yaml`, `spec/tools.yaml`, `spec/benchmarks/*.yaml` | One definition of every benchmark: timed region, tools + pinned versions + allowed calls, expected engines, data + hashes, stats, environments, publication gates. Strictly validated; hashed. |
| Harness | `run.py`, `isobench/` | Executes the spec. Owns the clock. Three-phase adapters (prepare / activate / compute / collect). Sweeps every multi-threaded tool alike. Randomized interleaving, per-call timing, fixed n samples, CPU time + major faults per sample. strace validation pass per configuration. |
| Conformance audit (1) | `audit/conformance.py` | C01–C16: re-derives from raw results + the archived source snapshot that the run did exactly what the spec says → `attestation.json`. |
| Auditor self-test | `audit/selftest.py`, `audit/known_bad/` | Known-bad specs, adapters, native code, tampered results and live strace fixtures; each must flip exactly its target check. Proves the auditor works. |
| Methodology audit (2) | `audit/METHODOLOGY_CHECKLIST.md`, `audit/REVIEW_LOG.md` | Spec vs SIGPLAN checklist, Hoefler & Belli, Heiser's benchmarking crimes, SPEC/TPC disclosure, MLPerf, EU comparative-advertising law, FTC substantiation. Independent review logged. |
| Claims audit (3) | `spec/claims.yaml`, `audit/claims.py` | Every rendered number verified against raw samples (incl. 95% CI > 1 for "faster"); no hard-coded numbers; every risk phrase registered with evidence; scope rules (no 3D/polygon speed claims). |
| Gates | `gate.py`, `export_site.py`, `../aws_bench.sh --framework` | No bare-metal run without a PASSing preflight of the same source + spec; no website data without a PASSing reference attestation. |

## Measured once (R13): the result store

Each (benchmark, configuration) is measured once per code, data, method and machine,
then reused (`isobench/store.py`, `bench/results/framework/store/`). A record's
**timing key** hashes the data (field sha256, levels, level code), the configuration, the
tool's code (adapter class or worker source), its build (artifact sha256, version, pip
freeze), the measurement method (sample-timing code, stats / syscheck / timed-region
policy) and the machine (instance type, CPU, kernel, OS, Python, turbo, governor). A run
measures only configurations whose key is not in the store: after a change to our code,
only our configurations; competitors only when their pins or worker code change.

- **Admission** (`store_admit.py RESULTS_DIR`, automatic after a reference run): a record
  enters only if the run passed every run-level check and the record every per-record
  check (versions, engine, static, syscalls, statistics, effective configuration, data,
  sweeps, resources, work). Keys are computed from what the run executed; the checks
  apply today's evaluation rules. Run-to-run variation (CV above `stats.max_cv`) is
  recorded with the record and disclosed (C09 `variability`, dashboard, site), never a
  reason to discard a measurement: a tool whose time varies has that as a property.
- **Keys cover measurement only**: rounds, warm-up, order, loop calibration, timing
  mode, timed region, sweeps, process environment, how the syscall trace is recorded.
  Evaluation rules (variation threshold, tolerances, allowlists, budgets, waivers) are
  re-applied to stored records, so changing them never forces a re-measurement.
- **Anchors** (`global.yaml reuse.anchor_benchmarks`): three small benchmarks are
  re-measured completely (every configuration, each in its stored configuration, about
  3 minutes) whenever anything is reused. The run stops if the MEDIAN drift over all
  of them exceeds `max_drift` (3%): a machine unlike the store's shifts every
  configuration, while one tool's own variation (e.g. VTK, −5.8% on 2026-11-22) does
  not stop the run and is recorded.
- **C17** recomputes every reused record's key from the run's verified snapshot,
  environment and artifact hashes, and checks the anchors.
- **Builds and image**: the competitor build (GDAL, workers, the exact pip lock) is kept
  per `cloud/competitors.env` + worker sources and installed byte for byte by later
  preflights; the machine image is pinned (`store/ami.json`).

## Lifecycle

0. **Correctness before cost**: `validate_local.py` runs our configurations once on every
   benchmark on this machine and compares the output with the competitors' stored outputs
   (C08 rules, saddle and tie budgets, waivers). `gate.py` refuses a paid run without a
   PASS for the current sources and spec.
1. Change spec/harness → run the self-test locally (`audit/selftest.py --baseline <a local run>`).
2. **Preflight** (cheap Linux VM): `bench/aws_bench.sh --instance cpu-test --framework preflight --data <bundle>`
   — builds everything on the target OS, runs the preflight profile with full strace validation and the
   live auditor self-test, attests.
3. **Reference** (bare metal): `bench/aws_bench.sh --instance intel-sapphire --framework reference --data <bundle>`
   — refused by `gate.py` unless step 2 PASSed for this exact source tree + spec; attested locally with
   the preflight link (C13).
4. **Publish**: `export_site.py <reference dir>` (refuses non-PASS) → build the site with
   `CLAIMS_MANIFEST=...` → `audit/claims.py --manifest ... --results <reference dir>` must PASS.

Any edit to measured code or the spec after step 2 invalidates the preflight (hash-checked).

## Adding a tool or benchmark

- Tool: add an adapter (Python: `compute()` may only call what `compute_allowlist` lists; native: put the
  timed code between standalone `/* BEGIN_TIMED */` / `/* END_TIMED */` comment lines), pin its version,
  cite evidence for its fastest configuration, declare its output class. Ours must declare `engine`.
- Benchmark: one YAML file; real data needs a pinned sha256 and attribution; levels must be uniform if
  GDAL's callback API is used.
- Never loosen a tolerance to make a run pass — add a bounded, direction-labelled waiver with a
  justification, and expect the methodology review to challenge it.

## Like-for-like (C08, C16)

Every 2D tool's raw output (workers dump it with `DUMP`) goes through one code path,
`isobench/geometry.py`: normalise from the tool's declared frame (`coords`), clip to the
node domain, summarise. The reference for a tool is the median over the *other*
implementation families (`family`); isosweep never votes.

| Check | What it catches | Tolerance |
|---|---|---|
| W1 vertices, total + every level | less work (fewer edge crossings found) | distinct vertices per level, ±1% (±2 per level) |
| W1 length, total + every level | wrong geometry, extra/missing lines | ±0.5% (±2 cells per level) + the saddle budget |
| W2 unjoined ends | less assembly (pieces of one contour left unconnected) | exact: an open line may end only on the raster edge or a void |
| W2 line count | gross non-assembly | ±5% + one per saddle cell |
| W3 accuracy | wrong frame, wrong interpolation, low precision | every vertex on a cell edge; distance to the exact crossing ≤ 1e-5 cells (float64), 2e-3 (float32) |
| C16 work | different input dtype than declared; GDAL's half-cell border extension above its bound | declared `work` inventory per adapter |

The saddle budget (`geometry.saddle_budget`, computed from the field) is the most that a
tool's choice of pairing in saddle cells can change length and line count; vertices
cannot change. Differences beyond it are real.

The tie budget (`geometry.tie_budget`, `polygons.tie_area_budget`, from the field) is the
same for nodes exactly equal to a level, which some tools count as above (z >= level)
and others as below (z > level): one vertex and one line per tied node, and the exact
length / area difference in the cells touching tied nodes under the two conventions.
(2026-11-21 reference run: on the Alps DEM tile and HRRR pressure the tools split two
against two on tied nodes; the median of an even split was a value no tool produced.)

Waivers: only for differences that favour the competitor, always bounded (`max_rel`),
direction-checked against the measured difference. A difference that favours isosweep
is never waivable — fix it or exclude the tool. Every adapter's `work.differences` list
is disclosed with the results.

### Filled bands (kind `bands2d`, class `polygons.bands`; `isobench/polygons.py`)

Every tool produces every band between consecutive levels **and both outer bands**
(isosweep `filled()`, ContourPy `multi_filled` over ±inf, GDAL POLYGONIZE with
`MIN,…,MAX`, contour-rs `isobands` with bounds outside the data range), as polygons
with holes. Same one-path summary (frame, clip to the node domain, net clipped area
recorded) and leave-one-out references (from outputs not waived for that check).

| Check | What it catches | Tolerance |
|---|---|---|
| B1 total area | gaps, overlaps, unassigned holes, missing bands | == domain area (exact oracle), all bands present |
| B1 band area, every band | wrong regions | ±1e-4 (±1 cell²) + saddle area budget |
| B1 band vertices, every band | less work | distinct interior vertices ±1% (±2); crossings of a tool's own outer bound (GDAL MIN/MAX) not counted, recorded |
| B2 polygons per band | less assembly | ±2% + one per saddle cell + two per tie node |
| B3 accuracy | wrong frame / interpolation | as W3 |

Measured conventions, waived as favouring the competitor (bounded): threaded ContourPy
tiles polygons at chunk seams (extra seam vertices, unassembled); contour-rs keeps a
band region whole where a hole touches it (fewer polygons; areas agree).
