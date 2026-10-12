"""
Class assignment from benchmark results (implements grid_dynamics.txt).

Pipeline
 1. Lap scoring   : drop lap 1 (standing start), score a car on a track by the MEDIAN of its
                    timed laps (not the single fastest lap), which filters drafting spikes,
                    curb errors and AI stutters.
 2. Normalisation : per track, express every car as a % delta to the fastest car on that track;
                    a car's rating is the MEAN delta over all test tracks.
 3. Class borders : natural breaks, not fixed time steps.
      a. Gap analysis  : a hole in the performance spectrum of at least 30% of a class width
                         (and 6x the typical gap) is a class border, unless that would leave a
                         stretch with fewer than min_size cars that can be merged into a neighbour.
      b. Jenks         : inside each stretch between holes, exact Fisher-Jenks optimal breaks
                         (minimum within-class variance), with the FEWEST classes that keep every
                         class within the max spread (grid parity) and at least min_size cars.
      c. Nudging       : each Jenks cut may slide to the largest gap in its feasible range, so
                         near-identical cars are not split when a cleaner gap is available.
    Work is done on ln(1 + delta/100) so a "2% class" means 2% of lap time everywhere.
 4. Quality flags : borderline cars (inside the tie zone of a class border, or inside their own
                    lap-to-lap noise), high-variance cars (delta differs between tracks by more
                    than the class spread: keep the class, balance with ballast/restrictor) and
                    the lap-time slowdown that BoP would have to deliver on the track where the
                    car beats its class.
Pure standard library.
"""
import bisect, math, statistics

from ac_car_classifier import class_label

MIN_TIMED = 3
INF = float("inf")
EPS = 1e-12


# ---------------------------------------------------------------- lap scoring
def timed_laps(entry):
    """Valid timed laps in ms: every recorded lap except lap 1."""
    laps = entry.get("laps_ms") or []
    return [t for t in laps[1:] if isinstance(t, (int, float)) and t > 0]


def lap_score(entry):
    """-> (median timed lap in ms, number of timed laps). Old entries without lap lists fall back to best_ms."""
    timed = timed_laps(entry)
    if timed:
        return float(statistics.median(timed)), len(timed)
    if entry.get("best_ms"):
        return float(entry["best_ms"]), 0
    return None, 0


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


def fixed_partition(y, k, m):
    """Plain Jenks into exactly k classes (no width limit)."""
    n = len(y)
    k = max(1, min(k, n))
    m = max(1, min(m, n // k))
    D, P = _dp_tables(y, None, m, k)
    if D[k][n - 1] < INF:
        return _backtrack(P, n, k)
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
def rate(results, tracks, class_gap=2.0, min_size=3, fixed_k=0, tie_zone=0.10, sensitivity=None,
         masses=None, allow_partial=False, current_only=False, profile=None, nudge=True):
    """results: {car: {track: entry}} (lap_results.json). -> (cars, summary).
    cars[id]: cls, idx, score (mean delta %), gap (% behind the fastest rated car), times (median s per
    track), deltas, variance, n_timed, noise (std. error of the median, % of lap time), flags (list),
    flags_text, bop.
    summary: classes [{label, n, lo, hi, width}], boundaries [{between, gap, kind}], warnings."""
    tracks = [t for t in tracks if t]
    masses = masses or {}

    def usable(e):
        return e.get("status") == "ok" and (not current_only or e.get("profile") == profile)

    scores, best = {}, {}
    for car, per in results.items():
        for t in tracks:
            e = per.get(t) or {}
            if not usable(e):
                continue
            ms, n = lap_score(e)
            if ms is None:
                continue
            timed = timed_laps(e)
            # standard error of the median score, in % of lap time: how far the score could move on a re-run
            spread = (1.2533 * statistics.stdev(timed) / math.sqrt(len(timed)) / ms * 100) if len(timed) > 1 else 0.0
            scores[(car, t)] = (ms, n, spread)
            if t not in best or ms < best[t]:
                best[t] = ms

    cars = {}
    for car in results:
        have = [t for t in tracks if (car, t) in scores]
        if not have or (len(have) < len(tracks) and not allow_partial):
            continue
        deltas = [(scores[(car, t)][0] / best[t] - 1) * 100 if (car, t) in scores else None for t in tracks]
        valid = [d for d in deltas if d is not None]
        cars[car] = {
            "id": car, "deltas": deltas, "score": sum(valid) / len(valid),
            "times": [scores[(car, t)][0] / 1000 if (car, t) in scores else None for t in tracks],
            "n_timed": min(scores[(car, t)][1] for t in have),
            "noise": max(scores[(car, t)][2] for t in have),
            "variance": 0.0, "flags": [], "flags_text": "", "bop": "", "cls": None, "idx": None, "gap": 0.0,
        }
    summary = {"classes": [], "boundaries": [], "warnings": [], "k": 0}
    order = sorted(cars.values(), key=lambda c: (c["score"], c["id"]))
    n = len(order)
    if n == 0:
        return cars, summary

    if not fixed_k and (not class_gap or class_gap <= 0):
        class_gap = 2.0
    y = [math.log1p(c["score"] / 100) for c in order]
    m_req = max(1, int(min_size))

    # --- class borders
    g_sig = None
    if fixed_k and fixed_k > 0:
        classes = fixed_partition(y, int(fixed_k), m_req)
        width = None
    else:
        width = math.log1p(class_gap / 100)
        classes, g_sig = natural_classes(y, width, m_req)
    if nudge and len(classes) > 1:
        classes = nudge_cuts(y, classes, width, m_req)
    summary["k"] = len(classes)

    labels = [class_label(i + 1) for i in range(len(classes))]
    for idx, (a, b) in enumerate(classes):
        for i in range(a, b + 1):
            order[i]["cls"], order[i]["idx"] = labels[idx], idx
        small = (b - a + 1) < m_req
        summary["classes"].append({
            "label": labels[idx], "n": b - a + 1, "lo": order[a]["score"], "hi": order[b]["score"],
            "width": (math.exp(y[b] - y[a]) - 1) * 100, "small": small})
        if small:
            summary["warnings"].append(f"class {labels[idx]} has only {b - a + 1} car(s) (min {m_req}): "
                                       "cars on both sides are too far apart to merge within the max spread")
        if width is not None and y[b] - y[a] > width + 1e-9:
            summary["warnings"].append(f"class {labels[idx]} is wider than the max spread")
    for c in order:
        c["gap"] = (math.exp(math.log1p(c["score"] / 100) - y[0]) - 1) * 100

    # --- boundaries and borderline cars
    mids = []
    for t in range(len(classes) - 1):
        c = classes[t][1]
        gap_y = y[c + 1] - y[c]
        kind = "natural gap" if (g_sig is not None and gap_y >= g_sig - EPS) else "cut"
        summary["boundaries"].append({"between": labels[t] + "/" + labels[t + 1],
                                      "gap": (math.exp(gap_y) - 1) * 100, "kind": kind})
        mids.append((y[c] + y[c + 1]) / 2)
    if mids:
        for i, car in enumerate(order):
            pos = bisect.bisect(mids, y[i])
            near = [p for p in (pos - 1, pos) if 0 <= p < len(mids)]
            p = min(near, key=lambda q: abs(y[i] - mids[q]))
            dist = (math.exp(abs(y[i] - mids[p])) - 1) * 100
            if dist < max(tie_zone, car["noise"]):
                car["flags"].append("borderline " + summary["boundaries"][p]["between"] + f" ({dist:.2f}%)")

    # --- variance and BoP hints
    n_tracks = len(tracks)
    for idx, (a, b) in enumerate(classes):
        members = order[a:b + 1]
        norms = []
        for ti in range(n_tracks):
            vals = [c["deltas"][ti] for c in members if c["deltas"][ti] is not None]
            norms.append(statistics.median(vals) if vals else None)
        for car in members:
            valid = [d for d in car["deltas"] if d is not None]
            car["variance"] = (max(valid) - min(valid)) if len(valid) > 1 else 0.0
            if len(valid) > 1 and class_gap and car["variance"] > class_gap:
                car["flags"].append(f"high variance ({car['variance']:.1f}%)")
                bits = []
                for ti, t in enumerate(tracks):
                    d = car["deltas"][ti]
                    if d is None or norms[ti] is None:
                        continue
                    excess = norms[ti] - d            # > 0: quicker than its class on this track
                    if excess > 0.25 * class_gap:
                        bits.append(_bop_text(t, excess, car["id"], sensitivity, masses))
                car["bop"] = "; ".join(bits)
    for car in order:
        car["flags_text"] = "; ".join(car["flags"])
        if car["n_timed"] and car["n_timed"] < MIN_TIMED:
            summary["warnings"].append(f"{car['id']}: only {car['n_timed']} timed lap(s), median is weak")
    return cars, summary


def _bop_text(track, excess, car_id, sensitivity, masses):
    name = track.partition("/")[0]
    text = f"{name}: needs to be {excess:.2f}% slower"
    if sensitivity and sensitivity > 0:
        cut = excess / sensitivity                 # % power-to-weight change for that lap-time change
        text += f" (~{cut:.1f}% restrictor"
        if masses.get(car_id):
            text += f" or ~{masses[car_id] * cut / 100:.0f} kg ballast"
        text += ")"
    return text
