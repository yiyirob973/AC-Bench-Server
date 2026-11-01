<p align="center">
  <img src="branding/ac-bench.png" alt="AC Bench logo" width="140">
</p>

<h1 align="center">AC Bench Server</h1>

<p align="center">
  Shared class ratings for Assetto Corsa cars. Collect benchmark results, validate them, and tell the app which class each car belongs to.
</p>

---

AC Bench ranks Assetto Corsa cars by how fast they are on a standard benchmark, so players can build evenly matched grids instead of guessing from mods and car names.

This repository runs the server that stores those results, checks them for consistency, and publishes the class table. The desktop app uploads lap times and asks the server which class each car belongs to.

## Public server

A public instance is available here:

- https://bench.cauch.uk

If you just want to benchmark cars and see their classes, you probably do not need this repository. Install the desktop app, use the public server, and register with the invite code shown in the app.

## Who this repo is for

This project is for people who want to run their own AC Bench server, for example:

- a private community with its own rating list
- a group wanting a shared, controlled class table
- a maintainer who wants to host the backend and admin tools

If you only want to use AC Bench as a player, use the desktop app and connect to an existing server.

## What the server does

- Accepts sign-ups by invite code only
- Validates every uploaded benchmark result
- Recalculates car scores from the raw lap times
- Combines results from multiple users into a single class table
- Publishes a public page of cars and their classes
- Provides a dashboard for admin operations
- Answers desktop app lookups for car classes and tags

## How a result becomes a class

1. Someone runs a benchmark in the desktop app.
2. The app uploads the result alongside its personal key.
3. The server validates the upload: key validity, benchmark profile, track setup, lap plausibility, and consistency checks.
4. The server recalculates the score from the lap times and compares it with other uploads for that same car.
5. Each car ends up in one of three states:
   - verified: enough different users agree, or a trusted user confirms it
   - provisional: only one user has uploaded it so far
   - disputed: the uploads disagree, so the result is not rated yet
6. Cars are ranked by lap time and split into class bands at natural gaps.

Classes are named `1A`, `1B`, `1C`, and so on, moving up to `1Z`, then `2A`, `2B`, and so on. The desktop app writes these into car tags as `#Pride-1A`, `#Pride-1B`, and so on.

A car's rating is expressed relative to the fastest car in the table, so the fastest car sits at `0%` and slower cars are measured by how far behind it they are.

### Flags

A car may carry a flag, which is advisory for building grids and does not affect whether it is verified.

- `borderline`: the car is very close to the edge of its class
- `high variance`: the car's performance varies unusually across tracks

## Accounts and permissions

- Sign-up requires an invite code from the server operator
- Codes look like `XXXX-XXXX-XXXX-XXXX` and can be pasted in any case
- Accounts are either `user` or `trusted`
- Trusted users may help confirm results more strongly
- There are no passwords; each account has an API key that the app stores locally
- Keys are stored on the server as hashes only
- To use the same account on another PC, the app asks the server for a one-time code that expires after a few minutes
- Banning an account blocks all of that user's devices

## How uploads are checked

An upload is rejected unless all of the following hold:

- the API key is valid and the account is not banned
- the benchmark profile matches the one the server expects
- the track and lap count are valid and the batch size is within limits
- the lap times are plausible and consistent with each other
- the game setup matches the operator-approved reference

The reference is a set of file fingerprints for the benchmark tracks and the game executable. This prevents players from uploading results from modified files or from a different setup.

Cars are identified by the actual installed physics files (`data.acd` or the `data/` folder, plus `collider.ini`), not by folder name. That means a renamed or updated car mod does not silently inherit an old class.

The benchmark conditions are chosen by the operator and delivered to the app via the server, so the app itself does not hard-code them.

## Settings

Most server settings can be changed with `config set`; `config get` shows the current values.

| Setting | What it does |
|---|---|
| `tolerance_pct` | Two results agree if they are within this percentage of each other |
| `min_confirmations` | How many different users must agree for a result to be verified |
| `spread_limit_pct` | How far the fastest and slowest lap in one upload may differ |
| `class_gap` | The widest a class may be, as a percentage of lap time |
| `min_class_size` | The smallest class the server will make when possible |
| `tie_zone` | How close to a class boundary a car must be to get the borderline flag |
| `variance_limit_pct` | How unusual a car's track balance must be to get the high variance flag |
| `include_provisional` | Also use single-user results when calculating classes |
| `class_refresh_seconds` | The minimum time between class recalculations |
| `export_max_age` | How far behind the full class download may be; `0` means always current |
| `rate_submits_per_hour` | Uploads allowed per account per hour |
| `rate_register_per_hour` | Sign-up attempts allowed per address per hour |
| `rate_lookups_per_hour` | Car lookups allowed per address per hour |

## Running your own server

Docker Compose is the easiest way to get started.

```bash
mkdir acbench-server && cd acbench-server
curl -L https://raw.githubusercontent.com/yiyirob973/AC-Bench-Server/HEAD/docker-compose.yml -o docker-compose.yml
docker compose up -d --build
```

The image is built from the repository's default branch. To build another branch, set `ACB_REF`:

```bash
ACB_REF=dev docker compose up -d --build
```

Put the server behind a reverse proxy that handles HTTPS. Do not expose port `8080` directly to the internet, and do not run it without TLS except on a trusted private network.

### First-time setup

```bash
docker compose exec acbench python server.py init
docker compose exec acbench python server.py set-env --from /import/track_reference.json
docker compose exec acbench python server.py invite --role user --uses 20
```

The server also works without Docker, as long as Python 3 is installed:

```bash
python server.py init
python server.py serve --port 8080
```

### Admin dashboard

The admin dashboard lives at `/admin` and signs administrators in through PocketID (OpenID Connect). It allows you to manage users and invites, inspect disputed results, set the reference, adjust settings, and rebuild the class table.

Required environment variables:

| Variable | Meaning |
|---|---|
| `ACB_PUBLIC_URL` | Public address of the server, e.g. `https://bench.example.com` |
| `ACB_OIDC_ISSUER` | Your PocketID address |
| `ACB_OIDC_CLIENT_ID` | The client ID created in PocketID |
| `ACB_OIDC_CLIENT_SECRET` | The client secret |
| `ACB_ADMIN_GROUPS`, `ACB_ADMIN_SUBS` or `ACB_ADMIN_EMAILS` | Who may sign in; at least one is required |
| `ACB_ADMIN_SESSION_HOURS` | How long a sign-in lasts; default is 8 |

If required variables are missing, the dashboard stays closed.

### Backups

`backup.sh` copies the database while the server is running and keeps recent backups. Run it from cron and copy the backup files off the machine to a safe location.

If you restore a database manually, restart the server afterward so it can rebuild any in-memory state.

If you have several CPU cores, set `WEB_CONCURRENCY` in the compose file to run more worker processes.

### Useful commands

```bash
docker compose logs -f acbench
docker compose ps
docker compose down
```

## Admin commands

Run these with `python server.py <command>` or through `docker compose exec acbench`.

| Command | What it does |
|---|---|
| `serve [--host HOST] [--port PORT]` | Run the server |
| `init` | Create the database |
| `set-env --from FILE` | Set the approved reference from a track reference file |
| `invite [--role user\|trusted] [--uses N]` | Generate an invite code |
| `create-user NAME [--role user\|trusted]` | Create an account directly |
| `ban NAME` / `unban NAME` | Block or unblock an account |
| `backup --to PATH` | Copy the database |
| `recompute-all` | Re-check every stored result |
| `recalc-classes` | Rebuild the class table now |
| `stats` | Show a summary |
| `disputed` | List disputed results |
| `config get` / `config set KEY JSON_VALUE` | Read or change a setting |

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` and `/stats` | Public page listing cars and classes |
| `GET` | `/v1/health` | Check whether the server is up |
| `GET` | `/v1/config` | Accepted profile, limits, and version info |
| `GET` | `/v1/conditions` | Benchmark conditions the app should use |
| `POST` | `/v1/register` | Sign up with an invite code |
| `POST` | `/v1/submit` | Upload results; requires a bearer token API key |
| `GET` | `/v1/classes` | Full class table and all car records |
| `GET` | `/v1/cars/<car_hash>` | One car's record |
| `POST` | `/v1/cars/lookup` | Look up many cars at once |
| `POST` | `/v1/devices/link` | Get a code for signing in on another device |
| `POST` | `/v1/devices/redeem` | Exchange that code for a key on the new device |

An upload contains the `profile`, `environments`, and `results`. The server replies with which entries were accepted or rejected and the current record for each car in that result set.

A lookup is `POST /v1/cars/lookup` with JSON like:

```json
{"hashes": ["abc123...", "def456..."]}
```

The list contains lowercase SHA-256 car hashes. Cars the server does not know are omitted from the reply.

## Privacy and limits

The server stores:

- user names
- hashed API keys
- uploaded benchmark results
- short-lived rate-limit information
- the class table
- admin names and email addresses for dashboard sign-ins

This is a community benchmark service, not a general-purpose commercial traffic system.

### Known limits

- Results are reported by users themselves, so they cannot be fully trusted
- The reference fingerprints are public, so a determined cheater could try to fake them
- Invite-only sign-up, agreement between users, trusted accounts, plausibility checks, and bans help reduce abuse
- Accounts that coordinate can still agree on fake data, so access should be limited to people you trust
- The server is designed for communities, not large commercial traffic volumes

## Repository layout

```text
server.py            API, result merging, and CLI
admin.py             admin dashboard sign-in and actions
admin.html           admin dashboard page
ac_rating.py         car rating and class generation
stats.html           public cars page
branding/            logo files served by the server and used in this README
Dockerfile
docker-compose.yml
backup.sh            database backup utility for cron
```

The server itself needs nothing beyond the Python standard library. The Docker image adds Gunicorn.

## Related

The desktop app, [AC Bench Desktop](https://github.com/yiyirob973/AC-Bench-Desktop), is the client that runs the benchmark and uploads results to this server.

## Disclaimer

This project is provided as-is for community-driven Assetto Corsa benchmarking and class rating workflows. It is not affiliated with, authorized by, or endorsed by Kunos, Steam, or any official Assetto Corsa service.

Use it at your own risk, and always keep backups of your database and configuration before making changes to the server or its reference data.
