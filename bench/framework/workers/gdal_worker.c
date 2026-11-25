/* Copyright 2026 Parmond AB
 * SPDX-License-Identifier: Apache-2.0 */
/* gdal_worker — GDAL contour engine under the isobench worker protocol.
 *
 * Protocol (one command per stdin line, one reply per stdout line):
 *   LOAD <field.f64> <nx> <ny> <levels_csv> <api: callback|generate_ex|polygonize>  -> OK
 *        polygonize = GDALContourGenerateEx POLYGONIZE=YES with FIXED_LEVELS=MIN,<levels>,MAX:
 *        filled bands between consecutive levels plus both outer bands
 *   RUN <loops> <markers 0|1>  -> TIME <wall_ns> <cpu_ns> <majflt> <min_ns> <max_ns>
 *        each call timed on its own (CLOCK_MONOTONIC + CLOCK_PROCESS_CPUTIME_ID
 *        around the BEGIN_TIMED block only); resetting the previous result
 *        (callback buffer, in-memory layer) happens before the clock starts
 *   DUMP <path> -> OK   the last result, binary (outside the timed block), in
 *        GDAL's own coordinates (the harness normalises frames):
 *        lines:    int64 nlines; per line: float64 level, int64 n, n x (x, y) float64
 *        polygons: int64 npolygons; per polygon: float64 lo, float64 hi, int64 nrings;
 *                  per ring: int64 n, n x (x, y) float64 (ring 0 = exterior)
 *   INFO -> JSON {version, api, lib, field_sum1, field_sum2}
 *        lib = path of the libgdal actually loaded (R8: hashed); field_sum* =
 *        checksums of the field as loaded (R11: compared with the harness's array)
 *   QUIT
 * Everything a static audit must see as "timed" is between BEGIN_TIMED and
 * END_TIMED. Markers (fd 2) bracket one compute for the strace validation pass.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <inttypes.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/resource.h>
#include "cpl_string.h"
#include "gdal.h"
#include "gdal_alg.h"
#include "ogr_api.h"

static double *g_z;
static int g_nx, g_ny, g_nlev, g_api_cb, g_poly;
static double *g_lev;
static char *g_lev_csv;
static uint64_t g_sum1, g_sum2;  /* field checksums (LOAD) */
static double g_base, g_interval;
static GDALDatasetH g_vds;       /* in-memory vector dataset (generate_ex), created in LOAD */
static GDALRasterBandH g_band;   /* MEM raster band (generate_ex), created in LOAD */
static char **g_opts;            /* GDALContourGenerateEx options, built in LOAD */
static OGRLayerH g_lyr;          /* fresh output layer for the next call (untimed) */

/* result storage for the callback API: every polyline copied (as any caller must) */
static double *r_x, *r_y, *r_lev; static long *r_off; static long r_npts, r_cap, r_nlines, r_lcap;

static CPLErr writer(double level, int n, double *x, double *y, void *ud) {
    (void)ud;
    /* BEGIN_TIMED callee (runs inside GDAL_CG_FeedLine, i.e. inside the clocked block) */
    if (r_npts + n > r_cap) {
        r_cap = (r_npts + n) * 2;
        r_x = realloc(r_x, sizeof(double) * r_cap);
        r_y = realloc(r_y, sizeof(double) * r_cap);
    }
    if (r_nlines + 2 > r_lcap) {
        r_lcap = (r_nlines + 2) * 2;
        r_off = realloc(r_off, sizeof(long) * r_lcap);
        r_lev = realloc(r_lev, sizeof(double) * r_lcap);
    }
    memcpy(r_x + r_npts, x, sizeof(double) * n);
    memcpy(r_y + r_npts, y, sizeof(double) * n);
    r_off[r_nlines] = r_npts;
    r_lev[r_nlines] = level;
    r_nlines++;
    r_npts += n;
    r_off[r_nlines] = r_npts;
    /* END_TIMED */
    return CE_None;
}

static long long clk(clockid_t c) {
    struct timespec t; clock_gettime(c, &t);
    return (long long)t.tv_sec * 1000000000LL + t.tv_nsec;
}

/* Untimed per-call reset: an empty result buffer / a fresh empty output layer
   (the previous call's layer is released here, outside the clock: R10). */
static void reset_result(void) {
    if (g_api_cb) {
        r_npts = 0; r_nlines = 0;
        return;
    }
    if (GDALDatasetGetLayerCount(g_vds) > 0) GDALDatasetDeleteLayer(g_vds, 0);
    g_lyr = GDALDatasetCreateLayer(g_vds, "contour", NULL, g_poly ? wkbMultiPolygon : wkbLineString, NULL);
    OGRFieldDefnH fid = OGR_Fld_Create("ID", OFTInteger);
    OGR_L_CreateField(g_lyr, fid, 0); OGR_Fld_Destroy(fid);
    OGRFieldDefnH fel = OGR_Fld_Create(g_poly ? "EMIN" : "ELEV", OFTReal);
    OGR_L_CreateField(g_lyr, fel, 0); OGR_Fld_Destroy(fel);
    if (g_poly) {
        OGRFieldDefnH fmx = OGR_Fld_Create("EMAX", OFTReal);
        OGR_L_CreateField(g_lyr, fmx, 0); OGR_Fld_Destroy(fmx);
    }
}

/* One timed call. The clock reads sit directly on the BEGIN/END_TIMED lines
   (checked by the static audit, R11): the region the clock measures is the
   region the audit analyses. → wall ns; *cpu = process CPU ns. */
static long long compute(long long *cpu) {
    long long t0, t1, c0, c1;
    if (g_api_cb) {
        c0 = clk(CLOCK_PROCESS_CPUTIME_ID); t0 = clk(CLOCK_MONOTONIC);
        /* BEGIN_TIMED */
        GDALContourGeneratorH h = GDAL_CG_Create(g_nx, g_ny, 0, 0.0, g_interval, g_base, writer, NULL);
        for (int r = 0; r < g_ny; r++) GDAL_CG_FeedLine(h, g_z + (size_t)r * g_nx);
        GDAL_CG_Destroy(h);
        /* END_TIMED */
        t1 = clk(CLOCK_MONOTONIC); c1 = clk(CLOCK_PROCESS_CPUTIME_ID);
    } else {
        c0 = clk(CLOCK_PROCESS_CPUTIME_ID); t0 = clk(CLOCK_MONOTONIC);
        /* BEGIN_TIMED */
        GDALContourGenerateEx(g_band, g_lyr, g_opts, NULL, NULL);
        /* END_TIMED */
        t1 = clk(CLOCK_MONOTONIC); c1 = clk(CLOCK_PROCESS_CPUTIME_ID);
    }
    *cpu = c1 - c0;
    return t1 - t0;
}


static void dump(const char *path) {
    FILE *f = fopen(path, "wb");
    if (!f) { printf("ERR dump\n"); return; }
    long long nl = 0;
    fwrite(&nl, 8, 1, f);
    if (g_api_cb) {
        for (long i = 0; i < r_nlines; i++) {
            long long n = r_off[i + 1] - r_off[i];
            fwrite(&r_lev[i], 8, 1, f); fwrite(&n, 8, 1, f);
            for (long k = r_off[i]; k < r_off[i + 1]; k++) { fwrite(&r_x[k], 8, 1, f); fwrite(&r_y[k], 8, 1, f); }
            nl++;
        }
    } else if (g_poly) {
        OGRLayerH lyr = GDALDatasetGetLayer(g_vds, 0);
        OGR_L_ResetReading(lyr);
        OGRFeatureH ft;
        while ((ft = OGR_L_GetNextFeature(lyr)) != NULL) {
            OGRGeometryH mp = OGR_F_GetGeometryRef(ft);
            double lo = OGR_F_GetFieldAsDouble(ft, 1), hi = OGR_F_GetFieldAsDouble(ft, 2);
            int np_ = mp ? OGR_G_GetGeometryCount(mp) : 0;
            for (int p = 0; p < np_; p++) {
                OGRGeometryH pg = OGR_G_GetGeometryRef(mp, p);
                long long nr = OGR_G_GetGeometryCount(pg);
                fwrite(&lo, 8, 1, f); fwrite(&hi, 8, 1, f); fwrite(&nr, 8, 1, f);
                for (int r = 0; r < nr; r++) {
                    OGRGeometryH ring = OGR_G_GetGeometryRef(pg, r);
                    long long n = OGR_G_GetPointCount(ring);
                    fwrite(&n, 8, 1, f);
                    for (int k = 0; k < n; k++) {
                        double x = OGR_G_GetX(ring, k), y = OGR_G_GetY(ring, k);
                        fwrite(&x, 8, 1, f); fwrite(&y, 8, 1, f);
                    }
                }
                nl++;
            }
            OGR_F_Destroy(ft);
        }
    } else {
        OGRLayerH lyr = GDALDatasetGetLayer(g_vds, 0);
        OGR_L_ResetReading(lyr);
        OGRFeatureH ft;
        while ((ft = OGR_L_GetNextFeature(lyr)) != NULL) {
            OGRGeometryH g = OGR_F_GetGeometryRef(ft);
            double lev = OGR_F_GetFieldAsDouble(ft, 1);
            long long n = OGR_G_GetPointCount(g);
            fwrite(&lev, 8, 1, f); fwrite(&n, 8, 1, f);
            for (int k = 0; k < n; k++) {
                double x = OGR_G_GetX(g, k), y = OGR_G_GetY(g, k);
                fwrite(&x, 8, 1, f); fwrite(&y, 8, 1, f);
            }
            nl++;
            OGR_F_Destroy(ft);
        }
    }
    fseek(f, 0, SEEK_SET);
    fwrite(&nl, 8, 1, f);
    fclose(f);
    printf("OK\n");
}

int main(void) {
    GDALAllRegister();
    char line[1 << 16];
    setvbuf(stdout, NULL, _IOLBF, 0);
    while (fgets(line, sizeof line, stdin)) {
        char cmd[16] = {0};
        sscanf(line, "%15s", cmd);
        if (!strcmp(cmd, "LOAD")) {
            char path[4096], api[32]; char *levbuf = malloc(1 << 20);
            if (sscanf(line, "LOAD %4095s %d %d %1048575s %31s", path, &g_nx, &g_ny, levbuf, api) != 5) {
                printf("ERR bad LOAD\n"); continue;
            }
            size_t n = (size_t)g_nx * g_ny;
            g_z = malloc(n * sizeof(double));
            FILE *fp = fopen(path, "rb");
            if (!fp || fread(g_z, sizeof(double), n, fp) != n) { printf("ERR read\n"); continue; }
            fclose(fp);
            g_sum1 = g_sum2 = 0;     /* sum and index-weighted sum of the bits, mod 2^64 */
            for (size_t i = 0; i < n; i++) {
                uint64_t w; memcpy(&w, g_z + i, 8);
                g_sum1 += w; g_sum2 += w * (uint64_t)(i + 1);
            }
            g_lev_csv = strdup(levbuf);
            g_nlev = 1; for (char *p = levbuf; *p; p++) g_nlev += (*p == ',');
            g_lev = malloc(sizeof(double) * g_nlev);
            int k = 0; for (char *t = strtok(levbuf, ","); t; t = strtok(NULL, ",")) g_lev[k++] = atof(t);
            free(levbuf);
            g_api_cb = !strcmp(api, "callback");
            g_poly = !strcmp(api, "polygonize");
            if (g_api_cb) {
                /* GDAL_CG_* takes base + interval: the benchmark's levels must be uniform */
                g_interval = g_nlev > 1 ? g_lev[1] - g_lev[0] : 1.0;
                for (int i = 2; i < g_nlev; i++)
                    if (fabs((g_lev[i] - g_lev[i - 1]) - g_interval) > 1e-9 * fabs(g_interval) + 1e-12) {
                        printf("ERR callback API needs uniformly spaced levels\n"); goto next;
                    }
                g_base = g_lev[0];
            } else {
                GDALDatasetH rds = GDALCreate(GDALGetDriverByName("MEM"), "", g_nx, g_ny, 1, GDT_Float64, NULL);
                g_band = GDALGetRasterBand(rds, 1);
                if (GDALRasterIO(g_band, GF_Write, 0, 0, g_nx, g_ny, g_z, g_nx, g_ny, GDT_Float64, 0, 0) != CE_None) {
                    printf("ERR rasterio\n"); goto next;
                }
                double gt[6] = {0, 1, 0, 0, 0, 1};
                GDALSetGeoTransform(rds, gt);
                GDALDriverH vdrv = GDALGetDriverByName("MEM");
                g_vds = GDALCreate(vdrv, "out", 0, 0, 0, GDT_Unknown, NULL);
                if (g_poly) {
                    /* filled bands incl. both outer bands (MIN / MAX keywords) */
                    char *fl = malloc(strlen(g_lev_csv) + 16);
                    sprintf(fl, "MIN,%s,MAX", g_lev_csv);
                    g_opts = CSLSetNameValue(NULL, "FIXED_LEVELS", fl);
                    free(fl);
                    g_opts = CSLSetNameValue(g_opts, "ID_FIELD", "0");
                    g_opts = CSLSetNameValue(g_opts, "ELEV_FIELD_MIN", "1");
                    g_opts = CSLSetNameValue(g_opts, "ELEV_FIELD_MAX", "2");
                    g_opts = CSLSetNameValue(g_opts, "POLYGONIZE", "YES");
                } else {
                    g_opts = CSLSetNameValue(NULL, "FIXED_LEVELS", g_lev_csv);
                    g_opts = CSLSetNameValue(g_opts, "ID_FIELD", "0");
                    g_opts = CSLSetNameValue(g_opts, "ELEV_FIELD", "1");
                }
            }
            printf("OK\n");
        } else if (!strcmp(cmd, "RUN")) {
            int loops = 1, markers = 0;
            sscanf(line, "RUN %d %d", &loops, &markers);
            struct rusage ru0, ru1;
            getrusage(RUSAGE_SELF, &ru0);
            long long wall = 0, cpu = 0, mn = -1, mx = 0;
            if (markers) { static const char m[] = "ISOBENCH_T0\n"; fwrite(m, 1, sizeof m - 1, stderr); fflush(stderr); }
            for (int i = 0; i < loops; i++) {
                reset_result();
                long long c, d = compute(&c);
                wall += d; cpu += c;
                if (mn < 0 || d < mn) mn = d;
                if (d > mx) mx = d;
            }
            if (markers) { static const char m[] = "ISOBENCH_T1\n"; fwrite(m, 1, sizeof m - 1, stderr); fflush(stderr); }
            getrusage(RUSAGE_SELF, &ru1);
            printf("TIME %lld %lld %ld %lld %lld\n", wall, cpu, ru1.ru_majflt - ru0.ru_majflt, mn, mx);
        } else if (!strcmp(cmd, "DUMP")) {
            char path[4096];
            if (sscanf(line, "DUMP %4095s", path) == 1) dump(path); else printf("ERR bad DUMP\n");
        } else if (!strcmp(cmd, "INFO")) {
            Dl_info dl;
            const char *lib = dladdr((void *)GDALVersionInfo, &dl) && dl.dli_fname ? dl.dli_fname : "";
            printf("{\"version\":\"%s\",\"api\":\"%s\",\"lib\":\"%s\",\"field_sum1\":\"%" PRIu64 "\","
                   "\"field_sum2\":\"%" PRIu64 "\"}\n", GDALVersionInfo("RELEASE_NAME"),
                   g_api_cb ? "GDAL_CG_* callback" : g_poly ? "GDALContourGenerateEx POLYGONIZE (in-memory layer)"
                   : "GDALContourGenerateEx (in-memory layer)", lib, g_sum1, g_sum2);
        } else if (!strcmp(cmd, "QUIT")) {
            break;
        } else {
            printf("ERR unknown command\n");
        }
    next:;
    }
    return 0;
}
