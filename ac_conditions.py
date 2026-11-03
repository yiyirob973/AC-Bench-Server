#!/usr/bin/env python3
"""Benchmark conditions as the server publishes them (GET /v1/conditions).

The app doesn't decide the baseline. Profile, tracks, lap counts, batch limit and the canonical environment (digests of
the vanilla track folders and of acs.exe) all come from the server. Anything that depends on them (benchmark, vanilla
check, upload) fetches them first with If-None-Match. The last answer is kept in conditions_cache.json only so the
server can reply "304 not modified". A cache older than 12 hours, or one from another server, is refused and has to be
fetched again.

  python ac_conditions.py fetch [--server https://example.org]
  python ac_conditions.py show

The server address is stored in cloud_config.json (shared with ac_cloud.py).
"""
import argparse, json, os, re, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path


import ac_net    # noqa: E402
import ac_paths  # noqa: E402
HERE = ac_paths.DATA
CACHE = HERE / "conditions_cache.json"
CLOUD_CFG = HERE / "cloud_config.json"
MAX_AGE_S = 12 * 3600
ALLOW_STALE = False  # only the GUI sets this, in its own process, to show the last known conditions while offline


class ConditionsUnavailable(RuntimeError):
    """The server's benchmark conditions can't be used (not downloaded, too old, invalid or unreachable)."""


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


def _int(d, key, lo, hi):
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ConditionsUnavailable(f"The server's conditions have a missing or invalid '{key}'.")
    return v


def validate(data):
    """Check a /v1/conditions answer and return a clean dict. Anything unexpected raises ConditionsUnavailable."""
    if not isinstance(data, dict):
        raise ConditionsUnavailable("The server's conditions are not a JSON object.")
    if data.get("format") != 1:
        raise ConditionsUnavailable(f"The server's conditions use format {data.get('format')!r}; this app understands "
                                    "format 1. Update the app.")
    profile = data.get("profile")
    if not isinstance(profile, str) or not _NAME.match(profile):
        raise ConditionsUnavailable("The server's conditions have an invalid profile name.")
    tracks = data.get("tracks")
    if not isinstance(tracks, list) or not 1 <= len(tracks) <= 6:
        raise ConditionsUnavailable("The server's conditions have no valid track list.")
    for t in tracks:
        parts = t.split("/") if isinstance(t, str) else []
        if not 1 <= len(parts) <= 2 or not all(_NAME.match(p) for p in parts):
            raise ConditionsUnavailable(f"The server sent an unsafe track name: {t!r}")
    laps, timed = _int(data, "laps", 2, 50), _int(data, "timed_laps", 1, 49)
    if laps != timed + 1:
        raise ConditionsUnavailable("The server's lap counts do not fit the scoring (laps must be timed_laps + 1: "
                                    "lap 1 is the standing start and is dropped).")
    max_batch, max_entries = _int(data, "max_batch", 1, 64), _int(data, "max_entries", 1, 1_000_000)
    env = data.get("env")
    if (not isinstance(env, dict) or not isinstance(env.get("tracks"), dict) or not env["tracks"]
            or not isinstance(env.get("acs"), str) or not env["acs"]):
        raise ConditionsUnavailable("The server has no canonical environment yet (the operator must run set-env).")
    for t in tracks:
        if not isinstance(env["tracks"].get(t.partition("/")[0]), str):
            raise ConditionsUnavailable(f"The server's environment has no digest for track {t.partition('/')[0]}.")
    extra = data.get("conditions", {})
    if not isinstance(extra, dict):
        raise ConditionsUnavailable("The server's 'conditions' field is not an object.")
    accepting = data.get("accepting_submissions")
    if not isinstance(accepting, bool):
        raise ConditionsUnavailable("The server's conditions have no valid 'accepting_submissions'.")
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise ConditionsUnavailable("The server's conditions have no version.")
    return {"profile": profile, "tracks": list(tracks), "laps": laps, "timed_laps": timed, "max_batch": max_batch,
            "max_entries": max_entries, "env": env, "conditions": extra, "accepting_submissions": accepting,
            "version": version}


# ---------------------------------------------------------------- server address / cache files
def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path, data):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def server_url():
    cfg = _read_json(CLOUD_CFG) or {}
    s = cfg.get("server")
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        return ac_net.check_server_url(s)
    except ValueError as e:   # an address saved by an older version, or edited by hand
        raise ConditionsUnavailable(str(e))


def set_server(url):
    try:
        url = ac_net.check_server_url(url)
    except ValueError as e:
        raise ConditionsUnavailable(str(e))
    cfg = _read_json(CLOUD_CFG) or {}
    if cfg.get("server") != url:
        cfg["server"] = url
        _write_json(CLOUD_CFG, cfg)


def _read_cache():
    c = _read_json(CACHE)
    return c if isinstance(c, dict) else None


# ---------------------------------------------------------------- fetch / load
def fetch(server=None, say=print):
    """GET /v1/conditions (If-None-Match with the last ETag). Returns (clean conditions, 'updated' | 'unchanged')."""
    if server:
        set_server(server)
    url = server_url()
    if not url:
        raise ConditionsUnavailable("No server address set. Fill it in on the Server tab: the benchmark conditions "
                                    "come from the server.")
    cached = _read_cache()
    etag = cached.get("etag") if cached and cached.get("server") == url and cached.get("data") else None
    headers = {"Accept": "application/json", "User-Agent": "ac_bench-conditions/1"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url + "/v1/conditions", headers=headers)
    try:
        with ac_net.open_url(req, timeout=30) as r:
            code, raw, new_etag = r.status, ac_net.read_capped(r, ac_net.MAX_SMALL_REPLY), r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304 and etag:
            code, raw, new_etag = 304, b"", etag
        else:
            raise ConditionsUnavailable(f"The server answered {e.code} for /v1/conditions: cannot continue.")
    except urllib.error.URLError as e:
        raise ConditionsUnavailable(f"Could not reach the server: {e.reason}")
    except OSError as e:
        raise ConditionsUnavailable(f"Could not reach the server: {e}")
    except ValueError as e:
        raise ConditionsUnavailable(str(e))
    if code == 304:
        clean = validate(cached["data"])
        cached["fetched_at"] = time.time()
        _write_json(CACHE, cached)
        return clean, "unchanged"
    try:
        data = json.loads(raw)
    except ValueError:
        raise ConditionsUnavailable("The server's conditions are not valid JSON.")
    clean = validate(data)
    _write_json(CACHE, {"server": url, "etag": new_etag, "fetched_at": time.time(), "data": data})
    return clean, "updated"


def fetch_baseline_tracks(server=None):
    """For the server operator: the profile and track list the server publishes, without needing a canonical environment.
    A brand-new server has none yet (that's what the reference is for), so fetch() and load() would refuse it. Nothing is
    cached and only the track list and profile are trusted. Returns (profile, [track specs]).
    """
    if server:
        set_server(server)
    url = server_url()
    if not url:
        raise ConditionsUnavailable("No server address set: the track list comes from the server.")
    req = urllib.request.Request(url + "/v1/conditions", headers={"Accept": "application/json",
                                                                   "User-Agent": "ac_bench-conditions/1"})
    try:
        with ac_net.open_url(req, timeout=30) as r:
            data = json.loads(ac_net.read_capped(r, ac_net.MAX_SMALL_REPLY))
    except urllib.error.HTTPError as e:
        raise ConditionsUnavailable(f"The server answered {e.code} for /v1/conditions: cannot continue.")
    except (urllib.error.URLError, OSError) as e:
        raise ConditionsUnavailable(f"Could not reach the server: {getattr(e, 'reason', e)}")
    except ValueError:
        raise ConditionsUnavailable("The server's conditions are not valid JSON.")
    if not isinstance(data, dict) or data.get("format") != 1:
        raise ConditionsUnavailable("The server's conditions use an unknown format. Update the app.")
    profile, tracks = data.get("profile"), data.get("tracks")
    if not isinstance(profile, str) or not _NAME.match(profile):
        raise ConditionsUnavailable("The server's conditions have an invalid profile name.")
    if not isinstance(tracks, list) or not 1 <= len(tracks) <= 6:
        raise ConditionsUnavailable("The server's conditions have no valid track list.")
    for t in tracks:
        parts = t.split("/") if isinstance(t, str) else []
        if not 1 <= len(parts) <= 2 or not all(_NAME.match(x) for x in parts):
            raise ConditionsUnavailable(f"The server sent an unsafe track name: {t!r}")
    return profile, list(tracks)


def load():
    """The last fetched conditions as a clean dict. A missing, foreign or too-old cache is refused; nothing is made up."""
    c = _read_cache()
    if not c or not c.get("data"):
        raise ConditionsUnavailable("The benchmark conditions have not been downloaded from the server yet. "
                                    "Press Refresh on the Benchmark tab (or run: ac_conditions.py fetch).")
    if c.get("server") != server_url():
        raise ConditionsUnavailable("The stored benchmark conditions came from a different server. Fetch them again.")
    age = time.time() - float(c.get("fetched_at") or 0)
    if not ALLOW_STALE and not -300 <= age <= MAX_AGE_S:
        raise ConditionsUnavailable("The stored benchmark conditions are older than 12 hours. Fetch them again "
                                    "(the app does this before every benchmark, check and upload).")
    return validate(c["data"])


def load_any():
    """Like load(), but returns None instead of raising (for display)."""
    try:
        return load()
    except ConditionsUnavailable:
        return None


def fetched_at():
    c = _read_cache()
    return float(c["fetched_at"]) if c and c.get("fetched_at") else None


def summary(d):
    return (f"profile {d['profile']} | tracks {', '.join(d['tracks'])} | {d['laps']} laps "
            f"(lap 1 dropped, median of {d['timed_laps']}) | up to {d['max_batch']} cars per pack | "
            f"server version {d['version']} | submissions {'open' if d['accepting_submissions'] else 'CLOSED'}")


def main():
    ac_paths.utf8_output()
    ap = argparse.ArgumentParser(description="Benchmark conditions from the server")
    sub = ap.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--server")
    sub.add_parser("show")
    a = ap.parse_args()
    try:
        if a.command == "fetch":
            d, how = fetch(a.server)
            print(("Benchmark conditions unchanged: " if how == "unchanged" else "Benchmark conditions updated: ") + summary(d))
        else:
            d = load()
            print(summary(d))
    except ConditionsUnavailable as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
