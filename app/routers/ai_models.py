"""
ai_models.py — AI Lab Router for WATCH-DOG
============================================
Full CRUD for AI Models + file upload + async training engine.

Mount: /api/ai-models
"""

import io
import json
import os
import re
import csv
import uuid
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from .. import models
from ..auth import get_default_user as get_current_user

# ── Storage paths ──────────────────────────────────────────────────────────────
_BACKEND_DIR   = Path("C:/WATCH-DOG/app/backend")
AI_MODELS_DIR  = _BACKEND_DIR / "ai_models"    # per-model uploads & data
TRAINING_DIR   = _BACKEND_DIR / "training_data" # existing bot tick/trade/session data

router = APIRouter(prefix="/api/ai-models", tags=["ai-models"])

ALLOWED_EXTS = {".csv", ".json", ".jsonl", ".txt", ".pdf", ".xlsx", ".xls"}


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas (inline — light dependency footprint)
# ─────────────────────────────────────────────────────────────────────────────

class ModelCreate(BaseModel):
    name:        str
    description: Optional[str] = ""

class ModelUpdate(BaseModel):
    name:               Optional[str]   = None
    description:        Optional[str]   = None
    connected_bot_ids:  Optional[list]  = None
    live_sync:          Optional[bool]  = None
    training_mode:      Optional[str]   = None
    training_frequency: Optional[str]   = None
    data_weight:        Optional[str]   = None
    learn_risk:         Optional[bool]  = None

class FileOut(BaseModel):
    id:            int
    original_name: str
    file_type:     Optional[str]
    size_bytes:    int
    record_count:  int
    created_at:    datetime
    class Config:
        from_attributes = True

class RunOut(BaseModel):
    id:           int
    started_at:   datetime
    completed_at: Optional[datetime]
    duration_sec: Optional[float]
    status:       str
    data_summary: Optional[dict]
    performance:  Optional[dict]
    error_msg:    Optional[str]
    class Config:
        from_attributes = True

class ModelOut(BaseModel):
    id:                 int
    name:               str
    description:        str
    connected_bot_ids:  list
    status:             str
    total_data_points:  int
    last_trained_at:    Optional[datetime]
    created_at:         datetime
    live_sync:          bool
    training_mode:      str
    training_frequency: str
    data_weight:        str
    learn_risk:         bool
    trades_since_train: int
    files:              List[FileOut]
    training_runs:      List[RunOut]
    class Config:
        from_attributes = True


# ─────────────────────────────────────────────────────────────────────────────
# Filesystem helpers
# ─────────────────────────────────────────────────────────────────────────────

def _model_uploads_dir(model_id: int) -> Path:
    d = AI_MODELS_DIR / str(model_id) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _model_runs_dir(model_id: int) -> Path:
    d = AI_MODELS_DIR / str(model_id) / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _load_jsonl(path: Path) -> list:
    """Read all *.jsonl files under path and return list of dicts."""
    records = []
    if not path.exists():
        return records
    for f in sorted(path.glob("*.jsonl")):
        try:
            with f.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            records.append(obj)
        except Exception:
            pass
    return records


def _parse_uploaded_file(filepath: Path, original_name: str) -> list:
    """Parse an uploaded file into a list of dicts. Returns (records, count)."""
    ext = Path(original_name).suffix.lower()
    try:
        if ext in (".jsonl",):
            records = []
            with filepath.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            records.append(obj)
            return records

        if ext == ".json":
            raw = filepath.read_text("utf-8")
            data = json.loads(raw)
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
            if isinstance(data, dict):
                return [data]
            return []

        if ext == ".csv":
            raw = filepath.read_text("utf-8", errors="replace")
            reader  = csv.DictReader(io.StringIO(raw))
            records = []
            for row in reader:
                clean = {}
                for k, v in row.items():
                    if v in ("", None):
                        clean[k] = None
                    else:
                        try:
                            clean[k] = float(v) if "." in v else int(v)
                        except (ValueError, TypeError):
                            clean[k] = v
                records.append(clean)
            return records

        if ext in (".xlsx", ".xls"):
            try:
                import pandas as pd
                df = pd.read_excel(filepath)
                return df.to_dict("records")
            except ImportError:
                return []

        # txt / pdf / other — treat as 1 document record (no rows to count)
        return []

    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Training engine — runs in a background thread
# ─────────────────────────────────────────────────────────────────────────────

def _analyze_data(trades: list, ticks: list, sessions: list,
                  uploaded_records: list, model) -> dict:
    """
    Comprehensive trading analysis using pure Python (+ pandas if installed).
    Returns a structured performance dict.
    """
    result: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overview":      {},
        "by_side":       {},
        "timing":        {},
        "risk":          {},
        "position":      {},
        "patterns":      {},
        "recommendations": [],
    }

    # ── Overview from sessions ─────────────────────────────────────────────────
    if sessions:
        profitable    = [s for s in sessions if s.get("profitable")]
        total_pnl     = sum(float(s.get("total_pnl", 0) or 0) for s in sessions)
        win_rate      = len(profitable) / len(sessions) * 100
        session_pnls  = [float(s.get("total_pnl", 0) or 0) for s in sessions]
        result["overview"] = {
            "total_sessions":       len(sessions),
            "winning_sessions":     len(profitable),
            "win_rate":             round(win_rate, 1),
            "total_pnl":            round(total_pnl, 2),
            "avg_pnl_per_session":  round(total_pnl / len(sessions), 2),
            "best_session":         round(max(session_pnls), 2),
            "worst_session":        round(min(session_pnls), 2),
            "pnl_std_dev":          round(_std(session_pnls), 2),
        }
    else:
        result["overview"] = {
            "total_sessions": 0, "winning_sessions": 0,
            "win_rate": 0, "total_pnl": 0,
        }

    # ── By-side breakdown (YES vs NO on Kalshi) ────────────────────────────────
    yes_sess = [s for s in sessions if s.get("session_result") == "YES"]
    no_sess  = [s for s in sessions if s.get("session_result") == "NO"]
    def _side_stats(ss):
        if not ss:
            return {"count": 0, "win_rate": 0, "total_pnl": 0}
        wins = [s for s in ss if s.get("profitable")]
        pnl  = sum(float(s.get("total_pnl", 0) or 0) for s in ss)
        return {
            "count":    len(ss),
            "win_rate": round(len(wins) / len(ss) * 100, 1),
            "total_pnl": round(pnl, 2),
            "avg_pnl":  round(pnl / len(ss), 2),
        }
    result["by_side"] = {
        "yes": _side_stats(yes_sess),
        "no":  _side_stats(no_sess),
        "preferred_side": (
            "YES" if yes_sess and (not no_sess or
                len([s for s in yes_sess if s.get("profitable")]) / len(yes_sess) >
                len([s for s in no_sess  if s.get("profitable")]) / len(no_sess))
            else "NO" if no_sess else "NONE"
        ),
    }

    # ── Risk analysis from trades ──────────────────────────────────────────────
    pnls = [float(t.get("pnl", 0) or 0) for t in trades if t.get("pnl") is not None]
    if model.learn_risk and pnls:
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        avg_win  = sum(wins)   / len(wins)   if wins   else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        result["risk"] = {
            "total_pnl":      round(sum(pnls), 2),
            "avg_win":        round(avg_win, 2),
            "avg_loss":       round(avg_loss, 2),
            "win_loss_ratio": round(abs(avg_win / avg_loss), 2) if avg_loss else 0,
            "max_gain":       round(max(pnls), 2),
            "max_drawdown":   round(min(pnls), 2),
            "profit_factor":  round(sum(wins) / abs(sum(losses)), 2) if losses and wins else 0,
            "total_wins":     len(wins),
            "total_losses":   len(losses),
            "expectancy":     round(sum(pnls) / len(pnls), 2),
        }

    # ── Position sizing from trades ────────────────────────────────────────────
    qtys = [float(t.get("quantity", 0) or t.get("contracts", 0) or 0)
            for t in trades if (t.get("quantity") or t.get("contracts"))]
    if qtys:
        result["position"] = {
            "avg_size":   round(sum(qtys) / len(qtys), 1),
            "min_size":   round(min(qtys), 1),
            "max_size":   round(max(qtys), 1),
            "optimal_size_suggestion": round(
                sum(qtys) / len(qtys) * (1.1 if result["overview"].get("win_rate", 0) > 55 else 0.9), 1
            ),
        }

    # ── Timing patterns ───────────────────────────────────────────────────────
    timed = [s for s in sessions if s.get("session_start") or s.get("ts")]
    if timed:
        hours_count: dict = {}
        for s in timed:
            ts_str = s.get("session_start") or s.get("ts") or ""
            try:
                hour = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).hour
                if s.get("profitable"):
                    hours_count[hour] = hours_count.get(hour, {"wins": 0, "total": 0})
                    hours_count[hour]["wins"]  += 1
                    hours_count[hour]["total"] += 1
                else:
                    hours_count[hour] = hours_count.get(hour, {"wins": 0, "total": 0})
                    hours_count[hour]["total"] += 1
            except Exception:
                pass
        best_hour = max(hours_count, key=lambda h: hours_count[h]["wins"] / hours_count[h]["total"]
                        if hours_count[h]["total"] > 0 else 0, default=None)
        result["timing"] = {
            "by_hour": {str(k): v for k, v in hours_count.items()},
            "best_hour":  best_hour,
            "total_sessions_with_ts": len(timed),
        }

    # ── Uploaded data insights ────────────────────────────────────────────────
    if uploaded_records:
        result["patterns"]["uploaded_records"] = len(uploaded_records)
        # Try to detect numeric columns and summarize
        if uploaded_records:
            sample = uploaded_records[0]
            num_cols = [k for k, v in sample.items() if isinstance(v, (int, float))][:5]
            col_stats = {}
            for col in num_cols:
                vals = [r[col] for r in uploaded_records if isinstance(r.get(col), (int, float))]
                if vals:
                    col_stats[col] = {
                        "count": len(vals),
                        "mean":  round(sum(vals) / len(vals), 4),
                        "min":   round(min(vals), 4),
                        "max":   round(max(vals), 4),
                    }
            result["patterns"]["column_summary"] = col_stats

    # ── Recommendations ────────────────────────────────────────────────────────
    recs = []
    ov = result["overview"]
    wr = ov.get("win_rate", 0)

    if wr > 0:
        if wr >= 60:
            recs.append({"type": "positive", "msg": f"Strong {wr}% win rate — current strategy is performing well."})
        elif wr >= 50:
            recs.append({"type": "neutral",  "msg": f"{wr}% win rate — marginally profitable. Look for higher-conviction setups."})
        elif wr >= 40:
            recs.append({"type": "warning",  "msg": f"{wr}% win rate — below break-even. Consider tightening entry criteria."})
        else:
            recs.append({"type": "danger",   "msg": f"Critical: {wr}% win rate — strategy needs fundamental review."})

    risk = result.get("risk", {})
    if risk.get("win_loss_ratio", 0) > 0:
        rlr = risk["win_loss_ratio"]
        if rlr < 0.8:
            recs.append({"type": "warning", "msg": f"Risk/reward ratio {rlr} — winners are smaller than losers. Widen take-profit targets."})
        elif rlr > 1.5:
            recs.append({"type": "positive", "msg": f"Excellent risk/reward {rlr} — exits are well-timed."})

    by_side = result.get("by_side", {})
    yes_wr = by_side.get("yes", {}).get("win_rate", 0)
    no_wr  = by_side.get("no",  {}).get("win_rate", 0)
    if yes_wr > no_wr + 15 and by_side.get("yes", {}).get("count", 0) >= 5:
        recs.append({"type": "positive", "msg": f"YES side wins {yes_wr}% vs NO side {no_wr}% — apply YES bias."})
    elif no_wr > yes_wr + 15 and by_side.get("no", {}).get("count", 0) >= 5:
        recs.append({"type": "positive", "msg": f"NO side wins {no_wr}% vs YES side {yes_wr}% — apply NO bias."})

    pos = result.get("position", {})
    if pos.get("optimal_size_suggestion"):
        recs.append({"type": "neutral", "msg": f"Suggested position size: {pos['optimal_size_suggestion']} contracts (based on win rate)."})

    result["recommendations"] = recs
    return result


def _std(vals: list) -> float:
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5


def _run_training_job(model_id: int, user_id: int):
    """Background thread — collects data, analyzes, stores results in DB."""
    db = SessionLocal()
    run_id = None
    t_start = time.time()
    try:
        model = db.query(models.AIModel).filter(models.AIModel.id == model_id).first()
        if not model:
            return

        # Create training run record
        run = models.TrainingRun(model_id=model_id, user_id=user_id, status="running")
        db.add(run)
        model.status = "training"
        db.commit()
        db.refresh(run)
        run_id = run.id

        # ── 1. Collect bot trading data ──────────────────────────────────────
        all_trades, all_ticks, all_sessions = [], [], []
        bots_used = []

        for bot_id in (model.connected_bot_ids or []):
            bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
            bot_name = bot.name if bot else f"Bot #{bot_id}"
            safe_name = re.sub(r'[^a-z0-9_]', '_', bot_name.lower())

            folder = TRAINING_DIR / f"bot_{bot_id}_{safe_name}"
            if folder.exists():
                trades   = _load_jsonl(folder / "trades")
                ticks    = _load_jsonl(folder / "ticks")
                sessions = _load_jsonl(folder / "sessions")
                for rec in trades + ticks + sessions:
                    rec["_bot_id"]   = bot_id
                    rec["_bot_name"] = bot_name
                all_trades.extend(trades)
                all_ticks.extend(ticks)
                all_sessions.extend(sessions)
                bots_used.append({"id": bot_id, "name": bot_name,
                                   "trades": len(trades), "ticks": len(ticks),
                                   "sessions": len(sessions)})
            else:
                bots_used.append({"id": bot_id, "name": bot_name,
                                   "trades": 0, "ticks": 0, "sessions": 0,
                                   "note": "no training folder yet"})

        # ── 2. Apply data weighting ──────────────────────────────────────────
        weight = model.data_weight
        if weight == "recent":
            all_trades   = sorted(all_trades,   key=lambda t: t.get("ts", 0))[-1000:]
            all_sessions = sorted(all_sessions, key=lambda s: s.get("ts", ""))[-300:]
        elif weight == "historical":
            all_trades   = sorted(all_trades,   key=lambda t: t.get("ts", 0))[:1000]
            all_sessions = sorted(all_sessions, key=lambda s: s.get("ts", ""))[:300]

        # ── 3. Collect uploaded file data ────────────────────────────────────
        uploads_dir      = _model_uploads_dir(model_id)
        file_records     = db.query(models.ModelFile).filter(
            models.ModelFile.model_id == model_id).all()
        uploaded_records = []
        files_used       = []
        for mf in file_records:
            fp = uploads_dir / mf.filename
            if fp.exists():
                recs = _parse_uploaded_file(fp, mf.original_name)
                uploaded_records.extend(recs)
                files_used.append({"name": mf.original_name, "records": len(recs),
                                    "type": mf.file_type})

        # ── 4. Analyze ───────────────────────────────────────────────────────
        performance  = _analyze_data(all_trades, all_ticks, all_sessions,
                                     uploaded_records, model)
        total_points = (len(all_trades) + len(all_ticks) + len(all_sessions)
                        + len(uploaded_records))

        data_summary = {
            "total_trades":       len(all_trades),
            "total_ticks":        len(all_ticks),
            "total_sessions":     len(all_sessions),
            "uploaded_records":   len(uploaded_records),
            "total_data_points":  total_points,
            "bots_used":          bots_used,
            "files_used":         files_used,
            "data_weight":        weight,
            "training_mode":      model.training_mode,
        }

        duration = round(time.time() - t_start, 2)

        # ── 5. Persist ───────────────────────────────────────────────────────
        run.status       = "completed"
        run.completed_at = datetime.now(timezone.utc)
        run.duration_sec = duration
        run.data_summary = data_summary
        run.performance  = performance
        model.status            = "ready"
        model.last_trained_at   = datetime.now(timezone.utc)
        model.total_data_points = total_points
        model.trades_since_train = 0
        db.commit()

    except Exception as exc:
        duration = round(time.time() - t_start, 2)
        try:
            model = db.query(models.AIModel).filter(models.AIModel.id == model_id).first()
            if model:
                model.status = "error"
            if run_id:
                run = db.query(models.TrainingRun).filter(
                    models.TrainingRun.id == run_id).first()
                if run:
                    run.status       = "failed"
                    run.completed_at = datetime.now(timezone.utc)
                    run.duration_sec = duration
                    run.error_msg    = str(exc)
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# CRUD endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[ModelOut])
def list_models(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return (db.query(models.AIModel)
            .filter(models.AIModel.user_id == user.id)
            .order_by(models.AIModel.created_at.desc())
            .all())


@router.post("/", response_model=ModelOut, status_code=201)
def create_model(data: ModelCreate, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    m = models.AIModel(user_id=user.id, name=data.name,
                       description=data.description or "")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


@router.get("/{model_id}", response_model=ModelOut)
def get_model(model_id: int, db: Session = Depends(get_db),
              user=Depends(get_current_user)):
    m = db.query(models.AIModel).filter(
        models.AIModel.id == model_id,
        models.AIModel.user_id == user.id).first()
    if not m:
        raise HTTPException(404, "Model not found")
    return m


@router.put("/{model_id}", response_model=ModelOut)
def update_model(model_id: int, data: ModelUpdate,
                 db: Session = Depends(get_db), user=Depends(get_current_user)):
    m = db.query(models.AIModel).filter(
        models.AIModel.id == model_id,
        models.AIModel.user_id == user.id).first()
    if not m:
        raise HTTPException(404, "Model not found")
    for field, val in data.model_dump(exclude_none=True).items():
        setattr(m, field, val)
    db.commit()
    db.refresh(m)
    return m


@router.delete("/{model_id}", status_code=204)
def delete_model(model_id: int, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    """Delete the model record.  Uploaded files are removed; bot training data is kept."""
    m = db.query(models.AIModel).filter(
        models.AIModel.id == model_id,
        models.AIModel.user_id == user.id).first()
    if not m:
        raise HTTPException(404, "Model not found")
    # Remove uploaded files from disk
    uploads_dir = AI_MODELS_DIR / str(model_id)
    if uploads_dir.exists():
        import shutil
        shutil.rmtree(uploads_dir, ignore_errors=True)
    db.delete(m)
    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# File upload / management
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{model_id}/upload")
async def upload_file(
    model_id:  int,
    file:      UploadFile = File(...),
    db:        Session    = Depends(get_db),
    user=Depends(get_current_user),
):
    m = db.query(models.AIModel).filter(
        models.AIModel.id == model_id,
        models.AIModel.user_id == user.id).first()
    if not m:
        raise HTTPException(404, "Model not found")

    original = file.filename or "upload"
    ext      = Path(original).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, f"File type '{ext}' not allowed. "
                            f"Supported: {', '.join(sorted(ALLOWED_EXTS))}")

    content   = await file.read()
    disk_name = f"{uuid.uuid4().hex}{ext}"
    dest      = _model_uploads_dir(model_id) / disk_name
    dest.write_bytes(content)

    # Parse to count records
    records      = _parse_uploaded_file(dest, original)
    record_count = len(records)

    mf = models.ModelFile(
        model_id      = model_id,
        user_id       = user.id,
        filename      = disk_name,
        original_name = original,
        file_type     = ext.lstrip("."),
        size_bytes    = len(content),
        record_count  = record_count,
    )
    db.add(mf)
    db.commit()
    db.refresh(mf)
    return {"id": mf.id, "original_name": original, "record_count": record_count,
            "size_bytes": len(content), "file_type": ext.lstrip(".")}


@router.delete("/{model_id}/files/{file_id}", status_code=204)
def delete_file(model_id: int, file_id: int,
                db: Session = Depends(get_db), user=Depends(get_current_user)):
    mf = db.query(models.ModelFile).filter(
        models.ModelFile.id == file_id,
        models.ModelFile.model_id == model_id).first()
    if not mf:
        raise HTTPException(404, "File not found")
    fp = _model_uploads_dir(model_id) / mf.filename
    if fp.exists():
        fp.unlink(missing_ok=True)
    db.delete(mf)
    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Training
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{model_id}/train")
def train_model(model_id: int, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    m = db.query(models.AIModel).filter(
        models.AIModel.id == model_id,
        models.AIModel.user_id == user.id).first()
    if not m:
        raise HTTPException(404, "Model not found")
    if m.status == "training":
        raise HTTPException(400, "Model is already training")

    threading.Thread(target=_run_training_job, args=(model_id, user.id),
                     daemon=True).start()
    return {"message": "Training started", "model_id": model_id}


@router.get("/{model_id}/runs", response_model=List[RunOut])
def list_runs(model_id: int, db: Session = Depends(get_db),
              user=Depends(get_current_user)):
    m = db.query(models.AIModel).filter(
        models.AIModel.id == model_id,
        models.AIModel.user_id == user.id).first()
    if not m:
        raise HTTPException(404, "Model not found")
    return (db.query(models.TrainingRun)
            .filter(models.TrainingRun.model_id == model_id)
            .order_by(models.TrainingRun.started_at.desc())
            .all())


@router.delete("/{model_id}/runs/{run_id}", status_code=204)
def delete_run(model_id: int, run_id: int, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    m = db.query(models.AIModel).filter(
        models.AIModel.id == model_id,
        models.AIModel.user_id == user.id).first()
    if not m:
        raise HTTPException(404, "Model not found")
    run = db.query(models.TrainingRun).filter(
        models.TrainingRun.id == run_id,
        models.TrainingRun.model_id == model_id).first()
    if not run:
        raise HTTPException(404, "Run not found")
    db.delete(run)
    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Live sync — called by the trade recorder whenever a new trade is logged
# ─────────────────────────────────────────────────────────────────────────────

def notify_trade(bot_id: int, trade_record: dict):
    """
    Called from bots.py when a trade is logged and live_sync is enabled.
    Increments trades_since_train and auto-triggers training if threshold met.
    """
    db = SessionLocal()
    try:
        live_models = (db.query(models.AIModel)
                       .filter(models.AIModel.live_sync == True)
                       .all())
        for m in live_models:
            if bot_id in (m.connected_bot_ids or []):
                m.trades_since_train = (m.trades_since_train or 0) + 1
                db.commit()

                freq = m.training_frequency
                tst  = m.trades_since_train
                should_train = (
                    (freq == "every_25" and tst >= 25) or
                    (freq == "every_50" and tst >= 50)
                )
                if should_train and m.status != "training":
                    threading.Thread(target=_run_training_job,
                                     args=(m.id, m.user_id), daemon=True).start()
    except Exception:
        pass
    finally:
        db.close()
