"""
auth.py  -  Authentication utilities for WATCH-DOG
===================================================
Handles:
  - Default system user (keeps existing behaviour intact)
  - JWT creation / verification  (added for Whop integration)
  - Password hashing helpers     (used by legacy register/login endpoints)
  - Supabase JWT validation      (for cloud-authenticated requests)

Note: bot data lives in Supabase. This module no longer mirrors bots
locally - it only validates the user identity and provisions the local
singleton User row.
"""

import os
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .database import get_db, SessionLocal
from . import models

# ── Config ────────────────────────────────────────────────────────────────────
SECRET_KEY              = os.getenv("SECRET_KEY", "watchdog-secret-change-this")
ALGORITHM               = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = int(os.getenv("TOKEN_EXPIRE_DAYS", "30"))

DEFAULT_USERNAME = "watchdog"
DEFAULT_EMAIL    = "watchdog@local"

# ── Helpers ───────────────────────────────────────────────────────────────────
pwd_context      = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme    = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


# ── Password helpers ──────────────────────────────────────────────────────────

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def authenticate_user(db: Session, username: str, password: str) -> Optional[models.User]:
    user = db.query(models.User).filter(models.User.username == username).first()
    if not user or not user.hashed_password:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


# ── JWT helpers ───────────────────────────────────────────────────────────────

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta if expires_delta
        else timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def get_current_user_jwt(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exception

    payload = decode_token(token)
    if payload is None:
        raise credentials_exception

    user_id: Optional[int] = payload.get("user_id")
    if user_id is None:
        username: Optional[str] = payload.get("sub")
        if username is None:
            raise credentials_exception
        user = db.query(models.User).filter(models.User.username == username).first()
    else:
        user = db.query(models.User).filter(models.User.id == user_id).first()

    if user is None or not user.is_active:
        raise credentials_exception
    return user


# ─────────────────────────────────────────────────────────────────────────────
# SUPABASE AUTH
# ─────────────────────────────────────────────────────────────────────────────
# Validates a Supabase JWT against /auth/v1/user. Auto-provisions the local
# singleton User (id=1) and stamps supabase_uid on it.
# ─────────────────────────────────────────────────────────────────────────────

SUPABASE_URL      = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
_SUPABASE_CACHE_TTL = 60
_SUPABASE_CACHE_MAX = 256

_supabase_cache: dict[str, Tuple[float, dict]] = {}
_supabase_cache_lock = threading.Lock()


def _cache_get(token: str) -> Optional[dict]:
    now = time.time()
    with _supabase_cache_lock:
        entry = _supabase_cache.get(token)
        if not entry:
            return None
        exp, data = entry
        if exp < now:
            _supabase_cache.pop(token, None)
            return None
        return data


def _cache_put(token: str, data: dict) -> None:
    with _supabase_cache_lock:
        if len(_supabase_cache) >= _SUPABASE_CACHE_MAX:
            oldest = min(_supabase_cache.items(), key=lambda kv: kv[1][0])[0]
            _supabase_cache.pop(oldest, None)
        _supabase_cache[token] = (time.time() + _SUPABASE_CACHE_TTL, data)


def _bearer_from_request(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def _validate_with_supabase(token: str) -> Optional[dict]:
    cached = _cache_get(token)
    if cached is not None:
        return cached
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    url = f"{SUPABASE_URL}/auth/v1/user"
    headers = {
        "apikey":        SUPABASE_ANON_KEY,
        "authorization": f"Bearer {token}",
        "accept":        "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or not data.get("id"):
            return None
        _cache_put(token, data)
        return data
    except (httpx.HTTPError, ValueError) as e:
        print(f"[auth] supabase validation failed: {e}")
        return None


def _provision_user_from_supabase(db: Session, sb_user: dict) -> models.User:
    """Single-user desktop model: collapse every Supabase identity onto the
    LOCAL singleton (id=1) and stamp supabase_uid on it."""
    uid   = sb_user["id"]
    email = (sb_user.get("email") or "").lower().strip() or None

    user = db.query(models.User).filter(models.User.id == 1).first()
    if not user:
        user = models.User(
            id=1, username="watchdog", email="watchdog@local",
            hashed_password="", is_active=True,
        )
        db.add(user); db.commit(); db.refresh(user)

    changed = False
    if user.supabase_uid != uid:
        existing = (db.query(models.User)
                    .filter(models.User.supabase_uid == uid,
                            models.User.id != 1).first())
        if not existing:
            user.supabase_uid = uid
            changed = True
    if email and (not user.email or user.email == "watchdog@local") and user.email != email:
        user.email = email
        changed = True
    if changed:
        try:
            db.commit()
        except Exception:
            db.rollback()
    return user


async def get_current_user_supabase(
    request: Request,
    db: Session = Depends(get_db),
) -> models.User:
    token = _bearer_from_request(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase auth not configured on this server (set SUPABASE_URL + SUPABASE_ANON_KEY)",
        )

    sb_user = await _validate_with_supabase(token)
    if not sb_user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    user = _provision_user_from_supabase(db, sb_user)
    if not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is inactive")
    return user


async def get_current_user_cloud(
    request: Request,
    db: Session = Depends(get_db),
) -> "models.User":
    """Cloud-aware auth dep. If a Bearer token validates as a Supabase JWT,
    provisions/stamps the local singleton and returns it. Otherwise falls
    back to the default user (legacy / Whop-licensed sessions)."""
    token = _bearer_from_request(request)
    if not token:
        return get_default_user(db=db)

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return get_default_user(db=db)

    sb_user = await _validate_with_supabase(token)
    if not sb_user:
        return get_default_user(db=db)

    user = _provision_user_from_supabase(db, sb_user)
    if not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is inactive")
    return user


# ── Default user (keeps all existing routes working unchanged) ────────────────

def ensure_default_user():
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == 1).first()
        if not user:
            user = models.User(
                username=DEFAULT_USERNAME,
                email=DEFAULT_EMAIL,
                hashed_password="",
                is_active=True,
            )
            db.add(user)
            db.commit()
    finally:
        db.close()


def get_default_user(db: Session = Depends(get_db)) -> models.User:
    user = db.query(models.User).filter(models.User.id == 1).first()
    if not user:
        user = models.User(
            username=DEFAULT_USERNAME,
            email=DEFAULT_EMAIL,
            hashed_password="",
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
