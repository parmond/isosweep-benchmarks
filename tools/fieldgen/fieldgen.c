/*
 * fieldgen.c — Deterministic test-case scalar field generator.
 *
 * Generates realistic 2D scalar fields from (seed, profile, n) for
 * benchmarking and testing isocurve extraction algorithms.
 *
 * All randomness comes from xoshiro256** seeded via SplitMix64.
 * Sub-streams: each primitive layer uses seed + layer_offset to ensure
 * independence between layers.
 */

#include "fieldgen.h"
#include "fieldgen_prng.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* ================================================================
 * Profile name table
 * ================================================================ */

static const struct {
    const char *name;
    const char *domain;
} profile_table[FGEN_PROFILE_COUNT] = {
    [FGEN_TERRAIN_ALPINE]      = {"terrain_alpine",      "geographic"},
    [FGEN_TERRAIN_PLATEAU]     = {"terrain_plateau",      "geographic"},
    [FGEN_TERRAIN_COASTAL]     = {"terrain_coastal",      "geographic"},
    [FGEN_TERRAIN_ROLLING]     = {"terrain_rolling",      "geographic"},
    [FGEN_METEO_PRESSURE]      = {"meteo_pressure",       "meteorological"},
    [FGEN_METEO_TEMPERATURE]   = {"meteo_temperature",    "meteorological"},
    [FGEN_METEO_PRECIPITATION] = {"meteo_precipitation",  "meteorological"},
    [FGEN_SEMI_LINESPACE]      = {"semi_linespace",       "semiconductor"},
    [FGEN_SEMI_CONTACTS]       = {"semi_contacts",        "semiconductor"},
    [FGEN_SEMI_DOPING]         = {"semi_doping",          "semiconductor"},
    [FGEN_SEMI_CMP]            = {"semi_cmp",             "semiconductor"},
    [FGEN_MEDICAL_BLOB]        = {"medical_blob",         "medical"},
    [FGEN_MEDICAL_VESSEL]      = {"medical_vessel",       "medical"},
    [FGEN_PATH_FLAT]           = {"path_flat",            "pathological"},
    [FGEN_PATH_STEP]           = {"path_step",            "pathological"},
    [FGEN_PATH_CHECKERBOARD]   = {"path_checkerboard",    "pathological"},
    [FGEN_PATH_DYNRANGE]       = {"path_dynrange",        "pathological"},
    [FGEN_PATH_SADDLE]         = {"path_saddle",          "pathological"},
};

const char *fgen_profile_name(fgen_profile_t profile) {
    if (profile < 0 || profile >= FGEN_PROFILE_COUNT) return "unknown";
    return profile_table[profile].name;
}

int fgen_profile_by_name(const char *name) {
    for (int i = 0; i < FGEN_PROFILE_COUNT; i++) {
        if (!strcmp(name, profile_table[i].name)) return i;
    }
    return -1;
}

/* ================================================================
 * Helpers
 * ================================================================ */

static double *alloc_grid(int n) {
    return (double *)calloc((size_t)n * n, sizeof(double));
}

static double *alloc_coords(int n) {
    double *c = (double *)malloc(n * sizeof(double));
    if (c) {
        for (int i = 0; i < n; i++)
            c[i] = i / (double)(n - 1);
    }
    return c;
}

/* Compute min/max of grid */
static void grid_minmax(const double *grid, int n, double *vmin, double *vmax) {
    double lo = grid[0], hi = grid[0];
    long total = (long)n * n;
    for (long i = 1; i < total; i++) {
        if (grid[i] < lo) lo = grid[i];
        if (grid[i] > hi) hi = grid[i];
    }
    *vmin = lo;
    *vmax = hi;
}

/* Smoothstep interpolation */
static double smoothstep(double t) {
    t = t < 0.0 ? 0.0 : (t > 1.0 ? 1.0 : t);
    return t * t * (3.0 - 2.0 * t);
}

/* ================================================================
 * Primitive: Gaussian blobs
 * ================================================================ */

static void layer_gaussian_blobs(double *grid, int n, fgen_rng_t *rng,
                                 int nblobs, double amp_lo, double amp_hi,
                                 double sigma_lo, double sigma_hi) {
    double inv = 1.0 / (n - 1);
    for (int b = 0; b < nblobs; b++) {
        double cx = fgen_rng_uniform(rng);
        double cy = fgen_rng_uniform(rng);
        double amp = fgen_rng_range(rng, amp_lo, amp_hi);
        double sigma = fgen_rng_range(rng, sigma_lo, sigma_hi);
        double inv2s2 = 1.0 / (2.0 * sigma * sigma);
        /* Skip pixels beyond 4*sigma for performance */
        int r_lo = (int)((cx - 4.0*sigma) * (n-1)); if (r_lo < 0) r_lo = 0;
        int r_hi = (int)((cx + 4.0*sigma) * (n-1)) + 1; if (r_hi > n) r_hi = n;
        int c_lo = (int)((cy - 4.0*sigma) * (n-1)); if (c_lo < 0) c_lo = 0;
        int c_hi = (int)((cy + 4.0*sigma) * (n-1)) + 1; if (c_hi > n) c_hi = n;
        for (int r = r_lo; r < r_hi; r++) {
            double x = r * inv;
            double dx = x - cx;
            for (int c = c_lo; c < c_hi; c++) {
                double y = c * inv;
                double dy = y - cy;
                grid[r * n + c] += amp * exp(-(dx*dx + dy*dy) * inv2s2);
            }
        }
    }
}

/* ================================================================
 * Primitive: Spectral sum (cosine waves with random phases)
 * ================================================================ */

static void layer_spectral_sum(double *grid, int n, fgen_rng_t *rng,
                               int nwaves, double amp_scale,
                               double freq_lo, double freq_hi,
                               double decay_y, double center_y) {
    double inv = 1.0 / (n - 1);
    for (int k = 0; k < nwaves; k++) {
        double freq = fgen_rng_range(rng, freq_lo, freq_hi);
        double phase_x = fgen_rng_range(rng, 0.0, 2.0 * M_PI);
        double phase_y = fgen_rng_range(rng, 0.0, 2.0 * M_PI);
        double amp = amp_scale / (1.0 + k * 0.3);
        for (int r = 0; r < n; r++) {
            double x = r * inv;
            double lat_factor = (decay_y > 0.0)
                ? exp(-(x - center_y) * (x - center_y) / (2.0 * decay_y * decay_y))
                : 1.0;
            double wave_r = amp * cos(freq * 2.0 * M_PI * x + phase_x) * lat_factor;
            for (int c = 0; c < n; c++) {
                double y = c * inv;
                grid[r * n + c] += wave_r * cos(freq * 2.0 * M_PI * y + phase_y);
            }
        }
    }
}

/* ================================================================
 * Primitive: Octave value noise (fBm)
 *
 * For each octave, generate a coarse random grid and bilinearly
 * interpolate to full resolution with smoothstep.
 * ================================================================ */

static void layer_fbm(double *grid, int n, uint64_t base_seed,
                      int octaves, double persistence, double base_amp) {
    double amp = base_amp;

    for (int oct = 0; oct < octaves; oct++) {
        /* Coarse grid resolution for this octave */
        int coarse = 4 << oct;  /* 4, 8, 16, 32, ... */
        if (coarse > n) coarse = n;

        /* Generate coarse random values */
        int cn1 = coarse + 1;
        double *coarse_grid = (double *)malloc(cn1 * cn1 * sizeof(double));
        if (!coarse_grid) return;

        fgen_rng_t rng;
        fgen_rng_seed(&rng, base_seed + (uint64_t)oct * 0x100000001ULL);
        for (int i = 0; i < cn1 * cn1; i++)
            coarse_grid[i] = fgen_rng_range(&rng, -1.0, 1.0);

        /* Interpolate to full resolution */
        double scale = (double)coarse / (n - 1);
        for (int r = 0; r < n; r++) {
            double fr = r * scale;
            int cr = (int)fr;
            if (cr >= coarse) cr = coarse - 1;
            double tr = smoothstep(fr - cr);

            for (int c = 0; c < n; c++) {
                double fc = c * scale;
                int cc = (int)fc;
                if (cc >= coarse) cc = coarse - 1;
                double tc = smoothstep(fc - cc);

                double v00 = coarse_grid[cr * cn1 + cc];
                double v10 = coarse_grid[(cr+1) * cn1 + cc];
                double v01 = coarse_grid[cr * cn1 + (cc+1)];
                double v11 = coarse_grid[(cr+1) * cn1 + (cc+1)];

                double v0 = v00 + (v10 - v00) * tr;
                double v1 = v01 + (v11 - v01) * tr;
                grid[r * n + c] += amp * (v0 + (v1 - v0) * tc);
            }
        }

        free(coarse_grid);
        amp *= persistence;
    }
}

/* Ridge transform: abs(noise) creates sharp ridges */
static void transform_ridge(double *grid, int n) {
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] = fabs(grid[i]);
}

/* ================================================================
 * Primitive: Separable Gaussian blur
 * ================================================================ */

static void layer_gaussian_blur(double *grid, int n, double sigma) {
    if (sigma < 0.5) return;
    int radius = (int)(4.0 * sigma + 0.5);
    if (radius < 1) radius = 1;
    if (radius >= n) radius = n - 1;

    /* Build kernel */
    int ksize = 2 * radius + 1;
    double *kernel = (double *)malloc(ksize * sizeof(double));
    if (!kernel) return;
    double sum = 0.0;
    for (int i = 0; i < ksize; i++) {
        double x = i - radius;
        kernel[i] = exp(-x * x / (2.0 * sigma * sigma));
        sum += kernel[i];
    }
    for (int i = 0; i < ksize; i++) kernel[i] /= sum;

    double *buf = (double *)malloc(n * sizeof(double));
    if (!buf) { free(kernel); return; }

    /* Blur rows */
    for (int r = 0; r < n; r++) {
        double *row = grid + r * n;
        memcpy(buf, row, n * sizeof(double));
        for (int c = 0; c < n; c++) {
            double v = 0.0;
            for (int k = -radius; k <= radius; k++) {
                int cc = c + k;
                if (cc < 0) cc = 0;
                if (cc >= n) cc = n - 1;
                v += buf[cc] * kernel[k + radius];
            }
            row[c] = v;
        }
    }

    /* Blur columns */
    for (int c = 0; c < n; c++) {
        for (int r = 0; r < n; r++) buf[r] = grid[r * n + c];
        for (int r = 0; r < n; r++) {
            double v = 0.0;
            for (int k = -radius; k <= radius; k++) {
                int rr = r + k;
                if (rr < 0) rr = 0;
                if (rr >= n) rr = n - 1;
                v += buf[rr] * kernel[k + radius];
            }
            grid[r * n + c] = v;
        }
    }

    free(buf);
    free(kernel);
}

/* ================================================================
 * Primitive: Periodic grating
 * ================================================================ */

static void layer_periodic_grating(double *grid, int n, double pitch,
                                   double amplitude, int direction) {
    /* direction: 0 = horizontal lines, 1 = vertical lines */
    double inv = 1.0 / (n - 1);
    double freq = 2.0 * M_PI / pitch;
    for (int r = 0; r < n; r++) {
        for (int c = 0; c < n; c++) {
            double coord = (direction == 0) ? c * inv : r * inv;
            grid[r * n + c] += amplitude * (0.5 + 0.5 * cos(freq * coord));
        }
    }
}

/* ================================================================
 * Primitive: Radial profile
 * ================================================================ */

static void layer_radial(double *grid, int n, double cx, double cy,
                         double base_val, double slope, double edge_rolloff) {
    double inv = 1.0 / (n - 1);
    for (int r = 0; r < n; r++) {
        double x = r * inv;
        double dx = x - cx;
        for (int c = 0; c < n; c++) {
            double y = c * inv;
            double dy = y - cy;
            double dist = sqrt(dx*dx + dy*dy);
            double v = base_val + slope * dist * dist;
            if (edge_rolloff > 0.0 && dist > (0.5 - edge_rolloff))
                v *= smoothstep((0.5 - dist) / edge_rolloff);
            grid[r * n + c] += v;
        }
    }
}

/* ================================================================
 * Primitive: Sigmoid / step
 * ================================================================ */

static void layer_sigmoid(double *grid, int n, double position, double width,
                          double amplitude, int direction) {
    double inv = 1.0 / (n - 1);
    for (int r = 0; r < n; r++) {
        for (int c = 0; c < n; c++) {
            double coord = (direction == 0) ? r * inv : c * inv;
            double t = (coord - position) / width;
            grid[r * n + c] += amplitude * tanh(t);
        }
    }
}

/* ================================================================
 * Primitive: Linear gradient
 * ================================================================ */

static void layer_gradient(double *grid, int n, double v0, double v1,
                           int direction) {
    double inv = 1.0 / (n - 1);
    for (int r = 0; r < n; r++) {
        for (int c = 0; c < n; c++) {
            double t = (direction == 0) ? r * inv : c * inv;
            grid[r * n + c] += v0 + (v1 - v0) * t;
        }
    }
}

/* ================================================================
 * Primitive: Additive noise
 * ================================================================ */

static void layer_noise(double *grid, int n, fgen_rng_t *rng, double amplitude) {
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] += amplitude * fgen_rng_normal(rng);
}

/* ================================================================
 * Primitive: Clamp
 * ================================================================ */

static void layer_clamp(double *grid, int n, double lo, double hi) {
    long total = (long)n * n;
    for (long i = 0; i < total; i++) {
        if (grid[i] < lo) grid[i] = lo;
        if (grid[i] > hi) grid[i] = hi;
    }
}

/* ================================================================
 * Profile implementations
 * ================================================================ */

/* --- Geographic profiles --- */

static void gen_terrain_alpine(double *grid, int n, uint64_t seed) {
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);

    double persistence = fgen_rng_range(&rng, 0.50, 0.60);
    double ridge_strength = fgen_rng_range(&rng, 0.3, 0.7);
    double tilt_angle = fgen_rng_range(&rng, 0.0, 2.0 * M_PI);
    double tilt_mag = fgen_rng_range(&rng, 0.1, 0.3);

    /* Fractal base terrain */
    layer_fbm(grid, n, seed + 100, 8, persistence, 1.0);

    /* Ridge transform on a portion */
    if (ridge_strength > 0.4) {
        double *ridge = alloc_grid(n);
        if (ridge) {
            layer_fbm(ridge, n, seed + 200, 6, 0.55, 1.0);
            transform_ridge(ridge, n);
            long total = (long)n * n;
            for (long i = 0; i < total; i++)
                grid[i] = grid[i] * (1.0 - ridge_strength) + ridge[i] * ridge_strength;
            free(ridge);
        }
    }

    /* Large-scale tilt */
    double inv = 1.0 / (n - 1);
    double ct = cos(tilt_angle), st = sin(tilt_angle);
    for (int r = 0; r < n; r++) {
        double x = r * inv - 0.5;
        for (int c = 0; c < n; c++) {
            double y = c * inv - 0.5;
            grid[r * n + c] += tilt_mag * (x * ct + y * st);
        }
    }

    /* Scale to elevation-like range [500, 4500] */
    double vmin, vmax;
    grid_minmax(grid, n, &vmin, &vmax);
    double range = vmax - vmin;
    if (range < 1e-10) range = 1.0;
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] = 500.0 + 4000.0 * (grid[i] - vmin) / range;
}

static void gen_terrain_plateau(double *grid, int n, uint64_t seed) {
    layer_fbm(grid, n, seed + 100, 6, 0.5, 1.0);

    /* Normalize to [0, 1] */
    double vmin, vmax;
    grid_minmax(grid, n, &vmin, &vmax);
    double range = vmax - vmin;
    if (range < 1e-10) range = 1.0;
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] = (grid[i] - vmin) / range;

    /* Sigmoid clamp to create plateau top and valley floor */
    for (long i = 0; i < total; i++) {
        double v = grid[i];
        /* Flatten above 0.7 (plateau) and below 0.3 (valley) */
        if (v > 0.6) v = 0.6 + 0.4 * smoothstep((v - 0.6) / 0.15);
        if (v < 0.3) v = 0.3 - 0.3 * smoothstep((0.3 - v) / 0.15);
        grid[i] = v;
    }

    /* Add cliff noise at the plateau edge */
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed + 300);
    double *edge_noise = alloc_grid(n);
    if (edge_noise) {
        layer_fbm(edge_noise, n, seed + 400, 4, 0.6, 0.03);
        for (long i = 0; i < total; i++)
            grid[i] += edge_noise[i];
        free(edge_noise);
    }

    /* Scale to [200, 2500] */
    grid_minmax(grid, n, &vmin, &vmax);
    range = vmax - vmin;
    if (range < 1e-10) range = 1.0;
    for (long i = 0; i < total; i++)
        grid[i] = 200.0 + 2300.0 * (grid[i] - vmin) / range;
}

static void gen_terrain_coastal(double *grid, int n, uint64_t seed) {
    layer_fbm(grid, n, seed + 100, 7, 0.52, 1.0);

    /* Bias so ~50% is below sea level */
    double vmin, vmax;
    grid_minmax(grid, n, &vmin, &vmax);
    double mid = (vmin + vmax) * 0.5;
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] -= mid;  /* Center around zero = sea level */

    /* Scale: land up to 800m, sea down to -200m */
    grid_minmax(grid, n, &vmin, &vmax);
    for (long i = 0; i < total; i++) {
        if (grid[i] >= 0.0)
            grid[i] = 800.0 * grid[i] / (vmax > 0.0 ? vmax : 1.0);
        else
            grid[i] = -200.0 * grid[i] / (vmin < 0.0 ? -vmin : 1.0);
    }
}

static void gen_terrain_rolling(double *grid, int n, uint64_t seed) {
    layer_fbm(grid, n, seed + 100, 4, 0.40, 1.0);
    layer_gaussian_blur(grid, n, n * 0.02);

    /* Scale to [100, 500] */
    double vmin, vmax;
    grid_minmax(grid, n, &vmin, &vmax);
    double range = vmax - vmin;
    if (range < 1e-10) range = 1.0;
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] = 100.0 + 400.0 * (grid[i] - vmin) / range;
}

/* --- Meteorological profiles --- */

static void gen_meteo_pressure(double *grid, int n, uint64_t seed) {
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);

    int ncenters = 15 + (int)(fgen_rng_uniform(&rng) * 10);

    /* Background pressure */
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] = 1013.25;

    /* Large-scale waves (zonal) */
    layer_spectral_sum(grid, n, &rng, 6, 15.0, 1.0, 6.0, 0.15, 0.5);

    /* Pressure centers (highs and lows) */
    layer_gaussian_blobs(grid, n, &rng, ncenters, -30.0, 20.0, 0.05, 0.15);

    /* Light smoothing */
    layer_gaussian_blur(grid, n, n * 0.005);
}

static void gen_meteo_temperature(double *grid, int n, uint64_t seed) {
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);

    /* Latitude gradient (warm south, cold north) */
    layer_gradient(grid, n, 30.0, -10.0, 0);

    /* Frontal boundary */
    double front_pos = fgen_rng_range(&rng, 0.35, 0.65);
    double front_angle = fgen_rng_range(&rng, -0.2, 0.2);
    double inv = 1.0 / (n - 1);
    for (int r = 0; r < n; r++) {
        double x = r * inv;
        for (int c = 0; c < n; c++) {
            double y = c * inv;
            double shifted = x - front_pos - front_angle * (y - 0.5);
            grid[r * n + c] += -10.0 * tanh(shifted / 0.03);
        }
    }

    /* Mesoscale noise */
    fgen_rng_t noise_rng;
    fgen_rng_seed(&noise_rng, seed + 200);
    double *noise = alloc_grid(n);
    if (noise) {
        layer_fbm(noise, n, seed + 300, 4, 0.5, 3.0);
        layer_gaussian_blur(noise, n, n * 0.02);
        long total = (long)n * n;
        for (long i = 0; i < total; i++)
            grid[i] += noise[i];
        free(noise);
    }
}

static void gen_meteo_precipitation(double *grid, int n, uint64_t seed) {
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);

    int ncells = 50 + (int)(fgen_rng_uniform(&rng) * 150);

    /* Many small convective cells */
    layer_gaussian_blobs(grid, n, &rng, ncells, 5.0, 50.0, 0.01, 0.04);

    /* Floor at zero (no negative precip) */
    layer_clamp(grid, n, 0.0, 1e10);

    /* Light blur */
    layer_gaussian_blur(grid, n, n * 0.003);
}

/* --- Semiconductor profiles --- */

static void gen_semi_linespace(double *grid, int n, uint64_t seed) {
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);

    double pitch = fgen_rng_range(&rng, 0.02, 0.06);

    /* Periodic line/space grating */
    layer_periodic_grating(grid, n, pitch, 1.0, 0);

    /* OPC sub-resolution features */
    int nopc = 10 + (int)(fgen_rng_uniform(&rng) * 10);
    layer_gaussian_blobs(grid, n, &rng, nopc, 0.3, 0.8, 0.005, 0.015);

    /* PSF blur (scaled by grid size) */
    double psf_sigma = n * 0.015;
    layer_gaussian_blur(grid, n, psf_sigma);

    /* Square (intensity is amplitude^2) */
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] = grid[i] * grid[i];

    /* Normalize to [0, 1] */
    double vmin, vmax;
    grid_minmax(grid, n, &vmin, &vmax);
    double range = vmax - vmin;
    if (range < 1e-10) range = 1.0;
    for (long i = 0; i < total; i++)
        grid[i] = (grid[i] - vmin) / range;

    /* Add noise */
    fgen_rng_t noise_rng;
    fgen_rng_seed(&noise_rng, seed + 500);
    layer_noise(grid, n, &noise_rng, 0.005);
    layer_clamp(grid, n, 0.0, 1.0);
}

static void gen_semi_contacts(double *grid, int n, uint64_t seed) {
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);

    double spacing = fgen_rng_range(&rng, 0.03, 0.06);
    double contact_sigma = spacing * 0.25;

    /* Regular grid of contacts with staggered rows */
    double inv = 1.0 / (n - 1);
    for (double cx = spacing; cx < 1.0; cx += spacing) {
        int row_idx = (int)(cx / spacing);
        double offset = (row_idx % 2) ? spacing * 0.5 : 0.0;
        for (double cy = spacing + offset; cy < 1.0; cy += spacing) {
            double inv2s2 = 1.0 / (2.0 * contact_sigma * contact_sigma);
            int r_lo = (int)((cx - 4.0*contact_sigma) * (n-1)); if (r_lo < 0) r_lo = 0;
            int r_hi = (int)((cx + 4.0*contact_sigma) * (n-1)) + 1; if (r_hi > n) r_hi = n;
            int c_lo = (int)((cy - 4.0*contact_sigma) * (n-1)); if (c_lo < 0) c_lo = 0;
            int c_hi = (int)((cy + 4.0*contact_sigma) * (n-1)) + 1; if (c_hi > n) c_hi = n;
            for (int r = r_lo; r < r_hi; r++) {
                double x = r * inv;
                double dx = x - cx;
                for (int c = c_lo; c < c_hi; c++) {
                    double y = c * inv;
                    double dy = y - cy;
                    grid[r * n + c] += exp(-(dx*dx + dy*dy) * inv2s2);
                }
            }
        }
    }

    /* PSF blur */
    layer_gaussian_blur(grid, n, n * 0.01);

    /* Square for intensity */
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] = grid[i] * grid[i];

    /* Normalize */
    double vmin, vmax;
    grid_minmax(grid, n, &vmin, &vmax);
    double range = vmax - vmin;
    if (range < 1e-10) range = 1.0;
    for (long i = 0; i < total; i++)
        grid[i] = (grid[i] - vmin) / range;

    /* Noise */
    fgen_rng_t noise_rng;
    fgen_rng_seed(&noise_rng, seed + 500);
    layer_noise(grid, n, &noise_rng, 0.003);
    layer_clamp(grid, n, 0.0, 1.0);
}

static void gen_semi_doping(double *grid, int n, uint64_t seed) {
    /* Doping concentration in log10 space */
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);
    long total = (long)n * n;
    double inv = 1.0 / (n - 1);

    /* Background doping 1e15 -> log10 = 15 */
    for (long i = 0; i < total; i++)
        grid[i] = 15.0;

    /* Well implant */
    double well_cx = fgen_rng_range(&rng, 0.4, 0.6);
    double well_sy = fgen_rng_range(&rng, 0.15, 0.25);
    for (int r = 0; r < n; r++) {
        double x = r * inv;
        for (int c = 0; c < n; c++) {
            double y = c * inv;
            double dx = x - well_cx;
            grid[r * n + c] += 2.5 * exp(-dx*dx / (2.0*0.3*0.3))
                                    * exp(-y*y / (2.0*well_sy*well_sy));
        }
    }

    /* Source/drain implants (high concentration) */
    double sd_positions[4];
    int nsd = 2 + (int)(fgen_rng_uniform(&rng) * 3);
    for (int s = 0; s < nsd && s < 4; s++) {
        sd_positions[s] = fgen_rng_range(&rng, 0.2, 0.8);
        double sd_sigma = fgen_rng_range(&rng, 0.03, 0.06);
        for (int r = 0; r < n; r++) {
            double x = r * inv;
            double dx = x - sd_positions[s];
            for (int c = 0; c < n; c++) {
                double y = c * inv;
                grid[r * n + c] += 5.0 * exp(-dx*dx / (2.0*sd_sigma*sd_sigma))
                                        * exp(-y*y / (2.0*0.05*0.05));
            }
        }
    }
}

static void gen_semi_cmp(double *grid, int n, uint64_t seed) {
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);

    /* Radial base (wafer bowl shape) */
    layer_radial(grid, n, 0.5, 0.5, 1000.0, -50.0, 0.05);

    /* Azimuthal harmonics */
    double inv = 1.0 / (n - 1);
    int nharmonics = 2 + (int)(fgen_rng_uniform(&rng) * 3);
    for (int h = 0; h < nharmonics; h++) {
        double harm_amp = fgen_rng_range(&rng, 3.0, 8.0);
        double harm_k = fgen_rng_range(&rng, 2.0, 5.0);
        double harm_phase = fgen_rng_range(&rng, 0.0, 2.0 * M_PI);
        for (int r = 0; r < n; r++) {
            double x = r * inv - 0.5;
            for (int c = 0; c < n; c++) {
                double y = c * inv - 0.5;
                double angle = atan2(y, x);
                double dist = sqrt(x*x + y*y);
                if (dist < 0.48)
                    grid[r * n + c] += harm_amp * cos(harm_k * angle + harm_phase)
                                     * (dist / 0.48);
            }
        }
    }

    /* Local bumps */
    layer_gaussian_blobs(grid, n, &rng, 20, -5.0, 5.0, 0.02, 0.04);

    /* Zero outside wafer edge */
    for (int r = 0; r < n; r++) {
        double x = r * inv - 0.5;
        for (int c = 0; c < n; c++) {
            double y = c * inv - 0.5;
            double dist = sqrt(x*x + y*y);
            if (dist > 0.49) grid[r * n + c] = 0.0;
        }
    }

    layer_gaussian_blur(grid, n, n * 0.008);
}

/* --- Medical profiles --- */

static void gen_medical_blob(double *grid, int n, uint64_t seed) {
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);

    /* Background gradient */
    layer_gradient(grid, n, 0.1, 0.3, 0);

    /* 5-15 soft blobs at varying contrast */
    int nblobs = 5 + (int)(fgen_rng_uniform(&rng) * 10);
    layer_gaussian_blobs(grid, n, &rng, nblobs, 0.2, 0.8, 0.03, 0.10);

    /* Smooth edges */
    layer_gaussian_blur(grid, n, n * 0.01);

    /* Normalize to [0, 1] */
    double vmin, vmax;
    grid_minmax(grid, n, &vmin, &vmax);
    double range = vmax - vmin;
    if (range < 1e-10) range = 1.0;
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] = (grid[i] - vmin) / range;
}

static void gen_medical_vessel(double *grid, int n, uint64_t seed) {
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);
    double inv = 1.0 / (n - 1);

    /* Background */
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] = 0.1;

    /* Elongated Gaussian "vessel segments" */
    int nsegments = 8 + (int)(fgen_rng_uniform(&rng) * 12);
    for (int s = 0; s < nsegments; s++) {
        double sx = fgen_rng_range(&rng, 0.1, 0.9);
        double sy = fgen_rng_range(&rng, 0.1, 0.9);
        double angle = fgen_rng_range(&rng, 0.0, M_PI);
        double length = fgen_rng_range(&rng, 0.05, 0.2);
        double width = fgen_rng_range(&rng, 0.005, 0.015);
        double amp = fgen_rng_range(&rng, 0.3, 0.8);
        double ca = cos(angle), sa = sin(angle);

        /* Bounding box for the segment */
        double half_diag = length + 4.0 * width;
        int r_lo = (int)((sx - half_diag) * (n-1)); if (r_lo < 0) r_lo = 0;
        int r_hi = (int)((sx + half_diag) * (n-1)) + 1; if (r_hi > n) r_hi = n;
        int c_lo = (int)((sy - half_diag) * (n-1)); if (c_lo < 0) c_lo = 0;
        int c_hi = (int)((sy + half_diag) * (n-1)) + 1; if (c_hi > n) c_hi = n;

        for (int r = r_lo; r < r_hi; r++) {
            double x = r * inv;
            double dx = x - sx;
            for (int c = c_lo; c < c_hi; c++) {
                double y = c * inv;
                double dy = y - sy;
                /* Rotate into segment-local coords */
                double along = dx * ca + dy * sa;
                double across = -dx * sa + dy * ca;
                if (fabs(along) < length) {
                    double v = amp * exp(-across * across / (2.0 * width * width))
                                   * exp(-along * along / (2.0 * length * length * 0.25));
                    grid[r * n + c] += v;
                }
            }
        }
    }

    layer_gaussian_blur(grid, n, n * 0.005);
}

/* --- Pathological profiles --- */

static void gen_path_flat(double *grid, int n, uint64_t seed) {
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] = 0.5 + 1e-6 * fgen_rng_normal(&rng);
}

static void gen_path_step(double *grid, int n, uint64_t seed) {
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);
    double pos = fgen_rng_range(&rng, 0.3, 0.7);
    double width = 1.0 / (n - 1);  /* 1-cell width */
    layer_sigmoid(grid, n, pos, width, 1.0, 0);
    /* Shift to [0, 1] */
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] = 0.5 + 0.5 * grid[i];
}

static void gen_path_checkerboard(double *grid, int n, uint64_t seed) {
    (void)seed;
    for (int r = 0; r < n; r++) {
        for (int c = 0; c < n; c++) {
            grid[r * n + c] = ((r + c) & 1) ? 1.0 : 0.0;
        }
    }
}

static void gen_path_dynrange(double *grid, int n, uint64_t seed) {
    layer_fbm(grid, n, seed + 100, 5, 0.5, 1.0);
    long total = (long)n * n;
    for (long i = 0; i < total; i++)
        grid[i] = exp(10.0 * grid[i]);
}

static void gen_path_saddle(double *grid, int n, uint64_t seed) {
    fgen_rng_t rng;
    fgen_rng_seed(&rng, seed);
    double freq = fgen_rng_range(&rng, 4.0, 8.0);
    double inv = 1.0 / (n - 1);
    for (int r = 0; r < n; r++) {
        double x = r * inv;
        for (int c = 0; c < n; c++) {
            double y = c * inv;
            grid[r * n + c] = sin(freq * M_PI * x) * sin(freq * M_PI * y);
        }
    }
}

/* ================================================================
 * Dispatch
 * ================================================================ */

typedef void (*gen_func_t)(double *grid, int n, uint64_t seed);

static const gen_func_t generators[FGEN_PROFILE_COUNT] = {
    [FGEN_TERRAIN_ALPINE]      = gen_terrain_alpine,
    [FGEN_TERRAIN_PLATEAU]     = gen_terrain_plateau,
    [FGEN_TERRAIN_COASTAL]     = gen_terrain_coastal,
    [FGEN_TERRAIN_ROLLING]     = gen_terrain_rolling,
    [FGEN_METEO_PRESSURE]      = gen_meteo_pressure,
    [FGEN_METEO_TEMPERATURE]   = gen_meteo_temperature,
    [FGEN_METEO_PRECIPITATION] = gen_meteo_precipitation,
    [FGEN_SEMI_LINESPACE]      = gen_semi_linespace,
    [FGEN_SEMI_CONTACTS]       = gen_semi_contacts,
    [FGEN_SEMI_DOPING]         = gen_semi_doping,
    [FGEN_SEMI_CMP]            = gen_semi_cmp,
    [FGEN_MEDICAL_BLOB]        = gen_medical_blob,
    [FGEN_MEDICAL_VESSEL]      = gen_medical_vessel,
    [FGEN_PATH_FLAT]           = gen_path_flat,
    [FGEN_PATH_STEP]           = gen_path_step,
    [FGEN_PATH_CHECKERBOARD]   = gen_path_checkerboard,
    [FGEN_PATH_DYNRANGE]       = gen_path_dynrange,
    [FGEN_PATH_SADDLE]         = gen_path_saddle,
};

int fgen_generate(uint64_t seed, fgen_profile_t profile, int n,
                  fgen_result_t *result) {
    if (profile < 0 || profile >= FGEN_PROFILE_COUNT) return -1;
    if (n < 2) return -1;

    double *grid = alloc_grid(n);
    double *xcoords = alloc_coords(n);
    double *ycoords = alloc_coords(n);
    if (!grid || !xcoords || !ycoords) {
        free(grid); free(xcoords); free(ycoords);
        return -1;
    }

    generators[profile](grid, n, seed);

    result->grid = grid;
    result->xcoords = xcoords;
    result->ycoords = ycoords;
    result->n = n;
    grid_minmax(grid, n, &result->value_min, &result->value_max);

    return 0;
}

double *fgen_iso_levels(const fgen_result_t *result, int nlev) {
    if (nlev < 1) return NULL;
    double *lev = (double *)malloc(nlev * sizeof(double));
    if (!lev) return NULL;

    double range = result->value_max - result->value_min;
    double margin = range * 0.05;
    double lo = result->value_min + margin;
    double hi = result->value_max - margin;

    if (nlev == 1) {
        lev[0] = (lo + hi) * 0.5;
    } else {
        for (int i = 0; i < nlev; i++)
            lev[i] = lo + (hi - lo) * i / (double)(nlev - 1);
    }
    return lev;
}
