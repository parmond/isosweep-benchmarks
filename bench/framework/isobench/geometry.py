"""Output summaries (computed untimed) and like-for-like checks (W1-W3, R4).

Every tool's raw output — polylines in the tool's own coordinate frame — goes
through ONE code path here, so no tool's numbers come from different code:

  normalize   to grid-index coordinates (x = column, y = row; node (r, c) at
              (c, r)) from the frame the tool's spec declares (W3 proves it)
  clip        to the node domain [0, nx-1] x [0, ny-1]: tools that extend
              contours half a cell beyond the outer nodes (pixel-is-area: GDAL,
              contour-rs) are compared on the same domain; the clipped length
              is recorded as a measured difference (W5)
  canonical   vertex counts = distinct vertices per level: the set of edge
              crossings, independent of how a tool splits or closes its lines
              (duplicates at split points or a repeated closing vertex are
              formatting, not work)
  levels      tools that do not report a line's level get it from the field
  joining     W2: an open line may only end on the domain boundary (raster edge
              or a NoData void); every other end is an unjoined end: the tool
              left two pieces of one contour unconnected (less assembly work).
              Exact and independent of saddle conventions.
  accuracy    W3: every vertex must lie on a cell edge, at the position where
              the field interpolated along that edge equals the line's level;
              the error is the distance along the edge, in cells

compare_lines: leave-one-out consensus (R4/W1/W2). For each tool the reference
is the median over the OTHER independent implementation families; isosweep is
never part of a reference. Totals and every level are checked: vertices
(W1), length (W1), line counts (W2, direction-labelled), accuracy (W3).
"""
import math
import statistics

import numpy as np

TIE_SNAP = 1e-3   # cells: a vertex this close to a grid node counts as at the node (tie conventions)

FRAMES = {"xy": (False, 0.0), "xy+0.5": (False, 0.5), "yx": (True, 0.0), "yx+0.5": (True, 0.5)}


def normalize(lines, frame):
    swap, off = FRAMES[frame]
    out = []
    for lev, a in lines:
        a = np.asarray(a, dtype=np.float64)
        if a.ndim != 2 or len(a) == 0:
            continue
        if swap:
            a = a[:, ::-1]
        if off:
            a = a - off
        out.append((lev, a))
    return out


def _clip_segment(p, q, x1, y1):
    """Liang-Barsky against [0,x1] x [0,y1]: (t0, t1) or None."""
    t0, t1 = 0.0, 1.0
    d = q - p
    for pv, dv, lo, hi in ((p[0], d[0], 0.0, x1), (p[1], d[1], 0.0, y1)):
        if dv == 0:
            if pv < lo or pv > hi:
                return None
            continue
        a, b = (lo - pv) / dv, (hi - pv) / dv
        if a > b:
            a, b = b, a
        t0, t1 = max(t0, a), min(t1, b)
        if t0 > t1:
            return None
    return t0, t1


def clip(lines, nx, ny):
    """Clip polylines to the node domain. → (lines, clipped_length).
    Vertices inside the domain are kept bit-exact (W3 needs exact edge
    coordinates); only crossing points of the domain boundary are new."""
    x1, y1 = nx - 1.0, ny - 1.0
    out, cut = [], 0.0
    for lev, a in lines:
        if a[:, 0].min() >= 0 and a[:, 1].min() >= 0 and a[:, 0].max() <= x1 and a[:, 1].max() <= y1:
            out.append((lev, a))
            continue
        cur = []

        def flush():
            if len(cur) >= 2:
                out.append((lev, np.array(cur)))
            cur.clear()
        for i in range(len(a) - 1):
            p, q = a[i], a[i + 1]
            seg = float(np.hypot(*(q - p)))
            c = _clip_segment(p, q, x1, y1)
            if c is None:
                cut += seg
                flush()
                continue
            t0, t1 = c
            cut += seg * (1.0 - (t1 - t0))
            P = p if t0 == 0.0 else p + t0 * (q - p)
            Q = q if t1 == 1.0 else p + t1 * (q - p)
            if not cur:
                cur.append(P)
            cur.append(Q)
            if t1 < 1.0:
                flush()
        flush()
    return out, cut


def canonical_count(a):
    """Vertices without consecutive duplicates or a repeated closing vertex."""
    if len(a) == 0:
        return 0
    keep = np.ones(len(a), bool)
    keep[1:] = np.any(a[1:] != a[:-1], axis=1)
    b = a[keep]
    if len(b) > 1 and np.array_equal(b[0], b[-1]):
        return len(b) - 1
    return len(b)


def _edge_error(z, x, y, level):
    """Distance (cells) along its cell edge from each vertex to the exact
    crossing of `level`; NaN for a vertex that is not on a cell edge."""
    ny, nx = z.shape
    xi = np.abs(x - np.round(x)) < 1e-6
    yi = np.abs(y - np.round(y)) < 1e-6
    err = np.full(len(x), np.nan)
    h = yi & ~xi                                   # on a row: between columns
    r = np.clip(np.round(y[h]).astype(int), 0, ny - 1)
    c = np.clip(np.floor(x[h]).astype(int), 0, nx - 2)
    a, b = z[r, c], z[r, c + 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        err[h] = np.abs((x[h] - c) - (level - a) / (b - a))
    w = xi & ~yi                                   # on a column: between rows
    c2 = np.clip(np.round(x[w]).astype(int), 0, nx - 1)
    r2 = np.clip(np.floor(y[w]).astype(int), 0, ny - 2)
    a2, b2 = z[r2, c2], z[r2 + 1, c2]
    with np.errstate(divide="ignore", invalid="ignore"):
        err[w] = np.abs((y[w] - r2) - (level - a2) / (b2 - a2))
    node = xi & yi        # on a node: an exact tie, or a crossing rounded onto the
    rr = np.clip(np.round(y[node]).astype(int), 0, ny - 1)       # node (float32 output):
    cc = np.clip(np.round(x[node]).astype(int), 0, nx - 1)       # distance to the nearest
    best = np.where(z[rr, cc] == level, 0.0, np.inf)            # crossing on an incident edge
    for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        r2, c2 = rr + dr, cc + dc
        ok = (r2 >= 0) & (r2 < ny) & (c2 >= 0) & (c2 < nx)
        a = z[rr[ok], cc[ok]]
        b = z[r2[ok], c2[ok]]
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (level - a) / (b - a)
        t = np.where((t >= 0) & (t <= 1), t, np.inf)
        cur = best[ok]
        best[ok] = np.minimum(cur, t)
    err[node] = np.where(np.isfinite(best), best, np.nan)
    # exact ties: a node equal to the level may be reported at the node or nudged off it
    # by a tool's tie rule (GDAL ~5e-7 cells, isosweep within float32 rounding) — no error
    near = (np.abs(x - np.round(x)) < TIE_SNAP) & (np.abs(y - np.round(y)) < TIE_SNAP)
    if near.any():
        rr = np.clip(np.round(y[near]).astype(int), 0, ny - 1)
        cc = np.clip(np.round(x[near]).astype(int), 0, nx - 1)
        e = err[near]
        e[z[rr, cc] == level] = 0.0
        err[near] = e
    return err


VALUE_EXACT = 1e-6   # x field span: a vertex this close in VALUE is exact (near-ties on flat edges)


def vertex_error(z, x, y, level, span):
    """W3 per vertex: distance (cells) along its edge to the exact crossing, or 0 when
    the vertex is exact in value (|z(vertex) - level| <= VALUE_EXACT * span) — a tool's
    near-tie rule may move a vertex along a nearly flat edge without changing its
    value. → (err, n_value_exact_only)."""
    err = _edge_error(z, x, y, level)
    v = _level_at(z, x, y)
    vexact = np.isfinite(v) & (np.abs(v - level) <= VALUE_EXACT * span) & np.isfinite(err) & (err > 1e-5)
    err[vexact] = 0.0
    return err, int(vexact.sum())


def _level_at(z, x, y):
    """Field value at on-edge vertices (for tools that report no level)."""
    ny, nx = z.shape
    xi = np.abs(x - np.round(x)) < 1e-6
    yi = np.abs(y - np.round(y)) < 1e-6
    v = np.full(len(x), np.nan)
    h = yi & ~xi
    r = np.clip(np.round(y[h]).astype(int), 0, ny - 1)
    c = np.clip(np.floor(x[h]).astype(int), 0, nx - 2)
    v[h] = z[r, c] + (x[h] - c) * (z[r, c + 1] - z[r, c])
    w = xi & ~yi
    c2 = np.clip(np.round(x[w]).astype(int), 0, nx - 1)
    r2 = np.clip(np.floor(y[w]).astype(int), 0, ny - 2)
    v[w] = z[r2, c2] + (y[w] - r2) * (z[r2 + 1, c2] - z[r2, c2])
    nd = xi & yi                                   # a vertex on a node (exact tie)
    v[nd] = z[np.clip(np.round(y[nd]).astype(int), 0, ny - 1), np.clip(np.round(x[nd]).astype(int), 0, nx - 1)]
    return v


def _unjoined_ends(lines, z):
    """Open-line endpoints that are neither on the raster edge nor next to a
    NoData node (W2)."""
    ny, nx = z.shape
    nd = ~np.isfinite(z) | (np.abs(z) >= 1e19)
    ends = []
    for _, a in lines:
        if len(a) < 2 or np.allclose(a[0], a[-1], rtol=0.0, atol=1e-7):
            continue
        ends.append(a[0])
        ends.append(a[-1])
    if not ends:
        return 0
    e = np.asarray(ends)
    x, y = e[:, 0], e[:, 1]
    eps = 1e-6
    edge = (x <= eps) | (y <= eps) | (x >= nx - 1 - eps) | (y >= ny - 1 - eps)
    void = np.zeros(len(e), bool)
    if nd.any():
        for dr in (-1, 0, 1, 2):
            for dc in (-1, 0, 1, 2):
                r = np.clip(np.floor(y).astype(int) + dr, 0, ny - 1)
                c = np.clip(np.floor(x).astype(int) + dc, 0, nx - 1)
                void |= nd[r, c]
    return int((~(edge | void)).sum())


def snap_ties(a):
    """Vertices within TIE_SNAP of a grid node → the node: a contour through a node
    equal to its level is one crossing whatever a tool's tie rule (at the node,
    or nudged onto each incident edge)."""
    r = np.round(a)
    near = np.all(np.abs(a - r) < TIE_SNAP, axis=1)
    out = a.copy()
    out[near] = r[near]
    return out


def summarize_lines(lines, frame, z, levels, max_check=400000):
    """Raw tool output → summary for the like-for-like checks."""
    ny, nx = z.shape
    lines = normalize(lines, frame)
    lines, cut = clip(lines, nx, ny)
    unjoined = _unjoined_ends(lines, z)
    lev = np.asarray(levels, dtype=np.float64)
    li_list = []
    for L, a in lines:
        if L is not None:
            li = int(np.argmin(np.abs(lev - float(L))))
        else:
            v = _level_at(z, a[:, 0], a[:, 1])
            v = v[np.isfinite(v)]
            li = int(np.argmin(np.abs(lev - float(np.median(v))))) if len(v) else -1
        li_list.append(li)
    per, verts = {}, {}
    n_feat = 0
    total = 0.0
    errs, offedge, checked, vexact = [], 0, 0, 0
    span = float(z.max() - z.min()) or 1.0
    stride = max(1, sum(len(a) for _, a in lines) // max_check)
    k = 0
    for (L, a), li in zip(lines, li_list):
        if len(a) < 2:
            continue
        L_ = float(np.sqrt((np.diff(a, axis=0) ** 2).sum(axis=1)).sum())
        n_feat += 1
        total += L_
        key = str(li)
        e = per.setdefault(key, {"count": 0, "points": 0, "length": 0.0})
        e["count"] += 1
        e["length"] += L_
        verts.setdefault(key, []).append(a)
        sel = a[(k + np.arange(len(a))) % stride == 0]        # W3 on a deterministic sample
        k += len(a)
        if len(sel) and li >= 0:
            err, nv = vertex_error(z, sel[:, 0], sel[:, 1], lev[li], span)
            vexact += nv
            ok = np.isfinite(err)
            offedge += int((~ok).sum())
            checked += int(ok.sum())
            if ok.any():
                errs.append(float(np.max(err[ok])))
    n_pts = 0
    for key, arrs in verts.items():             # distinct vertices per level (ties snapped to the node)
        u = len(np.unique(snap_ties(np.vstack(arrs)), axis=0))
        per[key]["points"] = u
        n_pts += u
    return {"features": n_feat, "points": n_pts, "total_length": total,
            "mean_points_per_feature": (n_pts / n_feat) if n_feat else 0.0,
            "per_level": per, "clipped_length": cut, "unjoined_ends": unjoined,
            "accuracy": {"max_err_cells": max(errs) if errs else 0.0,
                         "checked_vertices": checked, "off_edge_vertices": offedge,
                         "value_exact_only": vexact}}


def saddle_budget(z, levels):
    """Per level: how much a tool's saddle convention can change the output.
    A saddle cell (diagonal corners on the same side of the level) has two
    valid pairings of its four crossings; tools resolve it differently
    (fixed rule vs cell-centre value). Returns {level index: {cells, length}}
    with length = sum over saddle cells of |len(pairing A) - len(pairing B)|:
    the most per-level length can differ by convention alone. Line counts can
    change by at most one per saddle cell. Vertices cannot change."""
    z = np.asarray(z, dtype=np.float64)
    a, b, c, d = z[:-1, :-1], z[:-1, 1:], z[1:, 1:], z[1:, :-1]   # corners: TL TR BR BL
    out = {}
    for i, L in enumerate(np.asarray(levels, dtype=np.float64)):
        A, B, C, D = a >= L, b >= L, c >= L, d >= L
        sad = (A == C) & (B == D) & (A != B)
        if not sad.any():
            out[str(i)] = {"cells": 0, "length": 0.0}
            continue
        aa, bb, cc, dd = a[sad], b[sad], c[sad], d[sad]
        with np.errstate(divide="ignore", invalid="ignore"):
            top = np.column_stack([(L - aa) / (bb - aa), np.zeros(len(aa))])        # (x, y) in cell
            right = np.column_stack([np.ones(len(aa)), (L - bb) / (cc - bb)])
            bottom = np.column_stack([(L - dd) / (cc - dd), np.ones(len(aa))])
            left = np.column_stack([np.zeros(len(aa)), (L - aa) / (dd - aa)])
        dist = lambda p, q: np.hypot(*(p - q).T)                    # noqa: E731
        p1 = dist(top, left) + dist(bottom, right)
        p2 = dist(top, right) + dist(bottom, left)
        out[str(i)] = {"cells": int(sad.sum()), "length": float(np.abs(p1 - p2).sum())}
    return out


def _cell_len(a, b, c, d, L, above):
    """Contour length inside each cell (corners TL, TR, BR, BL; unit square)
    when a node counts as above where above(z) holds; saddle cells take the
    longer pairing (the shorter one is the saddle budget's business)."""
    A, B, C, D = above(a), above(b), above(c), above(d)

    def cross(p, q, Ap, Aq, pa, pb):     # crossing on edge p->q at points pa..pb (unit coords)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.where(q != p, (L - p) / (q - p), 0.0)
        t = np.clip(np.nan_to_num(t), 0.0, 1.0)
        return np.column_stack([pa[0] + t * (pb[0] - pa[0]), pa[1] + t * (pb[1] - pa[1])]), Ap != Aq
    top, ht = cross(a, b, A, B, (0, 0), (1, 0))
    right, hr = cross(b, c, B, C, (1, 0), (1, 1))
    bottom, hb = cross(d, c, D, C, (0, 1), (1, 1))
    left, hl = cross(a, d, A, D, (0, 0), (0, 1))
    pts = [top, right, bottom, left]
    has = np.column_stack([ht, hr, hb, hl])
    dist = lambda p, q: np.hypot(*(p - q).T)                    # noqa: E731
    n = has.sum(axis=1)
    out = np.zeros(len(a))
    two = n == 2
    if two.any():                        # the one segment between the two crossed edges
        idx = np.argsort(~has[two], axis=1, kind="stable")[:, :2]
        p = np.stack(pts, axis=1)[two]
        out[two] = dist(p[np.arange(len(p)), idx[:, 0]], p[np.arange(len(p)), idx[:, 1]])
    four = n == 4
    if four.any():
        t_, r_, b_, l_ = (x[four] for x in pts)
        out[four] = np.maximum(dist(t_, l_) + dist(b_, r_), dist(t_, r_) + dist(b_, l_))
    return out


def tie_budget(z, levels):
    """Per level: how much a tool's tie convention can change the output. A
    node exactly equal to the level is 'above' for some tools (z >= level)
    and 'below' for others (z > level); the contour then passes on the other
    side of it. Returns {level index: {nodes, length}}: nodes = tied nodes
    (each moves at most the one vertex at the node, and can join or split a
    line there); length = sum over cells touching a tied node of the
    difference between the contour length in the cell under the two
    conventions — exactly what the convention alone can change."""
    z = np.asarray(z, dtype=np.float64)
    a, b, c, d = z[:-1, :-1], z[:-1, 1:], z[1:, 1:], z[1:, :-1]
    out = {}
    for i, L in enumerate(np.asarray(levels, dtype=np.float64)):
        tied = int(np.count_nonzero(z == L))
        if not tied:
            out[str(i)] = {"nodes": 0, "length": 0.0}
            continue
        m = (a == L) | (b == L) | (c == L) | (d == L)
        aa, bb, cc, dd = a[m], b[m], c[m], d[m]
        ge = _cell_len(aa, bb, cc, dd, L, lambda v: v >= L)
        gt = _cell_len(aa, bb, cc, dd, L, lambda v: v > L)
        out[str(i)] = {"nodes": tied, "length": float(np.abs(ge - gt).sum())}
    return out


def mesh_summary(n_triangles, n_vertices):
    return {"triangles": int(n_triangles), "vertices": int(n_vertices)}


def _rel(v, ref):
    return abs(v - ref) / ref if ref else (0.0 if v == ref else math.inf)


def compare_lines(summaries, families, waivers, tol, ours="isosweep", precision=None, budget=None, ties=None):
    """summaries/families/waivers keyed by config_id; budget = saddle_budget()
    and ties = tie_budget() of the benchmark (differences a saddle or tie
    convention alone can explain are allowed on top of the tolerances;
    anything beyond is real). → findings."""
    precision = precision or {}
    budget = budget or {}
    ties = ties or {}
    b_len = sum(v["length"] for v in budget.values()) + sum(v["length"] for v in ties.values())
    t_nodes = sum(v["nodes"] for v in ties.values())
    b_cnt = sum(v["cells"] for v in budget.values()) + t_nodes
    fams = sorted({families[c] for c in summaries if families[c] != ours})
    if len(fams) < 2:
        return [{"check": "consensus", "ok": False, "waived": False,
                 "detail": f"need >= 2 independent families, have {fams}"}]

    def fam_metric(fam, fn):
        vals = [fn(summaries[c]) for c in summaries if families[c] == fam]
        return statistics.median(vals)

    keys = set()
    for s in summaries.values():
        keys |= set(s["per_level"])
    findings = []
    for cid, s in summaries.items():
        own = families[cid]
        refs = [f for f in fams if f != own]
        w = waivers.get(cid, {})

        def ref(fn):
            return statistics.median(fam_metric(f, fn) for f in refs)

        def add(check, ok, detail, direction=None):
            if not ok and check in w:
                mx = w[check].get("max_rel")
                within = mx is not None and detail.get("rel", math.inf) <= mx
                if w[check]["direction"] == "favours_isosweep" or (
                        direction and w[check]["direction"] != direction):
                    within = False         # a waiver may never cover a difference that favours us
                findings.append({"tool": cid, "check": check, "ok": within, "waived": True,
                                 "waiver_direction": w[check]["direction"], "detail": detail})
            else:
                findings.append({"tool": cid, "check": check, "ok": ok, "waived": False, "detail": detail})

        rl = ref(lambda x: x["total_length"])
        rp = ref(lambda x: x["points"])
        # line-count reference: only fully joined outputs (a split line is not a convention)
        joined = {c for c in summaries if summaries[c].get("unjoined_ends", 0) == 0}
        rf_refs = [f for f in refs if any(families[c] == f for c in joined)]
        rf = statistics.median(statistics.median(summaries[c]["features"] for c in joined if families[c] == f)
                               for f in rf_refs) if rf_refs else ref(lambda x: x["features"])
        add("W1_total_length", abs(s["total_length"] - rl) <= tol["length_rel_tol"] * rl + b_len,
            {"rel": _rel(s["total_length"], rl), "value": s["total_length"], "reference": rl,
             "saddle_budget": b_len, "refs": refs})
        add("W1_total_points", abs(s["points"] - rp) <= tol["points_rel_tol"] * rp + t_nodes,
            {"rel": _rel(s["points"], rp), "value": s["points"], "reference": rp, "tie_nodes": t_nodes})
        # W2: unjoined ends = less joining work (favours that tool); exact
        uj = s.get("unjoined_ends", 0)
        add("W2_unjoined_ends", uj == 0,
            {"rel": uj / 2.0 / rf if rf else math.inf, "value": uj, "reference_features": rf},
            direction="favours_competitor" if uj else None)
        # line count: saddle conventions alone move it by <= 1 per saddle cell
        relf = _rel(s["features"], rf)
        add("W2_feature_count", abs(s["features"] - rf) <= tol["feature_count_rel_tol"] * rf + b_cnt,
            {"rel": relf, "value": s["features"], "reference": rf, "saddle_cells": b_cnt},
            direction="favours_competitor" if s["features"] > rf else None)
        worst_l = worst_p = (0.0, None)
        for key in sorted(keys, key=lambda k: int(k)):
            v = s["per_level"].get(key, {"length": 0.0, "points": 0})
            r_len = ref(lambda x: x["per_level"].get(key, {"length": 0.0})["length"])
            r_pts = ref(lambda x: x["per_level"].get(key, {"points": 0})["points"])
            dl = abs(v["length"] - r_len)
            dp = abs(v["points"] - r_pts)
            tb = ties.get(key, {"nodes": 0, "length": 0.0})
            excl = dl - max(tol["level_length_rel_tol"] * r_len, tol["level_length_abs"]) \
                - budget.get(key, {"length": 0.0})["length"] - tb["length"]
            excp = dp - max(tol["level_points_rel_tol"] * r_pts, tol["level_points_abs"]) - tb["nodes"]
            if excl > worst_l[0] or worst_l[1] is None:
                worst_l = (excl, {"level": key, "value": v["length"], "reference": r_len, "rel": _rel(v["length"], r_len)})
            if excp > worst_p[0] or worst_p[1] is None:
                worst_p = (excp, {"level": key, "value": v["points"], "reference": r_pts, "rel": _rel(v["points"], r_pts)})
        if worst_l[1] is not None:
            add("W1_level_length", worst_l[0] <= 0, worst_l[1])
            add("W1_level_points", worst_p[0] <= 0, worst_p[1])
        acc = s["accuracy"]
        lim = tol["accuracy_cells"].get(precision.get(cid, "float64"))
        add("W3_accuracy", acc["off_edge_vertices"] == 0 and acc["max_err_cells"] <= lim,
            dict(acc, limit=lim, precision=precision.get(cid, "float64")))
    return findings


def compare_surface3d(summaries, waivers, tol):
    findings = []
    unwaived = [t for t in summaries if "triangle_count" not in waivers.get(t, {})]
    cons = statistics.median(summaries[t]["triangles"] for t in unwaived)
    for t, s in summaries.items():
        rel = abs(s["triangles"] - cons) / cons if cons else math.inf
        ok = rel <= tol["triangle_count_rel_tol"]
        w = waivers.get(t, {}).get("triangle_count")
        if not ok and w:
            findings.append({"tool": t, "check": "triangle_count", "ok": rel <= w.get("max_rel", 0),
                             "waived": True, "waiver_direction": w["direction"],
                             "detail": {"rel": rel, "value": s["triangles"], "consensus": cons}})
        else:
            findings.append({"tool": t, "check": "triangle_count", "ok": ok, "waived": False,
                             "detail": {"rel": rel, "value": s["triangles"], "consensus": cons}})
    return findings
