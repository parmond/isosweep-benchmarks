"""Summary statistics and the confidence interval of a speed ratio.

The ratio CI is distribution-free (R9): Hodges–Lehmann estimate of the shift
between log-times, with the Mann–Whitney (Bauer 1972) confidence bounds from
the order statistics of all m*n pairwise differences. With n = 30 samples
per configuration this is exact enough (normal approximation of the
Wilcoxon rank-sum quantile) and makes no distributional assumption; the
estimate is the median of pairwise ratios, the bounds are order statistics.
"""
import math
import statistics


def summarize(samples):
    s = sorted(samples)
    n = len(s)
    q = statistics.quantiles(s, n=4, method="inclusive") if n >= 2 else [s[0]] * 3
    mean = statistics.fmean(s)
    sd = statistics.stdev(s) if n >= 2 else 0.0
    return {"n": n, "median": statistics.median(s), "min": s[0], "max": s[-1],
            "q1": q[0], "q3": q[2], "mean": mean, "cv": (sd / mean) if mean > 0 else 0.0}


def _z(level):
    # two-sided standard normal quantile (inverse via erfc bisection; no scipy)
    target = (1 - level) / 2
    lo, hi = 0.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if 0.5 * math.erfc(mid / math.sqrt(2)) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def ratio_ci(num, den, level=0.95):
    """Speed ratio num/den (e.g. competitor time / isosweep time) with a
    distribution-free CI: (estimate, lo, hi). Hodges–Lehmann on log-times."""
    m, n = len(num), len(den)
    d = sorted(math.log(a) - math.log(b) for a in num for b in den)
    N = m * n
    est = math.exp(statistics.median(d))
    k = int(math.floor(N / 2 - _z(level) * math.sqrt(m * n * (m + n + 1) / 12.0)))
    k = max(0, min(k, N - 1))
    return est, math.exp(d[k]), math.exp(d[N - 1 - k])
