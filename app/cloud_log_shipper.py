"""
cloud_log_shipper.py — Asynchronous Supabase log shipper.

Each bot stdout line is written to local SQLite by the bots router. This
module ALSO writes that line to Supabase `bot_logs_tail` so the renderer
can subscribe via realtime instead of polling `127.0.0.1:8000/api/bots/
{id}/logs`.

v1.1.2 changes: ALL silent failure paths now emit structured log lines.
If the table stays empty post-deploy, the next user-supplied backend.log
will pinpoint exactly which return path fired. The single-line module-load
banner also proves the v1.1.2 build is the one actually running (useful
when Windows file-locks prevent NSIS from replacing the backend exe).

Single public entry: `ship_log(...)`. Calls are non-blocking.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from typing import Optional

import httpx

from app.cloud_db import SUPABASE_URL, SUPABASE_ANON_KEY, _auth, _headers

log = logging.getLogger("watchdog.cloud_log_shipper")

# Module-load banner (v1.1.2). Fires once per backend boot. Absence in
# backend.log proves the watchdog-backend.exe wasn't replaced on install.
log.info("[cloud_log_shipper] module loaded — v1.1.2 instrumented build (SUPABASE_URL=%s, ANON_KEY=%s)",
         ("set" if SUPABASE_URL else "EMPTY"),
         ("set" if SUPABASE_ANON_KEY else "EMPTY"))

# ── Tunables ────────────────────────────────────────────────────────────────
_MAX_QUEUE      = 5_000
_BATCH_SIZE     = 100
_FLUSH_INTERVAL = 0.25
_HTTP_TIMEOUT   = 5.0
_ENDPOINT       = "/rest/v1/bot_logs_tail"

# Diagnostic rate-limit: emit one warning per minute for repeating silent
# failures (auth missing, env vars missing). Without this, the log spams.
_RATELIMIT_SECS = 60.0

# ── State ───────────────────────────────────────────────────────────────────
_q: "queue.Queue[dict]" = queue.Queue(maxsize=_MAX_QUEUE)
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_thread_lock = threading.Lock()

# v1.1.2 diagnostic state
_first_ship_log_called = False
_first_ship_lock = threading.Lock()
_last_warn_envmissing_at = 0.0
_last_warn_authnone_at = 0.0
_total_rows_shipped = 0
_batch_counter = 0


def ship_log(bot_id: str, user_id: str, level: str, message: str,
             local_log_id: Optional[int] = None) -> None:
    """Enqueue a log line for async shipping to Supabase. NEVER blocks."""
    global _first_ship_log_called, _last_warn_envmissing_at

    # v1.1.2 first-call telemetry — confirms the bots.py import + call chain.
    if not _first_ship_log_called:
        with _first_ship_lock:
            if not _first_ship_log_called:
                _first_ship_log_called = True
                log.info("[cloud_log_shipper] first ship_log() call — bot_id=%s user_id=%s level=%s msg_preview=%r",
                         bot_id, user_id, level, (message or "")[:80])

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        # Rate-limited warning — if env vars never arrive, this fires once a minute.
        now = time.monotonic()
        if now - _last_warn_envmissing_at >= _RATELIMIT_SECS:
            _last_warn_envmissing_at = now
            log.warning("[cloud_log_shipper] disabled — SUPABASE_URL=%r or SUPABASE_ANON_KEY=%r missing (will continue to drop until env vars arrive)",
                        bool(SUPABASE_URL), bool(SUPABASE_ANON_KEY))
        return

    _ensure_thread()
    row = {
        "bot_id":       str(bot_id),
        "user_id":      str(user_id),
        "level":        str(level).lower(),
        "message":      message,
        "local_log_id": local_log_id,
    }
    try:
        _q.put_nowait(row)
    except queue.Full:
        try:
            _q.get_nowait()
            _q.put_nowait(row)
        except (queue.Empty, queue.Full):
            pass


def _ensure_thread() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(target=_drain, name="cloud-log-shipper", daemon=True)
        _thread.start()
        log.info("[cloud_log_shipper] drain thread started")


def _drain() -> None:
    """Background loop: build a batch (up to _BATCH_SIZE or _FLUSH_INTERVAL),
    POST it, repeat. Exits when _stop_event is set AND the queue is empty."""
    while True:
        if _stop_event.is_set() and _q.empty():
            log.info("[cloud_log_shipper] drain thread exiting (stop_event + empty queue)")
            return
        batch: list[dict] = []
        deadline = time.monotonic() + _FLUSH_INTERVAL
        while len(batch) < _BATCH_SIZE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                row = _q.get(timeout=remaining)
                batch.append(row)
            except queue.Empty:
                break
        if batch:
            _post_batch(batch)


def _post_batch(batch: list[dict]) -> None:
    global _last_warn_authnone_at, _total_rows_shipped, _batch_counter

    auth = _auth()
    if not auth:
        # Rate-limited diagnostic — if session.json never arrives or has no
        # user_id, this fires once a minute. Critical for diagnosing why the
        # table is empty: it tells us auth is the blocker, not the network.
        now = time.monotonic()
        if now - _last_warn_authnone_at >= _RATELIMIT_SECS:
            _last_warn_authnone_at = now
            log.warning("[cloud_log_shipper] _auth() returned None — session.json missing or has no user_id; dropping batch of %d rows (will continue dropping until session present)",
                        len(batch))
        return

    # v1.1.1 fix: unpack the tuple, don't pass it whole to _headers.
    jwt_str, _uid = auth

    try:
        url = f"{SUPABASE_URL}{_ENDPOINT}"
        hdrs = _headers(jwt_str)
        hdrs["Prefer"] = "resolution=ignore-duplicates,return=minimal"
        with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
            r = c.post(url, headers=hdrs, content=json.dumps(batch))
            if r.status_code in (200, 201, 204, 409):
                # Success path.
                _total_rows_shipped += len(batch)
                _batch_counter += 1
                # v1.1.2: log every 100th batch so we know the happy path is alive.
                # First batch ALWAYS logged so the user can verify on first bot start.
                if _batch_counter == 1 or _batch_counter % 100 == 0:
                    log.info("[cloud_log_shipper] batch #%d succeeded (%d rows in this batch, %d total since boot)",
                             _batch_counter, len(batch), _total_rows_shipped)
            elif r.status_code == 401:
                # Specifically called out: 401 means the JWT is invalid or the
                # token-binding is wrong. RLS/auth issue — different fix from
                # a network error.
                log.warning("[cloud_log_shipper] batch REJECTED with 401 Unauthorized — JWT invalid or RLS denied. %d rows dropped. Response body: %s",
                            len(batch), r.text[:300])
            else:
                log.warning("[cloud_log_shipper] batch rejected with status=%d (%d rows dropped). Response body: %s",
                            r.status_code, len(batch), r.text[:300])
    except Exception as e:
        log.warning("[cloud_log_shipper] POST failed (%d rows dropped): %s", len(batch), e)


def shutdown(wait: bool = True, timeout: float = 3.0) -> None:
    """Stop the background thread, flushing pending rows. Call from app
    shutdown hooks to avoid losing the last ~250 ms of queued logs.

    v1.1.2: now actually invoked from app/main.py FastAPI lifespan."""
    log.info("[cloud_log_shipper] shutdown requested (queue size=%d, total shipped=%d)",
             _q.qsize(), _total_rows_shipped)
    _stop_event.set()
    if wait and _thread is not None:
        _thread.join(timeout=timeout)
        if _thread.is_alive():
            log.warning("[cloud_log_shipper] drain thread did not exit within %.1fs; remaining queue size=%d",
                        timeout, _q.qsize())