from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List

from ..database import get_db
from .. import models, schemas
from ..auth import get_default_user as get_current_user

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats", response_model=schemas.DashboardStats)
def get_stats(db: Session = Depends(get_db), user=Depends(get_current_user)):
    bots = db.query(models.Bot).filter(models.Bot.user_id == user.id).all()
    bot_ids = [b.id for b in bots]
    total_trades = 0
    if bot_ids:
        total_trades = db.query(models.Trade).filter(models.Trade.bot_id.in_(bot_ids)).count()
    recent_logs = (db.query(models.BotLog)
                   .filter(models.BotLog.user_id == user.id)
                   .order_by(models.BotLog.created_at.desc())
                   .limit(200)
                   .all())
    return {
        "total_bots": len(bots),
        "running_bots": sum(1 for b in bots if b.status == models.BotStatus.RUNNING),
        "total_runs": sum(b.run_count or 0 for b in bots),
        "total_trades": total_trades,
        "recent_logs": recent_logs,
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

    since_id=0  → return the latest `limit` logs (desc), for initial page load.
    since_id>0  → return only logs with id > since_id in asc order (new lines only).
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
