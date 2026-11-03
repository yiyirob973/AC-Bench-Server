#!/usr/bin/env python3
"""Client for the AC Bench server.

  python ac_cloud.py ping     --server https://example.org
  python ac_cloud.py register --server https://example.org --invite CODE --name MyName
  python ac_cloud.py upload   --cars-dir "<AC>/content/cars" [--attest-legacy]

upload: sends every benchmark result that was driven on the verified vanilla environment under the current baseline
        (lap_results.json + track_check_log.json). Results are matched to cars by the hash of the car's physics files,
        never by folder name. The server recomputes the scores and may reject entries; the reasons are printed.
The server address and your API key are kept in cloud_config.json next to this file. The benchmark conditions (profile,
tracks, laps, environment) are fetched from the server (ac_conditions.py) before an upload, none of it is set here.
"""
import argparse, json, sys, urllib.error, urllib.request
from pathlib import Path


import ac_paths  # noqa: E402
HERE = ac_paths.DATA   # settings, results and caches live next to the exe (or the .py files)
from ac_car_classifier import read_ui, class_sort_key   # noqa: E402
import ac_carid               # noqa: E402
import ac_net                 # noqa: E402
import ac_conditions          # noqa: E402  (profile, tracks, laps and environment all come from the server)

CFG_FILE = HERE / "cloud_config.json"
UPLOADED = HERE / "cloud_uploaded.json"
CHUNK = 400


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=1), encoding="utf-8")


def settings(args):
    cfg = load_json(CFG_FILE, {})
    if getattr(args, "server", None):
        cfg["server"] = args.server.rstrip("/")
    if not cfg.get("server"):
        raise SystemExit("No server set. Pass --server URL (or fill it in on the GUI's Server tab).")
    try:
        cfg["server"] = ac_net.check_server_url(cfg["server"])
    except ValueError as e:
        raise SystemExit(str(e))
    return cfg


def call(cfg, method, path, body=None, auth=False, etag=None):
    """Returns (status, parsed json or None, headers)."""
    headers = {"Accept": "application/json", "User-Agent": "ac_bench-cloud/1"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if auth:
        if not cfg.get("api_key"):
            raise SystemExit("Not registered yet. Run: ac_cloud.py register --invite CODE --name NAME")
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    if etag:
        headers["If-None-Match"] = etag
    try:   # the call() callers pass cfg straight in, so the address is checked here as well as in settings()
        base = ac_net.check_server_url(cfg["server"])
    except ValueError as e:
        raise SystemExit(str(e))
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with ac_net.open_url(req, timeout=60) as r:
            raw = ac_net.read_capped(r)
            return r.status, (json.loads(raw) if raw else None), dict(r.headers)
    except ValueError as e:
        raise SystemExit("The server sent something the app can't use: %s" % e)
    except urllib.error.HTTPError as e:
        raw = e.read(ac_net.MAX_ERROR_BODY)
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except Exception:
            return e.code, {"error": raw[:200].decode("utf-8", "replace")}, dict(e.headers)
    except urllib.error.URLError as e:
        raise SystemExit("Could not reach the server: %s" % e.reason)


# ------------------------------------------------------------------ commands
def cmd_ping(args):
    cfg = settings(args)
    code, h, _ = call(cfg, "GET", "/v1/health")
    if code != 200:
        raise SystemExit("Server answered %s" % code)
    save_json(CFG_FILE, cfg)
    c, how = ac_conditions.fetch(cfg["server"])
    print("Server OK. Benchmark conditions %s: %s" % (how, ac_conditions.summary(c)))
    if not c["accepting_submissions"]:
        print("PROBLEM: the server is not accepting submissions right now")
    print("Registered: %s" % ("yes" if cfg.get("api_key") else "no"))


def cmd_register(args):
    cfg = settings(args)
    print("Registering as %s with %s (invite code of %d characters)..." % (args.name, cfg["server"], len(args.invite.strip())),
          flush=True)
    code, r, _ = call(cfg, "POST", "/v1/register", {"invite": args.invite.strip(), "name": args.name.strip()})
    if code != 201:
        raise SystemExit("Registration failed at %s (HTTP %s): %s" % (cfg["server"], code, (r or {}).get("error", code)))
    cfg["api_key"], cfg["name"] = r["api_key"], r["name"]
    save_json(CFG_FILE, cfg)
    print("Registered as %s (role %s). The key is saved in %s: keep that file private." % (r["name"], r["role"], CFG_FILE.name))


def cmd_link(args):
    """On a signed-in device: get a single-use code that signs another device in as the same account."""
    cfg = settings(args)
    code, r, _ = call(cfg, "POST", "/v1/devices/link", {}, auth=True)
    if code != 201:
        raise SystemExit("Could not create a link code at %s (HTTP %s): %s" % (cfg["server"], code, (r or {}).get("error", code)))
    print("LINK CODE: %s" % r["code"])
    print("Valid for %d minutes, works once. On the other device, open the Setup tab, enter this code under "
          "'Use the same account on another device' and press 'Sign in'." % (r["expires_in"] // 60))
    return r


def cmd_login(args):
    """On a new device: trade a link code for this device's own key to the same account."""
    cfg = settings(args)
    import socket
    print("Signing in at %s ..." % cfg["server"], flush=True)
    code, r, _ = call(cfg, "POST", "/v1/devices/redeem", {"code": args.code.strip(), "label": socket.gethostname()})
    if code != 201:
        raise SystemExit("Sign-in failed at %s (HTTP %s): %s" % (cfg["server"], code, (r or {}).get("error", code)))
    cfg["api_key"], cfg["name"] = r["api_key"], r["name"]
    save_json(CFG_FILE, cfg)
    print("Signed in as %s (role %s). This device has its own key, saved in %s: keep that file private." % (r["name"], r["role"], CFG_FILE.name))


def latest_env(env_log):
    """(id, env) of the newest passing vanilla check that recorded an environment."""
    for entry in reversed(env_log):
        if entry.get("status") == "ok" and entry.get("env_id") and entry.get("env"):
            return entry["env_id"], entry["env"]
    return None, None


def old_check_digests(env_log):
    """Per track id: the digests seen in passing checks from before environments were recorded."""
    seen = {}
    for entry in env_log:
        if entry.get("status") == "ok" and not entry.get("ignore_ui") and not entry.get("env_id"):
            for t, v in (entry.get("tracks") or {}).items():
                if isinstance(v, dict) and v.get("digest"):
                    seen.setdefault(t, set()).add(v["digest"])
    return seen


def build_entries(results, env_log, cars_dir, server_cfg, attest_legacy=False):
    """Results that can be uploaded, the environments they use, and a count of why the others were skipped.

    Results benchmarked before environments were stamped have no env id. With attest_legacy they get stamped with the
    current verified environment, but only if every older passing vanilla check on record for that track saw the same
    track digest (so the track hasn't changed since) and a current verified environment exists. acs.exe can't be proven
    for old runs, so you're vouching for it.
    """
    envs = {}
    for entry in env_log:
        if entry.get("status") == "ok" and entry.get("env_id") and entry.get("env"):
            envs[entry["env_id"]] = entry["env"]
    cur_id, cur_env = latest_env(env_log)
    history = old_check_digests(env_log)
    skipped, entries, used_envs = {}, [], {}

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1

    hash_cache = {}
    for car, per in results.items():
        for track, e in per.items():
            if e.get("status") != "ok":
                continue
            tid = track.partition("/")[0]
            env_ref = e.get("env")
            if track not in server_cfg["tracks"]:
                skip("track not part of the server baseline")
                continue
            if e.get("profile") != server_cfg["profile"]:
                skip("made under another baseline profile (benchmark again with the current app)")
                continue
            if (e.get("batch") or 1) > server_cfg["max_batch"]:
                skip("cars shared the track (batch above the server limit)")
                continue
            if env_ref and env_ref not in envs:
                skip("its environment id is not in track_check_log.json")
                continue
            if not env_ref:                       # benchmarked before environments were stamped
                if not attest_legacy:
                    skip("benchmarked before the cloud update, no environment stamp "
                         "(use 'include older results' / --attest-legacy)")
                    continue
                if not cur_env or tid not in (cur_env.get("tracks") or {}):
                    skip("no current vanilla check on record for this track (press Verify tracks now first)")
                    continue
                if not history.get(tid):
                    skip("no earlier passing vanilla check on record for this track")
                    continue
                if history[tid] != {cur_env["tracks"][tid]}:
                    skip("the track was different at benchmark time (older vanilla checks do not match the current one)")
                    continue
                env_ref = cur_id
                envs[cur_id] = cur_env
            if len(e.get("laps_ms") or []) < server_cfg["laps"]:
                skip("too few laps recorded")
                continue
            h = e.get("car_hash")
            if not h and cars_dir:
                if car not in hash_cache:
                    hash_cache[car] = ac_carid.car_hash(Path(cars_dir) / car)
                h = hash_cache[car]
            if not h:
                skip("car files not found, cannot identify the car")
                continue
            name = read_ui(Path(cars_dir) / car).get("name", car) if cars_dir else car
            entries.append({"car_id": car, "car_hash": h, "name": name, "track": track,
                            "laps_ms": e["laps_ms"][:server_cfg["laps"]], "batch": e.get("batch") or 1, "env": env_ref})
            used_envs[env_ref] = envs[env_ref]
    return entries, used_envs, skipped


def cmd_upload(args):
    upload_results(args)


def upload_results(args, only_cars=None):
    """Send results to the server. only_cars limits it to those car folders (the automatic upload after each pack uses it).
    Raises SystemExit with the reason if the upload can't be done. Results that were already sent are remembered, so a
    later call only sends what's new.
    """
    cfg = settings(args)
    cond, _ = ac_conditions.fetch(cfg["server"])          # If-None-Match: always ask the server first
    if not cond["accepting_submissions"]:
        raise SystemExit("The server is not accepting submissions right now.")
    sc = {"profile": cond["profile"], "tracks": cond["tracks"], "laps": cond["laps"], "max_batch": cond["max_batch"]}
    import ac_rating                                         # after the fetch, since it may read the baseline on import
    results = load_json(args.results, {})
    if only_cars is not None:
        results = {car: per for car, per in results.items() if car in only_cars}
    env_log = load_json(args.log, [])
    entries, envs, skipped = build_entries(results, env_log, args.cars_dir, sc, getattr(args, "attest_legacy", False))
    done = load_json(UPLOADED, {})
    if not args.all:
        before = len(entries)
        entries = [e for e in entries if done.get(e["car_hash"] + "|" + e["track"]) != round(ac_rating.lap_score({"laps_ms": e["laps_ms"]})[0])]
        if before - len(entries):
            print("%d result(s) already uploaded unchanged (use --all to resend)" % (before - len(entries)))
    for why, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print("skipped %4d: %s" % (n, why))
    if not entries:
        print("Nothing new to upload.")
        return
    accepted, reasons, status, classed = 0, {}, {}, {}
    step = max(1, min(CHUNK, cond["max_entries"]))
    for i in range(0, len(entries), step):
        chunk = entries[i:i + step]
        body = {"client": {"app": "ac_bench", "version": 1}, "profile": sc["profile"],
                "environments": {k: envs[k] for k in {e["env"] for e in chunk}}, "results": chunk}
        code, r, _ = call(cfg, "POST", "/v1/submit", body, auth=True)
        if code != 200:
            raise SystemExit("Upload failed: %s (%s)" % ((r or {}).get("error", "error"), code))
        bad = {x["index"] for x in r["rejected"]}
        for x in r["rejected"]:
            reasons[x["reason"]] = reasons.get(x["reason"], 0) + 1
        for j, e in enumerate(chunk):
            if j not in bad and r["rejected_total"] == len(r["rejected"]):
                done[e["car_hash"] + "|" + e["track"]] = round(ac_rating.lap_score({"laps_ms": e["laps_ms"]})[0])
        accepted += r["accepted"]
        for rec in r.get("cars", []):
            classed[rec["car_hash"]] = rec
        for k, v in r["merged_status"].items():
            status[k] = status.get(k, 0) + v
    save_json(UPLOADED, done)
    print("accepted %d of %d results. Merged state: %s" % (accepted, len(entries), json.dumps(status)))
    for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("rejected %4d: %s" % (n, why))
    if classed:
        print("The server's classes for the cars you uploaded:")
        for rec in sorted(classed.values(), key=lambda x: (class_sort_key(x["class"]) if x.get("class") else (2,), x.get("name", ""))):
            gap = rec.get("gap_pct")
            print("  %-4s %-34s %s%s" % (rec.get("class") or "-", (rec.get("name") or rec.get("car_id") or "")[:34],
                                         rec.get("status", ""), "" if gap is None else ", %.2f%% off the fastest" % gap))


def main():
    ac_paths.utf8_output()
    ap = argparse.ArgumentParser(description="AC Bench cloud client")
    ap.add_argument("--server", help="server address, e.g. https://acbench.example.org")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("ping")
    s = sub.add_parser("register")
    s.add_argument("--invite", required=True)
    s.add_argument("--name", required=True)
    sub.add_parser("link")
    s = sub.add_parser("login")
    s.add_argument("--code", required=True)
    s = sub.add_parser("upload")
    s.add_argument("--results", default=str(HERE / "lap_results.json"))
    s.add_argument("--log", default=str(HERE / "track_check_log.json"))
    s.add_argument("--cars-dir", help="content/cars (used to identify cars and read names)")
    s.add_argument("--all", action="store_true", help="resend results that were already uploaded")
    s.add_argument("--attest-legacy", action="store_true",
                   help="also upload results benchmarked before environments were stamped, if the track digests of all "
                        "older vanilla checks equal the current verified environment (run Verify tracks now first)")
    args = ap.parse_args()
    try:
        {"ping": cmd_ping, "register": cmd_register, "link": cmd_link, "login": cmd_login,
         "upload": cmd_upload}[args.command](args)
    except ac_conditions.ConditionsUnavailable as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
