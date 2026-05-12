"""
cloud_log_shipper.py — Asynchronous Supabase log shipper (v1.1.0).

Each bot stdout line is written to local SQLite by the bots router. This
module ALSO writes that line to Supabase `bot_logs_tail` so the renderer
can subscribe via realtime instead of polling `127.0.0.1:8000/api/bots/
{id}/logs`. The renderer no longer touching the localhost HTTP API is the
v1.1.0 architectural goal — it removes a per-second source of
`ERR_CONNECTION_REFUSED` whenever the backend is restarting / hung.

Single public entry point: `ship_log(...)`. Calls are NON-blocking — the
log line is appended to an in-memory queue and a background daemon thread
batches them to Supabase. The bot stdout-reading loop never waits on a
network call, so SQLite-write speed stays unchanged.

Failure mode: if Supabase is unreachable / throws / 401s, the batch is
dropped and a warning is logged. Local SQLite logs remain authoritative,
the renderer just sees a gap. A future backfill job can repair history.
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

# ── Tunables ────────────────────────────────────────────────────────────────
_MAX_QUEUE      = 5_000   # drop oldest if exceeded — memory safety cap
_BATCH_SIZE     = 100     # max rows per POST
_FLUSH_INTERVAL = 0.25    # seconds — at most 4 flushes/sec under load
_HTTP_TIMEOUT   = 5.0     # seconds per POST
_ENDPOINT       = "/rest/v1/bot_logs_tail"

# ── State ───────────────────────────────────────────────────────────────────
_q: "queue.Queue[dict]" = queue.Queue(maxsize=_MAX_QUEUE)
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_thread_lock = threading.Lock()


def ship_log(bot_id: str, user_id: str, level: str, message: str,
             local_log_id: Optional[int] = None) -> None:
    """Enqueue a log line for async shipping to Supabase. NEVER blocks.

    Args mirror the local-SQLite write site in bots.py:
      bot_id, user_id  : UUIDs as strings (Supabase column type is uuid)
      level            : 'INFO' | 'WARNING' | 'ERROR' (case-insensitive)
      message          : already-ANSI-stripped log line
      local_log_id     : autoincrement id from SQLite — dedup key against
                         the (bot_id, local_log_id) unique partial index.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return  # Supabase not configured — no-op (dev / local-only mode)
    _ensure_thread()
    row = {
        "bot_id":       str(bot_id),
        "user_id":      str(user_id),
        # Lowercase to match the column default. Renderer normalises back
        # to uppercase when reading so consumer filters keep working.
        "level":        str(level).lower(),
        "message":      message,
        "local_log_id": local_log_id,
    }
    try:
        _q.put_nowait(row)
    except queue.Full:
        # Memory cap hit. Drop the OLDEST row, keep the new one — the
        # renderer will see a small gap but stays current. Better than
        # blocking the bot loop.
        try:
            _q.get_nowait()
            _q.put_nowait(row)
        except (queue.Empty, queue.Full):
            pass


def _ensure_thread() -> None:
    global _thread
    # Fast path
    if _thread is not None and _thread.is_alive():
        return
    # Slow path: serialise the spawn
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(target=_drain, name="cloud-log-shipper", daemon=True)
        _thread.start()


def _drain() -> None:
    """Background loop: build a batch (up to _BATCH_SIZE or _FLUSH_INTERVAL),
    POST it, repeat. Exits when _stop_event is set AND the queue is empty."""
    while True:
        if _stop_event.is_set() and _q.empty():
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
    auth = _auth()
    if not auth:
        # No user JWT (e.g. user signed out, or backend booted before login).
        # Drop the batch — local SQLite has the rows; renderer cant subscribe
        # without auth anyway.
        return
    # ── v1.1.1 bug-fix ────────────────────────────────────────────────────
    # _auth() returns the tuple (jwt, user_id); _headers() expects only the
    # jwt string. v1.1.0 passed the whole tuple, producing the malformed
    # header `Authorization: Bearer ('jwt', 'uid')` → Supabase rejected every
    # POST → bot_logs_tail stayed empty. Unpack here.
    jwt_str, _uid = auth
    try:
        url = f"{SUPABASE_URL}{_ENDPOINT}"
        hdrs = _headers(jwt_str)
        # ignore-duplicates: if the same (bot_id, local_log_id) row was
        # already inserted (e.g. queue shipped it once, retry shipped again
        # before the dedup index removed it), the second attempt is a no-op
        # instead of a 409 conflict.
        hdrs["Prefer"] = "resolution=ignore-duplicates,return=minimal"
        with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
            r = c.post(url, headers=hdrs, content=json.dumps(batch))
            if r.status_code not in (200, 201, 204, 409):
                log.warning(
                    "cloud_log_shipper: %d rows rejected (status=%d): %s",
                    len(batch), r.status_code, r.text[:300],
                )
    except Exception as e:
        # Drop the batch — local SQLite has it. Never re-queue (would loop
        # forever if Supabase is permanently broken).
        log.warning(
            "cloud_log_shipper: POST failed (%d rows dropped): %s",
            len(batch), e,
        )


def shutdown(wait: bool = True, timeout: float = 3.0) -> None:
    """Stop the background thread, flushing pending rows. Call from app
    shutdown hooks to avoid losing the last ~250 ms of logs."""
    _stop_event.set()
    if wait and _thread is not None:
        _thread.join(timeout=timeout)