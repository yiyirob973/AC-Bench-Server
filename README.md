<p align="center">
  <img src="branding/ac-bench.png" alt="AC Bench logo" width="140">
</p>

<h1 align="center">AC Bench Server</h1>

<p align="center">
  The shared class list for AC Bench: collects benchmark results, checks them and tells the app which class each car is in.
</p>

---

AC Bench sorts Assetto Corsa cars into classes by how fast they are on a standard benchmark, so you can put together grids where the cars are evenly matched. People run the benchmark on their own PC with the [AC Bench Desktop app](https://github.com/yiyirob973/AC-Bench-Desktop) and upload the results. This server collects those results, checks them, combines them, and tells the app which class each car belongs to.

## Do I need this?

Most people don't. If you just want to benchmark cars or see their classes, use the desktop app. You will need an invite code from whoever runs the server you want to join.

This repository is for people who run a server of their own, for example a community that wants its own shared class list.

## What the server does

- Takes sign-ups by invite code only
- Checks every upload and works out the lap scores itself instead of trusting the app
- Compares results from different people and settles on one result per car and track
- Keeps a class table that updates as new results come in, and answers the app's questions about which class a car is in
- Serves a public page with the full list of cars and their classes, in the AC Bench colors
- Gives the operator an admin dashboard and a few command line tools

## How a result becomes a class

1. Someone benchmarks a car in the desktop app. The app uploads the result with their personal key.
2. The server checks the upload: the key must be valid, the benchmark setup must match the one the server asks for, and the lap times must make sense.
3. The server recalculates the score from the lap times and compares it with what other people have uploaded for the same car.
4. Each car ends up with one of three statuses:
   - **verified**: enough different people agree on the time, or a trusted user has confirmed it
   - **provisional**: only one person has uploaded it so far
   - **disputed**: the uploads disagree, so the car isn't rated until more results come in
5. Rated cars are ranked by lap time and split into classes at the natural gaps between them. The reply to an upload says which class each car in it belongs to.

Classes are named 1A (the fastest), 1B, and so on up to 1Z, then 2A, 2B and onward. The desktop app writes the class into each car's tags as `#Pride-1A`, `#Pride-1B` and so on.

A car's rating is how far behind the fastest car it is, so the fastest car is at 0%. As faster and slower cars are added, the class borders move to fit them, which means a car can change class over time. The class table has a version number that only goes up when the borders or class names actually change, so an app can tell whether it needs to look again.

### Flags

A car can carry a flag. These are advice for building grids and have no effect on whether a car is verified.

- **borderline**: the car is very close to the edge of its class, so a small change could move it. Set by `tie_zone`.
- **high variance**: the car's balance between the benchmark tracks is unusual for its class. A car that is simply slow in the same way as the other cars in its class isn't flagged, however far behind it is. Set by `variance_limit_pct`. In a class with only one or two cars there is little to compare against, so this flag is less reliable there.

## Accounts

- Sign-up needs an invite code from the operator. Codes look like `XXXX-XXXX-XXXX-XXXX` and can be pasted in any case, with or without the hyphens.
- Accounts are either `user` or `trusted`. A trusted user's results count as confirmed on their own.
- There are no passwords. Each account has an API key that the app keeps. Keys are stored on the server as hashes only.
- To use an account on another PC, the signed-in app asks the server for a single-use code (valid for 10 minutes), and the new device swaps it for its own key. Each device has a separate key.
- Banning an account blocks all of its devices.

## How uploads are checked

An upload is turned away unless all of these hold:

- the API key is valid and the account isn't banned
- the benchmark profile matches the one the server expects
- the track and lap count are valid and the batch size is within limits
- the lap times are plausible and consistent with each other
- the game setup matches the operator's approved reference

The reference is a set of file fingerprints for the benchmark tracks and the game executable. It stops people uploading results from modified files or a different setup. The operator sets it from the desktop app's track reference file, either with `set-env` or in the admin dashboard.

Cars are identified by the physics files that are actually installed (`data.acd` or the `data/` folder, plus `collider.ini`), not by folder name. A renamed or updated car mod can't inherit an old rating just because it kept its name.

The benchmark conditions, such as which tracks are used, are chosen by the operator. The app asks the server for them, so nothing about them is fixed in the app.

## Settings

Every setting can be changed with `config set`, and most of them in the admin dashboard. Defaults are in brackets.

| Setting | What it does |
|---|---|
| `tolerance_pct` (0.5) | Two results agree if they are within this percentage of each other |
| `min_confirmations` (2) | How many different people must agree for a result to be verified |
| `spread_limit_pct` (1.5) | The most an upload's own timed laps may differ, slowest against fastest, as a percentage |
| `class_gap` (2) | The widest a class may be, as a percentage of lap time |
| `min_class_size` (3) | The smallest class the server will make if it can avoid it |
| `tie_zone` (0.1) | How close to a class border a car must be to get the borderline flag |
| `variance_limit_pct` (3) | How unusual a car's track balance must be to get the high variance flag |
| `include_provisional` (off) | Also use single-user results when working out the classes |
| `class_refresh_seconds` (10) | The shortest time between two recalculations of the classes |
| `export_max_age` (30) | How many seconds the full class download may lag behind new uploads. 0 means always current |
| `rate_submits_per_hour` (20) | Uploads allowed per account per hour |
| `rate_register_per_hour` (10) | Sign-up attempts allowed per address per hour |
| `rate_lookups_per_hour` (240) | Car lookups allowed per address per hour |

Settings like these may need adjusting once real results come in.

## Running your own server

Docker Compose is the easiest way. The compose file builds the server straight from this repository.

```bash
mkdir acbench-server && cd acbench-server
curl -L https://raw.githubusercontent.com/yiyirob973/AC-Bench-Server/main/docker-compose.yml -o docker-compose.yml
docker compose up -d --build
```

Or clone the repository and run `docker compose up -d --build` from it. The server listens on port 8080. The database lives in a Docker volume, and `./import` is mounted read-only so you can drop in files such as the track reference before running admin commands.

Put the server behind a reverse proxy that handles HTTPS. Don't expose port 8080 to the internet directly, and don't run it without TLS anywhere other than your own machine, because API keys travel in a request header.

First-time setup:

```bash
docker compose exec acbench python server.py init
docker compose exec acbench python server.py set-env --from /import/track_check_log.json
docker compose exec acbench python server.py invite --role user --uses 20
```

Without Docker, the server needs only Python 3 and its standard library:

```bash
python server.py init
python server.py serve --port 8080
```

### Admin dashboard

The dashboard lives at `/admin` and signs you in through PocketID (OpenID Connect). It lets you manage users and invites, look at disputed results, set the reference, change settings and rebuild the classes. Set these environment variables in the compose file:

| Variable | Meaning |
|---|---|
| `ACB_PUBLIC_URL` | The public address of your server, for example `https://bench.example.com` |
| `ACB_OIDC_ISSUER` | Your PocketID address |
| `ACB_OIDC_CLIENT_ID` | The client ID you created in PocketID |
| `ACB_OIDC_CLIENT_SECRET` | The client secret |
| `ACB_ADMIN_GROUPS`, `ACB_ADMIN_SUBS` or `ACB_ADMIN_EMAILS` | Who may sign in. At least one is required |
| `ACB_ADMIN_SESSION_HOURS` | How long a sign-in lasts (default 8) |

If any of the required variables is missing, the dashboard stays closed.

### Backups

`backup.sh` makes a copy of the database while the server is running and keeps 30 days of them. Run it from cron, and copy the backups off the machine. If you restore a database by hand, restart the container afterwards.

If you have several CPU cores, set `WEB_CONCURRENCY` in the compose file to run more worker processes.

### Useful commands

```bash
docker compose logs -f acbench
docker compose ps
docker compose down
```

## Admin commands

Run these with `python server.py <command>` (or through `docker compose exec acbench`).

| Command | What it does |
|---|---|
| `serve [--host HOST] [--port PORT]` | Run the server |
| `init` | Create the database |
| `set-env --from FILE` | Set the approved reference from a track reference file |
| `invite [--role user\|trusted] [--uses N]` | Make an invite code |
| `create-user NAME [--role user\|trusted]` | Make an account directly |
| `ban NAME` / `unban NAME` | Block or unblock an account |
| `backup --to PATH` | Copy the database |
| `recompute-all` | Re-check every stored result |
| `recalc-classes` | Rebuild the class table now. The server also does this by itself |
| `stats` | Show a summary |
| `disputed` | List disputed results |
| `config get` / `config set KEY JSON_VALUE` | Read or change a setting |

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` and `/stats` | The public page listing cars and their classes |
| `GET` | `/v1/health` | Check that the server is up |
| `GET` | `/v1/config` | The accepted profile, limits and version information |
| `GET` | `/v1/conditions` | The benchmark conditions the app should use |
| `POST` | `/v1/register` | Sign up with an invite code |
| `POST` | `/v1/submit` | Upload results (needs the API key as a bearer token) |
| `GET` | `/v1/classes` | The whole class table and every car, in one download |
| `GET` | `/v1/cars/<car_hash>` | One car's record |
| `POST` | `/v1/cars/lookup` | Records for up to 500 cars at once |
| `POST` | `/v1/devices/link` | Get a code for signing in on another device |
| `POST` | `/v1/devices/redeem` | Swap that code for a key on the new device |

An upload contains the `profile`, the `environments` and the `results`. The reply lists accepted and rejected entries with the reason for each rejection, and the current record for every car the upload touched: its class, status, flags and times.

A lookup is `POST /v1/cars/lookup` with `{"hashes": [...]}`, holding 1 to 500 lowercase SHA-256 car hashes. No key is needed. Cars the server doesn't know are left out of the reply, and the answer is always current.

## Privacy

The server stores user names, hashed API keys, uploaded results, short-lived rate-limit records and the class table. Admins who sign in to the dashboard also have their name and email address kept in the session and in a log of admin actions.

## Limits

- Results are reported by the users themselves, so they can't be fully trusted.
- The reference fingerprints are public, so a determined cheater could try to fake them.
- The checks that help are invite-only sign-up, agreement between users, trusted accounts, plausibility checks and bans.
- Accounts that work together can still agree on fake results, so keep sign-ups limited to people you trust.
- The server is built for a community, not for large commercial traffic.

## Repository layout

```text
server.py            the API, result merging and command line
admin.py             the admin dashboard's sign-in and actions
admin.html           the admin dashboard page
ac_rating.py         splits cars into classes and names them
stats.html           the public cars page
branding/            the logo: favicon.ico and logo.png are served by the server, ac-bench.png is for this README
Dockerfile
docker-compose.yml
backup.sh            database backup, meant to run from cron
```

The server needs nothing beyond the Python standard library. The Docker image adds gunicorn.

## Related

The desktop app, [AC Bench Desktop](https://github.com/yiyirob973/AC-Bench-Desktop), is where benchmarks are run and cars are analysed. This repository is the server it talks to.
