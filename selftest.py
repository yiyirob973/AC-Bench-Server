#!/usr/bin/env python3
"""
Self-test for the AC Bench cloud server. Starts the real server on localhost with a temporary
database, simulates several users with the real client code, and checks the rules end to end.
Run it after any change:   python selftest.py          (prints PASS / FAIL per check)
"""
import re
import argparse, contextlib, csv, hashlib, io, json, random, sys, tempfile, threading
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import server as S          # noqa: E402
import ac_cloud as C        # noqa: E402
import ac_rating as R       # noqa: E402
import ac_baseline as bl    # noqa: E402
import ac_carid             # noqa: E402

PASSED, FAILED = [], []


def check(cond, label):
    (PASSED if cond else FAILED).append(label)
    print(("PASS  " if cond else "FAIL  ") + label)


def main():
    random.seed(5)
    tmp = Path(tempfile.mkdtemp(prefix="acb_selftest_"))
    db_path = tmp / "t.db"

    def admin(**kw):
        kw.setdefault("db", str(db_path))
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            S.cmd_admin(argparse.Namespace(**kw))
        return buf.getvalue()

    env = {"profile": bl.PROFILE_ID, "ignore_ui": False, "tracks": {"magione": "d" * 64, "ks_vallelunga": "e" * 64}, "acs": "f" * 64}
    env_id = hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()[:16]
    admin(command="init")
    (tmp / "canon.json").write_text(json.dumps([{"status": "ok", "env_id": env_id, "env": env}]))
    admin(command="set-env", source=str(tmp / "canon.json"))

    class Quiet(WSGIRequestHandler):
        def log_message(self, *a):
            pass

    srv = make_server("127.0.0.1", 0, S.create_app(str(db_path)), handler_class=Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % srv.server_address[1]

    # fake library: cars with physics files, so each has a real hash
    cars_dir = tmp / "ac" / "content" / "cars"
    tracks, best_t = bl.TRACKS, [90000.0, 70000.0]
    cars, n = {}, 0
    for centre, sd, count in [(0.0, 0.2, 6), (2.9, 0.3, 8), (9.0, 1.0, 14), (20.5, 0.7, 8)]:
        for _ in range(count):
            n += 1
            cid = "car_%02d" % n
            d = cars_dir / cid
            (d / "data").mkdir(parents=True)
            (d / "ui").mkdir()
            (d / "data" / "car.ini").write_text("MASS=%d" % (1000 + n))
            (d / "ui" / "ui_car.json").write_text(json.dumps({"name": cid.upper(), "specs": {"bhp": "300bhp", "weight": "1200kg"}}))
            base = max(0.0, random.gauss(centre, sd))
            cars[cid] = (base, base + random.gauss(0, 0.2))

    def laps(ms):
        return [int(ms * 1.07)] + [int(ms * (1 + random.gauss(0, 0.0005))) for _ in range(3)]

    def entries(shift=None, drop2=(), noise=0.0005):
        out = []
        for cid, deltas in cars.items():
            for ti, (t, d) in enumerate(zip(tracks, deltas)):
                if ti == 1 and cid in drop2:
                    continue
                ms = best_t[ti] * (1 + max(0, d) / 100) * (1 + random.gauss(0, noise)) * (1 + (shift or {}).get((cid, ti), 0.0))
                out.append({"car_id": cid, "car_hash": ac_carid.car_hash(cars_dir / cid), "name": cid.upper(),
                            "track": t, "laps_ms": laps(ms), "batch": 1, "env": env_id})
        return out

    def as_results(es):
        res = {}
        for e in es:
            res.setdefault(e["car_id"], {})[e["track"]] = {"status": "ok", "laps_ms": e["laps_ms"], "profile": bl.PROFILE_ID,
                                                          "batch": 1, "env": env_id, "car_hash": e["car_hash"]}
        return res

    def client(name):
        for attr, fn in (("CFG_FILE", "cfg"), ("UPLOADED", "up"), ("CLOUD_JSON", "cj"), ("CLOUD_CSV", "cc"), ("MISSING", "ms")):
            setattr(C, attr, tmp / ("%s_%s.json" % (name, fn)))

    def run(fn, **kw):
        kw.setdefault("server", url)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                fn(argparse.Namespace(**kw))
            except SystemExit as e:
                print("EXIT:", e)
        return buf.getvalue()

    def api(name, method, path, body=None, auth=True, etag=None):
        client(name)
        cfg = C.settings(argparse.Namespace(server=url))
        return C.call(cfg, method, path, body, auth=auth, etag=etag)

    (tmp / "log.json").write_text(json.dumps([{"status": "ok", "env_id": env_id, "env": env}]))

    # 1. accounts
    code = admin(command="invite", role="user", uses=2).split(":")[1].split()[0]
    tcode = admin(command="invite", role="trusted", uses=1).split(":")[1].split()[0]
    for who in ("A", "B"):
        client(who)
        check("Registered as" in run(C.cmd_register, invite=code, name="user_" + who), "invite registers user " + who)
    client("X")
    check("invalid or used-up" in run(C.cmd_register, invite=code, name="user_X"), "a used-up invite is refused")
    client("T")
    check("role trusted" in run(C.cmd_register, invite=tcode, name="modder_T"), "trusted invite creates a trusted user")

    # 2. one submission = provisional, resubmitting unchanged results is skipped
    esA = entries()
    (tmp / "A.json").write_text(json.dumps(as_results(esA)))
    client("A")
    out = run(C.cmd_upload, results=str(tmp / "A.json"), log=str(tmp / "log.json"), cars_dir=str(cars_dir), all=False)
    check("accepted %d of %d" % (len(esA), len(esA)) in out and '"provisional": %d' % len(esA) in out, "first upload is accepted and provisional")
    check("Nothing new" in run(C.cmd_upload, results=str(tmp / "A.json"), log=str(tmp / "log.json"), cars_dir=str(cars_dir), all=False),
          "unchanged results are not re-uploaded")

    # 3. a second user agrees (verified), deviating results are disputed
    shift = {("car_03", 0): 0.02, ("car_10", 1): 0.02, ("car_20", 0): -0.02}
    esB = entries(shift=shift, drop2=("car_05", "car_06"))
    (tmp / "B.json").write_text(json.dumps(as_results(esB)))
    client("B")
    out = run(C.cmd_upload, results=str(tmp / "B.json"), log=str(tmp / "log.json"), cars_dir=str(cars_dir), all=False)
    check('"verified"' in out and '"disputed"' in out, "agreeing submissions verify, disagreeing ones are disputed")
    stats = admin(command="stats")
    check("disputed     3" in stats or "disputed     4" in stats or "disputed     5" in stats, "the three forced deviations are disputed")

    # 3b. results benchmarked before environments were stamped (no env, no car hash): need attestation
    lcode = admin(command="invite", role="user", uses=1).split(":")[1].split()[0]
    client("L")
    run(C.cmd_register, invite=lcode, name="user_L")
    legacy_ids = ("car_11", "car_12", "car_13", "car_14")
    legacy = {}
    for e in esA:
        if e["car_id"] in legacy_ids:
            legacy.setdefault(e["car_id"], {})[e["track"]] = {"status": "ok", "laps_ms": e["laps_ms"], "profile": bl.PROFILE_ID, "batch": 1}
    (tmp / "L.json").write_text(json.dumps(legacy))
    old_ok = {"status": "ok", "ignore_ui": False, "tracks": {"magione": {"files": 5, "digest": "d" * 64},
                                                              "ks_vallelunga": {"files": 3, "digest": "e" * 64}}}
    cur = {"status": "ok", "env_id": env_id, "env": env}
    (tmp / "logL.json").write_text(json.dumps([old_ok, old_ok, cur]))
    out = run(C.cmd_upload, results=str(tmp / "L.json"), log=str(tmp / "logL.json"), cars_dir=str(cars_dir), all=False)
    check("before the cloud update" in out and "Nothing new" in out, "old results are not uploaded without attestation (and the reason is explained)")
    out = run(C.cmd_upload, results=str(tmp / "L.json"), log=str(tmp / "logL.json"), cars_dir=str(cars_dir), all=False, attest_legacy=True)
    check("accepted 8 of 8" in out, "old results are attested and accepted when the older checks match the current environment")
    changed = dict(old_ok, tracks={"magione": {"files": 5, "digest": "0" * 64}, "ks_vallelunga": {"files": 3, "digest": "e" * 64}})
    (tmp / "logL2.json").write_text(json.dumps([changed, cur]))
    out = run(C.cmd_upload, results=str(tmp / "L.json"), log=str(tmp / "logL2.json"), cars_dir=str(cars_dir), all=True, attest_legacy=True)
    check("track was different at benchmark time" in out, "attestation is refused when the track changed since the benchmark")
    (tmp / "logL3.json").write_text(json.dumps([cur]))
    out = run(C.cmd_upload, results=str(tmp / "L.json"), log=str(tmp / "logL3.json"), cars_dir=str(cars_dir), all=True, attest_legacy=True)
    check("no earlier passing vanilla check" in out, "attestation is refused when there is no earlier vanilla check on record")
    (tmp / "logL4.json").write_text(json.dumps([old_ok]))
    out = run(C.cmd_upload, results=str(tmp / "L.json"), log=str(tmp / "logL4.json"), cars_dir=str(cars_dir), all=True, attest_legacy=True)
    check("no current vanilla check" in out, "attestation is refused when there is no current verified environment")

    # 4. rejections
    good = esA[0]
    bad_env_id = "ab" * 8

    def post(changes=None, envs=None, profile=None, auth=True):
        e = dict(good)
        e.update(changes or {})
        code, r, _ = api("A", "POST", "/v1/submit", {"profile": profile or bl.PROFILE_ID,
                                                    "environments": {env_id: env} if envs is None else envs, "results": [e]}, auth=auth)
        return code, ((r.get("rejected") or [{}])[0].get("reason", "") if code == 200 else (r or {}).get("error", ""))

    cases = [
        ("modified track", post({"env": bad_env_id}, {bad_env_id: dict(env, tracks={"magione": "0" * 64, "ks_vallelunga": "e" * 64})}), "not identical"),
        ("other acs.exe", post({"env": bad_env_id}, {bad_env_id: dict(env, acs="1" * 64)}), "acs.exe"),
        ("ui files ignored", post({"env": bad_env_id}, {bad_env_id: dict(env, ignore_ui=True)}), "UI files"),
        ("no environment", post({"env": None}), "attestation"),
        ("shared track (batch 2)", post({"batch": 2}), "shared the track"),
        ("inconsistent laps", post({"laps_ms": [99000, 90000, 99000, 80000]}), "inconsistent"),
        ("implausible time", post({"laps_ms": [1000, 900, 900, 900]}), "plausible"),
        ("too few laps", post({"laps_ms": [99000, 90000, 90000]}), "at least"),
        ("unknown track", post({"track": "monza"}), "baseline"),
        ("bad hash", post({"car_hash": "xyz"}), "car_hash"),
        ("non-numeric lap", post({"laps_ms": [99000, 90000, 90000, "x"]}), "bad lap"),
    ]
    for label, (code, reason), needle in cases:
        check(code == 200 and needle in reason, "rejected: " + label)
    check(post(profile="baseline-v0")[0] == 422, "rejected: other baseline profile (422)")
    check(post(auth=False)[0] == 401, "rejected: no API key (401)")
    check(api("A", "POST", "/v1/submit", {"profile": bl.PROFILE_ID, "environments": {}, "results": [dict(good)] * 501})[0] == 413,
          "rejected: more than 500 entries (413)")

    # 5. class table, sync onto a library, same answer as the local algorithm
    out = admin(command="publish-classes", include_provisional=False)
    check("class table v1" in out, "class table is published")
    client("T")
    out = run(C.cmd_sync, cars_dir=str(cars_dir), include_provisional=False)
    rows = list(csv.DictReader(open(C.CLOUD_CSV)))
    check(len(rows) >= 25 and {"class", "id", "gap_pct", "score_pct", "flags", "bop"} <= set(rows[0]), "sync writes a class CSV the grid builder can read")
    check(set(C.MISSING.read_text().split()) >= {"car_03", "car_10", "car_20"}, "disputed cars are listed as missing a class")
    data = json.loads(C.CLOUD_JSON.read_text())
    by_id = {c["car_id"]: c for c in data["cars"]}
    verified = {cid: {t: {"status": "ok", "best_ms": c["times_ms"][t]} for t in tracks} for cid, c in by_id.items() if c["status"] == "verified"}
    local, _ = R.rate(verified, tracks, class_gap=2.0, min_size=3)
    check(all(local[cid]["cls"] == by_id[cid]["class"] for cid in local), "server classes equal the local ac_rating classes")

    # 6. a car is identified by its physics: a changed car is a different car
    d = cars_dir / "car_01" / "data" / "car.ini"
    old = d.read_text()
    d.write_text("MASS=1")
    run(C.cmd_sync, cars_dir=str(cars_dir), include_provisional=False)
    check("car_01" in C.MISSING.read_text().split(), "a modified car does not inherit the original car's class")
    d.write_text(old)

    # 7. classes stay put when other cars arrive
    before = {cid: c["class"] for cid, c in by_id.items()}
    newdir = cars_dir / "car_new"
    (newdir / "data").mkdir(parents=True)
    (newdir / "data" / "car.ini").write_text("MASS=777")
    newh = ac_carid.car_hash(newdir)
    fast = [{"car_id": "car_new", "car_hash": newh, "name": "NEW", "track": t, "laps_ms": laps(best_t[i] * 0.97),
             "batch": 1, "env": env_id} for i, t in enumerate(tracks)]
    for who in ("A", "B"):
        _, resp, _ = api(who, "POST", "/v1/submit", {"profile": bl.PROFILE_ID, "environments": {env_id: env}, "results": fast})
    mine = {c["car_id"]: c for c in resp.get("cars", [])}
    check(set(mine) == {"car_new"} and re.fullmatch(r"\d+[A-Z]", mine["car_new"].get("class") or "") is not None,
          "the submit response carries the server's class for the car just uploaded")
    after = {c["car_id"]: c for c in api("T", "GET", "/v1/classes")[1]["cars"]}
    check(all(after[cid]["class"] == before[cid] for cid in before), "existing cars keep their class when a new car is added")
    check(after["car_new"]["class"] == "1A" and after["car_new"]["status"] == "verified", "the faster new car lands in class 1A")
    _, _, hd = api("T", "GET", "/v1/classes")
    check(api("T", "GET", "/v1/classes", etag=hd["ETag"])[0] == 304, "unchanged data answers 304 (ETag)")

    # 8. trusted user counts alone; banning a user recomputes
    check(api("T", "GET", "/v1/cars/" + newh)[1]["car"]["status"] == "verified", "single-car lookup works")
    admin(command="ban", name="user_B")
    st = {c["car_id"]: c["status"] for c in api("T", "GET", "/v1/classes")[1]["cars"]}
    check(st["car_new"] == "provisional" and st["car_01"] == "provisional", "banning a user drops their confirmations")
    admin(command="unban", name="user_B")
    st = {c["car_id"]: c["status"] for c in api("T", "GET", "/v1/classes")[1]["cars"]}
    check(st["car_new"] == "verified", "unbanning restores them")

    # 9. rate limit
    admin(command="config", action="set", key="rate_submits_per_hour", value="3")
    codes = [api("A", "POST", "/v1/submit", {"profile": bl.PROFILE_ID, "environments": {env_id: env}, "results": [dict(good)]})[0] for _ in range(3)]
    check(429 in codes, "submissions are rate limited (429)")

    srv.shutdown()
    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    for f in FAILED:
        print("  FAILED:", f)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
