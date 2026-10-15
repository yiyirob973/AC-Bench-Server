#!/usr/bin/env python3
"""
Assetto Corsa car classifier.

Scans content/cars/*, estimates a relative performance score from car data
(power, mass, tyre grip) and groups cars into classes by estimated lap-time gap.

Usage:
  python ac_car_classifier.py "C:/.../assettocorsa/content/cars"
  python ac_car_classifier.py CARS_DIR --class-gap 2.0 --ref-car ks_mazda_mx5_cup --ref-time 112.4
"""
import argparse, configparser, csv, json, math, re
from pathlib import Path

# Tuning knobs for the pace model: S = (hp/kg)^PW_EXP * grip^GRIP_EXP
PW_EXP = 0.40
GRIP_EXP = 0.80


def num(v):
    m = re.search(r"\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None


def read_ini(path):
    cp = configparser.ConfigParser(strict=False, interpolation=None,
                                   inline_comment_prefixes=(";", "//"))
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            cp.read_file(f)
    except Exception:
        pass
    return cp


def ini_num(cp, section, key):
    try:
        return num(cp.get(section, key))
    except Exception:
        return None


def read_ui(car_dir):
    p = car_dir / "ui" / "ui_car.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="ignore"), strict=False)
    except Exception:
        return {}


def peak_hp_from_lut(path):
    """power.lut is rpm|torque(Nm). Naturally-aspirated curve only (ignores turbo)."""
    best = 0.0
    try:
        for line in path.read_text(errors="ignore").splitlines():
            if "|" not in line:
                continue
            rpm, tq = (num(x) for x in line.split("|")[:2])
            if rpm and tq:
                best = max(best, rpm * tq * 2 * math.pi / 60 / 745.7)
    except Exception:
        pass
    return best or None


def analyze(car_dir):
    ui = read_ui(car_dir)
    specs = ui.get("specs", {}) if isinstance(ui.get("specs"), dict) else {}
    data = car_dir / "data"
    notes = []

    hp = num(specs.get("bhp", ""))          # prefer UI value: includes turbo
    mass = num(specs.get("weight", ""))
    grip = None

    if data.is_dir():
        car_ini = read_ini(data / "car.ini")
        mass = ini_num(car_ini, "BASIC", "TOTALMASS") or mass
        if not hp:
            hp = peak_hp_from_lut(data / "power.lut")
            if hp:
                notes.append("hp from power.lut (no turbo)")
        tyres = read_ini(data / "tyres.ini")
        vals = [v for v in (ini_num(tyres, "FRONT", "DY_REF"),
                            ini_num(tyres, "REAR", "DY_REF")) if v]
        if vals:
            grip = sum(vals) / len(vals)
    elif (car_dir / "data.acd").exists():
        notes.append("data.acd encrypted: UI specs only, grip unknown")
    else:
        notes.append("no data found")

    if not grip:
        grip = 1.0
        notes.append("grip defaulted")

    if not hp or not mass:
        return None, f"{car_dir.name}: missing hp/mass"

    score = (hp / mass) ** PW_EXP * grip ** GRIP_EXP
    return {
        "id": car_dir.name,
        "name": ui.get("name", car_dir.name),
        "hp": round(hp),
        "mass_kg": round(mass),
        "grip": round(grip, 3),
        "score": score,
        "notes": "; ".join(notes),
    }, None


def class_label(n):
    """1 -> A, 2 -> B, ... 26 -> Z, 27 -> AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def class_sort_key(label):
    """Alphabetical order that also handles AA after Z."""
    return (len(label), label)


def norm_class(v):
    """Class value from a CSV -> letter label. Old numeric CSVs (1, 2, 3) become A, B, C."""
    s = str(v).strip()
    return class_label(int(s)) if s.isdigit() else s.upper()


def assign_classes(rows, gap_pct):
    rows.sort(key=lambda r: r["score"], reverse=True)
    best = rows[0]["score"]
    cls, anchor = 1, 1.0
    for r in rows:
        t_rel = best / r["score"]            # 1.0 = fastest car
        r["gap_pct"] = round((t_rel - 1) * 100, 2)
        if t_rel / anchor - 1 > gap_pct / 100:
            cls += 1
            anchor = t_rel
        r["class"] = class_label(cls)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cars_dir", help="path to content/cars")
    ap.add_argument("--class-gap", type=float, default=2.0,
                    help="max lap-time spread inside a class, in %% (default 2.0)")
    ap.add_argument("--ref-car", help="car id with a known lap time on your track")
    ap.add_argument("--ref-time", type=float, help="that car's lap time in seconds")
    ap.add_argument("--out", default="car_classes.csv")
    args = ap.parse_args()

    rows, skipped = [], []
    for d in sorted(Path(args.cars_dir).iterdir()):
        if d.is_dir():
            row, err = analyze(d)
            rows.append(row) if row else skipped.append(err)
    if not rows:
        raise SystemExit("No cars could be analyzed.")

    assign_classes(rows, args.class_gap)

    if args.ref_car and args.ref_time:
        ref = next((r for r in rows if r["id"] == args.ref_car), None)
        if ref:
            for r in rows:
                r["est_time_s"] = round(args.ref_time * ref["score"] / r["score"], 2)
        else:
            print(f"ref car '{args.ref_car}' not found; skipping seconds")

    fields = ["class", "id", "name", "hp", "mass_kg", "grip", "gap_pct", "est_time_s", "notes"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"{len(rows)} cars analyzed, {len(skipped)} skipped -> {args.out}\n")
    for c in sorted({r["class"] for r in rows}, key=class_sort_key):
        grp = [r for r in rows if r["class"] == c]
        print(f"Class {c}: {len(grp):3d} cars, gap {grp[0]['gap_pct']:.1f}% to {grp[-1]['gap_pct']:.1f}%")
    if skipped:
        print("\nSkipped:", *skipped[:10], sep="\n  ")


if __name__ == "__main__":
    main()
