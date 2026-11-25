/* Copyright 2026 Parmond AB
 * SPDX-License-Identifier: Apache-2.0 */
/*
 * fieldgen.h — Deterministic test-case scalar field generator.
 *
 * Generates 2D scalar fields from a seed + profile, producing data that
 * resembles real-world domains (terrain, meteorological, semiconductor,
 * medical) and pathological edge cases for isocurve algorithms.
 *
 * Determinism: same (seed, profile, n) always produces identical output.
 * Cross-platform note: determinism is guaranteed per-platform (libm
 * functions like sin/cos/exp may differ between glibc and Apple libm).
 */

#ifndef FIELDGEN_H
#define FIELDGEN_H

#include <stdint.h>

/* Field profile identifiers */
typedef enum {
    /* Geographic */
    FGEN_TERRAIN_ALPINE,
    FGEN_TERRAIN_PLATEAU,
    FGEN_TERRAIN_COASTAL,
    FGEN_TERRAIN_ROLLING,
    /* Meteorological */
    FGEN_METEO_PRESSURE,
    FGEN_METEO_TEMPERATURE,
    FGEN_METEO_PRECIPITATION,
    /* Semiconductor */
    FGEN_SEMI_LINESPACE,
    FGEN_SEMI_CONTACTS,
    FGEN_SEMI_DOPING,
    FGEN_SEMI_CMP,
    /* Medical */
    FGEN_MEDICAL_BLOB,
    FGEN_MEDICAL_VESSEL,
    /* Pathological */
    FGEN_PATH_FLAT,
    FGEN_PATH_STEP,
    FGEN_PATH_CHECKERBOARD,
    FGEN_PATH_DYNRANGE,
    FGEN_PATH_SADDLE,
    /* Sentinel */
    FGEN_PROFILE_COUNT
} fgen_profile_t;

/* Generation result */
typedef struct {
    double *grid;           /* row-major n x n, caller must free */
    double *xcoords;        /* length n, caller must free */
    double *ycoords;        /* length n, caller must free */
    double value_min;       /* actual minimum in grid */
    double value_max;       /* actual maximum in grid */
    int n;                  /* grid dimension */
} fgen_result_t;

/*
 * Generate a deterministic n x n scalar field.
 * Returns 0 on success, -1 on allocation failure.
 * Caller must free result->grid, result->xcoords, result->ycoords.
 */
int fgen_generate(uint64_t seed, fgen_profile_t profile, int n,
                  fgen_result_t *result);

/* Profile name string (e.g. "terrain_alpine") */
const char *fgen_profile_name(fgen_profile_t profile);

/* Look up profile by name string. Returns -1 if not found. */
int fgen_profile_by_name(const char *name);

/*
 * Generate nlev iso-levels appropriate for the given result.
 * Levels span [value_min + margin, value_max - margin] uniformly.
 * Caller must free the returned array.
 */
double *fgen_iso_levels(const fgen_result_t *result, int nlev);

#endif /* FIELDGEN_H */
