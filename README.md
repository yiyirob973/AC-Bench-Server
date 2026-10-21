# AC Bench Server

The cloud backend for AC Bench. It validates benchmark submissions, recomputes lap scores, merges community results, and keeps a live class table that tells the desktop app which class each car is in.

## For most users

If you want to benchmark cars, start with the [AC Bench Desktop app](https://github.com/yiyirob973/AC-Bench-Desktop).

This repository is the server backend. It is only needed if you are operating the shared class database or hosting the public benchmark service.

## Public access after deployment

Once the server is running, the public dashboard is available here:

- `http://<server-host>/`
- `http://<server-host>/stats`

The page lists the cars that were added most recently first, with a tab for all cars, class filters, search and a CSV
download. It reads `/v1/classes` from the same server. To point a copy of the page at another server, open it with
`?server=https://other-host` (that server then has to allow cross-origin reads).

The JSON API remains under:

- `http://<server-host>/v1/health`
- `http://<server-host>/v1/config`
- `http://<server-host>/v1/classes`

## What the server does

- Accepts invite-only account registrations
- Validates uploaded benchmark submissions
- Verifies the benchmark environment against a canonical tracked reference
- Recomputes lap scores on the server instead of trusting the client
- Merges submissions from multiple users into one agreed result per car and track
- Keeps a live class table, rebuilt from the merged times, for the desktop app to consume
- Stores database backups and server configuration for operator maintenance

## Workflow

1. Users run the desktop app to benchmark cars on fixed vanilla tracks
2. The desktop app uploads results to this server with a valid API key
3. The server validates the submission against the canonical environment
4. The server recomputes scores and merges data from multiple users
5. The class table follows the merged times by itself, and the answer to the upload says which class each car is in
6. Users download the table and see cars assigned to classes

## Server responsibilities in detail

### Accounts and access

- Invite-only registration
- One-time invite codes issued by the operator
- Accounts have roles: `user` or `trusted`
- API keys stored as hashes, never plain text
- Banned users are rejected from uploads

### Car identity

Car identity is based on the installed car physics, not the folder name.

The server hashes:
- `data.acd` or `data/` (all files)
- `collider.ini`

This prevents a renamed or updated car mod from inheriting a previous rating by name alone.

### Environment validation

Every upload includes an environment attestation. The server only accepts submissions from a canonical operator-approved vanilla environment.

The canonical environment includes:
- track digests for accepted benchmark tracks
- `acs.exe` hash
- benchmark baseline profile id

This prevents users from uploading results made under a different setup or modified game state.

### Validation rules

Each upload is rejected unless it satisfies all relevant checks:
- valid API key
- baseline profile matches the server
- valid track and lap count
- batch size is within limits
- lap times are plausible and consistent
- environment attestation matches the canonical server environment

The score is always recomputed on the server. Client-reported values are not trusted.

### Agreement and merged status

The server groups submissions by car hash and track, then measures agreement between users.

Status outcomes:
- `verified`: enough independent agreement or a trusted user confirms it
- `provisional`: only one user has uploaded data so far
- `disputed`: submissions disagree and are not treated as definitive yet

### Time table and class table

The two are kept apart:

- The **time table** (`merged`) holds one agreed lap time per car and track, plus the log of that time. Nothing is
  stored per car beyond that: a car's rating is worked out from its own lap times whenever it is needed (the mean of
  ln(lap time) over the benchmark tracks), so an upload only touches the cars it is about.
- The **class table** is a single live row: the cut points between classes on that rating scale, the class labels and
  each class's average lap time per track (used for the balance hints). It also holds the score of the fastest rated
  car. Every upload checks whether one of its cars beats that score and lowers it if so.

The class table is rebuilt from the time table by a background job shortly after results change (at most every
`class_refresh_seconds`, default 10). The database averages each car's lap times and counts the cars into small score
buckets with grouped queries, and the border search runs on those buckets, so the search takes the same time for 5,000
cars as for 100,000. Reading the lap rows still grows with the number of cars, though only at database speed (roughly
0.1 s at 5,000 cars and 0.6 s at 100,000 in tests). Borders are found to within 1/20 of a class width.

Asking for a car's class is a lookup of its rating among the cut points, so it costs the same whatever the number of
cars, and an upload answers with the class straight away. The numbers shown (`gap_pct`, which `score_pct` repeats, and
the per-track `deltas`) are measured from the fastest rated car and the fastest laps right now, so the fastest car is
at 0% the moment it is uploaded and nothing is ever negative. If the fastest car is later banned or disputed, the
numbers stay slightly high until the next rebuild sets the fastest score again. When a faster car arrives the class
borders move at the next rebuild; the class table's `version` goes up only when its borders or labels actually change,
so a client can compare it to know whether classes may have moved.

### Flags on a car

A car's record can carry flags, shown on the stats page and returned in `flags`:

- **borderline 1B/1C (0.07%)**: the car sits within `tie_zone` percent of a class border, so a small change could move it.
- **high variance (12.9%)**: the car's balance between tracks is unusual for its class. For each track the server takes the
  car's lap time against the class's average lap time on that track, then looks at how far apart those comparisons are.
  A slow car that is slow in the usual way for its class scores about 0 however far behind the fastest car it is, so the
  flag does not just mark slow cars. The limit is the `variance_limit_pct` setting (default 3). The value is returned as
  `variance_pct`. A class with only one or two cars has little to compare against, so the flag is weak there.

Both flags are advice for balancing a grid. They do not change whether a car is verified: that depends only on
`tolerance_pct` and `min_confirmations`. Changing `variance_limit_pct` takes effect at the next class table rebuild
(within `class_refresh_seconds`) and does not change the class table's `version`.

## Operational model

The server is intentionally simple and conservative:

- Python standard library only
- WSGI + SQLite
- no direct admin web UI
- no public registration or anonymous uploads
- designed for a community-scale deployment behind TLS

This keeps the server auditable and easy to run without a large infrastructure stack.

### Performance and concurrency

- Each worker thread keeps one long-lived SQLite connection (WAL, `synchronous=NORMAL`). A power cut can lose the
  last few commits but never corrupts the database; keep the daily backups.
- The class table, `/v1/conditions` document and `/stats` page are cached per process and revalidated cheaply, so
  unchanged requests cost almost nothing. `/v1/classes` and `/stats` are gzip-compressed (`Accept-Encoding: gzip`)
  and support `If-None-Match` (strong or weak ETags).
- While one thread rebuilds the `/v1/classes` export, other requests are served the previous export (at most one
  rebuild old); with `export_max_age` 0 or when the class table was just rebuilt they wait for the fresh one.
- Uploads are validated before the database write lock is taken, then written in one short transaction.
- Scale with the compose/Docker environment: `WEB_CONCURRENCY` (worker processes, default 2) and
  `GUNICORN_CMD_ARGS` (default `--worker-class gthread --threads 8 ...`). Roughly one worker per CPU core.
- If you restore a database file by hand, restart the container (workers keep their connections open).

## Security and deployment notes

- TLS is mandatory for any non-local deployment
- API keys travel in an authorization header
- registration and submit endpoints are rate-limited
- reverse proxy headers can be trusted only if the proxy is controlled by the operator
- the database should be backed up regularly
- the server is meant to sit behind a TLS reverse proxy; do not expose raw port 8080 publicly

## Docker / Compose deployment

The recommended deployment is Docker Compose. The compose file builds the server directly from the GitHub repository, so you do not need a nested repo structure or a local checkout of the full server code.

### Option 1: Download the compose file and run it

```bash
mkdir acbench-server && cd acbench-server
curl -L https://raw.githubusercontent.com/yiyirob973/AC-Bench-Server/main/docker-compose.yml -o docker-compose.yml
docker compose up -d --build
```

Then open:

```text
http://127.0.0.1:8080/
http://127.0.0.1:8080/stats
```

### Option 2: Clone the repo and run locally

```bash
git clone https://github.com/yiyirob973/AC-Bench-Server.git
cd AC-Bench-Server
docker compose up -d --build
```

This uses the repository's `Dockerfile` and exposes the app on port 8080. The compose file keeps the database in a Docker volume and mounts `./import` as read-only so you can drop files like `track_check_log.json` into it before running administrative commands.

### Useful commands

```bash
docker compose logs -f acbench
docker compose ps
docker compose down
docker volume inspect acbench-data
```

### Production-style run

For real deployments, keep the container behind a reverse proxy that terminates TLS. The server itself should not be exposed directly on port 8080 unless you are only testing locally.

## Server admin commands

All CLI entry points are:

- `serve [--host HOST] [--port PORT]`
- `init`
- `set-env --from FILE` (also available in the admin dashboard: "Canonical environment" section, upload or paste the track reference)
- `invite [--role user|trusted] [--uses N]`
- `create-user NAME [--role user|trusted]`
- `ban NAME`
- `unban NAME`
- `backup --to PATH`
- `recompute-all`
- `recalc-classes` (rebuild the class table right now; the server also does it by itself)
- `stats`
- `disputed`
- `config get`
- `config set KEY JSON_VALUE`

Example:

```bash
python server.py init
python server.py set-env --from track_check_log.json
python server.py invite --role user --uses 20
python server.py serve --port 8080
python server.py recalc-classes
```

## API

The server exposes a small JSON API.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Stats page |
| `GET` | `/stats` | Stats page |
| `GET` | `/v1/health` | Liveness check |
| `GET` | `/v1/config` | Accepted profile, limits, canonical environment, version info |
| `POST` | `/v1/register` | Register with an invite code |
| `POST` | `/v1/submit` | Upload benchmark results using a bearer token |
| `GET` | `/v1/classes` | Download the live class table and car data (each car has `added`, when it first appeared, and `updated`) |
| `GET` | `/v1/cars/<car_hash>` | Fetch a single car record |

Upload payloads include:
- `profile`
- `environments`
- `results`

Server responses include accepted and rejected entries, with reasons for rejections.

## Privacy and data handling

The server stores:
- user names
- hashed API keys
- uploaded results
- rate-limit events
- the live class table

Rate-limit events are short-lived and are purged after a period of time.

## Honest limits

- Results are self-reported by users and therefore never perfectly trustless.
- The environment digests are public, so a determined cheater could try to forge a matching attestation.
- Defenses include invite-only access, agreement checks, trusted roles, plausibility checks, and bans.
- Colluding accounts can still agree on fake data, so access must be restricted to trusted users.
- The server is designed for a community-scale deployment, not for heavy industrial throughput.
- Validation values such as tolerance and spread thresholds may need tuning after real-world data is collected.

## Repository layout

```text
server.py            the API, the merging and the command line
admin.py             /admin: PocketID login and the operator dashboard's API
admin.html           the admin dashboard page
ac_rating.py         cuts the class table (natural breaks) and names the classes
stats.html           the public /stats page
Dockerfile
docker-compose.yml
backup.sh            daily database backup, meant for cron
README.md
```

Everything sits at the root of the repository, and the server needs nothing beyond the Python standard library (gunicorn is added in the Docker image).

## Relationship to the desktop app

The desktop app remains the main user-facing tool for benchmark runs and car analysis. This repo is the backend service that validates submissions, merges them, and keeps the class table.

## Summary

AC Bench Server is the validation and sharing backend for the AC Bench ecosystem. It gives the desktop app a trustworthy way to share benchmark results, reconcile competing submissions, and keep shared class data for a community without exposing the project to anonymous or untrusted uploads.

For most users, start with the [AC Bench Desktop app](https://github.com/yiyirob973/AC-Bench-Desktop).


## Live car lookup

`POST /v1/cars/lookup` with `{"hashes": [...]}` (1-500 lowercase sha256 car physics hashes, no key needed) returns the
current records for just those cars: `{"profile", "tracks", "table_version", "labels", "cars": [...]}`. Unknown cars are
absent. Nothing is cached, so the answer is always current. Rate limit: `rate_lookups_per_hour` per IP (default 240,
editable in the admin dashboard), counted in memory by each worker process (so with N workers the effective
limit can reach N x that).


## Class labels

Classes are named `1A` (top class), `1B` ... `1Z`, then `2A` ... `2Z`, `3A` ... Two-letter codes such as `AA` are no
longer used: searching for class "A" matched cars in other classes. Tags are `#Pride-1A`, `#Pride-1B`, ... and applying
tags replaces any older `#Pride-` tag (for example `#Pride-A`). The live class table always uses these names.


## Submit response

`POST /v1/submit` answers with `accepted`, `rejected`, `merged_status` and now also `cars`: the current class record
(class, gap, status, flags, per-track times) of every car the submission touched, plus `tracks` and `table_version`.
The client never classifies; it shows what the server says.

## Invite codes

Codes look like `54TE-7JWY-TSXD-8T9K` (16 characters, no look-alikes such as 0/O or 1/I/L, no `_`). Registration accepts
them as pasted or typed: lowercase, without the hyphens, with surrounding spaces/newlines/quotes. Codes made by older
versions still work exactly as they were issued. The error says whether the code is unknown (incomplete copy, or a
different server than the one that issued it) or has been used up or revoked.


## Same account on another device

There is no password: an account is an API key kept in the app's `cloud_config.json`. To use the account on another PC,
the signed-in app calls `POST /v1/devices/link` and gets a single-use code (`XXXX-XXXX-XXXX`, valid 10 minutes, only the
newest code works). The new device sends it to `POST /v1/devices/redeem` and receives its own API key for the same
account (stored in `user_keys`, so every device has a separate key). Banning the account disables all its devices; the
admin users table shows how many devices an account has.
