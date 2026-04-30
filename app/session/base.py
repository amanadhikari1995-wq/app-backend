"""
session/base.py — abstract base class every market detector implements.

Each detector owns the logic for ONE market type. The base class is
deliberately tiny: detectors only have to answer "what's the current
session?" — the manager does scheduling, change-detection, and broadcast.

Adding a new market = subclass MarketSessionDetector, implement two methods,
register it. See detectors/kalshi_15m.py for a worked example.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .types import Session


class MarketSessionDetector(ABC):
    """
    Implementations:
      market_type     — class attribute, the unique short id ("kalshi_15m")
      poll_interval   — how often the manager calls detect_current(), in seconds.
                        Lower = more responsive, higher = less load. Detectors
                        on calendar-driven markets can use 60s; high-frequency
                        rolling markets like Kalshi 15m should use 5–10s.

    Methods:
      detect_current() — return the active Session, or None if the market is
                         currently closed (between sessions). Must NOT raise
                         on transient failures — return None and let the
                         manager retry next tick.

      sessions_equal() — equality predicate. The manager calls this to decide
                         whether two consecutive detect_current() results
                         represent the SAME session or a NEW session. The
                         default compares (market_type, ticker), which is
                         right for almost all markets. Override only if your
                         market reuses tickers across sessions.
    """

    # Subclasses MUST set these.
    market_type:   str  = ""
    poll_interval: int  = 10        # seconds between detect_current() calls

    @abstractmethod
    def detect_current(self) -> Optional[Session]:
        """
        Return the currently-active session for this market, or None if
        none is open right now (the market is between sessions).

        Implementations should:
          • Be cheap — this runs every `poll_interval` seconds.
          • Be tolerant of network/API errors — return None on failure,
            don't raise. The manager interprets None as "no session right
            now" and will retry on the next tick.
          • Be deterministic for a given market state — calling this
            twice in a row with no underlying change should return Sessions
            that compare equal under sessions_equal().
        """
        raise NotImplementedError

    def sessions_equal(self, a: Session, b: Session) -> bool:
        """
        Default: same market + same ticker → same session.
        Override if your market reuses tickers across sessions, or if a
        ticker change isn't enough to identify a transition.
        """
        return a.market_type == b.market_type and a.ticker == b.ticker
