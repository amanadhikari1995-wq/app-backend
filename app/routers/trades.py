from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from typing import List

from ..database import get_db
from .. import models, schemas
from ..auth import get_default_user as get_current_user

router = APIRouter(prefix="/api/trades", tags=["trades"])


# ── Bot-authenticated endpoint (called from inside bot code) ──────────────────
@router.post("/record", response_model=schemas.TradeOut, status_code=201)
def record_trade(
    data: schemas.TradeCreate,
    x_bot_secret: str = Header(..., alias="X-Bot-Secret"),
    db: Session = Depends(get_db),
):
    """Called from within bot code to record a trade. Authenticated via the bot's secret token."""
    bot = db.query(models.Bot).filter(models.Bot.bot_secret == x_bot_secret).first()
    if not bot:
        raise HTTPException(401, "Invalid bot secret")
    trade = models.Trade(bot_id=bot.id, user_id=bot.user_id, **data.model_dump())
    db.add(trade)
    db.commit()
    db.refresh(trade)
    # ── Notify AI Lab so live-sync models receive the trade ───────────────────
    try:
        from .ai_models import notify_trade as _notify_trade
        _notify_trade(bot.id, data.model_dump())
    except Exception:
        pass
    return trade


# ── User-authenticated endpoints (called from frontend) ───────────────────────
@router.get("/", response_model=List[schemas.TradeOut])
def list_trades(
    bot_id: int,
    limit: int = 500,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id, models.Bot.user_id == user.id).first()
    if not bot:
        raise HTTPException(404, "Bot not found")
    return (db.query(models.Trade)
            .filter(models.Trade.bot_id == bot_id)
            .order_by(models.Trade.created_at.desc())
            .limit(limit)
            .all())


@router.get("/stats", response_model=schemas.TradeStats)
def trade_stats(
    bot_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id, models.Bot.user_id == user.id).first()
    if not bot:
        raise HTTPException(404, "Bot not found")

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
    bot_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id, models.Bot.user_id == user.id).first()
    if not bot:
        raise HTTPException(404, "Bot not found")
    db.query(models.Trade).filter(models.Trade.bot_id == bot_id).delete()
    db.commit()


@router.delete("/{trade_id}", status_code=204)
def delete_trade(
    trade_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    trade = (db.query(models.Trade)
             .join(models.Bot)
             .filter(models.Trade.id == trade_id, models.Bot.user_id == user.id)
             .first())
    if not trade:
        raise HTTPException(404, "Trade not found")
    db.delete(trade)
    db.commit()
