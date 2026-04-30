"""
session/__init__.py — public entry point for the global session manager.

Typical usage from elsewhere in the app:

    from app.session import get_manager, SessionEvent, Session

    mgr = get_manager()
    mgr.start()                  # at app boot

    # somewhere else: subscribe to all session events
    unsub = mgr.subscribe(my_callback)

    # or to one specific market
    unsub_kalshi = mgr.subscribe(my_callback, market_type="kalshi_15m")

To add a new market: drop a file in app/session/detectors/your_market.py
that defines a MarketSessionDetector subclass decorated with
@register_detector. It'll be auto-discovered on next backend restart.
"""
from .base     import MarketSessionDetector
from .manager  import SessionManager, get_manager
from .registry import register_detector, all_market_types
from .types    import Session, SessionEvent, SessionState

__all__ = [
    "MarketSessionDetector",
    "Session",
    "SessionEvent",
    "SessionManager",
    "SessionState",
    "all_market_types",
    "get_manager",
    "register_detector",
]
