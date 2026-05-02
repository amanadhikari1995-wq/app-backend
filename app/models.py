from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Enum, ForeignKey, Float, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum
import uuid

from .database import Base


class BotStatus(str, enum.Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class LogLevel(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Supabase user UUID — populated on first authenticated request via the
    # cloud-sync flow (auth.get_current_user_supabase). NULL for the legacy
    # singleton "watchdog" user (id=1) and any local-only account that never
    # signed in via Supabase. Indexed + unique-when-present (the partial
    # unique index lives in database.ensure_columns()).
    supabase_uid = Column(String, index=True, nullable=True)

    bots = relationship("Bot", back_populates="user", cascade="all, delete-orphan")
    bot_logs = relationship("BotLog", back_populates="user", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="user", cascade="all, delete-orphan")


class Bot(Base):
    __tablename__ = "bots"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    code = Column(Text, nullable=False)
    status = Column(Enum(BotStatus), default=BotStatus.IDLE)
    run_count = Column(Integer, default=0)
    bot_secret = Column(String, default=lambda: str(uuid.uuid4()), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_run_at = Column(DateTime(timezone=True), nullable=True)

    # Cloud-sync (step 3 of cloud-sync rollout). Populated when the row is
    # mirrored to the Supabase `bots` table; null for bots that haven't synced
    # yet (offline create, legacy default-user bots before migration).
    cloud_id       = Column(String,  index=True, nullable=True)
    cloud_synced_at = Column(DateTime(timezone=True), nullable=True)

    # ── Settings ──────────────────────────────────────────────────────────────
    # Run schedule
    schedule_type  = Column(String,  default="always")   # "always" | "custom"
    schedule_start = Column(String,  nullable=True)       # "HH:MM"
    schedule_end   = Column(String,  nullable=True)       # "HH:MM"
    # Risk management
    max_amount_per_trade   = Column(Float,   nullable=True)
    max_contracts_per_trade = Column(Integer, nullable=True)
    max_daily_loss         = Column(Float,   nullable=True)
    # General
    auto_restart = Column(Boolean, default=False)

    user = relationship("User", back_populates="bots")
    logs = relationship("BotLog", back_populates="bot", cascade="all, delete-orphan")
    api_connections = relationship("ApiConnection", back_populates="bot", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="bot", cascade="all, delete-orphan")


class BotLog(Base):
    __tablename__ = "bot_logs"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    level = Column(Enum(LogLevel), default=LogLevel.INFO)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    bot = relationship("Bot", back_populates="logs")
    user = relationship("User", back_populates="bot_logs")


class ApiConnection(Base):
    __tablename__ = "api_connections"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    base_url = Column(String, nullable=True)
    api_key = Column(String, nullable=True)
    api_secret = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    bot = relationship("Bot", back_populates="api_connections")


class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    entry_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    quantity = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    note = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    bot = relationship("Bot", back_populates="trades")
    user = relationship("User", back_populates="trades")


# ── Dashboard Personal Widgets ────────────────────────────────────────────────

class Photo(Base):
    __tablename__ = "photos"
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)      # stored on disk
    original_name = Column(String, nullable=False)
    caption = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Note(Base):
    __tablename__ = "notes"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False, default="Untitled")
    content = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class UserFile(Base):
    __tablename__ = "user_files"
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)      # stored on disk
    original_name = Column(String, nullable=False)
    mime_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FinanceEntry(Base):
    __tablename__ = "finance_entries"
    id = Column(Integer, primary_key=True, index=True)
    entry_type = Column(String, nullable=False)    # "income" | "expense"
    amount = Column(Float, nullable=False)
    category = Column(String, nullable=False)
    description = Column(String, nullable=True)
    date = Column(String, nullable=False)          # YYYY-MM-DD
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ── AI Lab Models ────────────────────────────────────────────────────────────

class AIModel(Base):
    """
    An AI model that aggregates trading data from connected bots + uploaded files.
    Data persists permanently — even if connected bots are later deleted.
    """
    __tablename__ = "ai_models"

    id                  = Column(Integer, primary_key=True, index=True)
    user_id             = Column(Integer, ForeignKey("users.id"), nullable=False)
    name                = Column(String, nullable=False)
    description         = Column(String, default="")
    connected_bot_ids   = Column(JSON, default=list)    # [1, 3, 7, ...]

    # Status & metrics
    status              = Column(String, default="idle")  # idle | training | ready | error
    total_data_points   = Column(Integer, default=0)
    last_trained_at     = Column(DateTime(timezone=True), nullable=True)
    created_at          = Column(DateTime(timezone=True), server_default=func.now())

    # Training configuration
    live_sync           = Column(Boolean, default=False)    # auto-sync every new trade
    training_mode       = Column(String, default="backtest") # backtest | live
    training_frequency  = Column(String, default="manual")   # manual | every_25 | every_50 | daily
    data_weight         = Column(String, default="balanced")  # balanced | recent | historical
    learn_risk          = Column(Boolean, default=True)       # learn stop-loss / sizing

    # Counters for auto-training
    trades_since_train  = Column(Integer, default=0)

    training_runs = relationship("TrainingRun", back_populates="model",
                                 cascade="all, delete-orphan")
    files         = relationship("ModelFile", back_populates="model",
                                 cascade="all, delete-orphan")


class TrainingRun(Base):
    """One completed (or failed) training job for an AIModel."""
    __tablename__ = "training_runs"

    id           = Column(Integer, primary_key=True, index=True)
    model_id     = Column(Integer, ForeignKey("ai_models.id"), nullable=False)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    started_at   = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_sec = Column(Float, nullable=True)
    status       = Column(String, default="running")   # running | completed | failed
    data_summary = Column(JSON, nullable=True)          # record counts, bot names, files used …
    performance  = Column(JSON, nullable=True)          # win_rate, pnl, risk, recommendations …
    error_msg    = Column(Text, nullable=True)

    model = relationship("AIModel", back_populates="training_runs")


class ModelFile(Base):
    """A user-uploaded file attached to an AIModel for training."""
    __tablename__ = "model_files"

    id            = Column(Integer, primary_key=True, index=True)
    model_id      = Column(Integer, ForeignKey("ai_models.id"), nullable=False)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    filename      = Column(String, nullable=False)       # disk name (uuid-prefixed)
    original_name = Column(String, nullable=False)       # user-visible name
    file_type     = Column(String, nullable=True)        # csv | json | jsonl | txt | pdf | xlsx
    size_bytes    = Column(Integer, default=0)
    record_count  = Column(Integer, default=0)           # parsed data rows (0 for docs)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    model = relationship("AIModel", back_populates="files")


# ── Community Chat ────────────────────────────────────────────────────────────

class ChatMessage(Base):
    """Persisted chat message — recipient_id=None means group/community chat."""
    __tablename__ = "chat_messages"

    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    sender_id     = Column(String, nullable=False, index=True)
    sender_name   = Column(String, nullable=False)
    sender_avatar = Column(String, nullable=True)        # relative URL or None
    recipient_id  = Column(String, nullable=True, index=True)  # None = group chat
    content       = Column(Text,   nullable=True)
    message_type  = Column(String, default="text")       # text | image | file
    file_name     = Column(String, nullable=True)        # stored filename (UUID)
    file_original = Column(String, nullable=True)        # original filename shown to user
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


# ── Whop Integration ──────────────────────────────────────────────────────────

class WhopMembership(Base):
    """
    Stores the Whop membership record created when a user authenticates
    with an access code.  One record per verified license key.
    Re-verification updates the existing record rather than inserting a new one.
    """
    __tablename__ = "whop_memberships"

    id             = Column(Integer, primary_key=True, index=True)
    user_id        = Column(Integer, ForeignKey("users.id"), nullable=False)
    membership_id  = Column(String, nullable=False, index=True)  # Whop mem_xxx
    whop_user_id   = Column(String, nullable=True)               # Whop user_xxx
    license_key    = Column(String, nullable=False, unique=True)  # The access code
    status         = Column(String, nullable=False, default="active")  # active | expired | canceled
    plan_name      = Column(String, nullable=True)
    whop_email     = Column(String, nullable=True)
    whop_username  = Column(String, nullable=True)
    verified_at    = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", backref="whop_memberships")
