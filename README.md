# AC Bench Server

This repository contains the cloud backend for AC Bench. It accepts benchmark submissions from the desktop app, validates them, merges user results, and publishes versioned class tables that clients can download.

The desktop app is the benchmarking client. It runs on Windows, validates the vanilla track setup, benchmarks cars with AI-controlled laps, and uploads the results to this server. The server is the trust layer: it checks the environment, recomputes the score, verifies agreement between users, and publishes the class table.

## What the server does

- Accepts invite-only account registrations
- Validates uploaded benchmark submissions
- Verifies the benchmark environment against a canonical, tracked reference
- Recomputes lap scores on the server instead of trusting the client
- Merges submissions from multiple users into one agreed result per car and track
- Publishes versioned class tables for the desktop app to consume
- Stores database backups and server configuration for operator maintenance

## The desktop-to-server flow

The desktop app does the benchmark work. The server does the verification and publishing.

1. The desktop app benchmarks cars on fixed vanilla tracks using the AC Bench profile.
2. It computes lap times, tracks metadata, and environment attestation data.
3. The user uploads the result set to the server with a valid API key.
4. The server checks:
   - API key is valid and not banned
   - profile matches the accepted baseline
   - track is part of the configured benchmark set
   - environment matches the canonical vanilla reference
   - lap data is plausible and consistent
5. The server recomputes score values and merges submissions by car and track.
6. Verified data is used to generate or refresh class tables.
7. The desktop app downloads a new class table and updates class assignments locally.

This separation keeps the desktop app focused on measurement while the server becomes the authoritative source for shared class data.

## Server responsibilities in detail

### Accounts and access

- Invite-only registration
- One-time invite codes are issued by the operator
- Accounts have roles such as `user` and `trusted`
- API keys are stored as hashes, never as plain text
- Banned users are rejected from uploads

### Car identity

Car identity is based on the installed car physics, not the folder name.

The server hashes:
- `data.acd` or `data/`
- `collider.ini`

This prevents a renamed or updated car mod from inheriting a previous rating by name alone.

### Environment validation

Every upload includes an environment attestation. The server only accepts submissions from a canonical, operator-approved vanilla environment.

That canonical environment includes:
- track digests for the accepted benchmark tracks
- `acs.exe` hash
- benchmark baseline profile id

This is how the server prevents users from uploading results made under a different setup or modified game state.

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

The server groups submissions by:
- car hash
- track

Then it measures agreement between users.

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

## Docker deployment

The repository includes a Docker setup for running the server.

### Local test

```bash
python selftest.py
python server.py init
python server.py set-env --from track_check_log.json
python server.py invite --role user --uses 20
python server.py serve --port 8080
```

### Production-style run

```bash
docker build -f cloud_server/Dockerfile -t acbench-server .
docker run -d --name acbench -p 127.0.0.1:8080:8080 -v acbench-data:/data acbench-server
docker exec acbench python server.py init
docker exec -it acbench python server.py set-env --from /data/track_check_log.json
docker exec acbench python server.py invite --role user --uses 20
```

A reverse proxy such as Caddy or nginx should terminate TLS in front of the app.

## Server admin commands

All CLI entry points are:

- `serve [--host HOST] [--port PORT]`
- `init`
- `set-env --from FILE`
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

Server response includes accepted and rejected entries, with reasons for rejections.

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
cloud_server/
  Dockerfile
  README.md
  backup.sh
  docker-compose.yml
  selftest.py
  server.py
  import/
```

## Relationship to the desktop app

This server is not a standalone benchmarking tool. It is the backend for the AC Bench desktop app.

The desktop app is responsible for:
- benchmarking cars on fixed vanilla tracks
- validating the environment
- collecting lap data and classing cars
- generating class tables and campaign metadata
- uploading the results to this API

This server is responsible for:
- proving the environment is valid
- validating uploads
- merging and reconciling results from multiple users
- publishing trusted, versioned class tables

In other words: the desktop app measures; the server validates and agrees.

## Summary

AC Bench Server is the authority layer for the AC Bench ecosystem. It gives the desktop app a trustworthy way to share benchmark results, reconcile competing submissions, and publish shared class data across a community without exposing the project to anonymous or untrusted uploads.

This is the backend component of the AC Bench system; the desktop app and the Lua in-game logger are the client-side measurement tools that feed it.
