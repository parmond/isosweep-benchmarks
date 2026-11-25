#!/bin/bash
# Copyright 2026 Parmond AB
# SPDX-License-Identifier: Apache-2.0
# setup_ubuntu.sh — provision an Ubuntu 24.04 node for the isobench framework.
#
# Build mode (preflight): pinned GDAL 3.13.3 + GDAL master@commit (identical
# config), Python packages, Rust toolchain + Cargo.lock. Builds our binding +
# field generator and every worker, then packs everything that runs into
# ~/artifacts.tar.gz (+ requirements.lock = the venv's exact pip freeze) and
# writes ~/artifact_manifest.json.
#
# Install mode (reference; ARTIFACTS=bundle EXPECT_BUNDLE_SHA=sha256): builds
# nothing. Installs the preflight's bundle unchanged and the same venv from its
# lock (R8 build-once: the reference runs the bytes the preflight validated).
# Fails loudly (prints the failing build log).
set -euo pipefail
ARTIFACTS="${ARTIFACTS:-}"
EXPECT_BUNDLE_SHA="${EXPECT_BUNDLE_SHA:-}"
# R13: a stored competitor bundle (GDAL builds, competitor workers, the exact pip
# lock) is installed instead of rebuilt, so competitor binaries stay byte-identical
COMPETITORS="${COMPETITORS:-}"
EXPECT_COMPETITORS_SHA="${EXPECT_COMPETITORS_SHA:-}"
# the repository (the private source tree, or the public isosweep-benchmarks repo)
ROOT="${REPO_DIR:-$HOME/isocurves}"
FW=$ROOT/bench/framework
# isosweep itself: built from source when the tree has it; otherwise from an
# evaluation bundle (ISOSWEEP_SDK=dir with include/isosweep.h, lib/libisosweep.a and
# an isocurves-*.whl); with neither, its configurations are skipped (run.py --exclude-ours)
ISOSWEEP_SDK="${ISOSWEEP_SDK:-}"
. "$FW/cloud/competitors.env"
T=~/tools
B=~/bin
mkdir -p $T $B
NPROC=$(nproc)
log() { echo "[setup $(date -u +%H:%M:%S)] $*"; }
fail() { echo "SETUP FAILED: $1"; [ -f "$2" ] && tail -60 "$2"; exit 1; }

log "apt packages"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq build-essential cmake ninja-build pkg-config git curl ca-certificates \
  xz-utils time strace libproj-dev libgeos-dev libsqlite3-dev libtiff-dev libgeotiff-dev \
  libcurl4-openssl-dev libdeflate-dev libzstd-dev zlib1g-dev libexpat1-dev python3-venv python3-dev >/dev/null

if [ -n "$ARTIFACTS" ]; then
  log "install mode: bundle $ARTIFACTS"
  got=$(sha256sum "$ARTIFACTS" | cut -d' ' -f1)
  [ -n "$EXPECT_BUNDLE_SHA" ] && [ "$got" = "$EXPECT_BUNDLE_SHA" ] || fail "bundle sha256 $got != expected $EXPECT_BUNDLE_SHA" ""
  tar xzf "$ARTIFACTS" -C ~
  python3 -m venv ~/venv
  ~/venv/bin/pip install -q --no-deps -r ~/requirements.lock
  ~/venv/bin/python -c "import sys; sys.path.insert(0,'$ROOT/python'); import isocurves; print('binding', isocurves.build_info())"
  for v in 3.13.3 master; do echo "gdal_worker_$v: $(printf 'INFO\nQUIT\n' | $B/gdal_worker_$v)"; done
  ~/venv/bin/python $FW/isobench/provenance.py manifest "$ARTIFACTS" ~ installed ~/artifact_manifest.json
  ~/venv/bin/python $FW/isobench/provenance.py > ~/node_tree_hash.txt
  log "setup complete (installed bundle $got; node tree hash $(cat ~/node_tree_hash.txt))"
  exit 0
fi

build_gdal() { # name ref prefix
  local name=$1 ref=$2 prefix=$3 src=$T/gdal-$1-src
  mkdir -p "$src"; cd "$src"
  git init -q; git remote add origin https://github.com/OSGeo/gdal.git
  git fetch -q --depth 1 origin "$ref"; git checkout -q FETCH_HEAD
  cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
    -DCMAKE_INSTALL_RPATH="$prefix/lib" \
    -DGDAL_BUILD_OPTIONAL_DRIVERS=OFF -DOGR_BUILD_OPTIONAL_DRIVERS=OFF \
    -DOGR_ENABLE_DRIVER_GPKG=ON -DOGR_ENABLE_DRIVER_SHAPE=ON -DOGR_ENABLE_DRIVER_FLATGEOBUF=ON \
    -DOGR_ENABLE_DRIVER_GEOJSON=ON -DOGR_ENABLE_DRIVER_SQLITE=ON -DGDAL_USE_GEOS=ON \
    -DBUILD_PYTHON_BINDINGS=OFF -DBUILD_JAVA_BINDINGS=OFF -DBUILD_CSHARP_BINDINGS=OFF \
    -DBUILD_TESTING=OFF > "$T/gdal-$name-cmake.log" 2>&1 || fail "gdal $name cmake" "$T/gdal-$name-cmake.log"
  ninja -C build -j"$NPROC" install > "$T/gdal-$name-build.log" 2>&1 || fail "gdal $name build" "$T/gdal-$name-build.log"
}
if [ -n "$COMPETITORS" ]; then
  log "competitor builds: stored bundle (not rebuilt)"
  got=$(sha256sum "$COMPETITORS" | cut -d' ' -f1)
  [ -n "$EXPECT_COMPETITORS_SHA" ] && [ "$got" = "$EXPECT_COMPETITORS_SHA" ] || fail "competitor bundle sha256 $got != expected $EXPECT_COMPETITORS_SHA" ""
  tar xzf "$COMPETITORS" -C ~
  python3 -m venv ~/venv
  ~/venv/bin/pip install -q --no-deps -r ~/requirements.lock
else
  log "GDAL ${GDAL_STABLE_REF#v} and master@$GDAL_MASTER_COMMIT (parallel)"
  ( build_gdal 3.13.3 "$GDAL_STABLE_REF" $T/gdal-3.13.3 ) & P1=$!
  ( build_gdal master "$GDAL_MASTER_COMMIT" $T/gdal-master ) & P2=$!

  log "python venv (pinned)"
  python3 -m venv ~/venv
  ~/venv/bin/pip install -q --upgrade pip setuptools wheel
  # shellcheck disable=SC2086
  ~/venv/bin/pip install -q $PIP_PINS

  log "rust"
  curl -sSf https://sh.rustup.rs | sh -s -- -y -q --profile minimal --default-toolchain "$RUST_TOOLCHAIN" >/dev/null
  export PATH=$HOME/.cargo/bin:$PATH
  (cd $FW/workers/rs_worker && cargo build -q --release --locked) || fail "rs_worker build" /dev/null
  cp $FW/workers/rs_worker/target/release/rs_worker $B/
fi

OURS=()                                   # isosweep files that go into the artifact bundle
if [ -d "$ROOT/src" ]; then
  log "our engine (from source): field generator + python binding"
  make -C $ROOT clean >/dev/null 2>&1 || true
  make -C $ROOT lib fieldgen -j"$NPROC" > $T/lib.log 2>&1 || fail "libisosweep + fieldgen" $T/lib.log
  gcc -O3 -std=c11 -Wall -flto -o $B/isosweep_worker $FW/workers/isosweep_worker.c -I$ROOT/include \
    $ROOT/build/libisosweep.a -lm -lpthread || fail "isosweep_worker" ""
  (cd $ROOT/python && rm -rf build isocurves/_core*.so && ~/venv/bin/python setup.py -q build_ext --inplace > $T/binding.log 2>&1) \
    || fail "binding" $T/binding.log
  ~/venv/bin/python -c "import sys; sys.path.insert(0,'$ROOT/python'); import isocurves; print('binding', isocurves.build_info())"
  OURS=("$(basename $ROOT)/build/libisosweep.a" $(cd ~ && ls $(basename $ROOT)/python/isocurves/_core*.so))
else
  log "field generator"
  make -C $ROOT fieldgen > $T/lib.log 2>&1 || fail "fieldgen" $T/lib.log
  if [ -n "$ISOSWEEP_SDK" ]; then
    log "isosweep from the evaluation bundle $ISOSWEEP_SDK"
    gcc -O3 -std=c11 -Wall -flto -o $B/isosweep_worker $FW/workers/isosweep_worker.c -I$ISOSWEEP_SDK/include \
      $ISOSWEEP_SDK/lib/libisosweep.a -lm -lpthread || fail "isosweep_worker" ""
    ~/venv/bin/pip install -q --no-deps "$ISOSWEEP_SDK"/isocurves-*.whl || fail "isocurves wheel" ""
    ~/venv/bin/python -c "import isocurves; print('isocurves', isocurves.build_info())"
  else
    log "no isosweep (set ISOSWEEP_SDK to an evaluation bundle): its configurations will be skipped"
  fi
fi

if [ -z "$COMPETITORS" ]; then
  wait $P1 || fail "gdal 3.13.3" ""
  wait $P2 || fail "gdal master" ""
  log "GDAL workers + tif2f64"
  for v in 3.13.3 master; do
    P=$T/gdal-$v
    gcc -O2 -Wall -o $B/gdal_worker_$v $FW/workers/gdal_worker.c $($P/bin/gdal-config --cflags) \
      $($P/bin/gdal-config --libs) -Wl,-rpath,$P/lib -lm || fail "gdal_worker $v" ""
  done
  P=$T/gdal-3.13.3
  gcc -O2 -Wall -o $B/tif2f64 $FW/workers/tif2f64.c $($P/bin/gdal-config --cflags) $($P/bin/gdal-config --libs) \
    -Wl,-rpath,$P/lib -lm || fail "tif2f64" ""
fi
for v in 3.13.3 master; do echo "gdal_worker_$v: $(printf 'INFO\nQUIT\n' | $B/gdal_worker_$v)"; done
log "artifact bundle (build once; the reference installs it unchanged)"
~/venv/bin/pip freeze --all > ~/requirements.lock
(cd ~ && tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf ~/artifacts.tar.gz \
  requirements.lock bin tools/gdal-3.13.3 tools/gdal-master "$(basename $ROOT)/build/fieldgen" "${OURS[@]}") \
  || fail "artifact bundle" ""
~/venv/bin/python $FW/isobench/provenance.py manifest ~/artifacts.tar.gz ~ built ~/artifact_manifest.json
~/venv/bin/python $FW/isobench/provenance.py > ~/node_tree_hash.txt
log "setup complete (bundle $(sha256sum ~/artifacts.tar.gz | cut -d' ' -f1); node tree hash $(cat ~/node_tree_hash.txt))"
