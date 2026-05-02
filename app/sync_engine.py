"""
sync_engine.py — Bulletproof bidirectional bot sync between local SQLite and
Supabase Postgres.

ARCHITECTURE
============
A single daemon thread that wakes up every SYNC_INTERVAL_S and runs one
reconciliation cycle:

    1. Read JWT from session.json (canonical source, also used by wd_cloud.py).
    2. PULL: GET /rest/v1/bots from Supabase. For each cloud row:
         - if no local row with matching cloud_id → INSERT into local SQLite
         - if local row exists AND cloud.updated_at > local.cloud_updated_at
           → UPDATE local from cloud
    3. PUSH: scan local for rows that diverge from cloud:
         - bots without cloud_id  → POST to /rest/v1/bots, stamp returned id
         - local.updated_at > local.cloud_updated_at → PATCH cloud
    4. DELETE PROPAGATION: cloud rows missing from local → DELETE local
       (this is intentional — last-write-wins; rely on cloud as source of truth).

Failure modes are explicit:
    - session.json missing            → sync paused, no cloud writes attempted
    - JWT expired                     → sync paused, log + retry next cycle
    - Supabase 5xx / network error    → log + retry next cycle, never crash
    - Conflict (both sides edited)    → cloud's updated_at wins (newer)

State accessible via get_status() so the UI / a /api/sync/status endpoint can
surface "syncing", "paused", "error", last sync time, items pending.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from .database import SessionLocal
from . import models

log = logging.getLogger("watchdog.sync")

SUPABASE_URL      = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

SYNC_INTERVAL_S   = float(os.getenv("WATCHDOG_SYNC_INTERVAL_S", "5.0"))
HTTP_TIMEOUT_S    = 10.0

# Columns we mirror in both directions. Keep in lockstep with sql/cloud-sync.sql
# AND models.Bot. Anything not in this list stays local-only (e.g. status,
# run_count, is_running — those are runtime state managed locally).
SYNCED_FIELDS = (
    "name", "description", "code", "bot_type",
    "schedule_type", "schedule_start", "schedule_end",
    "max_amount_per_trade", "max_contracts_per_trade", "max_daily_loss",
    "auto_restart",
)


# ── Status surface for /api/sync/status ─────────────────────────────────────

@dataclass
class SyncStatus:
    state:           str   = "idle"              # idle|syncing|paused|error
    last_sync_at:    Optional[str] = None        # ISO-8601
    last_error:      Optional[str] = None
    pulls_total:     int   = 0                   # cumulative cloud→local writes
    pushes_total:    int   = 0                   # cumulative local→cloud writes
    cycles_total:    int   = 0
    last_cycle_ms:   Optional[float] = None
    paused_reason:   Optional[str] = None
    supabase_uid:    Optional[str] = None        # who we're syncing as

    def snapshot(self) -> dict:
        return asdict(self)

_status = SyncStatus()
_status_lock = threading.Lock()


def get_status() -> dict:
    with _status_lock:
        return _status.snapshot()


def _set_status(**kwargs) -> None:
    with _status_lock:
        for k, v in kwargs.items():
            setattr(_status, k, v)


# ── session.json + JWT ──────────────────────────────────────────────────────

def _user_data_dir() -> Path:
    """Mirror the path computed by run_backend.py + Electron main.js."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "WatchDog"
    if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        return Path.home() / "Library" / "Application Support" / "WatchDog"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "WatchDog"


def _read_session() -> Optional[dict]:
    """Return the session.json contents or None. Never raises."""
    try:
        p = _user_data_dir() / "session.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("session.json read failed: %s", e)
        return None


def _is_jwt_expired(jwt: str) -> bool:
    """Cheap exp check — decodes JWT payload without verifying signature.
    We don't need verification here because the cloud will reject expired
    tokens with 401 anyway; this just lets us skip pointless network calls."""
    try:
        import base64
        parts = jwt.split(".")
        if len(parts) != 3:
            return True
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if not exp:
            return False
        return int(exp) < int(time.time()) + 30  # 30s safety margin
    except Exception:
        return True


# ── Cloud HTTP helpers ──────────────────────────────────────────────────────

def _headers(jwt: str, prefer_return: bool = True) -> dict:
    h = {
        "apikey":        SUPABASE_ANON_KEY,
        "authorization": f"Bearer {jwt}",
        "accept":        "application/json",
        "content-type":  "application/json",
    }
    if prefer_return:
        h["prefer"] = "return=representation"
    return h


def _bot_payload(bot: models.Bot, supabase_uid: str) -> dict:
    out: dict[str, Any] = {"user_id": supabase_uid}
    for f in SYNCED_FIELDS:
        v = getattr(bot, f, None)
        if v is not None:
            out[f] = v
    return out


def _pull_cloud_bots(client: httpx.Client, jwt: str) -> Optional[list[dict]]:
    url = f"{SUPABASE_URL}/rest/v1/bots?select=*&order=updated_at.desc"
    try:
        r = client.get(url, headers=_headers(jwt, prefer_return=False), timeout=HTTP_TIMEOUT_S)
        if r.status_code == 401:
            log.warning("cloud pull 401 — JWT likely expired")
            return None
        if r.status_code != 200:
            log.warning("cloud pull %d: %s", r.status_code, r.text[:200])
            return None
        return r.json()
    except httpx.HTTPError as e:
        log.warning("cloud pull network error: %s", e)
        return None


def _push_insert(client: httpx.Client, jwt: str, supabase_uid: str, bot: models.Bot) -> Optional[dict]:
    url = f"{SUPABASE_URL}/rest/v1/bots"
    try:
        r = client.post(url, headers=_headers(jwt), json=_bot_payload(bot, supabase_uid), timeout=HTTP_TIMEOUT_S)
        if r.status_code in (200, 201):
            rows = r.json()
            return rows[0] if rows else None
        log.warning("cloud insert %d for bot id=%s: %s", r.status_code, bot.id, r.text[:200])
        return None
    except httpx.HTTPError as e:
        log.warning("cloud insert network error: %s", e)
        return None


def _push_update(client: httpx.Client, jwt: str, cloud_id: str, bot: models.Bot) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/bots?id=eq.{cloud_id}"
    payload = {f: getattr(bot, f, None) for f in SYNCED_FIELDS if getattr(bot, f, None) is not None}
    try:
        r = client.patch(url, headers=_headers(jwt, prefer_return=False), json=payload, timeout=HTTP_TIMEOUT_S)
        if r.status_code in (200, 204):
            return True
        log.warning("cloud update %d for cloud_id=%s: %s", r.status_code, cloud_id, r.text[:200])
        return False
    except httpx.HTTPError as e:
        log.warning("cloud update network error: %s", e)
        return False


# ── The sync loop ───────────────────────────────────────────────────────────

def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Supabase returns RFC3339 with microseconds; tolerate both.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _apply_cloud_to_local(local: models.Bot, cb: dict) -> bool:
    changed = False
    for col in SYNCED_FIELDS:
        v = cb.get(col)
        if v is None:
            continue
        if getattr(local, col) != v:
            setattr(local, col, v)
            changed = True
    return changed


def _build_local_from_cloud(user_id: int, cb: dict) -> models.Bot:
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


def _local_user_for_supabase(db: Session, supabase_uid: str, email: Optional[str]) -> models.User:
    """Find or auto-provision the local users row for this Supabase identity.
    Mirrors the logic in auth.py._provision_user_from_supabase but without
    requiring a Bearer token in a request — we run from background context."""
    user = db.query(models.User).filter(models.User.supabase_uid == supabase_uid).first()
    if user:
        return user
    if email:
        user = db.query(models.User).filter(models.User.email == email).first()
        if user:
            user.supabase_uid = supabase_uid
            db.commit()
            return user
    # Brand-new user — create. Adopt legacy id=1 bots into them.
    base = (email.split("@")[0] if email else f"user-{supabase_uid[:8]}")[:32] or f"user-{supabase_uid[:8]}"
    username, suffix = base, 1
    while db.query(models.User).filter(models.User.username == username).first():
        username = f"{base}-{supabase_uid[:6]}{'' if suffix == 1 else suffix}"
        suffix += 1
        if suffix > 5:
            username = f"user-{supabase_uid[:12]}"
            break
    user = models.User(
        username=username, email=email or f"{supabase_uid}@cloud-sync.local",
        hashed_password="", is_active=True, supabase_uid=supabase_uid,
    )
    db.add(user); db.commit(); db.refresh(user)
    # Adopt legacy default-user bots if this is a fresh provision and they own none.
    try:
        adopted = (db.query(models.Bot)
                   .filter(models.Bot.user_id == 1, models.Bot.cloud_id.is_(None))
                   .update({"user_id": user.id}, synchronize_session=False))
        if adopted:
            db.commit()
            log.info("adopted %d legacy bot(s) into user %s", adopted, supabase_uid[:8])
    except Exception as e:
        db.rollback()
        log.warning("adoption failed: %s", e)
    return user


def _run_one_cycle(client: httpx.Client) -> None:
    """One reconciliation cycle. Bounded work, never raises."""
    cycle_start = time.time()

    # 1. Source of truth for auth: session.json. If missing or expired, pause.
    sess = _read_session()
    if not sess:
        _set_status(state="paused", paused_reason="session.json missing — sign in to desktop app")
        return
    jwt = sess.get("access_token")
    supabase_uid = sess.get("user_id") or sess.get("user", {}).get("id")
    email = sess.get("email")
    if not jwt or not supabase_uid:
        _set_status(state="paused", paused_reason="session.json missing access_token / user_id")
        return
    if _is_jwt_expired(jwt):
        _set_status(state="paused", paused_reason="JWT expired — Electron should refresh", supabase_uid=supabase_uid)
        return
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        _set_status(state="paused", paused_reason="SUPABASE_URL / ANON_KEY not in env")
        return

    _set_status(state="syncing", supabase_uid=supabase_uid)

    db = SessionLocal()
    pulls = pushes = 0
    try:
        user = _local_user_for_supabase(db, supabase_uid, email)

        # ── PULL: cloud → local ─────────────────────────────────────────
        cloud_rows = _pull_cloud_bots(client, jwt)
        if cloud_rows is None:
            _set_status(state="error", last_error="cloud pull failed")
            return

        cloud_by_id = {cb["id"]: cb for cb in cloud_rows if cb.get("id")}

        for cid, cb in cloud_by_id.items():
            local = db.query(models.Bot).filter(models.Bot.cloud_id == cid).first()
            if not local:
                db.add(_build_local_from_cloud(user.id, cb))
                pulls += 1
            else:
                # Conflict resolution: cloud wins iff cloud.updated_at >
                # local.cloud_synced_at (when we last took cloud's value).
                cloud_dt = _parse_iso(cb.get("updated_at"))
                local_dt = local.cloud_synced_at
                if cloud_dt and (not local_dt or cloud_dt > local_dt):
                    if _apply_cloud_to_local(local, cb):
                        local.cloud_synced_at = cloud_dt
                        pulls += 1

        # ── PUSH: local → cloud ─────────────────────────────────────────
        # Two cases: never-synced (no cloud_id) AND locally-newer than cloud.
        for bot in db.query(models.Bot).filter(models.Bot.user_id == user.id).all():
            if not bot.cloud_id:
                row = _push_insert(client, jwt, supabase_uid, bot)
                if row and row.get("id"):
                    bot.cloud_id = row["id"]
                    cloud_dt = _parse_iso(row.get("updated_at"))
                    bot.cloud_synced_at = cloud_dt or datetime.now(timezone.utc)
                    pushes += 1
                continue
            # Already synced: detect local mutation by comparing the bot's
            # in-memory hash against the cloud's. Cheap heuristic — if the
            # cloud row we just pulled differs from the local payload we'd
            # send, push.
            cb = cloud_by_id.get(bot.cloud_id)
            if cb:
                local_payload = _bot_payload(bot, supabase_uid)
                cloud_payload = {k: cb.get(k) for k in local_payload if k != "user_id"}
                local_payload.pop("user_id", None)
                if local_payload != cloud_payload:
                    if _push_update(client, jwt, bot.cloud_id, bot):
                        bot.cloud_synced_at = datetime.now(timezone.utc)
                        pushes += 1

        # ── DELETE PROPAGATION: cloud row vanished → local row removed ──
        # Only delete local rows that DID have a cloud_id (i.e. were synced
        # before). Local-only rows (cloud_id NULL) stay — they'll be pushed
        # next cycle.
        synced_local = (db.query(models.Bot)
                        .filter(models.Bot.user_id == user.id,
                                models.Bot.cloud_id.isnot(None))
                        .all())
        for bot in synced_local:
            if bot.cloud_id not in cloud_by_id:
                db.delete(bot)
                pulls += 1   # count as pull (cloud-side change reflected here)

        db.commit()

        with _status_lock:
            _status.pulls_total  += pulls
            _status.pushes_total += pushes
            _status.cycles_total += 1
            _status.last_sync_at = datetime.now(timezone.utc).isoformat()
            _status.last_cycle_ms = (time.time() - cycle_start) * 1000
            _status.last_error = None
            _status.paused_reason = None
            _status.state = "idle"

        if pulls or pushes:
            log.info("sync cycle: +%d pulled, +%d pushed (%.0f ms)",
                     pulls, pushes, (time.time() - cycle_start) * 1000)

    except Exception as e:
        # Catch-all: any unexpected exception MUST NOT take down the thread.
        try:
            db.rollback()
        except Exception:
            pass
        _set_status(state="error", last_error=f"cycle exception: {e}")
        log.exception("sync cycle failed")
    finally:
        db.close()


# ── Thread management ───────────────────────────────────────────────────────

_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _loop():
    log.info("sync engine starting (interval=%.1fs)", SYNC_INTERVAL_S)
    # Use one httpx Client across cycles so connection pool + DNS is reused.
    with httpx.Client() as client:
        while not _stop_event.is_set():
            try:
                _run_one_cycle(client)
            except Exception as e:
                log.exception("loop top-level guard caught: %s", e)
            _stop_event.wait(SYNC_INTERVAL_S)
    log.info("sync engine stopped")


def start() -> None:
    """Start the daemon thread. Idempotent."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="sync-engine", daemon=True)
    _thread.start()


def stop() -> None:
    _stop_event.set()
