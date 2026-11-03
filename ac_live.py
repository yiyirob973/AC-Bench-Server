"""Live server data for the Server cars list.

Rather than downloading the whole class export and matching it by hand, the app hashes the cars installed on this PC
(cached, only changed cars are re-hashed) and asks the server about just those (POST /v1/cars/lookup, up to 500 hashes
per request). The answer is current when it's asked for and is kept in memory only. Nothing from the server's ratings is
written to disk.
"""
import hashlib
import json
import re
import threading
from pathlib import Path

import ac_carid
import ac_cloud

import ac_paths  # noqa: E402
HERE = ac_paths.DATA
HASH_CACHE = HERE / "car_hash_cache.json"   # {car folder: {"sig": [...], "hash": hex or null}}, local hashes only
BATCH = 500


HASH = re.compile(r"[0-9a-f]{64}")


class LiveUnavailable(Exception):
    pass


def _sig(car_dir):
    """Cheap change signature for a car's physics files (stat only, nothing is read)."""
    out = []
    for name in ("data.acd", "collider.ini"):
        p = car_dir / name
        try:
            st = p.stat()
            out.append([name, st.st_size, st.st_mtime_ns])
        except OSError:
            out.append([name, None, None])
    ddir = car_dir / "data"
    n = size = newest = 0
    if ddir.is_dir():
        for p in ddir.rglob("*"):
            try:
                if p.is_file():
                    st = p.stat()
                    n, size, newest = n + 1, size + st.st_size, max(newest, st.st_mtime_ns)
            except OSError:
                pass
    out.append(["data/", n, size, newest])
    return out


def _fingerprint(car_dir):
    """Short fingerprint of a car's physics files by name and size only. The in-game app computes the same one from
    its own folder listing, so when they match it can take the hash from the list below without opening any file."""
    items = []
    base = Path(car_dir)
    for name in ("data.acd", "collider.ini"):
        p = base / name
        try:
            if p.is_file():
                items.append("%s:%d" % (name, p.stat().st_size))
        except OSError:
            pass
    ddir = base / "data"
    if ddir.is_dir():
        for p in ddir.rglob("*"):
            try:
                if p.is_file():
                    items.append("data/%s:%d" % (p.relative_to(ddir).as_posix(), p.stat().st_size))
            except OSError:
                pass
    items.sort()
    return hashlib.sha256("|".join(items).encode("utf-8")).hexdigest()[:16]


def write_lua_hashes(cars_dir, entries):
    """Leave {car id: [fingerprint, hash]} next to the in-game app (car_hashes.json), so it doesn't have to read and hash
    car files while you play. Cars missing from entries keep their old line. Does nothing if the app isn't installed.
    """
    app = Path(cars_dir).parent.parent / "apps" / "lua" / "ac_bench"
    if not app.is_dir() or not entries:
        return
    f = app / "car_hashes.json"
    try:
        old = json.loads(f.read_text(encoding="utf-8")).get("cars", {})
        if not isinstance(old, dict):
            old = {}
    except (OSError, ValueError, AttributeError):
        old = {}
    cars = dict(old)
    cars.update(entries)
    text = json.dumps({"v": 1, "cars": cars}, separators=(",", ":"))
    try:
        if f.is_file() and f.read_text(encoding="utf-8") == text:
            return
        f.write_text(text, encoding="utf-8")
    except OSError:
        pass


def local_hashes(cars_dir, ids, progress=None, cancel=None):
    """{folder id: physics hash} for the cars that have one. Uses and updates the on-disk cache."""
    try:
        cache = json.loads(HASH_CACHE.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    base, result, dirty, seed = Path(cars_dir), {}, False, {}
    for n, cid in enumerate(ids, 1):
        if cancel and cancel():
            raise LiveUnavailable("cancelled")
        d = base / cid
        sig = _sig(d)
        e = cache.get(cid)
        if isinstance(e, dict) and e.get("sig") == sig:
            h = e.get("hash")
            if h and not e.get("fp"):
                e["fp"] = _fingerprint(d)
                dirty = True
        else:
            h = ac_carid.car_hash(d)
            cache[cid] = e = {"sig": sig, "hash": h}
            if h:
                e["fp"] = _fingerprint(d)
            dirty = True
        if h:
            result[cid] = h
            seed[cid] = [e["fp"], h]
        if progress and n % 25 == 0:
            progress(n, len(ids))
    stale = [k for k in cache if k not in set(ids)]
    for k in stale:
        cache.pop(k)
        dirty = True
    if dirty:
        try:
            HASH_CACHE.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    write_lua_hashes(cars_dir, seed)
    return result


def lookup(server, hashes):
    """Ask the server about these hashes. Returns {"records": {hash: record}, "tracks": [...], "table_version": n,
    "generated": unix time}. Raises LiveUnavailable with a readable reason.
    """
    if not server or not server.strip():
        raise LiveUnavailable("No server address set.")
    cfg = {"server": server.strip().rstrip("/")}
    hashes = sorted(set(hashes))
    records, meta = {}, {}
    for i in range(0, len(hashes), BATCH):
        try:
            code, data, _ = ac_cloud.call(cfg, "POST", "/v1/cars/lookup", {"hashes": hashes[i:i + BATCH]})
        except SystemExit as e:           # ac_cloud.call reports network errors this way
            raise LiveUnavailable(str(e))
        if code == 404:
            raise LiveUnavailable("The server is too old: it has no live lookup (/v1/cars/lookup). Update the server.")
        if code == 429:
            raise LiveUnavailable("The server's lookup rate limit was reached; try again later.")
        if code != 200 or not isinstance(data, dict):
            raise LiveUnavailable("Lookup failed: %s" % ((data or {}).get("error", code) if isinstance(data, dict) else code))
        meta = data
        cars = data.get("cars", [])
        if not isinstance(cars, list):
            raise LiveUnavailable("The server's answer is not in the expected form.")
        for rec in cars:
            h = rec.get("car_hash") if isinstance(rec, dict) else None
            if isinstance(h, str) and HASH.fullmatch(h):   # anything else is dropped, not guessed at
                records[h] = rec
    return {"records": records, "tracks": meta.get("tracks", []), "table_version": meta.get("table_version"),
            "generated": meta.get("generated")}


def to_rows_source(ids_to_hash, live):
    """{folder id: csv-like row} for the cars the server knows, in the shape ac_bench_gui.load_cloud_rows reads."""
    out = {}
    for cid, h in ids_to_hash.items():
        rec = live["records"].get(h)
        if not rec:
            continue
        r = {"id": cid, "class": rec.get("class") or "", "cloud_status": rec.get("status", ""),
             "gap_pct": rec.get("gap_pct", ""), "score_pct": rec.get("score_pct", ""),
             "variance_pct": rec.get("variance_pct", ""), "flags": "; ".join(rec.get("flags") or []),
             "bop": rec.get("bop", "") or ""}
        for t in live["tracks"]:
            ms = (rec.get("times_ms") or {}).get(t)
            r["time_" + t.replace("/", "_")] = "" if ms is None else round(ms / 1000, 3)
        out[cid] = r
    return out


def fetch_async(server, cars_dir, ids, on_progress, on_done):
    """Hash and look up on a worker thread. on_progress(text) and on_done(result_or_None, error_or_None) are called from
    that thread, so the caller has to hand them over to the UI thread (root.after).
    """
    def work():
        try:
            ih = local_hashes(cars_dir, ids, lambda n, t: on_progress("hashing cars %d / %d ..." % (n, t)))
            if not ih:
                raise LiveUnavailable("None of the installed cars has physics files to identify it by.")
            on_progress("asking the server about %d cars ..." % len(ih))
            live = lookup(server, ih.values())
            on_done({"by_id": to_rows_source(ih, live), "live": live, "hashed": len(ih)}, None)
        except LiveUnavailable as e:
            on_done(None, str(e))
        except Exception as e:           # a worker thread must never die without saying why
            on_done(None, "%s: %s" % (type(e).__name__, e))
    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


def cmd_tag(args):
    """Apply class tags: find the cars installed on this PC, ask the server about only those and tag only those. Nothing is
    downloaded beyond the answer for the local cars and nothing is saved.
    """
    import argparse  # noqa: F401  (parsed by main)
    import re
    import ac_tag_writer
    base = Path(args.cars_dir)
    if not base.is_dir():
        raise SystemExit("Cars folder not found: %s" % base)
    excl = re.compile(args.exclude) if args.exclude else None
    ids = sorted(d.name for d in base.iterdir()
                 if d.is_dir() and (d / "ui" / "ui_car.json").exists() and not (excl and excl.search(d.name)))
    if not ids:
        raise SystemExit("No taggable cars found in %s" % base)
    print("Found %d car(s) installed locally; identifying them ..." % len(ids))
    try:
        ih = local_hashes(base, ids, lambda n, t: print("  hashed %d / %d" % (n, t), flush=True))
        if not ih:
            raise LiveUnavailable("None of the installed cars has physics files to identify it by.")
        print("Asking the server about %d car(s)..." % len(ih), flush=True)
        live = lookup(args.server, ih.values())
    except LiveUnavailable as e:
        raise SystemExit(str(e))
    rows = to_rows_source(ih, live)
    ok = {"verified", "provisional"} if args.include_provisional else {"verified"}
    labels = {cid: r["class"] for cid, r in rows.items() if r["class"] and r["cloud_status"].lower() in ok}
    print("The server knows %d of your cars; %d have a %s class." % (
        len(rows), len(labels), "verified or provisional" if args.include_provisional else "verified"))
    if not labels:
        print("Nothing to tag.")
        return 0
    argv = [str(base), "--prefix", args.prefix]
    if args.exclude:
        argv += ["--exclude", args.exclude]
    for old in args.also_remove:
        argv += ["--also-remove", old]
    if args.apply:
        argv.append("--apply")
    ac_tag_writer.main(argv, labels=labels)
    return 0


def main():
    ac_paths.utf8_output()
    import argparse
    ap = argparse.ArgumentParser(description="Live server data for the cars installed on this PC.")
    sub = ap.add_subparsers(dest="command", required=True)
    t = sub.add_parser("tag", help="write class tags for the locally installed cars (looked up live)")
    t.add_argument("--server", required=True)
    t.add_argument("--cars-dir", required=True)
    t.add_argument("--prefix", default="#Pride-")
    t.add_argument("--exclude", default="")
    t.add_argument("--also-remove", action="append", default=[])
    t.add_argument("--include-provisional", action="store_true")
    t.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    raise SystemExit({"tag": cmd_tag}[args.command](args))


if __name__ == "__main__":
    main()
