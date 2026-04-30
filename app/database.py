"""
database.py — SQLite engine with production-grade pragmas.

Why these settings matter (root cause of the long-running-session lag):

  • WAL (Write-Ahead Log) mode lets readers and writers operate concurrently.
    Default rollback-journal mode locks the ENTIRE database during every
    write. Under combined load (bot subprocess writing logs + FastAPI
    handling polls + frontend dashboard polling every 1.5s), the default
    config produces "database is locked" errors that hang the whole API.

  • busy_timeout=5000 makes SQLite wait up to 5s for a lock instead of
    erroring instantly. Eliminates spurious lock errors under bursty load.

  • synchronous=NORMAL fsyncs less aggressively than the default FULL.
    Still safe under WAL (no risk of corruption, only the most recent
    committed transaction can be lost on crash).

  • cache_size=-20000 = 20 MB of in-memory page cache. Default is ~2 MB.
    Drastically cuts disk reads on hot tables like bot_logs.

  • temp_store=MEMORY keeps temporary tables (used by ORDER BY, etc.) in RAM.

Plus an idx on bot_logs(bot_id, id DESC) — without this, every "fetch
recent logs for bot N" query scans the full table. After hours of bot
activity that's the difference between an O(1) index seek and an
O(N) scan over thousands of rows on every poll.
"""
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./watchdog.db")
_IS_SQLITE = "sqlite" in DATABASE_URL

# `timeout=30` — pysqlite's busy-wait when no busy_timeout pragma is set yet.
# pragmas below override it once the connection is open.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30} if _IS_SQLITE else {},
)


# Apply pragmas on EVERY new connection (connection pool may rotate them).
if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA cache_size=-20000")   # negative = KB → 20 MB
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


def ensure_indexes() -> None:
    """
    Create performance-critical indexes that aren't on the ORM models.
    Idempotent — `IF NOT EXISTS` makes repeated calls a no-op.
    Called once at app startup from main.py.
    """
    if not _IS_SQLITE:
        return
    with engine.connect() as conn:
        # bot_logs: hot path — every poll fetches "last N logs for bot X"
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_bot_logs_bot_id_id "
            "ON bot_logs (bot_id, id DESC)"
        ))
        # trades: similar hot path for trade history per bot
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_trades_bot_id_created "
            "ON trades (bot_id, created_at DESC)"
        ))
        conn.commit()
