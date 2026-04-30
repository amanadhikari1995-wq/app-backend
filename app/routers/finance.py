"""Personal Finance Tracker router — CRUD for income/expense entries + summary."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from collections import defaultdict

from ..database import get_db
from .. import models

router = APIRouter(prefix="/api/finance", tags=["finance"])


class FinanceIn(BaseModel):
    entry_type: str          # "income" | "expense"
    amount: float
    category: str
    description: Optional[str] = ""
    date: str                # YYYY-MM-DD


def _row(e: models.FinanceEntry) -> dict:
    return {
        "id":          e.id,
        "entry_type":  e.entry_type,
        "amount":      e.amount,
        "category":    e.category,
        "description": e.description or "",
        "date":        e.date,
        "created_at":  e.created_at.isoformat() if e.created_at else None,
    }


@router.get("/")
def list_entries(db: Session = Depends(get_db)):
    entries = db.query(models.FinanceEntry).order_by(models.FinanceEntry.date.desc()).all()
    return [_row(e) for e in entries]


@router.post("/")
def create_entry(body: FinanceIn, db: Session = Depends(get_db)):
    if body.entry_type not in ("income", "expense"):
        raise HTTPException(400, "entry_type must be 'income' or 'expense'")
    e = models.FinanceEntry(**body.dict())
    db.add(e)
    db.commit()
    db.refresh(e)
    return _row(e)


@router.put("/{entry_id}")
def update_entry(entry_id: int, body: FinanceIn, db: Session = Depends(get_db)):
    e = db.query(models.FinanceEntry).filter(models.FinanceEntry.id == entry_id).first()
    if not e:
        raise HTTPException(404, "Entry not found")
    if body.entry_type not in ("income", "expense"):
        raise HTTPException(400, "entry_type must be 'income' or 'expense'")
    for k, v in body.dict().items():
        setattr(e, k, v)
    db.commit()
    db.refresh(e)
    return _row(e)


@router.delete("/{entry_id}")
def delete_entry(entry_id: int, db: Session = Depends(get_db)):
    e = db.query(models.FinanceEntry).filter(models.FinanceEntry.id == entry_id).first()
    if not e:
        raise HTTPException(404, "Entry not found")
    db.delete(e)
    db.commit()
    return {"ok": True}


@router.get("/summary")
def get_summary(db: Session = Depends(get_db)):
    """
    Returns overall totals and per-month breakdown.
    Response shape:
    {
      "total_income":   float,
      "total_expense":  float,
      "balance":        float,
      "by_month":       { "YYYY-MM": { "income": f, "expense": f, "balance": f } },
      "by_category":    { "category": { "income": f, "expense": f } }
    }
    """
    entries = db.query(models.FinanceEntry).all()

    total_income = 0.0
    total_expense = 0.0
    by_month: dict = defaultdict(lambda: {"income": 0.0, "expense": 0.0, "balance": 0.0})
    by_cat:   dict = defaultdict(lambda: {"income": 0.0, "expense": 0.0})

    for e in entries:
        month = e.date[:7] if e.date and len(e.date) >= 7 else "unknown"
        if e.entry_type == "income":
            total_income += e.amount
            by_month[month]["income"] += e.amount
            by_month[month]["balance"] += e.amount
            by_cat[e.category]["income"] += e.amount
        else:
            total_expense += e.amount
            by_month[month]["expense"] += e.amount
            by_month[month]["balance"] -= e.amount
            by_cat[e.category]["expense"] += e.amount

    return {
        "total_income":  round(total_income, 2),
        "total_expense": round(total_expense, 2),
        "balance":       round(total_income - total_expense, 2),
        "by_month":      dict(sorted(by_month.items(), reverse=True)),
        "by_category":   dict(by_cat),
    }
