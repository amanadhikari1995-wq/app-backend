"""
wd_cloud.py — WATCH-DOG Cloud Connector
========================================

Bridges your local WATCH-DOG desktop app to the deployed web backend
at watchdogbot.cloud so your web dashboard can control local bots.

What this does:
  1. Fetches Supabase config from the cloud server automatically
  2. Authenticates with Supabase (email + password)
  3. Connects to wss://watchdogbot.cloud/ws as a "desktop" peer
  4. Relays bot run / stop commands from the web dashboard → local backend
  5. Streams live status updates back to the dashboard in real-time
  6. Auto-refreshes Supabase tokens before they expire (~55 min cycle)
  7. Auto-reconnects with exponential back-off on network drops

Usage:
  python wd_cloud.py                           # reads from .env
  CLOUD_EMAIL=you@example.com CLOUD_PASSWORD=secret python wd_cloud.py

Required environment variables (.env or shell):
  CLOUD_EMAIL        Your watchdogbot.cloud account email
  CLOUD_PASSWORD     Your watchdogbot.cloud account password

Optional overrides:
  CLOUD_API_URL      https://watchdogbot.cloud   (default)
  LOCAL_API_URL      http://localhost:8000        (local WATCH-DOG backend)
  LOCAL_API_USER     admin                        (local backend login)
  LOCAL_API_PASS     admin                        (local backend password)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

# Reconfigure stdout/stderr to UTF-8 so emoji + Unicode log lines don't
# crash on Windows where the default codepage (cp1252) can't encode them.
# Frozen exes inherit the host codepage; the parent (Electron) also sets
# PYTHONIOENCODING=utf-8, but this is belt-and-braces.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx
import websockets
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wd_cloud")

# ── Configuration ──────────────────────────────────────────────────────────────
CLOUD_API_URL  = os.getenv("CLOUD_API_URL",  "https://watchdogbot.cloud").rstrip("/")
LOCAL_API_URL  = os.getenv("LOCAL_API_URL",  "http://localhost:8000").rstrip("/")
LOCAL_API_USER = os.getenv("LOCAL_API_USER", "admin")
LOCAL_API_PASS = os.getenv("LOCAL_API_PASS", "admin")
CLOUD_EMAIL    = os.getenv("CLOUD_EMAIL",    "")
CLOUD_PASSWORD = os.getenv("CLOUD_PASSWORD", "")

# ── Session file (written by Electron main on user login) ───────────────────
# Same path computed by run_backend.py and electron/session-store.js. Both
# must agree, so this resolution is deliberately copy-pasted in three places
# rather than hidden behind a common helper that's not available in a
# PyInstaller-frozen exe.
def _user_data_dir() -> "pathlib.Path":
    import pathlib
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(pathlib.Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(pathlib.Path.home() / ".local" / "share")
    return pathlib.Path(base) / "WatchDog"

SESSION_FILE = _user_data_dir() / "session.json"

# Derived WebSocket URL (https → wss, http → ws)
CLOUD_WS_URL = CLOUD_API_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"

# Reconnect settings
_RECONNECT_BASE_S  = 2.0    # start with 2-second back-off
_RECONNECT_MAX_S   = 60.0   # cap at 60 seconds
_TOKEN_REFRESH_S   = 55 * 60  # refresh Supabase token every 55 min (expires at 60)


# ──────────────────────────────────────────────────────────────────────────────
# 1. CLOUD AUTH  (Supabase via REST — no extra SDK needed)
# ──────────────────────────────────────────────────────────────────────────────
class CloudAuth:
    """
    Handles the desktop's Supabase session. Three sources of credentials,
    tried in this order:

      1. session.json written by Electron main when the user signs in.
         Holds {access_token, refresh_token, expires_at, user_id, email}.
         This is the path every real install takes.

      2. WATCHDOG_AUTH_TOKEN env var. Convenient for tests and CI — pass
         a fresh token directly without going through the file.

      3. CLOUD_EMAIL + CLOUD_PASSWORD env vars (legacy / dev). Same flow
         this class always had; we keep it so devs running wd_cloud.py
         standalone don't have to mock Electron.

    Token refresh: when the access_token has < 5 minutes left, we use the
    refresh_token to mint a new one via Supabase's REST API. The new
    {access_token, refresh_token, expires_at} is written back to
    session.json so the renderer (and a future restart) sees it.
    """

    def __init__(self) -> None:
        self._supabase_url: Optional[str]  = None
        self._anon_key:     Optional[str]  = None
        self.access_token:  Optional[str]  = None
        self.refresh_token: Optional[str]  = None
        self._expires_at:   float          = 0.0
        self._user_id:      Optional[str]  = None
        self._email:        Optional[str]  = None
        # Source of the current credentials — drives how we refresh:
        #   "session_file" → write back to session.json after refresh
        #   "env"          → don't write back (env-only test path)
        #   "password"     → re-do email/password login
        self._cred_source:  str            = "none"
        self._client = httpx.AsyncClient(timeout=15.0)

    # ── session.json read/write ──────────────────────────────────────────────

    def _load_session_file(self) -> bool:
        """Returns True if session.json was found and loaded into self.*."""
        try:
            if not SESSION_FILE.exists():
                return False
            with SESSION_FILE.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            tok = data.get("access_token")
            if not tok:
                return False
            self.access_token  = tok
            self.refresh_token = data.get("refresh_token")
            self._expires_at   = float(data.get("expires_at") or 0)
            self._user_id      = data.get("user_id")
            self._email        = data.get("email")
            self._cred_source  = "session_file"
            log.info("Loaded session for %s (uid=%s, expires in %ds)",
                     self._email or "?",
                     (self._user_id or "?")[:8],
                     max(0, int(self._expires_at - time.time())))
            return True
        except Exception as e:
            log.warning("Could not read %s: %s", SESSION_FILE, e)
            return False

    def _save_session_file(self) -> None:
        """Persist refreshed tokens back so the renderer sees the same state."""
        if self._cred_source != "session_file":
            return  # only write back if that's where we read from
        try:
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "access_token":  self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at":    int(self._expires_at),
                "user_id":       self._user_id,
                "email":         self._email,
                "saved_at":      datetime.datetime.utcnow().isoformat() + "Z",
            }
            tmp = SESSION_FILE.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            tmp.replace(SESSION_FILE)
        except Exception as e:
            log.warning("Could not write %s: %s", SESSION_FILE, e)

    async def _fetch_supabase_config(self) -> None:
        """Auto-discover Supabase URL + anon key from the cloud server."""
        if self._supabase_url:
            return
        try:
            r = await self._client.get(f"{CLOUD_API_URL}/api/config")
            r.raise_for_status()
            cfg = r.json()
            sb = cfg.get("supabase", {})
            self._supabase_url = sb.get("url") or cfg.get("supabaseUrl") or os.getenv("SUPABASE_URL", "")
            self._anon_key     = sb.get("anonKey") or cfg.get("supabaseAnonKey") or os.getenv("SUPABASE_ANON_KEY", "")
            log.info("Supabase config loaded from %s/api/config", CLOUD_API_URL)
        except Exception as e:
            # Fall back to env vars
            self._supabase_url = os.getenv("SUPABASE_URL", "")
            self._anon_key     = os.getenv("SUPABASE_ANON_KEY", "")
            log.warning("Could not fetch /api/config (%s) — using env vars", e)

        if not self._supabase_url or not self._anon_key:
            raise RuntimeError(
                "Supabase URL / anon key not available. "
                "Set SUPABASE_URL and SUPABASE_ANON_KEY in your .env, "
                "or make sure the cloud server /api/config endpoint is reachable."
            )

    async def login(self) -> str:
        """
        Acquire an access_token using whichever credentials are available.
        Tries (in order):
          1. session.json (created by Electron main when user signs in)
          2. WATCHDOG_AUTH_TOKEN env var (test/CI)
          3. CLOUD_EMAIL + CLOUD_PASSWORD env vars (legacy / dev)
        """
        # Path 1 — Electron-shared session file
        if self._load_session_file():
            # If the file's token is already expired, fall through to refresh()
            if self._expires_at > time.time() + 60:
                return self.access_token
            # Token expired but we have a refresh_token → use it
            if self.refresh_token:
                try:
                    return await self.refresh()
                except Exception as e:
                    log.warning("Session-file refresh failed (%s) — falling back", e)
            # Fall through to other auth paths

        # Path 2 — direct token via env (tests / CI)
        env_token = os.getenv("WATCHDOG_AUTH_TOKEN", "").strip()
        if env_token:
            self.access_token  = env_token
            self.refresh_token = os.getenv("WATCHDOG_REFRESH_TOKEN", "") or None
            self._expires_at   = time.time() + 3600    # assume 1h, will refresh on demand
            self._cred_source  = "env"
            log.info("Using access token from WATCHDOG_AUTH_TOKEN env var.")
            return self.access_token

        # Path 3 — legacy email/password
        if not CLOUD_EMAIL or not CLOUD_PASSWORD:
            raise RuntimeError(
                "No credentials available.\n"
                "  - Sign in via the desktop app (creates session.json automatically), OR\n"
                "  - Set WATCHDOG_AUTH_TOKEN, OR\n"
                "  - Set CLOUD_EMAIL + CLOUD_PASSWORD in your .env file."
            )

        await self._fetch_supabase_config()
        url = f"{self._supabase_url}/auth/v1/token?grant_type=password"
        headers = {"apikey": self._anon_key, "Content-Type": "application/json"}
        body    = {"email": CLOUD_EMAIL, "password": CLOUD_PASSWORD}

        log.info("Logging in to Supabase as %s …", CLOUD_EMAIL)
        r = await self._client.post(url, headers=headers, json=body)
        if r.status_code == 400:
            raise RuntimeError(f"Login failed - bad credentials: {r.text}")
        r.raise_for_status()
        data = r.json()
        self.access_token  = data["access_token"]
        self.refresh_token = data.get("refresh_token", "")
        self._expires_at   = time.time() + data.get("expires_in", 3600)
        self._email        = CLOUD_EMAIL
        self._cred_source  = "password"
        log.info("Supabase login successful - token valid for %d s", data.get("expires_in", 3600))
        return self.access_token

    async def refresh(self) -> str:
        """Use refresh_token (Supabase) to mint a new access_token. Writes
        the result back to session.json if that's where we read from."""
        await self._fetch_supabase_config()
        if not self.refresh_token:
            raise RuntimeError("No refresh_token available to refresh with.")

        url = f"{self._supabase_url}/auth/v1/token?grant_type=refresh_token"
        headers = {"apikey": self._anon_key, "Content-Type": "application/json"}
        body    = {"refresh_token": self.refresh_token}

        log.info("Refreshing Supabase token (source=%s) …", self._cred_source)
        r = await self._client.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
        self.access_token  = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self._expires_at   = time.time() + data.get("expires_in", 3600)

        # Pick up user_id from the new token if we don't have it
        if not self._user_id and "user" in data:
            self._user_id = (data["user"] or {}).get("id")

        # Persist back so the renderer + future restarts see the same state.
        self._save_session_file()

        log.info("Token refreshed - expires in %d s", data.get("expires_in", 3600))
        return self.access_token

    @property
    def token(self) -> Optional[str]:
        return self.access_token

    def needs_refresh(self) -> bool:
        """True when the token has less than 5 minutes left."""
        return bool(self.access_token) and time.time() >= self._expires_at - 300

    async def ensure_valid_token(self) -> str:
        """Return a valid access token, refreshing it if necessary. If the
        session file changed under us (user signed in / out from another
        process), re-read it on the next call."""
        # Hot-reload session.json when its mtime changes
        if self._cred_source == "session_file" and SESSION_FILE.exists():
            try:
                disk_token = None
                with SESSION_FILE.open("r", encoding="utf-8") as fh:
                    disk_token = (json.load(fh) or {}).get("access_token")
                if disk_token and disk_token != self.access_token:
                    log.info("session.json updated externally — reloading")
                    self._load_session_file()
            except Exception:
                pass

        if not self.access_token:
            return await self.login()
        if self.needs_refresh():
            try:
                return await self.refresh()
            except Exception as e:
                log.warning("Token refresh failed (%s) — re-logging in", e)
                return await self.login()
        return self.access_token

    async def close(self) -> None:
        await self._client.aclose()


# ──────────────────────────────────────────────────────────────────────────────
# 2. LOCAL BACKEND CLIENT  (talks to localhost:8000)
# ──────────────────────────────────────────────────────────────────────────────
class LocalBackendClient:
    """
    Thin async wrapper around the local WATCH-DOG FastAPI backend.
    Handles login (JWT) and all bot operations.
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=LOCAL_API_URL,
            timeout=10.0,
            follow_redirects=True,   # FastAPI redirects /path → /path/
        )
        self._token: Optional[str] = None

    async def _login(self) -> None:
        """Obtain a JWT from the local backend (admin-login endpoint)."""
        try:
            r = await self._client.post(
                "/api/auth/admin-login",
                json={"password": LOCAL_API_PASS},
            )
            r.raise_for_status()
            self._token = r.json().get("access_token")
            log.debug("Local backend login OK")
        except Exception as e:
            log.warning("Local backend login skipped (%s) — proceeding without auth", e)
            self._token = None

    def _auth_headers(self) -> Dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    async def _get(self, path: str) -> Any:
        try:
            r = await self._client.get(path, headers=self._auth_headers())
            if r.status_code == 401:
                await self._login()
                r = await self._client.get(path, headers=self._auth_headers())
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Local GET %s failed: %s", path, e)
            return None

    async def _post(self, path: str, **kwargs: Any) -> Any:
        try:
            r = await self._client.post(path, headers=self._auth_headers(), **kwargs)
            if r.status_code == 401:
                await self._login()
                r = await self._client.post(path, headers=self._auth_headers(), **kwargs)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Local POST %s failed: %s", path, e)
            return None

    async def list_bots(self) -> list:
        """Return all bots as [{ id, name, status }, ...]."""
        data = await self._get("/api/bots")
        if data is None:
            return []
        # Normalize — local backend returns list of bot dicts
        bots = data if isinstance(data, list) else data.get("bots", [])
        return [
            {
                "id":     b.get("id"),
                "name":   b.get("name", f"Bot {b.get('id')}"),
                "status": b.get("status", "stopped"),
            }
            for b in bots
        ]

    async def start_bot(self, bot_id: int) -> bool:
        """Start a bot. Returns True on success."""
        result = await self._post(f"/api/bots/{bot_id}/start")
        return result is not None

    async def stop_bot(self, bot_id: int) -> bool:
        """Stop a bot. Returns True on success."""
        result = await self._post(f"/api/bots/{bot_id}/stop")
        return result is not None

    async def get_bot_status(self, bot_id: int) -> Optional[str]:
        """Fetch single bot status: 'running' | 'stopped' | 'error' | None."""
        data = await self._get(f"/api/bots/{bot_id}")
        if data is None:
            return None
        return data.get("status", "stopped")

    async def close(self) -> None:
        await self._client.aclose()


# ──────────────────────────────────────────────────────────────────────────────
# 3. CLOUD CONNECTOR  (WebSocket bridge between local ↔ cloud)
# ──────────────────────────────────────────────────────────────────────────────
class CloudConnector:
    """
    Maintains the WebSocket connection to wss://watchdogbot.cloud/ws
    with role=desktop.

    Incoming commands from the web dashboard:
      run          { bot_id }   → start bot on local backend
      stop         { bot_id }   → stop bot on local backend
      list_request {}           → respond with current bots_list

    Outgoing updates to the web dashboard:
      bots_list    [{ id, name, status }]
      status_update { bot_id, status, logs }
    """

    def __init__(self, auth: CloudAuth, local: LocalBackendClient) -> None:
        self._auth    = auth
        self._local   = local
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False

    # ── send helpers ──────────────────────────────────────────────────────────

    async def _send(self, msg: Dict[str, Any]) -> None:
        if self._ws:
            try:
                await self._ws.send(json.dumps(msg))
            except Exception as e:
                log.warning("WebSocket send failed: %s", e)

    async def _send_bots_list(self) -> None:
        bots = await self._local.list_bots()
        await self._send({"type": "bots_list", "data": bots})
        log.info("Sent bots_list (%d bots) to cloud dashboard", len(bots))

    async def _send_status_update(self, bot_id: int, status: str, logs: str = "") -> None:
        await self._send({
            "type": "status_update",
            "data": {"bot_id": bot_id, "status": status, "logs": logs},
        })
        log.info("Sent status_update bot_id=%d status=%s", bot_id, status)

    # ── command handlers ──────────────────────────────────────────────────────

    async def _handle_run(self, data: Dict[str, Any]) -> None:
        bot_id = data.get("bot_id")
        if bot_id is None:
            log.warning("run command missing bot_id")
            return
        log.info("Dashboard requested START for bot %s", bot_id)
        await self._send_status_update(bot_id, "starting")
        ok = await self._local.start_bot(int(bot_id))
        status = await self._local.get_bot_status(int(bot_id)) or ("running" if ok else "error")
        await self._send_status_update(bot_id, status,
                                       "" if ok else "Failed to start bot")

    async def _handle_stop(self, data: Dict[str, Any]) -> None:
        bot_id = data.get("bot_id")
        if bot_id is None:
            log.warning("stop command missing bot_id")
            return
        log.info("Dashboard requested STOP for bot %s", bot_id)
        await self._send_status_update(bot_id, "stopping")
        ok = await self._local.stop_bot(int(bot_id))
        status = await self._local.get_bot_status(int(bot_id)) or ("stopped" if ok else "error")
        await self._send_status_update(bot_id, status,
                                       "" if ok else "Failed to stop bot")

    async def _handle_list_request(self, _data: Dict[str, Any]) -> None:
        await self._send_bots_list()

    # ── generic HTTP tunnel ───────────────────────────────────────────────────
    #
    # Browser sends an arbitrary HTTP call addressed at the local FastAPI
    # backend. We replay it against http://localhost:8000 and return the
    # response. The web dashboard uses this for ANY API call (logs, config,
    # detail pages, etc.) so the same React codebase that runs in Electron
    # works unchanged in a browser.
    #
    # Allow-list paths to localhost API only — never proxy to arbitrary URLs.
    # ──────────────────────────────────────────────────────────────────────────

    _RPC_METHOD_WHITELIST = {"GET", "POST", "PUT", "PATCH", "DELETE"}

    async def _handle_rpc_request(self, msg: Dict[str, Any]) -> None:
        request_id = msg.get("request_id")
        method     = (msg.get("method") or "").upper()
        path       = msg.get("path") or ""
        body       = msg.get("body")

        # Empty request_id means we cannot correlate the response — reject.
        if not request_id:
            log.warning("rpc_request missing request_id, dropped")
            return

        async def _reply(status: int, data: Any = None, error: Optional[str] = None) -> None:
            await self._send({
                "type":       "rpc_response",
                "request_id": request_id,
                "status":     status,
                "data":       data,
                "error":      error,
            })

        # Validate
        if method not in self._RPC_METHOD_WHITELIST:
            return await _reply(405, error=f"Method not allowed: {method}")
        if not path.startswith("/"):
            return await _reply(400, error=f"Path must start with /: {path!r}")
        # Defense-in-depth: reject scheme/host injection in the path
        if "://" in path or path.startswith("//"):
            return await _reply(400, error="Path must be relative to local backend")

        log.info("RPC %s %s (req=%s)", method, path, request_id[:8] if request_id else "?")

        try:
            r = await self._local._client.request(
                method, path,
                headers=self._local._auth_headers(),
                json=body if body is not None else None,
            )
            # Reauth on 401 once
            if r.status_code == 401:
                await self._local._login()
                r = await self._local._client.request(
                    method, path,
                    headers=self._local._auth_headers(),
                    json=body if body is not None else None,
                )

            # Try JSON, fall back to text body
            try:
                data = r.json()
            except Exception:
                data = {"_text": r.text}

            return await _reply(r.status_code, data=data,
                                error=None if r.is_success else (data.get("detail") or data.get("error") if isinstance(data, dict) else None))
        except httpx.RequestError as e:
            log.error("RPC %s %s — local backend unreachable: %s", method, path, e)
            return await _reply(502, error=f"Local backend unreachable: {e}")
        except Exception as e:
            log.exception("RPC %s %s — unhandled error", method, path)
            return await _reply(500, error=str(e))

    # ── message dispatcher ────────────────────────────────────────────────────

    async def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Received non-JSON message: %r", raw[:200])
            return

        msg_type = msg.get("type", "")
        data     = msg.get("data", {})
        log.debug("← [cloud] %s", msg_type)

        if msg_type == "run":
            await self._handle_run(data)
        elif msg_type == "stop":
            await self._handle_stop(data)
        elif msg_type == "list_request":
            await self._handle_list_request(data)
        elif msg_type == "rpc_request":
            await self._handle_rpc_request(msg)
        elif msg_type in ("subscribe", "unsubscribe"):
            # Phase 3 — placeholder until live event streams ship
            log.debug("subscribe/unsubscribe received (Phase 3 not implemented yet)")
        else:
            log.debug("Ignored unknown message type: %r", msg_type)

    # ── token refresh loop ────────────────────────────────────────────────────

    async def _token_refresh_loop(self) -> None:
        """Background task: proactively refresh Supabase token before expiry."""
        while self._running:
            await asyncio.sleep(60)           # check every minute
            if self._auth.needs_refresh():
                try:
                    await self._auth.ensure_valid_token()
                    log.info("Token refreshed proactively")
                except Exception as e:
                    log.error("Proactive token refresh failed: %s", e)

    # ── status poll loop (optional, sends updates every 30 s) ─────────────────

    async def _status_poll_loop(self) -> None:
        """
        Every 30 seconds re-fetch local bot states and push to dashboard.
        This keeps the dashboard in sync even when bots change state
        without an explicit run/stop command.
        """
        await asyncio.sleep(30)               # don't spam on startup
        while self._running:
            await self._send_bots_list()
            await asyncio.sleep(30)

    # ── main connect/receive loop ─────────────────────────────────────────────

    async def _connect_once(self) -> None:
        """
        Establish one WebSocket connection, do initial handshake,
        then receive messages until disconnected.
        """
        token = await self._auth.ensure_valid_token()
        ws_url = f"{CLOUD_WS_URL}?role=desktop&token={token}"

        extra_headers = {"Authorization": f"Bearer {token}"}

        log.info("Connecting to %s …", CLOUD_WS_URL)
        async with websockets.connect(
            ws_url,
            additional_headers=extra_headers,
            ping_interval=25,      # keep-alive (server also pings every 30 s)
            ping_timeout=15,
            close_timeout=10,
        ) as ws:
            self._ws = ws
            log.info("✅ Connected to cloud dashboard relay")

            # Send initial state immediately
            await self._send_bots_list()

            # Start background tasks
            refresh_task = asyncio.create_task(self._token_refresh_loop())
            poll_task    = asyncio.create_task(self._status_poll_loop())

            try:
                async for raw in ws:
                    await self._dispatch(raw)
            except websockets.exceptions.ConnectionClosedOK:
                log.info("WebSocket closed normally")
            except websockets.exceptions.ConnectionClosedError as e:
                log.warning("WebSocket closed with error: %s", e)
            finally:
                self._ws = None
                refresh_task.cancel()
                poll_task.cancel()
                try:
                    await asyncio.gather(refresh_task, poll_task,
                                         return_exceptions=True)
                except Exception:
                    pass

    async def run(self) -> None:
        """
        Main loop: connect, reconnect on failure with exponential back-off.
        Runs forever until self._running is set to False.
        """
        self._running = True
        delay = _RECONNECT_BASE_S
        log.info("Cloud connector starting — target: %s", CLOUD_WS_URL)

        while self._running:
            try:
                await self._connect_once()
                delay = _RECONNECT_BASE_S     # reset back-off on clean disconnect
            except Exception as e:
                log.error("Connection failed: %s", e)

            if not self._running:
                break

            log.info("Reconnecting in %.0f s …", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX_S)

    def stop(self) -> None:
        self._running = False
        if self._ws:
            asyncio.create_task(self._ws.close())


# ──────────────────────────────────────────────────────────────────────────────
# 4. REST API HELPERS  (call cloud endpoints from desktop)
# ──────────────────────────────────────────────────────────────────────────────
class CloudApiClient:
    """
    Optional: call cloud REST endpoints authenticated as the current user.

    Usage:
        api = CloudApiClient(auth)
        profile = await api.get_profile()
        status  = await api.get_subscription_status()
    """

    def __init__(self, auth: CloudAuth) -> None:
        self._auth = auth
        self._client = httpx.AsyncClient(base_url=CLOUD_API_URL, timeout=15.0)

    async def _headers(self) -> Dict[str, str]:
        token = await self._auth.ensure_valid_token()
        return {"Authorization": f"Bearer {token}"}

    async def get_profile(self) -> Optional[Dict]:
        """GET /api/user/profile — returns user profile dict."""
        try:
            r = await self._client.get("/api/user/profile",
                                       headers=await self._headers())
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("get_profile failed: %s", e)
            return None

    async def get_subscription_status(self) -> Optional[str]:
        """GET /api/subscription/status — returns subscription_status string."""
        try:
            r = await self._client.get("/api/subscription/status",
                                       headers=await self._headers())
            r.raise_for_status()
            return r.json().get("subscription_status")
        except Exception as e:
            log.error("get_subscription_status failed: %s", e)
            return None

    async def get_me(self) -> Optional[Dict]:
        """GET /api/auth/me — returns authenticated Supabase user."""
        try:
            r = await self._client.get("/api/auth/me",
                                       headers=await self._headers())
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("get_me failed: %s", e)
            return None

    async def check_free_slot(self) -> Dict:
        """GET /api/subscription/check-free-slot — no auth required."""
        try:
            r = await self._client.get("/api/subscription/check-free-slot")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("check_free_slot failed: %s", e)
            return {}

    async def close(self) -> None:
        await self._client.aclose()


# ──────────────────────────────────────────────────────────────────────────────
# 5. SUBSCRIPTION GATE  (refuse to run if no active subscription)
# ──────────────────────────────────────────────────────────────────────────────
async def _check_subscription(api: CloudApiClient) -> bool:
    """
    Returns True if the user has an active or free_trial subscription.
    Prints a clear error and returns False otherwise.
    """
    status = await api.get_subscription_status()
    log.info("Subscription status: %s", status)
    if status in ("active", "free_trial"):
        return True
    # Plain ASCII — runs reliably under Windows cp1252 even before
    # PYTHONIOENCODING=utf-8 takes effect.
    print("\n" + "-" * 60)
    print("  [X]  No active subscription")
    print(f"     Status: {status}")
    print(f"     Visit {CLOUD_API_URL} to subscribe or claim your free trial.")
    print("-" * 60 + "\n")
    return False


# ──────────────────────────────────────────────────────────────────────────────
# 6. ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────
async def _main() -> None:
    if not CLOUD_EMAIL or not CLOUD_PASSWORD:
        print("\n[X] CLOUD_EMAIL and CLOUD_PASSWORD must be set in your .env file.")
        print("    Add them and re-run.\n")
        sys.exit(1)

    auth  = CloudAuth()
    local = LocalBackendClient()
    api   = CloudApiClient(auth)

    try:
        # Login
        await auth.login()

        # Verify profile is reachable
        me = await api.get_me()
        if me:
            log.info("Logged in as: %s", me.get("email", "unknown"))

        # Subscription gate — comment out if you want to bypass
        if not await _check_subscription(api):
            sys.exit(1)

        # Attempt local backend login (non-fatal if local is offline)
        try:
            await local._login()
            bots = await local.list_bots()
            log.info("Local backend: found %d bots", len(bots))
        except Exception as e:
            log.warning("Local backend not reachable: %s", e)

        # Start the connector
        connector = CloudConnector(auth, local)

        def _handle_shutdown():
            log.info("Shutdown signal received")
            connector.stop()

        loop = asyncio.get_running_loop()
        for sig in ("SIGINT", "SIGTERM"):
            try:
                import signal
                loop.add_signal_handler(
                    getattr(signal, sig), _handle_shutdown
                )
            except (ImportError, NotImplementedError, AttributeError):
                pass    # Windows: handle via KeyboardInterrupt

        await connector.run()

    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")
    finally:
        await auth.close()
        await local.close()
        await api.close()
        log.info("Cloud connector stopped")


if __name__ == "__main__":
    asyncio.run(_main())
