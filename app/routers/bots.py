"""
bots.py - runtime-only bot management.

Bot definitions and api_connections live in Supabase. This router does NOT
own that data. It only:
  - spawns bot subprocesses (POST /run)
  - kills them              (POST /stop)
  - buffers logs in SQLite  (GET  /logs)
  - delegates AI Fix to Claude

bot_id everywhere is a Supabase UUID string.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
import logging
import subprocess
import sys
import os
import re
import tempfile
import threading
import difflib
import uuid as _uuid
from datetime import datetime, timezone

from ..database import get_db, SessionLocal
from .. import models, schemas, cloud_db
from ..auth import get_default_user as get_current_user

_SDK_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'sdk')
_ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-9;?]*[ -/]*[@-~])')


router = APIRouter(prefix="/api/bots", tags=["bots"])

# In-memory process registry: {bot_uuid: subprocess.Popen}
_processes: dict[str, subprocess.Popen] = {}

# Map bot_secret -> bot_uuid for trade-submission auth (set on run, cleared on stop)
_running_secrets: dict[str, str] = {}


def get_bot_uuid_for_secret(secret: str) -> Optional[str]:
    """Used by trades.py to authenticate trade submissions."""
    return _running_secrets.get(secret)


class BotRunIn(BaseModel):
    """Request body for POST /{bot_id}/run"""
    demo_mode: bool = False


def _env_prefix(name: str) -> str:
    """Turn a connection name into a safe env-var prefix."""
    return re.sub(r'[^A-Z0-9]+', '_', (name or "").upper()).strip('_')


def _build_env(bot_uuid: str, bot_row: dict, conns: list[dict], bot_secret: str) -> dict:
    """Build environment for the bot subprocess from cloud-fetched data."""
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    sdk_path      = os.path.abspath(_SDK_DIR)
    watchdog_path = "C:/WATCH-DOG/app/backend"
    env['PYTHONPATH'] = (
        sdk_path + os.pathsep +
        watchdog_path + os.pathsep +
        env.get('PYTHONPATH', '')
    )

    env["WATCHDOG_API_URL"]    = os.getenv("WATCHDOG_API_URL", "http://localhost:8000")
    env["WATCHDOG_BOT_SECRET"] = bot_secret
    env["WATCHDOG_BOT_ID"]     = bot_uuid

    name_raw = (bot_row.get("name") or "bot")
    _bname = re.sub(r'[^a-z0-9]+', '_', name_raw.lower()).strip('_')
    _bname = re.sub(r'_(bot|trade|trader)$', '', _bname) or "bot"
    env["WATCHDOG_BOT_NAME"] = _bname

    # Risk settings (optional)
    if bot_row.get("max_amount_per_trade") is not None:
        env["WATCHDOG_MAX_AMOUNT_PER_TRADE"]    = str(bot_row["max_amount_per_trade"])
    if bot_row.get("max_contracts_per_trade") is not None:
        env["WATCHDOG_MAX_CONTRACTS_PER_TRADE"] = str(bot_row["max_contracts_per_trade"])
    if bot_row.get("max_daily_loss") is not None:
        env["WATCHDOG_MAX_DAILY_LOSS"]          = str(bot_row["max_daily_loss"])

    for c in conns:
        prefix = _env_prefix(c.get("name") or "")
        if not prefix:
            continue
        if c.get("api_key"):
            env[f"{prefix}_KEY"] = c["api_key"]
            env[prefix]          = c["api_key"]
        if c.get("api_secret"):
            env[f"{prefix}_SECRET"] = c["api_secret"]
        if c.get("base_url"):
            env[f"{prefix}_URL"] = c["base_url"]
    return env


def _run_once(bot_uuid: str, tmp_path: str, user_id: int, db,
              env: dict, demo_mode: bool = False) -> int:
    """Run the bot script once, stream logs to SQLite, return exit code."""
    _popen_kwargs: dict = {}
    if sys.platform == "win32":
        _popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    env["DRY_RUN"] = "1" if demo_mode else "0"

    try:
        _wd_runner = os.path.join(_SDK_DIR, 'wd_runner.py')
        process = subprocess.Popen(
            [sys.executable, '-u', _wd_runner, tmp_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            env=env,
            **_popen_kwargs,
        )
    except Exception as exc:
        err = f"[WATCHDOG] Failed to launch bot process: {exc}"
        try:
            db.add(models.BotLog(bot_id=bot_uuid, user_id=user_id,
                                 level=models.LogLevel.ERROR, message=err))
            db.commit()
        except Exception:
            pass
        return -1

    _processes[bot_uuid] = process

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
                db.add(models.BotLog(bot_id=bot_uuid, user_id=user_id,
                                     level=level, message=line))
                db.commit()
            except Exception as _bl_e:
                # Log it so we can see it (was silent before — hid major bugs)
                try: db.rollback()
                except Exception: pass
                logging.getLogger("watchdog.bot.log").warning(
                    "BotLog insert failed (bot_id=%s): %s", bot_uuid, _bl_e
                )
    except Exception as stream_exc:
        err = f"[WATCHDOG] Log stream error for bot {bot_uuid}: {stream_exc}"
        try:
            db.add(models.BotLog(bot_id=bot_uuid, user_id=user_id,
                                 level=models.LogLevel.ERROR, message=err))
            db.commit()
        except Exception:
            pass
    finally:
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()

    return process.returncode


def _execute(bot_uuid: str, code: str, user_id: int, env: dict,
             bot_secret: str, demo_mode: bool = False):
    """Background-thread entry point: write code to a temp file, run, log status."""
    db = SessionLocal()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp_path = f.name

        # Mark cloud as RUNNING
        cloud_db.update_bot_status(
            bot_uuid,
            status="RUNNING",
            is_running=True,
            last_run_at=datetime.now(timezone.utc).isoformat(),
        )

        rc = _run_once(bot_uuid, tmp_path, user_id, db, env, demo_mode=demo_mode)

        exit_msg = f"[WATCHDOG] Process exited with code {rc}"
        exit_level = models.LogLevel.INFO if rc == 0 else models.LogLevel.ERROR
        try:
            db.add(models.BotLog(bot_id=bot_uuid, user_id=user_id,
                                 level=exit_level, message=exit_msg))
            db.commit()
        except Exception:
            pass

        # Determine final cloud status: STOPPED if user requested stop (process
        # already removed from _processes), IDLE on clean exit, ERROR otherwise.
        if bot_uuid not in _processes:
            final = "STOPPED"
        elif rc == 0:
            final = "IDLE"
        else:
            final = "ERROR"
        # Fetch current run_count + increment (cloud is source of truth)
        try:
            current = cloud_db.get_bot(bot_uuid) or {}
            new_run_count = int(current.get("run_count") or 0) + 1
        except Exception:
            new_run_count = None
        cloud_db.update_bot_status(bot_uuid, status=final, is_running=False,
                                   run_count=new_run_count)

    except Exception as exc:
        import traceback as _tb
        err_msg = f"[WATCHDOG] Runner error: {exc}\n{_tb.format_exc()}"
        try:
            db.rollback()
            db.add(models.BotLog(bot_id=bot_uuid, user_id=user_id,
                                 level=models.LogLevel.ERROR, message=err_msg[:2000]))
            db.commit()
        except Exception as _le:
            logging.getLogger("watchdog.bot.exec").warning("err-log insert failed: %s", _le)
        try:
            from .. import error_reporter as _er
            _er.report_error(exc, source="bot-subprocess", bot_id=bot_uuid)
        except Exception:
            pass
        cloud_db.update_bot_status(bot_uuid, status="ERROR", is_running=False)
    finally:
        db.close()
        _processes.pop(bot_uuid, None)
        _running_secrets.pop(bot_secret, None)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@router.post("/{bot_id}/run")
def run_bot(
    bot_id: str,
    body: BotRunIn = BotRunIn(),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    bot = cloud_db.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "Bot not found in cloud")
    if bot_id in _processes:
        raise HTTPException(400, "Bot is already running")

    code = bot.get("code") or ""
    if not code.strip():
        raise HTTPException(400, "Bot has no code to run")

    conns = cloud_db.list_bot_connections(bot_id)
    bot_secret = str(_uuid.uuid4())
    _running_secrets[bot_secret] = bot_id

    env = _build_env(bot_id, bot, conns, bot_secret)

    threading.Thread(
        target=_execute,
        args=(bot_id, code, user.id, env, bot_secret, body.demo_mode),
        daemon=True,
    ).start()
    return {"message": "Bot started", "bot_id": bot_id, "demo_mode": body.demo_mode}


@router.post("/{bot_id}/stop")
def stop_bot(
    bot_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    proc = _processes.pop(bot_id, None)
    if proc is not None:
        try:
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
    # Best-effort cloud status flip; final status will be confirmed by _execute
    cloud_db.update_bot_status(bot_id, status="STOPPED", is_running=False)
    return {"message": "Bot stopped"}


@router.get("/{bot_id}/logs", response_model=List[schemas.BotLogOut])
def get_bot_logs(
    bot_id: str,
    limit: int = 200,
    since_id: int = 0,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    q = db.query(models.BotLog).filter(models.BotLog.bot_id == bot_id)
    if since_id > 0:
        return (q
                .filter(models.BotLog.id > since_id)
                .order_by(models.BotLog.created_at.asc())
                .limit(limit)
                .all())
    return (q
            .order_by(models.BotLog.created_at.desc())
            .limit(limit)
            .all())


@router.delete("/{bot_id}/logs", status_code=204)
def clear_bot_logs(bot_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    db.query(models.BotLog).filter(models.BotLog.bot_id == bot_id).delete()
    db.commit()


# ── AI Fix ────────────────────────────────────────────────────────────────────
@router.post("/{bot_id}/ai-fix", response_model=schemas.AiFixResponse)
def ai_fix_bot(
    bot_id: str,
    data: schemas.AiFixRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """
    Analyse recent error logs and the bot code with Claude, then return a
    suggested fix. Claude is NEVER given write access - it only returns a
    proposed diff that the user must explicitly apply.
    """
    import anthropic

    bot = cloud_db.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "Bot not found in cloud")

    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
    if not api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured on the server")

    error_block = "\n".join(data.error_logs[-60:])
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

    bot_code = bot.get("code") or ""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        model_name = os.getenv("WATCHDOG_AI_FIX_MODEL", "claude-haiku-4-5-20251001")
        message = client.messages.create(
            model=model_name,
            max_tokens=8192,
            system=[
                {"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": f"=== BOT CODE ===\n{bot_code}",
                     "cache_control": {"type": "ephemeral"}},
                    {"type": "text",
                     "text": f"=== ERROR LOGS ===\n{error_block}{extra}"},
                ],
            }],
        )
        raw = message.content[0].text.strip()
        try:
            from ..token_tracker import record as _record_tokens
            _record_tokens(message.usage.input_tokens, message.usage.output_tokens)
        except Exception:
            pass
    except Exception as exc:
        raise HTTPException(502, f"Claude API error: {exc}")

    import json
    try:
        if raw.startswith("```"):
            raw = re.sub(r'^```[a-z]*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw.rstrip())
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, f"Claude returned invalid JSON: {exc}\n\nRaw response (first 500 chars):\n{raw[:500]}")

    fixed_code = payload.get("fixed_code", bot_code)
    changes_raw = payload.get("changes", [])
    if not changes_raw:
        diff_lines = list(difflib.unified_diff(
            bot_code.splitlines(),
            fixed_code.splitlines(),
            fromfile="original", tofile="fixed", lineterm=""
        ))
        if diff_lines:
            changes_raw = [{
                "description": "Automated fix applied",
                "old_code": bot_code,
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
