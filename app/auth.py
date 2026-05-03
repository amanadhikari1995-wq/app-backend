"""
auth.py  —  Authentication utilities for WATCH-DOG
===================================================
Handles:
  • Default system user (keeps existing behaviour intact)
  • JWT creation / verification  (added for Whop integration)
  • Password hashing helpers     (used by legacy register/login endpoints)
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


# ─────────────────────────────────────────────────────────────────────────────
# SUPABASE AUTH (step 2 of the cloud-sync rollout)
# ─────────────────────────────────────────────────────────────────────────────
# A new dependency `get_current_user_supabase` that validates a Supabase JWT
# by calling Supabase's `/auth/v1/user` endpoint and auto-provisions a local
# `User` row keyed by `supabase_uid` if missing.
#
# DESIGN NOTES
#   1. We do NOT verify the JWT signature locally — that would require
#      shipping `SUPABASE_JWT_SECRET` to every desktop install, which would
#      let any user decode/forge any other user's tokens. Calling the
#      Supabase REST endpoint with the bearer token validates the signature
#      AND returns the canonical user record in one round-trip.
#   2. Validation results cache for ~60s per token (in-process LRU). After
#      the first call, validation costs a dict lookup. Cache survives until
#      the token expires or gets evicted.
#   3. If env vars (SUPABASE_URL + SUPABASE_ANON_KEY) aren't set we degrade
#      gracefully to "no Supabase auth available" — routers that depend on
#      this function will return 503 with a clear message instead of a
#      misleading 401.
#   4. NO EXISTING ROUTER USES THIS YET. Step 2 is purely additive: this
#      function is exported and ready, but `get_current_user` (the alias
#      every router actually imports) still resolves to `get_default_user`.
#      Per-router migration starts in step 5.
# ─────────────────────────────────────────────────────────────────────────────

SUPABASE_URL      = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
_SUPABASE_CACHE_TTL = 60          # seconds
_SUPABASE_CACHE_MAX = 256         # LRU cap — most users have ≤2 active tokens

# token → (expires_at_epoch, supabase_user_dict)
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
        # Trivial LRU eviction — drop the oldest if we exceed the cap.
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
    """Returns the Supabase user dict (with `id`, `email`, `user_metadata`)
    or None if the token is invalid / expired / Supabase is unreachable."""
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
        # Network glitch or bad JSON — refuse to validate. Caller returns 401.
        # Don't cache failures; the token might be valid on the next request.
        print(f"[auth] supabase validation failed: {e}")
        return None


def _provision_user_from_supabase(db: Session, sb_user: dict) -> models.User:
    """SINGLE-USER DESKTOP MODEL.

    The desktop app is, in practice, single-user: one human signs into one
    Windows account and uses one WatchDog install. We therefore collapse
    every Supabase identity onto the LOCAL singleton (id=1). Storing the
    supabase_uid on that singleton is enough to identify the user to the
    cloud — no need for a parallel local user_id=2.

    Why this matters: previously this function created a SECOND local user
    row when a Supabase JWT validated. The renderer's CRUD then attributed
    bots to user_id=2, while the sync_engine kept moving them back to
    user_id=1, and the two paths fought every cycle. Single-user model =
    no fight = bots stay where they are.

    The supabase_uid + email get stamped onto user_id=1 idempotently.
    Existing user_id != 1 rows are NOT touched here — sync_engine handles
    cleanup of those, and we want this dep to be fast (no DELETEs).
    """
    uid   = sb_user["id"]
    email = (sb_user.get("email") or "").lower().strip() or None

    user = db.query(models.User).filter(models.User.id == 1).first()
    if not user:
        # Should never happen — ensure_default_user creates this on startup.
        user = models.User(
            id=1, username="watchdog", email="watchdog@local",
            hashed_password="", is_active=True,
        )
        db.add(user); db.commit(); db.refresh(user)

    # Idempotent stamp. We only update if the value actually differs to
    # avoid no-op writes that touch updated_at (the Bot table has triggers
    # in some configs).
    changed = False
    if user.supabase_uid != uid:
        # Defensive: if a different row already holds this uid (legacy
        # orphan), unique-index UPDATE would fail. Sync engine cleans those
        # up; here we just skip the stamp on conflict — next request will
        # try again after sync engine clears the orphan.
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
            # Don't let a stamp failure block the request — caller still
            # gets the singleton user, which is what they need.
    return user


async def get_current_user_supabase(
    request: Request,
    db: Session = Depends(get_db),
) -> models.User:
    """FastAPI dependency. Returns the local `User` whose `supabase_uid`
    matches the Bearer token's owner. Auto-provisions on first call.
    Raises 401 if the token is missing/invalid; 503 if Supabase auth is
    not configured on this instance."""
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


# ─────────────────────────────────────────────────────────────────────────────
# CLOUD-SYNCED USER DEPENDENCY (step 3 — used by the bots router)
# ─────────────────────────────────────────────────────────────────────────────
# Same as get_current_user_supabase, but also performs a one-shot cloud→local
# sync the FIRST time a given Supabase user authenticates against this Python
# process. After the sync, the cloud_sync_migrations table marks the user
# done; subsequent calls skip the sync (cheap dict lookup against a process-
# local set).
#
# Falls back to get_default_user when no Bearer token is present, so legacy
# desktop sessions (Whop license, no Supabase login) keep working unchanged.
#
# The cloud sync is INTENTIONALLY non-fatal: any cloud failure logs a warning
# and returns the local user. The user can always work offline; we just don't
# get cross-device sync until the next authenticated request.
# ─────────────────────────────────────────────────────────────────────────────

# Throttle: re-pull from cloud at most once every N seconds per user. Without
# this the auth dep would hit Supabase on every request. With it, the user
# sees changes from another device within a few seconds of any authed call.
_last_pull: dict[str, float] = {}                 # uid → epoch seconds
_last_pull_lock = threading.Lock()
_PULL_INTERVAL_S = 6.0                            # tune: lower = fresher, more bandwidth


def _should_pull_now(uid: str) -> bool:
    now = time.time()
    with _last_pull_lock:
        last = _last_pull.get(uid, 0.0)
        if (now - last) < _PULL_INTERVAL_S:
            return False
        _last_pull[uid] = now
        return True


def _build_local_bot_from_cloud(user_id: int, cb: dict) -> "models.Bot":
    """Translate a cloud `bots` row into a local SQLAlchemy Bot instance."""
    return models.Bot(
        user_id        = user_id,
        cloud_id       = cb.get("id"),
        cloud_synced_at= datetime.now(timezone.utc),
        name           = cb.get("name") or "Untitled",
        description    = cb.get("description"),
        code           = cb.get("code") or "",
        bot_type       = cb.get("bot_type"),
        schedule_type  = cb.get("schedule_type") or "always",
        schedule_start = cb.get("schedule_start"),
        schedule_end   = cb.get("schedule_end"),
        max_amount_per_trade    = cb.get("max_amount_per_trade"),
        max_contracts_per_trade = cb.get("max_contracts_per_trade"),
        max_daily_loss          = cb.get("max_daily_loss"),
        auto_restart   = bool(cb.get("auto_restart") or False),
    )


def _apply_cloud_to_local_bot(local: "models.Bot", cb: dict) -> bool:
    """Update an existing local Bot row from a cloud row. Returns True if
    any field actually changed (so callers know whether to commit)."""
    changed = False
    for col_local, col_cloud in (
        ("name", "name"), ("description", "description"), ("code", "code"),
        ("bot_type", "bot_type"),
        ("schedule_type", "schedule_type"),
        ("schedule_start", "schedule_start"), ("schedule_end", "schedule_end"),
        ("max_amount_per_trade", "max_amount_per_trade"),
        ("max_contracts_per_trade", "max_contracts_per_trade"),
        ("max_daily_loss", "max_daily_loss"),
        ("auto_restart", "auto_restart"),
    ):
        v = cb.get(col_cloud)
        if v is None:
            continue
        if getattr(local, col_local) != v:
            setattr(local, col_local, v)
            changed = True
    if changed:
        local.cloud_synced_at = datetime.now(timezone.utc)
    return changed


async def _maybe_sync_user(db: Session, user: "models.User", token: str) -> None:
    """Cloud↔local reconciliation for the current Supabase user. Runs at most
    once every _PULL_INTERVAL_S seconds per user (throttle), never blocks
    authentication on cloud failure.

    Two phases, two transactions (so a failure in phase 2 doesn't undo phase 1):
      Phase 1 — PULL: for every cloud bot, upsert the local row by cloud_id.
                Adopt legacy default-user (id=1) bots into this user on first
                run by re-attributing them.
      Phase 2 — PUSH: any local bot for this user without a cloud_id gets
                pushed up. Cloud returns the inserted rows; we match by
                position (PostgREST preserves order) — not by name, which
                isn't unique."""
    uid = user.supabase_uid
    if not uid:
        return
    if not _should_pull_now(uid):
        return

    from . import cloud_client

    # ── PHASE 0: Adopt legacy default-user bots (H6) ───────────────────────
    # Only runs once: skipped if the user has any bots already. Re-attributes
    # bots stranded under user_id=1 to the Supabase user so they sync up.
    try:
        already_owns = (db.query(models.Bot)
                        .filter(models.Bot.user_id == user.id).count())
        if already_owns == 0 and user.id != 1:
            # Adopt the singleton "watchdog" user's bots — common for fresh
            # Supabase logins on a desktop that previously ran legacy auth.
            adopted = (db.query(models.Bot)
                       .filter(models.Bot.user_id == 1)
                       .update({"user_id": user.id}, synchronize_session=False))
            if adopted:
                db.commit()
                print(f"[auth] adopted {adopted} legacy bot(s) into user {uid[:8]}")
    except Exception as e:
        db.rollback()
        print(f"[auth] adoption skipped: {e}")

    # ── PHASE 1: PULL cloud → local ───────────────────────────────────────
    new_locals = 0
    updated_locals = 0
    cloud_bots: list = []
    try:
        cloud_bots = await cloud_client.list_cloud_bots(token) or []
        for cb in cloud_bots:
            cid = cb.get("id")
            if not cid:
                continue
            local = (db.query(models.Bot)
                     .filter(models.Bot.cloud_id == cid).first())
            if local:
                if _apply_cloud_to_local_bot(local, cb):
                    updated_locals += 1
            else:
                db.add(_build_local_bot_from_cloud(user.id, cb))
                new_locals += 1
        if new_locals or updated_locals:
            db.commit()
            print(f"[auth] cloud-sync pull: +{new_locals} new, {updated_locals} updated for {uid[:8]}")
    except Exception as e:
        db.rollback()
        print(f"[auth] cloud-sync pull failed (will retry): {e}")
        return  # don't attempt push if pull failed — likely network issue

    # ── PHASE 2: PUSH local → cloud (only on first sync) ──────────────────
    try:
        already = await cloud_client.has_migrated(token, uid)
        if not already:
            local_unsynced = (db.query(models.Bot)
                              .filter(models.Bot.user_id == user.id,
                                      models.Bot.cloud_id.is_(None))
                              .all())
            if local_unsynced:
                inserted = await cloud_client.upsert_bots_batch(token, uid, local_unsynced)
                # H4 fix: match by index (PostgREST guarantees response order
                # mirrors request order) instead of by name (not unique).
                for local_bot, cloud_row in zip(local_unsynced, inserted):
                    cid = (cloud_row or {}).get("id")
                    if cid:
                        local_bot.cloud_id = cid
                        local_bot.cloud_synced_at = datetime.now(timezone.utc)
                if local_unsynced:
                    db.commit()
            await cloud_client.mark_migration_done(token, uid, len(local_unsynced))
    except Exception as e:
        db.rollback()
        print(f"[auth] cloud-sync push failed (will retry): {e}")


async def get_current_user_cloud(
    request: Request,
    db: Session = Depends(get_db),
) -> "models.User":
    """Cloud-aware auth dep. If a Bearer token is present, validates it via
    Supabase, auto-provisions the local user, and runs a one-shot cloud sync.
    Falls back to the default user when no token is supplied."""
    token = _bearer_from_request(request)
    if not token:
        # Legacy / Whop-license / no-Supabase-login path.
        return get_default_user(db=db)

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        # Cloud not configured on this build — degrade silently to default user
        # so we don't 503 every request just because env wasn't set.
        return get_default_user(db=db)

    sb_user = await _validate_with_supabase(token)
    if not sb_user:
        # Token didn't validate as Supabase. Could be:
        #   - Legacy Whop login (different JWT signing key)
        #   - Local-issued JWT from /api/auth/login (own SECRET_KEY)
        #   - Expired or different-project Supabase token
        # Fall back to the default user — same behaviour as the original
        # get_default_user dep. This is the desktop app, single-user; there's
        # no tenant isolation to violate by serving the singleton's data.
        # Cloud sync simply won't fire for this request — it'll re-attempt
        # next request, and start working as soon as the user signs in via
        # the new Supabase flow.
        return get_default_user(db=db)

    user = _provision_user_from_supabase(db, sb_user)
    if not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is inactive")

    await _maybe_sync_user(db, user, token)
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
