"""
Detector for Kalshi rolling 15-minute BTC binary markets (KXBTC15M).

A new market opens every 15 minutes around the clock. Detection strategy:
GET /markets?series_ticker=KXBTC15M&status=open and pick the first result.
The ticker changes each session, so the default sessions_equal() (compare
market_type + ticker) correctly identifies transitions.

This detector does NOT need credentials — Kalshi's `status=open` listing
is unauthenticated.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

from ..base     import MarketSessionDetector
from ..registry import register_detector
from ..types    import Session, SessionState

log = logging.getLogger("session.kalshi_15m")

_BASE = os.getenv(
    "KALSHI_API_URL",
    "https://api.elections.kalshi.com/trade-api/v2",
).rstrip("/")
_SERIES = "KXBTC15M"
_TIMEOUT_S = 6.0


@register_detector
class KalshiBinary15mDetector(MarketSessionDetector):
    """Polls Kalshi every 5s for the currently-open KXBTC15M market."""
    market_type   = "kalshi_15m"
    poll_interval = 5

    def detect_current(self) -> Optional[Session]:
        try:
            r = requests.get(
                f"{_BASE}/markets",
                params={"series_ticker": _SERIES, "status": "open", "limit": 5},
                timeout=_TIMEOUT_S,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            log.debug("Kalshi list-markets failed: %s", e)
            return None

        markets = payload.get("markets") or []
        if not markets:
            return None

        m = markets[0]
        ticker = m.get("ticker")
        if not ticker:
            return None

        # Kalshi timestamps are ISO-8601 with a 'Z' suffix.
        opened_at = _parse_iso(m.get("open_time")) or datetime.now(timezone.utc)
        closes_at = _parse_iso(m.get("close_time"))

        return Session(
            market_type=self.market_type,
            ticker=ticker,
            opened_at=opened_at,
            closes_at=closes_at,
            state=SessionState.OPEN,
            meta={
                "title":         m.get("title"),
                "subtitle":      m.get("subtitle"),
                "yes_bid":       m.get("yes_bid"),
                "no_bid":        m.get("no_bid"),
                "floor_strike":  m.get("floor_strike"),
            },
        )


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
