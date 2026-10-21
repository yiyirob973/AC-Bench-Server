"""
Class assignment from benchmark results (implements grid_dynamics.txt).

How a class table is cut
 1. Normalisation : per track, express every car as a % delta to the fastest car on that track;
                    a car's rating is the mean delta over all the benchmark tracks.
 2. Class borders : natural breaks, not fixed time steps.
      a. Gap analysis  : a hole in the performance spectrum of at least 30% of a class width
                         (and 6x the typical gap) is a class border, unless that would leave a
                         stretch with fewer than min_size cars that can be merged into a neighbour.
      b. Jenks         : inside each stretch between holes, exact Fisher-Jenks optimal breaks
                         (minimum within-class variance), with the FEWEST classes that keep every
                         class within the max spread (grid parity) and at least min_size cars.
      c. Nudging       : each Jenks cut may slide to the largest gap in its feasible range, so
                         near-identical cars are not split when a cleaner gap is available.
    Work is done on ln(1 + delta/100) so a "2% class" means 2% of lap time everywhere.

The borderline / high-variance flags and the balance hints are not worked out here: the server derives
them per car from the published table (see classify in server.py). Pure standard library.
"""
import math

INF = float("inf")
EPS = 1e-12


def class_label(n):
    """Class number -> label. Two-letter codes (AA, AB, ...) made a search for class "A" match other classes,
    so labels are <group><letter>: 1 -> 1A (top class), 2 -> 1B, ... 26 -> 1Z, 27 -> 2A, ... 52 -> 2Z, 53 -> 3A."""
    if n < 1:
        return ""
    return "%d%s" % ((n - 1) // 26 + 1, chr(65 + (n - 1) % 26))


# ---------------------------------------------------------------- 1-D optimal breaks (Fisher-Jenks)
def _greedy_count(y, w):
    k, start = 1, y[0]
    for v in y:
        if v - start > w + EPS:
            k, start = k + 1, v
    return k


def _prefix(y, wt):
    """Running totals over weighted points: how many cars, sum of y, sum of y squared."""
    n = len(y)
    cw, p1, p2 = [0] * (n + 1), [0.0] * (n + 1), [0.0] * (n + 1)
    for i, v in enumerate(y):
        cw[i + 1] = cw[i] + wt[i]
        p1[i + 1] = p1[i] + wt[i] * v
        p2[i + 1] = p2[i] + wt[i] * v * v
    return cw, p1, p2


def _dp_tables(y, wt, w, m, kmax):
    """D[k][j] = least within-class sum of squares covering points 0..j with k classes, each class holding at
    least m cars (the weights count cars) and (if w is not None) spanning at most w."""
    n = len(y)
    cw, p1, p2 = _prefix(y, wt)
    lo = [0] * n
    if w is not None:
        i = 0
        for j in range(n):
            while y[j] - y[i] > w + EPS:
                i += 1
            lo[j] = i
    D = [[INF] * n for _ in range(kmax + 1)]
    P = [[0] * n for _ in range(kmax + 1)]
    for j in range(n):
        if lo[j] == 0 and cw[j + 1] >= m:
            s1 = p1[j + 1]
            D[1][j] = p2[j + 1] - s1 * s1 / cw[j + 1]
    hi = 0    # last start i whose class i..j still has m cars; only moves forward as j grows
    for k in range(2, kmax + 1):
        prev, cur, pk = D[k - 1], D[k], P[k]
        hi = 0
        for j in range(n):
            if cw[j + 1] < k * m:
                continue
            while hi + 1 <= j and cw[j + 1] - cw[hi + 1] >= m:
                hi += 1
            i_lo = lo[j]
            best, bi = INF, -1
            pj2, pj1, cj = p2[j + 1], p1[j + 1], cw[j + 1]
            for i in range(max(i_lo, 1), hi + 1):
                if cw[i] < (k - 1) * m:
                    continue
                pv = prev[i - 1]
                if pv == INF:
                    continue
                s1 = pj1 - p1[i]
                v = pv + (pj2 - p2[i]) - s1 * s1 / (cj - cw[i])
                if v < best:
                    best, bi = v, i
            cur[j], pk[j] = best, bi
    return D, P


def _backtrack(P, n, k):
    out, j = [], n - 1
    for c in range(k, 1, -1):
        i = P[c][j]
        out.append((i, j))
        j = i - 1
    out.append((0, j))
    out.reverse()
    return out


def _fewest_classes(y, wt, w, m):
    """Smallest k whose optimal partition satisfies width w and min size m (None if impossible)."""
    n, total = len(y), sum(wt)
    if total < m:
        return None
    kg = _greedy_count(y, w)
    kmax = max(1, min(total // m, kg + max(3, kg // 2)))
    D, P = _dp_tables(y, wt, w, m, kmax)
    for k in range(1, kmax + 1):
        if D[k][n - 1] < INF:
            return _backtrack(P, n, k)
    return None


def constrained_jenks(y, wt, w, m):
    """Fewest-class Jenks partition of one stretch; relaxes the min size only if it must."""
    n, total = len(y), sum(wt)
    if n == 1:
        return [(0, 0)]
    for mm in range(max(1, min(m, total)), 0, -1):
        res = _fewest_classes(y, wt, w, mm)
        if res:
            return res
    return [(i, i) for i in range(n)]


# ---------------------------------------------------------------- natural class borders
def _median_gap(y, wt):
    """Median distance between neighbouring cars. Cars that share a point are zero apart."""
    total = sum(wt)
    zeros = total - len(y)
    nz = sorted(y[i + 1] - y[i] for i in range(len(y) - 1))
    count = total - 1
    if count <= 0:
        return 0.0
    def at(idx):
        return 0.0 if idx < zeros else nz[idx - zeros]
    return at(count // 2) if count % 2 else (at(count // 2 - 1) + at(count // 2)) / 2


def natural_classes(y, wt, w, m, sig_mult=6.0, sig_frac=0.3):
    """Gap analysis, then constrained Jenks inside each stretch. y: sorted points, wt: cars at each point.
    -> (classes as (first point, last point) pairs, significant gap)."""
    n = len(y)
    gaps = [y[i + 1] - y[i] for i in range(n - 1)]
    med = _median_gap(y, wt)
    g_sig = max(sig_frac * w, sig_mult * med)
    segs, start = [], 0
    for i, g in enumerate(gaps):
        if g >= g_sig:
            segs.append([start, i])
            start = i + 1
    segs.append([start, n - 1])
    cw = _prefix(y, wt)[0]
    merged = True          # undersized stretches join a neighbour when the merged stretch can still be
                           # cut into classes that satisfy the max spread and the min size
    while merged:
        merged = False
        for si, (a, b) in enumerate(segs):
            if cw[b + 1] - cw[a] >= m or len(segs) == 1:
                continue
            options = []
            if si > 0:
                options.append((y[a] - y[segs[si - 1][1]], si - 1, si))
            if si < len(segs) - 1:
                options.append((y[segs[si + 1][0]] - y[b], si, si + 1))
            for _, left, right in sorted(options):
                lo_i, hi_i = segs[left][0], segs[right][1]
                if _fewest_classes(y[lo_i:hi_i + 1], wt[lo_i:hi_i + 1], w, m) is not None:     # still splittable into valid classes
                    segs[left:right + 1] = [[lo_i, hi_i]]
                    merged = True
                    break
            if merged:
                break
    classes = []
    for a, b in segs:
        for s, e in constrained_jenks(y[a:b + 1], wt[a:b + 1], w, m):
            classes.append((a + s, a + e))
    return classes, g_sig


def nudge_cuts(y, wt, classes, w, m, ratio=1.5, passes=4):
    """Slide each cut to the largest gap its two classes can still afford."""
    cw = _prefix(y, wt)[0]
    cls = [list(c) for c in classes]
    for _ in range(passes):
        moved = False
        for t in range(len(cls) - 1):
            a, c = cls[t]
            c1, b = cls[t + 1]
            cur = y[c + 1] - y[c]
            best_gap, best_c = cur, c
            m_left, m_right = min(m, cw[c + 1] - cw[a]), min(m, cw[b + 1] - cw[c1])   # never shrink a class below min size
            for cc in range(a, b):
                if cw[cc + 1] - cw[a] < m_left or cw[b + 1] - cw[cc + 1] < m_right:
                    continue
                if w is not None and (y[cc] - y[a] > w + EPS or y[b] - y[cc + 1] > w + EPS):
                    continue
                g = y[cc + 1] - y[cc]
                if g > best_gap + EPS or (abs(g - best_gap) <= EPS and abs(cc - c) < abs(best_c - c)):
                    best_gap, best_c = g, cc
            if best_c != c and best_gap > cur * ratio + EPS:
                cls[t], cls[t + 1] = [a, best_c], [best_c + 1, b]
                moved = True
        if not moved:
            break
    return [tuple(c) for c in cls]


def class_borders(y, wt, class_gap=2.0, min_size=3):
    """Class borders for cars grouped at sorted points y (wt[i] cars at y[i]; use one car per point for single cars).
    y is ln(1 + delta/100), so class_gap is a share of lap time. -> list of (first point, last point)."""
    if not class_gap or class_gap <= 0:
        class_gap = 2.0
    m_req = max(1, int(min_size))
    width = math.log1p(class_gap / 100)
    classes, _ = natural_classes(y, wt, width, m_req)
    if len(classes) > 1:
        classes = nudge_cuts(y, wt, classes, width, m_req)
    return classes


