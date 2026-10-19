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
import math, statistics

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


def _dp_tables(y, w, m, kmax):
    """D[k][j] = least within-class sum of squares covering y[0..j] with k classes, each class
    holding at least m values and (if w is not None) spanning at most w."""
    n = len(y)
    p1, p2 = [0.0] * (n + 1), [0.0] * (n + 1)
    for i, v in enumerate(y):
        p1[i + 1] = p1[i] + v
        p2[i + 1] = p2[i] + v * v
    lo = [0] * n
    if w is not None:
        i = 0
        for j in range(n):
            while y[j] - y[i] > w + EPS:
                i += 1
            lo[j] = i
    D = [[INF] * n for _ in range(kmax + 1)]
    P = [[0] * n for _ in range(kmax + 1)]
    for j in range(m - 1, n):
        if lo[j] == 0:
            s1 = p1[j + 1]
            D[1][j] = p2[j + 1] - s1 * s1 / (j + 1)
    for k in range(2, kmax + 1):
        prev, cur, pk = D[k - 1], D[k], P[k]
        for j in range(k * m - 1, n):
            i_lo = max(lo[j], (k - 1) * m)
            best, bi = INF, -1
            pj2 = p2[j + 1]
            pj1 = p1[j + 1]
            for i in range(i_lo, j - m + 2):
                pv = prev[i - 1]
                if pv == INF:
                    continue
                s1 = pj1 - p1[i]
                v = pv + (pj2 - p2[i]) - s1 * s1 / (j - i + 1)
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


def _fewest_classes(y, w, m):
    """Smallest k whose optimal partition satisfies width w and min size m (None if impossible)."""
    n = len(y)
    if n < m:
        return None
    kg = _greedy_count(y, w)
    kmax = max(1, min(n // m, kg + max(3, kg // 2)))
    D, P = _dp_tables(y, w, m, kmax)
    for k in range(1, kmax + 1):
        if D[k][n - 1] < INF:
            return _backtrack(P, n, k)
    return None


def constrained_jenks(y, w, m):
    """Fewest-class Jenks partition of one stretch; relaxes the min size only if it must."""
    n = len(y)
    if n == 1:
        return [(0, 0)]
    for mm in range(max(1, min(m, n)), 0, -1):
        res = _fewest_classes(y, w, mm)
        if res:
            return res
    return [(i, i) for i in range(n)]


# ---------------------------------------------------------------- natural class borders
def natural_classes(y, w, m, sig_mult=6.0, sig_frac=0.3):
    """Gap analysis, then constrained Jenks inside each stretch. -> (classes, significant gap)."""
    n = len(y)
    gaps = [y[i + 1] - y[i] for i in range(n - 1)]
    med = statistics.median(gaps) if gaps else 0.0
    g_sig = max(sig_frac * w, sig_mult * med)
    segs, start = [], 0
    for i, g in enumerate(gaps):
        if g >= g_sig:
            segs.append([start, i])
            start = i + 1
    segs.append([start, n - 1])
    merged = True          # undersized stretches join a neighbour when the merged stretch can still be
                           # cut into classes that satisfy the max spread and the min size
    while merged:
        merged = False
        for si, (a, b) in enumerate(segs):
            if b - a + 1 >= m or len(segs) == 1:
                continue
            options = []
            if si > 0:
                options.append((y[a] - y[segs[si - 1][1]], si - 1, si))
            if si < len(segs) - 1:
                options.append((y[segs[si + 1][0]] - y[b], si, si + 1))
            for _, left, right in sorted(options):
                lo_i, hi_i = segs[left][0], segs[right][1]
                if _fewest_classes(y[lo_i:hi_i + 1], w, m) is not None:     # still splittable into valid classes
                    segs[left:right + 1] = [[lo_i, hi_i]]
                    merged = True
                    break
            if merged:
                break
    classes = []
    for a, b in segs:
        for s, e in constrained_jenks(y[a:b + 1], w, m):
            classes.append((a + s, a + e))
    return classes, g_sig


def nudge_cuts(y, classes, w, m, ratio=1.5, passes=4):
    """Slide each cut to the largest gap its two classes can still afford."""
    cls = [list(c) for c in classes]
    for _ in range(passes):
        moved = False
        for t in range(len(cls) - 1):
            a, c = cls[t]
            c1, b = cls[t + 1]
            cur = y[c + 1] - y[c]
            best_gap, best_c = cur, c
            m_left, m_right = min(m, c - a + 1), min(m, b - c1 + 1)   # never shrink a class below min size
            for cc in range(a + m_left - 1, b - m_right + 1):
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


# ---------------------------------------------------------------- main entry
def rate(results, tracks, class_gap=2.0, min_size=3):
    """results: {car: {track: score_ms}}. Cars that miss any of the tracks are left out.
    -> (cars, summary).
    cars[id]: id, deltas (% behind the fastest car, one per track), score (their mean), cls (class label).
    summary: classes [{label, n, lo, hi, width, small}], warnings."""
    tracks = [t for t in tracks if t]
    best = {t: min(per[t] for per in results.values() if t in per) for t in tracks
            if any(t in per for per in results.values())}
    cars = {}
    for car, per in results.items():
        if not all(t in per for t in tracks):
            continue
        deltas = [(per[t] / best[t] - 1) * 100 for t in tracks]
        cars[car] = {"id": car, "deltas": deltas, "score": sum(deltas) / len(deltas), "cls": None}
    summary = {"classes": [], "warnings": []}
    order = sorted(cars.values(), key=lambda c: (c["score"], c["id"]))
    if not order:
        return cars, summary

    if not class_gap or class_gap <= 0:
        class_gap = 2.0
    y = [math.log1p(c["score"] / 100) for c in order]
    m_req = max(1, int(min_size))
    width = math.log1p(class_gap / 100)
    classes, _ = natural_classes(y, width, m_req)
    if len(classes) > 1:
        classes = nudge_cuts(y, classes, width, m_req)

    labels = [class_label(i + 1) for i in range(len(classes))]
    for idx, (a, b) in enumerate(classes):
        for i in range(a, b + 1):
            order[i]["cls"] = labels[idx]
        small = (b - a + 1) < m_req
        summary["classes"].append({
            "label": labels[idx], "n": b - a + 1, "lo": order[a]["score"], "hi": order[b]["score"],
            "width": (math.exp(y[b] - y[a]) - 1) * 100, "small": small})
        if small:
            summary["warnings"].append(f"class {labels[idx]} has only {b - a + 1} car(s) (min {m_req}): "
                                       "cars on both sides are too far apart to merge within the max spread")
        if y[b] - y[a] > width + 1e-9:
            summary["warnings"].append(f"class {labels[idx]} is wider than the max spread")
    return cars, summary
