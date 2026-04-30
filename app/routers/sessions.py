"""
sessions.py — REST surface for the global session manager.

  GET /api/sessions/             → list of currently-active sessions
  GET /api/sessions/{market}     → one specific market's current session

Also bridges session events into the existing Live Logs pipeline so the
dashboard's log panel shows session transitions automatically — no bot
code required to surface "new session" / "session ended" lines.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from ..session import SessionEvent, get_manager

log = logging.getLogger("session.router")

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("/")
def list_sessions() -> List[Dict[str, Any]]:
    """All currently-active sessions across every registered market type."""
    return [s.to_dict() for s in get_manager().all_current().values()]


@router.get("/{market_type}")
def get_session(market_type: str) -> Dict[str, Any]:
    """Currently-active session for one market, or 404 if none open."""
    sess = get_manager().current(market_type)
    if sess is None:
        raise HTTPException(404, f"No open session for market_type={market_type!r}")
    return sess.to_dict()


# ── Live Logs integration ────────────────────────────────────────────────
# The session manager broadcasts events to subscribers on its polling
# threads. We register a wildcard subscriber here that turns each event
# into a global log line so the dashboard's "Live Activity" panel surfaces
# session transitions without any bot involvement.

def _format_event_line(ev: SessionEvent) -> str:
    """Build the human-readable log line for a session event."""
    s = ev.session
    when = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
    if ev.type == "session_started":
        closes = s.closes_at.isoformat() if s.closes_at else "(continuous)"
        return (f"{when} | INFO     | [SESSION] STARTED  market={s.market_type}  "
                f"ticker={s.ticker}  closes={closes}")
    if ev.type == "session_ended":
        return (f"{when} | INFO     | [SESSION] ENDED    market={s.market_type}  "
                f"ticker={s.ticker}")
    return f"{when} | INFO     | [SESSION] {ev.type}  market={s.market_type}  ticker={s.ticker}"


def _on_session_event(ev: SessionEvent) -> None:
    """
    Wildcard subscriber. Writes each transition to the bot_logs table for
    every currently-active bot, so the dashboard's per-bot log feeds AND
    the global Live Activity panel both surface session changes.

    If you don't want session lines mixed into bot logs, comment out the
    bot_logs insert below and instead emit to a dedicated table.
    """
    line = _format_event_line(ev)
    log.info(line)              # always log to backend stdout

    # Mirror into bot_logs so the Dashboard's existing log-pollers pick it up.
    try:
        from ..database import SessionLocal
        from .. import models
        db = SessionLocal()
        try:
            running_bots = (db.query(models.Bot)
                              .filter(models.Bot.status == models.BotStatus.RUNNING)
                              .all())
            for bot in running_bots:
                db.add(models.BotLog(
                    bot_id=bot.id,
                    user_id=bot.user_id,
                    level=models.LogLevel.INFO,
                    message=line,
                ))
            db.commit()
        finally:
            db.close()
    except Exception:
        # Never let a logging failure crash the session manager.
        log.exception("failed to mirror session event into bot_logs")


# Register on module import. main.py imports this router at startup, so the
# subscription is wired up exactly once for the lifetime of the backend.
_unsub = get_manager().subscribe(_on_session_event)
