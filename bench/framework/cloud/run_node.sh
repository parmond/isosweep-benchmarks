#!/bin/bash
# run_node.sh PROFILE — run the framework on the node: harness, then (preflight)
# the live auditor self-test, then the conformance audit. Results: ~/fw_results
set -euo pipefail
PROFILE=$1
FW="${REPO_DIR:-$HOME/isocurves}/bench/framework"
OUT=~/fw_results
PY=~/venv/bin/python
export TIF2F64=~/bin/tif2f64
cd $FW
(cd ~/landscape_data && sha256sum -c MANIFEST.sha256 --quiet) || { echo "dataset checksum mismatch"; exit 1; }
# the launched tree must be the tree on the node (when an orchestrator recorded one)
PROV_ARG=""
if [ -f ~/provenance.json ]; then
  LAUNCH=$(~/venv/bin/python -c "import json;print(json.load(open('$HOME/provenance.json'))['source_tree_hash'])")
  NODE=$(cat ~/node_tree_hash.txt)
  [ "$LAUNCH" = "$NODE" ] || { echo "source tree on node ($NODE) != launched tree ($LAUNCH)"; exit 1; }
  PROV_ARG="--provenance-file $HOME/provenance.json"
fi
# without an isosweep build (no source tree, no evaluation bundle), leave its configurations out
OURS_ARG=""
[ -x ~/bin/isosweep_worker ] || OURS_ARG="--exclude-ours"
# R13: the result store, when the orchestrator uploaded one
STORE_ARG=""
[ -d ~/store/timing ] && STORE_ARG="--store $HOME/store"
# R7: the harness runs with a clean environment (global.yaml process_env.allow); C02 checks it
env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 TIF2F64="$TIF2F64" \
  $PY run.py --profile "$PROFILE" --out $OUT --data-dir ~/landscape_data --bin-dir ~/bin $STORE_ARG \
  $PROV_ARG $OURS_ARG --artifact-manifest ~/artifact_manifest.json 2>&1 | grep --line-buffered -vE "SMPTools backend|available backends|^Using " | tee ~/run.log
cp ~/run.log $OUT/run.log
cp ~/artifact_manifest.json $OUT/artifact_manifest.json
if [ "$PROFILE" = "preflight" ]; then
  $PY audit/selftest.py --baseline $OUT --live --out $OUT/selftest.json 2>&1 | tee $OUT/selftest.log || true
  $PY audit/conformance.py $OUT --selftest $OUT/selftest.json || true
else
  # C13 (preflight link) is evaluated again locally with the preflight results.
  $PY audit/conformance.py $OUT || true
fi
