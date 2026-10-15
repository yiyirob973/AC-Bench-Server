# AC Bench Server

The cloud backend for AC Bench. It validates benchmark submissions, recomputes lap scores, merges community results, and publishes versioned class tables for the desktop app.

## For most users

If you want to benchmark cars, start with the [AC Bench Desktop app](https://github.com/yiyirob973/AC-Bench-Desktop).

This repository is the server backend. It is only needed if you are operating the shared class database or hosting the public benchmark service.

## Public access after deployment

Once the server is running, the public dashboard is available here:

- `http://<server-host>/`
- `http://<server-host>/stats`

The dashboard is a static HTML page served from the server root and is intended for operator-facing monitoring.

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
- Publishes versioned class tables for the desktop app to consume
- Stores database backups and server configuration for operator maintenance

## Workflow

1. Users run the desktop app to benchmark cars on fixed vanilla tracks
2. The desktop app uploads results to this server with a valid API key
3. The server validates the submission against the canonical environment
4. The server recomputes scores and merges data from multiple users
5. The server publishes a versioned class table
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

### Class tables

Class tables are versioned and published by the operator.

A published table includes:
- reference times per track
- cut points between classes
- class labels and distribution data
- metadata for variance and borderline flags

The desktop app downloads the latest version. Class membership remains fixed to the table version at publish time, rather than moving silently as more data arrives.

## Operational model

The server is intentionally simple and conservative:

- Python standard library only
- WSGI + SQLite
- no direct admin web UI
- no public registration or anonymous uploads
- designed for a community-scale deployment behind TLS

This keeps the server auditable and easy to run without a large infrastructure stack.

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
- `publish-classes [--include-provisional]`
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
python server.py publish-classes
```

## API

The server exposes a small JSON API.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Static stats dashboard |
| `GET` | `/stats` | Static stats dashboard |
| `GET` | `/v1/health` | Liveness check |
| `GET` | `/v1/config` | Accepted profile, limits, canonical environment, version info |
| `POST` | `/v1/register` | Register with an invite code |
| `POST` | `/v1/submit` | Upload benchmark results using a bearer token |
| `GET` | `/v1/classes` | Download the published class table and car data |
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
- versioned class tables

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
README.md
Dockerfile
server.py
stats.html
selftest.py
```

This repository is intentionally kept lean: server code and the public dashboard live at the root level, without a nested cloud_server directory.

## Relationship to the desktop app

The desktop app remains the main user-facing tool for benchmark runs and car analysis. This repo is the backend service that validates submissions, merges them, and publishes class tables.

## Summary

AC Bench Server is the validation and publication backend for the AC Bench ecosystem. It gives the desktop app a trustworthy way to share benchmark results, reconcile competing submissions, and publish shared class data across a community without exposing the project to anonymous or untrusted uploads.

For most users, start with the [AC Bench Desktop app](https://github.com/yiyirob973/AC-Bench-Desktop).


## Live car lookup

`POST /v1/cars/lookup` with `{"hashes": [...]}` (1-500 lowercase sha256 car physics hashes, no key needed) returns the
current records for just those cars: `{"profile", "tracks", "table_version", "labels", "cars": [...]}`. Unknown cars are
absent. Nothing is cached, so the answer is always current. Rate limit: `rate_lookups_per_hour` per IP (default 240,
editable in the admin dashboard).


## Class labels

Classes are named `1A` (top class), `1B` ... `1Z`, then `2A` ... `2Z`, `3A` ... Two-letter codes such as `AA` are no
longer used: searching for class "A" matched cars in other classes. Tags are `#Pride-1A`, `#Pride-1B`, ... and applying
tags replaces any older `#Pride-` tag (for example `#Pride-A`). The server renames the classes of already published
tables the same way, so no migration is needed.


## Submit response

`POST /v1/submit` answers with `accepted`, `rejected`, `merged_status` and now also `cars`: the current class record
(class, gap, status, flags, per-track times) of every car the submission touched, plus `tracks` and `table_version`.
The client never classifies; it shows what the server says.
