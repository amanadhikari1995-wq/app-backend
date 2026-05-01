from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from sqlalchemy import text
import os

from .database import engine, Base
from .routers import bots, api_connections, dashboard, trades, trainer, news, photos, notes, user_files, finance, whop, system_stats, ai_models, analyze, chat, sessions
from .auth import ensure_default_user

load_dotenv()


def _run_migrations():
    """Safe incremental SQLite migrations — each statement is a no-op if column exists."""
    statements = [
        # v3.1 — per-bot API connections
        "ALTER TABLE api_connections ADD COLUMN bot_id INTEGER REFERENCES bots(id)",
        # v3.2 — bot secret for trade recording
        "ALTER TABLE bots ADD COLUMN bot_secret TEXT NOT NULL DEFAULT ''",
        # v3.3 — bot settings
        "ALTER TABLE bots ADD COLUMN schedule_type TEXT NOT NULL DEFAULT 'always'",
        "ALTER TABLE bots ADD COLUMN schedule_start TEXT",
        "ALTER TABLE bots ADD COLUMN schedule_end TEXT",
        "ALTER TABLE bots ADD COLUMN max_amount_per_trade REAL",
        "ALTER TABLE bots ADD COLUMN max_contracts_per_trade INTEGER",
        "ALTER TABLE bots ADD COLUMN max_daily_loss REAL",
        "ALTER TABLE bots ADD COLUMN auto_restart INTEGER NOT NULL DEFAULT 0",
    ]
    with engine.connect() as conn:
        for sql in statements:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # Column already exists

        # Soft-delete orphaned api_connections (no bot_id)
        try:
            conn.execute(text("UPDATE api_connections SET is_active = 0 WHERE bot_id IS NULL"))
            conn.commit()
        except Exception:
            pass

        # v5.0 — AI Lab tables
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

        # v4.0 — Whop membership table
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

        # Backfill empty bot_secret values
        try:
            import uuid
            rows = conn.execute(
                text("SELECT id FROM bots WHERE bot_secret = '' OR bot_secret IS NULL")
            ).fetchall()
            for (bot_id,) in rows:
                conn.execute(text("UPDATE bots SET bot_secret = :s WHERE id = :id"),
                             {"s": str(uuid.uuid4()), "id": bot_id})
            if rows:
                conn.commit()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    # Create perf-critical composite indexes that aren't declared on the
    # ORM models (bot_logs, trades). Without these, queries that fetch
    # "recent logs for bot N" do a full table scan that gets pathological
    # after hours of bot activity.
    from .database import ensure_indexes
    ensure_indexes()
    ensure_default_user()
    # Reset any bots stuck in RUNNING from a previous server process — their
    # subprocesses are gone after a restart so the status must be corrected.
    try:
        from .database import SessionLocal
        from . import models
        _db = SessionLocal()
        stale = _db.query(models.Bot).filter(models.Bot.status == models.BotStatus.RUNNING).all()
        for b in stale:
            b.status = models.BotStatus.IDLE
            print(f"[watchdog] Reset stale RUNNING bot → IDLE: id={b.id} name={b.name}")
        _db.commit()
        _db.close()
    except Exception as _e:
        print(f"[watchdog] Could not reset stale bot statuses: {_e}")
    print("[watchdog] Database ready — v3.5.0 (AI Trainer)")

    # ── Global Session Manager ────────────────────────────────────────────
    # Auto-discovers every detector under app/session/detectors/ and starts
    # a polling thread per registered market_type. Bots can subscribe via
    # the REST endpoint /api/sessions/{market} or the wd_session SDK.
    try:
        from .session import get_manager
        from .session.registry import discover
        discover()                       # import all detector modules
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
    version="3.3.0",
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────────────────────────
#
# This API binds to 127.0.0.1 only — it is reachable solely from processes
# running on the same machine. CORS therefore can't be a defense against
# attacker websites: those would have to first be opened by the user, AND
# would then face a browser that refuses cross-origin localhost requests
# anyway under modern same-origin rules. Auth is enforced separately via
# Bearer JWTs (see app/auth.py).
#
# We accept any Origin so we don't have to keep chasing every legitimate
# caller as it changes form:
#   • Electron (packaged):  app://.        ─┐
#   • Electron (file://):   file://         │ all three look different
#   • Next.js dev server:   http://localhost:3000   to the browser, all
#   • Tauri (future):       tauri://localhost       three need to talk
#                                                   to this backend
#
# `allow_credentials=False` is intentional — we do NOT use cookies for
# auth, only Authorization: Bearer headers. This combination (regex='.*'
# + credentials=False) is permitted by the CORS spec; the alternative
# (specific origin + credentials=True) was incorrectly excluding every
# Electron build of the app.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(whop.router)        # Whop auth — registered first so /api/auth/me works
app.include_router(bots.router)
app.include_router(api_connections.router)
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
app.include_router(sessions.router)    # global session manager REST surface


@app.get("/")
def root():
    return {"status": "WATCH-DOG Universal Bot Platform running", "version": "3.3.0"}


@app.get("/health")
def health():
    return {"status": "ok"}
