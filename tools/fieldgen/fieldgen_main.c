/* Copyright 2026 Parmond AB
 * SPDX-License-Identifier: Apache-2.0 */
/*
 * fieldgen — write a deterministic synthetic benchmark field as raw row-major
 * float64 little-endian (n x n), byte-identical to the former
 * `iso2d/bench --fieldgen P --seed S --size N --dump-field PATH`.
 *
 *   fieldgen --profile medical_blob --seed 42 --size 512 --out field.f64
 *   fieldgen --list
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "fieldgen.h"

int main(int argc, char **argv) {
    const char *profile = NULL, *out = NULL;
    unsigned long long seed = 42;
    int n = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--profile") && i + 1 < argc) profile = argv[++i];
        else if (!strcmp(argv[i], "--seed") && i + 1 < argc) seed = strtoull(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--size") && i + 1 < argc) n = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--out") && i + 1 < argc) out = argv[++i];
        else if (!strcmp(argv[i], "--list")) {
            for (int p = 0; p < FGEN_PROFILE_COUNT; p++) printf("%s\n", fgen_profile_name(p));
            return 0;
        } else { fprintf(stderr, "unknown argument %s\n", argv[i]); return 2; }
    }
    if (!profile || !out || n < 2) {
        fprintf(stderr, "usage: fieldgen --profile P --seed S --size N --out PATH | --list\n");
        return 2;
    }
    int p = fgen_profile_by_name(profile);
    if (p < 0) { fprintf(stderr, "unknown profile %s (see --list)\n", profile); return 2; }
    fgen_result_t r;
    if (fgen_generate(seed, p, n, &r) != 0) { fprintf(stderr, "allocation failure\n"); return 1; }
    FILE *f = fopen(out, "wb");
    if (!f) { fprintf(stderr, "cannot open %s\n", out); return 1; }
    size_t nc = (size_t)n * n;
    int ok = fwrite(r.grid, sizeof(double), nc, f) == nc;
    fclose(f);
    free(r.grid); free(r.xcoords); free(r.ycoords);
    if (!ok) { fprintf(stderr, "write failed\n"); return 1; }
    return 0;
}
