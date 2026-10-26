#!/usr/bin/env python3
"""
AC Bench cloud server: stores benchmark lap times and the classes derived from them.

Users benchmark cars with the desktop tool and upload; the server validates every entry, merges
submissions from different users into one agreed time per car and track, and publishes the classes
for download. Standard library only (WSGI + SQLite). Put it behind a TLS reverse proxy.

Run
  python server.py init                                   create the database
  python server.py set-env --from track_check_log.json    canonical vanilla environment (see README)
  python server.py invite --role user --uses 20           print an invite code
  python server.py serve --port 8080                      small deployments; production: gunicorn server:application

Operate
  python server.py recalc-classes                         rebuild the class table now (the server also does it by itself)
  python server.py stats | disputed | ban NAME | unban NAME | config get|set KEY VALUE | recompute-all
  python server.py set-role NAME --role trusted|user      a trusted user's submissions verify on their own
  python server.py backup --to /data/backups/acbench-2026-01-01.db    consistent copy while running

Admin web UI
  /admin (PocketID / OpenID Connect login) does the same operator jobs in a browser, and edits the benchmark
  conditions; see admin.py for the ACB_OIDC_* / ACB_ADMIN_* environment variables.

Benchmark conditions (public, no key needed)
  POST /v1/submit  -> {"accepted", "rejected", "merged_status", "cars": [class record of each car just touched], ...}
  POST /v1/devices/link    (key needed) -> a single-use code, valid 10 minutes, to sign in on another device
  POST /v1/devices/redeem  {"code": ...} -> a new API key for the same account (the other device's own key)
  POST /v1/cars/lookup  {"hashes": [up to 500 car physics hashes]} -> live records for only those cars (no key needed)
  GET /v1/conditions  profile, tracks, laps, batch limit, free-form "conditions" and the reference environment
                      the server will accept; has an ETag, so clients can revalidate with If-None-Match.

What the server checks (a submission entry is rejected, with a reason, unless all hold)
  - valid API key (created from an invite), not banned, within the rate limit
  - baseline profile, track and lap count are the ones the server accepts, batch <= max_batch
  - environment attestation matches the canonical one: the vanilla track digests and the acs.exe hash
  - lap data is plausible and consistent; the score is recomputed here (median of the timed laps),
    the client's own numbers are never trusted

What the server does with it
  - one entry per user per car per track (a new upload replaces your old one)
  - car identity is the hash of the car's physics files, not its folder name
  - agreement: submissions within tolerance_pct of each other confirm one another. 2+ agreeing
    users (or one trusted user) = verified; a single user = provisional; users that disagree =
    disputed (not used until more data arrives)
  - classes come from one live class table (reference times = the fastest car per track, plus cut points),
    rebuilt in the background from the merged times. A car's class is a lookup in it, so it can move as the field grows.
"""

import argparse, bisect, collections, functools, gzip, hashlib, json, math, os, re, secrets, sqlite3, statistics, sys, threading, time
from http import HTTPStatus
from pathlib import Path
from socketserver import ThreadingMixIn
from types import SimpleNamespace
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ac_rating  # noqa: E402
import admin  # noqa: E402
from ac_rating import class_label  # noqa: E402

API_VERSION = 1
DEFAULT_CONFIG = {
    "profile": "baseline-v2",           # baseline profile id the server accepts
    "tracks": ["magione", "ks_vallelunga/club_circuit"],
    "laps": 4,                          # recorded laps per car: lap 1 + the timed laps
    "max_batch": 1,                     # cars sharing the track in the benchmark race (1 = cleanest)
    "tolerance_pct": 0.5,               # submissions agree if within this % of each other
    "min_confirmations": 2,             # distinct users that must agree (trusted users count alone)
    "spread_limit_pct": 1.5,            # max (slowest - fastest) / median over the timed laps
    "plaus_min_ms": 20000, "plaus_max_ms": 900000,  # absolute lap time bounds until a class table exists
    "plaus_low": 0.4, "plaus_high": 6.0,            # bounds as multiples of the class-table reference time
    "max_entries": 500, "max_body_bytes": 2000000,
    "rate_submits_per_hour": 20, "rate_register_per_hour": 10, "rate_lookups_per_hour": 240,
    "trust_proxy_header": False,        # honour X-Forwarded-For (only behind your own proxy)
    "class_gap": 2.0, "min_class_size": 3, "tie_zone": 0.10,  # class table settings (see ac_rating.py)
    "include_provisional": False,       # cut the classes from provisional results too, not only verified ones
    "variance_limit_pct": 3.0,          # flag a car whose balance between tracks differs this much from its class's usual balance
    "class_refresh_seconds": 10,        # the class borders are recalculated at most this often while results come in
    "env": None,                        # canonical {"tracks": {track: digest}, "acs": sha256}
    "conditions": {},                   # free-form benchmark conditions served to clients (weather, time, ...)
    "export_max_age": 30,               # seconds the /v1/classes export may lag behind new uploads (0 = always fresh)
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, key_hash TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL DEFAULT 'user', banned INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS user_keys (          -- extra API keys of one account: one per signed-in device
  key_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), label TEXT NOT NULL DEFAULT '',
  created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS device_links (       -- short-lived single-use codes to sign in on another device
  code_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS invites (
  code_hash TEXT PRIMARY KEY, role TEXT NOT NULL, uses_left INTEGER NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  car_hash TEXT NOT NULL, car_id TEXT NOT NULL, name TEXT NOT NULL, track TEXT NOT NULL,
  score_ms REAL NOT NULL, spread_pct REAL NOT NULL, laps_json TEXT NOT NULL, batch INTEGER NOT NULL,
  env_id TEXT, used INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL,
  UNIQUE (user_id, car_hash, track));
CREATE INDEX IF NOT EXISTS sub_key ON submissions (car_hash, track);
CREATE TABLE IF NOT EXISTS merged (
  car_hash TEXT NOT NULL, track TEXT NOT NULL, score_ms REAL NOT NULL, n_used INTEGER NOT NULL,
  n_total INTEGER NOT NULL, spread_pct REAL NOT NULL, status TEXT NOT NULL, updated INTEGER NOT NULL, ln_ms REAL,
  PRIMARY KEY (car_hash, track));
CREATE TABLE IF NOT EXISTS cars (
  car_hash TEXT PRIMARY KEY, car_id TEXT NOT NULL, name TEXT NOT NULL, updated INTEGER NOT NULL, added INTEGER);
CREATE INDEX IF NOT EXISTS users_created ON users (created, id);
CREATE INDEX IF NOT EXISTS merged_status ON merged (status, updated);
CREATE TABLE IF NOT EXISTS class_table (        -- the one live class table, rebuilt from merged (the time table) as results change
  id INTEGER PRIMARY KEY CHECK (id = 1), rev INTEGER NOT NULL, version INTEGER NOT NULL, built INTEGER NOT NULL,
  dv INTEGER NOT NULL, params TEXT NOT NULL, body TEXT NOT NULL,
  best REAL);   -- score of the fastest rated car: lowered by uploads that beat it, set exactly at every rebuild
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, who TEXT NOT NULL, kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ev_idx ON events (who, kind, ts);
CREATE INDEX IF NOT EXISTS ev_ts ON events (ts);
"""

CAR_ID = re.compile(r"^[A-Za-z0-9_.\- ]{1,80}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ENV_ID = re.compile(r"^[0-9a-f]{8,64}$")
USER_NAME = re.compile(r"^[A-Za-z0-9_.\-]{3,40}$")


# ------------------------------------------------------------------ helpers

def now():
    return int(time.time())


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Conn(sqlite3.Connection):
    path = None   # which database file this connection belongs to (keys the per-process caches)


def connect(path):
    db = sqlite3.connect(str(path), timeout=15, factory=Conn)
    db.path = str(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")     # safe with WAL (no corruption; a power cut can lose the last commits)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA cache_size=-4000")       # 4 MB page cache per connection
    db.execute("PRAGMA mmap_size=134217728")    # read through the OS page cache, shared by all connections
    return db


_local = threading.local()


def request_db(path):
    """One long-lived connection per worker thread: no connect/close (and no WAL checkpoint on close) per request."""
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    db = conns.get(path)
    if db is None:
        db = conns[path] = connect(path)
    return db


def init_db(path):
    db = connect(path)
    db.executescript(SCHEMA + admin.SCHEMA)
    db.execute("BEGIN IMMEDIATE")   # workers starting together take turns here, and the second finds the work done
    if db.execute("SELECT 1 FROM cars LIMIT 1").fetchone() is None and db.execute("SELECT 1 FROM submissions LIMIT 1").fetchone():
        # existing database from before the cars table: take each car's most recent submission's names (one-time scan)
        db.execute("INSERT OR IGNORE INTO cars(car_hash, car_id, name, updated) "
                   "SELECT car_hash, car_id, name, MAX(created) FROM submissions GROUP BY car_hash")
    if db.execute("SELECT 1 FROM class_table").fetchone() is None and \
            db.execute("SELECT 1 FROM sqlite_master WHERE name='class_tables'").fetchone():
        # database from the time of published class tables: the newest one becomes the live table, then it is rebuilt
        old = db.execute("SELECT version, created, body FROM class_tables ORDER BY version DESC LIMIT 1").fetchone()
        if old:
            body = json.loads(old["body"])
            body["labels"] = [class_label(i + 1) for i in range(len(body.get("labels", [])))]
            db.execute("INSERT OR IGNORE INTO class_table(id, rev, version, built, dv, params, body) VALUES(1, 1, ?, ?, -1, '', ?)",
                       (old["version"], old["created"], json.dumps(body)))
    if "ln_ms" not in {c["name"] for c in db.execute("PRAGMA table_info(merged)")}:
        db.execute("ALTER TABLE merged ADD COLUMN ln_ms REAL")
    if "best" not in {c["name"] for c in db.execute("PRAGMA table_info(class_table)")}:
        db.execute("ALTER TABLE class_table ADD COLUMN best REAL")
    db.execute("CREATE INDEX IF NOT EXISTS merged_track_best ON merged (track, status, score_ms)")
    if not any(c["name"] == "added" for c in db.execute("PRAGMA table_info(cars)")):
        db.execute("ALTER TABLE cars ADD COLUMN added INTEGER")     # databases from before "new cars" was tracked
    # when a car first showed up: the earliest submission we still have for it, else its last update
    db.execute("UPDATE cars SET added=COALESCE((SELECT MIN(created) FROM submissions s WHERE s.car_hash=cars.car_hash), updated) "
               "WHERE added IS NULL")
    db.commit()
    todo = db.execute("SELECT car_hash, track, score_ms FROM merged WHERE ln_ms IS NULL").fetchall()
    if todo:   # an upgraded database: the log of every lap time is worked out once, before any request
        db.executemany("UPDATE merged SET ln_ms=? WHERE car_hash=? AND track=?", [(math.log(r["score_ms"]), r["car_hash"], r["track"]) for r in todo])
        db.commit()
    return db


class Cfg(dict):
    raw = None    # the stored JSON this config was read from (lets handlers cache what they derive from it)


def get_config(db):
    cfg = Cfg(DEFAULT_CONFIG)
    row = db.execute("SELECT v FROM meta WHERE k='config'").fetchone()
    if row:
        cfg.update(json.loads(row["v"]))
        cfg.raw = row["v"]
    return cfg


def set_config(db, cfg):
    stored = {k: cfg[k] for k in DEFAULT_CONFIG}
    db.execute("INSERT INTO meta(k, v) VALUES('config', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (json.dumps(stored),))
    db.commit()


def bump(db):
    """Data version counter: changes whenever merged data or class tables change (drives ETag and cache)."""
    db.execute("INSERT INTO meta(k, v) VALUES('dv', '1') ON CONFLICT(k) DO UPDATE SET v = CAST(v AS INTEGER) + 1")
    _class_wake.set()


def data_version(db):
    row = db.execute("SELECT v FROM meta WHERE k='dv'").fetchone()
    return int(row["v"]) if row else 0


CLASS_FORMAT = 3   # 3: borders and class averages are on the mean-ln-lap-time scale (see car_score); nothing is stored per car


class Table(dict):
    rev = 0     # grows each time the stored row is rewritten; the body can change without a new version


_table_cache = {}   # database path -> (rev, parsed table)


def latest_table(db):
    """The live class table, or None before there are enough rated cars. The returned dict is shared between
    requests: treat it as read-only."""
    row = db.execute("SELECT rev FROM class_table WHERE id=1").fetchone()
    if row is None:
        return None
    hit = _table_cache.get(db.path)
    if hit and hit[0] == row["rev"]:
        return hit[1]
    row = db.execute("SELECT rev, body FROM class_table WHERE id=1").fetchone()
    if row is None:
        return None
    table = Table(json.loads(row["body"]))
    table.rev = row["rev"]
    if table.get("format") != CLASS_FORMAT:   # a table from before the ratings were stored per car: rebuilt on start
        table = None
    _table_cache[db.path] = (row["rev"], table)
    return table


def clean_text(s):
    return "".join(ch for ch in s if ch.isprintable()).strip()


class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ------------------------------------------------------------------ validation

def check_env(env, cfg, track):
    canon = cfg.get("env")
    if not canon:
        return "server has no canonical environment configured yet"
    if not isinstance(env, dict):
        return "missing environment attestation (run the vanilla track check before benchmarking)"
    if env.get("profile") != cfg["profile"]:
        return "baseline profile %r is not accepted (server wants %r)" % (env.get("profile"), cfg["profile"])
    if env.get("ignore_ui"):
        return "environment was checked with UI files ignored"
    tid = track.partition("/")[0]
    want = (canon.get("tracks") or {}).get(tid)
    if not want:
        return "server has no reference digest for track %s yet" % tid
    if (env.get("tracks") or {}).get(tid) != want:
        return "track %s is not identical to the vanilla reference" % tid
    if env.get("acs") != canon.get("acs"):
        return "game executable (acs.exe) differs from the reference"
    return None


def clean_envs(envs):
    """Structural check of the environments map sent with a submission."""
    if not isinstance(envs, dict) or len(envs) > 50:
        raise HttpError(400, "environments must be an object with at most 50 entries")
    out = {}
    for k, v in envs.items():
        if not isinstance(k, str) or not ENV_ID.match(k) or not isinstance(v, dict):
            raise HttpError(400, "bad environment entry")
        tr = v.get("tracks")
        if not isinstance(tr, dict) or len(tr) > 20 or not all(isinstance(a, str) and isinstance(b, str)
                                                                for a, b in tr.items()):
            raise HttpError(400, "bad environment tracks")
        out[k] = {"profile": v.get("profile") if isinstance(v.get("profile"), str) else None,
                  "ignore_ui": bool(v.get("ignore_ui")), "tracks": tr,
                  "acs": v.get("acs") if isinstance(v.get("acs"), str) else None}
    return out


def plausible_bounds(cfg, refs, track):
    if refs and track in refs:
        return refs[track] * cfg["plaus_low"], refs[track] * cfg["plaus_high"]
    return cfg["plaus_min_ms"], cfg["plaus_max_ms"]


def validate_entry(e, cfg, envs, refs):
    """-> (normalized entry, None) or (None, reason). The score is always recomputed here."""
    if not isinstance(e, dict):
        return None, "entry is not an object"
    car_id, car_hash, track = e.get("car_id"), e.get("car_hash"), e.get("track")
    if not isinstance(car_id, str) or not CAR_ID.match(car_id):
        return None, "bad car_id"
    if not isinstance(car_hash, str) or not HEX64.match(car_hash):
        return None, "bad or missing car_hash"
    name = e.get("name", car_id)
    if not isinstance(name, str):
        return None, "bad name"
    name = clean_text(name)[:120] or car_id
    if track not in cfg["tracks"]:
        return None, "track is not part of the baseline"
    batch = e.get("batch")
    if not isinstance(batch, int) or isinstance(batch, bool) or not 1 <= batch <= 64:
        return None, "bad batch"
    if batch > cfg["max_batch"]:
        return None, "batch %d is above the allowed %d (cars shared the track)" % (batch, cfg["max_batch"])
    laps = e.get("laps_ms")
    if not isinstance(laps, list) or not cfg["laps"] <= len(laps) <= 12:
        return None, "laps_ms must hold at least %d laps" % cfg["laps"]
    for x in laps:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not 0 < x < 3600000:
            return None, "bad lap time"
    laps = [float(x) for x in laps[:cfg["laps"]]]
    timed = laps[1:]
    score = statistics.median(timed)
    spread = (max(timed) - min(timed)) / score * 100
    if spread > cfg["spread_limit_pct"]:
        return None, "timed laps too inconsistent (%.2f%% spread)" % spread
    lo, hi = plausible_bounds(cfg, refs, track)
    if not lo <= score <= hi:
        return None, "lap time outside the plausible range for this track"
    env_id = e.get("env")
    why = check_env(envs.get(env_id) if isinstance(env_id, str) else None, cfg, track)
    if why:
        return None, why
    return {"car_id": car_id, "car_hash": car_hash, "name": name, "track": track, "score_ms": score,
            "spread_pct": spread, "laps": laps, "batch": batch, "env_id": env_id}, None


# ------------------------------------------------------------------ merge

def car_score(times_ms, tracks):
    """A car's rating on one scale: the mean of ln(lap time) over the benchmark tracks. Nothing is stored for it:
    it is worked out from the car's own times whenever it is needed, and only differences between cars matter."""
    return sum(math.log(times_ms[t]) for t in tracks) / len(tracks)


def note_fastest(db, cfg, car_hashes):
    """The "is it the fastest" check of an upload: if any of these cars is rated and beats the stored fastest score,
    that score is lowered. (A rebuild of the class table sets it exactly again, so it can't stay wrong for long.)"""
    tracks = cfg["tracks"]
    hashes = sorted(set(car_hashes))
    per = {}
    for i in range(0, len(hashes), 500):
        part = hashes[i:i + 500]
        for r in db.execute("SELECT car_hash, track, score_ms, status FROM merged WHERE car_hash IN (%s)" % ",".join("?" * len(part)), part):
            per.setdefault(r["car_hash"], []).append((r["track"], r["score_ms"], r["status"]))
    best = None
    for rows in per.values():
        have = {t: ms for t, ms, _ in rows}
        if car_status([st for _, _, st in rows], have, tracks) in ("verified", "provisional"):
            sc = car_score(have, tracks)
            best = sc if best is None or sc < best else best
    if best is not None:
        db.execute("UPDATE class_table SET best=? WHERE id=1 AND (best IS NULL OR best > ?)", (best, best))


def recompute(db, cfg, car_hash, track):
    """Agreement between users for one car on one track. Updates submissions.used and merged; returns the new
    merged status (None when the car has no submissions left).

    Picks the biggest group of submissions that agree with each other (ties: the tightest, then the first row).
    Done on the sorted scores with binary search, O(n log n) instead of comparing every pair."""
    rows = db.execute(
        "SELECT s.id, s.score_ms, s.used, u.role FROM submissions s JOIN users u ON u.id = s.user_id "
        "WHERE s.car_hash=? AND s.track=? AND u.banned=0", (car_hash, track)).fetchall()
    if not rows:
        db.execute("DELETE FROM merged WHERE car_hash=? AND track=?", (car_hash, track))
        return None
    tol = cfg["tolerance_pct"] / 100
    ordered = sorted(rows, key=lambda r: r["score_ms"])
    scores = [r["score_ms"] for r in ordered]
    n = len(scores)
    best_key, best_span = None, (0, 0)
    for centre in rows:  # original row order, so ties resolve exactly as before
        c = centre["score_ms"]
        t_ = tol * c
        lo, hi = bisect.bisect_left(scores, c - t_), bisect.bisect_right(scores, c + t_)
        while lo > 0 and abs(scores[lo - 1] - c) <= t_:  # settle float rounding at the edges: exact same test as
            lo -= 1                                       # abs(score - c) <= tol * c
        while lo < hi and abs(scores[lo] - c) > t_:
            lo += 1
        while hi < n and abs(scores[hi] - c) <= t_:
            hi += 1
        while hi > lo and abs(scores[hi - 1] - c) > t_:
            hi -= 1
        key = (hi - lo, -(scores[hi - 1] - scores[lo]))
        if best_key is None or key > best_key:
            best_key, best_span = key, (lo, hi)
    cluster = ordered[best_span[0]:best_span[1]]
    used = {r["id"] for r in cluster}
    trusted = any(r["role"] == "trusted" for r in cluster)
    if len(cluster) >= cfg["min_confirmations"] or trusted:
        status = "verified"
    elif len(rows) == 1:
        status = "provisional"
    else:
        status = "disputed"
    cl_scores = [r["score_ms"] for r in cluster]
    median = statistics.median(cl_scores)
    spread = (max(cl_scores) - min(cl_scores)) / median * 100
    changed = [(1 if r["id"] in used else 0, r["id"]) for r in rows if r["used"] != (1 if r["id"] in used else 0)]
    if changed:
        db.executemany("UPDATE submissions SET used=? WHERE id=?", changed)
    db.execute(
        "INSERT INTO merged(car_hash, track, score_ms, n_used, n_total, spread_pct, status, updated, ln_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(car_hash, track) DO UPDATE SET score_ms=excluded.score_ms, "
        "n_used=excluded.n_used, n_total=excluded.n_total, spread_pct=excluded.spread_pct, "
        "status=excluded.status, updated=excluded.updated, ln_ms=excluded.ln_ms",
        (car_hash, track, median, len(cluster), len(rows), spread, status, now(), math.log(median)))
    return status


# ------------------------------------------------------------------ classes

_label = functools.lru_cache(maxsize=None)(class_label)


def _nearest_cut(cuts, y):
    """Index of the cut closest to y (the first one on an exact tie), by bisection: the cuts are sorted."""
    i = bisect.bisect_left(cuts, y)
    if i == 0:
        k = 0
    elif i == len(cuts):
        k = len(cuts) - 1
    else:
        k = i - 1 if abs(cuts[i - 1] - y) <= abs(cuts[i] - y) else i
    while k > 0 and cuts[k - 1] == cuts[k]:
        k -= 1
    return k


_live_cache = {}   # database path -> (data version, tracks, live reference)


def live_reference(db, tracks):
    """What the ratings are measured against right now: the fastest rated car's score (kept by the class table, lowered
    by uploads) and the fastest lap per track (one index lookup each)."""
    dv = data_version(db)
    hit = _live_cache.get(db.path)
    if hit and hit[0] == dv and hit[1] == tracks:
        return hit[2]
    row = db.execute("SELECT best FROM class_table WHERE id=1").fetchone()
    refs = {}
    for t in tracks:
        v = db.execute("SELECT MIN(score_ms) FROM merged WHERE track=? AND status IN ('verified','provisional')", (t,)).fetchone()[0]
        if v is not None:
            refs[t] = v
    live = {"base": row["best"] if row else None, "refs": refs}
    _live_cache[db.path] = (dv, list(tracks), live)
    return live


def classify(table, times_ms, live):
    """times_ms: {track: score_ms}. The class is a lookup of the car's score among the class table's borders; the
    numbers shown (gap, per-track deltas) are measured from the fastest car and lap times right now."""
    tracks = table["tracks"]
    score = car_score(times_ms, tracks)
    cuts = table["cuts"]
    label = _label(bisect.bisect_right(cuts, score) + 1)
    base = live["base"] if live["base"] is not None else score
    gap = max(0.0, math.expm1(score - base) * 100)
    deltas = {t: max(0.0, (times_ms[t] / live["refs"].get(t, times_ms[t]) - 1) * 100) for t in tracks}
    # balance: how this car's lap times sit on each track compared with its class's average, with the overall level
    # taken out. A slow car that is slow in the usual way for its class has a balance of 0 however far behind it is.
    norms = table["norms"].get(label, {})
    off = [math.log(times_ms[t]) - norms[t] for t in tracks if t in norms]       # > 0: slower here than the class usually is
    variance = math.expm1(max(off) - min(off)) * 100 if len(off) == len(tracks) else 0.0
    flags, bop = [], ""
    if cuts:
        k = _nearest_cut(cuts, score)
        dist = math.expm1(abs(cuts[k] - score)) * 100
        if dist < table["tie_zone"]:
            flags.append("borderline %s/%s (%.2f%%)" % (_label(k + 1), _label(k + 2), dist))
    if variance > table.get("variance_limit", table["class_gap"]):
        flags.append("high variance (%.1f%%)" % variance)
    bits = []
    for t in tracks:
        if t in norms:
            faster = -(math.log(times_ms[t]) - norms[t])   # > 0: this car is quicker here than its class usually is
            if faster * 100 > 0.25 * table["class_gap"]:
                bits.append("%s: needs to be %.2f%% slower" % (t.partition("/")[0], math.expm1(faster) * 100))
    bop = "; ".join(bits)
    return {"class": label, "score_pct": gap, "gap_pct": gap, "deltas": deltas, "variance_pct": variance,
            "flags": flags, "bop": bop}


_class_wake = threading.Event()   # set whenever results change; wakes this process's class refresher


def _class_params(cfg):
    return json.dumps([CLASS_FORMAT, cfg["profile"], cfg["tracks"], cfg["class_gap"], cfg["min_class_size"], cfg["tie_zone"],
                       bool(cfg["include_provisional"]), cfg["variance_limit_pct"]])


def class_table_stale(db, cfg):
    row = db.execute("SELECT dv, params FROM class_table WHERE id=1").fetchone()
    return row is None or row["dv"] != data_version(db) or row["params"] != _class_params(cfg)


BUCKETS_PER_CLASS = 20   # score buckets per class width: borders are found to within 1/20 of a class


def build_class_table(db, cfg):
    """Work the class borders out of the time table. The database averages each car's ln lap times and counts the
    cars into small score buckets (grouped queries, nothing is kept per car); the border search then runs on the
    buckets, so its time depends on how wide the field is, not on how many cars there are. -> (table body, fastest
    score), or None when fewer than two cars are rated."""
    tracks = cfg["tracks"]
    tm = ",".join("?" * len(tracks))
    # one row per car that is rated: results on every track, none disputed (and none provisional unless asked for)
    cars_sql = ("SELECT car_hash, AVG(CASE WHEN track IN (%s) THEN ln_ms END) AS s, SUM(track IN (%s)) AS n, "
                "SUM(status='disputed') AS d, SUM(status='provisional') AS p FROM merged GROUP BY car_hash" % (tm, tm))
    db.execute("DROP TABLE IF EXISTS temp.rated")   # scratch copy for this build (the connection's own temp database), so the laps are only scanned once
    db.execute("CREATE TEMP TABLE rated AS SELECT car_hash, s, p FROM (%s) WHERE n=? AND d=0" % cars_sql, tuple(tracks) * 2 + (len(tracks),))
    eligible = "SELECT car_hash, s FROM temp.rated%s" % ("" if cfg["include_provisional"] else " WHERE p=0")
    n, low = db.execute("SELECT COUNT(*), MIN(s) FROM (%s)" % eligible).fetchone()
    if n < 2:
        return None
    best = db.execute("SELECT MIN(s) FROM temp.rated").fetchone()[0]
    width = math.log1p(cfg["class_gap"] / 100)
    step = width / BUCKETS_PER_CLASS
    rows = db.execute("SELECT CAST((s - ?) / ? AS INTEGER) AS b, COUNT(*), AVG(s), MIN(s), MAX(s) FROM (%s) GROUP BY b ORDER BY b" % eligible,
                      (low, step)).fetchall()
    ids, cnt, pos = [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]
    lo_s, hi_s = [r[3] for r in rows], [r[4] for r in rows]
    classes = ac_rating.class_borders(pos, cnt, cfg["class_gap"], cfg["min_class_size"])
    labels = [class_label(i + 1) for i in range(len(classes))]
    cuts = [(hi_s[a_end] + lo_s[b_start]) / 2 for (_, a_end), (b_start, _) in zip(classes, classes[1:])]
    class_of = {}
    for ci, (a_, b_) in enumerate(classes):
        for bi in range(a_, b_ + 1):
            class_of[ids[bi]] = labels[ci]
    sums = {}
    for b_id, track, total, count in db.execute(
            "SELECT x.b, m.track, SUM(m.ln_ms), COUNT(*) FROM (SELECT car_hash, CAST((s - ?) / ? AS INTEGER) AS b FROM (%s)) x "
            "JOIN merged m ON m.car_hash = x.car_hash WHERE m.track IN (%s) GROUP BY x.b, m.track" % (eligible, tm),
            (low, step) + tuple(tracks)):
        if b_id in class_of and total is not None:
            acc = sums.setdefault(class_of[b_id], {}).setdefault(track, [0.0, 0])
            acc[0] += total
            acc[1] += count
    norms = {lab: {t: v[0] / v[1] for t, v in d.items()} for lab, d in sums.items()}
    summary = []
    for ci, (a_, b_) in enumerate(classes):
        lo, hi = lo_s[a_], hi_s[b_]
        summary.append({"label": labels[ci], "n": sum(cnt[a_:b_ + 1]), "lo": math.expm1(lo - best) * 100,
                        "hi": math.expm1(hi - best) * 100, "width": math.expm1(hi - lo) * 100})
    refs = {}
    for t in tracks:
        v = db.execute("SELECT MIN(score_ms) FROM merged WHERE track=? AND status IN ('verified','provisional')", (t,)).fetchone()[0]
        if v is not None:
            refs[t] = v
    body = {"format": CLASS_FORMAT, "profile": cfg["profile"], "tracks": tracks, "refs_ms": refs, "cuts": cuts, "labels": labels,
            "norms": norms, "class_gap": cfg["class_gap"], "tie_zone": cfg["tie_zone"], "variance_limit": cfg["variance_limit_pct"],
            "n_cars": n, "classes": summary,
            "include_provisional": bool(cfg["include_provisional"])}
    return body, best


def refresh_classes(db, cfg, force=False):
    """Bring the class table up to date with the time table. -> (table, rebuilt). The version number only goes up
    when the borders or labels actually change."""
    row = db.execute("SELECT rev, dv, params, body FROM class_table WHERE id=1").fetchone()
    dv, params = data_version(db), _class_params(cfg)   # read before the data, so a change in between leaves it stale
    if row and not force and row["dv"] == dv and row["params"] == params:
        return latest_table(db), False
    prev = json.loads(row["body"]) if row else None
    db.execute("BEGIN")          # one snapshot for all the queries of this build
    try:
        built = build_class_table(db, cfg)
    finally:
        db.rollback()
    if built is None:
        return latest_table(db), False
    body, best = built
    same = prev is not None and all(prev.get(k) == body[k] for k in ("format", "profile", "tracks", "cuts", "labels"))
    version = (prev["version"] if same else prev["version"] + 1) if prev else 1
    body["version"], body["created"] = version, now()
    if row:
        # compare-and-set: another worker may have rebuilt in the meantime, then theirs stands
        cur = db.execute("UPDATE class_table SET rev=rev+1, version=?, built=?, dv=?, params=?, body=?, best=? WHERE id=1 AND rev=?",
                         (version, body["created"], dv, params, json.dumps(body), best, row["rev"]))
        if cur.rowcount == 0:
            db.rollback()
            return latest_table(db), False
    else:
        db.execute("INSERT OR IGNORE INTO class_table(id, rev, version, built, dv, params, body, best) VALUES(1, 1, ?, ?, ?, ?, ?, ?)",
                   (version, body["created"], dv, params, json.dumps(body), best))
    db.commit()
    return latest_table(db), True


_refreshers = set()


def start_refresher(db_path):
    """Background thread that keeps the class table current: it wakes when results change here, and also looks every
    few seconds, so changes made by another worker or the command line are picked up too."""
    if db_path in _refreshers or os.environ.get("ACB_NO_REFRESH"):
        return
    _refreshers.add(db_path)
    threading.Thread(target=_refresh_loop, args=(db_path,), name="class-refresh", daemon=True).start()


def _refresh_loop(db_path, poll=5.0):
    last, cost = None, 0.0   # when the table was last rebuilt here and how long that took; the first rebuild does not wait
    while True:
        try:
            db = request_db(db_path)
            cfg = get_config(db)
            if class_table_stale(db, cfg):
                if last is not None:   # wait at least the configured time, and five times a rebuild's duration so a big table never hogs the CPU
                    time.sleep(max(0.0, last + max(max(1, int(cfg["class_refresh_seconds"])), 5 * cost) - time.monotonic()))
                last, cpu = time.monotonic(), time.thread_time()
                refresh_classes(db, cfg)
                cost = time.thread_time() - cpu   # CPU time, so a busy server does not stretch the wait
        except Exception as e:   # the database may be busy or gone; try again on the next round
            sys.stderr.write("class refresh failed: %r\n" % (e,))
            _local.conns = {}
        if _class_wake.wait(poll):
            time.sleep(0.3)      # let the request that woke us finish its commit
            _class_wake.clear()


def car_status(statuses, have, tracks):
    """One car's status from its per-track merge statuses; `have` is the set of tracks it has a result for."""
    if "disputed" in statuses:
        return "disputed"
    if any(t not in have for t in tracks):
        return "incomplete"
    if "provisional" in statuses:
        return "provisional"
    return "verified"


def car_counts(db, cfg):
    """How many cars are in each status (cars, not track results: one car has a result per track)."""
    per = {}
    for h, track, status in db.execute("SELECT car_hash, track, status FROM merged"):
        sts, have = per.setdefault(h, ([], set()))
        sts.append(status)
        have.add(track)
    counts = {"verified": 0, "provisional": 0, "incomplete": 0, "disputed": 0}
    for sts, have in per.values():
        counts[car_status(sts, have, cfg["tracks"])] += 1
    return counts


def car_records(db, cfg, only_hash=None, only_hashes=None):
    table = latest_table(db)
    q = "SELECT car_hash, track, score_ms, n_used, status, updated FROM merged"
    args = ()
    names_q, names_args = "SELECT car_hash, car_id, name, added FROM cars", ()
    if only_hashes is not None:
        only_hashes = sorted(set(only_hashes))
        marks = ",".join("?" * len(only_hashes))
        q, args = q + " WHERE car_hash IN (%s)" % marks, tuple(only_hashes)
        names_q, names_args = names_q + " WHERE car_hash IN (%s)" % marks, tuple(only_hashes)
    elif only_hash:
        q, args = q + " WHERE car_hash=?", (only_hash,)
        names_q, names_args = names_q + " WHERE car_hash=?", (only_hash,)
    per_car = {}
    cur = db.cursor()
    cur.row_factory = None   # plain tuples: this loop runs once per car and per track
    for h, track, score_ms, n_used, status, updated in cur.execute(q, args):
        per_car.setdefault(h, {})[track] = (score_ms, n_used, status, updated)
    names = {h: (car_id, name, added) for h, car_id, name, added in cur.execute(names_q, names_args)}
    tracks = cfg["tracks"]
    classed = table["tracks"] if table else None
    live = live_reference(db, tracks) if table else None
    out = []
    for h, per in sorted(per_car.items()):
        status = car_status([v[2] for v in per.values()], per, tracks)
        car_id, name, added = names.get(h, ("", "", None))
        rec = {"car_hash": h, "car_id": car_id, "name": name,
               "status": status, "times_ms": {t: v[0] for t, v in per.items()},
               "n": {t: v[1] for t, v in per.items()}, "updated": max(v[3] for v in per.values()), "added": added,
               "class": None}
        if table and status in ("verified", "provisional"):
            rec.update(classify(table, {t: per[t][0] for t in classed}, live))
        out.append(rec)
    return table, out


# ------------------------------------------------------------------ http layer

def respond(code, obj, extra=None):
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
               ("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")] + (extra or [])
    return code, body, headers


def accepts_gzip(environ):
    ae = environ.get("HTTP_ACCEPT_ENCODING", "")
    return "gzip" in ae and not re.search(r"gzip\s*;\s*q=0(\.0*)?\s*(,|$)", ae)


def etag_matches(environ, etag):
    inm = environ.get("HTTP_IF_NONE_MATCH")
    if not inm:
        return False
    return inm.strip() == "*" or etag in [t.strip()[2:] if t.strip().startswith("W/") else t.strip() for t in inm.split(",")]


_file_cache = {}   # file name -> (mtime_ns, size, body, gzipped body, etag)


def serve_file(path, content_type, environ=None, compress=True):
    """Static page, read from disk once and again only when the file changes; revalidated with an ETag (304).
    Pass compress=False for files that are already compressed, such as images."""
    environ = environ or {}
    p = HERE / path
    try:
        st = p.stat()
    except OSError:
        raise HttpError(404, "not found")
    hit = _file_cache.get(path)
    if not hit or hit[0] != st.st_mtime_ns or hit[1] != st.st_size:
        body = p.read_bytes()
        hit = (st.st_mtime_ns, st.st_size, body, gzip.compress(body, 9, mtime=0), '"f-%s"' % sha(body.decode("utf-8", "replace"))[:16])
        _file_cache[path] = hit
    _, _, body, gz, etag = hit
    cache = [("ETag", etag), ("Cache-Control", "no-cache"), ("Vary", "Accept-Encoding")]
    if etag_matches(environ, etag):
        return 304, b"", cache
    enc = []
    if compress and accepts_gzip(environ):
        body, enc = gz, [("Content-Encoding", "gzip")]
    return 200, body, [("Content-Type", content_type), ("Content-Length", str(len(body))),
                       ("X-Content-Type-Options", "nosniff")] + enc + cache


def client_ip(environ, cfg):
    if cfg["trust_proxy_header"]:
        fwd = environ.get("HTTP_X_FORWARDED_FOR", "")
        if fwd:  # the LAST address is the one your own proxy added; earlier ones are client-controlled
            return fwd.split(",")[-1].strip()[:64]
    return environ.get("REMOTE_ADDR", "?")[:64]


def read_json(environ, cfg):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        raise HttpError(400, "bad Content-Length")
    if length <= 0:
        raise HttpError(400, "empty body")
    if length > cfg["max_body_bytes"]:
        raise HttpError(413, "body too large")
    raw = environ["wsgi.input"].read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HttpError(400, "body is not valid JSON")
    if not isinstance(data, dict):
        raise HttpError(400, "body must be a JSON object")
    return data


def authenticate(db, environ):
    header = environ.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer ") or len(header) > 200:
        raise HttpError(401, "missing API key")
    kh = sha(header[7:].strip())
    user = db.execute("SELECT id, name, role, banned FROM users WHERE key_hash=?", (kh,)).fetchone()
    if not user:   # a key issued to another device of the same account
        user = db.execute("SELECT u.id, u.name, u.role, u.banned FROM user_keys k JOIN users u ON u.id=k.user_id "
                          "WHERE k.key_hash=?", (kh,)).fetchone()
    if not user:
        raise HttpError(401, "unknown API key")
    if user["banned"]:
        raise HttpError(403, "this account is banned")
    return user


def rate_limit(db, who, kind, limit, window=3600):
    t = now()
    n = db.execute("SELECT COUNT(*) AS n FROM events WHERE who=? AND kind=? AND ts>?", (who, kind, t - window)).fetchone()["n"]
    if n >= limit:
        raise HttpError(429, "rate limit reached, try again later")
    db.execute("INSERT INTO events(ts, who, kind) VALUES(?,?,?)", (t, who, kind))
    if secrets.randbelow(50) == 0:
        db.execute("DELETE FROM events WHERE ts<?", (t - 86400,))
    db.commit()


def public_config(cfg, db):
    table = latest_table(db)
    return {"api": API_VERSION, "profile": cfg["profile"], "tracks": cfg["tracks"], "laps": cfg["laps"],
            "max_batch": cfg["max_batch"], "min_confirmations": cfg["min_confirmations"],
            "tolerance_pct": cfg["tolerance_pct"], "env": cfg["env"],
            "class_table_version": table["version"] if table else None}


def conditions_doc(cfg):
    """Everything a client needs to run a benchmark that the server will accept. `version` changes whenever
    any of it changes, so a client can cache the document and revalidate cheaply (ETag / If-None-Match)."""
    doc = {"format": 1, "profile": cfg["profile"], "tracks": cfg["tracks"], "laps": cfg["laps"],
           "timed_laps": cfg["laps"] - 1, "max_batch": cfg["max_batch"], "max_entries": cfg["max_entries"],
           "conditions": cfg.get("conditions") or {}, "env": cfg["env"], "accepting_submissions": bool(cfg["env"])}
    doc["version"] = sha(json.dumps(doc, sort_keys=True, separators=(",", ":")))[:16]
    return doc


_conditions_cache = (None, None, None)   # (stored config JSON, etag, body)


def handle_conditions(cfg, environ):
    global _conditions_cache
    raw = getattr(cfg, "raw", None)
    cached = _conditions_cache
    if raw is not None and cached[0] == raw:
        _, etag, body = cached
    else:
        doc = conditions_doc(cfg)
        etag = '"c-%s"' % doc["version"]
        body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        if raw is not None:
            _conditions_cache = (raw, etag, body)
    if etag_matches(environ, etag):
        return 304, b"", [("ETag", etag)]
    return 200, body, [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                       ("X-Content-Type-Options", "nosniff"), ("ETag", etag), ("Cache-Control", "no-cache")]


INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no I, L, O, 0, 1: nothing to mistake when read or typed


class MemoryLimiter:
    """Sliding-window limiter kept in this process (no database write per request). Used for the public, read-only
    lookup endpoint; with several workers each keeps its own window, so the effective limit is up to workers x limit."""
    MAX_KEYS = 100000

    def __init__(self):
        self.lock, self.hits, self.calls = threading.Lock(), {}, 0

    def check(self, who, limit, window=3600):
        t = time.monotonic()
        with self.lock:
            self.calls += 1
            if self.calls % 2000 == 0 or len(self.hits) > self.MAX_KEYS:
                for k in [k for k, q in self.hits.items() if not q or q[-1] <= t - window]:
                    del self.hits[k]
                if len(self.hits) > self.MAX_KEYS:
                    self.hits.clear()
            q = self.hits.get(who)
            if q is None:
                q = self.hits[who] = collections.deque()
            while q and q[0] <= t - window:
                q.popleft()
            if len(q) >= limit:
                raise HttpError(429, "rate limit reached, try again later")
            q.append(t)


_lookup_limiter = MemoryLimiter()


def new_invite_code():
    """16 characters shown as XXXX-XXXX-XXXX-XXXX. No '-' inside a group and no '_': a double-click selects a whole
    group, and the code can be typed in lowercase, with or without the hyphens."""
    raw = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def invite_hash(code):
    """What is stored for an invite: the hash of its normalised form (no hyphens or spaces, upper case)."""
    return sha(re.sub(r"[-\s]", "", code).upper())


def find_invite(db, text):
    """Look an invite up from whatever the person pasted or typed. Tolerates surrounding spaces/newlines/quotes,
    zero-width characters from copying, lowercase and missing hyphens; invites made by older versions (random
    mixed-case codes containing - and _) are still found by their exact text. -> (code_hash, row) or (None, None)."""
    compact = re.sub(r"[\s\u200b-\u200d\u2060\ufeff]+", "", text or "").strip("\"'`<>")
    for cand in dict.fromkeys([text.strip(), compact, re.sub(r"[-\s]", "", compact).upper()]):
        h = sha(cand)
        row = db.execute("SELECT role, uses_left FROM invites WHERE code_hash=?", (h,)).fetchone()
        if row:
            return h, row
    return None, None


def handle_register(db, cfg, environ):
    data = read_json(environ, cfg)
    rate_limit(db, "ip:" + client_ip(environ, cfg), "register", cfg["rate_register_per_hour"])
    invite, name = data.get("invite"), data.get("name")
    if not isinstance(invite, str) or not isinstance(name, str) or not USER_NAME.match(name):
        raise HttpError(400, "invite and a name of 3-40 letters, digits, . _ - are required")
    code_hash, row = find_invite(db, invite)
    if not row:
        raise HttpError(403, "unknown invite code: check that it was copied completely and that this is the same "
                             "server the invite was created on")
    if row["uses_left"] <= 0:
        raise HttpError(403, "this invite code has been used up or revoked")
    key = "acb_" + secrets.token_urlsafe(32)
    try:
        db.execute("INSERT INTO users(name, key_hash, role, created) VALUES(?,?,?,?)", (name, sha(key), row["role"], now()))
    except sqlite3.IntegrityError:
        raise HttpError(409, "that name is taken")
    db.execute("UPDATE invites SET uses_left = uses_left - 1 WHERE code_hash=?", (code_hash,))
    db.commit()
    return respond(201, {"api_key": key, "name": name, "role": row["role"]})


LINK_TTL = 600   # seconds a device link code stays valid


def handle_link(db, cfg, environ):
    """Signed-in device asks for a code that lets ANOTHER device sign in as the same account (single use, 10 minutes)."""
    user = authenticate(db, environ)
    rate_limit(db, "user:%d" % user["id"], "link", 10)
    raw = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(12))
    code = "-".join(raw[i:i + 4] for i in range(0, 12, 4))
    t = now()
    db.execute("DELETE FROM device_links WHERE created<? OR user_id=?", (t - LINK_TTL, user["id"]))   # only the newest code works
    db.execute("INSERT INTO device_links(code_hash, user_id, created) VALUES(?,?,?)", (invite_hash(code), user["id"], t))
    db.commit()
    return respond(201, {"code": code, "expires_in": LINK_TTL, "name": user["name"]})


def handle_redeem(db, cfg, environ):
    """New device presents a link code and receives its own API key for the same account (no API key needed yet)."""
    data = read_json(environ, cfg)
    rate_limit(db, "ip:" + client_ip(environ, cfg), "redeem", cfg["rate_register_per_hour"])
    code, label = data.get("code"), data.get("label", "")
    if not isinstance(code, str) or not isinstance(label, str):
        raise HttpError(400, "code is required")
    h = sha(re.sub(r"[-\s\u200b-\u200d\u2060\ufeff]+", "", code).strip("\"'`<>").upper())
    row = db.execute("SELECT l.user_id, l.created, u.name, u.role, u.banned FROM device_links l "
                     "JOIN users u ON u.id=l.user_id WHERE l.code_hash=?", (h,)).fetchone()
    if not row or now() - row["created"] > LINK_TTL:
        raise HttpError(403, "unknown or expired link code: create a new one on the signed-in device "
                             "(codes work once and for %d minutes)" % (LINK_TTL // 60))
    if row["banned"]:
        raise HttpError(403, "this account is banned")
    db.execute("DELETE FROM device_links WHERE code_hash=?", (h,))
    key = "acb_" + secrets.token_urlsafe(32)
    db.execute("INSERT INTO user_keys(key_hash, user_id, label, created) VALUES(?,?,?,?)",
               (sha(key), row["user_id"], label.strip()[:40], now()))
    db.commit()
    return respond(201, {"api_key": key, "name": row["name"], "role": row["role"]})


def handle_submit(db, cfg, environ):
    user = authenticate(db, environ)
    rate_limit(db, "user:%d" % user["id"], "submit", cfg["rate_submits_per_hour"])
    data = read_json(environ, cfg)
    if data.get("profile") != cfg["profile"]:
        raise HttpError(422, "baseline profile %r is not accepted (server wants %r)" % (data.get("profile"), cfg["profile"]))
    if not cfg.get("env"):
        raise HttpError(503, "server is not configured yet (no canonical environment)")
    entries = data.get("results")
    if not isinstance(entries, list) or not entries:
        raise HttpError(400, "results must be a non-empty list")
    if len(entries) > cfg["max_entries"]:
        raise HttpError(413, "at most %d results per upload" % cfg["max_entries"])
    envs = clean_envs(data.get("environments", {}))
    table = latest_table(db)
    refs = table["refs_ms"] if table else None
    # validate everything first: it only reads the request, so it must not run while the write lock is held
    valid, rejected = [], []
    for i, e in enumerate(entries):
        norm, why = validate_entry(e, cfg, envs, refs)
        if why:
            if len(rejected) < 100:
                rejected.append({"index": i, "car_id": e.get("car_id") if isinstance(e, dict) else None,
                                 "track": e.get("track") if isinstance(e, dict) else None, "reason": why})
            continue
        valid.append(norm)
    accepted, touched, status = len(valid), set(), {}
    if valid:
        t = now()
        db.executemany(
            "INSERT INTO submissions(user_id, car_hash, car_id, name, track, score_ms, spread_pct, laps_json, batch, env_id, used, created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(user_id, car_hash, track) DO UPDATE SET "
            "car_id=excluded.car_id, name=excluded.name, score_ms=excluded.score_ms, spread_pct=excluded.spread_pct, "
            "laps_json=excluded.laps_json, batch=excluded.batch, env_id=excluded.env_id, created=excluded.created",
            [(user["id"], n["car_hash"], n["car_id"], n["name"], n["track"], n["score_ms"], n["spread_pct"],
              json.dumps(n["laps"]), n["batch"], n["env_id"], t) for n in valid])
        db.executemany(
            "INSERT INTO cars(car_hash, car_id, name, updated, added) VALUES(?,?,?,?,?) ON CONFLICT(car_hash) DO UPDATE SET "
            "car_id=excluded.car_id, name=excluded.name, updated=excluded.updated",
            [(n["car_hash"], n["car_id"], n["name"], t, t) for n in valid])
        touched = {(n["car_hash"], n["track"]) for n in valid}
        for h, tr in touched:
            key = recompute(db, cfg, h, tr) or "none"
            status[key] = status.get(key, 0) + 1
        note_fastest(db, cfg, [h for h, _ in touched])
        bump(db)
        db.commit()
    # the server decides the class: answer with the current record (class, gap, status, flags) of every car just touched
    cars = []
    if touched:
        if latest_table(db) is None:
            refresh_classes(db, cfg)   # the very first table: no reason to make the uploader wait for the refresher
        table, cars = car_records(db, cfg, only_hashes={h for h, _ in touched})
    return respond(200, {"accepted": accepted, "rejected": rejected, "rejected_total": len(entries) - accepted,
                         "merged_status": status, "cars": cars, "tracks": cfg["tracks"],
                         "table_version": table["version"] if touched and table else None})


_export = None   # (etag, body, gzipped body, data version, table version, built at): replaced as a whole, never edited
_export_lock = threading.Lock()


def _export_fresh(c, tv, dv, ttl):
    return c is not None and c[4] == tv and (c[3] == dv or time.time() - c[5] < ttl)


def _build_export(db, cfg, table, tv, dv):
    global _export
    _, cars = car_records(db, cfg)
    payload = json.dumps({"format": 1, "profile": cfg["profile"], "tracks": cfg["tracks"],
                          "table": table, "cars": cars}, separators=(",", ":"))
    # the etag covers the content only (not the timestamp), so it is the same on every worker
    body = (payload[:-1] + ',"generated":%d}' % now()).encode("utf-8")
    _export = ('"%d-%s"' % (tv, sha(payload)[:16]), body, gzip.compress(body, 6, mtime=0), dv, tv, time.time())


def handle_classes(db, cfg, environ):
    table = latest_table(db)
    tv, dv, ttl = (table.rev if table else 0), data_version(db), max(0, int(cfg.get("export_max_age", 0)))
    c = _export
    if not _export_fresh(c, tv, dv, ttl):
        # one thread rebuilds. Others keep getting the previous export while it runs (it is at most a rebuild old),
        # unless it belongs to an older class table or export_max_age is 0 (always fresh): then they wait for it.
        if _export_lock.acquire(blocking=False):
            got = True
        elif c is not None and ttl and c[4] == tv:
            got = False
        else:
            _export_lock.acquire()
            got = True
        if got:
            try:
                if not _export_fresh(_export, tv, dv, ttl):
                    _build_export(db, cfg, table, tv, dv)
            finally:
                _export_lock.release()
            c = _export
    etag, body, gz = c[0], c[1], c[2]
    cache = "public, max-age=%d" % ttl if ttl else "no-cache"
    base = [("ETag", etag), ("Cache-Control", cache), ("Vary", "Accept-Encoding")]
    if etag_matches(environ, etag):
        return 304, b"", base
    enc = []
    if accepts_gzip(environ):
        body, enc = gz, [("Content-Encoding", "gzip")]
    return 200, body, [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                       ("X-Content-Type-Options", "nosniff")] + enc + base


MAX_LOOKUP = 500


def handle_lookup(db, cfg, environ):
    """Live data for just the cars a client has installed: POST {"hashes": [car physics hashes]} (public, like
    /v1/classes, but only the requested cars are returned and nothing is cached, so the data is always current).
    Cars the server knows nothing about are simply absent from "cars"."""
    _lookup_limiter.check("ip:" + client_ip(environ, cfg), cfg["rate_lookups_per_hour"])
    data = read_json(environ, cfg)
    hashes = data.get("hashes")
    if (not isinstance(hashes, list) or not 1 <= len(hashes) <= MAX_LOOKUP
            or not all(isinstance(h, str) and HEX64.match(h) for h in hashes)):
        raise HttpError(400, "hashes must be a list of 1-%d lowercase sha256 hex strings" % MAX_LOOKUP)
    table, cars = car_records(db, cfg, only_hashes=hashes)
    return respond(200, {"format": 1, "profile": cfg["profile"], "tracks": cfg["tracks"],
                         "table_version": table["version"] if table else None,
                         "labels": table["labels"] if table else [], "cars": cars, "generated": now()})


def _admin_ctx():
    """The server functions admin.py needs (injected so admin.py never imports this module)."""
    return SimpleNamespace(HttpError=HttpError, respond=respond, read_json=read_json, recompute=recompute,
                           bump=bump, refresh_classes=refresh_classes, set_config=set_config,
                           latest_table=latest_table, car_counts=car_counts, parse_env=parse_env, apply_env=apply_env,
                           new_invite_code=new_invite_code, invite_hash=invite_hash)


BRANDING_FILES = {"/favicon.ico": ("favicon.ico", "image/x-icon"), "/logo.png": ("logo.png", "image/png")}


def route(environ, db_path):
    method, path = environ["REQUEST_METHOD"], environ.get("PATH_INFO", "")
    if method == "GET" and path in ("/", "/stats"):
        return serve_file("stats.html", "text/html; charset=utf-8", environ)
    if method == "GET" and path in BRANDING_FILES:      # the logo and the tab icon
        name, kind = BRANDING_FILES[path]
        return serve_file("branding/" + name, kind, environ, compress=False)
    db = request_db(db_path)
    try:
        if method == "GET" and path == "/v1/health":
            db.execute("SELECT 1").fetchone()   # health still means "the database answers"
            return respond(200, {"ok": True, "api": API_VERSION, "time": now()})
        cfg = get_config(db)
        if path == "/admin" or path.startswith("/admin/"):
            return admin.handle(environ, db, cfg, _admin_ctx())
        if method == "GET" and path == "/v1/conditions":
            return handle_conditions(cfg, environ)
        if method == "GET" and path == "/v1/config":
            return respond(200, public_config(cfg, db))
        if method == "POST" and path == "/v1/register":
            return handle_register(db, cfg, environ)
        if method == "POST" and path == "/v1/devices/link":
            return handle_link(db, cfg, environ)
        if method == "POST" and path == "/v1/devices/redeem":
            return handle_redeem(db, cfg, environ)
        if method == "POST" and path == "/v1/submit":
            return handle_submit(db, cfg, environ)
        if method == "GET" and path == "/v1/classes":
            return handle_classes(db, cfg, environ)
        if method == "POST" and path == "/v1/cars/lookup":
            return handle_lookup(db, cfg, environ)
        m = re.match(r"^/v1/cars/([0-9a-f]{64})$", path)
        if method == "GET" and m:
            table, cars = car_records(db, cfg, m.group(1))
            if not cars:
                raise HttpError(404, "unknown car")
            return respond(200, {"car": cars[0], "table_version": table["version"] if table else None})
        raise HttpError(404, "not found")
    finally:
        if db.in_transaction:   # a failed request must not leave a transaction (and its write lock) open on a reused connection
            db.rollback()


def create_app(db_path):
    init_db(db_path).close()
    start_refresher(db_path)

    def app(environ, start_response):
        try:
            code, body, headers = route(environ, db_path)
        except HttpError as e:
            code, body, headers = respond(e.code, {"error": e.msg})
        except Exception as e:  # never leak internals to the client
            sys.stderr.write("internal error: %r\n" % (e,))
            code, body, headers = respond(500, {"error": "internal error"})
        start_response("%d %s" % (code, HTTPStatus(code).phrase), headers)
        return [body]

    return app


_app = None


def application(environ, start_response):
    """WSGI entry point for gunicorn / waitress: gunicorn -w 2 server:application (DB path: ACB_DB)."""
    global _app
    if _app is None:
        _app = create_app(os.environ.get("ACB_DB", "acbench.db"))
    return _app(environ, start_response)


# ------------------------------------------------------------------ admin CLI

def cmd_serve(args):
    class Threaded(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    class Quiet(WSGIRequestHandler):
        def log_message(self, fmt, *a):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    srv = make_server(args.host, args.port, create_app(args.db), server_class=Threaded, handler_class=Quiet)
    print("serving on http://%s:%d (use a TLS reverse proxy in front of this)" % (args.host, args.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


TRACK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,59}$")   # a track folder name (no layout, no path parts)


def parse_env(data):
    """Pull a canonical environment out of a track reference (as made by the desktop app's `create`), a bare
    {"tracks": ..., "acs": ...} object, or a track_check_log.json list (latest passed check wins).
    -> {"profile": str|None, "tracks": {track_id: sha256}, "acs": sha256}. Raises ValueError with a readable reason."""
    env = None
    if isinstance(data, list):
        for entry in reversed(data):
            if isinstance(entry, dict) and entry.get("status") == "ok" and entry.get("env"):
                env = entry["env"]
                break
    elif isinstance(data, dict):
        env = data.get("env", data)
    if not isinstance(env, dict) or not env.get("tracks") or not env.get("acs"):
        raise ValueError("No usable environment (track digests + acs) found in that data.")
    tracks, acs = env["tracks"], env["acs"]
    if not isinstance(tracks, dict) or not 1 <= len(tracks) <= 20:
        raise ValueError("tracks must be an object with 1-20 entries (track folder -> sha256).")
    for t, dg in tracks.items():
        if not isinstance(t, str) or not TRACK_ID.match(t):
            raise ValueError("bad track folder name: %r" % (t,))
        if not isinstance(dg, str) or not HEX64.match(dg):
            raise ValueError("digest for %s is not a lowercase sha256 hex string" % t)
    if not isinstance(acs, str) or not HEX64.match(acs):
        raise ValueError("acs is not a lowercase sha256 hex string")
    if env.get("ignore_ui"):
        raise ValueError("this reference was made with UI files ignored; the server refuses such environments. "
                         "Create it again from the whole track folders.")
    profile = env.get("profile")
    return {"profile": profile if isinstance(profile, str) else None, "tracks": dict(tracks), "acs": acs}


def apply_env(db, cfg, env):
    """Make `env` (from parse_env) the canonical environment. -> list of warnings."""
    warnings = []
    if env["profile"] and env["profile"] != cfg["profile"]:
        warnings.append("file is for profile %r, server is set to %r" % (env["profile"], cfg["profile"]))
    cfg["env"] = {"tracks": env["tracks"], "acs": env["acs"]}
    set_config(db, cfg)
    bump(db)
    db.commit()
    missing = [t.partition("/")[0] for t in cfg["tracks"] if t.partition("/")[0] not in env["tracks"]]
    if missing:
        warnings.append("no digest for track(s): %s (create the reference with every baseline track)" % ", ".join(missing))
    return warnings


def cmd_set_env(args, db):
    try:
        env = parse_env(json.loads(Path(args.source).read_text(encoding="utf-8")))
    except ValueError as e:
        raise SystemExit(str(e))
    cfg = get_config(db)
    warnings = apply_env(db, cfg, env)
    print("Canonical environment set:", json.dumps(cfg["env"], indent=1))
    for w in warnings:
        print("WARNING:", w)


def cmd_admin(args):
    db = init_db(args.db)
    cfg = get_config(db)
    c = args.command
    if c == "init":
        set_config(db, cfg)
        print("database ready:", args.db)
    elif c == "set-env":
        cmd_set_env(args, db)
    elif c == "invite":
        code = new_invite_code()
        db.execute("INSERT INTO invites(code_hash, role, uses_left, created) VALUES(?,?,?,?)",
                   (invite_hash(code), args.role, args.uses, now()))
        db.commit()
        print("invite code (shown once): %s  role=%s uses=%d" % (code, args.role, args.uses))
    elif c == "create-user":
        key = "acb_" + secrets.token_urlsafe(32)
        db.execute("INSERT INTO users(name, key_hash, role, created) VALUES(?,?,?,?)", (args.name, sha(key), args.role, now()))
        db.commit()
        print("API key for %s (shown once): %s" % (args.name, key))
    elif c == "set-role":
        row = db.execute("SELECT id FROM users WHERE name=?", (args.name,)).fetchone()
        if not row:
            raise SystemExit("no such user")
        db.execute("UPDATE users SET role=? WHERE id=?", (args.role, row["id"]))
        keys = db.execute("SELECT DISTINCT car_hash, track FROM submissions WHERE user_id=?", (row["id"],)).fetchall()
        for k in keys:
            recompute(db, cfg, k["car_hash"], k["track"])
        bump(db)
        db.commit()
        print("%s is now %s; %d merged entries recomputed" % (args.name, args.role, len(keys)))
    elif c in ("ban", "unban"):
        row = db.execute("SELECT id FROM users WHERE name=?", (args.name,)).fetchone()
        if not row:
            raise SystemExit("no such user")
        db.execute("UPDATE users SET banned=? WHERE id=?", (1 if c == "ban" else 0, row["id"]))
        keys = db.execute("SELECT DISTINCT car_hash, track FROM submissions WHERE user_id=?", (row["id"],)).fetchall()
        for k in keys:
            recompute(db, cfg, k["car_hash"], k["track"])
        bump(db)
        db.commit()
        print("%s: %s, %d merged entries recomputed" % (c, args.name, len(keys)))
    elif c == "backup":
        dest = Path(args.to)
        dest.parent.mkdir(parents=True, exist_ok=True)
        out = sqlite3.connect(str(dest))
        db.backup(out)
        out.close()
        print("backup written:", dest)
    elif c == "recompute-all":
        keys = db.execute("SELECT DISTINCT car_hash, track FROM submissions").fetchall()
        for k in keys:
            recompute(db, cfg, k["car_hash"], k["track"])
        bump(db)
        db.commit()
        print("recomputed", len(keys))
    elif c == "recalc-classes":
        table, _ = refresh_classes(db, cfg, force=True)
        if table is None:
            raise SystemExit("Not enough cars with complete data yet (need at least 2).")
        print("class table v%d: %d cars, %d classes" % (table["version"], table["n_cars"], len(table["labels"])))
        for cl in table["classes"]:
            print("  %s: %3d cars, rating %.2f%% .. %.2f%%" % (cl["label"], cl["n"], cl["lo"], cl["hi"]))
    elif c == "stats":
        counts = car_counts(db, cfg)
        for label, n in (("cars rated", counts["verified"] + counts["provisional"]), ("verified", counts["verified"]),
                         ("provisional", counts["provisional"]), ("incomplete", counts["incomplete"]),
                         ("disputed", counts["disputed"])):
            print("%-12s %d" % (label, n))
        for label, q in (("users", "SELECT COUNT(*) FROM users"), ("banned", "SELECT COUNT(*) FROM users WHERE banned=1")):
            print("%-12s %d" % (label, db.execute(q).fetchone()[0]))
        table = latest_table(db)
        print("%-12s %s" % ("class table", "v%d, %d cars, %d classes" % (table["version"], table["n_cars"], len(table["labels"]))
                            if table else "none yet"))
    elif c == "disputed":
        for m in db.execute("SELECT car_hash, track FROM merged WHERE status='disputed'"):
            subs = db.execute("SELECT u.name, s.car_id, s.score_ms FROM submissions s JOIN users u ON u.id=s.user_id "
                              "WHERE s.car_hash=? AND s.track=?", (m["car_hash"], m["track"])).fetchall()
            print(m["car_hash"][:12], m["track"], ", ".join("%s=%.0fms" % (s["name"], s["score_ms"]) for s in subs))
    elif c == "config":
        if args.action == "get":
            print(json.dumps(cfg, indent=1))
        else:
            if args.key not in DEFAULT_CONFIG:
                raise SystemExit("unknown key; valid: " + ", ".join(DEFAULT_CONFIG))
            cfg[args.key] = json.loads(args.value)
            set_config(db, cfg)
            print(args.key, "=", json.dumps(cfg[args.key]))


def main():
    ap = argparse.ArgumentParser(description="AC Bench cloud server")
    ap.add_argument("--db", default=os.environ.get("ACB_DB", "acbench.db"))
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    sub.add_parser("init")
    s = sub.add_parser("set-env")
    s.add_argument("--from", dest="source", required=True, help="track_check_log.json or a JSON with tracks + acs")
    s = sub.add_parser("invite")
    s.add_argument("--role", choices=["user", "trusted"], default="user")
    s.add_argument("--uses", type=int, default=1)
    s = sub.add_parser("create-user")
    s.add_argument("name")
    s.add_argument("--role", choices=["user", "trusted"], default="user")
    for n in ("ban", "unban"):
        sub.add_parser(n).add_argument("name")
    s = sub.add_parser("set-role")
    s.add_argument("name")
    s.add_argument("--role", choices=["user", "trusted"], required=True)
    sub.add_parser("recompute-all")
    sub.add_parser("backup").add_argument("--to", required=True, help="file to write a consistent copy of the database to")
    sub.add_parser("recalc-classes")
    sub.add_parser("stats")
    sub.add_parser("disputed")
    s = sub.add_parser("config")
    s.add_argument("action", choices=["get", "set"])
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    args = ap.parse_args()
    if args.command == "serve":
        cmd_serve(args)
    else:
        cmd_admin(args)


if __name__ == "__main__":
    main()
