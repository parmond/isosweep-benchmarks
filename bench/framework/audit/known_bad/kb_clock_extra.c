/* Known-bad native worker: markers are bracketed correctly, but a second clock
   read elsewhere defines the region that is actually reported. */
#include <time.h>
static long long clk(clockid_t c) { struct timespec t; clock_gettime(c, &t); return t.tv_sec * 1000000000LL + t.tv_nsec; }
long long run(void) {
    long long outer = clk(CLOCK_MONOTONIC);
    long long t0 = clk(CLOCK_MONOTONIC);
    /* BEGIN_TIMED */
    GDALContourGenerateEx(0, 0, 0, 0, 0);
    /* END_TIMED */
    long long t1 = clk(CLOCK_MONOTONIC);
    (void)t0; (void)t1;
    return clk(CLOCK_MONOTONIC) - outer;
}
