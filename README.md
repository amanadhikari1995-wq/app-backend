# WATCH-DOG — Backend

Two Python services that ship inside the
[`app-frontend`](https://github.com/amanadhikari1995-wq/app-frontend) Electron
installer:

- **`watchdog-backend`** — FastAPI server on `localhost:8000`. Manages user
  bots, sessions, trades, AI training, dashboard data.
- **`watchdog-cloud`** — relay client that bridges this local API to the
  cloud dashboard at `wss://watchdogbot.cloud/ws`, so the user can control
  their bots from a web browser.

Both are bundled into single-file `.exe`s by PyInstaller and dropped into
the Electron installer as `extraResources`.

## Stack

- **API**: FastAPI + uvicorn + SQLAlchemy
- **DB**: SQLite (one file, in `%LOCALAPPDATA%/WatchDog/`)
- **Auth**: python-jose JWT (local users), Supabase JWT (cloud)
- **Crypto / trading SDKs**: ccxt (multi-exchange), Kalshi REST (Ed25519/RSA-PSS)
- **AI**: LangChain + LangGraph + Anthropic for code analysis
- **Scheduler**: APScheduler for session detectors (kalshi 15m, crypto 24x7, US stocks)
- **Packaging**: PyInstaller 6.x → single-file Windows .exe

## Folder layout

```
app/                    FastAPI app
  main.py               app entry, CORS, lifespan, migrations
  database.py           SQLAlchemy engine + session
  models.py             ORM models
  schemas.py            Pydantic request/response shapes
  auth.py               JWT issue + verify
  bot_manager.py        Spawn / kill / monitor user bot processes
  token_tracker.py      Per-bot resource tracking
  routers/              FastAPI routers (bots, trades, dashboard, ...)
  session/              Live market session detectors
    base.py             Detector ABC
    registry.py         @register_detector decorator
    manager.py          Spawns detector threads
    router.py           Dispatch session events to bots
    detectors/          One file per market (kalshi_15m, crypto_24x7, ...)
sdk/
  wd_cloud.py           Cloud relay connector — Supabase login, WebSocket
                        bridge, RPC tunnel, status updates
run_backend.py          Programmatic uvicorn entry — used by PyInstaller
                        instead of `python -m uvicorn` (which doesn't work
                        in a frozen exe). Sets up the user-data dir, configures
                        UTF-8 stdout, then runs the FastAPI app.
backend.spec            PyInstaller config for watchdog-backend.exe
cloud.spec              PyInstaller config for watchdog-cloud.exe
build-exes.bat          One-command build — produces both .exes
start-backend.bat       Dev launcher — installs deps + uvicorn --reload
start-cloud.bat         Dev launcher — runs wd_cloud.py against a dev backend
requirements.txt        Production deps (PyInstaller listed too)
```

## Common commands

```bash
# dev — auto-reload backend
start-backend.bat

# dev — cloud relay against a running backend
start-cloud.bat

# production build — produces dist/watchdog-backend.exe and dist/watchdog-cloud.exe
build-exes.bat
```

## Configuration

Runtime config is read from environment variables. Bundled exes also read a
`.env` file at `%LOCALAPPDATA%/WatchDog/.env`. See
[`.env.example`](.env.example) for the full list.

Key vars:

| Var | What |
|---|---|
| `CLOUD_EMAIL` / `CLOUD_PASSWORD` | watchdogbot.cloud credentials for `wd_cloud.py` |
| `KALSHI_API_KEY` / `KALSHI_API_SECRET` | per-bot, set via the dashboard |
| `ANTHROPIC_API_KEY` | for the AI Lab / code analyzer |
| `SECRET_KEY` | local backend JWT signing |
| `DATABASE_URL` | SQLite path; defaults to user-data dir |

## How the bundled `.exe` differs from `uvicorn` in dev

| | Dev (`start-backend.bat`) | Bundled `.exe` |
|---|---|---|
| Working directory | This folder | `%LOCALAPPDATA%/WatchDog` |
| `watchdog.db` | Here | `%LOCALAPPDATA%/WatchDog/watchdog.db` |
| Logs | stdout only | stdout + `%LOCALAPPDATA%/WatchDog/logs/backend.log` |
| Reload on save | Yes | No (frozen) |
| Python interpreter | Required on user's PC | Embedded |

## Releasing

The two `.exe`s built here get **bundled into the
[app-frontend](https://github.com/amanadhikari1995-wq/app-frontend) installer**.
They are not released independently. Workflow:

1. Make backend changes here, push to `main`
2. Build: `build-exes.bat`
3. Switch to `app-frontend`, build the installer (which copies the new
   `.exe`s in via `extraResources`)
4. Publish the installer as a new GitHub Release on `watchdog-website`

The Electron app's auto-updater pulls the new release within ~4 hours and
applies it on restart, picking up both UI + backend changes at once.
