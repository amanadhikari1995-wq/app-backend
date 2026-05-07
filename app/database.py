"""
database.py - SQLite engine with production-grade pragmas.

Why these settings matter (root cause of the long-running-session lag):

  - WAL (Write-Ahead Log) mode lets readers and writers operate concurrently.
  - busy_timeout=5000 makes SQLite wait up to 5s for a lock instead of erroring.
  - synchronous=NORMAL fsyncs less aggressively than the default FULL.
  - cache_size=-20000 = 20 MB of in-memory page cache.
  - temp_store=MEMORY keeps temporary tables (used by ORDER BY, etc.) in RAM.

Plus an idx on bot_logs(bot_id, id DESC) for the "fetch recent logs for bot N"
hot path. Bots themselves now live in Supabase; only runtime-buffered tables
(bot_logs, trades, ai_models, etc.) are owned by this DB.
"""
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./watchdog.db")
_IS_SQLITE = "sqlite" in DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30} if _IS_SQLITE else {},
)


if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA cache_size=-20000")
            cur.execute("PRAGMA temp_store=MEMORY")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_columns() -> None:
    """
    Add columns that may be missing on legacy SQLite databases. Idempotent -
    each statement is wrapped in try/except so re-running on a current DB is
    a no-op. Called once at app startup from main.py BEFORE ensure_indexes.
    """
    with engine.connect() as conn:
        # users.supabase_uid - populated on first authenticated request
        try:
            conn.execute(text("ALTER TABLE users ADD COLUMN supabase_uid VARCHAR"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_supabase_uid_unique "
                "ON users (supabase_uid) WHERE supabase_uid IS NOT NULL"
            ))
            conn.commit()
        except Exception:
            pass

        # Legacy data fix: bot_id columns on bot_logs + trades used to be
        # INTEGER (FK to local bots.id). After the v3.6.0 refactor they're
        # TEXT (Supabase UUID). SQLite is type-permissive so the column type
        # stays whatever it was at table creation, but the row VALUES still
        # come back as int. Pydantic str-typed BotLogOut/TradeOut then 500s
        # the dashboard endpoint with a validation error, which the frontend
        # surfaces as "Local backend unreachable".
        # CAST every existing int bot_id to TEXT so reads come back as str.
        for tbl in ("bot_logs", "trades"):
            try:
                conn.execute(text(
                    f"UPDATE {tbl} SET bot_id = CAST(bot_id AS TEXT) "
                    f"WHERE bot_id IS NOT NULL AND typeof(bot_id) = 'integer'"
                ))
                conn.commit()
            except Exception:
                pass


def ensure_indexes() -> None:
    """
    Create performance-critical indexes for runtime-buffered tables.
    Idempotent. bot_id is now a TEXT column (Supabase UUID); the existing
    index name is preserved so older DBs benefit immediately.
    """
    if not _IS_SQLITE:
        return
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_bot_logs_bot_id_id "
            "ON bot_logs (bot_id, id DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_trades_bot_id_created "
            "ON trades (bot_id, created_at DESC)"
        ))
        conn.commit()
