# Copyright 2026 Parmond AB
# SPDX-License-Identifier: Apache-2.0
"""Filled-band output (class polygons.bands): summaries and like-for-like checks.

Every tool's raw output — polygons (exterior + holes) per band, in the tool's
own frame — goes through this one code path (as geometry.py does for lines):

  normalize   frame → grid-index coordinates (x = column, y = row)
  clip        to the node domain [0, nx-1] x [0, ny-1] (GDAL and contour-rs
              polygonize to the pixel edges, half a cell further out); each
              ring is clipped against the rectangle (Sutherland-Hodgman: exact
              for areas); the clipped area is recorded (W5)
  bands       keyed by band index 0..nlev: band 0 = (-inf, l0), band k =
              [l(k-1), l(k)), band nlev = [l(nlev-1), +inf)
  area        per band: sum of signed ring areas with exteriors positive and
              holes negative, whatever winding a tool uses (exterior = first
              ring of a polygon)
  vertices    per band: distinct vertices strictly inside the domain = the
              edge crossings of the band's two levels (boundary vertices are a
              convention: corners, every boundary node, or pixel edges)
  accuracy    W3: every interior vertex lies on a cell edge at the exact
              crossing of the band's lower or upper level
  degenerate  rings with fewer than 3 distinct points, or smaller than 1e-4
              cells, carry no area and are not counted (recorded for
              disclosure; e.g. GDAL's MAX keyword rings the max node)

Checks (compare_bands): total area == domain area (exact oracle); per band
area and interior vertices vs the leave-one-out family median (isosweep never
votes), with the saddle budget computed from the field; polygons per band
(W2, assembly into polygons with holes); W3 accuracy.
"""
import math
import statistics

import numpy as np

from .geometry import FRAMES, snap_ties, vertex_error


def _area(a):
    x, y = a[:, 0] - a[0, 0], a[:, 1] - a[0, 1]
    return 0.5 * float(np.dot(x[:-1], y[1:]) - np.dot(x[1:], y[:-1]) + x[-1] * y[0] - x[0] * y[-1])


def _clip_ring(a, x1, y1):
    """Sutherland-Hodgman against [0,x1] x [0,y1]. Exact for the clipped area."""
    pts = [tuple(p) for p in a]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    for axis, bound, keep_le in ((0, 0.0, False), (0, x1, True), (1, 0.0, False), (1, y1, True)):
        if not pts:
            break
        out = []
        inside = (lambda p: p[axis] <= bound) if keep_le else (lambda p: p[axis] >= bound)  # noqa: E731
        prev = pts[-1]
        for cur in pts:
            ci, pi = inside(cur), inside(prev)
            if ci != pi:
                t = (bound - prev[axis]) / (cur[axis] - prev[axis])
                q = [prev[0] + t * (cur[0] - prev[0]), prev[1] + t * (cur[1] - prev[1])]
                q[axis] = bound
                out.append(tuple(q))
            if ci:
                out.append(cur)
            prev = cur
        pts = out
    return np.asarray(pts, dtype=np.float64).reshape(-1, 2)


def band_index(lo, hi, levels):
    """(lo, hi) → 0..nlev (None / ±inf for the outer bands; outer bands may also
    be given with the field's min / max, as GDAL's MIN / MAX keywords do)."""
    lev = np.asarray(levels, dtype=np.float64)
    tol = 1e-9 * (np.abs(lev).max() + 1.0)
    if lo is not None and np.isfinite(lo):
        i = int(np.argmin(np.abs(lev - lo)))
        if abs(lev[i] - lo) <= tol:
            return i + 1
    if hi is not None and np.isfinite(hi):
        i = int(np.argmin(np.abs(lev - hi)))
        if abs(lev[i] - hi) <= tol:
            return i
    return -1


def summarize_bands(polys, frame, z, levels, max_check=400000):
    """polys: [(lo, hi, [exterior, hole, ...])] in the tool's frame."""
    ny, nx = z.shape
    x1, y1 = nx - 1.0, ny - 1.0
    swap, off = FRAMES[frame]
    lev = np.asarray(levels, dtype=np.float64)
    per, verts = {}, {}
    clipped_area, n_polys, n_holes = 0.0, 0, 0
    errs, offedge, checked, unknown, degenerate, over, outer_bound = [], 0, 0, 0, 0, 0, 0
    span = float(z.max() - z.min()) or 1.0
    allv = sum(len(r) for _, _, rings in polys for r in rings)
    stride = max(1, allv // max_check)
    k = 0
    eps = 1e-9
    for lo, hi, rings in polys:
        bi = band_index(lo, hi, lev)
        if bi < 0:
            unknown += 1
            continue
        key = str(bi)
        e = per.setdefault(key, {"area": 0.0, "polygons": 0, "holes": 0, "points": 0})
        e["polygons"] += 1
        n_polys += 1
        for j, r in enumerate(rings):
            a = np.asarray(r, dtype=np.float64)
            if a.ndim != 2 or len(a) < 3:
                continue
            ext = a.max(axis=0) - a.min(axis=0)
            if len(np.unique(a, axis=0)) < 3 or (ext[0] < 1e-4 and ext[1] < 1e-4):
                degenerate += 1          # e.g. GDAL's ~1e-8 ring around the field's max node (MAX keyword)
                continue
            if swap:
                a = a[:, ::-1]
            if off:
                a = a - off
            raw = abs(_area(a))
            if a[:, 0].min() < 0 or a[:, 1].min() < 0 or a[:, 0].max() > x1 or a[:, 1].max() > y1:
                c = _clip_ring(a, x1, y1)
                ca = abs(_area(c)) if len(c) >= 3 else 0.0
                clipped_area += (raw - ca) * (1.0 if j == 0 else -1.0)   # net: the border strip covered
                raw = ca
            sgn = 1.0 if j == 0 else -1.0
            e["area"] += sgn * raw
            if j:
                e["holes"] += 1
                n_holes += 1
            inner = a[(a[:, 0] > eps) & (a[:, 1] > eps) & (a[:, 0] < x1 - eps) & (a[:, 1] < y1 - eps)]
            own = [float(v) for v in (lo, hi) if v is not None and np.isfinite(v)
                   and not np.any(lev == float(v))]
            if len(inner) and own:
                # crossings of a tool's own outer bound (GDAL's MIN / MAX keywords contour the
                # field's extremes): not benchmark work, not counted (recorded)
                bench = [lev[i] for i in (bi - 1, bi) if 0 <= i < len(lev)]
                eb = np.fmin.reduce([vertex_error(z, inner[:, 0], inner[:, 1], L, span)[0] for L in bench]) \
                    if bench else np.full(len(inner), np.inf)
                eo = np.fmin.reduce([vertex_error(z, inner[:, 0], inner[:, 1], L, span)[0] for L in own])
                ob = ~(eb <= 1e-3) & (eo <= 1e-3)
                outer_bound += int(ob.sum())
                inner = inner[~ob]
            if len(inner):
                verts.setdefault(key, []).append(inner)
                sel = inner[(k + np.arange(len(inner))) % stride == 0]
                k += len(inner)
                if len(sel):
                    # the band's two levels (and a tool's own finite outer bound, e.g. GDAL MIN/MAX)
                    cand = [lev[i] for i in (bi - 1, bi) if 0 <= i < len(lev)]
                    cand += [float(v) for v in (lo, hi) if v is not None and np.isfinite(v)]
                    err = np.fmin.reduce([vertex_error(z, sel[:, 0], sel[:, 1], L, span)[0] for L in cand])
                    ok = np.isfinite(err)
                    offedge += int((~ok).sum())
                    over += int((~ok).sum()) + int((err[ok] > 1e-5).sum())
                    checked += int(ok.sum())
                    if ok.any():
                        errs.append(float(np.max(err[ok])))
    n_pts = 0
    for key, arrs in verts.items():
        u = len(np.unique(snap_ties(np.vstack(arrs)), axis=0))
        per[key]["points"] = u
        n_pts += u
    return {"bands": len(per), "polygons": n_polys, "holes": n_holes, "points": n_pts,
            "total_area": sum(v["area"] for v in per.values()), "per_band": per,
            "clipped_area": clipped_area, "unmatched_polygons": unknown, "degenerate_rings": degenerate,
            "outer_bound_vertices": outer_bound,
            "accuracy": {"max_err_cells": max(errs) if errs else 0.0,
                         "checked_vertices": checked, "off_edge_vertices": offedge,
                         "vertices_over_1e-5": over}}


def saddle_area_budget(z, levels):
    """Per level L: sum over saddle cells of |area(z >= L) under the two
    pairings| — the most a saddle convention can move the area above L.
    Band k's budget = budget(level k-1) + budget(level k)."""
    z = np.asarray(z, dtype=np.float64)
    a, b, c, d = z[:-1, :-1], z[:-1, 1:], z[1:, 1:], z[1:, :-1]     # TL TR BR BL
    out = []
    for L in np.asarray(levels, dtype=np.float64):
        A, B, C, D = a >= L, b >= L, c >= L, d >= L
        sad = (A == C) & (B == D) & (A != B)
        ties = int((z == L).sum())       # a node on the level: pieces may touch there (split or merged)
        if not sad.any():
            out.append({"cells": 0, "area": 0.0, "ties": ties})
            continue
        aa, bb, cc, dd = a[sad], b[sad], c[sad], d[sad]
        with np.errstate(divide="ignore", invalid="ignore"):
            tt = (L - aa) / (bb - aa)          # crossing on top edge, from TL
            tr = (L - bb) / (cc - bb)          # right edge, from TR (downwards)
            tb = (L - dd) / (cc - dd)          # bottom edge, from BL
            tl = (L - aa) / (dd - aa)          # left edge, from TL (downwards)
        # corner triangles cut off by the crossing segments (legs along the cell edges)
        tri_tl = 0.5 * tt * tl
        tri_tr = 0.5 * (1 - tt) * tr
        tri_br = 0.5 * (1 - tr) * (1 - tb)
        tri_bl = 0.5 * tb * (1 - tl)
        hi_tl = A[sad]                          # TL (and BR) above the level
        sep_hi = np.where(hi_tl, tri_tl + tri_br, tri_tr + tri_bl)       # high corners separated
        join_hi = 1.0 - np.where(hi_tl, tri_tr + tri_bl, tri_tl + tri_br)  # high corners joined
        out.append({"cells": int(sad.sum()), "area": float(np.abs(join_hi - sep_hi).sum()), "ties": ties})
    return out


def _above_areas(a, b, c, d, L, above):
    """Area of the region above the level inside each unit cell (corners TL,
    TR, BR, BL) when a node counts as above where above(z) holds. Returns
    (one, other): equal except in saddle cells, where they are the two
    pairings (high corners joined / separated)."""
    A, B, C, D = above(a), above(b), above(c), above(d)

    def t(p, q):
        with np.errstate(divide="ignore", invalid="ignore"):
            v = np.where(q != p, (L - p) / (q - p), 0.0)
        return np.clip(np.nan_to_num(v), 0.0, 1.0)
    tt, tr, tb, tl = t(a, b), t(b, c), t(d, c), t(a, d)
    # boundary walk TL -> top -> TR -> right -> BR -> bottom -> BL -> left, keeping
    # above corners and every crossing; shoelace over the kept points
    pts = [((0.0, 0.0), A), ((tt, 0.0), A != B), ((1.0, 0.0), B), ((1.0, tr), B != C),
           ((1.0, 1.0), C), ((tb, 1.0), C != D), ((0.0, 1.0), D), ((0.0, tl), D != A)]
    n = len(a)
    xs = np.stack([np.broadcast_to(np.asarray(p[0][0], dtype=float), (n,)) for p in pts], axis=1)
    ys = np.stack([np.broadcast_to(np.asarray(p[0][1], dtype=float), (n,)) for p in pts], axis=1)
    keep = np.stack([np.broadcast_to(p[1], (n,)) for p in pts], axis=1)
    area = np.zeros(n)
    for i in range(n):                       # cells touching tied nodes: few
        k = keep[i]
        if not k.any():
            continue
        x, y = xs[i][k], ys[i][k]
        area[i] = 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))
    sad = (A == C) & (B == D) & (A != B)
    other = area.copy()
    if sad.any():                            # separated: the two high-corner triangles only
        hi_tl = A[sad]
        tri_tl, tri_tr = 0.5 * tt[sad] * tl[sad], 0.5 * (1 - tt[sad]) * tr[sad]
        tri_br, tri_bl = 0.5 * (1 - tr[sad]) * (1 - tb[sad]), 0.5 * tb[sad] * (1 - tl[sad])
        other[sad] = np.where(hi_tl, tri_tl + tri_br, tri_tr + tri_bl)
    return area, other


def tie_area_budget(z, levels):
    """Per level L: what a tie convention alone can move the area above L — a
    node equal to L is above for some tools (z >= L), below for others
    (z > L). Sum over cells touching a tied node of the largest difference
    between the cell's area above L under the two conventions (saddle cells:
    over both pairings). Band k's budget adds levels k-1 and k."""
    z = np.asarray(z, dtype=np.float64)
    a, b, c, d = z[:-1, :-1], z[:-1, 1:], z[1:, 1:], z[1:, :-1]
    out = []
    for L in np.asarray(levels, dtype=np.float64):
        tied = int(np.count_nonzero(z == L))
        if not tied:
            out.append({"nodes": 0, "area": 0.0})
            continue
        m = (a == L) | (b == L) | (c == L) | (d == L)
        aa, bb, cc, dd = a[m], b[m], c[m], d[m]
        g1, g2 = _above_areas(aa, bb, cc, dd, L, lambda v: v >= L)
        s1, s2 = _above_areas(aa, bb, cc, dd, L, lambda v: v > L)
        diff = np.max(np.abs(np.stack([g1 - s1, g1 - s2, g2 - s1, g2 - s2])), axis=0)
        out.append({"nodes": tied, "area": float(diff.sum())})
    return out


def _rel(v, ref):
    return abs(v - ref) / ref if ref else (0.0 if v == ref else math.inf)


def compare_bands(summaries, families, waivers, tol, domain_area, budget, ours="isosweep", precision=None,
                  ties=None):
    precision = precision or {}
    fams = sorted({families[c] for c in summaries if families[c] != ours})
    if len(fams) < 2:
        return [{"check": "consensus", "ok": False, "waived": False,
                 "detail": f"need >= 2 independent families, have {fams}"}]

    def fam_metric(fam, fn):
        return statistics.median(fn(summaries[c]) for c in summaries if families[c] == fam)

    nb = len(budget) + 1
    b_area = [0.0] * nb
    b_cells = [0] * nb
    for i, v in enumerate(budget):                 # level i bounds bands i and i+1
        for k in (i, i + 1):
            b_area[k] += v["area"]
            b_cells[k] += v["cells"] + 2 * v.get("ties", 0)
    for i, v in enumerate(ties or []):             # tie conventions move area across level i
        for k in (i, i + 1):
            b_area[k] += v["area"]
    findings = []
    for cid, s in summaries.items():
        own = families[cid]
        refs = [f for f in fams if f != own]
        w = waivers.get(cid, {})

        def ref(fn, check=None):
            # the reference for a check comes only from outputs not waived for it
            pool = {c for c in summaries if not (check and check in waivers.get(c, {}))}
            fs = [f for f in refs if any(families[c] == f for c in pool)] or refs
            pool = pool or set(summaries)
            return statistics.median(statistics.median(fn(summaries[c]) for c in pool if families[c] == f)
                                     if any(families[c] == f for c in pool) else fam_metric(f, fn) for f in fs)

        def add(check, ok, detail, direction=None):
            if not ok and check in w:
                mx = w[check].get("max_rel")
                within = mx is not None and detail.get("rel", math.inf) <= mx
                if w[check]["direction"] == "favours_isosweep" or (direction and w[check]["direction"] != direction):
                    within = False
                findings.append({"tool": cid, "check": check, "ok": within, "waived": True,
                                 "waiver_direction": w[check]["direction"], "detail": detail})
            else:
                findings.append({"tool": cid, "check": check, "ok": ok, "waived": False, "detail": detail})

        ta = s["total_area"]
        add("B1_total_area", abs(ta - domain_area) <= tol["total_area_rel_tol"] * domain_area,
            {"rel": _rel(ta, domain_area), "value": ta, "domain_area": domain_area})
        add("B1_all_bands", s["bands"] == nb and s["unmatched_polygons"] == 0,
            {"bands": s["bands"], "expected": nb, "unmatched_polygons": s["unmatched_polygons"]})
        worst_a = worst_p = worst_n = (0.0, None)
        for k in range(nb):
            key = str(k)
            v = s["per_band"].get(key, {"area": 0.0, "points": 0, "polygons": 0})
            ra = ref(lambda x: x["per_band"].get(key, {"area": 0.0})["area"])
            rp = ref(lambda x: x["per_band"].get(key, {"points": 0})["points"], "B1_band_points")
            rn = ref(lambda x: x["per_band"].get(key, {"polygons": 0})["polygons"], "B2_band_polygons")
            ea = abs(v["area"] - ra) - max(tol["band_area_rel_tol"] * ra, tol["band_area_abs"]) - b_area[k]
            ep = abs(v["points"] - rp) - max(tol["band_points_rel_tol"] * rp, tol["band_points_abs"])
            en = abs(v["polygons"] - rn) - max(tol["band_polygons_rel_tol"] * rn, 1) - b_cells[k]
            if worst_a[1] is None or ea > worst_a[0]:
                worst_a = (ea, {"band": key, "value": v["area"], "reference": ra, "rel": _rel(v["area"], ra),
                                "saddle_and_tie_area": b_area[k]})
            if worst_p[1] is None or ep > worst_p[0]:
                worst_p = (ep, {"band": key, "value": v["points"], "reference": rp, "rel": _rel(v["points"], rp)})
            if worst_n[1] is None or en > worst_n[0]:
                worst_n = (en, {"band": key, "value": v["polygons"], "reference": rn,
                                "rel": _rel(v["polygons"], rn), "saddle_cells": b_cells[k]})
        add("B1_band_area", worst_a[0] <= 0, worst_a[1])
        add("B1_band_points", worst_p[0] <= 0, worst_p[1])
        add("B2_band_polygons", worst_n[0] <= 0, worst_n[1],
            direction="favours_competitor" if worst_n[1]["value"] > worst_n[1]["reference"] else None)
        acc = s["accuracy"]
        lim = tol["accuracy_cells"].get(precision.get(cid, "float64"))
        add("B3_accuracy", acc["off_edge_vertices"] == 0 and acc["max_err_cells"] <= lim,
            dict(acc, limit=lim, precision=precision.get(cid, "float64"),
                 rel=acc.get("vertices_over_1e-5", 0) / max(acc["checked_vertices"], 1)),
            direction="favours_competitor")
    return findings
