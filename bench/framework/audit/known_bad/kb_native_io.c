/* Copyright 2026 Parmond AB
 * SPDX-License-Identifier: Apache-2.0 */
/* Known-bad native worker fragment: file I/O inside the timed block. */
#include <stdio.h>
int main(int argc, char **argv) {
    char buf[64];
    fputs("ISOBENCH_T0\n", stderr); fflush(stderr);
    /* BEGIN_TIMED */
    FILE *f = fopen(argv[1], "rb");
    fread(buf, 1, sizeof buf, f);
    fclose(f);
    /* END_TIMED */
    fputs("ISOBENCH_T1\n", stderr); fflush(stderr);
    return 0;
}
