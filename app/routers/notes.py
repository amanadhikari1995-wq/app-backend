"""Notes router — simple CRUD, persisted in SQLite."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from ..database import get_db
from .. import models

router = APIRouter(prefix="/api/notes", tags=["notes"])


class NoteIn(BaseModel):
    title: Optional[str] = "Untitled"
    content: str = ""


def _row(n: models.Note) -> dict:
    return {
        "id":         n.id,
        "title":      n.title or "Untitled",
        "content":    n.content or "",
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
    }


@router.get("/")
def list_notes(db: Session = Depends(get_db)):
    notes = db.query(models.Note).order_by(models.Note.updated_at.desc()).all()
    return [_row(n) for n in notes]


@router.post("/")
def create_note(body: NoteIn, db: Session = Depends(get_db)):
    note = models.Note(title=body.title, content=body.content)
    db.add(note)
    db.commit()
    db.refresh(note)
    return _row(note)


@router.put("/{note_id}")
def update_note(note_id: int, body: NoteIn, db: Session = Depends(get_db)):
    note = db.query(models.Note).filter(models.Note.id == note_id).first()
    if not note:
        raise HTTPException(404, "Note not found")
    note.title = body.title or "Untitled"
    note.content = body.content
    db.commit()
    db.refresh(note)
    return _row(note)


@router.delete("/{note_id}")
def delete_note(note_id: int, db: Session = Depends(get_db)):
    note = db.query(models.Note).filter(models.Note.id == note_id).first()
    if not note:
        raise HTTPException(404, "Note not found")
    db.delete(note)
    db.commit()
    return {"ok": True}
