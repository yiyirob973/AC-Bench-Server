#!/usr/bin/env python3
"""Writes class tags into each car's ui/ui_car.json (they show up as tags in Content Manager).

    ac_tag_writer.py CARS_DIR [--prefix #Pride-] [--also-remove PREFIX ...] --untag [--apply]
    ac_tag_writer.py CARS_DIR --restore

Without --apply it's a dry run. Tags never pile up:
  * every tag that starts with the prefix (or an --also-remove prefix, e.g. tags from an older version) is removed
    first and then exactly one class tag is added, so applying twice, or after a car changed class, can't leave two
    class tags behind;
  * the whole tag list is de-duplicated (case-insensitive, first spelling and the order are kept);
  * a file is only rewritten when its tag list really changes, and it's written atomically;
  * the first backup (ui_car.json.gridbak) is never overwritten, so --restore always brings back the original file.
"""
import ac_paths
import argparse, json, os, re, sys, tempfile
from pathlib import Path

BOM = b"\xef\xbb\xbf"
LABEL = re.compile(r"[1-9][0-9]?[A-Z]")   # 1A .. 1Z, 2A .. 2Z, the only class names the server hands out


def valid_label(label):
    return isinstance(label, str) and len(label) <= 4 and LABEL.fullmatch(label) is not None


def norm(tag):
    return tag.strip().casefold()


def dedupe(tags):
    """Drop repeated tags (ignoring case and whitespace), keeping the first one and the order."""
    seen, out = set(), []
    for t in tags:
        key = norm(t) if isinstance(t, str) else None
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        out.append(t)
    return out


def new_tags(existing, prefix, label, untag=False, also_remove=()):
    """The tag list after removing every tag with this prefix (or an also_remove prefix) and, unless untag, adding the single class tag."""
    prefixes = tuple(norm(x) for x in (prefix, *also_remove) if x.strip())
    kept = [t for t in existing if not (isinstance(t, str) and norm(t).startswith(prefixes))]
    if not untag:
        kept.append(prefix + label)
    return dedupe(kept)


def write_atomic(path, data):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def restore(cars_dir):
    n = 0
    for d in sorted(p for p in cars_dir.iterdir() if p.is_dir()):
        f = d / "ui" / "ui_car.json"
        bak = Path(str(f) + ".gridbak")
        if bak.exists():
            write_atomic(f, bak.read_bytes())
            bak.unlink()
            n += 1
            print(f"restored {d.name}")
    print(f"Restored {n} car file(s).")


def main(argv=None, labels=None):
    """labels is {car id: class} from the caller (ac_live.py tag). Without labels only --untag and --restore do anything."""
    ac_paths.utf8_output()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cars_dir")
    ap.add_argument("--prefix", default="#Pride-")
    ap.add_argument("--also-remove", action="append", default=[],
                    help="also remove tags starting with this prefix (repeatable), e.g. tags from an older version")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--untag", action="store_true")
    ap.add_argument("--restore", action="store_true")
    a = ap.parse_args(argv)

    cars = Path(a.cars_dir)
    if not cars.is_dir():
        sys.exit(f"Cars folder not found: {cars}")
    if a.restore:
        return restore(cars)
    if labels is None and not a.untag:
        sys.exit("Nothing to do: use --untag or --restore (class tags are applied by ac_live.py tag).")
    for x in (a.prefix, *a.also_remove):
        if len(x.strip()) < 3:
            sys.exit("A tag prefix is too short; it must be at least 3 characters so other tags are never removed.")
    labels = labels or {}
    bad = sorted(k for k, v in labels.items() if not valid_label(v))
    if bad:   # a class the server should never send: leave those cars alone
        print("Ignoring %d car(s) whose class from the server is not in the expected form: %s"
              % (len(bad), ", ".join(bad[:5]) + (" ..." if len(bad) > 5 else "")))
        labels = {k: v for k, v in labels.items() if k not in bad}
    excl = re.compile(a.exclude) if a.exclude else None

    changed = same = skipped = 0
    for d in sorted(p for p in cars.iterdir() if p.is_dir()):
        label = labels.get(d.name)
        f = d / "ui" / "ui_car.json"
        if excl and excl.search(d.name):
            skipped += 1
            continue
        if not f.exists() or (label is None and not a.untag):
            skipped += 1
            continue
        raw = f.read_bytes()
        try:
            data = json.loads(raw.decode("utf-8-sig", errors="replace"), strict=False)
        except ValueError as e:
            print(f"skip {d.name}: ui_car.json is not valid JSON ({e}); left untouched")
            skipped += 1
            continue
        old = data.get("tags", [])
        if not isinstance(old, list):
            print(f"skip {d.name}: 'tags' is not a list; left untouched")
            skipped += 1
            continue
        tags = new_tags(old, a.prefix, label or "", untag=a.untag, also_remove=a.also_remove)
        if tags == old:
            same += 1
            continue
        changed += 1
        print(f"{'set' if a.apply else 'would set'} {d.name}: {old} -> {tags}")
        if not a.apply:
            continue
        bak = Path(str(f) + ".gridbak")
        if not bak.exists():  # keep the first backup, it is the original
            write_atomic(bak, raw)
        data["tags"] = tags
        out = json.dumps(data, indent=4, ensure_ascii=False).encode("utf-8")
        write_atomic(f, (BOM if raw.startswith(BOM) else b"") + out)

    verb = "Changed" if a.apply else "Would change"
    print(f"{verb} {changed} car(s); {same} already correct; {skipped} skipped."
          + ("" if a.apply else " Dry run: nothing written (use Apply tags)."))


if __name__ == "__main__":
    main()
