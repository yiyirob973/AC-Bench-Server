#!/usr/bin/env python3
"""
Assetto Corsa grid builder. Reads car_classes.csv from ac_car_classifier.py
and picks a field of cars whose estimated lap times sit close together.

Examples:
  python ac_grid_builder.py car_classes.csv --list-classes
  python ac_grid_builder.py car_classes.csv --size 20 --max-gap 2.5
  python ac_grid_builder.py car_classes.csv --size 12 --class C --seed 7
  python ac_grid_builder.py car_classes.csv --size 16 --around ks_mazda_mx5_cup
  python ac_grid_builder.py car_classes.csv --size 10 --tightest --include "^ks_"
"""
import argparse, csv, random, re, sys
from collections import Counter

from ac_car_classifier import norm_class, class_sort_key


def load(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["gap_pct"] = float(r["gap_pct"])
                r["class"] = norm_class(r["class"])
            except (KeyError, ValueError):
                continue
            rows.append(r)
    if not rows:
        sys.exit("No usable rows in CSV.")
    return rows


def largest_window(rows, max_gap):
    """Window of width max_gap (in % lap time) that holds the most cars."""
    best = []
    for r in rows:
        lo = r["gap_pct"]
        pool = [x for x in rows if lo <= x["gap_pct"] <= lo + max_gap]
        if len(pool) > len(best):
            best = pool
    return best


def around_car(rows, car_id, max_gap):
    c = next((r for r in rows if r["id"] == car_id), None)
    if not c:
        sys.exit(f"Car '{car_id}' not found (after filters).")
    half = max_gap / 2
    return [r for r in rows if abs(r["gap_pct"] - c["gap_pct"]) <= half]


def tightest(rows, size):
    """Run of `size` neighbouring cars with the smallest lap-time spread."""
    if len(rows) < size:
        return rows
    i = min(range(len(rows) - size + 1),
            key=lambda k: rows[k + size - 1]["gap_pct"] - rows[k]["gap_pct"])
    return rows[i:i + size]


def sample(pool, size, max_per_car, rng):
    chosen = rng.sample(pool, min(size, len(pool)))
    extra = size - len(chosen)
    for _ in range(max_per_car - 1):
        if extra <= 0:
            break
        add = rng.sample(pool, min(extra, len(pool)))
        chosen += add
        extra -= len(add)
    return chosen, extra


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--size", type=int, default=20, help="number of cars on the grid")
    ap.add_argument("--max-gap", type=float, default=2.5,
                    help="max lap-time spread across the grid, in %% (default 2.5)")
    ap.add_argument("--class", dest="classes", type=str, action="append",
                    help="only use this class, e.g. A (repeatable; old numbers 1,2 still work)")
    ap.add_argument("--around", help="center the grid on this car id")
    ap.add_argument("--tightest", action="store_true",
                    help="take the tightest possible spread instead of random picks")
    ap.add_argument("--include", help="regex: only car ids matching")
    ap.add_argument("--exclude", help="regex: drop car ids matching")
    ap.add_argument("--max-per-car", type=int, default=1,
                    help="allow the same car multiple times (default 1)")
    ap.add_argument("--seed", type=int, help="seed for repeatable random picks")
    ap.add_argument("--list-classes", action="store_true")
    ap.add_argument("--out", default="grid.csv")
    args = ap.parse_args()

    rows = load(args.csv)

    if args.list_classes:
        counts = Counter(r["class"] for r in rows)
        for c in sorted(counts, key=class_sort_key):
            g = [r["gap_pct"] for r in rows if r["class"] == c]
            print(f"Class {c}: {counts[c]:3d} cars, gap {min(g):.1f}%..{max(g):.1f}%")
        return

    if args.classes:
        wanted = {norm_class(c) for c in args.classes}
        rows = [r for r in rows if r["class"] in wanted]
    if args.include:
        rows = [r for r in rows if re.search(args.include, r["id"])]
    if args.exclude:
        rows = [r for r in rows if not re.search(args.exclude, r["id"])]
    if not rows:
        sys.exit("No cars left after filters.")
    rows.sort(key=lambda r: r["gap_pct"])

    rng = random.Random(args.seed)

    if args.tightest:
        grid = tightest(rows, args.size)
        short = args.size - len(grid)
    else:
        pool = (around_car(rows, args.around, args.max_gap) if args.around
                else largest_window(rows, args.max_gap))
        grid, short = sample(pool, args.size, args.max_per_car, rng)
        print(f"Pool: {len(pool)} cars within {args.max_gap}% of lap time")

    grid.sort(key=lambda r: r["gap_pct"])
    spread = grid[-1]["gap_pct"] - grid[0]["gap_pct"]

    print(f"\nGrid: {len(grid)} cars, spread {spread:.2f}% of lap time\n")
    for i, r in enumerate(grid, 1):
        print(f"{i:2d}. {r['name'][:38]:38s} {r['hp']:>5s}hp  +{r['gap_pct']:.2f}%")
    if short > 0:
        print(f"\nOnly {len(grid)} of {args.size} cars available. "
              f"Widen --max-gap, relax filters, or raise --max-per-car.")

    fields = ["slot", "id", "name", "class", "gap_pct", "hp", "mass_kg", "est_time_s"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for i, r in enumerate(grid, 1):
            w.writerow({**r, "slot": i})
    print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
