#!/usr/bin/env python3
"""Vanilla track check. Makes sure the tracks used for benchmarking are identical to the environment the server
publishes, so lap-time ratings from different AC installs stay comparable.

How it works
  * The server's canonical environment (GET /v1/conditions, "env") holds one digest per benchmark track folder and the
    SHA-256 of acs.exe. Players have no local reference file. Only the server operator creates one ("create", below).
  * "verify" hashes every file in content/tracks/<track>/ (all layouts, AI racing lines included), reduces them to the
    same digest the server uses and compares. It also hashes acs.exe, which holds the AI driving code. Exit code 0
    means identical.
  * The server only keeps fingerprints, so a mismatch can't list which files differ. To get back to vanilla, run
    Steam > Assetto Corsa > Properties > Installed Files > Verify integrity. That restores changed or deleted Kunos
    files but doesn't remove extra files you added to the track folders, so remove those yourself.
  * The digest is tried over the whole folder first. Only if that doesn't match for every track is it tried over the
    folder without its ui/ subfolder, so it follows however the server's environment was made. That's automatic, there
    is no setting for it.

Warnings only, they never block: CSP track config files in extension/config/tracks/ for these tracks.

  python ac_track_check.py verify --ac-dir "<AC>"
  python ac_track_check.py create --ac-dir "<AC>" [--out track_reference.json]   (operator: make the reference)
Exit codes: 0 identical / reference written, 2 different or incomplete install, 3 can't check (no usable data from the server).
"""
import argparse, configparser, hashlib, json, os, sys, time
from pathlib import Path

import ac_conditions
import ac_paths

JUNK = {"thumbs.db", "desktop.ini", ".ds_store"}          # OS clutter, not part of the game
COND_SECTIONS = ("DYNAMIC_TRACK", "WEATHER", "TEMPERATURE", "LIGHTING")


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_tree(root):
    files = {}
    for dp, dn, fn in os.walk(root):
        dn.sort()
        for n in sorted(fn):
            if n.lower() in JUNK:
                continue
            p = Path(dp) / n
            files[p.relative_to(root).as_posix()] = [p.stat().st_size, sha256_file(p)]
    return files


def file_info(p):
    return [Path(p).stat().st_size, sha256_file(p)]


def track_ids(specs):
    """'ks_vallelunga/club_circuit' becomes 'ks_vallelunga' (the whole track folder is checked)."""
    return list(dict.fromkeys(s.strip().partition("/")[0] for s in specs if s.strip()))


def csp_files(ac, track):
    base = Path(ac) / "extension" / "config" / "tracks"
    out = {}
    if base.is_dir():
        for p in sorted(base.iterdir()):
            if p.is_file() and p.name.lower().startswith(track.lower()):
                out[p.name] = [p.stat().st_size, sha256_file(p)]
    return out


def conditions(template):
    """Race sections of a race.ini. Kept for older callers; the server's conditions are what counts now."""
    cp = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
    cp.optionxform = str
    with open(template, encoding="utf-8", errors="ignore") as f:
        cp.read_file(f)
    return {s: dict(cp[s]) for s in COND_SECTIONS if s in cp}


def keep(files, ignore_ui):
    if not ignore_ui:
        return files
    return {p: v for p, v in files.items() if p.split("/")[0].lower() != "ui"}


def digest(files):
    h = hashlib.sha256()
    for p in sorted(files):
        h.update(f"{p}\t{files[p][0]}\t{files[p][1]}\n".encode())
    return h.hexdigest()


REFERENCE_FILE = ac_paths.DATA / "track_reference.json"


def create(ac, out=None, say=print, server=None):
    """For the server operator: build the canonical track reference (the environment) from this install.
    Which tracks to hash comes from the server (its published track list, no environment needed yet). The digests cover
    the whole track folders, exactly as verify() computes them, plus the SHA-256 of acs.exe. Writes `out` (default
    track_reference.json) as {"format": 1, "env": {profile, tracks, acs, ignore_ui}, ...}, which the server loads with
    python server.py set-env --from track_reference.json
    The install has to be vanilla (Steam > Verify integrity first, and remove extra files from the track folders).
    Returns {"status": ok | modified | noref, "path": ..., "env": ...}
    """
    out = Path(out) if out else REFERENCE_FILE
    result = {"status": "ok", "path": None, "env": None}
    try:
        profile, specs = ac_conditions.fetch_baseline_tracks(server)
    except ac_conditions.ConditionsUnavailable as e:
        say(str(e))
        result["status"] = "noref"
        return result
    ids = track_ids(specs)
    exe = Path(ac) / "acs.exe"
    if not exe.exists():
        say(f"acs.exe not found in {ac}")
        result["status"] = "modified"
        return result
    digests = {}
    for t in ids:
        tdir = Path(ac) / "content" / "tracks" / t
        if not tdir.is_dir():
            say(f"{t}: MISSING: folder not found: {tdir}")
            result["status"] = "modified"
            continue
        say(f"{t}: hashing every file ...")
        files = hash_tree(tdir)
        digests[t] = digest(files)
        ai = sum(1 for p in files if p.lower().endswith(".ai"))
        say(f"{t}: {len(files)} files, digest {digests[t][:12]}" + ("" if ai else "  WARNING: no .ai line files found"))
        if csp_files(ac, t):
            say(f"{t}: WARNING: CSP track config files exist; the reference should be made from a clean install")
    if result["status"] != "ok":
        say("Reference NOT written: fix the missing items above.")
        return result
    env = {"profile": profile, "ignore_ui": False, "tracks": digests, "acs": file_info(exe)[1]}
    doc = {"format": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "env": env}
    out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    result["path"], result["env"] = str(out), env
    say(f"Track reference written: {out}")
    say(f"  profile {profile} | tracks {', '.join(ids)} | acs.exe {env['acs'][:12]}")
    say(f"Load it on the server with:  python server.py set-env --from {out.name}")
    return result


def verify(ac, specs=None, ref_path=None, template=None, ignore_ui=False, say=print):
    """Check this install against the environment the server publishes.
    Returns {"status": ok | modified | noref, "tracks": {t: {"files": n, "digest": ...}}, "warnings": [...], ...}
    ref_path, template and ignore_ui are accepted for older callers and ignored, nothing here is chosen locally.
    Every track of the server's baseline is checked, whatever `specs` lists.
    """
    result = {"status": "ok", "tracks": {}, "warnings": [], "ignore_ui": False}
    try:
        cond = ac_conditions.load()
    except ac_conditions.ConditionsUnavailable as e:
        say(str(e))
        result["status"] = "noref"
        return result
    want = cond["env"]["tracks"]
    ids = track_ids(cond["tracks"])
    for t in track_ids(specs or []):
        if t not in ids:
            say(f"{t}: not part of the server's baseline ({', '.join(ids)}): refusing to check it.")
            result["status"] = "noref"
    if result["status"] != "ok":
        return result

    local = {}   # track id -> {"full": (files, digest), "noui": (files, digest), "ai": n}
    for t in ids:
        tdir = Path(ac) / "content" / "tracks" / t
        if not tdir.is_dir():
            say(f"{t}: MISSING: folder not found: {tdir}")
            result["status"] = "modified"
            continue
        say(f"{t}: hashing every file ...")
        files = hash_tree(tdir)
        noui = keep(files, True)
        local[t] = {"full": (len(files), digest(files)), "noui": (len(noui), digest(noui)),
                    "ai": sum(1 for p in files if p.lower().endswith(".ai"))}

    mode = None                       # None = whole folder, True = without ui/
    if len(local) == len(ids):
        for ui_mode in (False, True):
            key = "noui" if ui_mode else "full"
            if all(local[t][key][1] == want[t] for t in ids):
                mode = ui_mode
                break
    key = "noui" if mode else "full"
    result["ignore_ui"] = bool(mode)
    if (mode is None and len(local) == len(ids)
            and all(want[t] in (local[t]["full"][1], local[t]["noui"][1]) for t in ids)):
        result["status"] = "modified"
        say("The tracks match the server's environment, but not all in the same way (some only without their ui/ "
            "folder): treating the install as not vanilla.")
    for t in ids:
        if t not in local:
            continue
        n, dg = local[t][key]
        result["tracks"][t] = {"files": n, "digest": dg}
        if mode is None:   # no consistent match: judge each track on its own so only the bad one is flagged
            ok_t = want[t] in (local[t]["full"][1], local[t]["noui"][1])
        else:
            ok_t = dg == want[t]
        if ok_t:
            say(f"{t}: OK, {n} files identical to the server's environment (digest {dg[:12]})")
            if local[t]["ai"]:
                say(f"{t}: AI lines present ({local[t]['ai']} files, covered by the digest)")
            else:
                result["warnings"].append(f"{t}: no .ai line files found in the track, the AI may not drive it properly")
        else:
            result["status"] = "modified"
            say(f"{t}: NOT VANILLA: this install's digest {dg[:12]} differs from the server's {want[t][:12]}. "
                "The server only keeps a fingerprint, so the changed files cannot be listed: run Steam's "
                "'Verify integrity of game files' and remove any extra files you added to this track folder.")
        csp = csp_files(ac, t)
        if csp:
            result["warnings"].append(f"{t}: CSP track config files exist ({len(csp)}); they can change how the track or "
                                      "the AI behaves")
    if mode:
        result["warnings"].append("the track digests match the server's environment only without the ui/ folders "
                                  "(the server's environment ignores them)")

    exe = Path(ac) / "acs.exe"
    if exe.exists():
        sha = file_info(exe)[1]
        result["game"] = {"acs.exe": sha}
        if sha == cond["env"]["acs"]:
            say("acs.exe: OK, same game version as the server's environment (AI driving code)")
        else:
            say("acs.exe: DIFFERENT from the server's environment (other game version or patched). The AI is part of this "
                "file, so its behaviour may differ: ratings would not be comparable.")
            result["status"] = "modified"
    else:
        say("acs.exe: MISSING")
        result["status"] = "modified"
    for w in result["warnings"]:
        say("WARNING: " + w)
    return result


def make_env(result, profile):
    """Fingerprint of a passed check: which exact tracks and game executable the laps were driven on.
    Returns (short id, dict). It's uploaded with the results and the server compares it with its canonical one.
    """
    env = {"profile": profile, "ignore_ui": bool(result.get("ignore_ui")),
           "tracks": {t: v["digest"] for t, v in result["tracks"].items()},
           "acs": (result.get("game") or {}).get("acs.exe")}
    env_id = hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()[:16]
    return env_id, env


def append_log(path, result, env_id=None, env=None):
    """Keep a short history of checks next to the results (which install state was benchmarked)."""
    try:
        log = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else []
    except Exception:
        log = []
    log.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "status": result["status"],
                "ignore_ui": result.get("ignore_ui", False), "tracks": result["tracks"],
                "warnings": result["warnings"], "env_id": env_id, "env": env})
    Path(path).write_text(json.dumps(log, indent=1), encoding="utf-8")


def main():
    ac_paths.utf8_output()
    ap = argparse.ArgumentParser(description="Vanilla track check against the server's environment")
    ap.add_argument("action", choices=["verify", "create"])
    ap.add_argument("--ac-dir", required=True)
    ap.add_argument("--log", default=str(ac_paths.DATA / "track_check_log.json"),
                    help="append the result (with the environment id) to this log")
    # accepted and ignored so old command lines keep working; the baseline is never chosen here
    ap.add_argument("--out", help="create: where to write the reference (default track_reference.json)")
    ap.add_argument("--reference", help=argparse.SUPPRESS)
    ap.add_argument("--template", help=argparse.SUPPRESS)
    ap.add_argument("--ignore-ui-files", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--tracks", nargs="*", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.action == "create":
        return {"ok": 0, "modified": 2, "noref": 3}[create(args.ac_dir, args.out)["status"]]
    res = verify(args.ac_dir, args.tracks)
    env_id = env = None
    if res["status"] == "ok":
        env_id, env = make_env(res, ac_conditions.load()["profile"])
    append_log(args.log, res, env_id, env)
    return {"ok": 0, "modified": 2, "noref": 3}[res["status"]]


if __name__ == "__main__":
    sys.exit(main())
