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
from ..cloud_log_shipper import ship_log as _cloud_ship_log

# ── Cloud log shipping helper (v1.1.0) ───────────────────────────────────────
# Each SQLite log insert is mirrored to Supabase bot_logs_tail via the async
# shipper so the renderer can subscribe to realtime INSERT events instead of
# polling 127.0.0.1:8000/api/bots/{id}/logs. NEVER raises — a Supabase outage
# must not break the local bot run.
def _ship(bl) -> None:
    try:
        lvl = getattr(bl.level, "value", str(bl.level))
        _cloud_ship_log(str(bl.bot_id), str(bl.user_id), str(lvl), bl.message or "", bl.id)
    except Exception:
        pass
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


def _bot_runtime_dir(bot_uuid: str) -> str:
    """Per-bot working directory (CWD for the subprocess)."""
    base = os.environ.get("WATCHDOG_DATA_DIR")
    if not base:
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
            base = os.path.join(base, "WatchDog")
        else:
            base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
            base = os.path.join(base, "WatchDog")
    p = os.path.join(base, "bot-runtime", bot_uuid)
    os.makedirs(p, exist_ok=True)
    return p


def _materialize_secrets_to_disk(runtime_dir: str, conns: list, env: dict) -> None:
    """Write PEM-style secrets as files in the bot's runtime dir.
    Bots that read e.g. open('kalshi_private_key.pem') then work without
    code change. We write multiple filename variants to cover common
    conventions used by trading-bot tutorials and SDKs.
    """
    for c in (conns or []):
        secret = (c.get("api_secret") or "")
        if not secret or "BEGIN" not in secret[:50]:
            continue
        raw_name = (c.get("name") or "")
        sanitized = re.sub(r'[^a-z0-9]+', '_', raw_name.lower()).strip('_') or "connection"
        first_word = sanitized.split('_')[0] or sanitized
        candidates = [f"{first_word}_private_key.pem"]
        if sanitized != first_word:
            candidates.append(f"{sanitized}_private_key.pem")
        for fname in candidates:
            try:
                fpath = os.path.join(runtime_dir, fname)
                with open(fpath, "w", encoding="utf-8", newline="\n") as f:
                    f.write(secret)
                env[f"{first_word.upper()}_PRIVATE_KEY_FILE"] = fpath
            except Exception:
                pass

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
              env: dict, demo_mode: bool = False,
              python_exe: Optional[str] = None,
              cwd: Optional[str] = None,
              recent_lines_out: Optional[list] = None) -> int:
    """Run the bot script once, stream logs to SQLite, return exit code.

    If python_exe is provided (set when the bot has requirements + a venv),
    we use it instead of sys.executable. The venv path runs the bot script
    directly (no wd_runner wrapper) — wd_runner is only needed in the
    bundled-PyInstaller case to switch the frozen exe into "run a script"
    mode. A real venv python.exe just runs the script natively.

    If recent_lines_out is a list, the last ~50 stdout lines are APPENDED to it
    (mutated in place). Used by _execute() to scan for ModuleNotFoundError
    after a non-zero exit and decide whether to auto-install and retry.
    """
    _popen_kwargs: dict = {}
    if sys.platform == "win32":
        _popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    env["DRY_RUN"] = "1" if demo_mode else "0"

    try:
        # ── v2 wd_runner now also wraps user code in a friendly-error
        # layer (Gap 3 of the bot-DX overhaul). Route BOTH the venv and
        # bundled-Python paths through it so the user sees consistent
        # "[ERROR] line 42: <type> — <hint>" output regardless of whether
        # their bot needs a venv. wd_runner is pure-stdlib for the wrapper
        # logic; its `import wd_autolog` is in a try/except, so the venv
        # path (where wd_autolog isn't available) just emits a single
        # info line about hooks being unavailable and moves on.
        _wd_runner = os.path.join(_SDK_DIR, 'wd_runner.py')
        if python_exe:
            cmd = [python_exe, '-u', _wd_runner, tmp_path]
        else:
            cmd = [sys.executable, '-u', _wd_runner, tmp_path]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            env=env,
            cwd=cwd,
            **_popen_kwargs,
        )
    except Exception as exc:
        err = f"[WATCHDOG] Failed to launch bot process: {exc}"
        try:
            bl = models.BotLog(bot_id=bot_uuid, user_id=user_id,
                               level=models.LogLevel.ERROR, message=err)
            db.add(bl); db.commit(); _ship(bl)
        except Exception:
            pass
        return -1

    _processes[bot_uuid] = process

    # Rolling buffer of recent stdout lines for retry-on-missing-module scanning.
    # Bounded to 50 entries so a runaway log doesn't blow memory.
    from collections import deque as _deque
    _recent: _deque = _deque(maxlen=50)

    try:
        for raw in process.stdout:
            line = _ANSI_RE.sub('', raw.rstrip())
            if not line:
                continue
            _recent.append(line)
            lower = line.lower()
            first = line.split(']')[0].lstrip('[').upper() if line.startswith('[') else ''
            if first in ('ERROR', 'EXCEPTION', 'FATAL') or any(w in lower for w in ('traceback', 'exception', 'error')):
                level = models.LogLevel.ERROR
            elif first == 'WARNING' or 'warning' in lower or 'warn' in lower:
                level = models.LogLevel.WARNING
            else:
                level = models.LogLevel.INFO
            try:
                bl = models.BotLog(bot_id=bot_uuid, user_id=user_id,
                                   level=level, message=line)
                db.add(bl); db.commit(); _ship(bl)
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
            bl = models.BotLog(bot_id=bot_uuid, user_id=user_id,
                               level=models.LogLevel.ERROR, message=err)
            db.add(bl); db.commit(); _ship(bl)
        except Exception:
            pass
    finally:
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()

    # Expose the tail so the caller can scan for retryable failures.
    if recent_lines_out is not None:
        recent_lines_out.extend(_recent)

    return process.returncode


def _execute(bot_uuid: str, code: str, user_id: int, env: dict,
             bot_secret: str, demo_mode: bool = False,
             python_exe: Optional[str] = None, requirements: Optional[str] = None,
             conns_for_bot: Optional[list] = None):
    """Background-thread entry point: write code to a temp file, run, log status."""
    db = SessionLocal()
    tmp_path = None
    if conns_for_bot is None: conns_for_bot = []
    try:
        # ── Pre-flight syntax check ─────────────────────────────────────────
        # Gap 5: catch SyntaxError BEFORE spawning a subprocess so the user
        # sees a single friendly line with the bad line number, not a
        # mysterious "Process exited with code 1" after a venv install.
        # Save-time validation could also live here in a future endpoint;
        # for now we surface it at run-time which gives 100% coverage.
        try:
            compile(code, "<bot>", "exec")
        except SyntaxError as _se:
            line_no = getattr(_se, "lineno", None)
            col_no  = getattr(_se, "offset", None)
            msg     = getattr(_se, "msg", "syntax error")
            where   = f" (line {line_no}" + (f", col {col_no}" if col_no else "") + ")" if line_no else ""
            friendly = f"[WATCHDOG] Syntax error in your code{where}: {msg}"
            try:
                bl = models.BotLog(bot_id=bot_uuid, user_id=user_id,
                                   level=models.LogLevel.ERROR, message=friendly)
                db.add(bl); db.commit(); _ship(bl)
            except Exception:
                try: db.rollback()
                except Exception: pass
            cloud_db.update_bot_status(bot_uuid, status="ERROR", is_running=False)
            return

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

        # Phase 2: per-bot venv lifecycle.
        #   - Explicit requirements field set -> install exactly those.
        #   - Empty requirements -> AST-scan the code, auto-detect imports,
        #     install whatever's not stdlib/bundled. (Zero-config UX.)
        if not python_exe:
            from .. import bot_venv as _bv

            def _setup_log(line: str) -> None:
                try:
                    bl = models.BotLog(bot_id=bot_uuid, user_id=user_id,
                                       level=models.LogLevel.INFO, message=line)
                    db.add(bl); db.commit(); _ship(bl)
                except Exception:
                    try: db.rollback()
                    except Exception: pass

            effective_reqs = (requirements or "").strip()
            if not effective_reqs:
                detected = _bv.detect_requirements_from_code(code)
                if detected:
                    effective_reqs = "\n".join(detected)
                    _setup_log(f"[setup] auto-detected dependencies: {', '.join(detected)}")

            if effective_reqs:
                py_path, err = _bv.prepare_venv(bot_uuid, effective_reqs, log_callback=_setup_log)
                if err:
                    _setup_log(f"[setup] FAILED: {err}")
                    cloud_db.update_bot_status(bot_uuid, status="ERROR", is_running=False)
                    return
                python_exe = str(py_path) if py_path else None
            # else: code uses only stdlib/bundled libs -> bundled python is fine

        # Phase 2: write PEM secrets as files + set CWD so bot code that does
        # open("kalshi_private_key.pem") works without modification.
        runtime_dir = _bot_runtime_dir(bot_uuid)
        _materialize_secrets_to_disk(runtime_dir, conns_for_bot, env)

        # ── Self-heal retry loop ────────────────────────────────────────────
        # Gap 1: when the bot crashes with `ModuleNotFoundError: No module
        # named 'X'` — which AST detection misses for dynamic imports, lazy
        # imports inside try/except, or transitive failures — we parse the
        # tail of subprocess output, install 'X' into the venv via uv, and
        # retry. Capped at MAX_RETRIES so a genuinely-broken bot never loops.
        # Only fires when we have a venv to install INTO (python_exe is set);
        # without a venv, we have no place to put the new package.
        MAX_RETRIES = 3
        attempts = 0
        rc = -1
        while True:
            tail_lines: list = []
            rc = _run_once(bot_uuid, tmp_path, user_id, db, env,
                           demo_mode=demo_mode, python_exe=python_exe,
                           cwd=runtime_dir, recent_lines_out=tail_lines)

            # User asked to stop -> _processes has been popped; respect that.
            if bot_uuid not in _processes:
                break
            if rc == 0:
                break
            if attempts >= MAX_RETRIES:
                break

            from .. import bot_venv as _bv2
            missing = _bv2.parse_missing_module("\n".join(tail_lines))
            if not missing:
                break  # exit code != 0 but not a missing-module failure

            # If we don't have a venv yet (bundled-Python path), bootstrap one
            # now containing just the missing module so the retry can run there.
            def _setup_log_retry(line: str) -> None:
                try:
                    bl = models.BotLog(bot_id=bot_uuid, user_id=user_id,
                                       level=models.LogLevel.INFO, message=line)
                    db.add(bl); db.commit(); _ship(bl)
                except Exception:
                    try: db.rollback()
                    except Exception: pass

            if not python_exe:
                py_path, perr = _bv2.prepare_venv(bot_uuid, missing,
                                                  log_callback=_setup_log_retry)
                if perr or not py_path:
                    _setup_log_retry(f"[setup] cannot create venv for retry: {perr}")
                    break
                python_exe = str(py_path)
            else:
                ok, ierr = _bv2.install_one_into_venv(
                    bot_uuid, missing, log_callback=_setup_log_retry
                )
                if not ok:
                    _setup_log_retry(f"[setup] auto-install of {missing!r} failed: {ierr}")
                    break

            attempts += 1
            _setup_log_retry(f"[setup] retrying bot (attempt {attempts}/{MAX_RETRIES}) after installing {missing!r}")
            # Loop continues — re-runs the bot with the new dependency.

        exit_msg = f"[WATCHDOG] Process exited with code {rc}"
        exit_level = models.LogLevel.INFO if rc == 0 else models.LogLevel.ERROR
        try:
            bl = models.BotLog(bot_id=bot_uuid, user_id=user_id,
                               level=exit_level, message=exit_msg)
            db.add(bl); db.commit(); _ship(bl)
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
            bl = models.BotLog(bot_id=bot_uuid, user_id=user_id,
                               level=models.LogLevel.ERROR, message=err_msg[:2000])
            db.add(bl); db.commit(); _ship(bl)
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
    requirements = bot.get("requirements") or ""

    threading.Thread(
        target=_execute,
        args=(bot_id, code, user.id, env, bot_secret, body.demo_mode, None, requirements, conns),
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
