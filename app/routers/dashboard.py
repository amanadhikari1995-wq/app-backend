from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List

from ..database import get_db
from .. import models, schemas
from ..auth import get_default_user as get_current_user
from ..routers.bots import _processes as _bot_processes

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats", response_model=schemas.DashboardStats)
def get_stats(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """
    Stats for the desktop dashboard. Bots themselves live in Supabase, so
    `total_bots` and `total_runs` are reported by the website (closer to the
    source of truth). The backend reports only what it owns:
      - running_bots: live process count in this backend
      - total_trades: rows in the local trades buffer
      - recent_logs:  latest log lines from bot_logs
    """
    running_bots = len(_bot_processes)
    total_trades = db.query(models.Trade).filter(models.Trade.user_id == user.id).count()
    recent_logs = (db.query(models.BotLog)
                   .filter(models.BotLog.user_id == user.id)
                   .order_by(models.BotLog.created_at.desc())
                   .limit(200)
                   .all())
    return {
        "total_bots":   0,             # Supabase is source of truth
        "running_bots": running_bots,
        "total_runs":   0,             # Supabase is source of truth
        "total_trades": total_trades,
        "recent_logs":  recent_logs,
    }


@router.get("/logs", response_model=List[schemas.BotLogOut])
def get_recent_logs(
    since_id: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """
    Lightweight log-streaming endpoint used by the global Logs page.

    since_id=0  -> return the latest `limit` logs (desc), for initial page load.
    since_id>0  -> return only logs with id > since_id in asc order (new lines only).
    """
    q = db.query(models.BotLog).filter(models.BotLog.user_id == user.id)
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
