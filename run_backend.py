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
import traceback
import datetime

# Force UTF-8 stdout/stderr so emoji + non-ASCII log lines don't crash
# on Windows under the default cp1252 codepage.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _emergency_crash_dump(exc_type, exc_value, tb):
    """
    Last-resort crash logger.

    The bundled-exe failure mode that prompted this code: backend.log shows
    "Booting WATCH-DOG backend — data dir: …" then nothing, the process
    exits, and electron's supervisor respawns it 5× with the same silent
    crash. The user's only window into the failure is backend.log — but
    Python's default sys.excepthook prints the traceback to stderr, which
    in a frozen exe vanishes (Electron pipes it to the main-process console
    that end users never see).

    This hook writes the full traceback to backend.crash.log inside the
    same logs dir as backend.log. We write to a SEPARATE file (not
    backend.log) so the crash is easy to find even when the root logger is
    in an unknown state, and so the dump survives even if the root logger
    initialised with bad handlers (which is itself a possible cause of
    the silent crash).
    """
    try:
        log_dir = os.environ.get("WATCHDOG_LOG_DIR")
        if not log_dir:
            # Best-effort fallback to the same dir we'd compute ourselves
            base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
            log_dir = str(pathlib.Path(base) / "WatchDog" / "logs")
        pathlib.Path(log_dir).mkdir(parents=True, exist_ok=True)
        crash_path = pathlib.Path(log_dir) / "backend.crash.log"
        with open(crash_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n{'=' * 72}\n")
            f.write(f"CRASH @ {datetime.datetime.now().isoformat(timespec='seconds')}\n")
            f.write(f"  argv         : {sys.argv}\n")
            f.write(f"  executable   : {sys.executable}\n")
            f.write(f"  frozen       : {getattr(sys, 'frozen', False)}\n")
            f.write(f"  _MEIPASS     : {getattr(sys, '_MEIPASS', None)}\n")
            f.write(f"  cwd          : {os.getcwd()}\n")
            f.write(f"  python ver   : {sys.version}\n")
            f.write(f"{'-' * 72}\n")
            traceback.print_exception(exc_type, exc_value, tb, file=f)
            f.write(f"{'=' * 72}\n")
    except Exception:
        # Absolutely nothing we can do — fall through to default handler.
        pass
    # Always also write to stderr for the Electron console.
    sys.__excepthook__(exc_type, exc_value, tb)


# Install the hook IMMEDIATELY, before we touch anything else. If a crash
# happens during module-level imports of app.main, _bootstrap_paths(), or
# even logging.basicConfig itself, we still capture it.
sys.excepthook = _emergency_crash_dump


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


def _maybe_run_as_python() -> bool:
    """If we were invoked with a script argument (e.g. by bots router doing
    `subprocess.Popen([sys.executable, '-u', wd_runner.py, tmp_path])`),
    act as a Python interpreter executing that script.

    In a frozen PyInstaller exe, sys.executable IS this exe — not python.exe.
    Without this branch, the bot subprocess just re-runs the backend
    server, hits the port pre-flight, and exits. Bots never actually
    execute their code.

    Returns True if we ran a script (caller should NOT run the backend).
    """
    args = sys.argv[1:]
    # Skip leading -u, -B, -O, etc. Python flags so we accept the same
    # CLI shape the rest of the codebase uses with sys.executable.
    while args and args[0].startswith('-') and len(args[0]) <= 3:
        args.pop(0)
    if not args:
        return False
    script = args[0]
    if not script.lower().endswith('.py'):
        return False
    if not os.path.exists(script):
        return False

    # Bootstrap paths so the script (e.g. wd_runner.py) inherits the same
    # WATCHDOG_DATA_DIR + WATCHDOG_LOG_DIR env the backend uses.
    _bootstrap_paths()

    # Replace sys.argv with [script, *script_args] so the script sees
    # itself as argv[0] (matches `python script.py args` behavior).
    sys.argv = [script] + args[1:]
    # Ensure the script's directory is on sys.path so its sibling imports
    # work (Python normally does this for the main script).
    script_dir = os.path.dirname(os.path.abspath(script))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    import runpy
    runpy.run_path(script, run_name='__main__')
    return True


def main() -> None:
    # Bot-runner mode: when invoked with a script argument, behave like
    # `python <script>` instead of starting the backend server. This is
    # the path bots router takes when spawning a bot subprocess.
    if _maybe_run_as_python():
        return

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
    # Diagnostic line: lets us see at a glance whether we're running as the
    # PyInstaller --onedir exe vs. a stray python.exe, and where _MEIPASS
    # points (a common silent-crash cause is _MEIPASS missing dlls/.pyd).
    log.info(
        "Boot env — frozen=%s _MEIPASS=%s exe=%s argv=%s",
        getattr(sys, "frozen", False),
        getattr(sys, "_MEIPASS", None),
        sys.executable,
        sys.argv,
    )

    # Import the FastAPI app AFTER paths are bootstrapped — some modules
    # touch the filesystem on import (e.g. database.py creates the DB).
    #
    # Wrapped in try/except because a ModuleNotFoundError or any other
    # import-time failure here is THE most common silent-crash cause for
    # the bundled exe, and Python's default behaviour is to print to stderr
    # and exit — which in a frozen exe means the failure is invisible to the
    # user (backend.log only shows "Booting…" and stops). Logging with
    # exc_info=True writes the full traceback into backend.log so the next
    # time this happens, the user can see what failed.
    try:
        from app.main import app
    except Exception:
        log.critical("FATAL: failed to import app.main — backend cannot start", exc_info=True)
        # Re-raise so sys.excepthook (above) ALSO writes to backend.crash.log.
        raise

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
        # INFO not WARNING — this is the expected, healthy outcome when a
        # sibling instance has already bound the port. Service.start() in
        # backend-runner.js sees code=0 and correctly does not respawn.
        log.info("Port %s:%d already serving — sibling watchdog-backend instance "
                 "won the race. This process exiting cleanly (code 0).", host, port)
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
    # Same try/except rationale as the import above — a uvicorn startup
    # failure (port stolen mid-flight, FastAPI lifespan crash, etc.)
    # otherwise vanishes to stderr and the user sees "backend unreachable".
    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="info",
            access_log=True,  # diagnostic: show HTTP requests in backend.log
            reload=False,           # MUST be False inside a frozen exe
            workers=1,
        )
    except Exception:
        log.critical("FATAL: uvicorn.run raised — backend exited", exc_info=True)
        raise


if __name__ == "__main__":
    # Outer guard. Even though sys.excepthook is installed at module load,
    # explicitly wrapping main() here means a crash that happens BEFORE
    # logging.basicConfig (e.g. inside _bootstrap_paths if %LOCALAPPDATA%
    # is unwritable) still produces a backend.crash.log dump and a
    # non-zero exit — which is the contract backend-runner.js's supervisor
    # expects when deciding whether to respawn.
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # Trigger our excepthook explicitly (sys.exit will skip it).
        _emergency_crash_dump(*sys.exc_info())
        sys.exit(1)
