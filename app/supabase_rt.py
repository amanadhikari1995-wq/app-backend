"""
supabase_rt.py — Real-time write-through for WATCH-DOG.

Every mutation in the local FastAPI backend (create/update/delete bot or
api_connection, bot run/stop) is immediately pushed to Supabase so:

  - The web dashboard picks up the change via Supabase Realtime
    (INSERT/UPDATE/DELETE events on `bots` and `api_connections` tables).

  - Any second desktop install sees the change on its next startup_pull()
    without waiting for a polling cycle.

Design goals
------------
  - Fully async (httpx.AsyncClient) -- never blocks FastAPI request threads.
  - Called from FastAPI BackgroundTasks -- fire-and-forget after HTTP response.
  - All failures swallowed + logged -- local SQLite is the runtime source.
  - startup_pull() runs once at backend startup to hydrate local SQLite
    from Supabase (makes fresh installs work immediately on login).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger("watchdog.supabase_rt")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUPABASE_URL      = os.getenv("SUPABASE_URL",      "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
HTTP_TIMEOUT_S    = 10.0

# Fields synced for bots (config only -- status pushed separately)
BOT_SYNC_FIELDS = (
    "name", "description", "code", "bot_type",
    "schedule_type", "schedule_start", "schedule_end",
    "max_amount_per_trade", "max_contracts_per_trade", "max_daily_loss",
    "auto_restart",
)

# Fields synced for api_connections
CONN_SYNC_FIELDS = ("name", "base_url", "api_key", "api_secret", "is_active")


# ---------------------------------------------------------------------------
# Session helpers (mirrors sync_engine / relay.py)
# ---------------------------------------------------------------------------

def _user_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "WatchDog"
    try:
        sysname = os.uname().sysname  # type: ignore[attr-defined]
    except AttributeError:
        sysname = ""
    if sysname == "Darwin":
        return Path.home() / "Library" / "Application Support" / "WatchDog"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "WatchDog"


def _read_session() -> Optional[dict]:
    try:
        p = _user_data_dir() / "session.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.debug("session.json read error: %s", exc)
        return None


def _token_expired(token: str, margin_s: int = 60) -> bool:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return True
        pad     = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad))
        exp     = payload.get("exp")
        return bool(exp) and int(exp) < time.time() + margin_s
    except Exception:
        return True


def _get_auth() -> Optional[tuple]:
    """Return (jwt, supabase_uid) or None if no live session."""
    sess = _read_session()
    if not sess:
        return None
    jwt = sess.get("access_token", "")
    uid = sess.get("user_id") or (sess.get("user") or {}).get("id") or ""
    if not jwt or not uid or _token_expired(jwt):
        return None
    return jwt, uid


def _headers(jwt: str) -> dict:
    return {
        "apikey":        SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {jwt}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
        "Prefer":        "return=representation",
    }


# ---------------------------------------------------------------------------
# Bot write-through
# ---------------------------------------------------------------------------

async def push_bot(bot_id: int) -> None:
    """
    Upsert one bot to Supabase. If the bot has no cloud_id, inserts and
    stamps the returned UUID back into local SQLite. If it has a cloud_id,
    patches the existing Supabase row.

    Called as a FastAPI BackgroundTask after every create/update.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return
    auth = _get_auth()
    if not auth:
        log.debug("push_bot id=%s: no live session", bot_id)
        return
    jwt, uid = auth

    from .database import SessionLocal
    from . import models

    db = SessionLocal()
    try:
        bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
        if not bot:
            return

        payload: dict[str, Any] = {"user_id": uid}
        for f in BOT_SYNC_FIELDS:
            payload[f] = getattr(bot, f, None)

        # Include live status so web dashboard sees running state immediately
        status_val = bot.status.value if hasattr(bot.status, "value") else str(bot.status or "IDLE")
        payload["status"]     = status_val
        payload["is_running"] = status_val == "RUNNING"
        payload["run_count"]  = bot.run_count or 0
        if bot.last_run_at:
            payload["last_run_at"] = bot.last_run_at.isoformat()

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            if bot.cloud_id:
                url = f"{SUPABASE_URL}/rest/v1/bots?id=eq.{bot.cloud_id}"
                r   = await client.patch(url, headers=_headers(jwt), json=payload)
                if r.is_success:
                    bot.cloud_synced_at = datetime.now(timezone.utc)
                    db.commit()
                    log.debug("push_bot: patched cloud_id=%s", bot.cloud_id)
                else:
                    log.warning("push_bot patch %d: %s", r.status_code, r.text[:200])
            else:
                url = f"{SUPABASE_URL}/rest/v1/bots"
                r   = await client.post(url, headers=_headers(jwt), json=payload)
                if r.is_success:
                    rows     = r.json()
                    cloud_id = rows[0].get("id") if rows else None
                    if cloud_id:
                        bot.cloud_id        = cloud_id
                        bot.cloud_synced_at = datetime.now(timezone.utc)
                        db.commit()
                        log.info("push_bot: inserted id=%s -> cloud_id=%s", bot_id, cloud_id)
                    else:
                        log.warning("push_bot insert returned no id: %s", r.text[:200])
                else:
                    log.warning("push_bot insert %d: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("push_bot id=%s error: %s", bot_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


async def push_bot_status(bot_id: int) -> None:
    """
    Push only status fields (status, is_running, run_count, last_run_at).
    Called on every run/stop so web dashboard sees status change in real-time.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return
    auth = _get_auth()
    if not auth:
        return
    jwt, _ = auth

    from .database import SessionLocal
    from . import models

    db = SessionLocal()
    try:
        bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
        if not bot or not bot.cloud_id:
            log.debug("push_bot_status: bot id=%s has no cloud_id -- skipping", bot_id)
            return

        status_val = (bot.status.value if hasattr(bot.status, "value")
                      else str(bot.status or "IDLE"))
        payload: dict[str, Any] = {
            "status":     status_val,
            "is_running": status_val == "RUNNING",
            "run_count":  bot.run_count or 0,
        }
        if bot.last_run_at:
            payload["last_run_at"] = bot.last_run_at.isoformat()

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            url = f"{SUPABASE_URL}/rest/v1/bots?id=eq.{bot.cloud_id}"
            r   = await client.patch(url, headers=_headers(jwt), json=payload)
            if r.is_success:
                log.debug("push_bot_status: id=%s -> %s", bot_id, status_val)
            else:
                log.warning("push_bot_status %d: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("push_bot_status id=%s: %s", bot_id, exc)
    finally:
        db.close()


async def delete_bot_cloud(cloud_id: str) -> None:
    """Delete a bot from Supabase by its cloud_id (Supabase UUID)."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY or not cloud_id:
        return
    auth = _get_auth()
    if not auth:
        return
    jwt, _ = auth

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            url = f"{SUPABASE_URL}/rest/v1/bots?id=eq.{cloud_id}"
            r   = await client.delete(url, headers=_headers(jwt))
            if r.is_success:
                log.info("delete_bot_cloud: removed cloud_id=%s", cloud_id)
            else:
                log.warning("delete_bot_cloud %d: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("delete_bot_cloud cloud_id=%s: %s", cloud_id, exc)


# ---------------------------------------------------------------------------
# API Connection write-through
# ---------------------------------------------------------------------------

async def push_conn(conn_id: int) -> None:
    """
    Upsert one api_connection to Supabase. Maps local bot_id to bot.cloud_id
    for the Supabase foreign key. Stamps conn.cloud_id on insert.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return
    auth = _get_auth()
    if not auth:
        return
    jwt, uid = auth

    from .database import SessionLocal
    from . import models

    db = SessionLocal()
    try:
        conn = (db.query(models.ApiConnection)
                .filter(models.ApiConnection.id == conn_id,
                        models.ApiConnection.is_active == True)
                .first())
        if not conn:
            # Soft-deleted or missing -- try to find for delete propagation
            conn_any = db.query(models.ApiConnection).filter(
                models.ApiConnection.id == conn_id).first()
            if conn_any and conn_any.cloud_id:
                await delete_conn_cloud(conn_any.cloud_id)
            return

        # Map local bot_id (int) -> Supabase bot UUID
        bot_cloud_id: Optional[str] = None
        if conn.bot_id:
            bot = db.query(models.Bot).filter(models.Bot.id == conn.bot_id).first()
            bot_cloud_id = bot.cloud_id if bot else None

        payload: dict[str, Any] = {"user_id": uid}
        if bot_cloud_id:
            payload["bot_id"] = bot_cloud_id
        for f in CONN_SYNC_FIELDS:
            payload[f] = getattr(conn, f, None)

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            if conn.cloud_id:
                url = f"{SUPABASE_URL}/rest/v1/api_connections?id=eq.{conn.cloud_id}"
                r   = await client.patch(url, headers=_headers(jwt), json=payload)
                if r.is_success:
                    log.debug("push_conn: patched cloud_id=%s", conn.cloud_id)
                else:
                    log.warning("push_conn patch %d: %s", r.status_code, r.text[:200])
            else:
                url = f"{SUPABASE_URL}/rest/v1/api_connections"
                r   = await client.post(url, headers=_headers(jwt), json=payload)
                if r.is_success:
                    rows     = r.json()
                    cloud_id = rows[0].get("id") if rows else None
                    if cloud_id:
                        conn.cloud_id = cloud_id
                        db.commit()
                        log.info("push_conn: inserted id=%s -> cloud_id=%s", conn_id, cloud_id)
                else:
                    log.warning("push_conn insert %d: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("push_conn id=%s: %s", conn_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


async def delete_conn_cloud(cloud_id: str) -> None:
    """Delete an api_connection from Supabase by cloud_id."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY or not cloud_id:
        return
    auth = _get_auth()
    if not auth:
        return
    jwt, _ = auth

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            url = f"{SUPABASE_URL}/rest/v1/api_connections?id=eq.{cloud_id}"
            r   = await client.delete(url, headers=_headers(jwt))
            if r.is_success:
                log.info("delete_conn_cloud: removed cloud_id=%s", cloud_id)
            else:
                log.warning("delete_conn_cloud %d: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("delete_conn_cloud cloud_id=%s: %s", cloud_id, exc)


# ---------------------------------------------------------------------------
# Startup pull: Supabase -> local SQLite
# ---------------------------------------------------------------------------

def startup_pull() -> None:
    """
    Pull all bots and api_connections from Supabase into local SQLite.
    Run once at backend startup so a fresh install (or new device login)
    immediately has the user's full data set without waiting for any cycle.

    Runs synchronously -- called from a short-lived thread in main.py
    lifespan so it does not block the FastAPI event loop startup.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        log.info("startup_pull: SUPABASE_URL/KEY not set -- skipping")
        return

    auth = _get_auth()
    if not auth:
        log.info("startup_pull: no session.json -- skipping (user not signed in yet)")
        return
    jwt, uid = auth

    log.info("startup_pull: pulling data for uid=%s...", uid[:8])

    from .database import SessionLocal
    from . import models

    db = SessionLocal()
    try:
        h = {
            "apikey":        SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {jwt}",
            "Accept":        "application/json",
        }

        with httpx.Client(timeout=15.0) as client:
            # ----------------------------------------------------------------
            # Pull bots
            # ----------------------------------------------------------------
            r = client.get(
                f"{SUPABASE_URL}/rest/v1/bots?select=*&order=created_at.asc",
                headers=h,
            )
            if r.is_success:
                local_user = db.query(models.User).filter(models.User.id == 1).first()
                if local_user:
                    pulled = 0
                    for cb in r.json():
                        cid = cb.get("id")
                        if not cid:
                            continue
                        local = db.query(models.Bot).filter(
                            models.Bot.cloud_id == cid).first()
                        if local:
                            for f in BOT_SYNC_FIELDS:
                                v = cb.get(f)
                                if v is not None:
                                    setattr(local, f, v)
                            local.cloud_synced_at = datetime.now(timezone.utc)
                            pulled += 1
                        else:
                            new_bot = models.Bot(
                                user_id         = local_user.id,
                                cloud_id        = cid,
                                cloud_synced_at = datetime.now(timezone.utc),
                                name            = cb.get("name") or "Untitled",
                                description     = cb.get("description"),
                                code            = cb.get("code") or "",
                                bot_type        = cb.get("bot_type"),
                                schedule_type   = cb.get("schedule_type") or "always",
                                schedule_start  = cb.get("schedule_start"),
                                schedule_end    = cb.get("schedule_end"),
                                max_amount_per_trade    = cb.get("max_amount_per_trade"),
                                max_contracts_per_trade = cb.get("max_contracts_per_trade"),
                                max_daily_loss          = cb.get("max_daily_loss"),
                                auto_restart    = bool(cb.get("auto_restart") or False),
                            )
                            db.add(new_bot)
                            pulled += 1
                    db.commit()
                    log.info("startup_pull: synced %d bots", pulled)
            else:
                log.warning("startup_pull bots: %d %s", r.status_code, r.text[:200])

            # ----------------------------------------------------------------
            # Pull api_connections
            # ----------------------------------------------------------------
            r2 = client.get(
                f"{SUPABASE_URL}/rest/v1/api_connections?select=*&order=created_at.asc",
                headers=h,
            )
            if r2.is_success:
                local_user = db.query(models.User).filter(models.User.id == 1).first()
                if local_user:
                    pulled_conns = 0
                    for cc in r2.json():
                        cid = cc.get("id")
                        if not cid:
                            continue
                        # Map cloud bot_id (UUID) -> local bot id (int)
                        local_bot_id: Optional[int] = None
                        cloud_bot_uuid = cc.get("bot_id")
                        if cloud_bot_uuid:
                            local_bot = db.query(models.Bot).filter(
                                models.Bot.cloud_id == cloud_bot_uuid).first()
                            if local_bot:
                                local_bot_id = local_bot.id

                        local_conn = db.query(models.ApiConnection).filter(
                            models.ApiConnection.cloud_id == cid).first()
                        if local_conn:
                            for f in CONN_SYNC_FIELDS:
                                v = cc.get(f)
                                if v is not None:
                                    setattr(local_conn, f, v)
                            if local_bot_id:
                                local_conn.bot_id = local_bot_id
                            pulled_conns += 1
                        elif cc.get("is_active", True):
                            new_conn = models.ApiConnection(
                                user_id    = local_user.id,
                                bot_id     = local_bot_id,
                                cloud_id   = cid,
                                name       = cc.get("name") or "Unnamed",
                                base_url   = cc.get("base_url"),
                                api_key    = cc.get("api_key"),
                                api_secret = cc.get("api_secret"),
                                is_active  = cc.get("is_active", True),
                            )
                            db.add(new_conn)
                            pulled_conns += 1
                    db.commit()
                    log.info("startup_pull: synced %d api_connections", pulled_conns)
            else:
                log.warning("startup_pull api_connections: %d %s",
                            r2.status_code, r2.text[:200])

    except Exception as exc:
        log.warning("startup_pull failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
