from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from ..database import get_db
from .. import models, schemas
from ..auth import get_default_user as get_current_user
from ..bot_manager import get_bot          # ← per-bot filesystem isolation


def _sync_bot_connections(bot_id: int, db: Session) -> None:
    """
    Re-read all active connections for bot_id from DB and write them
    to that bot's isolated folder.  Called after any add or delete.
    """
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
    if not bot:
        return
    conns = (db.query(models.ApiConnection)
             .filter(models.ApiConnection.bot_id == bot_id,
                     models.ApiConnection.is_active == True)
             .all())
    get_bot(bot_id, bot.name).sync_connections([
        {
            "id":         c.id,
            "name":       c.name,
            "base_url":   c.base_url,
            "api_key":    c.api_key,
            "api_secret": c.api_secret,
        }
        for c in conns
    ])

router = APIRouter(prefix="/api/connections", tags=["api_connections"])


@router.get("/", response_model=List[schemas.ApiConnectionOut])
def list_connections(
    bot_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    q = (db.query(models.ApiConnection)
         .filter(models.ApiConnection.user_id == user.id,
                 models.ApiConnection.is_active == True))
    if bot_id is not None:
        q = q.filter(models.ApiConnection.bot_id == bot_id)
    return q.order_by(models.ApiConnection.created_at.desc()).all()


@router.post("/", response_model=schemas.ApiConnectionOut, status_code=201)
def create_connection(
    data: schemas.ApiConnectionCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if data.bot_id is not None:
        bot = db.query(models.Bot).filter(
            models.Bot.id == data.bot_id, models.Bot.user_id == user.id
        ).first()
        if not bot:
            raise HTTPException(404, "Bot not found")
    conn = models.ApiConnection(user_id=user.id, **data.model_dump())
    db.add(conn)
    db.commit()
    db.refresh(conn)
    # ── Mirror all connections for this bot to its isolated folder ────────────
    if data.bot_id is not None:
        _sync_bot_connections(data.bot_id, db)
    return conn


@router.put("/{conn_id}", response_model=schemas.ApiConnectionOut)
def update_connection(
    conn_id: int,
    data: schemas.ApiConnectionCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    conn = (db.query(models.ApiConnection)
            .join(models.Bot, models.ApiConnection.bot_id == models.Bot.id)
            .filter(models.ApiConnection.id == conn_id,
                    models.Bot.user_id == user.id,
                    models.ApiConnection.is_active == True)
            .first())
    if not conn:
        raise HTTPException(404, "Connection not found")
    for field, value in data.model_dump().items():
        setattr(conn, field, value)
    db.commit()
    db.refresh(conn)
    if conn.bot_id is not None:
        _sync_bot_connections(conn.bot_id, db)
    return conn


@router.delete("/{conn_id}", status_code=204)
def delete_connection(
    conn_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    conn = (db.query(models.ApiConnection)
            .join(models.Bot, models.ApiConnection.bot_id == models.Bot.id)
            .filter(models.ApiConnection.id == conn_id, models.Bot.user_id == user.id)
            .first())
    if not conn:
        raise HTTPException(404, "Connection not found")
    bot_id_for_sync = conn.bot_id
    conn.is_active = False
    db.commit()
    # ── Re-sync the bot's folder with the updated (key removed) list ──────────
    if bot_id_for_sync is not None:
        _sync_bot_connections(bot_id_for_sync, db)
