"""
chat.py — Community Chat system

Real-time WebSocket + REST API.
  • Group chat  → recipient_id is None  (broadcast to all)
  • Private DMs → recipient_id is set   (routed to specific user)

No external services required — pure FastAPI + SQLite.
"""

import uuid
import logging
from pathlib import Path
from typing import Dict, Optional, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from fastapi import UploadFile, File, Form, Query
from fastapi.responses import FileResponse
from sqlalchemy import or_, and_

from ..database import SessionLocal
from .. import models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# ── Upload directories ────────────────────────────────────────────────────────
_BACKEND = Path(__file__).parent.parent.parent   # app/backend/
CHAT_DIR   = _BACKEND / "uploads" / "chat"
AVATAR_DIR = _BACKEND / "uploads" / "avatars"
CHAT_DIR.mkdir(parents=True, exist_ok=True)
AVATAR_DIR.mkdir(parents=True, exist_ok=True)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# ── WebSocket connection manager ──────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self._sockets: Dict[str, WebSocket] = {}
        self._users:   Dict[str, dict]      = {}

    async def connect(self, user_id: str, info: dict, ws: WebSocket):
        await ws.accept()
        self._sockets[user_id] = ws
        self._users[user_id]   = {**info, "online": True}

    def disconnect(self, user_id: str):
        self._sockets.pop(user_id, None)
        if user_id in self._users:
            self._users[user_id]["online"] = False

    async def send_to(self, user_id: str, data: dict) -> bool:
        ws = self._sockets.get(user_id)
        if not ws:
            return False
        try:
            await ws.send_json(data)
            return True
        except Exception:
            self.disconnect(user_id)
            return False

    async def broadcast(self, data: dict, exclude: Optional[str] = None):
        dead: List[str] = []
        for uid, ws in list(self._sockets.items()):
            if uid == exclude:
                continue
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(uid)
        for uid in dead:
            self.disconnect(uid)

    def online_users(self) -> List[dict]:
        return [u for u in self._users.values() if u.get("online")]

    def all_users(self) -> List[dict]:
        return list(self._users.values())


manager = ConnectionManager()


# ── Serializer ────────────────────────────────────────────────────────────────
def _to_dict(m: models.ChatMessage) -> dict:
    return {
        "id":            m.id,
        "sender_id":     m.sender_id,
        "sender_name":   m.sender_name,
        "sender_avatar": m.sender_avatar,
        "recipient_id":  m.recipient_id,
        "content":       m.content,
        "message_type":  m.message_type,
        "file_url":      f"/api/chat/file/{m.file_name}" if m.file_name else None,
        "file_original": m.file_original,
        "created_at":    m.created_at.isoformat() if m.created_at else None,
    }


# ── REST — message history ────────────────────────────────────────────────────
@router.get("/messages/group")
def get_group_messages(limit: int = 150):
    db = SessionLocal()
    try:
        rows = (db.query(models.ChatMessage)
                .filter(models.ChatMessage.recipient_id == None)   # noqa: E711
                .order_by(models.ChatMessage.created_at.desc())
                .limit(limit).all())
        return [_to_dict(r) for r in reversed(rows)]
    finally:
        db.close()


@router.get("/messages/dm/{other_id}")
def get_dm_messages(other_id: str, me: str, limit: int = 150):
    db = SessionLocal()
    try:
        rows = (db.query(models.ChatMessage)
                .filter(or_(
                    and_(models.ChatMessage.sender_id == me,
                         models.ChatMessage.recipient_id == other_id),
                    and_(models.ChatMessage.sender_id == other_id,
                         models.ChatMessage.recipient_id == me),
                ))
                .order_by(models.ChatMessage.created_at.desc())
                .limit(limit).all())
        return [_to_dict(r) for r in reversed(rows)]
    finally:
        db.close()


@router.get("/users/online")
def get_online_users():
    return manager.online_users()


@router.get("/conversations/{user_id}")
def get_conversations(user_id: str):
    """Return list of users who have DM'd this user (for sidebar)."""
    db = SessionLocal()
    try:
        rows = (db.query(models.ChatMessage)
                .filter(
                    models.ChatMessage.recipient_id != None,   # noqa: E711
                    or_(models.ChatMessage.sender_id == user_id,
                        models.ChatMessage.recipient_id == user_id),
                )
                .order_by(models.ChatMessage.created_at.desc())
                .all())
        seen: Dict[str, dict] = {}
        for m in rows:
            other_id   = m.recipient_id if m.sender_id == user_id else m.sender_id
            other_name = m.sender_name  if m.sender_id != user_id else "(you)"
            if other_id not in seen:
                seen[other_id] = {
                    "user_id":      other_id,
                    "username":     other_name,
                    "last_message": (m.content or "📎 media")[:60],
                    "last_time":    m.created_at.isoformat() if m.created_at else None,
                }
        return list(seen.values())
    finally:
        db.close()


# ── REST — avatar ─────────────────────────────────────────────────────────────
@router.post("/avatar")
async def upload_avatar(user_id: str = Form(...), file: UploadFile = File(...)):
    original = file.filename or "avatar.jpg"
    ext = Path(original).suffix.lower() or ".jpg"
    if ext not in _IMAGE_EXTS:
        raise HTTPException(400, "Only image files accepted")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(400, "Max 5 MB")
    fname = f"{user_id}{ext}"
    (AVATAR_DIR / fname).write_bytes(data)
    url = f"/api/chat/avatar/{fname}"
    # Patch in-memory user info so it reflects in online list immediately
    if user_id in manager._users:
        manager._users[user_id]["avatar"] = url
    return {"avatar_url": url}


@router.get("/avatar/{filename}")
def serve_avatar(filename: str):
    p = AVATAR_DIR / filename
    if not p.exists():
        raise HTTPException(404, "Avatar not found")
    return FileResponse(str(p))


# ── REST — chat file / image upload ──────────────────────────────────────────
@router.post("/upload")
async def upload_chat_file(file: UploadFile = File(...)):
    original = file.filename or "file.bin"
    ext = Path(original).suffix.lower() or ".bin"
    allowed = _IMAGE_EXTS | {".mp4", ".pdf", ".txt", ".csv"}
    if ext not in allowed:
        raise HTTPException(400, "File type not allowed")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Max 20 MB")
    fname = f"{uuid.uuid4().hex}{ext}"
    (CHAT_DIR / fname).write_bytes(data)
    return {
        "file_url":      f"/api/chat/file/{fname}",
        "file_name":     fname,
        "original_name": original,
        "is_image":      ext in _IMAGE_EXTS,
    }


@router.get("/file/{filename}")
def serve_chat_file(filename: str):
    p = CHAT_DIR / filename
    if not p.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(p))


# ── WebSocket ─────────────────────────────────────────────────────────────────
@router.websocket("/ws/{user_id}")
async def websocket_endpoint(
    ws:       WebSocket,
    user_id:  str,
    username: str           = Query(default="User"),
    avatar:   Optional[str] = Query(default=None),
):
    info = {"id": user_id, "username": username, "avatar": avatar}
    await manager.connect(user_id, info, ws)

    # Greet the new connection
    await ws.send_json({
        "type":         "init",
        "online_users": manager.online_users(),
    })
    # Announce to others
    await manager.broadcast(
        {"type": "user_online", "user": info, "online_users": manager.online_users()},
        exclude=user_id,
    )

    try:
        while True:
            data = await ws.receive_json()
            ev   = data.get("type", "")

            # ── Text or media message ─────────────────────────────────────────
            if ev in ("group_message", "dm"):
                db = SessionLocal()
                try:
                    msg = models.ChatMessage(
                        id            = str(uuid.uuid4()),
                        sender_id     = user_id,
                        sender_name   = data.get("sender_name", username),
                        sender_avatar = data.get("sender_avatar", avatar),
                        recipient_id  = data.get("recipient_id"),   # None = group
                        content       = data.get("content", ""),
                        message_type  = data.get("message_type", "text"),
                        file_name     = data.get("file_name"),
                        file_original = data.get("file_original"),
                    )
                    db.add(msg)
                    db.commit()
                    db.refresh(msg)
                    payload = _to_dict(msg)
                finally:
                    db.close()

                if ev == "group_message":
                    await manager.broadcast({"type": "group_message", "message": payload})
                else:
                    rid = data.get("recipient_id")
                    if rid:
                        await manager.send_to(rid, {"type": "dm", "message": payload})
                    # Always echo back to sender
                    await manager.send_to(user_id, {"type": "dm", "message": payload})

            # ── Typing indicator ──────────────────────────────────────────────
            elif ev == "typing":
                room = data.get("room", "group")
                if room == "group":
                    await manager.broadcast(
                        {"type": "typing", "user_id": user_id, "username": username, "room": "group"},
                        exclude=user_id,
                    )
                else:
                    rid = data.get("recipient_id")
                    if rid:
                        await manager.send_to(rid, {
                            "type": "typing", "user_id": user_id,
                            "username": username, "room": "dm",
                        })

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("Chat WS error user=%s: %s", user_id, exc)
    finally:
        manager.disconnect(user_id)
        await manager.broadcast({
            "type":         "user_offline",
            "user_id":      user_id,
            "online_users": manager.online_users(),
        })
