/* Known-bad native worker: the clock starts before untimed-looking work
   (file I/O outside the markers is still inside the measured region). */
#include <stdio.h>
#include <time.h>
static long long clk(clockid_t c) { struct timespec t; clock_gettime(c, &t); return t.tv_sec * 1000000000LL + t.tv_nsec; }
long long run(const char *path, double *z, int n) {
    long long t0 = clk(CLOCK_MONOTONIC);
    FILE *f = fopen(path, "rb");
    fread(z, sizeof(double), n, f);
    fclose(f);
    /* BEGIN_TIMED */
    GDALContourGenerateEx(0, 0, 0, 0, 0);
    /* END_TIMED */
    return clk(CLOCK_MONOTONIC) - t0;
}
