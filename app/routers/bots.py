from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
import asyncio
import subprocess
import sys
import os
import re
import shutil
import tempfile
import threading
import difflib
from datetime import datetime, timezone

from ..database import get_db, SessionLocal
from ..bot_manager import get_bot as _bot_fs  # ← per-bot filesystem isolation (aliased to avoid shadowing the GET /bots/{id} endpoint)

_SDK_DIR    = os.path.join(os.path.dirname(__file__), '..', '..', 'sdk')
_ANSI_RE    = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-9;?]*[ -/]*[@-~])')
from .. import models, schemas, cloud_client
# Cloud-aware auth dep — for Supabase-authenticated callers, this also runs a
# one-shot cloud→local sync the first time the user is seen by this process.
# Falls back to get_default_user when no Bearer token is supplied, so legacy
# sessions (Whop license, no Supabase login) keep working unchanged.
from ..auth import get_current_user_cloud as get_current_user, _bearer_from_request


# ── Cloud sync helpers ───────────────────────────────────────────────────────
# Bots router CRUD writes to local SQLite as the source of truth, then fires a
# fire-and-forget cloud mirror via FastAPI's BackgroundTasks. Failures are
# logged but never block the response. The next authenticated request from
# any device will re-sync the divergence.

def _is_cloud_user(user) -> bool:
    return bool(getattr(user, "supabase_uid", None)) and cloud_client.is_configured()

def _bg_run(coro):
    """BackgroundTasks expects a callable (not a coroutine). Wrap an async
    coroutine into a sync callable that runs it in a fresh event loop."""
    def _runner():
        try:
            asyncio.run(coro)
        except Exception as e:
            print(f"[cloud] background task failed: {e}")
    return _runner

async def _cloud_insert_and_stamp(token: str, supabase_uid: str, bot_id: int):
    """Insert local bot into cloud, then write the returned cloud_id back.
    Runs in a fresh DB session because BackgroundTasks executes after the
    response is sent — the request session is already closed."""
    db = SessionLocal()
    try:
        bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
        if not bot:
            return
        cloud_row = await cloud_client.insert_bot(token, supabase_uid, bot)
        if cloud_row and cloud_row.get("id"):
            bot.cloud_id = cloud_row["id"]
            bot.cloud_synced_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()

async def _cloud_update(token: str, cloud_id: str, bot_id: int):
    db = SessionLocal()
    try:
        bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
        if not bot:
            return
        ok = await cloud_client.update_bot(token, cloud_id, bot)
        if ok:
            bot.cloud_synced_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()

async def _cloud_delete(token: str, cloud_id: str):
    await cloud_client.delete_bot(token, cloud_id)

# Hostname is captured once at import — used by patch_bot_runtime so the
# website fleet panel can show "running on Sara's laptop".
import socket as _socket
_HOSTNAME = _socket.gethostname()

async def _cloud_runtime(token: str, cloud_id: str, is_running: bool):
    """Heartbeat one bot's runtime state to the cloud (is_running + last_seen
    + running_on). Fire-and-forget; failures are logged inside cloud_client."""
    await cloud_client.patch_bot_runtime(
        token, cloud_id,
        is_running=is_running,
        running_on=_HOSTNAME if is_running else None,
    )

router = APIRouter(prefix="/api/bots", tags=["bots"])

# In-memory process registry: {bot_id: subprocess.Popen}
_processes: dict = {}


class BotRunIn(BaseModel):
    """Request body for POST /{bot_id}/run"""
    demo_mode: bool = False


def _env_prefix(name: str) -> str:
    """Turn a connection name into a safe env-var prefix, e.g. 'Kalshi API' → 'KALSHI_API'."""
    return re.sub(r'[^A-Z0-9]+', '_', name.upper()).strip('_')


def _build_env(bot_id: int) -> dict:
    """Return os.environ copy extended with this bot's settings, API keys, and watchdog meta."""
    db = SessionLocal()
    env = os.environ.copy()
    env['PYTHONUNBUFFERED']  = '1'
    env['PYTHONIOENCODING']  = 'utf-8'
    sdk_path      = os.path.abspath(_SDK_DIR)
    watchdog_path = "C:/WATCH-DOG/app/backend"   # allows 'from training_logger import ...' and 'from pattern_analyzer import ...'
    env['PYTHONPATH'] = (
        sdk_path + os.pathsep +
        watchdog_path + os.pathsep +
        env.get('PYTHONPATH', '')
    )
    try:
        bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
        if bot:
            env["WATCHDOG_API_URL"]    = os.getenv("WATCHDOG_API_URL", "http://localhost:8000")
            env["WATCHDOG_BOT_SECRET"] = bot.bot_secret or ""
            env["WATCHDOG_BOT_ID"]     = str(bot_id)
            # Sanitized bot name for training data folder naming (strip trailing _bot / _trade)
            _bname = re.sub(r'[^a-z0-9]+', '_', (bot.name or "bot").lower()).strip('_')
            _bname = re.sub(r'_(bot|trade|trader)$', '', _bname)
            env["WATCHDOG_BOT_NAME"] = _bname
            # ── Isolated bot folder — bot code can use this to read/write its own data ──
            bfs = _bot_fs(bot_id, bot.name)
            env["WATCHDOG_BOT_DIR"]          = str(bfs.root)
            env["WATCHDOG_BOT_LOGS_DIR"]     = str(bfs.logs_dir)
            env["WATCHDOG_BOT_TRAINING_DIR"] = str(bfs.training_dir)
            # Risk management settings — readable by bot code
            if bot.max_amount_per_trade is not None:
                env["WATCHDOG_MAX_AMOUNT_PER_TRADE"]    = str(bot.max_amount_per_trade)
            if bot.max_contracts_per_trade is not None:
                env["WATCHDOG_MAX_CONTRACTS_PER_TRADE"] = str(bot.max_contracts_per_trade)
            if bot.max_daily_loss is not None:
                env["WATCHDOG_MAX_DAILY_LOSS"]          = str(bot.max_daily_loss)

        # Load bot-specific connections first, then fall back to user-level (bot_id IS NULL)
        bot_user_id = bot.user_id if bot else None
        conns = (db.query(models.ApiConnection)
                 .filter(
                     (models.ApiConnection.bot_id == bot_id) |
                     ((models.ApiConnection.bot_id == None) &
                      (models.ApiConnection.user_id == bot_user_id)),
                     models.ApiConnection.is_active == True,
                 )
                 .all())
        for c in conns:
            prefix = _env_prefix(c.name)
            if c.api_key:
                env[f"{prefix}_KEY"] = c.api_key
                env[prefix] = c.api_key        # also set the prefix itself directly
            if c.api_secret:
                env[f"{prefix}_SECRET"] = c.api_secret
            if c.base_url:
                env[f"{prefix}_URL"] = c.base_url
    finally:
        db.close()
    return env


# ─────────────────────────────────────────────────────────────────────────────
# AI Lab training-data sync — parses bot stdout for trade events
# ─────────────────────────────────────────────────────────────────────────────

class _TrainSync:
    """
    Parses bot stdout line-by-line and writes training data via TrainingLogger.
    One instance per bot run — lives inside _run_once().
    All exceptions are silently swallowed so sync never affects the bot.
    """

    # ── Compiled patterns ────────────────────────────────────────────────────
    _RE_SESSION = re.compile(r'\[SESSION\] New session\s*:\s*(\S+)', re.IGNORECASE)
    # Bought 5 YES @ 45c [KXBTC15M-26APR201915-15]
    _RE_BUY     = re.compile(
        r'Bought (\d+) (YES|NO) @ (\d+)c \[([^\]]+)\]', re.IGNORECASE)
    # [EXIT] yes x5  entry=45¢  exit=62¢  PnL=+$0.85  held=120s  [take_profit]
    _RE_EXIT    = re.compile(
        r'\[EXIT\] (yes|no) x(\d+)\s+entry=(\d+).*?exit=(\d+).*?PnL=([^\s]+).*?\[([^\]]+)\]',
        re.IGNORECASE)
    # [DRY-RUN] BUY KXBTC15M YES x5 @ 45c
    _RE_DR_BUY  = re.compile(
        r'\[DRY-RUN\] BUY (\S+) (YES|NO) x(\d+) @ (\d+)c', re.IGNORECASE)
    # [DRY-RUN] SELL KXBTC15M YES x5 @ 62c
    _RE_DR_SELL = re.compile(
        r'\[DRY-RUN\] SELL (\S+) (YES|NO) x(\d+) @ (\d+)c', re.IGNORECASE)

    def __init__(self, bot_id: int, bot_name: str):
        self.bot_id   = bot_id
        self.bot_name = bot_name   # already sanitized (safe for folder name)
        self._tlog    = None       # TrainingLogger — lazy-init on first session
        self._ticker  = None

    # ── Public API ────────────────────────────────────────────────────────────

    def feed(self, line: str):
        """Process one cleaned stdout line. Never raises."""
        try:
            self._process(line)
        except Exception:
            pass

    def close(self):
        """Call when the bot process exits — flushes the open session."""
        try:
            if self._tlog:
                self._tlog.end_session(final_yes=50)
        except Exception:
            pass

    # ── Internals ─────────────────────────────────────────────────────────────

    def _get_tlog(self):
        """Lazy-init TrainingLogger — only imports when first trade is seen."""
        if self._tlog is not None:
            return self._tlog
        try:
            _bd = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
            import sys as _sys
            if _bd not in _sys.path:
                _sys.path.insert(0, _bd)
            from training_logger import TrainingLogger
            self._tlog = TrainingLogger(
                bot_id   = self.bot_id,
                bot_name = self.bot_name,
            )
        except Exception as _e:
            print(f"[WATCHDOG] TrainingLogger init failed for bot {self.bot_id}: {_e}")
        return self._tlog

    @staticmethod
    def _pnl(pnl_str: str) -> float:
        """'+$0.85' / '-$1.20' → float (positive = profit, negative = loss)."""
        try:
            return float(re.sub(r'[+$]', '', pnl_str))
        except Exception:
            return 0.0

    def _notify(self, side: str, contracts: int, price: float,
                pnl, trade_type: str, ticker: str):
        """Fire ai_models.notify_trade() for live-sync AI Lab models."""
        try:
            from .ai_models import notify_trade as _nt
            _nt(self.bot_id, {
                "trade_type": trade_type,
                "side":       side,
                "contracts":  contracts,
                "price":      price,
                "pnl":        pnl,
                "ticker":     ticker or self._ticker or "",
            })
        except Exception:
            pass

    def _process(self, line: str):
        # ── New session ───────────────────────────────────────────────────────
        m = self._RE_SESSION.search(line)
        if m:
            self._ticker = m.group(1)
            tlog = self._get_tlog()
            if tlog:
                # End any previous open session cleanly before starting a new one
                try:
                    tlog.end_session(final_yes=50)
                except Exception:
                    pass
                tlog.start_session(ticker=self._ticker, open_yes=50)
            return

        # ── BUY entry (real order) ────────────────────────────────────────────
        m = self._RE_BUY.search(line)
        if m:
            contracts = int(m.group(1))
            side      = m.group(2).lower()
            price     = int(m.group(3))
            ticker    = m.group(4)
            tlog = self._get_tlog()
            if tlog:
                tlog.log_trade("buy", side, contracts, price, pnl=0, reason="entry")
            self._notify(side, contracts, price, pnl=None,
                         trade_type="BUY", ticker=ticker)
            return

        # ── EXIT / close (real order) — has reason + full PnL ────────────────
        m = self._RE_EXIT.search(line)
        if m:
            side      = m.group(1).lower()
            contracts = int(m.group(2))
            # entry_price = int(m.group(3))  # available but not needed here
            exit_price = int(m.group(4))
            pnl        = self._pnl(m.group(5))
            reason     = m.group(6)
            tlog = self._get_tlog()
            if tlog:
                event = "stop_loss" if "stop" in reason.lower() else "sell"
                tlog.log_trade(event, side, contracts, exit_price,
                               pnl=pnl, reason=reason)
            self._notify(side, contracts, exit_price, pnl=pnl,
                         trade_type="SELL", ticker=self._ticker or "")
            return

        # ── DRY-RUN BUY ───────────────────────────────────────────────────────
        m = self._RE_DR_BUY.search(line)
        if m:
            ticker    = m.group(1)
            side      = m.group(2).lower()
            contracts = int(m.group(3))
            price     = int(m.group(4))
            tlog = self._get_tlog()
            if tlog:
                tlog.log_trade("buy", side, contracts, price, pnl=0, reason="dry_run")
            self._notify(side, contracts, price, pnl=None,
                         trade_type="DRY_BUY", ticker=ticker)
            return

        # ── DRY-RUN SELL ──────────────────────────────────────────────────────
        m = self._RE_DR_SELL.search(line)
        if m:
            ticker    = m.group(1)
            side      = m.group(2).lower()
            contracts = int(m.group(3))
            price     = int(m.group(4))
            tlog = self._get_tlog()
            if tlog:
                tlog.log_trade("sell", side, contracts, price, pnl=0,
                               reason="dry_run_sell")
            self._notify(side, contracts, price, pnl=0,
                         trade_type="DRY_SELL", ticker=ticker)


def _run_once(bot_id: int, tmp_path: str, user_id: int, db, bfs,
              bot_name: str = "", demo_mode: bool = False) -> int:
    """
    Run the bot script once, stream logs to DB + bot folder, return exit code.
    bfs: BotFS instance for this bot (filesystem isolation).
    demo_mode=True → sets DRY_RUN=1 in the subprocess environment so the bot
                      never places real orders regardless of its internal config.
    Each bot runs in its own thread — exceptions here never affect other bots.
    """
    # On Windows isolate the subprocess from the parent console group so that
    # uvicorn --reload CTRL+C events don't propagate to the bot process.
    _popen_kwargs: dict = {}
    if sys.platform == "win32":
        _popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        _env = _build_env(bot_id)
        # ── Demo / Live mode enforcement ──────────────────────────────────────
        # demo_mode=True forces DRY_RUN=1 so the bot never places real orders.
        # This overrides any DRY_RUN value already present in the environment.
        _env["DRY_RUN"] = "1" if demo_mode else "0"

        # Launch via wd_runner.py so HTTP/WS auto-logging hooks are installed
        # before the bot's code runs. The runner pre-imports wd_autolog (which
        # monkey-patches requests/httpx/websocket-client) and then runpy's the
        # user's code.py as __main__. Bot authors don't have to add any logging
        # — every API call and websocket frame is captured automatically.
        _wd_runner = os.path.join(_SDK_DIR, 'wd_runner.py')
        process = subprocess.Popen(
            [sys.executable, '-u', _wd_runner, tmp_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            env=_env,
            **_popen_kwargs,
        )
    except Exception as exc:
        err = f"[WATCHDOG] Failed to launch bot process: {exc}"
        try:
            db.add(models.BotLog(bot_id=bot_id, user_id=user_id, level=models.LogLevel.ERROR, message=err))
            db.commit()
        except Exception:
            pass
        if bfs:
            bfs.append_log("ERROR", err)
        return -1

    _processes[bot_id] = process

    # ── AI Lab training-data sync ─────────────────────────────────────────────
    sync = _TrainSync(bot_id, bot_name)

    try:
        for raw in process.stdout:
            line = _ANSI_RE.sub('', raw.rstrip())
            if not line:
                continue
            lower = line.lower()
            first = line.split(']')[0].lstrip('[').upper() if line.startswith('[') else ''
            if first in ('ERROR', 'EXCEPTION', 'FATAL') or any(w in lower for w in ('traceback', 'exception', 'error')):
                level = models.LogLevel.ERROR
            elif first == 'WARNING' or 'warning' in lower or 'warn' in lower:
                level = models.LogLevel.WARNING
            else:
                level = models.LogLevel.INFO
            try:
                # ── DB (primary — drives the API) ─────────────────────────────
                db.add(models.BotLog(bot_id=bot_id, user_id=user_id, level=level, message=line))
                db.commit()
            except Exception as db_exc:
                # DB write failure must not kill log streaming for this bot
                pass
            try:
                # ── Bot folder (secondary — isolation / portability) ───────────
                bfs.append_log(level.value, line)
            except Exception:
                pass
            # ── AI Lab sync (parse trade events, write training data) ──────────
            sync.feed(line)
    except Exception as stream_exc:
        # Unexpected error reading stdout — log and fall through to process.wait()
        err = f"[WATCHDOG] Log stream error for bot {bot_id}: {stream_exc}"
        try:
            db.add(models.BotLog(bot_id=bot_id, user_id=user_id, level=models.LogLevel.ERROR, message=err))
            db.commit()
        except Exception:
            pass
    finally:
        # Flush any open training session before the process exits
        sync.close()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()

    return process.returncode


def _execute(bot_id: int, code: str, user_id: int, demo_mode: bool = False):
    db = SessionLocal()
    tmp_path = None
    bfs = None   # BotFS — set once we know the bot name
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp_path = f.name
        bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
        if bot:
            # ── Create isolated BotFS instance for this run ──────────────────
            bfs = _bot_fs(bot_id, bot.name)
            if not bfs.exists():
                # Backfill folder if bot was created before bot_manager existed
                bfs.create(code=code, description=bot.description or "")
            else:
                bfs.sync_code(code)   # ensure code.py is always current

            bot.status = models.BotStatus.RUNNING
            bot.last_run_at = datetime.now(timezone.utc)
            bot.run_count = (bot.run_count or 0) + 1
            db.commit()
            bfs.sync_status("RUNNING")

        # ── Sanitized bot name for training data folder matching ──────────────
        _raw_name = bot.name if bot else "bot"
        _bname = re.sub(r'[^a-z0-9]+', '_', _raw_name.lower()).strip('_')
        _bname = re.sub(r'_(bot|trade|trader)$', '', _bname) or "bot"

        restart_count = 0
        while True:
            rc = _run_once(bot_id, tmp_path, user_id, db, bfs, bot_name=_bname, demo_mode=demo_mode)

            # Check if manually stopped
            bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
            if not bot or bot.status == models.BotStatus.STOPPED:
                if bfs:
                    bfs.sync_status("STOPPED")
                break

            exit_msg = f"[WATCHDOG] Process exited with code {rc}"
            exit_level = models.LogLevel.INFO if rc == 0 else models.LogLevel.ERROR
            db.add(models.BotLog(bot_id=bot_id, user_id=user_id, level=exit_level, message=exit_msg))
            db.commit()
            if bfs:
                bfs.append_log(exit_level.value, exit_msg)

            # Auto-restart on crash (rc != 0) if enabled
            if rc != 0 and bot and bot.auto_restart and (bot_id in _processes):
                restart_count += 1
                restart_msg = f"[WATCHDOG] Auto-restarting (attempt #{restart_count})..."
                db.add(models.BotLog(bot_id=bot_id, user_id=user_id,
                                     level=models.LogLevel.WARNING, message=restart_msg))
                db.commit()
                if bfs:
                    bfs.append_log("WARNING", restart_msg)
                bot.last_run_at = datetime.now(timezone.utc)
                bot.run_count = (bot.run_count or 0) + 1
                db.commit()
                continue

            # Normal exit or auto_restart disabled
            if bot:
                final_status = models.BotStatus.IDLE if rc == 0 else models.BotStatus.ERROR
                bot.status = final_status
                db.commit()
                if bfs:
                    bfs.sync_status(final_status.value)
            break

    except Exception as exc:
        import traceback as _tb, sys as _sys
        full_tb = _tb.format_exc()
        err_msg = f"[WATCHDOG] Runner error: {exc}\n{full_tb}"
        _sys.stderr.write(f"\n=== BOT {bot_id} CRASH TRACEBACK ===\n{full_tb}\n===\n")
        try:
            db.rollback()
            db.add(models.BotLog(bot_id=bot_id, user_id=user_id,
                                 level=models.LogLevel.ERROR, message=err_msg[:2000]))
            db.commit()
        except Exception:
            pass
        try:
            bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
            if bot:
                bot.status = models.BotStatus.ERROR
                db.commit()
        except Exception:
            pass
        if bfs:
            bfs.append_log("ERROR", err_msg[:2000])
            bfs.sync_status("ERROR")
    finally:
        db.close()
        _processes.pop(bot_id, None)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@router.get("/", response_model=List[schemas.BotOut])
def list_bots(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return (db.query(models.Bot)
            .filter(models.Bot.user_id == user.id)
            .order_by(models.Bot.created_at.desc())
            .all())


@router.post("/", response_model=schemas.BotOut, status_code=201)
def create_bot(
    data: schemas.BotCreate,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    bot = models.Bot(user_id=user.id, **data.model_dump())
    db.add(bot)
    db.commit()
    db.refresh(bot)
    # ── Create isolated folder for this bot ───────────────────────────────────
    _bot_fs(bot.id, bot.name).create(
        code=bot.code,
        description=bot.description or "",
    )
    # ── Create AI Lab training data folder structure ──────────────────────────
    # WATCHDOG_DATA_DIR is set by run_backend.py in the bundled exe so
    # writes go to %LOCALAPPDATA%\WatchDog\training_data, not under
    # Program Files (denied). Falls back to repo-relative in dev mode.
    _td_root = os.path.join(
        os.environ.get("WATCHDOG_DATA_DIR") or os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '..')),
        'training_data')
    _safe_name = re.sub(r'[^a-z0-9_]', '_', bot.name.lower())
    _bot_td = os.path.join(_td_root, f"bot_{bot.id}_{_safe_name}")
    for _sub in ["ticks", "trades", "sessions", "documents", "ai_decisions", "logs"]:
        os.makedirs(os.path.join(_bot_td, _sub), exist_ok=True)
    # Cloud mirror happens via sync_engine (background thread, polls every 5s).
    # No per-request push — that path was unreliable (no retry, silently
    # failed when token validation 403'd, depended on supabase_uid being set).
    return bot


@router.get("/{bot_id}", response_model=schemas.BotOut)
def get_bot(bot_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id, models.Bot.user_id == user.id).first()
    if not bot:
        raise HTTPException(404, "Bot not found")
    return bot


@router.put("/{bot_id}", response_model=schemas.BotOut)
def update_bot(
    bot_id: int,
    data: schemas.BotUpdate,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id, models.Bot.user_id == user.id).first()
    if not bot:
        raise HTTPException(404, "Bot not found")
    fields = data.model_dump(exclude_none=True)
    for k, v in fields.items():
        setattr(bot, k, v)
    db.commit()
    db.refresh(bot)
    # ── Mirror changes to bot folder ──────────────────────────────────────────
    bfs = _bot_fs(bot.id, bot.name)
    if not bfs.exists():
        bfs.create(code=bot.code, description=bot.description or "")
    else:
        if "code" in fields:
            bfs.sync_code(fields["code"])
        bfs.sync_settings(
            schedule_type           = fields.get("schedule_type"),
            schedule_start          = fields.get("schedule_start"),
            schedule_end            = fields.get("schedule_end"),
            max_amount_per_trade    = fields.get("max_amount_per_trade"),
            max_contracts_per_trade = fields.get("max_contracts_per_trade"),
            max_daily_loss          = fields.get("max_daily_loss"),
            auto_restart            = fields.get("auto_restart"),
        )
    # Cloud mirror happens via sync_engine (background thread).
    return bot


@router.delete("/{bot_id}", status_code=204)
def delete_bot(
    bot_id: int,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id, models.Bot.user_id == user.id).first()
    if not bot:
        raise HTTPException(404, "Bot not found")
    cloud_id_to_delete = bot.cloud_id   # capture before db.delete invalidates the row
    if bot_id in _processes:
        _processes[bot_id].terminate()
        _processes.pop(bot_id, None)
    # ── Delete the bot's isolated folder before removing from DB ─────────────
    _bot_fs(bot_id, bot.name).delete()
    # ── Delete the bot's AI Lab training data folder (all matching bot_{id}_*) ─
    # WATCHDOG_DATA_DIR is set by run_backend.py in the bundled exe so
    # writes go to %LOCALAPPDATA%\WatchDog\training_data, not under
    # Program Files (denied). Falls back to repo-relative in dev mode.
    _td_root = os.path.join(
        os.environ.get("WATCHDOG_DATA_DIR") or os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '..')),
        'training_data')
    if os.path.isdir(_td_root):
        for _entry in os.scandir(_td_root):
            if _entry.is_dir() and _entry.name.startswith(f"bot_{bot_id}_"):
                shutil.rmtree(_entry.path, ignore_errors=True)
    db.delete(bot)
    db.commit()
    # Cloud delete propagates via sync_engine (it sees the local row vanish
    # and removes the corresponding cloud row on the next cycle, OR — if the
    # cloud row vanished first — the local row is already gone). See
    # sync_engine._run_one_cycle DELETE PROPAGATION block.
    # Note: with the sync_engine architecture, true bidirectional delete
    # propagation requires DELETE to also push to cloud. We do it here:
    if cloud_id_to_delete:
        try:
            from .. import cloud_client
            import asyncio
            # Sync delete (block briefly) so the cloud row goes away before
            # the engine's next cycle would re-create it from cloud → local.
            from ..auth import _bearer_from_request as _bft
            tok = _bft(request)
            if tok:
                asyncio.run(cloud_client.delete_bot(tok, cloud_id_to_delete))
        except Exception as e:
            print(f"[bots/delete] cloud delete fire-and-forget failed (sync engine will reconcile): {e}")


@router.post("/{bot_id}/run")
def run_bot(
    bot_id: int,
    body: BotRunIn = BotRunIn(),
    request: Request = None,
    background: BackgroundTasks = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id, models.Bot.user_id == user.id).first()
    if not bot:
        raise HTTPException(404, "Bot not found")
    if bot.status == models.BotStatus.RUNNING:
        raise HTTPException(400, "Bot is already running")
    threading.Thread(
        target=_execute,
        args=(bot_id, bot.code, user.id, body.demo_mode),
        daemon=True,
    ).start()
    # H1: cloud heartbeat — flip is_running=true so the website fleet panel
    # shows "running on <hostname>" across devices.
    if _is_cloud_user(user) and bot.cloud_id and request is not None and background is not None:
        token = _bearer_from_request(request)
        if token:
            background.add_task(_bg_run(_cloud_runtime(token, bot.cloud_id, True)))
    return {"message": "Bot started", "bot_id": bot_id, "demo_mode": body.demo_mode}


@router.post("/{bot_id}/stop")
def stop_bot(
    bot_id: int,
    request: Request = None,
    background: BackgroundTasks = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id, models.Bot.user_id == user.id).first()
    if not bot:
        raise HTTPException(404, "Bot not found")
    bot.status = models.BotStatus.STOPPED
    db.commit()
    if bot_id in _processes:
        proc = _processes.pop(bot_id, None)
        if proc:
            try:
                # On Windows, bot runs in its own process group (CREATE_NEW_PROCESS_GROUP).
                # Use CTRL_BREAK_EVENT to signal it instead of terminate() which is
                # ignored by processes in a separate group.
                if sys.platform == "win32":
                    import signal as _sig
                    proc.send_signal(_sig.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    # H1: cloud heartbeat — flip is_running=false.
    if _is_cloud_user(user) and bot.cloud_id and request is not None and background is not None:
        token = _bearer_from_request(request)
        if token:
            background.add_task(_bg_run(_cloud_runtime(token, bot.cloud_id, False)))
    return {"message": "Bot stopped"}


@router.get("/{bot_id}/logs", response_model=List[schemas.BotLogOut])
def get_bot_logs(
    bot_id: int,
    limit: int = 200,
    since_id: int = 0,          # if > 0: return only logs with id > since_id (ascending, new lines only)
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id, models.Bot.user_id == user.id).first()
    if not bot:
        raise HTTPException(404, "Bot not found")

    q = db.query(models.BotLog).filter(models.BotLog.bot_id == bot_id)
    if since_id > 0:
        # Incremental fetch: return only new lines in chronological order
        return (q
                .filter(models.BotLog.id > since_id)
                .order_by(models.BotLog.created_at.asc())
                .limit(limit)
                .all())
    # Initial full fetch: most-recent first (frontend reverses for display)
    return (q
            .order_by(models.BotLog.created_at.desc())
            .limit(limit)
            .all())


@router.delete("/{bot_id}/logs", status_code=204)
def clear_bot_logs(bot_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id, models.Bot.user_id == user.id).first()
    if not bot:
        raise HTTPException(404, "Bot not found")
    db.query(models.BotLog).filter(models.BotLog.bot_id == bot_id).delete()
    db.commit()
    # ── Also clear the bot folder's log files ─────────────────────────────────
    _bot_fs(bot_id, bot.name).clear_logs()


# ── AI Fix ────────────────────────────────────────────────────────────────────
@router.post("/{bot_id}/ai-fix", response_model=schemas.AiFixResponse)
def ai_fix_bot(
    bot_id: int,
    data: schemas.AiFixRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """
    Analyse recent error logs and the bot code with Claude, then return a
    suggested fix.  Claude is NEVER given write access — it only returns a
    proposed diff that the user must explicitly apply.
    """
    import anthropic

    bot = db.query(models.Bot).filter(models.Bot.id == bot_id, models.Bot.user_id == user.id).first()
    if not bot:
        raise HTTPException(404, "Bot not found")

    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
    if not api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured on the server")

    # ── Build the prompt ──────────────────────────────────────────────────────
    error_block = "\n".join(data.error_logs[-60:])   # cap at 60 lines
    extra = f"\n\nAdditional context from user:\n{data.extra_context}" if data.extra_context else ""

    system_prompt = (
        "You are an expert Python debugging assistant for an automated trading bot platform called WATCH-DOG.\n"
        "The user will give you:\n"
        "  1. Recent error/warning log lines from a running bot.\n"
        "  2. The full Python source code of the bot.\n\n"
        "Your task:\n"
        "  - Identify the root cause of every error.\n"
        "  - Produce a corrected version of the FULL bot code.\n"
        "  - Return a JSON object with EXACTLY these keys:\n"
        '    {\n'
        '      "explanation": "<clear English explanation of root cause and fix>",\n'
        '      "changes": [\n'
        '        { "description": "...", "old_code": "...", "new_code": "..." },\n'
        '        ...\n'
        '      ],\n'
        '      "fixed_code": "<complete corrected Python source>"\n'
        '    }\n'
        "Rules:\n"
        "  - fixed_code must be the COMPLETE file, not a snippet.\n"
        "  - old_code / new_code in changes should be the specific lines that changed (verbatim).\n"
        "  - Do NOT add markdown fences around the JSON.\n"
        "  - Do NOT change anything unrelated to the errors.\n"
        "  - Preserve all comments, docstrings, and structure.\n"
    )

    # NOTE: user_message used to be a single concatenated string. It's now
    # split into structured content blocks below so prompt caching can mark
    # the bot code (large, identical across retries) as cacheable.

    try:
        client = anthropic.Anthropic(api_key=api_key)
        # ── Cost-tuning notes ─────────────────────────────────────────────
        # 1. Default model is Haiku 4.5 (was Sonnet 4.5). ~10× cheaper.
        #    To force Sonnet for a specific deploy, set in backend env:
        #      WATCHDOG_AI_FIX_MODEL=claude-sonnet-4-5-20250929
        # 2. Prompt caching is enabled on (a) the system prompt and (b) the
        #    bot code. Both are identical across repeated AI-Fix calls on
        #    the same bot, so the second+ call pays ~10% input cost on the
        #    cached portions. Only the error_logs change between calls.
        # ──────────────────────────────────────────────────────────────────
        model_name = os.getenv("WATCHDOG_AI_FIX_MODEL", "claude-haiku-4-5-20251001")
        message = client.messages.create(
            model=model_name,
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{
                "role": "user",
                "content": [
                    # Cacheable — same across retries on this bot
                    {
                        "type": "text",
                        "text": f"=== BOT CODE ===\n{bot.code}",
                        "cache_control": {"type": "ephemeral"},
                    },
                    # Fresh every call — the actual reason we're being asked
                    {
                        "type": "text",
                        "text": f"=== ERROR LOGS ===\n{error_block}{extra}",
                    },
                ],
            }],
        )
        raw = message.content[0].text.strip()
        # Record token usage so the dashboard Live Stats panel can display it
        try:
            from ..token_tracker import record as _record_tokens
            _record_tokens(message.usage.input_tokens, message.usage.output_tokens)
        except Exception:
            pass
    except Exception as exc:
        raise HTTPException(502, f"Claude API error: {exc}")

    # ── Parse Claude's JSON response ──────────────────────────────────────────
    import json
    try:
        # Strip accidental markdown fences if model added them
        if raw.startswith("```"):
            raw = re.sub(r'^```[a-z]*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw.rstrip())
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, f"Claude returned invalid JSON: {exc}\n\nRaw response (first 500 chars):\n{raw[:500]}")

    # ── Build unified diff for display if changes list is missing ─────────────
    fixed_code = payload.get("fixed_code", bot.code)
    changes_raw = payload.get("changes", [])
    if not changes_raw:
        # Auto-generate a single change entry from the diff
        diff_lines = list(difflib.unified_diff(
            bot.code.splitlines(),
            fixed_code.splitlines(),
            fromfile="original", tofile="fixed", lineterm=""
        ))
        if diff_lines:
            changes_raw = [{
                "description": "Automated fix applied",
                "old_code": bot.code,
                "new_code": fixed_code,
            }]

    changes = [
        schemas.AiFixChange(
            description=c.get("description", ""),
            old_code=c.get("old_code", ""),
            new_code=c.get("new_code", ""),
        )
        for c in changes_raw
    ]

    return schemas.AiFixResponse(
        explanation=payload.get("explanation", ""),
        changes=changes,
        fixed_code=fixed_code,
    )
