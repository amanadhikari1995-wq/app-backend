"""
wd_session.py — bot-side helper for the global session manager.

A bot OPTIONALLY imports this to:
  • get the current session for its market type
  • register a callback that fires when its session changes
  • read seconds-left without hand-rolling timestamp math

Bots that don't import this still work — they just won't get callbacks.
The detection ITSELF lives entirely in the backend; this helper only
exposes the manager's state to the bot process.

Usage:

    from wd_session import current, on_session_start, seconds_left

    def handle_new_session(session):
        print(f"[BOT] New {session['market_type']} session: {session['ticker']}")
        # reset trade counters, clear position state, etc.

    on_session_start("kalshi_15m", handle_new_session)

    # ... in your trading loop:
    sess = current("kalshi_15m")
    if sess and seconds_left(sess) < 60:
        # last minute of session
        ...

Implementation: bots run in a SEPARATE process from the backend, so they
can't directly call get_manager().subscribe(). Instead, this module polls
the backend's REST endpoint at /api/sessions/{market} and fires registered
callbacks on local state changes.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, Optional

import requests

_API = os.getenv("WATCHDOG_API_URL", "http://localhost:8000").rstrip("/")
_POLL_S = 5.0

SessionDict = Dict[str, Any]
_Callback   = Callable[[SessionDict], None]


# Per-market: latest known session and registered callbacks.
_state:    Dict[str, Optional[SessionDict]] = {}
_callbacks: Dict[str, list[_Callback]]      = {}
_threads:   Dict[str, threading.Thread]     = {}
_lock = threading.Lock()


def current(market_type: str) -> Optional[SessionDict]:
    """Return the current session dict for a market, or None if none open."""
    try:
        r = requests.get(f"{_API}/api/sessions/{market_type}", timeout=5)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def seconds_left(session: SessionDict) -> Optional[int]:
    """Pull the precomputed seconds_left field from a session dict."""
    return session.get("seconds_left") if session else None


def on_session_start(market_type: str, callback: _Callback) -> Callable[[], None]:
    """
    Register `callback` to fire when a new session starts for `market_type`.
    Spawns a background poller per market on first registration.

    Returns an unsubscribe function.
    """
    with _lock:
        _callbacks.setdefault(market_type, []).append(callback)
        if market_type not in _threads:
            t = threading.Thread(
                target=_poll_loop, args=(market_type,),
                daemon=True, name=f"wd-session-{market_type}",
            )
            _threads[market_type] = t
            t.start()

    def _unsub() -> None:
        with _lock:
            lst = _callbacks.get(market_type, [])
            if callback in lst:
                lst.remove(callback)
    return _unsub


def _poll_loop(market_type: str) -> None:
    """Watch for ticker changes via the REST endpoint; fire callbacks on change."""
    last_ticker: Optional[str] = None
    while True:
        try:
            sess = current(market_type)
            new_ticker = sess.get("ticker") if sess else None
            if new_ticker != last_ticker and sess is not None:
                last_ticker = new_ticker
                with _lock:
                    cbs = list(_callbacks.get(market_type, []))
                    _state[market_type] = sess
                for cb in cbs:
                    try:
                        cb(sess)
                    except Exception:
                        pass    # never let a callback crash the poller
            elif sess is None:
                last_ticker = None
        except Exception:
            pass
        time.sleep(_POLL_S)
