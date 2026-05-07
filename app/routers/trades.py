from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from typing import List

from ..database import get_db
from .. import models, schemas
from ..auth import get_default_user as get_current_user
from .bots import get_bot_uuid_for_secret

router = APIRouter(prefix="/api/trades", tags=["trades"])


# ── Bot-authenticated endpoint (called from inside bot code) ──────────────────
@router.post("/record", response_model=schemas.TradeOut, status_code=201)
def record_trade(
    data: schemas.TradeCreate,
    x_bot_secret: str = Header(..., alias="X-Bot-Secret"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Called from within bot code to record a trade. Authenticated via the
    bot's secret token, which is generated per-run and held in memory by
    the bots router (no DB lookup)."""
    bot_uuid = get_bot_uuid_for_secret(x_bot_secret)
    if not bot_uuid:
        raise HTTPException(401, "Invalid bot secret")
    trade = models.Trade(bot_id=bot_uuid, user_id=user.id, **data.model_dump())
    db.add(trade)
    db.commit()
    db.refresh(trade)
    try:
        from .ai_models import notify_trade as _notify_trade
        _notify_trade(bot_uuid, data.model_dump())
    except Exception:
        pass
    return trade


# ── User-authenticated endpoints (called from frontend) ───────────────────────
@router.get("/", response_model=List[schemas.TradeOut])
def list_trades(
    bot_id: str,
    limit: int = 500,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return (db.query(models.Trade)
            .filter(models.Trade.bot_id == bot_id)
            .order_by(models.Trade.created_at.desc())
            .limit(limit)
            .all())


@router.get("/stats", response_model=schemas.TradeStats)
def trade_stats(
    bot_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    trades    = db.query(models.Trade).filter(models.Trade.bot_id == bot_id).all()
    pnl_list  = [t for t in trades if t.pnl is not None]
    winning   = [t for t in pnl_list if t.pnl > 0]
    losing    = [t for t in pnl_list if t.pnl <= 0]

    total_pnl     = sum(t.pnl for t in pnl_list)
    total_winning = sum(t.pnl for t in winning)
    total_losing  = abs(sum(t.pnl for t in losing))
    win_rate      = (len(winning) / len(pnl_list) * 100) if pnl_list else 0.0

    return schemas.TradeStats(
        total_trades=len(trades),
        winning_trades=len(winning),
        losing_trades=len(losing),
        win_rate=round(win_rate, 1),
        total_pnl=round(total_pnl, 2),
        total_winning=round(total_winning, 2),
        total_losing=round(total_losing, 2),
    )


@router.delete("/bot/{bot_id}", status_code=204)
def clear_bot_trades(
    bot_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    db.query(models.Trade).filter(models.Trade.bot_id == bot_id).delete()
    db.commit()


@router.delete("/{trade_id}", status_code=204)
def delete_trade(
    trade_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    trade = db.query(models.Trade).filter(models.Trade.id == trade_id,
                                          models.Trade.user_id == user.id).first()
    if not trade:
        raise HTTPException(404, "Trade not found")
    db.delete(trade)
    db.commit()
