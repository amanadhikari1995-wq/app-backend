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


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    version="3.6.0",
    lifespan=lifespan,
)

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
    return {"status": "WATCH-DOG Universal Bot Platform running", "version": "3.6.0"}


@app.get("/health")
def health():
    return {"status": "ok"}
