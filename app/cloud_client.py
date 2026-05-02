"""
cloud_client.py — Supabase PostgREST client for cross-device bot sync.

Why a custom client and not supabase-py:
  - We already have httpx as a dep; adding supabase-py is 50+ MB of transitive
    deps (postgrest-py, gotrue, realtime, storage3) we don't need.
  - PostgREST is just REST-over-tables; the surface we use (~5 endpoints) is
    trivial to wrap. This file is < 200 lines including comments.

Design points:
  1. Auth is the user's Bearer JWT — same one /auth/v1/user validates in
     auth.py. RLS on the `bots` table ensures the user can only see their
     own rows; this client doesn't need to filter.
  2. Every method takes the JWT as the first arg. We never store it in a
     module global because the same Python process can serve multiple users
     when the desktop runs as a multi-tenant relay (uncommon but supported).
  3. Network errors degrade gracefully: returns None / empty list. The
     caller (bots router) treats cloud as best-effort; local SQLite is the
     guarantee.
  4. We never `delete` from the cloud unconditionally. `delete_bot` does
     delete-by-cloud-id only — if the bot was never synced (no cloud_id),
     it's a no-op. This stops "I deleted on my phone, then offline create
     on my laptop" patterns from ever wiping cloud data behind the user.
"""

from __future__ import annotations

import os
import logging
from typing import Optional, Iterable, Any
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

SUPABASE_URL      = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

# Fields we mirror from local Bot rows into the cloud `bots` table. Keep in
# strict lockstep with sql/cloud-sync.sql column list. Anything not in this
# list stays local-only (run_count, last_run_at, status, etc are runtime
# state managed by whichever desktop is executing).
SYNCED_FIELDS = (
    "name", "description", "code", "bot_type",
    "schedule_type", "schedule_start", "schedule_end",
    "max_amount_per_trade", "max_contracts_per_trade", "max_daily_loss",
    "auto_restart",
)


def is_configured() -> bool:
    """True iff the cloud client has the env vars it needs to function."""
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)


def _headers(jwt: str, *, prefer_return: bool = True) -> dict:
    h = {
        "apikey":        SUPABASE_ANON_KEY,
        "authorization": f"Bearer {jwt}",
        "content-type":  "application/json",
        "accept":        "application/json",
    }
    if prefer_return:
        # PostgREST: return the inserted/updated row in the response body.
        h["prefer"] = "return=representation"
    return h


def _bot_to_cloud_payload(bot: Any, supabase_uid: str) -> dict:
    """Convert a local SQLAlchemy Bot row to the cloud table's column shape."""
    payload: dict[str, Any] = {"user_id": supabase_uid}
    for f in SYNCED_FIELDS:
        v = getattr(bot, f, None)
        if v is not None:
            payload[f] = v
    return payload


# ── Reads ────────────────────────────────────────────────────────────────────

async def list_cloud_bots(jwt: str) -> Optional[list[dict]]:
    """All bots the JWT's owner can see in the cloud. Returns None on error."""
    if not is_configured():
        return None
    url = f"{SUPABASE_URL}/rest/v1/bots?select=*&order=created_at.desc"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=_headers(jwt, prefer_return=False))
        if r.status_code != 200:
            log.warning("[cloud] list_bots %s: %s", r.status_code, r.text[:200])
            return None
        return r.json()
    except httpx.HTTPError as e:
        log.warning("[cloud] list_bots transport error: %s", e)
        return None


# ── Writes ───────────────────────────────────────────────────────────────────

async def insert_bot(jwt: str, supabase_uid: str, bot: Any) -> Optional[dict]:
    """Insert a new row in the cloud bots table. Returns the inserted row
    (so the caller can store cloud_id locally) or None on failure."""
    if not is_configured():
        return None
    url = f"{SUPABASE_URL}/rest/v1/bots"
    payload = _bot_to_cloud_payload(bot, supabase_uid)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, headers=_headers(jwt), json=payload)
        if r.status_code not in (200, 201):
            log.warning("[cloud] insert_bot %s: %s", r.status_code, r.text[:200])
            return None
        rows = r.json()
        return rows[0] if rows else None
    except httpx.HTTPError as e:
        log.warning("[cloud] insert_bot transport error: %s", e)
        return None


async def update_bot(jwt: str, cloud_id: str, bot: Any) -> bool:
    """Update an existing cloud row by its uuid. Returns True on success."""
    if not is_configured() or not cloud_id:
        return False
    url = f"{SUPABASE_URL}/rest/v1/bots?id=eq.{cloud_id}"
    payload = {f: getattr(bot, f, None) for f in SYNCED_FIELDS if getattr(bot, f, None) is not None}
    if not payload:
        return True
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.patch(url, headers=_headers(jwt, prefer_return=False), json=payload)
        if r.status_code not in (200, 204):
            log.warning("[cloud] update_bot %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except httpx.HTTPError as e:
        log.warning("[cloud] update_bot transport error: %s", e)
        return False


async def delete_bot(jwt: str, cloud_id: str) -> bool:
    """Delete an existing cloud row by its uuid. No-op if cloud_id is empty."""
    if not is_configured() or not cloud_id:
        return False
    url = f"{SUPABASE_URL}/rest/v1/bots?id=eq.{cloud_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.delete(url, headers=_headers(jwt, prefer_return=False))
        if r.status_code not in (200, 204):
            log.warning("[cloud] delete_bot %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except httpx.HTTPError as e:
        log.warning("[cloud] delete_bot transport error: %s", e)
        return False


# ── Migration helper ─────────────────────────────────────────────────────────

async def upsert_bots_batch(jwt: str, supabase_uid: str, bots: Iterable[Any]) -> list[dict]:
    """Bulk-insert local bots into the cloud (used by the one-shot migration
    on first authenticated request). Returns the list of cloud rows so the
    caller can stamp `cloud_id` on each local Bot."""
    if not is_configured():
        return []
    payload = [_bot_to_cloud_payload(b, supabase_uid) for b in bots]
    if not payload:
        return []
    url = f"{SUPABASE_URL}/rest/v1/bots"
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(url, headers=_headers(jwt), json=payload)
        if r.status_code not in (200, 201):
            log.warning("[cloud] upsert_bots_batch %s: %s", r.status_code, r.text[:200])
            return []
        return r.json() or []
    except httpx.HTTPError as e:
        log.warning("[cloud] upsert_bots_batch transport error: %s", e)
        return []


async def mark_migration_done(jwt: str, supabase_uid: str, bots_count: int, source_device: str = "") -> None:
    """Record that this Supabase user has been migrated, so we don't re-import."""
    if not is_configured():
        return
    url = f"{SUPABASE_URL}/rest/v1/cloud_sync_migrations"
    payload = {"user_id": supabase_uid, "bots_count": bots_count, "source_device": source_device or "unknown"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            await c.post(url, headers=_headers(jwt, prefer_return=False), json=payload)
    except httpx.HTTPError as e:
        log.warning("[cloud] mark_migration_done error: %s", e)


async def has_migrated(jwt: str, supabase_uid: str) -> bool:
    """True iff cloud_sync_migrations already has a row for this user."""
    if not is_configured():
        return True   # don't try to migrate without cloud access
    url = f"{SUPABASE_URL}/rest/v1/cloud_sync_migrations?user_id=eq.{supabase_uid}&select=user_id"
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(url, headers=_headers(jwt, prefer_return=False))
        if r.status_code != 200:
            return True
        return bool(r.json())
    except httpx.HTTPError:
        return True
