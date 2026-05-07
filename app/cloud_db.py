"""
cloud_db.py - synchronous Supabase REST client for runtime bot data.

Bot definitions and api_connections live in Supabase. The local FastAPI
backend reads them just-in-time when starting a bot. This is the ONLY
data-fetch surface for bot/api_connection in the backend.
"""
from __future__ import annotations

import json, logging, os
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("watchdog.cloud_db")

SUPABASE_URL      = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")


def _user_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "WatchDog"
    try:
        if os.uname().sysname == "Darwin":
            return Path.home() / "Library" / "Application Support" / "WatchDog"
    except AttributeError:
        pass
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "WatchDog"


def _read_session() -> Optional[dict]:
    try:
        p = _user_data_dir() / "session.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _auth():
    """Returns (jwt, user_id) or None."""
    sess = _read_session()
    if not sess:
        return None
    jwt = sess.get("access_token", "")
    uid = sess.get("user_id") or (sess.get("user") or {}).get("id") or ""
    if not jwt or not uid:
        return None
    return jwt, uid


def _headers(jwt: str):
    return {
        "apikey":        SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {jwt}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }


def get_bot(bot_id: str) -> Optional[dict]:
    """Fetch a bot row by Supabase UUID. Returns dict or None."""
    auth = _auth()
    if not auth or not SUPABASE_URL:
        return None
    jwt, _ = auth
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{SUPABASE_URL}/rest/v1/bots?id=eq.{bot_id}&select=*",
                      headers=_headers(jwt))
            if r.status_code != 200:
                log.warning("get_bot %s: %d %s", bot_id, r.status_code, r.text[:200])
                return None
            rows = r.json()
            return rows[0] if rows else None
    except Exception as e:
        log.warning("get_bot %s: %s", bot_id, e)
        return None


def list_bot_connections(bot_id: str) -> list[dict]:
    """All active api_connections for a given bot UUID."""
    auth = _auth()
    if not auth or not SUPABASE_URL:
        return []
    jwt, _ = auth
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(
                f"{SUPABASE_URL}/rest/v1/api_connections?bot_id=eq.{bot_id}&is_active=eq.true&select=*",
                headers=_headers(jwt))
            if r.status_code != 200:
                log.warning("list_conns %s: %d %s", bot_id, r.status_code, r.text[:200])
                return []
            return r.json() or []
    except Exception as e:
        log.warning("list_conns %s: %s", bot_id, e)
        return []


def update_bot_status(bot_id: str, *, status: Optional[str] = None,
                      is_running: Optional[bool] = None,
                      run_count: Optional[int] = None,
                      last_run_at: Optional[str] = None) -> bool:
    """Patch a bot's runtime status fields."""
    auth = _auth()
    if not auth or not SUPABASE_URL:
        return False
    jwt, _ = auth
    payload: dict = {}
    if status is not None:      payload["status"] = status
    if is_running is not None:  payload["is_running"] = is_running
    if run_count is not None:   payload["run_count"] = run_count
    if last_run_at is not None: payload["last_run_at"] = last_run_at
    if not payload:
        return True
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.patch(f"{SUPABASE_URL}/rest/v1/bots?id=eq.{bot_id}",
                        headers={**_headers(jwt), "Prefer": "return=minimal"},
                        json=payload)
            return r.status_code in (200, 204)
    except Exception as e:
        log.warning("update_bot_status %s: %s", bot_id, e)
        return False
