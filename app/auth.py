"""
auth.py  —  Authentication utilities for WATCH-DOG
===================================================
Handles:
  • Default system user (keeps existing behaviour intact)
  • JWT creation / verification  (added for Whop integration)
  • Password hashing helpers     (used by legacy register/login endpoints)
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
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
    """
    Create a signed JWT.
    Always stores user_id and whop_membership_id in payload so
    get_current_user_jwt can look up the correct user.
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta if expires_delta
        else timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT. Returns payload dict or None on failure."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def get_current_user_jwt(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    """
    FastAPI dependency — validates Bearer JWT and returns the matching User.
    Raises HTTP 401 if the token is missing, invalid, or expired.
    """
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
        # Fallback: legacy tokens that only carry "sub" (username)
        username: Optional[str] = payload.get("sub")
        if username is None:
            raise credentials_exception
        user = db.query(models.User).filter(models.User.username == username).first()
    else:
        user = db.query(models.User).filter(models.User.id == user_id).first()

    if user is None or not user.is_active:
        raise credentials_exception
    return user


# ── Default user (keeps all existing routes working unchanged) ────────────────

def ensure_default_user():
    """Create the single default user (id=1) if it doesn't exist yet."""
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
    """Always returns the single system user (id=1), creating it on demand."""
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
