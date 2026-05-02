"""
run_backend.py — Frozen-exe entry point for the FastAPI backend.

`python -m uvicorn app.main:app` doesn't work inside a PyInstaller .exe
because there's no `python` interpreter to invoke. This script imports
the FastAPI app object directly and runs uvicorn programmatically.

Also fixes two things that bite frozen Python apps:

  1. CWD is the .exe's directory at launch, NOT the dev project root.
     We resolve all writable paths (DB, logs, uploads) relative to a
     known user-writable location (%LOCALAPPDATA%/WatchDog) so the app
     works whether installed in Program Files or run portably.
  2. SQLite + filewatcher's --reload don't survive freezing. Reload is
     disabled here unconditionally; that's a dev-only feature anyway.

Run it standalone for testing:
    python run_backend.py

Build it into a single-file exe:
    pyinstaller backend.spec --clean
"""
from __future__ import annotations

import os
import sys
import pathlib
import logging

# Force UTF-8 stdout/stderr so emoji + non-ASCII log lines don't crash
# on Windows under the default cp1252 codepage.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _user_data_dir() -> pathlib.Path:
    """Cross-platform writable directory for DB + logs + uploads."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(pathlib.Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(pathlib.Path.home() / ".local" / "share")
    p = pathlib.Path(base) / "WatchDog"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _bootstrap_paths() -> None:
    """Point the app at user-writable directories for all runtime state."""
    data = _user_data_dir()
    (data / "logs").mkdir(exist_ok=True)
    (data / "uploads").mkdir(exist_ok=True)
    (data / "training_data").mkdir(exist_ok=True)
    (data / "ai_models").mkdir(exist_ok=True)
    (data / "bots").mkdir(exist_ok=True)

    # Make the app's relative paths resolve into the user dir, not next
    # to the .exe (which Program Files denies write access to).
    os.chdir(data)

    # Surface paths via env vars so any code that reads them works.
    os.environ.setdefault("WATCHDOG_DATA_DIR", str(data))
    os.environ.setdefault("WATCHDOG_DB_PATH",  str(data / "watchdog.db"))
    os.environ.setdefault("WATCHDOG_LOG_DIR",  str(data / "logs"))


def _resource_path(relative: str) -> str:
    """Read-only resource bundled inside the PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def main() -> None:
    _bootstrap_paths()

    # Configure logging to a file in the user dir, plus console
    log_path = pathlib.Path(os.environ["WATCHDOG_LOG_DIR"]) / "backend.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log = logging.getLogger("watchdog.bootstrap")
    log.info("Booting WATCH-DOG backend — data dir: %s", os.environ["WATCHDOG_DATA_DIR"])

    # Import the FastAPI app AFTER paths are bootstrapped — some modules
    # touch the filesystem on import (e.g. database.py creates the DB).
    from app.main import app

    import uvicorn
    import socket
    import time
    port = int(os.environ.get("WATCHDOG_API_PORT", "8000"))
    host = os.environ.get("WATCHDOG_API_HOST", "127.0.0.1")

    # ── Bind pre-flight ─────────────────────────────────────────────────────
    # On Windows, the kernel keeps the previous binder's socket in TIME_WAIT
    # for up to ~120s after a hard kill. If we try to bind 8000 immediately
    # after a crash/restart, uvicorn exits with WinError 10048 — which
    # backend-runner.js's Service interprets as "exited" → auto-respawn,
    # creating duplicate processes that all spam the same bind error in the
    # log. Avoid this entirely:
    #
    #   1. Probe the port. If something else is listening, check whether it
    #      looks like another instance of us (same host:port, accepts TCP).
    #      If yes, exit silently with success — backend-runner.js won't
    #      respawn (we'd be the duplicate).
    #
    #   2. If port is unbound but in TIME_WAIT, retry up to 12 times with
    #      0.75s spacing (= 9s total). Almost always frees within 5s.
    #
    # Net effect: zero WinError 10048 in the log under any restart pattern.
    # ────────────────────────────────────────────────────────────────────────
    def _is_port_in_use(h, p):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            return s.connect_ex((h, p)) == 0
        finally:
            s.close()

    def _can_bind(h, p):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((h, p))
            return True
        except OSError:
            return False
        finally:
            s.close()

    if _is_port_in_use(host, port):
        log.warning("Port %s:%d is already serving — assuming a sibling watchdog-backend "
                    "instance won the race. Exiting silently to avoid duplicate.", host, port)
        return  # clean exit, Service won't respawn

    for attempt in range(12):
        if _can_bind(host, port):
            break
        log.info("Port %s:%d not yet bindable (TIME_WAIT?), retrying… (%d/12)", host, port, attempt + 1)
        time.sleep(0.75)
    else:
        log.error("Port %s:%d still not bindable after 9s — giving up.", host, port)
        return  # clean exit, no error spam

    log.info("Starting uvicorn on %s:%d", host, port)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        reload=False,           # MUST be False inside a frozen exe
        workers=1,
    )


if __name__ == "__main__":
    main()
