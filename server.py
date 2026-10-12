#!/usr/bin/env python3
"""
AC Bench cloud server: stores benchmark lap times and the classes derived from them.

Users benchmark cars with the desktop tool and upload; the server validates every entry, merges
submissions from different users into one agreed time per car and track, and publishes the classes
for download. Standard library only (WSGI + SQLite). Put it behind a TLS reverse proxy.

Run
  python server.py init                          create the database
  python server.py set-env --from track_check_log.json   canonical vanilla environment (see README)
  python server.py invite --role user --uses 20  print an invite code
  python server.py serve --port 8080             small deployments; production: gunicorn server:application
Operate
  python server.py publish-classes               cut a new class table from the verified data
  python server.py stats | disputed | ban NAME | unban NAME | config get|set KEY VALUE | recompute-all
  python server.py set-role NAME --role trusted|user   a trusted user's submissions verify on their own
  python server.py backup --to /data/backups/acbench-2026-01-01.db     consistent copy while running

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
import argparse, bisect, hashlib, json, math, os, re, secrets, sqlite3, statistics, sys, time
from http import HTTPStatus
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):          # share ac_rating.py / ac_car_classifier.py with the desktop tool
    sys.path.insert(0, str(_p))
import ac_rating                          # noqa: E402
from ac_car_classifier import class_label  # noqa: E402

API_VERSION = 1

DEFAULT_CONFIG = {
    "profile": "baseline-v2",                      # baseline profile id the server accepts
    "tracks": ["magione", "ks_vallelunga/club_circuit"],
    "laps": 4,                                     # recorded laps per car: lap 1 + the timed laps
    "max_batch": 1,                                # cars sharing the track in the benchmark race (1 = cleanest)
    "tolerance_pct": 0.5,                          # submissions agree if within this % of each other
    "min_confirmations": 2,                        # distinct users that must agree (trusted users count alone)
    "spread_limit_pct": 1.5,                       # max (slowest - fastest) / median over the timed laps
    "plaus_min_ms": 20000, "plaus_max_ms": 900000,  # absolute lap time bounds until a class table exists
    "plaus_low": 0.4, "plaus_high": 6.0,           # bounds as multiples of the class-table reference time
    "max_entries": 500, "max_body_bytes": 2000000,
    "rate_submits_per_hour": 20, "rate_register_per_hour": 10,
    "trust_proxy_header": False,                   # honour X-Forwarded-For (only behind your own proxy)
    "class_gap": 2.0, "min_class_size": 3, "tie_zone": 0.10,   # class table settings (see ac_rating.py)
    "env": None,                                   # canonical {"tracks": {track: digest}, "acs": sha256}
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
CREATE TABLE IF NOT EXISTS class_tables (version INTEGER PRIMARY KEY, created INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, who TEXT NOT NULL, kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ev_idx ON events (who, kind, ts);
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
    db.executescript(SCHEMA)
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
    if (env.get("tracks") or {}).get(tid) != (canon.get("tracks") or {}).get(tid):
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
    """Agreement between users for one car on one track. Updates submissions.used and merged."""
    rows = db.execute(
        "SELECT s.id, s.score_ms, u.role FROM submissions s JOIN users u ON u.id = s.user_id "
        "WHERE s.car_hash=? AND s.track=? AND u.banned=0", (car_hash, track)).fetchall()
    if not rows:
        db.execute("DELETE FROM merged WHERE car_hash=? AND track=?", (car_hash, track))
        return
    tol = cfg["tolerance_pct"] / 100
    best, best_key = [], None
    for centre in rows:       # the biggest group of submissions that agree with each other (ties: the tightest)
        cluster = [r for r in rows if abs(r["score_ms"] - centre["score_ms"]) <= tol * centre["score_ms"]]
        key = (len(cluster), -(max(r["score_ms"] for r in cluster) - min(r["score_ms"] for r in cluster)))
        if best_key is None or key > best_key:
            best, best_key = cluster, key
    used = {r["id"] for r in best}
    trusted = any(r["role"] == "trusted" for r in best)
    if len(best) >= cfg["min_confirmations"] or trusted:
        status = "verified"
    elif len(rows) == 1:
        status = "provisional"
    else:
        status = "disputed"
    scores = [r["score_ms"] for r in best]
    spread = (max(scores) - min(scores)) / statistics.median(scores) * 100
    for r in rows:
        db.execute("UPDATE submissions SET used=? WHERE id=?", (1 if r["id"] in used else 0, r["id"]))
    db.execute(
        "INSERT INTO merged(car_hash, track, score_ms, n_used, n_total, spread_pct, status, updated) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(car_hash, track) DO UPDATE SET score_ms=excluded.score_ms, "
        "n_used=excluded.n_used, n_total=excluded.n_total, spread_pct=excluded.spread_pct, "
        "status=excluded.status, updated=excluded.updated",
        (car_hash, track, statistics.median(scores), len(best), len(rows), spread, status, now()))


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
            if norm is not None and norm - deltas[t] > 0.25 *
