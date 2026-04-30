"""
session/types.py — value types shared across the session manager.

All public types live here so detectors, the manager, and the SDK can
import from one place without circular dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


class SessionState(str, Enum):
    """Lifecycle states for a session."""
    PENDING = "pending"     # detector found a session that hasn't started yet
    OPEN    = "open"        # session is currently active
    CLOSED  = "closed"      # session has ended
    ERROR   = "error"       # detector failed to determine state


@dataclass(frozen=True)
class Session:
    """
    Immutable snapshot of one session.

    The manager creates a new Session object every time the active session
    changes. Bots receive a copy via their on_session_start callback.

    Fields:
      market_type   — short identifier ("kalshi_15m", "stocks_us", ...)
                      matching the detector's `market_type` attribute.
      ticker        — opaque market identifier specific to that market.
                      For Kalshi this is "KXBTCD-26APR2820"; for stocks
                      it could be "NYSE-2026-04-28" or just "NYSE".
      opened_at     — UTC timestamp when the session opened.
      closes_at     — UTC timestamp when the session is expected to close,
                      or None for continuous markets (e.g. crypto 24/7).
      state         — current SessionState.
      meta          — detector-specific extra data (strike price, market
                      title, etc). Free-form so detectors aren't constrained.
    """
    market_type: str
    ticker:      str
    opened_at:   datetime
    closes_at:   Optional[datetime]
    state:       SessionState = SessionState.OPEN
    meta:        Dict[str, Any] = field(default_factory=dict)

    @property
    def seconds_left(self) -> Optional[int]:
        """Seconds until close, or None if continuous."""
        if self.closes_at is None:
            return None
        delta = (self.closes_at - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(delta))

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe representation for REST and log payloads."""
        return {
            "market_type":  self.market_type,
            "ticker":       self.ticker,
            "opened_at":    self.opened_at.isoformat(),
            "closes_at":    self.closes_at.isoformat() if self.closes_at else None,
            "state":        self.state.value,
            "seconds_left": self.seconds_left,
            "meta":         self.meta,
        }


# ── Event payloads passed to subscribers ──────────────────────────────────

@dataclass(frozen=True)
class SessionEvent:
    """
    Emitted by SessionManager when a session transition happens.
    Type is one of:
      "session_started"  — new session opened
      "session_ended"    — previous session closed cleanly
      "session_error"    — detector threw or returned invalid data
    """
    type:     str
    session:  Session
    previous: Optional[Session] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type":     self.type,
            "session":  self.session.to_dict(),
            "previous": self.previous.to_dict() if self.previous else None,
            "ts":       datetime.now(timezone.utc).isoformat(),
        }
