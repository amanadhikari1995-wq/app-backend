"""
Detector for US equity regular-hours sessions (NYSE/NASDAQ 9:30am–4pm ET).

This is an EXAMPLE STUB — it implements the calendar logic for normal
weekdays only. To make it production-grade you'd add:
  • Holiday calendar (NYSE closes ~9 days/year)
  • Half-day handling (early close at 1pm ET on day before some holidays)
  • Pre-market / after-hours session detection

The pattern itself is what matters: clock-driven detectors don't need any
HTTP polling, just a time check.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo                  # Python 3.9+
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = timezone(timedelta(hours=-5))            # crude fallback (no DST)

from ..base     import MarketSessionDetector
from ..registry import register_detector
from ..types    import Session, SessionState

_OPEN_T  = time(9, 30)    # 09:30 ET
_CLOSE_T = time(16, 0)    # 16:00 ET


@register_detector
class StocksUsDetector(MarketSessionDetector):
    """Active 9:30 ET → 16:00 ET on weekdays (excluding holidays — TODO)."""
    market_type   = "stocks_us"
    poll_interval = 60                              # clock-driven; cheap

    def detect_current(self) -> Optional[Session]:
        now_et = datetime.now(tz=_ET)
        # Weekend? No session.
        if now_et.weekday() >= 5:
            return None
        # Within RTH window?
        if not (_OPEN_T <= now_et.time() < _CLOSE_T):
            return None

        opened_at = now_et.replace(hour=_OPEN_T.hour,  minute=_OPEN_T.minute,
                                    second=0, microsecond=0)
        closes_at = now_et.replace(hour=_CLOSE_T.hour, minute=_CLOSE_T.minute,
                                    second=0, microsecond=0)
        ticker = f"NYSE-{now_et.date().isoformat()}"

        return Session(
            market_type=self.market_type,
            ticker=ticker,
            opened_at=opened_at.astimezone(timezone.utc),
            closes_at=closes_at.astimezone(timezone.utc),
            state=SessionState.OPEN,
            meta={"timezone": "America/New_York"},
        )
