from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime
from .models import BotStatus, LogLevel


# ── Whop Auth ─────────────────────────────────────────────────────────────────

class WhopVerifyRequest(BaseModel):
    """Body sent from the frontend when the user submits their access code."""
    license_key: str

class WhopMembershipInfo(BaseModel):
    membership_id: str
    status: str
    plan_name: Optional[str]
    whop_username: Optional[str]
    whop_email: Optional[str]
    verified_at: datetime
    class Config:
        from_attributes = True

class WhopVerifyResponse(BaseModel):
    """Returned on successful verification."""
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"
    membership: WhopMembershipInfo


# ── Auth ─────────────────────────────────────────────────────────────────────
class UserCreate(BaseModel):
    email: EmailStr
    username: str
    password: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class UserOut(BaseModel):
    id: int
    email: str
    username: str
    is_active: bool
    created_at: datetime
    class Config:
        from_attributes = True

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"
    is_subscribed: bool

class SubscriptionStatus(BaseModel):
    is_subscribed: bool
    status: str  # "active" | "inactive"

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None


# ── Bots ──────────────────────────────────────────────────────────────────────
class BotCreate(BaseModel):
    name: str
    description: Optional[str] = None
    code: str

class BotUpdate(BaseModel):
    # Core
    name: Optional[str] = None
    description: Optional[str] = None
    code: Optional[str] = None
    # Run schedule
    schedule_type: Optional[str] = None
    schedule_start: Optional[str] = None
    schedule_end: Optional[str] = None
    # Risk management
    max_amount_per_trade: Optional[float] = None
    max_contracts_per_trade: Optional[int] = None
    max_daily_loss: Optional[float] = None
    # General
    auto_restart: Optional[bool] = None

class BotOut(BaseModel):
    id: int
    name: str
    description: Optional[str]
    code: str
    status: BotStatus
    run_count: int
    bot_secret: str
    created_at: datetime
    last_run_at: Optional[datetime]
    # Settings
    schedule_type: str
    schedule_start: Optional[str]
    schedule_end: Optional[str]
    max_amount_per_trade: Optional[float]
    max_contracts_per_trade: Optional[int]
    max_daily_loss: Optional[float]
    auto_restart: bool
    cloud_id: Optional[str] = None
    class Config:
        from_attributes = True


# ── Logs ──────────────────────────────────────────────────────────────────────
class BotLogOut(BaseModel):
    id: int
    bot_id: int
    level: LogLevel
    message: str
    created_at: datetime
    class Config:
        from_attributes = True


# ── API Connections ───────────────────────────────────────────────────────────
class ApiConnectionCreate(BaseModel):
    bot_id: int
    name: str
    base_url: str = ''
    api_key: Optional[str] = None
    api_secret: Optional[str] = None

class ApiConnectionOut(BaseModel):
    id: int
    bot_id: int
    name: str
    base_url: Optional[str]
    api_key: Optional[str]
    is_active: bool
    cloud_id: Optional[str] = None
    created_at: datetime
    class Config:
        from_attributes = True


# ── Trades ────────────────────────────────────────────────────────────────────
class TradeCreate(BaseModel):
    symbol: str
    side: str
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    quantity: Optional[float] = None
    pnl: Optional[float] = None
    note: Optional[str] = None

class TradeOut(BaseModel):
    id: int
    bot_id: int
    symbol: str
    side: str
    entry_price: Optional[float]
    exit_price: Optional[float]
    quantity: Optional[float]
    pnl: Optional[float]
    note: Optional[str]
    created_at: datetime
    class Config:
        from_attributes = True

class TradeStats(BaseModel):
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    total_winning: float
    total_losing: float


# ── Dashboard ─────────────────────────────────────────────────────────────────
class DashboardStats(BaseModel):
    total_bots: int
    running_bots: int
    total_runs: int
    total_trades: int
    recent_logs: List[BotLogOut]


# ── AI Fix ────────────────────────────────────────────────────────────────────
class AiFixRequest(BaseModel):
    error_logs: List[str]           # Recent log lines (ERROR/WARNING) sent as context
    extra_context: Optional[str] = None  # Any extra user note


class AiFixChange(BaseModel):
    description: str                # Human-readable summary of the change
    old_code: str                   # Original lines (may be empty for pure inserts)
    new_code: str                   # Replacement lines


class AiFixResponse(BaseModel):
    explanation: str                # Natural-language explanation of the root cause & fix
    changes: List[AiFixChange]      # Structured diff list
    fixed_code: str                 # Full patched code, ready to apply
