"""
cloud_db.py - synchronous Supabase REST client for runtime bot data.

Bot definitions and api_connections live in Supabase. The local FastAPI
backend reads them just-in-time when starting a bot. This is the ONLY
data-fetch surface for bot/api_connection in the backend.
"""
from __future__ import annotations

import base64, json, logging, os
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("watchdog.cloud_db")


def _user_id_from_jwt(jwt: str) -> str:
    """v1.1.4 fallback. Decode the JWT's `sub` claim WITHOUT signature
    verification — we already trust the token (we're about to send it back
    to Supabase as our own auth), so the signature isn't relevant for this
    decode. Used when session.json doesn't have `user_id` at the top level
    or nested under `user.id` (an older Electron build's setSession handler
    that wrote the token but not the resolved user_id).
    """
    try:
        parts = jwt.split('.')
        if len(parts) < 2:
            return ''
        # base64url payload with optional padding
        payload_b64 = parts[1]
        padding = (4 - len(payload_b64) % 4) % 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + ('=' * padding)))
        sub = payload.get('sub')
        if isinstance(sub, str) and sub:
            return sub
        return ''
    except Exception:
        return ''

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


_last_jwt_fallback_warn_at = 0.0


def _auth():
    """Returns (jwt, user_id) or None."""
    global _last_jwt_fallback_warn_at
    import time as _time
    sess = _read_session()
    if not sess:
        return None
    jwt = sess.get("access_token", "")
    uid = sess.get("user_id") or (sess.get("user") or {}).get("id") or ""
    if not uid and jwt:
        # v1.1.4: fall back to decoding the JWT's sub claim. session.json
        # in some Electron builds is written without an explicit user_id
        # field — without this fallback the cloud_log_shipper batched POSTs
        # all dropped silently because _auth() returned None.
        uid = _user_id_from_jwt(jwt)
        if uid:
            now = _time.monotonic()
            # Log once per minute so backend.log shows this path being used.
            if now - _last_jwt_fallback_warn_at > 60.0:
                _last_jwt_fallback_warn_at = now
                log.info("[cloud_db] _auth() — user_id absent from session.json; recovered from JWT sub claim (uid=%s)", uid)
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
