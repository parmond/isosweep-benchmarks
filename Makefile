# Copyright 2026 Parmond AB
# SPDX-License-Identifier: Apache-2.0
# isosweep benchmark harness. `make fieldgen` builds the deterministic field
# generator for the synthetic benchmarks (the same flags as the reference
# runs, so the fields are bit-identical on the same platform).
CC ?= cc

.PHONY: fieldgen selftest clean
fieldgen: build/fieldgen
build/fieldgen: tools/fieldgen/fieldgen_main.c tools/fieldgen/fieldgen.c tools/fieldgen/fieldgen.h tools/fieldgen/fieldgen_prng.h
	@mkdir -p build
	$(CC) -O3 -std=c11 -Wall -flto -Itools/fieldgen -o $@ tools/fieldgen/fieldgen_main.c tools/fieldgen/fieldgen.c -lm -flto

# The auditor's self-test needs a baseline run directory: BASELINE=runs/<dir>
selftest:
	cd bench/framework && python3 audit/selftest.py --baseline $(abspath $(BASELINE)) --out $(abspath $(BASELINE))/selftest.json

clean:
	rm -rf build
