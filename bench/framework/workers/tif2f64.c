/* tif2f64 — read one band of any GDAL raster as row-major float64 LE.
 * Lets the benchmark node turn GeoTIFF/GRIB inputs into the raw fields the
 * landscape runner feeds every tool, without GDAL Python bindings.
 *
 * Usage: tif2f64 INPUT OUTPUT.f64 [BAND] [XOFF YOFF XSIZE YSIZE]
 * Prints "NX NY" on stdout. Fails on NoData/NaN cells (benchmark fields must
 * be void-free so that every tool contours exactly the same data).
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include "gdal.h"

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: tif2f64 IN OUT [BAND] [XOFF YOFF XS YS]\n"); return 1; }
    GDALAllRegister();
    GDALDatasetH ds = GDALOpen(argv[1], GA_ReadOnly);
    if (!ds) return 1;
    int band = argc > 3 ? atoi(argv[3]) : 1;
    int xo = 0, yo = 0, xs = GDALGetRasterXSize(ds), ys = GDALGetRasterYSize(ds);
    if (argc > 7) { xo = atoi(argv[4]); yo = atoi(argv[5]); xs = atoi(argv[6]); ys = atoi(argv[7]); }
    GDALRasterBandH b = GDALGetRasterBand(ds, band);
    size_t n = (size_t)xs * ys;
    double *buf = malloc(n * sizeof(double));
    if (GDALRasterIO(b, GF_Read, xo, yo, xs, ys, buf, xs, ys, GDT_Float64, 0, 0) != CE_None) return 1;
    int has_nd = 0;
    double nd = GDALGetRasterNoDataValue(b, &has_nd);
    for (size_t i = 0; i < n; i++) {
        if (!isfinite(buf[i]) || (has_nd && buf[i] == nd)) {
            fprintf(stderr, "tif2f64: NoData/NaN at cell %zu — benchmark fields must be void-free\n", i);
            return 2;
        }
    }
    FILE *fp = fopen(argv[2], "wb");
    if (!fp || fwrite(buf, sizeof(double), n, fp) != n) return 1;
    fclose(fp);
    printf("%d %d\n", xs, ys);
    GDALClose(ds);
    return 0;
}
