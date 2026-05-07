"""
trainer.py — AI Training Dashboard Router for WATCH-DOG
=========================================================
Endpoints for viewing training data, uploading new data,
fetching from URLs, managing strategy files, and running
pattern analysis per-bot.

Mount: /api/trainer
"""

import json
import sys
import os
import csv
import io
import re
import httpx
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Query, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel

# ── Paths ──────────────────────────────────────────────────────────────────────
WATCH_DOG_DIR  = Path("C:/WATCH-DOG/app/backend")
TRAINING_DIR   = WATCH_DOG_DIR / "training_data"
STRATEGY_DIR   = TRAINING_DIR / "_strategies"

# Make sure pattern_analyzer is importable
if str(WATCH_DOG_DIR) not in sys.path:
    sys.path.insert(0, str(WATCH_DOG_DIR))

from ..database import get_db
from .. import models

router = APIRouter(prefix="/api/trainer", tags=["trainer"])


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

BOT_SUBDIRS = ["ticks", "trades", "sessions", "documents", "ai_decisions", "logs"]

def _create_bot_folder(bot_id: int, bot_name: str) -> Path:
    """
    Create the full folder structure for a bot under training_data/.
    Idempotent — safe to call multiple times.
    Returns the bot root folder path.
    """
    safe_name  = re.sub(r'[^a-z0-9_]', '_', bot_name.lower())
    bot_folder = TRAINING_DIR / f"bot_{bot_id}_{safe_name}"
    for sub in BOT_SUBDIRS:
        (bot_folder / sub).mkdir(parents=True, exist_ok=True)

    # Write / update config.json
    config_file = bot_folder / "config.json"
    config = {
        "bot_id":     bot_id,
        "bot_name":   bot_name,
        "safe_name":  safe_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "folder":     str(bot_folder),
        "subfolders": BOT_SUBDIRS,
    }
    # Preserve original created_at if file already exists
    if config_file.exists():
        try:
            existing = json.loads(config_file.read_text("utf-8"))
            config["created_at"] = existing.get("created_at", config["created_at"])
        except Exception:
            pass
    config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return bot_folder

def _bot_folders() -> list[Path]:
    if not TRAINING_DIR.exists():
        return []
    return sorted(
        f for f in TRAINING_DIR.iterdir()
        if f.is_dir() and f.name.startswith("bot_")
    )


def _parse_bot_folder(folder: Path) -> dict:
    """Extract bot_id and bot_name from folder name like bot_1_kalshi_btc_15m."""
    parts = folder.name.split("_", 2)
    bot_id   = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    bot_name = parts[2] if len(parts) > 2 else folder.name
    return {"bot_id": bot_id, "bot_name": bot_name, "folder": folder}


def _load_jsonl(path: Path) -> list:
    records = []
    if not path.exists():
        return records
    for f in sorted(path.glob("*.jsonl")):
        try:
            with f.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        parsed = json.loads(line)
                        if isinstance(parsed, dict):   # skip strings, ints, lists
                            records.append(parsed)
        except Exception:
            pass
    return records


def _bot_stats(info: dict) -> dict:
    folder   = info["folder"]
    sessions = _load_jsonl(folder / "sessions")
    ticks    = _load_jsonl(folder / "ticks")
    trades   = _load_jsonl(folder / "trades")

    known_ticks = [t for t in ticks if t.get("session_result") in ("YES", "NO")]
    winning_sessions = [s for s in sessions if s.get("profitable")]
    total_pnl = round(sum(s.get("total_pnl", 0) for s in sessions), 2)

    # Compute win rate
    win_rate = 0
    if sessions:
        win_rate = round(len(winning_sessions) / len(sessions) * 100)

    # Last session time
    last_session = None
    if sessions:
        ts_list = [s.get("ts") or s.get("session_end") for s in sessions if s.get("ts") or s.get("session_end")]
        if ts_list:
            last_session = max(ts_list)

    return {
        "bot_id":          info["bot_id"],
        "bot_name":        info["bot_name"],
        "folder_name":     folder.name,
        "total_sessions":  len(sessions),
        "winning_sessions":len(winning_sessions),
        "win_rate":        win_rate,
        "total_ticks":     len(ticks),
        "known_ticks":     len(known_ticks),
        "total_trades":    len(trades),
        "total_pnl":       total_pnl,
        "ready_to_learn":  len(known_ticks) >= 200,
        "progress_pct":    min(100, round(len(known_ticks) / 200 * 100)),
        "last_session":    last_session,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/trainer/init-bot  — called automatically on bot creation
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/init-bot")
async def init_bot_folder(bot_id: int = Form(...), bot_name: str = Form(...)):
    """
    Create the dedicated training_data/bot_{id}_{name}/ folder structure.
    Called automatically by the frontend when a new bot is created.
    Idempotent — safe to call again if folder already exists.
    """
    try:
        folder = _create_bot_folder(bot_id, bot_name)
        return {
            "success":    True,
            "folder":     str(folder),
            "bot_id":     bot_id,
            "bot_name":   bot_name,
            "subfolders": BOT_SUBDIRS,
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to create bot folder: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/trainer/folder-structure/{bot_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/folder-structure/{bot_id}")
def get_folder_structure(bot_id: int):
    """Return the folder structure + file counts for a bot."""
    for f in _bot_folders():
        info = _parse_bot_folder(f)
        if info["bot_id"] == bot_id:
            folder = f
            result = {"folder": str(folder), "subfolders": {}}
            for sub in BOT_SUBDIRS:
                sub_path = folder / sub
                if sub_path.exists():
                    files = list(sub_path.iterdir())
                    result["subfolders"][sub] = {
                        "exists":    True,
                        "file_count": len(files),
                        "files":     [{"name": fi.name, "size_kb": round(fi.stat().st_size / 1024, 1)} for fi in sorted(files)[:20]],
                    }
                else:
                    result["subfolders"][sub] = {"exists": False, "file_count": 0, "files": []}
            # Config
            cfg = folder / "config.json"
            if cfg.exists():
                try: result["config"] = json.loads(cfg.read_text("utf-8"))
                except Exception: result["config"] = {}
            return result
    raise HTTPException(404, f"No training data folder for bot {bot_id}")


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/trainer/overview
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/overview")
def get_overview(db: Session = Depends(get_db)):
    """Master stats across ALL bots — for the dashboard header cards."""
    # Bots live in Supabase; we don't filter by existence here.
    valid_bot_ids = None

    folders = _bot_folders()
    if valid_bot_ids is not None:
        folders = [f for f in folders if _parse_bot_folder(f)["bot_id"] in valid_bot_ids]
    bot_stats_list = [_bot_stats(_parse_bot_folder(f)) for f in folders]

    total_sessions  = sum(b["total_sessions"]  for b in bot_stats_list)
    total_ticks     = sum(b["total_ticks"]      for b in bot_stats_list)
    total_pnl       = round(sum(b["total_pnl"]  for b in bot_stats_list), 2)
    ready_bots      = sum(1 for b in bot_stats_list if b["ready_to_learn"])

    # Master index cross-bot stats
    master_records = []
    master_file = TRAINING_DIR / "_master_index.jsonl"
    if master_file.exists():
        try:
            with master_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        master_records.append(json.loads(line))
        except Exception:
            pass

    return {
        "total_bots":     len(folders),
        "ready_bots":     ready_bots,
        "total_sessions": total_sessions,
        "total_ticks":    total_ticks,
        "total_pnl":      total_pnl,
        "master_records": len(master_records),
        "bots":           bot_stats_list,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/trainer/patterns/{bot_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/patterns/{bot_id}")
def get_patterns(bot_id: int):
    """Run PatternAnalyzer for a specific bot and return the text + structured data."""
    # Find the bot folder
    folder = None
    bot_name = ""
    for f in _bot_folders():
        info = _parse_bot_folder(f)
        if info["bot_id"] == bot_id:
            folder   = f
            bot_name = info["bot_name"]
            break

    if not folder:
        raise HTTPException(404, f"No training data found for bot {bot_id}")

    try:
        from pattern_analyzer import PatternAnalyzer
        pa      = PatternAnalyzer(bot_id=bot_id, bot_name=bot_name)
        summary = pa.analyze()
        stats   = _bot_stats({"bot_id": bot_id, "bot_name": bot_name, "folder": folder})

        return {
            "bot_id":   bot_id,
            "bot_name": bot_name,
            "summary":  summary or "",
            "ready":    bool(summary),
            "stats":    stats,
        }
    except ImportError:
        raise HTTPException(500, "pattern_analyzer.py not found in C:/WATCH-DOG")
    except Exception as e:
        raise HTTPException(500, str(e))


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/trainer/sessions/{bot_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/sessions/{bot_id}")
def get_sessions(bot_id: int, limit: int = Query(50, ge=1, le=500)):
    """Return recent session records for a bot."""
    for f in _bot_folders():
        info = _parse_bot_folder(f)
        if info["bot_id"] == bot_id:
            sessions = _load_jsonl(f / "sessions")
            # Sort by ts descending
            sessions.sort(key=lambda s: s.get("ts") or s.get("session_end") or "", reverse=True)
            return {"sessions": sessions[:limit]}
    raise HTTPException(404, f"No training data for bot {bot_id}")


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/trainer/upload
# ─────────────────────────────────────────────────────────────────────────────

class UploadResult(BaseModel):
    records_added: int
    bot_id: int
    bot_name: str
    file_saved_to: str


DOCUMENT_EXTS = {'.txt', '.pdf', '.docx', '.md'}
DATA_EXTS     = {'.jsonl', '.json', '.csv'}
ALL_SUPPORTED = DOCUMENT_EXTS | DATA_EXTS


@router.post("/upload")
async def upload_training_file(
    bot_id:   int        = Form(...),
    bot_name: str        = Form(...),
    data_type:str        = Form("ticks"),   # "ticks" | "trades" | "sessions" (only for data files)
    file:     UploadFile = File(...),
):
    """
    Upload a file into the bot's dedicated folder.

    Document types (.txt .pdf .docx .md)  → saved to  documents/
    Data types     (.jsonl .json .csv)     → parsed & saved to {data_type}/

    Folder is auto-created via _create_bot_folder() so the full structure
    always exists after upload.
    """
    filename = file.filename or "upload"
    ext      = Path(filename).suffix.lower()

    if ext not in ALL_SUPPORTED:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALL_SUPPORTED))}")

    content   = await file.read()
    safe_name = re.sub(r'[^a-z0-9_]', '_', bot_name.lower())
    ts        = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # Ensure full folder structure exists
    bot_folder = _create_bot_folder(bot_id, bot_name)

    # ── Document: save raw file into documents/ ───────────────────────────────
    if ext in DOCUMENT_EXTS:
        doc_folder  = bot_folder / "documents"
        safe_fn     = re.sub(r'[^a-z0-9_.-]', '_', filename.lower())
        out_file    = doc_folder / f"{ts}_{safe_fn}"
        with out_file.open("wb") as fh:
            fh.write(content)
        return {
            "records_added": 1,
            "file_type":     "document",
            "bot_id":        bot_id,
            "bot_name":      safe_name,
            "file_saved_to": str(out_file),
        }

    # ── Data file: parse & save as JSONL ─────────────────────────────────────
    raw     = content.decode("utf-8", errors="replace")
    records = _parse_upload(raw, filename)
    if not records:
        raise HTTPException(400, "No valid records found in uploaded file")

    data_folder = bot_folder / data_type
    out_file    = data_folder / f"uploaded_{ts}.jsonl"
    with out_file.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")

    return UploadResult(
        records_added  = len(records),
        bot_id         = bot_id,
        bot_name       = safe_name,
        file_saved_to  = str(out_file),
    )


def _parse_upload(raw: str, filename: str) -> list:
    """Parse .jsonl, .json, or .csv into a list of dicts."""
    ext = Path(filename).suffix.lower()

    if ext == ".jsonl":
        records = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        records.append(parsed)
                except Exception:
                    pass
        return records

    if ext == ".json":
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
            if isinstance(data, dict):
                return [data]
        except Exception:
            pass
        return []

    if ext == ".csv":
        reader  = csv.DictReader(io.StringIO(raw))
        records = []
        for row in reader:
            # Convert numeric strings to float where possible
            clean = {}
            for k, v in row.items():
                if v == "" or v is None:
                    clean[k] = None
                else:
                    try:
                        clean[k] = float(v) if "." in v else int(v)
                    except (ValueError, TypeError):
                        clean[k] = v
            records.append(clean)
        return records

    # Unknown extension — try full JSON parse first, then line-by-line JSONL
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            return [data]
    except Exception:
        pass
    records = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    records.append(parsed)
            except Exception:
                pass
    return records


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/trainer/fetch-url
# ─────────────────────────────────────────────────────────────────────────────

class FetchURLRequest(BaseModel):
    url:       str
    bot_id:    int
    bot_name:  str
    data_type: str = "ticks"   # "ticks" | "trades" | "sessions"


@router.post("/fetch-url")
async def fetch_training_url(req: FetchURLRequest):
    """
    Fetch training data from a URL (must return .jsonl, .json, or .csv text).
    Saves to training_data/bot_{id}_{name}/{data_type}/fetched_<timestamp>.jsonl
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(req.url)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(400, f"URL returned {e.response.status_code}")
    except Exception as e:
        raise HTTPException(400, f"Failed to fetch URL: {e}")

    # Try to detect format from content-type or URL path
    content_type = resp.headers.get("content-type", "")
    url_path     = req.url.split("?")[0].lower()

    if ".csv" in url_path or "text/csv" in content_type:
        ext = ".csv"
    elif url_path.endswith(".jsonl"):
        ext = ".jsonl"
    elif url_path.endswith(".json") or url_path.endswith("/json") or "application/json" in content_type:
        ext = ".json"
    else:
        # Let _parse_upload auto-detect (tries full JSON first, then JSONL line-by-line)
        ext = ""

    raw     = resp.text
    records = _parse_upload(raw, f"file{ext}")

    if not records:
        raise HTTPException(400, "No valid records found at URL — expected JSON, JSONL, or CSV")

    safe_name = re.sub(r'[^a-z0-9_]', '_', req.bot_name.lower())
    folder    = TRAINING_DIR / f"bot_{req.bot_id}_{safe_name}" / req.data_type
    folder.mkdir(parents=True, exist_ok=True)

    ts       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_file = folder / f"fetched_{ts}.jsonl"

    with out_file.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")

    return {
        "records_added": len(records),
        "bot_id":        req.bot_id,
        "bot_name":      safe_name,
        "file_saved_to": str(out_file),
        "url":           req.url,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Strategy file endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/strategies")
def list_strategies():
    """List all uploaded strategy .py files."""
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for f in sorted(STRATEGY_DIR.glob("*.py")):
        stat = f.stat()
        files.append({
            "name":         f.stem,          # strip .py — use f.stem not f.name
            "file":         f.name,
            "size_bytes":   stat.st_size,
            "modified_at":  datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return {"strategies": files}


@router.post("/strategy/upload")
async def upload_strategy(
    name: str        = Form(...),    # Logical name, e.g. "aggressive_v2"
    file: UploadFile = File(...),
):
    """Upload a .py strategy file."""
    if not file.filename or not file.filename.endswith(".py"):
        raise HTTPException(400, "Strategy file must be a .py file")

    safe = re.sub(r'[^a-z0-9_]', '_', name.lower())
    if not safe:
        raise HTTPException(400, "Invalid strategy name")

    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    content   = await file.read()
    out_path  = STRATEGY_DIR / f"{safe}.py"

    with out_path.open("wb") as fh:
        fh.write(content)

    return {
        "strategy_name": safe,
        "file":          out_path.name,
        "size_bytes":    len(content),
        "saved_to":      str(out_path),
    }


@router.get("/strategy/{name}")
def get_strategy(name: str):
    """Return content of a strategy file."""
    safe = re.sub(r'[^a-z0-9_]', '_', name.lower())
    path = STRATEGY_DIR / f"{safe}.py"
    if not path.exists():
        raise HTTPException(404, f"Strategy '{name}' not found")
    return {"name": safe, "content": path.read_text(encoding="utf-8")}


@router.delete("/strategy/{name}")
def delete_strategy(name: str):
    """Delete a strategy file."""
    safe = re.sub(r'[^a-z0-9_]', '_', name.lower())
    path = STRATEGY_DIR / f"{safe}.py"
    if not path.exists():
        raise HTTPException(404, f"Strategy '{name}' not found")
    path.unlink()
    return {"deleted": safe}
