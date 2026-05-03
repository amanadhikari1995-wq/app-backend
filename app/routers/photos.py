"""
Photos router — upload, list, update caption, delete.
Files stored in  <backend>/uploads/photos/
"""
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
import shutil, uuid, os

from ..database import get_db
from .. import models

router = APIRouter(prefix="/api/photos", tags=["photos"])

# Use WATCHDOG_DATA_DIR (set by run_backend.py in the bundled exe) so we
# write to %LOCALAPPDATA%\WatchDog\uploads\photos, NOT under Program Files
# where Windows denies write access. Fall back to the old __file__-based
# path in dev mode (env var unset).
_DATA_DIR = os.environ.get("WATCHDOG_DATA_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
UPLOAD_DIR = os.path.join(_DATA_DIR, "uploads", "photos")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}


def _row(p: models.Photo) -> dict:
    return {
        "id":            p.id,
        "filename":      p.filename,
        "original_name": p.original_name,
        "caption":       p.caption or "",
        "url":           f"/api/photos/{p.id}/image",
        "created_at":    p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/")
def list_photos(db: Session = Depends(get_db)):
    photos = db.query(models.Photo).order_by(models.Photo.created_at).all()
    return [_row(p) for p in photos]


@router.post("/")
def upload_photo(
    file: UploadFile = File(...),
    caption: str = Form(""),
    db: Session = Depends(get_db),
):
    if file.content_type not in ALLOWED:
        raise HTTPException(400, "Only image files are allowed")
    ext = os.path.splitext(file.filename or "img")[1] or ".jpg"
    stored = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(UPLOAD_DIR, stored)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    photo = models.Photo(
        filename=stored,
        original_name=file.filename or stored,
        caption=caption or None,
    )
    db.add(photo)
    db.commit()
    db.refresh(photo)
    return _row(photo)


@router.get("/{photo_id}/image")
def get_image(photo_id: int, db: Session = Depends(get_db)):
    photo = db.query(models.Photo).filter(models.Photo.id == photo_id).first()
    if not photo:
        raise HTTPException(404, "Photo not found")
    path = os.path.join(UPLOAD_DIR, photo.filename)
    if not os.path.exists(path):
        raise HTTPException(404, "File not found on disk")
    return FileResponse(path)


@router.put("/{photo_id}")
def update_caption(
    photo_id: int,
    caption: str = Form(""),
    db: Session = Depends(get_db),
):
    photo = db.query(models.Photo).filter(models.Photo.id == photo_id).first()
    if not photo:
        raise HTTPException(404, "Photo not found")
    photo.caption = caption
    db.commit()
    db.refresh(photo)
    return _row(photo)


@router.delete("/{photo_id}")
def delete_photo(photo_id: int, db: Session = Depends(get_db)):
    photo = db.query(models.Photo).filter(models.Photo.id == photo_id).first()
    if not photo:
        raise HTTPException(404, "Photo not found")
    path = os.path.join(UPLOAD_DIR, photo.filename)
    if os.path.exists(path):
        os.remove(path)
    db.delete(photo)
    db.commit()
    return {"ok": True}
