/*
 * fieldgen_prng.h — Deterministic PRNG for test-case field generation.
 *
 * xoshiro256** by David Blackman and Sebastiano Vigna (public domain).
 * SplitMix64 for seed expansion.
 *
 * Pure integer arithmetic — bitwise identical output on any platform.
 */

#ifndef FIELDGEN_PRNG_H
#define FIELDGEN_PRNG_H

#include <stdint.h>
#include <math.h>

typedef struct {
    uint64_t s[4];
} fgen_rng_t;

/* --- SplitMix64 (seed expansion) --- */

static inline uint64_t splitmix64(uint64_t *state) {
    uint64_t z = (*state += 0x9e3779b97f4a7c15ULL);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

/* --- xoshiro256** --- */

static inline uint64_t rotl64(uint64_t x, int k) {
    return (x << k) | (x >> (64 - k));
}

static inline uint64_t fgen_rng_next(fgen_rng_t *rng) {
    uint64_t *s = rng->s;
    uint64_t result = rotl64(s[1] * 5, 7) * 9;
    uint64_t t = s[1] << 17;
    s[2] ^= s[0]; s[3] ^= s[1]; s[1] ^= s[2]; s[0] ^= s[3];
    s[2] ^= t;
    s[3] = rotl64(s[3], 45);
    return result;
}

/* Initialize from a single 64-bit seed */
static inline void fgen_rng_seed(fgen_rng_t *rng, uint64_t seed) {
    rng->s[0] = splitmix64(&seed);
    rng->s[1] = splitmix64(&seed);
    rng->s[2] = splitmix64(&seed);
    rng->s[3] = splitmix64(&seed);
}

/* Uniform double in [0, 1) */
static inline double fgen_rng_uniform(fgen_rng_t *rng) {
    return (fgen_rng_next(rng) >> 11) * (1.0 / 9007199254740992.0);
}

/* Uniform double in [lo, hi) */
static inline double fgen_rng_range(fgen_rng_t *rng, double lo, double hi) {
    return lo + fgen_rng_uniform(rng) * (hi - lo);
}

/* Approximate normal via Box-Muller (uses platform sin/cos — see note in fieldgen.c) */
static inline double fgen_rng_normal(fgen_rng_t *rng) {
    double u1 = fgen_rng_uniform(rng);
    double u2 = fgen_rng_uniform(rng);
    /* Avoid log(0) */
    if (u1 < 1e-15) u1 = 1e-15;
    return sqrt(-2.0 * log(u1)) * cos(6.283185307179586 * u2);
}

#endif /* FIELDGEN_PRNG_H */
