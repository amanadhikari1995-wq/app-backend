"""
User Files router — upload, list, download, delete.
Files stored in  <backend>/uploads/files/
"""
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
import shutil, uuid, os

from ..database import get_db
from .. import models

router = APIRouter(prefix="/api/files", tags=["files"])

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "uploads", "files")
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_SIZE = 50 * 1024 * 1024  # 50 MB


def _row(f: models.UserFile) -> dict:
    return {
        "id":            f.id,
        "filename":      f.filename,
        "original_name": f.original_name,
        "mime_type":     f.mime_type or "",
        "size_bytes":    f.size_bytes or 0,
        "download_url":  f"/api/files/{f.id}/download",
        "created_at":    f.created_at.isoformat() if f.created_at else None,
    }


@router.get("/")
def list_files(db: Session = Depends(get_db)):
    files = db.query(models.UserFile).order_by(models.UserFile.created_at.desc()).all()
    return [_row(f) for f in files]


@router.post("/")
def upload_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    ext = os.path.splitext(file.filename or "file")[1] or ""
    stored = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(UPLOAD_DIR, stored)
    size = 0
    with open(dest, "wb") as out:
        while True:
            chunk = file.file.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_SIZE:
                out.close()
                os.remove(dest)
                raise HTTPException(400, "File exceeds 50 MB limit")
            out.write(chunk)
    rec = models.UserFile(
        filename=stored,
        original_name=file.filename or stored,
        mime_type=file.content_type,
        size_bytes=size,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return _row(rec)


@router.get("/{file_id}/download")
def download_file(file_id: int, db: Session = Depends(get_db)):
    rec = db.query(models.UserFile).filter(models.UserFile.id == file_id).first()
    if not rec:
        raise HTTPException(404, "File not found")
    path = os.path.join(UPLOAD_DIR, rec.filename)
    if not os.path.exists(path):
        raise HTTPException(404, "File not found on disk")
    return FileResponse(path, filename=rec.original_name, media_type=rec.mime_type or "application/octet-stream")


@router.delete("/{file_id}")
def delete_file(file_id: int, db: Session = Depends(get_db)):
    rec = db.query(models.UserFile).filter(models.UserFile.id == file_id).first()
    if not rec:
        raise HTTPException(404, "File not found")
    path = os.path.join(UPLOAD_DIR, rec.filename)
    if os.path.exists(path):
        os.remove(path)
    db.delete(rec)
    db.commit()
    return {"ok": True}
