"""
error_reporter.py — write backend errors to Supabase app_errors table.

Same destination as the frontend's error-reporter.ts, so admin can see all
errors (frontend, backend, bot subprocess) in one place. Sync httpx (matches
cloud_db.py pattern). Fire-and-forget — never raises.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import time
import traceback
import uuid as _uuid
from typing import Optional

import httpx

log = logging.getLogger("watchdog.error_reporter")

SUPABASE_URL      = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

# One per backend boot — groups all errors from a single uvicorn lifetime.
_SESSION_ID = str(_uuid.uuid4())

# Per-fingerprint throttle so a tight error loop doesn't flood the table.
_THROTTLE_S    = 5.0
_last_sent_at: dict[str, float] = {}

# Sanitiser — strip anything that looks like a secret before writing.
_SECRET_PATTERNS = [
    re.compile(r"sk-[a-z0-9_-]{20,}", re.I),
    re.compile(r"sbp_[a-z0-9]{30,}", re.I),
    re.compile(r"eyJ[a-z0-9_-]{20,}\.[a-z0-9_-]{20,}\.[a-z0-9_-]{20,}", re.I),
    re.compile(r"(api[_-]?key|api[_-]?secret|password|secret)[\s\"':=]+[^\s\"',}]{16,}", re.I),
]


def _sanitize(s: str) -> str:
    if not s:
        return s
    out = s
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def _read_session_jwt() -> Optional[str]:
    """Same session.json source as cloud_db.py — uses the user's JWT so RLS
    attributes the error to the right user_id."""
    try:
        from pathlib import Path
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            p = Path(base) / "WatchDog" / "session.json"
        else:
            base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
            p = Path(base) / "WatchDog" / "session.json"
        if not p.exists():
            return None
        return (json.loads(p.read_text(encoding="utf-8")) or {}).get("access_token")
    except Exception:
        return None


def _fingerprint(error_type: str, message: str, stack: str) -> str:
    head = "\n".join(stack.split("\n")[:3]) if stack else ""
    raw = f"{error_type}|{message[:80]}|{head}"
    return hashlib.md5(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _app_version() -> str:
    return os.environ.get("WATCHDOG_VERSION", "unknown")


def _platform() -> str:
    try:
        import platform as _p
        return f"{_p.system()}-{_p.release()}-{_p.machine()}"
    except Exception:
        return "unknown"


def report_error(
    err: BaseException,
    *,
    source: str = "backend",
    bot_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> None:
    """Fire-and-forget error report. Never raises."""
    try:
        if not SUPABASE_URL or not SUPABASE_ANON_KEY:
            return

        error_type = type(err).__name__
        message    = _sanitize(str(err) or repr(err))[:1000]
        stack      = _sanitize(traceback.format_exc())[:8000] if traceback else ""
        fp         = _fingerprint(error_type, message, stack)

        now = time.time()
        if fp in _last_sent_at and now - _last_sent_at[fp] < _THROTTLE_S:
            return
        _last_sent_at[fp] = now

        jwt = _read_session_jwt()
        headers = {
            "apikey":        SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {jwt or SUPABASE_ANON_KEY}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

        ctx = dict(context or {})
        ctx.setdefault("hostname", socket.gethostname())

        payload = {
            "p_source":      source,
            "p_message":     message,
            "p_error_type":  error_type,
            "p_stack":       stack,
            "p_context":     ctx,
            "p_bot_id":      bot_id,
            "p_session_id":  _SESSION_ID,
            "p_fingerprint": fp,
            "p_app_version": _app_version(),
            "p_platform":    _platform(),
        }

        url = f"{SUPABASE_URL}/rest/v1/rpc/app_errors_record"
        with httpx.Client(timeout=5.0) as c:
            c.post(url, headers=headers, json=payload)
    except Exception as e:
        log.debug("error reporter itself errored (swallowed): %s", e)