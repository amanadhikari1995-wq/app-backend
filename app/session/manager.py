"""
session/manager.py — the global SessionManager singleton.

This is the orchestrator. It does NOT know how to detect any market itself.
It owns:
  • a background thread per registered market type
  • a registry of subscribers (callables that get notified on session change)
  • the "current session per market" state
  • clean transition (close old → open new) logic

Bots interact with it through `subscribe()` callbacks; the dashboard reads
`current(market_type)` and consumes events via the routers.

Lifecycle:
  • app startup  → SessionManager.start() — discovers detectors and spawns
                   one polling thread per market type.
  • app shutdown → SessionManager.stop()  — signals threads to exit, joins.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set

from .base import MarketSessionDetector
from .registry import all_market_types, discover, get_detector_class
from .types import Session, SessionEvent, SessionState

log = logging.getLogger("session.manager")


# Subscriber callback signature: receives a SessionEvent.
SubscriberFn = Callable[[SessionEvent], None]


class SessionManager:
    """
    Threaded session orchestrator.

    Public API:
      start()                          — begin background polling
      stop()                           — stop all threads cleanly
      current(market_type)             — get current Session or None
      all_current()                    — dict of market_type -> Session
      subscribe(callback, market_type=None)
                                       — register a callback for session events.
                                         If market_type is None, callback
                                         receives events from all markets.
                                         Returns an unsubscribe function.

    Threading model:
      • One thread per registered market type, polling at the detector's
        poll_interval. Threads are daemon so they don't block app shutdown.
      • Subscriber callbacks are invoked on the polling thread that emitted
        the event. Subscribers must be cheap and non-blocking. (Long work
        should be queued elsewhere.)
      • A single threading.Lock protects the subscriber list and the
        _current_per_market dict; no other shared state.
    """

    def __init__(self) -> None:
        self._lock          = threading.Lock()
        self._stop_event    = threading.Event()
        self._threads:        Dict[str, threading.Thread] = {}
        self._detectors:      Dict[str, MarketSessionDetector] = {}
        self._current_per_market: Dict[str, Session] = {}
        # Subscribers keyed by market_type ("" = wildcard, all markets).
        self._subscribers:    Dict[str, List[SubscriberFn]] = {"": []}
        self._started         = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Discover detectors, instantiate them, and start one thread per market."""
        if self._started:
            log.warning("SessionManager.start() called twice — ignoring")
            return
        self._started = True

        discover()                                       # import detector modules
        market_types = list(all_market_types())
        if not market_types:
            log.info("SessionManager: no detectors registered yet — manager idle")
            return

        log.info("SessionManager starting %d detector thread(s): %s",
                 len(market_types), market_types)
        for mt in market_types:
            try:
                detector_cls = get_detector_class(mt)
                detector     = detector_cls()
            except Exception as e:
                log.error("Failed to instantiate detector %s: %s", mt, e)
                continue
            self._detectors[mt] = detector
            t = threading.Thread(
                target=self._poll_loop, args=(mt, detector),
                name=f"session-{mt}", daemon=True,
            )
            self._threads[mt] = t
            t.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal all polling threads to exit and join them."""
        self._stop_event.set()
        for name, t in self._threads.items():
            t.join(timeout=timeout)
            if t.is_alive():
                log.warning("session thread %s did not exit cleanly within %.1fs",
                            name, timeout)
        self._threads.clear()
        self._detectors.clear()
        self._started = False

    # ── State queries ────────────────────────────────────────────────────

    def current(self, market_type: str) -> Optional[Session]:
        """Return the active Session for this market type, or None if no detector
        is registered or no session is currently open."""
        with self._lock:
            return self._current_per_market.get(market_type)

    def all_current(self) -> Dict[str, Session]:
        """Snapshot of the active session per market."""
        with self._lock:
            return dict(self._current_per_market)

    def known_market_types(self) -> Set[str]:
        return set(self._detectors.keys())

    # ── Subscription ─────────────────────────────────────────────────────

    def subscribe(
        self,
        callback:    SubscriberFn,
        market_type: Optional[str] = None,
    ) -> Callable[[], None]:
        """
        Register a callback for SessionEvents. Returns an `unsubscribe` thunk.

        market_type=None  → receive events for ALL markets (wildcard)
        market_type="..." → receive only events for that market

        The callback is invoked synchronously on the polling thread —
        keep it short. For long work, hand off to a queue.
        """
        key = market_type or ""
        with self._lock:
            self._subscribers.setdefault(key, []).append(callback)

        def _unsub() -> None:
            with self._lock:
                lst = self._subscribers.get(key, [])
                if callback in lst:
                    lst.remove(callback)
        return _unsub

    # ── Internal: per-market polling loop ────────────────────────────────

    def _poll_loop(self, market_type: str, detector: MarketSessionDetector) -> None:
        """
        Body of each market thread. Calls detector.detect_current() at the
        configured cadence and emits events on transitions.
        """
        log.info("[%s] poll loop started (interval=%ds)",
                 market_type, detector.poll_interval)
        while not self._stop_event.is_set():
            try:
                self._tick(market_type, detector)
            except Exception:
                log.exception("[%s] unhandled error in tick — continuing", market_type)
            # Sleep but wake up early on shutdown.
            self._stop_event.wait(timeout=max(1, detector.poll_interval))
        log.info("[%s] poll loop exiting", market_type)

    def _tick(self, market_type: str, detector: MarketSessionDetector) -> None:
        """Call the detector once and react to any change."""
        try:
            new_session = detector.detect_current()
        except Exception as e:
            log.warning("[%s] detector raised: %s", market_type, e)
            return

        with self._lock:
            old_session = self._current_per_market.get(market_type)

        # ── Case A: no session before, none now → idle, no-op
        if old_session is None and new_session is None:
            return

        # ── Case B: no session before, one now → emit session_started
        if old_session is None and new_session is not None:
            self._on_transition(market_type, old=None, new=new_session)
            return

        # ── Case C: had a session, none now → emit session_ended (graceful close)
        if old_session is not None and new_session is None:
            closed = Session(
                market_type=old_session.market_type,
                ticker=old_session.ticker,
                opened_at=old_session.opened_at,
                closes_at=old_session.closes_at,
                state=SessionState.CLOSED,
                meta=old_session.meta,
            )
            self._on_transition(market_type, old=old_session, new=None, closed_marker=closed)
            return

        # ── Case D: had a session AND have one now — same or different?
        assert old_session is not None and new_session is not None
        if detector.sessions_equal(old_session, new_session):
            # Same session — refresh the snapshot (close time may tick down,
            # meta may update with new prices, etc) but don't broadcast.
            with self._lock:
                self._current_per_market[market_type] = new_session
            return

        # Different session — clean rollover: close old, then start new.
        closed = Session(
            market_type=old_session.market_type,
            ticker=old_session.ticker,
            opened_at=old_session.opened_at,
            closes_at=old_session.closes_at,
            state=SessionState.CLOSED,
            meta=old_session.meta,
        )
        self._on_transition(market_type, old=old_session, new=None, closed_marker=closed)
        self._on_transition(market_type, old=None,        new=new_session)

    def _on_transition(
        self,
        market_type:   str,
        old:           Optional[Session],
        new:           Optional[Session],
        closed_marker: Optional[Session] = None,
    ) -> None:
        """
        Apply a transition and broadcast.

          old=None,  new=Session    → session_started
          old=Sess,  new=None       → session_ended (uses closed_marker as payload)
        """
        if new is not None:
            # session_started
            with self._lock:
                self._current_per_market[market_type] = new
            event = SessionEvent(type="session_started", session=new, previous=old)
            log.info("[%s] session STARTED  ticker=%s  closes=%s",
                     market_type, new.ticker,
                     new.closes_at.isoformat() if new.closes_at else "(continuous)")
        else:
            # session_ended
            assert closed_marker is not None
            with self._lock:
                self._current_per_market.pop(market_type, None)
            event = SessionEvent(type="session_ended", session=closed_marker, previous=None)
            log.info("[%s] session ENDED    ticker=%s",
                     market_type, closed_marker.ticker)

        self._broadcast(event)

    def _broadcast(self, event: SessionEvent) -> None:
        """Fire all relevant subscriber callbacks. Exceptions are swallowed
        so one bad subscriber can't poison the others."""
        with self._lock:
            wildcard = list(self._subscribers.get("", []))
            specific = list(self._subscribers.get(event.session.market_type, []))
        for cb in wildcard + specific:
            try:
                cb(event)
            except Exception:
                log.exception("subscriber callback failed for event %s", event.type)


# ── Module-level singleton ────────────────────────────────────────────────

_singleton: Optional[SessionManager] = None


def get_manager() -> SessionManager:
    """Module-level accessor — there is exactly one SessionManager per process."""
    global _singleton
    if _singleton is None:
        _singleton = SessionManager()
    return _singleton
