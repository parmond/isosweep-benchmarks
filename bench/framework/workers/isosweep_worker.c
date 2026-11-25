/* Copyright 2026 Parmond AB
 * SPDX-License-Identifier: Apache-2.0 */
/* isosweep_worker — libisosweep's C API under the isobench worker protocol, so
 * the library a C licensee links can be measured C to C (as GDAL's C API is),
 * without the Python binding (design B10).
 *
 * Protocol (one command per stdin line, one reply per stdout line):
 *   LOAD <field.f64> <nx> <ny> <levels_csv> <mode>/<threads>  -> OK
 *        mode: lines | bands. The float64 field is converted to float32 here,
 *        untimed (the library's input type; the Python surface also hands
 *        float32 over untimed)
 *   RUN <loops> <markers 0|1>  -> TIME <wall_ns> <cpu_ns> <majflt> <min_ns> <max_ns>
 *        each call timed on its own (CLOCK_MONOTONIC + CLOCK_PROCESS_CPUTIME_ID
 *        on the BEGIN/END_TIMED lines); the previous result is freed before the
 *        clock starts
 *   LAST -> JSON {engine, threads_used} as reported by the library for the last call
 *   DUMP <path> -> OK   the last result, binary (outside the timed block):
 *        lines:    int64 nlines; per line: float64 level, int64 n, n x (x, y) float64
 *        polygons: int64 npolygons; per polygon: float64 lo, float64 hi, int64 nrings;
 *                  per ring: int64 n, n x (x, y) float64 (ring 0 = exterior)
 *   INFO -> JSON {version, api, field_sum1, field_sum2}
 *   QUIT
 */
#define _GNU_SOURCE
#include <inttypes.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/resource.h>
#include "isosweep.h"

static int g_nx, g_ny, g_nlev, g_bands, g_threads;
static float *g_z;
static double *g_lev;
static uint64_t g_sum1, g_sum2;
static isosweep_grid_t g_grid;
static isosweep_options_t g_opts;
static isosweep_result_t *g_res;
static isosweep_status_t g_status;

static long long clk(clockid_t c) {
    struct timespec t; clock_gettime(c, &t);
    return (long long)t.tv_sec * 1000000000LL + t.tv_nsec;
}

/* One timed call; the clock reads sit on the BEGIN/END_TIMED lines (C06). */
static long long compute(long long *cpu) {
    long long t0, t1, c0, c1;
    isosweep_result_free(g_res);           /* untimed: release the previous result */
    g_res = NULL;
    c0 = clk(CLOCK_PROCESS_CPUTIME_ID); t0 = clk(CLOCK_MONOTONIC);
    /* BEGIN_TIMED */
    g_status = isosweep_contour(&g_grid, g_lev, g_nlev, &g_opts, &g_res);
    /* END_TIMED */
    t1 = clk(CLOCK_MONOTONIC); c1 = clk(CLOCK_PROCESS_CPUTIME_ID);
    *cpu = c1 - c0;
    return t1 - t0;
}

static void dump(const char *path) {
    FILE *f = fopen(path, "wb");
    if (!f || !g_res) { printf("ERR dump\n"); if (f) fclose(f); return; }
    long long cnt = 0;
    fwrite(&cnt, 8, 1, f);
    if (!g_bands) {
        int64_t n = isosweep_result_ncurves(g_res);
        for (int64_t i = 0; i < n; i++) {
            isosweep_curve_t c;
            isosweep_result_curve(g_res, i, &c);
            double lev = g_lev[c.level];
            long long np = c.npoints + (c.closed && c.npoints > 1);   /* closed: repeat vertex 0 */
            fwrite(&lev, 8, 1, f); fwrite(&np, 8, 1, f);
            for (long long k = 0; k < np; k++) {
                int64_t j = k < c.npoints ? k : 0;
                double x = c.xf ? c.xf[j] : c.xd[j], y = c.yf ? c.yf[j] : c.yd[j];
                fwrite(&x, 8, 1, f); fwrite(&y, 8, 1, f);
            }
            cnt++;
        }
    } else {
        isosweep_polygons_t p;
        isosweep_result_polygons(g_res, &p);
        int ne;
        const double *ev;
        isosweep_result_levels(g_res, &ne, &ev, NULL);
        for (int64_t q = 0; q < p.npolygons; q++) {
            int b = p.group[q];
            double lo = b >= 1 ? ev[b - 1] : -INFINITY, hi = b < ne ? ev[b] : INFINITY;
            long long nr = p.ring0[q + 1] - p.ring0[q];
            fwrite(&lo, 8, 1, f); fwrite(&hi, 8, 1, f); fwrite(&nr, 8, 1, f);
            for (int64_t r = p.ring0[q]; r < p.ring0[q + 1]; r++) {
                long long n = p.vertex0[r + 1] - p.vertex0[r];
                fwrite(&n, 8, 1, f);
                for (int64_t k = p.vertex0[r]; k < p.vertex0[r + 1]; k++) {
                    double x = p.xf ? p.xf[k] : p.xd[k], y = p.yf ? p.yf[k] : p.yd[k];
                    fwrite(&x, 8, 1, f); fwrite(&y, 8, 1, f);
                }
            }
            cnt++;
        }
    }
    fseek(f, 0, SEEK_SET);
    fwrite(&cnt, 8, 1, f);
    fclose(f);
    printf("OK\n");
}

int main(void) {
    char line[1 << 16];
    setvbuf(stdout, NULL, _IOLBF, 0);
    while (fgets(line, sizeof line, stdin)) {
        char cmd[16] = {0};
        sscanf(line, "%15s", cmd);
        if (!strcmp(cmd, "LOAD")) {
            char path[4096], mode[64]; char *levbuf = malloc(1 << 20);
            if (sscanf(line, "LOAD %4095s %d %d %1048575s %63s", path, &g_nx, &g_ny, levbuf, mode) != 5) {
                printf("ERR bad LOAD\n"); free(levbuf); continue;
            }
            char *slash = strchr(mode, '/');
            g_threads = slash ? atoi(slash + 1) : 1;
            if (slash) *slash = 0;
            g_bands = !strcmp(mode, "bands");
            size_t n = (size_t)g_nx * g_ny;
            double *zd = malloc(n * sizeof(double));
            FILE *fp = fopen(path, "rb");
            if (!zd || !fp || fread(zd, sizeof(double), n, fp) != n) {
                printf("ERR read\n"); free(zd); free(levbuf); if (fp) fclose(fp); continue;
            }
            fclose(fp);
            g_sum1 = g_sum2 = 0;     /* sum and index-weighted sum of the bits, mod 2^64 */
            free(g_z);
            g_z = malloc(n * sizeof(float));
            for (size_t i = 0; i < n; i++) {
                uint64_t w; memcpy(&w, zd + i, 8);
                g_sum1 += w; g_sum2 += w * (uint64_t)(i + 1);
                g_z[i] = (float)zd[i];
            }
            free(zd);
            g_nlev = 1; for (char *p = levbuf; *p; p++) g_nlev += (*p == ',');
            free(g_lev);
            g_lev = malloc(sizeof(double) * g_nlev);
            int k = 0; for (char *t = strtok(levbuf, ","); t; t = strtok(NULL, ",")) g_lev[k++] = atof(t);
            free(levbuf);
            isosweep_grid_init(&g_grid);
            g_grid.z = g_z; g_grid.dtype = ISOSWEEP_FLOAT32; g_grid.nrows = g_ny; g_grid.ncols = g_nx;
            isosweep_options_init(&g_opts);
            g_opts.output = g_bands ? ISOSWEEP_BANDS : ISOSWEEP_LINES;
            g_opts.engine = ISOSWEEP_ENGINE_MEMORY;
            g_opts.threads = g_threads;
            printf("OK\n");
        } else if (!strcmp(cmd, "RUN")) {
            int loops = 1, markers = 0;
            sscanf(line, "RUN %d %d", &loops, &markers);
            struct rusage ru0, ru1;
            getrusage(RUSAGE_SELF, &ru0);
            long long wall = 0, cpu = 0, mn = -1, mx = 0;
            if (markers) { static const char m[] = "ISOBENCH_T0\n"; fwrite(m, 1, sizeof m - 1, stderr); fflush(stderr); }
            for (int i = 0; i < loops; i++) {
                long long c, d = compute(&c);
                wall += d; cpu += c;
                if (mn < 0 || d < mn) mn = d;
                if (d > mx) mx = d;
            }
            if (markers) { static const char m[] = "ISOBENCH_T1\n"; fwrite(m, 1, sizeof m - 1, stderr); fflush(stderr); }
            getrusage(RUSAGE_SELF, &ru1);
            if (g_status != ISOSWEEP_OK) printf("ERR %s\n", isosweep_status_str(g_status));
            else printf("TIME %lld %lld %ld %lld %lld\n", wall, cpu, ru1.ru_majflt - ru0.ru_majflt, mn, mx);
        } else if (!strcmp(cmd, "LAST")) {
            isosweep_info_t info = { sizeof info };
            if (g_res) isosweep_result_info(g_res, &info);
            printf("{\"engine\":\"%s\",\"threads_used\":%d}\n", g_res ? info.engine : "none", info.threads_used);
        } else if (!strcmp(cmd, "DUMP")) {
            char path[4096];
            if (sscanf(line, "DUMP %4095s", path) == 1) dump(path); else printf("ERR bad DUMP\n");
        } else if (!strcmp(cmd, "INFO")) {
            printf("{\"version\":\"%s\",\"api\":\"isosweep_contour (%s)\",\"field_sum1\":\"%" PRIu64 "\","
                   "\"field_sum2\":\"%" PRIu64 "\"}\n", isosweep_version(), g_bands ? "bands" : "lines",
                   g_sum1, g_sum2);
        } else if (!strcmp(cmd, "QUIT")) {
            break;
        } else {
            printf("ERR unknown command\n");
        }
    }
    isosweep_result_free(g_res);
    return 0;
}
