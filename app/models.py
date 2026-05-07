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

    # Supabase user UUID - populated on first authenticated request.
    supabase_uid = Column(String, index=True, nullable=True)

    bot_logs = relationship("BotLog", back_populates="user", cascade="all, delete-orphan")
    trades   = relationship("Trade",  back_populates="user", cascade="all, delete-orphan")


class BotLog(Base):
    """
    High-volume runtime log buffer. bot_id is the Supabase UUID of the bot
    (string) - bots themselves no longer live in this DB.
    """
    __tablename__ = "bot_logs"
    id         = Column(Integer, primary_key=True, index=True)
    bot_id     = Column(String, index=True, nullable=False)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    level      = Column(Enum(LogLevel), default=LogLevel.INFO)
    message    = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="bot_logs")


class Trade(Base):
    """
    Trade analytics buffer. bot_id is the Supabase UUID of the bot (string).
    """
    __tablename__ = "trades"
    id          = Column(Integer, primary_key=True, index=True)
    bot_id      = Column(String, index=True, nullable=False)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol      = Column(String, nullable=False)
    side        = Column(String, nullable=False)
    entry_price = Column(Float, nullable=True)
    exit_price  = Column(Float, nullable=True)
    quantity    = Column(Float, nullable=True)
    pnl         = Column(Float, nullable=True)
    note        = Column(String, nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="trades")


# ── Dashboard Personal Widgets ────────────────────────────────────────────────

class Photo(Base):
    __tablename__ = "photos"
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)
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
    filename = Column(String, nullable=False)
    original_name = Column(String, nullable=False)
    mime_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FinanceEntry(Base):
    __tablename__ = "finance_entries"
    id = Column(Integer, primary_key=True, index=True)
    entry_type = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    category = Column(String, nullable=False)
    description = Column(String, nullable=True)
    date = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ── AI Lab Models ────────────────────────────────────────────────────────────

class AIModel(Base):
    """
    An AI model that aggregates trading data from connected bots + uploaded files.
    Data persists permanently - even if connected bots are later deleted.
    """
    __tablename__ = "ai_models"

    id                  = Column(Integer, primary_key=True, index=True)
    user_id             = Column(Integer, ForeignKey("users.id"), nullable=False)
    name                = Column(String, nullable=False)
    description         = Column(String, default="")
    connected_bot_ids   = Column(JSON, default=list)

    status              = Column(String, default="idle")
    total_data_points   = Column(Integer, default=0)
    last_trained_at     = Column(DateTime(timezone=True), nullable=True)
    created_at          = Column(DateTime(timezone=True), server_default=func.now())

    live_sync           = Column(Boolean, default=False)
    training_mode       = Column(String, default="backtest")
    training_frequency  = Column(String, default="manual")
    data_weight         = Column(String, default="balanced")
    learn_risk          = Column(Boolean, default=True)

    trades_since_train  = Column(Integer, default=0)

    training_runs = relationship("TrainingRun", back_populates="model",
                                 cascade="all, delete-orphan")
    files         = relationship("ModelFile", back_populates="model",
                                 cascade="all, delete-orphan")


class TrainingRun(Base):
    __tablename__ = "training_runs"

    id           = Column(Integer, primary_key=True, index=True)
    model_id     = Column(Integer, ForeignKey("ai_models.id"), nullable=False)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    started_at   = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_sec = Column(Float, nullable=True)
    status       = Column(String, default="running")
    data_summary = Column(JSON, nullable=True)
    performance  = Column(JSON, nullable=True)
    error_msg    = Column(Text, nullable=True)

    model = relationship("AIModel", back_populates="training_runs")


class ModelFile(Base):
    __tablename__ = "model_files"

    id            = Column(Integer, primary_key=True, index=True)
    model_id      = Column(Integer, ForeignKey("ai_models.id"), nullable=False)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    filename      = Column(String, nullable=False)
    original_name = Column(String, nullable=False)
    file_type     = Column(String, nullable=True)
    size_bytes    = Column(Integer, default=0)
    record_count  = Column(Integer, default=0)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    model = relationship("AIModel", back_populates="files")


# ── Community Chat ────────────────────────────────────────────────────────────

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    sender_id     = Column(String, nullable=False, index=True)
    sender_name   = Column(String, nullable=False)
    sender_avatar = Column(String, nullable=True)
    recipient_id  = Column(String, nullable=True, index=True)
    content       = Column(Text,   nullable=True)
    message_type  = Column(String, default="text")
    file_name     = Column(String, nullable=True)
    file_original = Column(String, nullable=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


# ── Whop Integration ──────────────────────────────────────────────────────────

class WhopMembership(Base):
    __tablename__ = "whop_memberships"

    id             = Column(Integer, primary_key=True, index=True)
    user_id        = Column(Integer, ForeignKey("users.id"), nullable=False)
    membership_id  = Column(String, nullable=False, index=True)
    whop_user_id   = Column(String, nullable=True)
    license_key    = Column(String, nullable=False, unique=True)
    status         = Column(String, nullable=False, default="active")
    plan_name      = Column(String, nullable=True)
    whop_email     = Column(String, nullable=True)
    whop_username  = Column(String, nullable=True)
    verified_at    = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", backref="whop_memberships")
