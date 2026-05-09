from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from sqlalchemy import text
import os

from .database import engine, Base
from .routers import bots, dashboard, trades, trainer, news, photos, notes, user_files, finance, whop, system_stats, ai_models, analyze, chat, sessions
from .auth import ensure_default_user

load_dotenv()


def _run_migrations():
    """Safe incremental SQLite migrations - each statement is a no-op if column exists.
    Bot/ApiConnection tables are no longer owned by this DB; only schema for
    runtime data (bot_logs, trades, ai_models, etc) needs upkeep."""
    with engine.connect() as conn:
        # v5.0 - AI Lab tables
        ai_lab_cols = [
            "ALTER TABLE ai_models ADD COLUMN live_sync INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE ai_models ADD COLUMN training_mode TEXT NOT NULL DEFAULT 'backtest'",
            "ALTER TABLE ai_models ADD COLUMN training_frequency TEXT NOT NULL DEFAULT 'manual'",
            "ALTER TABLE ai_models ADD COLUMN data_weight TEXT NOT NULL DEFAULT 'balanced'",
            "ALTER TABLE ai_models ADD COLUMN learn_risk INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE ai_models ADD COLUMN total_data_points INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE ai_models ADD COLUMN trades_since_train INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE training_runs ADD COLUMN duration_sec REAL",
        ]
        for sql in ai_lab_cols:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass

        whop_cols = [
            "ALTER TABLE whop_memberships ADD COLUMN plan_name TEXT",
            "ALTER TABLE whop_memberships ADD COLUMN whop_email TEXT",
            "ALTER TABLE whop_memberships ADD COLUMN whop_username TEXT",
        ]
        for sql in whop_cols:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass


def _drop_legacy_tables_with_wrong_bot_id_type():
    """v3.5.x stored bot_id as INTEGER FK to local bots.id. v3.6.0 made it
    a Supabase UUID string. SQLite never alters column types on its own,
    so existing tables keep the wrong INTEGER type and silently reject
    new UUID-string inserts (or store them garbled). Detect and drop the
    legacy tables here so Base.metadata.create_all recreates them with
    the correct String schema. The data we lose is rolling buffer (logs)
    or local-only analytics (trades) — re-creatable at next bot run."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            for tbl in ("bot_logs", "trades"):
                row = conn.execute(text(
                    f"SELECT type FROM pragma_table_info('{tbl}') WHERE name='bot_id'"
                )).fetchone()
                if row and row[0] and 'INT' in row[0].upper():
                    print(f"[watchdog] Detected legacy INTEGER bot_id in {tbl} - dropping (recreated with TEXT schema)")
                    conn.execute(text(f"DROP TABLE {tbl}"))
                    conn.commit()
    except Exception as e:
        print(f"[watchdog] legacy table drop check failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _drop_legacy_tables_with_wrong_bot_id_type()
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    from .database import ensure_columns, ensure_indexes
    ensure_columns()
    ensure_indexes()
    ensure_default_user()

    print("[watchdog] Database ready - runtime-only mode (bots in Supabase)")

    # ── Global Session Manager ────────────────────────────────────────────
    try:
        from .session import get_manager
        from .session.registry import discover
        discover()
        get_manager().start()
        print("[watchdog] Session manager started")
    except Exception as _e:
        print(f"[watchdog] Session manager failed to start: {_e}")

    yield

    try:
        from .session import get_manager
        get_manager().stop()
        print("[watchdog] Session manager stopped")
    except Exception:
        pass
    print("[watchdog] Shutting down")


app = FastAPI(
    title="WATCH-DOG Universal Bot Platform",
    description="Run any type of bot with your own Python code",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Auto error reporting to Supabase app_errors table ───────────────────────
# Every unhandled exception in a request handler gets logged AND written to
# the central Supabase table for admin visibility.
#
# IMPORTANT: All imports here are LAZY (inside the handler body). If the
# error_reporter module is somehow missing from the PyInstaller bundle, we
# log a warning and degrade gracefully — backend STILL STARTS. Eagerly
# importing here at module load was previously a silent killswitch:
# ModuleNotFoundError → main.py fails to import → backend exits → frontend
# shows "Local backend unreachable" with no clue why.
from fastapi import Request
from fastapi.responses import JSONResponse
import logging as _logging
_log = _logging.getLogger("watchdog.exception")

@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    _log.exception("Unhandled exception in %s %s", request.method, request.url.path)
    try:
        from .error_reporter import report_error as _report_error  # lazy
        _report_error(
            exc,
            source="backend",
            context={
                "method": request.method,
                "path":   str(request.url.path),
                "query":  str(request.url.query)[:500],
            },
        )
    except Exception as _re:
        _log.warning("Error reporter unavailable (degraded gracefully): %s", _re)
    return JSONResponse(status_code=500, content={
        "detail": "Internal server error",
        "type":   type(exc).__name__,
    })

# CORS - localhost-only API; accept any origin for desktop / dev / packaged builds.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(whop.router)
app.include_router(bots.router)
app.include_router(trades.router)
app.include_router(dashboard.router)
app.include_router(trainer.router)
app.include_router(news.router)
app.include_router(photos.router)
app.include_router(notes.router)
app.include_router(user_files.router)
app.include_router(finance.router)
app.include_router(system_stats.router)
app.include_router(ai_models.router)
app.include_router(analyze.router)
app.include_router(chat.router)
app.include_router(sessions.router)


@app.get("/")
def root():
    return {"status": "WATCH-DOG Universal Bot Platform running", "version": "1.0.0"}


@app.get("/health")
@app.get("/api/health")
def health():
    """ROOT CAUSE FIX: Electron supervisor (backend-runner.js) probes
    /api/health to decide if backend is alive. Without /api prefix it
    got 404 every probe -> supervisor killed+respawned backend every
    minute -> frontend permanently showed "Local backend unreachable".
    Both routes are accepted (no client breakage)."""
    return {"status": "ok", "version": "1.0.0"}
