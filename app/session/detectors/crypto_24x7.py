"""
Detector for continuous (24/7) markets like spot crypto.

Continuous markets have no real "session" — they trade non-stop. We
represent this as ONE long-running session per UTC day so bots that want
session-based bookkeeping (daily P&L, daily trade limits) still get clean
day rollovers at 00:00 UTC.

If you don't want any rollover at all, change the ticker to a constant
("CRYPTO-CONTINUOUS") and set closes_at to None — sessions_equal() will
keep returning True forever and no transitions ever fire.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional

from ..base     import MarketSessionDetector
from ..registry import register_detector
from ..types    import Session, SessionState


@register_detector
class Crypto24x7Detector(MarketSessionDetector):
    """One session per UTC day — rolls over at 00:00 UTC."""
    market_type   = "crypto_24x7"
    poll_interval = 30

    def detect_current(self) -> Optional[Session]:
        now = datetime.now(timezone.utc)
        day_start = datetime.combine(now.date(), time(0, 0), tzinfo=timezone.utc)
        day_end   = day_start + timedelta(days=1)
        return Session(
            market_type=self.market_type,
            ticker=f"CRYPTO-{now.date().isoformat()}",
            opened_at=day_start,
            closes_at=day_end,
            state=SessionState.OPEN,
            meta={"continuous": True},
        )
