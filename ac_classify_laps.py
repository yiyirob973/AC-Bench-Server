#!/usr/bin/env python3
"""Turns lap_results.json (from ac_lap_bench.py) into car_classes.csv, which ac_grid_builder.py and ac_tag_writer.py
read. The class assignment itself is in ac_rating.py: median lap score (lap 1 dropped), mean % delta over the tracks,
natural class borders (gap analysis + Jenks) within a maximum spread, and flags for borderline and high-variance cars.

  python ac_classify_laps.py lap_results.json "<AC>/content/cars" --class-gap 2.0
  python ac_classify_laps.py lap_results.json "<AC>/content/cars" --classes 6        # exactly 6 classes
  python ac_classify_laps.py lap_results.json "<AC>/content/cars" --bop-sensitivity 0.15
"""
import argparse, csv, json
from pathlib import Path

from ac_car_classifier import read_ui, num, class_sort_key
from ac_baseline import PROFILE_ID
import ac_rating as rating


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("cars_dir")
    ap.add_argument("--class-gap", type=float, default=2.0,
                    help="max spread inside a class, %% of lap time (default 2.0)")
    ap.add_argument("--min-class-size", type=int, default=3, help="fewest cars a class should hold (default 3)")
    ap.add_argument("--classes", type=int, default=0,
                    help="force exactly this many classes (plain Jenks, no spread limit); 0 = automatic")
    ap.add_argument("--tie-zone", type=float, default=0.10,
                    help="cars closer than this %% of lap time to a class border are flagged borderline")
    ap.add_argument("--bop-sensitivity", type=float, default=None,
                    help="optional: %% lap time per %% power-to-weight, turns the needed slowdown into restrictor %% / ballast kg")
    ap.add_argument("--allow-partial", action="store_true", help="rate cars that only have a valid time on some tracks")
    ap.add_argument("--current-only", action="store_true",
                    help="rate only results made under the current baseline profile (ignore older ones)")
    ap.add_argument("--no-nudge", action="store_true", help="keep the raw Jenks cuts (do not slide them to larger gaps)")
    ap.add_argument("--out", default="car_classes.csv")
    args = ap.parse_args()

    data = json.loads(Path(args.results).read_text(encoding="utf-8"))
    tracks = sorted({t for v in data.values() for t in v})
    tracks = [t for t in tracks if any(v.get(t, {}).get("status") == "ok" for v in data.values())]

    ui = {car: read_ui(Path(args.cars_dir) / car) for car in data}
    specs = {car: (ui[car].get("specs") or {}) for car in data}
    masses = {car: num(specs[car].get("weight", "")) for car in data}
    cars, summ = rating.rate(
        data, tracks, class_gap=args.class_gap, min_size=args.min_class_size, fixed_k=args.classes,
        tie_zone=args.tie_zone, sensitivity=args.bop_sensitivity, masses=masses,
        allow_partial=args.allow_partial, current_only=args.current_only, profile=PROFILE_ID,
        nudge=not args.no_nudge)
    if not cars:
        raise SystemExit("No cars with complete results.")

    rows = []
    for car in sorted(cars.values(), key=lambda c: (c["score"], c["id"])):
        cid = car["id"]
        hp, mass = num(specs[cid].get("bhp", "")), masses[cid]
        row = {
            "class": car["cls"], "id": cid, "name": ui[cid].get("name", cid),
            "hp": str(round(hp)) if hp else "", "mass_kg": str(round(mass)) if mass else "",
            "gap_pct": round(car["gap"], 3), "score_pct": round(car["score"], 3),
            "variance_pct": round(car["variance"], 3), "timed_laps": car["n_timed"],
            "flags": car["flags_text"], "bop": car["bop"],
            "notes": "" if all(t is not None for t in car["times"]) else "partial: not all tracks",
        }
        for ti, t in enumerate(tracks):
            key = t.replace("/", "_")
            row["time_" + key] = "" if car["times"][ti] is None else round(car["times"][ti], 3)
            row["delta_" + key] = "" if car["deltas"][ti] is None else round(car["deltas"][ti], 3)
        rows.append(row)

    fields = (["class", "id", "name", "hp", "mass_kg", "gap_pct", "score_pct"]
              + ["time_" + t.replace("/", "_") for t in tracks]
              + ["delta_" + t.replace("/", "_") for t in tracks]
              + ["variance_pct", "timed_laps", "flags", "bop", "notes"])
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    skipped = [c for c in data if c not in cars]
    n_old = sum(1 for c in data for t in tracks
                if data[c].get(t, {}).get("status") == "ok" and data[c][t].get("profile") != PROFILE_ID)
    if n_old and not args.current_only:
        print(f"WARNING: {n_old} lap times were made under older settings (not {PROFILE_ID}).\n"
              "         Re-benchmark those cars, or use --current-only to rate only baseline results.")
    print(f"{len(cars)} cars rated on {len(tracks)} tracks in {summ['k']} classes, {len(skipped)} skipped -> {args.out}\n")
    for c in summ["classes"]:
        print(f"Class {c['label']}: {c['n']:3d} cars  rating {c['lo']:.2f}% to {c['hi']:.2f}%  "
              f"(spread {c['width']:.2f}%){'  SMALL' if c['small'] else ''}")
    if summ["boundaries"]:
        print("\nBorders (gap between the last car of one class and the first of the next):")
        for b in summ["boundaries"]:
            print(f"  {b['between']}: {b['gap']:.2f}%  {b['kind']}")
    n_border = sum(1 for c in cars.values() if any(f.startswith("borderline") for f in c["flags"]))
    n_var = sum(1 for c in cars.values() if any(f.startswith("high variance") for f in c["flags"]))
    print(f"\nFlags: {n_border} borderline, {n_var} high variance (see the flags / bop columns).")
    for w in summ["warnings"][:8]:
        print("WARNING:", w)
    if len(summ["warnings"]) > 8:
        print(f"... and {len(summ['warnings']) - 8} more warnings")


if __name__ == "__main__":
    main()
