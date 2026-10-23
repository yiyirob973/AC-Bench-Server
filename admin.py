"""
Admin web UI for AC Bench Server, protected by OpenID Connect (PocketID). Standard library only.

Routes (all under /admin; nothing here is reachable without a valid admin session):
    GET  /admin                     dashboard (redirects to /admin/login when signed out)
    GET  /admin/login               starts the OIDC authorization-code + PKCE flow
    GET  /admin/callback            OIDC redirect target
    POST /admin/logout
    GET  /admin/api/overview        everything the dashboard shows
    POST /admin/api/env             set the canonical environment (what `set-env` does) from an uploaded track reference
    POST /admin/api/...             actions (ban, role, invites, classes, ...), CSRF protected

Configuration (environment variables; secrets are deliberately NOT stored in the SQLite config):
    ACB_PUBLIC_URL          public origin of this server, e.g. https://bench.example.com
    ACB_OIDC_ISSUER         PocketID base URL, e.g. https://id.example.com
    ACB_OIDC_CLIENT_ID      OIDC client id created in PocketID
    ACB_OIDC_CLIENT_SECRET  client secret (optional for a public client; PKCE is always used)
    ACB_ADMIN_SUBS          comma list of allowed PocketID user ids (the `sub` claim)   } at least one of
    ACB_ADMIN_EMAILS        comma list of allowed e-mail addresses                       } these three is
    ACB_ADMIN_GROUPS        comma list of allowed PocketID group names                   } required
    ACB_ADMIN_SESSION_HOURS session lifetime, default 8

Fails closed: if anything above is missing, /admin answers 503 and nobody gets in.
"""

import base64, hashlib, hmac, html, json, os, re, secrets, sys, time
import urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SESSION_COOKIE, STATE_COOKIE = "acb_admin", "acb_oidc"
SCOPES = "openid profile email groups"

SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_sessions (
  token_hash TEXT PRIMARY KEY, sub TEXT NOT NULL, email TEXT, name TEXT,
  csrf TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS oidc_states (
  state_hash TEXT PRIMARY KEY, verifier TEXT NOT NULL, nonce TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS admin_audit (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '');
"""

# config keys that may be edited from the UI: key -> (type, min, max). profile/tracks/laps are edited via /conditions, the canonical environment via /env.
EDITABLE = {
    "tolerance_pct": (float, 0.01, 10), "min_confirmations": (int, 1, 10),
    "spread_limit_pct": (float, 0.1, 10), "export_max_age": (int, 0, 3600),
    "rate_submits_per_hour": (int, 1, 10000), "rate_register_per_hour": (int, 1, 10000), "rate_lookups_per_hour": (int, 1, 100000),
    "class_gap": (float, 0.1, 20), "min_class_size": (int, 1, 100), "tie_zone": (float, 0, 5),
    "variance_limit_pct": (float, 0.1, 100),
}


PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,39}$")
TRACK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,59}(/[A-Za-z0-9][A-Za-z0-9_.\-]{0,59})?$")  # no "." / ".." segments


class LoginError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ------------------------------------------------------------------ settings / helpers

def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lst(name):
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def settings():
    pub = urllib.parse.urlsplit(os.environ.get("ACB_PUBLIC_URL", "").strip())
    origin = "%s://%s" % (pub.scheme, pub.netloc) if pub.scheme and pub.netloc else ""
    try:
        hours = float(os.environ.get("ACB_ADMIN_SESSION_HOURS", "8"))
    except ValueError:
        hours = 8
    return {
        "origin": origin,
        "issuer": os.environ.get("ACB_OIDC_ISSUER", "").strip().rstrip("/"),
        "client_id": os.environ.get("ACB_OIDC_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("ACB_OIDC_CLIENT_SECRET", "").strip(),
        "subs": _lst("ACB_ADMIN_SUBS"),
        "emails": [e.lower() for e in _lst("ACB_ADMIN_EMAILS")],
        "groups": _lst("ACB_ADMIN_GROUPS"),
        "ttl": max(300, int(hours * 3600)),
    }


def configured(s):
    return bool(s["origin"] and s["issuer"] and s["client_id"] and (s["subs"] or s["emails"] or s["groups"]))


def _cookie(s, name, value, max_age, path="/admin"):
    c = "%s=%s; Path=%s; HttpOnly; SameSite=Lax; Max-Age=%d" % (name, value, path, max_age)
    if s["origin"].startswith("https://"):
        c += "; Secure"
    return ("Set-Cookie", c)


def _cookies(environ):
    out = {}
    for part in environ.get("HTTP_COOKIE", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k:
            out[k] = v
    return out


def _same(a, b):
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def _redirect(url, extra=None):
    return 302, b"", [("Location", url), ("Content-Length", "0"), ("Cache-Control", "no-store")] + (extra or [])


SEC_HEADERS = [("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store"),
               ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY")]


def _message_page(code, title, msg, link=("/admin", "Back to /admin")):
    body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title><body style=\"font:16px system-ui;background:#14171c;color:#e6e8eb;display:grid;"
            "place-items:center;height:100vh;margin:0\"><div style=\"max-width:28rem;padding:1.5rem\"><h1 style="
            "\"font-size:1.2rem\">%s</h1><p>%s</p><p><a style=\"color:#7fb2ff\" href=\"%s\">%s</a></p>"
            "</div>" % (html.escape(title), html.escape(title), html.escape(msg),
                        html.escape(link[0]), html.escape(link[1]))).encode("utf-8")
    return code, body, [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))),
                        ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")] + SEC_HEADERS


# ------------------------------------------------------------------ OIDC (PocketID)

def _http(url, data=None, headers=None):
    # urllib's default "Python-urllib" User-Agent is blocked by many WAFs (Cloudflare error 1010)
    hdrs = {"User-Agent": "Mozilla/5.0 (compatible; ACBench-Server/1.0)", "Accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read(1000000).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LoginError(502, "identity provider request failed: %s (%s)" % (e, url.split("?")[0]))


_disc = {"t": 0.0, "doc": None}


def discovery(s):
    if _disc["doc"] and time.time() - _disc["t"] < 3600:
        return _disc["doc"]
    doc = _http(s["issuer"] + "/.well-known/openid-configuration")
    if str(doc.get("issuer", "")).rstrip("/") != s["issuer"]:
        raise LoginError(502, "identity provider issuer does not match ACB_OIDC_ISSUER")
    base = urllib.parse.urlsplit(s["issuer"])
    for k in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
        u = urllib.parse.urlsplit(str(doc.get(k, "")))
        if (u.scheme, u.netloc) != (base.scheme, base.netloc):  # endpoints must live on the issuer's origin
            raise LoginError(502, "unexpected %s in discovery document" % k)
    _disc.update(t=time.time(), doc=doc)
    return doc


def _id_token_claims(tok):
    # The id_token arrives straight from the token endpoint over TLS, so (OIDC Core 3.1.3.7) its signature
    # need not be verified; iss/aud/exp/nonce are still checked and the identity comes from /userinfo.
    try:
        part = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode("utf-8"))
    except Exception:
        raise LoginError(502, "identity provider returned an invalid id_token")


def start_login(db, s, now):
    d = discovery(s)
    db.execute("DELETE FROM oidc_states WHERE created<?", (now - 600,))
    if db.execute("SELECT COUNT(*) FROM oidc_states").fetchone()[0] > 500:
        raise LoginError(429, "too many login attempts, try again in a few minutes")
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    db.execute("INSERT INTO oidc_states(state_hash, verifier, nonce, created) VALUES(?,?,?,?)",
               (_sha(state), verifier, nonce, now))
    db.commit()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": s["client_id"], "redirect_uri": s["origin"] + "/admin/callback",
        "scope": SCOPES, "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    sep = "&" if "?" in d["authorization_endpoint"] else "?"
    return _redirect(d["authorization_endpoint"] + sep + q, [_cookie(s, STATE_COOKIE, state, 600)])


def allowed(s, claims):
    if claims.get("sub") in s["subs"]:
        return True
    email = claims.get("email")
    if isinstance(email, str) and email.lower() in s["emails"] and claims.get("email_verified") is not False:
        return True
    groups = claims.get("groups")
    return isinstance(groups, list) and any(g in s["groups"] for g in groups if isinstance(g, str))


def finish_login(db, s, environ, query, now):
    if query.get("error"):
        raise LoginError(403, "identity provider refused the login: %s" % query["error"][:80])
    code, state = query.get("code", ""), query.get("state", "")
    if not code or not state or not _same(_cookies(environ).get(STATE_COOKIE, ""), state):
        raise LoginError(400, "login state mismatch; start again from /admin")
    row = db.execute("SELECT verifier, nonce, created FROM oidc_states WHERE state_hash=?", (_sha(state),)).fetchone()
    db.execute("DELETE FROM oidc_states WHERE state_hash=?", (_sha(state),))  # single use
    db.commit()
    if not row or now - row["created"] > 600:
        raise LoginError(400, "login attempt expired; start again from /admin")
    d = discovery(s)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": s["origin"] + "/admin/callback",
            "client_id": s["client_id"], "code_verifier": row["verifier"]}
    if s["client_secret"]:
        form["client_secret"] = s["client_secret"]
    tok = _http(d["token_endpoint"], urllib.parse.urlencode(form).encode(),
                {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    if not tok.get("id_token") or not tok.get("access_token"):
        raise LoginError(502, "identity provider returned no tokens")
    idc = _id_token_claims(tok["id_token"])
    aud = idc.get("aud")
    if str(idc.get("iss", "")).rstrip("/") != s["issuer"] or s["client_id"] not in (aud if isinstance(aud, list) else [aud]):
        raise LoginError(403, "id_token issuer/audience mismatch")
    if not isinstance(idc.get("exp"), (int, float)) or idc["exp"] < now - 60:
        raise LoginError(403, "id_token expired")
    if not _same(idc.get("nonce", ""), row["nonce"]):
        raise LoginError(403, "id_token nonce mismatch")
    info = _http(d["userinfo_endpoint"], headers={"Authorization": "Bearer " + tok["access_token"], "Accept": "application/json"})
    if not info.get("sub") or info.get("sub") != idc.get("sub"):
        raise LoginError(403, "userinfo subject mismatch")
    claims = dict(idc)
    claims.update(info)
    who = claims.get("email") or claims["sub"]
    if not allowed(s, claims):
        _audit(db, str(who), "login_denied", "")
        raise LoginError(403, "%s is not allowed to use the admin interface" % who)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    db.execute("DELETE FROM admin_sessions WHERE expires<?", (now,))
    db.execute("INSERT INTO admin_sessions(token_hash, sub, email, name, csrf, created, expires) VALUES(?,?,?,?,?,?,?)",
               (_sha(token), claims["sub"], str(claims.get("email") or "")[:200],
                str(claims.get("name") or claims.get("preferred_username") or "")[:200], csrf, now, now + s["ttl"]))
    db.commit()
    _audit(db, str(who), "login", "")
    return _redirect("/admin", [_cookie(s, SESSION_COOKIE, token, s["ttl"]), _cookie(s, STATE_COOKIE, "", 0)])


# ------------------------------------------------------------------ sessions, audit, csrf

def _audit(db, actor, action, detail):
    db.execute("INSERT INTO admin_audit(ts, actor, action, detail) VALUES(?,?,?,?)",
               (int(time.time()), actor[:200], action, detail[:500]))
    db.commit()


def current_session(db, environ, now):
    tok = _cookies(environ).get(SESSION_COOKIE)
    if not tok:
        return None
    return db.execute("SELECT * FROM admin_sessions WHERE token_hash=? AND expires>?", (_sha(tok), now)).fetchone()


def check_csrf(ctx, s, environ, sess):
    if not _same(environ.get("HTTP_X_CSRF_TOKEN", ""), sess["csrf"]):
        raise ctx.HttpError(403, "missing or bad CSRF token")
    origin = environ.get("HTTP_ORIGIN")
    if origin and origin != s["origin"]:
        raise ctx.HttpError(403, "cross-origin request refused")
    if not environ.get("CONTENT_TYPE", "").lower().startswith("application/json"):
        raise ctx.HttpError(415, "Content-Type must be application/json")


# ------------------------------------------------------------------ api

def _int_arg(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError("%s must be an integer between %d and %d" % (what, lo, hi))
    return v


def clean_benchmark(d):
    """Validate the benchmark conditions sent from the dashboard. Raises ValueError with a readable reason."""
    profile, tracks = d.get("profile"), d.get("tracks")
    if not isinstance(profile, str) or not PROFILE_RE.match(profile):
        raise ValueError("profile must be 1-40 letters, digits, . _ - (starting with a letter or digit)")
    if (not isinstance(tracks, list) or not 1 <= len(tracks) <= 20 or len(set(map(str, tracks))) != len(tracks)
            or not all(isinstance(t, str) and TRACK_RE.match(t) for t in tracks)):
        raise ValueError("tracks must be 1-20 unique entries like magione or ks_vallelunga/club_circuit")
    laps = _int_arg(d.get("laps"), 2, 12, "laps")
    max_batch = _int_arg(d.get("max_batch"), 1, 64, "max_batch")
    cond = d.get("conditions", {})
    if not isinstance(cond, dict):
        raise ValueError("conditions must be a JSON object")
    if len(json.dumps(cond, separators=(",", ":"))) > 8192:
        raise ValueError("conditions must be under 8 KB")
    return {"profile": profile, "tracks": tracks, "laps": laps, "max_batch": max_batch, "conditions": cond}


def _condition_warnings(cfg, table, changed):
    w = []
    if set(changed) & {"profile", "tracks", "laps", "max_batch"}:
        w.append("Clients benchmarking under the old conditions will have their uploads rejected from now on.")
    env = cfg.get("env")
    if not env:
        w.append("No canonical environment is set (upload a track reference in the dashboard or run set-env); submissions are refused until it is.")
    else:
        missing = [t for t in cfg["tracks"] if t.partition("/")[0] not in (env.get("tracks") or {})]
        if missing:
            w.append("No reference digest yet for %s; uploads for them are refused until a track reference "
                     "that includes them is loaded." % ", ".join(missing))
    if table and (table.get("profile") != cfg["profile"] or table.get("tracks") != cfg["tracks"]):
        w.append("The class table (v%d) still reflects the old profile/tracks. It is rebuilt by itself once at least "
                 "two cars have verified results on every new track." % table["version"])
    return w


def list_users(db, ctx, environ):
    """One page of users, optionally filtered by name; the user table can be huge so it is never sent whole."""
    qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""), max_num_fields=10)
    try:
        limit = _int_arg(int(qs.get("limit", ["50"])[0]), 1, 100, "limit")
        offset = _int_arg(int(qs.get("offset", ["0"])[0]), 0, 100000000, "offset")
    except ValueError as e:
        raise ctx.HttpError(400, str(e))
    q = qs.get("q", [""])[0].strip()[:60]
    where, args = "", []
    if q:
        where = "WHERE u.name LIKE ? ESCAPE '!'"
        args = ["%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"]
    total = db.execute("SELECT COUNT(*) FROM users u " + where, args).fetchone()[0]
    users = [dict(r) for r in db.execute(
        "SELECT u.id, u.name, u.role, u.banned, u.created, "
        "(SELECT COUNT(DISTINCT s.car_hash) FROM submissions s WHERE s.user_id=u.id) AS cars, "
        "1 + (SELECT COUNT(*) FROM user_keys k WHERE k.user_id=u.id) AS devices FROM users u " + where +
        " ORDER BY u.created DESC, u.id DESC LIMIT ? OFFSET ?", args + [limit, offset])]
    return {"users": users, "total": total, "limit": limit, "offset": offset}


def overview(db, cfg, ctx, sess):
    q = lambda sql: db.execute(sql).fetchone()[0]
    cars = ctx.car_counts(db, cfg)   # counted per car, not per track result or per upload
    stats = {"cars_rated": cars["verified"] + cars["provisional"], "verified": cars["verified"],
             "provisional": cars["provisional"], "incomplete": cars["incomplete"], "disputed": cars["disputed"],
             "users": q("SELECT COUNT(*) FROM users"), "banned": q("SELECT COUNT(*) FROM users WHERE banned=1")}
    invites = [dict(r) for r in db.execute(
        "SELECT code_hash AS id, role, uses_left, created FROM invites ORDER BY created DESC LIMIT 100")]
    disputed = []
    for m in db.execute("SELECT car_hash, track FROM merged WHERE status='disputed' ORDER BY updated DESC LIMIT 100"):
        subs = [dict(r) for r in db.execute(
            "SELECT s.id, u.name AS user, s.car_id, s.name, s.score_ms, s.used FROM submissions s "
            "JOIN users u ON u.id=s.user_id WHERE s.car_hash=? AND s.track=? ORDER BY s.score_ms",
            (m["car_hash"], m["track"]))]
        disputed.append({"car_hash": m["car_hash"], "track": m["track"], "submissions": subs})
    table = ctx.latest_table(db)
    classes = {"include_provisional": bool(cfg.get("include_provisional")), "refresh_seconds": cfg.get("class_refresh_seconds"),
               "table": {"version": table["version"], "created": table["created"], "n_cars": table["n_cars"],
                         "classes": len(table["labels"])} if table else None}
    env = cfg.get("env")
    audit = [dict(r) for r in db.execute("SELECT ts, actor, action, detail FROM admin_audit ORDER BY id DESC LIMIT 50")]
    return {"me": {"name": sess["name"], "email": sess["email"], "sub": sess["sub"], "csrf": sess["csrf"]},
            "stats": stats, "invites": invites, "disputed": disputed, "classes": classes,
            "config": {k: cfg[k] for k in EDITABLE},
            "benchmark": {"profile": cfg["profile"], "tracks": cfg["tracks"], "laps": cfg["laps"],
                          "max_batch": cfg["max_batch"], "conditions": cfg.get("conditions") or {}},
            "env": {"set": bool(env), "tracks": dict(sorted((env or {}).get("tracks", {}).items())),
                    "acs": (env or {}).get("acs"), "wanted": sorted({t.partition("/")[0] for t in cfg["tracks"]})},
            "audit": audit}


def _recompute_user(db, cfg, ctx, uid):
    keys = db.execute("SELECT DISTINCT car_hash, track FROM submissions WHERE user_id=?", (uid,)).fetchall()
    for k in keys:
        ctx.recompute(db, cfg, k["car_hash"], k["track"])
    ctx.bump(db)
    db.commit()
    return len(keys)


def api(db, cfg, ctx, sess, environ, method, path, now):
    actor = sess["email"] or sess["sub"]
    if method == "GET":
        if path == "/admin/api/overview":
            return ctx.respond(200, overview(db, cfg, ctx, sess))
        if path == "/admin/api/users":
            return ctx.respond(200, list_users(db, ctx, environ))
        raise ctx.HttpError(404, "not found")
    if method != "POST":
        raise ctx.HttpError(405, "method not allowed")
    data = ctx.read_json(environ, cfg)
    parts = path[len("/admin/api/"):].split("/")

    if parts[0] == "users" and len(parts) == 3 and parts[1].isdigit():
        user = db.execute("SELECT id, name FROM users WHERE id=?", (int(parts[1]),)).fetchone()
        if not user:
            raise ctx.HttpError(404, "no such user")
        if parts[2] in ("ban", "unban"):
            db.execute("UPDATE users SET banned=? WHERE id=?", (1 if parts[2] == "ban" else 0, user["id"]))
            db.commit()
        elif parts[2] == "role":
            if data.get("role") not in ("user", "trusted"):
                raise ctx.HttpError(400, "role must be user or trusted")
            db.execute("UPDATE users SET role=? WHERE id=?", (data["role"], user["id"]))
            db.commit()
        else:
            raise ctx.HttpError(404, "not found")
        n = _recompute_user(db, cfg, ctx, user["id"])
        _audit(db, actor, "user_" + parts[2], "%s %s" % (user["name"], data.get("role", "")))
        return ctx.respond(200, {"ok": True, "recomputed": n})

    if parts == ["invites"]:
        role = data.get("role", "user")
        try:
            uses = _int_arg(data.get("uses", 1), 1, 1000, "uses")
        except ValueError as e:
            raise ctx.HttpError(400, str(e))
        if role not in ("user", "trusted"):
            raise ctx.HttpError(400, "role must be user or trusted")
        code = ctx.new_invite_code()
        db.execute("INSERT INTO invites(code_hash, role, uses_left, created) VALUES(?,?,?,?)", (ctx.invite_hash(code), role, uses, now))
        db.commit()
        _audit(db, actor, "invite_create", "role=%s uses=%d" % (role, uses))
        return ctx.respond(201, {"code": code, "role": role, "uses": uses})

    if parts == ["invites", "revoke"]:
        cur = db.execute("UPDATE invites SET uses_left=0 WHERE code_hash=?", (str(data.get("id", "")),))
        db.commit()
        if not cur.rowcount:
            raise ctx.HttpError(404, "no such invite")
        _audit(db, actor, "invite_revoke", str(data["id"])[:12])
        return ctx.respond(200, {"ok": True})

    if parts[0] == "submissions" and len(parts) == 3 and parts[1].isdigit() and parts[2] == "delete":
        sub = db.execute("SELECT s.car_hash, s.track, s.car_id, u.name AS user FROM submissions s "
                         "JOIN users u ON u.id=s.user_id WHERE s.id=?", (int(parts[1]),)).fetchone()
        if not sub:
            raise ctx.HttpError(404, "no such submission")
        db.execute("DELETE FROM submissions WHERE id=?", (int(parts[1]),))
        ctx.recompute(db, cfg, sub["car_hash"], sub["track"])
        ctx.bump(db)
        db.commit()
        _audit(db, actor, "submission_delete", "%s %s %s" % (sub["user"], sub["car_id"], sub["track"]))
        return ctx.respond(200, {"ok": True})

    if parts == ["recompute"]:
        keys = db.execute("SELECT DISTINCT car_hash, track FROM submissions").fetchall()
        for k in keys:
            ctx.recompute(db, cfg, k["car_hash"], k["track"])
        ctx.bump(db)
        db.commit()
        _audit(db, actor, "recompute_all", str(len(keys)))
        return ctx.respond(200, {"ok": True, "recomputed": len(keys)})

    if parts == ["classes"]:
        if "include_provisional" in data:
            cfg["include_provisional"] = bool(data["include_provisional"])
            ctx.set_config(db, cfg)
            db.commit()
        table, rebuilt = ctx.refresh_classes(db, cfg, force=True)
        if table is None:
            raise ctx.HttpError(422, "Not enough cars with results on every track yet (need at least 2).")
        _audit(db, actor, "classes_recalc", "v%d cars=%d provisional=%s" % (table["version"], table["n_cars"], cfg["include_provisional"]))
        return ctx.respond(200, {"version": table["version"], "n_cars": table["n_cars"], "classes": len(table["labels"])})

    if parts == ["conditions"]:
        try:
            new = clean_benchmark(data)
        except ValueError as e:
            raise ctx.HttpError(400, str(e))
        old = {k: cfg[k] for k in ("profile", "tracks", "laps", "max_batch")}
        old["conditions"] = cfg.get("conditions") or {}
        changed = [k for k in new if new[k] != old[k]]
        cfg.update(new)
        ctx.set_config(db, cfg)
        ctx.bump(db)  # invalidates cached /v1/classes exports, which embed profile and tracks
        db.commit()
        _audit(db, actor, "conditions_set", ("changed: " + ", ".join(changed)) if changed else "no change")
        return ctx.respond(200, {"ok": True, "changed": changed,
                                 "warnings": _condition_warnings(cfg, ctx.latest_table(db), changed)})

    if parts == ["env"]:
        ref = data.get("reference")
        if ref is None:
            raise ctx.HttpError(400, "reference is required (the contents of the track reference file)")
        try:
            env = ctx.parse_env(ref)
        except ValueError as e:
            raise ctx.HttpError(400, str(e))
        old = (cfg.get("env") or {})
        warnings = ctx.apply_env(db, cfg, env)
        old_t, new_t = old.get("tracks") or {}, env["tracks"]
        changed = sorted(t for t in new_t if old_t.get(t) != new_t[t])
        removed = sorted(t for t in old_t if t not in new_t)
        acs_changed = bool(old) and old.get("acs") != env["acs"]
        if old and (changed or removed or acs_changed):
            warnings.append("Clients whose installs do not match the new reference will have uploads rejected from now on.")
        _audit(db, actor, "env_set", "tracks=%s changed=%s removed=%s acs=%s%s" % (
            ",".join(sorted(new_t)), ",".join(changed) or "-", ",".join(removed) or "-", env["acs"][:12],
            " (acs.exe changed)" if acs_changed else ""))
        return ctx.respond(200, {"ok": True, "tracks": sorted(new_t), "changed": changed, "removed": removed,
                                 "acs_changed": acs_changed, "warnings": warnings})

    if parts == ["config"]:
        key, val = data.get("key"), data.get("value")
        if key not in EDITABLE:
            raise ctx.HttpError(400, "key is not editable here (valid: %s)" % ", ".join(EDITABLE))
        typ, lo, hi = EDITABLE[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or (typ is int and val != int(val)) or not lo <= val <= hi:
            raise ctx.HttpError(400, "%s must be a %s between %s and %s" % (key, typ.__name__, lo, hi))
        old = cfg[key]
        cfg[key] = typ(val)
        ctx.set_config(db, cfg)
        _audit(db, actor, "config_set", "%s: %s -> %s" % (key, old, cfg[key]))
        return ctx.respond(200, {"ok": True, "key": key, "value": cfg[key]})

    raise ctx.HttpError(404, "not found")


# ------------------------------------------------------------------ entry point

def dashboard():
    nonce = secrets.token_urlsafe(16)
    body = (HERE / "admin.html").read_text(encoding="utf-8").replace("{{NONCE}}", nonce).encode("utf-8")
    csp = ("default-src 'none'; script-src 'nonce-%s'; style-src 'nonce-%s'; connect-src 'self'; "
           "base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % (nonce, nonce))
    return 200, body, [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))),
                       ("Content-Security-Policy", csp)] + SEC_HEADERS


def handle(environ, db, cfg, ctx):
    """Called by server.route for every /admin path. Returns (status, body, headers)."""
    s = settings()
    if not configured(s):
        raise ctx.HttpError(503, "admin interface is not configured")
    method, path = environ["REQUEST_METHOD"], environ.get("PATH_INFO", "")
    path = path.rstrip("/") or "/"
    now = int(time.time())
    query = {k: v[0] for k, v in urllib.parse.parse_qs(environ.get("QUERY_STRING", ""), max_num_fields=20).items()}

    if method == "GET" and path == "/admin/signed-out":
        return _message_page(200, "Signed out", "You are signed out of the AC Bench admin. You may still be signed in "
                             "to PocketID, in which case signing in again is instant.", ("/admin/login", "Sign in again"))

    if method == "GET" and path in ("/admin/login", "/admin/callback"):
        try:
            if path == "/admin/login":
                return start_login(db, s, now)
            return finish_login(db, s, environ, query, now)
        except LoginError as e:
            sys.stderr.write("admin login error: %s\n" % e.msg)  # visible in `docker logs`
            # 502/504 would be replaced by Cloudflare's own error page, hiding the reason; show it as a 500
            return _message_page(500 if e.code in (502, 504) else e.code, "Sign-in failed", e.msg)

    sess = current_session(db, environ, now)
    if method == "GET" and path == "/admin":
        return dashboard() if sess else _redirect("/admin/login")
    if not sess:
        raise ctx.HttpError(401, "not signed in")
    if path == "/admin/logout" and method == "POST":
        check_csrf(ctx, s, environ, sess)
        db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (sess["token_hash"],))
        db.commit()
        code, body, headers = ctx.respond(200, {"ok": True})
        return code, body, headers + [_cookie(s, SESSION_COOKIE, "", 0)]
    if path.startswith("/admin/api/"):
        if method != "GET":
            check_csrf(ctx, s, environ, sess)
        return api(db, cfg, ctx, sess, environ, method, path, now)
    raise ctx.HttpError(404, "not found")
