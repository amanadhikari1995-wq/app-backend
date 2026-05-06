"""
sync_engine.py — Bulletproof bidirectional bot sync between local SQLite and
Supabase Postgres.

ARCHITECTURE
============
A single daemon thread that wakes up every SYNC_INTERVAL_S and runs one
reconciliation cycle:

    1. Read JWT from session.json (canonical source, also used by wd_cloud.py).
    2. PULL: GET /rest/v1/bots from Supabase. For each cloud row:
         - if no local row with matching cloud_id → INSERT into local SQLite
         - if local row exists AND cloud.updated_at > local.cloud_updated_at
           → UPDATE local from cloud
    3. PUSH: scan local for rows that diverge from cloud:
         - bots without cloud_id  → POST to /rest/v1/bots, stamp returned id
         - local.updated_at > local.cloud_updated_at → PATCH cloud
    4. DELETE PROPAGATION: cloud rows missing from local → DELETE local
       (this is intentional — last-write-wins; rely on cloud as source of truth).

Failure modes are explicit:
    - session.json missing            → sync paused, no cloud writes attempted
    - JWT expired                     → sync paused, log + retry next cycle
    - Supabase 5xx / network error    → log + retry next cycle, never crash
    - Conflict (both sides edited)    → cloud's updated_at wins (newer)

State accessible via get_status() so the UI / a /api/sync/status endpoint can
surface "syncing", "paused", "error", last sync time, items pending.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from .database import SessionLocal
from . import models

log = logging.getLogger("watchdog.sync")

SUPABASE_URL      = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

SYNC_INTERVAL_S   = float(os.getenv("WATCHDOG_SYNC_INTERVAL_S", "60.0"))
HTTP_TIMEOUT_S    = 10.0

# Columns we mirror in both directions. Keep in lockstep with sql/cloud-sync.sql
# AND models.Bot. Anything not in this list stays local-only (e.g. status,
# run_count, is_running — those are runtime state managed locally).
SYNCED_FIELDS = (
    "name", "description", "code", "bot_type",
    "schedule_type", "schedule_start", "schedule_end",
    "max_amount_per_trade", "max_contracts_per_trade", "max_daily_loss",
    "auto_restart",
)

# Columns mirrored for api_connections.
CONN_SYNC_FIELDS = (
    "name", "base_url", "api_key", "api_secret", "is_active",
)


# ── Status surface for /api/sync/status ─────────────────────────────────────

@dataclass
class SyncStatus:
    state:           str   = "idle"              # idle|syncing|paused|error
    last_sync_at:    Optional[str] = None        # ISO-8601
    last_error:      Optional[str] = None
    pulls_total:     int   = 0                   # cumulative cloud→local writes
    pushes_total:    int   = 0                   # cumulative local→cloud writes
    cycles_total:    int   = 0
    last_cycle_ms:   Optional[float] = None
    paused_reason:   Optional[str] = None
    supabase_uid:    Optional[str] = None        # who we're syncing as

    def snapshot(self) -> dict:
        return asdict(self)

_status = SyncStatus()
_status_lock = threading.Lock()


def get_status() -> dict:
    with _status_lock:
        return _status.snapshot()


def _set_status(**kwargs) -> None:
    with _status_lock:
        for k, v in kwargs.items():
            setattr(_status, k, v)


# ── session.json + JWT ──────────────────────────────────────────────────────

def _user_data_dir() -> Path:
    """Mirror the path computed by run_backend.py + Electron main.js."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "WatchDog"
    if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        return Path.home() / "Library" / "Application Support" / "WatchDog"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "WatchDog"


def _read_session() -> Optional[dict]:
    """Return the session.json contents or None. Never raises."""
    try:
        p = _user_data_dir() / "session.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("session.json read failed: %s", e)
        return None


def _is_jwt_expired(jwt: str, safety_margin_s: int = 30) -> bool:
    """Cheap exp check — decodes JWT payload without verifying signature.
    Returns True if exp < (now + safety_margin)."""
    try:
        import base64
        parts = jwt.split(".")
        if len(parts) != 3:
            return True
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if not exp:
            return False
        return int(exp) < int(time.time()) + safety_margin_s
    except Exception:
        return True


def _refresh_session(refresh_token: str) -> Optional[dict]:
    """Trade a refresh_token for a fresh access_token via Supabase.
    Returns the new session dict or None on failure."""
    if not refresh_token or not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    url = f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token"
    headers = {
        "apikey":       SUPABASE_ANON_KEY,
        "content-type": "application/json",
        "accept":       "application/json",
    }
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as c:
            r = c.post(url, headers=headers, json={"refresh_token": refresh_token})
        if r.status_code != 200:
            log.warning("token refresh %d: %s", r.status_code, r.text[:200])
            return None
        data = r.json()
        return {
            "access_token":  data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_at":    int(time.time()) + int(data.get("expires_in", 3600)),
            "user_id":       data.get("user", {}).get("id"),
            "email":         data.get("user", {}).get("email"),
        }
    except httpx.HTTPError as e:
        log.warning("token refresh network error: %s", e)
        return None


def _write_session(session: dict) -> bool:
    """Atomically write session.json. Used after a successful refresh."""
    try:
        path = _user_data_dir() / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(session, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception as e:
        log.warning("session.json write failed: %s", e)
        return False


# ── Cloud HTTP helpers ──────────────────────────────────────────────────────

def _headers(jwt: str, prefer_return: bool = True) -> dict:
    h = {
        "apikey":        SUPABASE_ANON_KEY,
        "authorization": f"Bearer {jwt}",
        "accept":        "application/json",
        "content-type":  "application/json",
    }
    if prefer_return:
        h["prefer"] = "return=representation"
    return h


def _bot_payload(bot: models.Bot, supabase_uid: str) -> dict:
    out: dict[str, Any] = {"user_id": supabase_uid}
    for f in SYNCED_FIELDS:
        v = getattr(bot, f, None)
        if v is not None:
            out[f] = v
    return out


def _pull_cloud_bots(client: httpx.Client, jwt: str) -> Optional[list[dict]]:
    url = f"{SUPABASE_URL}/rest/v1/bots?select=*&order=updated_at.desc"
    try:
        r = client.get(url, headers=_headers(jwt, prefer_return=False), timeout=HTTP_TIMEOUT_S)
        if r.status_code == 401:
            log.warning("cloud pull 401 — JWT likely expired")
            return None
        if r.status_code != 200:
            log.warning("cloud pull %d: %s", r.status_code, r.text[:200])
            return None
        return r.json()
    except httpx.HTTPError as e:
        log.warning("cloud pull network error: %s", e)
        return None


def _push_insert(client: httpx.Client, jwt: str, supabase_uid: str, bot: models.Bot) -> Optional[dict]:
    url = f"{SUPABASE_URL}/rest/v1/bots"
    try:
        r = client.post(url, headers=_headers(jwt), json=_bot_payload(bot, supabase_uid), timeout=HTTP_TIMEOUT_S)
        if r.status_code in (200, 201):
            rows = r.json()
            return rows[0] if rows else None
        log.warning("cloud insert %d for bot id=%s: %s", r.status_code, bot.id, r.text[:200])
        return None
    except httpx.HTTPError as e:
        log.warning("cloud insert network error: %s", e)
        return None


def _push_update(client: httpx.Client, jwt: str, cloud_id: str, bot: models.Bot) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/bots?id=eq.{cloud_id}"
    payload = {f: getattr(bot, f, None) for f in SYNCED_FIELDS if getattr(bot, f, None) is not None}
    try:
        r = client.patch(url, headers=_headers(jwt, prefer_return=False), json=payload, timeout=HTTP_TIMEOUT_S)
        if r.status_code in (200, 204):
            return True
        log.warning("cloud update %d for cloud_id=%s: %s", r.status_code, cloud_id, r.text[:200])
        return False
    except httpx.HTTPError as e:
        log.warning("cloud update network error: %s", e)
        return False


# ── The sync loop ───────────────────────────────────────────────────────────

def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Supabase returns RFC3339 with microseconds; tolerate both.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _apply_cloud_to_local(local: models.Bot, cb: dict) -> bool:
    changed = False
    for col in SYNCED_FIELDS:
        v = cb.get(col)
        if v is None:
            continue
        if getattr(local, col) != v:
            setattr(local, col, v)
            changed = True
    return changed


def _build_local_from_cloud(user_id: int, cb: dict) -> models.Bot:
    return models.Bot(
        user_id        = user_id,
        cloud_id       = cb.get("id"),
        cloud_synced_at= datetime.now(timezone.utc),
        name           = cb.get("name") or "Untitled",
        description    = cb.get("description"),
        code           = cb.get("code") or "",
        bot_type       = cb.get("bot_type"),
        schedule_type  = cb.get("schedule_type") or "always",
        schedule_start = cb.get("schedule_start"),
        schedule_end   = cb.get("schedule_end"),
        max_amount_per_trade    = cb.get("max_amount_per_trade"),
        max_contracts_per_trade = cb.get("max_contracts_per_trade"),
        max_daily_loss          = cb.get("max_daily_loss"),
        auto_restart   = bool(cb.get("auto_restart") or False),
    )


# -- api_connections helpers --

def _conn_payload(conn, supabase_uid, bot_cloud_id):
    out = {"user_id": supabase_uid}
    if bot_cloud_id: out["bot_id"] = bot_cloud_id
    for f in CONN_SYNC_FIELDS:
        v = getattr(conn, f, None)
        if v is not None: out[f] = v
    return out


def _pull_cloud_conns(client, jwt):
    url = f"{SUPABASE_URL}/rest/v1/api_connections?select=*&order=updated_at.desc"
    try:
        r = client.get(url, headers=_headers(jwt, prefer_return=False), timeout=HTTP_TIMEOUT_S)
        if r.status_code == 401: log.warning("conn pull 401 -- JWT expired"); return None
        if r.status_code != 200: log.warning("conn pull %d: %s", r.status_code, r.text[:200]); return None
        return r.json()
    except httpx.HTTPError as e: log.warning("conn pull error: %s", e); return None


def _push_insert_conn(client, jwt, payload):
    url = f"{SUPABASE_URL}/rest/v1/api_connections"
    try:
        r = client.post(url, headers=_headers(jwt), json=payload, timeout=HTTP_TIMEOUT_S)
        if r.status_code in (200, 201): rows = r.json(); return rows[0] if rows else None
        log.warning("conn insert %d: %s", r.status_code, r.text[:200]); return None
    except httpx.HTTPError as e: log.warning("conn insert error: %s", e); return None


def _push_update_conn(client, jwt, cloud_id, payload):
    url = f"{SUPABASE_URL}/rest/v1/api_connections?id=eq.{cloud_id}"
    patch = {k: v for k, v in payload.items() if k != "user_id"}
    try:
        r = client.patch(url, headers=_headers(jwt, prefer_return=False), json=patch, timeout=HTTP_TIMEOUT_S)
        if r.status_code in (200, 204): return True
        log.warning("conn update %d: %s", r.status_code, r.text[:200]); return False
    except httpx.HTTPError as e: log.warning("conn update error: %s", e); return False


def _local_user_for_supabase(db: Session, supabase_uid: str, email: Optional[str]) -> models.User:
    """For the desktop app (single-user install), the LOCAL user is always the
    singleton id=1. We just stamp its supabase_uid + email so the cloud-side
    knows who to push as. Bots stay owned by user_id=1 in the local DB so
    the renderer (which uses get_default_user) sees them.

    All Supabase-identified bots from the cloud are written into local SQLite
    with user_id=1, regardless of which Supabase user owns them in the cloud.
    The supabase_uid mapping for cloud writes lives on the User row, not on
    individual Bot rows.

    Bonus: any orphaned bots under other local user_ids (from earlier sync
    bugs) get re-adopted into id=1 here — repairs the user_id=2 strand.
    """
    user = db.query(models.User).filter(models.User.id == 1).first()
    if not user:
        # Should never happen — ensure_default_user creates this on startup.
        user = models.User(
            id=1, username="watchdog", email="watchdog@local",
            hashed_password="", is_active=True,
        )
        db.add(user); db.commit(); db.refresh(user)

    # Order matters: clean up orphans BEFORE updating singleton's supabase_uid,
    # because the partial unique index on (supabase_uid) WHERE NOT NULL would
    # otherwise reject the singleton update if another user row already holds
    # this supabase_uid (which it does — that's exactly the orphan we're
    # repairing).

    # 1) Move ALL user-owned rows from any non-singleton user_id → id=1.
    #    Bots first, then dependent tables (bot_logs, trades, ai_models,
    #    training_runs, model_files, whop_memberships) that have FK to user.
    #    Doing this before user delete avoids FOREIGN KEY violations.
    global _orphan_repair_done
    if not _orphan_repair_done:
        # Run ownership-repair SQL exactly once per process lifetime so we
        # don't hammer SQLite with 8 UPDATE queries on every 30s cycle.
        _orphan_repair_done = True
        repair_tables = [
            ("bots",            models.Bot),
            ("bot_logs",        models.BotLog),
            ("trades",          models.Trade),
            ("ai_models",       models.AIModel),
            ("training_runs",   models.TrainingRun),
            ("model_files",     models.ModelFile),
            ("whop_memberships", models.WhopMembership),
        ]
        total_moved = 0
        for label, model in repair_tables:
            try:
                n = (db.query(model)
                     .filter(model.user_id != 1)
                     .update({"user_id": 1}, synchronize_session=False))
                if n:
                    db.commit()
                    total_moved += n
                    log.info("repaired ownership: moved %d %s row(s) to user_id=1", n, label)
            except Exception as e:
                db.rollback()
                log.warning("ownership repair %s failed: %s", label, e)

        # 2) Garbage-collect non-singleton user rows. They own nothing now.
        #    Use raw SQL DELETE through SQLAlchemy bulk; FK should not fire
        #    because step 1 cleared everything.
        try:
            deleted = (db.query(models.User)
                       .filter(models.User.id != 1)
                       .delete(synchronize_session=False))
            if deleted:
                db.commit()
                log.info("cleaned up %d orphan user row(s)", deleted)
        except Exception as e:
            db.rollback()
            log.warning("orphan user cleanup failed: %s", e)

    # 2.5) USER SWITCH: if the singleton was previously stamped with a
    # DIFFERENT supabase_uid, the previous user's bots/connections/etc are
    # still owned by user_id=1 in local SQLite. The renderer (which reads
    # all rows for user_id=1) would show the previous user's data to the
    # new user — and any sync push would also leak the previous user's
    # data to the new user's cloud account. Wipe per-user data BEFORE
    # stamping the new identity.
    #
    # Skipped on first ever sign-in (previous_uid is None) and on sign-in
    # of the same user again (uids match — handled by the no-op branch
    # below).
    previous_uid = user.supabase_uid
    if previous_uid and previous_uid != supabase_uid:
        log.warning(
            "user switch detected on local install: %s… → %s… — wiping "
            "previous user's local data before pulling new user's cloud state",
            previous_uid[:8], supabase_uid[:8],
        )
        wipe_tables = [
            ("bots",             models.Bot),
            ("bot_logs",         models.BotLog),
            ("api_connections",  models.ApiConnection),
            ("trades",           models.Trade),
            ("ai_models",        models.AIModel),
            ("training_runs",    models.TrainingRun),
            ("model_files",      models.ModelFile),
            ("whop_memberships", models.WhopMembership),
        ]
        for label, model in wipe_tables:
            try:
                n = (db.query(model)
                     .filter(model.user_id == 1)
                     .delete(synchronize_session=False))
                if n:
                    log.info("user-switch wipe: removed %d %s row(s)", n, label)
            except Exception as e:
                db.rollback()
                log.warning("user-switch wipe %s failed: %s", label, e)
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            log.warning("user-switch wipe commit failed: %s", e)

    # 3) Now stamp supabase identity onto the singleton (no unique conflict).
    changed = False
    if user.supabase_uid != supabase_uid:
        user.supabase_uid = supabase_uid; changed = True
    if email and (not user.email or user.email == "watchdog@local"):
        user.email = email; changed = True
    if changed:
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            log.warning("singleton stamp failed: %s", e)

    return user


def _run_one_cycle(client: httpx.Client) -> None:
    """One reconciliation cycle. Bounded work, never raises."""
    cycle_start = time.time()

    # 1. Source of truth for auth: session.json. If missing or expired, pause.
    sess = _read_session()
    if not sess:
        _set_status(state="paused", paused_reason="session.json missing — sign in to desktop app")
        return
    jwt = sess.get("access_token")
    supabase_uid = sess.get("user_id") or sess.get("user", {}).get("id")
    email = sess.get("email")
    if not jwt or not supabase_uid:
        _set_status(state="paused", paused_reason="session.json missing access_token / user_id")
        return
    # Refresh proactively when we're inside a 60s window of expiry — saves
    # one whole sync cycle of "paused" status while waiting for Electron to
    # refresh the token (which it might never do if the renderer is closed).
    if _is_jwt_expired(jwt, safety_margin_s=60):
        refresh_token = sess.get("refresh_token")
        log.info("JWT expired (or nearly) — attempting refresh via refresh_token")
        new_sess = _refresh_session(refresh_token) if refresh_token else None
        if new_sess and new_sess.get("access_token"):
            # Merge: keep email if refresh response didn't return one.
            new_sess.setdefault("email", sess.get("email"))
            new_sess["saved_at"] = datetime.now(timezone.utc).isoformat()
            _write_session(new_sess)
            jwt = new_sess["access_token"]
            log.info("JWT refreshed; continuing sync cycle with new token")
        else:
            _set_status(state="paused",
                        paused_reason="JWT expired and refresh failed — sign in again",
                        supabase_uid=supabase_uid)
            return
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        _set_status(state="paused", paused_reason="SUPABASE_URL / ANON_KEY not in env")
        return

    _set_status(state="syncing", supabase_uid=supabase_uid)

    db = SessionLocal()
    pulls = pushes = 0
    try:
        user = _local_user_for_supabase(db, supabase_uid, email)

        # ── PULL: cloud → local ─────────────────────────────────────────
        cloud_rows = _pull_cloud_bots(client, jwt)
        if cloud_rows is None:
            _set_status(state="error", last_error="cloud pull failed")
            return

        cloud_by_id = {cb["id"]: cb for cb in cloud_rows if cb.get("id")}

        for cid, cb in cloud_by_id.items():
            local = db.query(models.Bot).filter(models.Bot.cloud_id == cid).first()
            if not local:
                db.add(_build_local_from_cloud(user.id, cb))
                pulls += 1
            else:
                # Conflict resolution: cloud wins iff cloud.updated_at >
                # local.cloud_synced_at (when we last took cloud's value).
                # SQLite drops tz info on DateTime columns; force both sides
                # to tz-aware UTC before comparing or Python raises.
                cloud_dt = _parse_iso(cb.get("updated_at"))
                local_dt = local.cloud_synced_at
                if local_dt is not None and local_dt.tzinfo is None:
                    local_dt = local_dt.replace(tzinfo=timezone.utc)
                if cloud_dt and (not local_dt or cloud_dt > local_dt):
                    if _apply_cloud_to_local(local, cb):
                        local.cloud_synced_at = cloud_dt
                        pulls += 1

        # ── PUSH: local → cloud ─────────────────────────────────────────
        # Two cases: never-synced (no cloud_id) AND locally-newer than cloud.
        for bot in db.query(models.Bot).filter(models.Bot.user_id == user.id).all():
            if not bot.cloud_id:
                row = _push_insert(client, jwt, supabase_uid, bot)
                if row and row.get("id"):
                    bot.cloud_id = row["id"]
                    cloud_dt = _parse_iso(row.get("updated_at"))
                    bot.cloud_synced_at = cloud_dt or datetime.now(timezone.utc)
                    pushes += 1
                continue
            # Already synced: detect local mutation by comparing the bot's
            # in-memory hash against the cloud's. Cheap heuristic — if the
            # cloud row we just pulled differs from the local payload we'd
            # send, push.
            cb = cloud_by_id.get(bot.cloud_id)
            if cb:
                local_payload = _bot_payload(bot, supabase_uid)
                cloud_payload = {k: cb.get(k) for k in local_payload if k != "user_id"}
                local_payload.pop("user_id", None)
                if local_payload != cloud_payload:
                    if _push_update(client, jwt, bot.cloud_id, bot):
                        bot.cloud_synced_at = datetime.now(timezone.utc)
                        pushes += 1

        # ── DELETE PROPAGATION: cloud row vanished → local row removed ──
        # Only delete local rows that DID have a cloud_id (i.e. were synced
        # before). Local-only rows (cloud_id NULL) stay — they'll be pushed
        # next cycle.
        synced_local = (db.query(models.Bot)
                        .filter(models.Bot.user_id == user.id,
                                models.Bot.cloud_id.isnot(None))
                        .all())
        # Safety: if cloud returned 0 bots but we have synced bots locally, this is
        # almost certainly a transient outage / RLS hiccup. Don't delete user data.
        if not cloud_by_id and synced_local:
            log.warning("cloud returned 0 bots but %d local bots have cloud_id — skipping delete propagation (transient?)", len(synced_local))
        else:
            for bot in synced_local:
                if bot.cloud_id not in cloud_by_id:
                    db.delete(bot)
                    pulls += 1   # count as pull (cloud-side change reflected here)

        # -- api_connections sync (pull+push+delete) --
        cloud_conns = _pull_cloud_conns(client, jwt)
        if cloud_conns is not None:
            conn_by_id = {cc["id"]: cc for cc in cloud_conns if cc.get("id")}
            for cid, cc in conn_by_id.items():
                lbid = None
                if cc.get("bot_id"):
                    lb = db.query(models.Bot).filter(models.Bot.cloud_id == cc["bot_id"]).first()
                    if lb: lbid = lb.id
                lc = db.query(models.ApiConnection).filter(models.ApiConnection.cloud_id == cid).first()
                if not lc:
                    db.add(models.ApiConnection(user_id=user.id, bot_id=lbid, cloud_id=cid,
                        name=cc.get("name") or "Unnamed", base_url=cc.get("base_url"),
                        api_key=cc.get("api_key"), api_secret=cc.get("api_secret"), is_active=cc.get("is_active", True))); pulls += 1
                else:
                    ch = False
                    for f in CONN_SYNC_FIELDS:
                        v = cc.get(f)
                        if v is not None and getattr(lc, f) != v: setattr(lc, f, v); ch = True
                    if lbid and lc.bot_id != lbid: lc.bot_id = lbid; ch = True
                    if ch: pulls += 1
            for c in db.query(models.ApiConnection).filter(models.ApiConnection.user_id == user.id, models.ApiConnection.is_active == True).all():
                bcid = None
                if c.bot_id:
                    lb2 = db.query(models.Bot).filter(models.Bot.id == c.bot_id).first()
                    if lb2: bcid = lb2.cloud_id
                pl = _conn_payload(c, supabase_uid, bcid)
                if not c.cloud_id:
                    row = _push_insert_conn(client, jwt, pl)
                    if row and row.get("id"): c.cloud_id = row["id"]; pushes += 1
                else:
                    cc2 = conn_by_id.get(c.cloud_id)
                    if cc2:
                        if {f: cc2.get(f) for f in CONN_SYNC_FIELDS} != {f: getattr(c, f, None) for f in CONN_SYNC_FIELDS}:
                            if _push_update_conn(client, jwt, c.cloud_id, pl): pushes += 1
                    else:
                        row = _push_insert_conn(client, jwt, pl)
                        if row and row.get("id"): c.cloud_id = row["id"]; pushes += 1
            # Safety: same guard for api_connections — don't delete on empty cloud response.
            synced_conns = db.query(models.ApiConnection).filter(models.ApiConnection.user_id == user.id, models.ApiConnection.cloud_id.isnot(None)).all()
            if not conn_by_id and synced_conns:
                log.warning("cloud returned 0 api_connections but %d local conns have cloud_id — skipping delete propagation (transient?)", len(synced_conns))
            else:
                for c in synced_conns:
                    if c.cloud_id not in conn_by_id: db.delete(c); pulls += 1

        db.commit()

        with _status_lock:
            _status.pulls_total  += pulls
            _status.pushes_total += pushes
            _status.cycles_total += 1
            _status.last_sync_at = datetime.now(timezone.utc).isoformat()
            _status.last_cycle_ms = (time.time() - cycle_start) * 1000
            _status.last_error = None
            _status.paused_reason = None
            _status.state = "idle"

        if pulls or pushes:
            log.info("sync cycle: +%d pulled, +%d pushed (%.0f ms)",
                     pulls, pushes, (time.time() - cycle_start) * 1000)

    except Exception as e:
        # Catch-all: any unexpected exception MUST NOT take down the thread.
        try:
            db.rollback()
        except Exception:
            pass
        _set_status(state="error", last_error=f"cycle exception: {e}")
        log.exception("sync cycle failed")
    finally:
        db.close()


# ── Thread management ───────────────────────────────────────────────────────

_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_orphan_repair_done = False  # run ownership-repair SQL only once per process


def _loop():
    log.info("sync engine starting (interval=%.1fs)", SYNC_INTERVAL_S)
    # Use one httpx Client across cycles so connection pool + DNS is reused.
    with httpx.Client() as client:
        while not _stop_event.is_set():
            try:
                _run_one_cycle(client)
            except Exception as e:
                log.exception("loop top-level guard caught: %s", e)
            _stop_event.wait(SYNC_INTERVAL_S)
    log.info("sync engine stopped")


def start() -> None:
    """Start the daemon thread. Idempotent."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="sync-engine", daemon=True)
    _thread.start()


def stop() -> None:
    _stop_event.set()
