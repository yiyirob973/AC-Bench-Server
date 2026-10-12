# AC Bench cloud server

Stores benchmark lap times and the classes derived from them. Users benchmark cars with the desktop
tool and upload; the server validates every entry, merges the submissions of different users into one
agreed time per car and track, and publishes the classes for download. Python standard library only
(WSGI + SQLite). Tested with `selftest.py` (39 checks), not yet with real users or real traffic.

## How it works

| Piece | Rule |
|---|---|
| Accounts | Invite-only. The operator mints invite codes; a user registers with one and gets a personal API key (stored hashed). Roles: `user`, `trusted` (e.g. mod creators). |
| Car identity | The hash of the car's physics files (`data.acd` / `data/`, `collider.ini`), not the folder name. A modified or updated car is a different car and never inherits a class. |
| Environment | Every result carries the id of a passed vanilla track check: digests of the benchmark tracks (AI lines included) and of `acs.exe`. The server only accepts results whose digests equal its canonical environment. |
| Validation | Baseline profile, tracks, lap count, `batch <= max_batch`, plausible and consistent laps. The score (median of the timed laps) is recomputed on the server; the client's numbers are never trusted. |
| Agreement | One entry per user per car per track. Submissions within `tolerance_pct` of each other confirm one another: 2+ agreeing users, or 1 trusted user = `verified`; one user = `provisional`; users that disagree = `disputed` (ignored until more data arrives). |
| Classes | A versioned class table (reference times + cut points, found with the natural-breaks algorithm in `ac_rating.py`) is published by the operator. A car's class is a lookup in the table, so **classes do not move when other cars are added**. A new table is a deliberate event. |
| Flags | `borderline` (within the tie zone of a class border), `high variance` (track deltas differ by more than the class spread) with the slowdown a BoP would need. |

## Honest limits

* Results are self-reported. The environment digests are public, so a determined cheater can forge an
  attestation. Defences: invite-only accounts, agreement between independent users, trusted roles,
  plausibility checks, bans. Colluding accounts can still agree on a fake time; keep invites to people you trust
  and review `disputed` and new classes.
* Whether the AI produces the same lap times on different PCs (CPU, frame rate, CSP version) is **not yet
  measured**. `tolerance_pct` (0.5) and `spread_limit_pct` (1.5) are starting guesses: after the first real
  multi-user data, look at `python server.py disputed` and tune them with `config set`.
* A single server process on SQLite is fine for a community; it is not built for heavy load.

## Operator quick start (local test)

```
python selftest.py                                   # should print "39 passed, 0 failed"
python server.py init
python server.py set-env --from track_check_log.json  # see below
python server.py invite --role user --uses 20         # prints a code once; hand it out
python server.py serve --port 8080
```

**Canonical environment.** On an install that is verified vanilla (Steam "Verify integrity", then create the
reference in the desktop tool's *Vanilla check* tab), press *Verify tracks now*. That appends an entry with
the environment to `track_check_log.json`. `set-env --from` that file stores its track digests and the
`acs.exe` hash as the only environment the server accepts. Do this on the maintainer's machine only.

**First class table.** Once enough cars are `verified` (`python server.py stats`), run
`python server.py publish-classes` (add `--include-provisional` to bootstrap from single submissions).
Classes need at least 2 cars with complete data on every track. Re-run it only when you want to change the
class boundaries; the version number goes up and clients pick it up on their next download.

## Production

Docker (build context = the `ac_bench` folder):

```
docker build -f cloud_server/Dockerfile -t acbench-server .
docker run -d --name acbench -p 127.0.0.1:8080:8080 -v acbench-data:/data acbench-server
docker exec acbench python server.py init
docker exec -it acbench python server.py set-env --from /data/track_check_log.json
docker exec acbench python server.py invite --role user --uses 20
```

TLS is mandatory (API keys travel in a header). Caddy example:

```
acbench.example.org {
    reverse_proxy 127.0.0.1:8080
    request_body { max_size 2MB }
    encode gzip
}
```

Set `config set trust_proxy_header true` only when the proxy is yours, so the registration rate limit sees real
client addresses (the server uses the LAST address in `X-Forwarded-For`, the one your proxy added). Back up `acbench.db` (`sqlite3 acbench.db ".backup backup.db"`). Several gunicorn workers on one
SQLite file (WAL mode) are fine at this scale.

## Admin commands

`init`, `set-env`, `invite [--role user|trusted] [--uses N]`, `create-user NAME [--role]`, `ban NAME`, `unban NAME`,
`recompute-all`, `publish-classes [--include-provisional]`, `stats`, `disputed`, `config get`,
`config set KEY JSON_VALUE` (keys: `profile, tracks, laps, max_batch, tolerance_pct, min_confirmations,
spread_limit_pct, plaus_*, max_entries, rate_*_per_hour, trust_proxy_header, class_gap, min_class_size, tie_zone`).
Admin actions run locally on the database; there is no admin web interface to attack.

## API (JSON, `Authorization: Bearer <key>` where marked)

| Method and path | Purpose |
|---|---|
| `GET /v1/health` | liveness |
| `GET /v1/config` | accepted profile, tracks, limits, canonical environment, class table version |
| `POST /v1/register` | `{invite, name}` -> `{api_key}` (rate limited per IP) |
| `POST /v1/submit` (key) | `{profile, environments:{id:{...}}, results:[{car_id, car_hash, name, track, laps_ms, batch, env}]}` (max 500 entries, 2 MB) -> `{accepted, rejected:[{index, reason}], merged_status}` |
| `GET /v1/classes` | class table + every car with status, class, times, flags (ETag / `If-None-Match` supported) |
| `GET /v1/cars/<car_hash>` | one car |

Privacy: the server stores user names, hashed API keys, submissions, and short-lived rate-limit events (client
address for registration; purged after 24 h).
