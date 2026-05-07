from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime
from .models import LogLevel


# ── Whop Auth ─────────────────────────────────────────────────────────────────

class WhopVerifyRequest(BaseModel):
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
    status: str

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None


# ── Logs ──────────────────────────────────────────────────────────────────────
class BotLogOut(BaseModel):
    id: int
    bot_id: str            # Supabase UUID
    level: LogLevel
    message: str
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
    bot_id: str            # Supabase UUID
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
    error_logs: List[str]
    extra_context: Optional[str] = None


class AiFixChange(BaseModel):
    description: str
    old_code: str
    new_code: str


class AiFixResponse(BaseModel):
    explanation: str
    changes: List[AiFixChange]
    fixed_code: str
