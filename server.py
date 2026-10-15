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
  python server.py publish-classes                        cut a new class table from the verified data
  python server.py stats | disputed | ban NAME | unban NAME | config get|set KEY VALUE | recompute-all
  python server.py set-role NAME --role trusted|user      a trusted user's submissions verify on their own
  python server.py backup --to /data/backups/acbench-2026-01-01.db    consistent copy while running

Admin web UI
  /admin (PocketID / OpenID Connect login) does the same operator jobs in a browser, and edits the benchmark
  conditions; see admin.py for the ACB_OIDC_* / ACB_ADMIN_* environment variables.

Benchmark conditions (public, no key needed)
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
  - classes come from a versioned class table: reference times + cut points fixed at publish time,
    so a car's class does not move when other cars are added. Publish a new table deliberately.
"""

import argparse, bisect, hashlib, json, math, os, re, secrets, sqlite3, statistics, sys, threading, time
from http import HTTPStatus
from pathlib import Path
from socketserver import ThreadingMixIn
from types import SimpleNamespace
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):  # share ac_rating.py / ac_car_classifier.py with the desktop tool
    sys.path.insert(0, str(_p))
import ac_rating  # noqa: E402
import admin  # noqa: E402
from ac_car_classifier import class_label  # noqa: E402

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
    "env": None,                        # canonical {"tracks": {track: digest}, "acs": sha256}
    "conditions": {},                   # free-form benchmark conditions served to clients (weather, time, ...)
    "export_max_age": 30,               # seconds the /v1/classes export may lag behind new uploads (0 = always fresh)
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, key_hash TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL DEFAULT 'user', banned INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
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
  n_total INTEGER NOT NULL, spread_pct REAL NOT NULL, status TEXT NOT NULL, updated INTEGER NOT NULL,
  PRIMARY KEY (car_hash, track));
CREATE TABLE IF NOT EXISTS cars (
  car_hash TEXT PRIMARY KEY, car_id TEXT NOT NULL, name TEXT NOT NULL, updated INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS users_created ON users (created, id);
CREATE INDEX IF NOT EXISTS merged_status ON merged (status, updated);
CREATE TABLE IF NOT EXISTS class_tables (version INTEGER PRIMARY KEY, created INTEGER NOT NULL, body TEXT NOT NULL);
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


def connect(path):
    db = sqlite3.connect(str(path), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db(path):
    db = connect(path)
    db.executescript(SCHEMA + admin.SCHEMA)
    if db.execute("SELECT 1 FROM cars LIMIT 1").fetchone() is None and db.execute("SELECT 1 FROM submissions LIMIT 1").fetchone():
        # existing database from before the cars table: take each car's most recent submission's names (one-time scan)
        db.execute("INSERT OR IGNORE INTO cars(car_hash, car_id, name, updated) "
                   "SELECT car_hash, car_id, name, MAX(created) FROM submissions GROUP BY car_hash")
    db.commit()
    return db


def get_config(db):
    cfg = dict(DEFAULT_CONFIG)
    row = db.execute("SELECT v FROM meta WHERE k='config'").fetchone()
    if row:
        cfg.update(json.loads(row["v"]))
    return cfg


def set_config(db, cfg):
    stored = {k: cfg[k] for k in DEFAULT_CONFIG}
    db.execute("INSERT INTO meta(k, v) VALUES('config', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (json.dumps(stored),))
    db.commit()


def bump(db):
    """Data version counter: changes whenever merged data or class tables change (drives ETag and cache)."""
    db.execute("INSERT INTO meta(k, v) VALUES('dv', '1') ON CONFLICT(k) DO UPDATE SET v = CAST(v AS INTEGER) + 1")


def data_version(db):
    row = db.execute("SELECT v FROM meta WHERE k='dv'").fetchone()
    return int(row["v"]) if row else 0


def latest_table(db):
    row = db.execute("SELECT version, created, body FROM class_tables ORDER BY version DESC LIMIT 1").fetchone()
    return json.loads(row["body"]) if row else None


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

def recompute(db, cfg, car_hash, track):
    """Agreement between users for one car on one track. Updates submissions.used and merged.

    Picks the biggest group of submissions that agree with each other (ties: the tightest, then the first row).
    Done on the sorted scores with binary search, O(n log n) instead of comparing every pair."""
    rows = db.execute(
        "SELECT s.id, s.score_ms, s.used, u.role FROM submissions s JOIN users u ON u.id = s.user_id "
        "WHERE s.car_hash=? AND s.track=? AND u.banned=0", (car_hash, track)).fetchall()
    if not rows:
        db.execute("DELETE FROM merged WHERE car_hash=? AND track=?", (car_hash, track))
        return
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
    spread = (max(cl_scores) - min(cl_scores)) / statistics.median(cl_scores) * 100
    changed = [(1 if r["id"] in used else 0, r["id"]) for r in rows if r["used"] != (1 if r["id"] in used else 0)]
    if changed:
        db.executemany("UPDATE submissions SET used=? WHERE id=?", changed)
    db.execute(
        "INSERT INTO merged(car_hash, track, score_ms, n_used, n_total, spread_pct, status, updated) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(car_hash, track) DO UPDATE SET score_ms=excluded.score_ms, "
        "n_used=excluded.n_used, n_total=excluded.n_total, spread_pct=excluded.spread_pct, "
        "status=excluded.status, updated=excluded.updated",
        (car_hash, track, statistics.median(cl_scores), len(cluster), len(rows), spread, status, now()))


# ------------------------------------------------------------------ classes

def classify(table, times_ms):
    """times_ms: {track: score_ms}. Class, gap and flags from the fixed class table."""
    tracks = table["tracks"]
    deltas = {t: (times_ms[t] / table["refs_ms"][t] - 1) * 100 for t in tracks}
    score = sum(deltas.values()) / len(tracks)
    cuts = table["cuts"]
    label = class_label(bisect.bisect_right(cuts, score) + 1)
    gap = ((1 + score / 100) / (1 + table["base_score"] / 100) - 1) * 100
    variance = max(deltas.values()) - min(deltas.values())
    flags, bop = [], ""
    if cuts:
        y = math.log1p(score / 100)
        k = min(range(len(cuts)), key=lambda i: abs(math.log1p(cuts[i] / 100) - y))
        dist = (math.exp(abs(math.log1p(cuts[k] / 100) - y)) - 1) * 100
        if dist < table["tie_zone"]:
            flags.append("borderline %s/%s (%.2f%%)" % (class_label(k + 1), class_label(k + 2), dist))
    if variance > table["class_gap"]:
        flags.append("high variance (%.1f%%)" % variance)
    bits = []
    for t in tracks:
        norm = table["norms"].get(label, {}).get(t)
        if norm is not None and norm - deltas[t] > 0.25 * table["class_gap"]:
            bits.append("%s: needs to be %.2f%% slower" % (t.partition("/")[0], norm - deltas[t]))
    bop = "; ".join(bits)
    return {"class": label, "score_pct": score, "gap_pct": gap, "deltas": deltas, "variance_pct": variance,
            "flags": flags, "bop": bop}


def publish_classes(db, cfg, include_provisional=False):
    """Cut a new class table from the merged data (natural breaks via ac_rating) and store it."""
    wanted = ("verified", "provisional") if include_provisional else ("verified",)
    per_car = {}
    for r in db.execute("SELECT car_hash, track, score_ms, status FROM merged"):
        if r["status"] in wanted:
            per_car.setdefault(r["car_hash"], {})[r["track"]] = {"status": "ok", "best_ms": r["score_ms"]}
    results = {h: v for h, v in per_car.items() if all(t in v for t in cfg["tracks"])}
    if len(results) < 2:
        raise SystemExit("Not enough cars with complete data (%d). Need at least 2." % len(results))
    cars, summ = ac_rating.rate(results, cfg["tracks"], class_gap=cfg["class_gap"],
                                min_size=cfg["min_class_size"], tie_zone=cfg["tie_zone"])
    order = sorted(cars.values(), key=lambda c: (c["score"], c["id"]))
    cuts, pos = [], 0
    for cl in summ["classes"][:-1]:
        pos += cl["n"]
        y1, y2 = math.log1p(order[pos - 1]["score"] / 100), math.log1p(order[pos]["score"] / 100)
        cuts.append(math.expm1((y1 + y2) / 2) * 100)
    refs = {t: min(v[t]["best_ms"] for v in results.values()) for t in cfg["tracks"]}
    labels = [class_label(i + 1) for i in range(len(summ["classes"]))]
    norms = {}
    for c in order:
        for ti, t in enumerate(cfg["tracks"]):
            norms.setdefault(c["cls"], {}).setdefault(t, []).append(c["deltas"][ti])
    norms = {lab: {t: statistics.median(v) for t, v in d.items()} for lab, d in norms.items()}
    version = (db.execute("SELECT MAX(version) AS v FROM class_tables").fetchone()["v"] or 0) + 1
    body = {"version": version, "created": now(), "profile": cfg["profile"], "tracks": cfg["tracks"],
            "refs_ms": refs, "cuts": cuts, "labels": labels, "norms": norms, "base_score": order[0]["score"],
            "class_gap": cfg["class_gap"], "tie_zone": cfg["tie_zone"], "n_cars": len(order),
            "classes": [{"label": c["label"], "n": c["n"], "lo": c["lo"], "hi": c["hi"], "width": c["width"]}
                        for c in summ["classes"]],
            "include_provisional": include_provisional}
    db.execute("INSERT INTO class_tables(version, created, body) VALUES(?,?,?)", (version, now(), json.dumps(body)))
    bump(db)
    db.commit()
    return body, summ


_STATUS_RANK = {"disputed": 3, "provisional": 2, "verified": 1}


def car_records(db, cfg, only_hash=None, only_hashes=None):
    table = latest_table(db)
    q = "SELECT car_hash, track, score_ms, n_used, n_total, status, updated FROM merged"
    args = ()
    names_q, names_args = "SELECT car_hash, car_id, name FROM cars", ()
    if only_hashes is not None:
        only_hashes = sorted(set(only_hashes))
        marks = ",".join("?" * len(only_hashes))
        q, args = q + " WHERE car_hash IN (%s)" % marks, tuple(only_hashes)
        names_q, names_args = names_q + " WHERE car_hash IN (%s)" % marks, tuple(only_hashes)
    elif only_hash:
        q, args = q + " WHERE car_hash=?", (only_hash,)
        names_q, names_args = names_q + " WHERE car_hash=?", (only_hash,)
    per_car = {}
    for r in db.execute(q, args):
        per_car.setdefault(r["car_hash"], {})[r["track"]] = r
    names = {}
    for r in db.execute(names_q, names_args):
        names[r["car_hash"]] = (r["car_id"], r["name"])
    out = []
    for h, per in sorted(per_car.items()):
        missing = [t for t in cfg["tracks"] if t not in per]
        sts = [per[t]["status"] for t in per]
        if "disputed" in sts:
            status = "disputed"
        elif missing:
            status = "incomplete"
        elif "provisional" in sts:
            status = "provisional"
        else:
            status = "verified"
        rec = {"car_hash": h, "car_id": names.get(h, ("", ""))[0], "name": names.get(h, ("", ""))[1],
               "status": status, "times_ms": {t: per[t]["score_ms"] for t in per},
               "n": {t: per[t]["n_used"] for t in per}, "updated": max(per[t]["updated"] for t in per),
               "class": None}
        if table and status in ("verified", "provisional"):
            rec.update(classify(table, {t: per[t]["score_ms"] for t in table["tracks"]}))
        out.append(rec)
    return table, out


# ------------------------------------------------------------------ http layer

def respond(code, obj, extra=None):
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
               ("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")] + (extra or [])
    return code, body, headers


def serve_file(path, content_type):
    p = HERE / path
    if not p.is_file():
        raise HttpError(404, "not found")
    body = p.read_bytes()
    headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("X-Content-Type-Options", "nosniff"),
        ("Cache-Control", "no-store"),
    ]
    return 200, body, headers


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
    user = db.execute("SELECT id, name, role, banned FROM users WHERE key_hash=?", (sha(header[7:].strip()),)).fetchone()
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


def handle_conditions(cfg, environ):
    doc = conditions_doc(cfg)
    etag = '"c-%s"' % doc["version"]
    if environ.get("HTTP_IF_NONE_MATCH") == etag:
        return 304, b"", [("ETag", etag)]
    body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    return 200, body, [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                       ("X-Content-Type-Options", "nosniff"), ("ETag", etag), ("Cache-Control", "no-cache")]


def handle_register(db, cfg, environ):
    data = read_json(environ, cfg)
    rate_limit(db, "ip:" + client_ip(environ, cfg), "register", cfg["rate_register_per_hour"])
    invite, name = data.get("invite"), data.get("name")
    if not isinstance(invite, str) or not isinstance(name, str) or not USER_NAME.match(name):
        raise HttpError(400, "invite and a name of 3-40 letters, digits, . _ - are required")
    row = db.execute("SELECT role, uses_left FROM invites WHERE code_hash=?", (sha(invite.strip()),)).fetchone()
    if not row or row["uses_left"] <= 0:
        raise HttpError(403, "invalid or used-up invite code")
    key = "acb_" + secrets.token_urlsafe(32)
    try:
        db.execute("INSERT INTO users(name, key_hash, role, created) VALUES(?,?,?,?)", (name, sha(key), row["role"], now()))
    except sqlite3.IntegrityError:
        raise HttpError(409, "that name is taken")
    db.execute("UPDATE invites SET uses_left = uses_left - 1 WHERE code_hash=?", (sha(invite.strip()),))
    db.commit()
    return respond(201, {"api_key": key, "name": name, "role": row["role"]})


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
    accepted, rejected, touched = 0, [], set()
    for i, e in enumerate(entries):
        norm, why = validate_entry(e, cfg, envs, refs)
        if why:
            if len(rejected) < 100:
                rejected.append({"index": i, "car_id": e.get("car_id") if isinstance(e, dict) else None,
                                 "track": e.get("track") if isinstance(e, dict) else None, "reason": why})
            continue
        db.execute(
            "INSERT INTO submissions(user_id, car_hash, car_id, name, track, score_ms, spread_pct, laps_json, batch, env_id, used, created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(user_id, car_hash, track) DO UPDATE SET "
            "car_id=excluded.car_id, name=excluded.name, score_ms=excluded.score_ms, spread_pct=excluded.spread_pct, "
            "laps_json=excluded.laps_json, batch=excluded.batch, env_id=excluded.env_id, created=excluded.created",
            (user["id"], norm["car_hash"], norm["car_id"], norm["name"], norm["track"], norm["score_ms"],
             norm["spread_pct"], json.dumps(norm["laps"]), norm["batch"], norm["env_id"], now()))
        db.execute("INSERT INTO cars(car_hash, car_id, name, updated) VALUES(?,?,?,?) ON CONFLICT(car_hash) DO UPDATE SET "
                   "car_id=excluded.car_id, name=excluded.name, updated=excluded.updated",
                   (norm["car_hash"], norm["car_id"], norm["name"], now()))
        touched.add((norm["car_hash"], norm["track"]))
        accepted += 1
    for h, t in touched:
        recompute(db, cfg, h, t)
    if touched:
        bump(db)
    db.commit()
    status = {}
    for h, t in touched:
        row = db.execute("SELECT status FROM merged WHERE car_hash=? AND track=?", (h, t)).fetchone()
        key = row["status"] if row else "none"
        status[key] = status.get(key, 0) + 1
    return respond(200, {"accepted": accepted, "rejected": rejected, "rejected_total": len(entries) - accepted,
                         "merged_status": status})


_export = {"etag": None, "body": None, "dv": None, "table": None, "built": 0.0}
_export_lock = threading.Lock()


def _export_fresh(tv, dv, ttl):
    c = _export
    return c["body"] is not None and c["table"] == tv and (c["dv"] == dv or time.time() - c["built"] < ttl)


def handle_classes(db, cfg, environ):
    table = latest_table(db)
    tv, dv, ttl = (table["version"] if table else 0), data_version(db), max(0, int(cfg.get("export_max_age", 0)))
    if not _export_fresh(tv, dv, ttl):
        with _export_lock:  # one thread rebuilds, the others wait for it instead of all doing the same work
            if not _export_fresh(tv, dv, ttl):
                _, cars = car_records(db, cfg)
                payload = json.dumps({"format": 1, "profile": cfg["profile"], "tracks": cfg["tracks"],
                                      "table": table, "cars": cars}, separators=(",", ":"))
                # the etag covers the content only (not the timestamp), so it is the same on every worker
                body = (payload[:-1] + ',"generated":%d}' % now()).encode("utf-8")
                _export.update(etag='"%d-%s"' % (tv, sha(payload)[:16]), body=body, dv=dv, table=tv, built=time.time())
    etag, body = _export["etag"], _export["body"]
    cache = "public, max-age=%d" % ttl if ttl else "no-cache"
    if environ.get("HTTP_IF_NONE_MATCH") == etag:
        return 304, b"", [("ETag", etag), ("Cache-Control", cache)]
    return 200, body, [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                       ("X-Content-Type-Options", "nosniff"), ("ETag", etag), ("Cache-Control", cache)]


MAX_LOOKUP = 500


def handle_lookup(db, cfg, environ):
    """Live data for just the cars a client has installed: POST {"hashes": [car physics hashes]} (public, like
    /v1/classes, but only the requested cars are returned and nothing is cached, so the data is always current).
    Cars the server knows nothing about are simply absent from "cars"."""
    rate_limit(db, "ip:" + client_ip(environ, cfg), "lookup", cfg["rate_lookups_per_hour"])
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
                           bump=bump, publish_classes=publish_classes, set_config=set_config,
                           latest_table=latest_table, parse_env=parse_env, apply_env=apply_env)


def route(environ, db_path):
    db = connect(db_path)
    try:
        cfg = get_config(db)
        method, path = environ["REQUEST_METHOD"], environ.get("PATH_INFO", "")
        if path == "/admin" or path.startswith("/admin/"):
            return admin.handle(environ, db, cfg, _admin_ctx())
        if method == "GET" and path in ("/", "/stats"):
            return serve_file("stats.html", "text/html; charset=utf-8")
        if method == "GET" and path == "/v1/health":
            return respond(200, {"ok": True, "api": API_VERSION, "time": now()})
        if method == "GET" and path == "/v1/conditions":
            return handle_conditions(cfg, environ)
        if method == "GET" and path == "/v1/config":
            return respond(200, public_config(cfg, db))
        if method == "POST" and path == "/v1/register":
            return handle_register(db, cfg, environ)
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
        db.close()


def create_app(db_path):
    init_db(db_path).close()

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
        code = secrets.token_urlsafe(9)
        db.execute("INSERT INTO invites(code_hash, role, uses_left, created) VALUES(?,?,?,?)",
                   (sha(code), args.role, args.uses, now()))
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
        db.commit()
        keys = db.execute("SELECT DISTINCT car_hash, track FROM submissions WHERE user_id=?", (row["id"],)).fetchall()
        for k in keys:
            recompute(db, cfg, k["car_hash"], k["track"])
        db.commit()
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
    elif c == "publish-classes":
        body, summ = publish_classes(db, cfg, args.include_provisional)
        print("class table v%d: %d cars, %d classes" % (body["version"], body["n_cars"], len(body["labels"])))
        for cl in body["classes"]:
            print("  %s: %3d cars, rating %.2f%% .. %.2f%%" % (cl["label"], cl["n"], cl["lo"], cl["hi"]))
        for w in summ["warnings"][:10]:
            print("WARNING:", w)
    elif c == "stats":
        for label, q in (("users", "SELECT COUNT(*) FROM users"), ("banned", "SELECT COUNT(*) FROM users WHERE banned=1"),
                         ("submissions", "SELECT COUNT(*) FROM submissions"),
                         ("verified", "SELECT COUNT(*) FROM merged WHERE status='verified'"),
                         ("provisional", "SELECT COUNT(*) FROM merged WHERE status='provisional'"),
                         ("disputed", "SELECT COUNT(*) FROM merged WHERE status='disputed'"),
                         ("class tables", "SELECT COUNT(*) FROM class_tables")):
            print("%-12s %d" % (label, db.execute(q).fetchone()[0]))
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
    s = sub.add_parser("publish-classes")
    s.add_argument("--include-provisional", action="store_true")
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
