"""
routers/whop.py  —  Whop subscription authentication
=====================================================
Endpoints
─────────
  POST /api/auth/whop/verify   — Validate an access code with the Whop API,
                                  create/update the local user record, and
                                  return a signed JWT the frontend stores.

  GET  /api/auth/me            — Return the currently authenticated user
                                  (requires Bearer JWT from verify above).
"""

import os
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, schemas
from ..auth import create_access_token, get_current_user_jwt

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["whop-auth"])

# ── Whop config (set in backend/.env) ────────────────────────────────────────
WHOP_API_KEY     = os.getenv("WHOP_API_KEY", "")
WHOP_VALIDATE_URL = "https://api.whop.com/api/v2/memberships/validate_license"

# Optional: restrict to a specific Whop product/plan ID
# Leave empty to accept any active membership
WHOP_PRODUCT_ID  = os.getenv("WHOP_PRODUCT_ID", "")


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _call_whop_api(license_key: str) -> dict:
    """
    Hit Whop's validate_license endpoint.
    Returns the parsed JSON body on HTTP 200, otherwise raises HTTPException.
    """
    if not WHOP_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Whop API key not configured on server. Please contact support.",
        )

    headers = {
        "Authorization": f"Bearer {WHOP_API_KEY}",
        "Content-Type":  "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                WHOP_VALIDATE_URL,
                json={"full_license_key": license_key.strip()},
                headers=headers,
            )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Whop API timed out. Please try again.",
        )
    except httpx.RequestError as exc:
        log.error("Whop API network error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Whop verification service. Check your internet connection.",
        )

    if resp.status_code == 422 or resp.status_code == 404:
        # Whop returns 422 for malformed / unknown keys
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access code. Please check the code and try again.",
        )

    if resp.status_code != 200:
        log.error("Whop API unexpected status %d: %s", resp.status_code, resp.text[:300])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Whop verification service returned an unexpected error ({resp.status_code}).",
        )

    return resp.json()


def _extract_membership(data: dict) -> dict:
    """
    Parse the Whop response into a flat dict we can work with.
    Normalises nested user / plan objects into top-level keys.
    """
    user_obj = data.get("user") or {}
    plan_obj = data.get("plan") or {}

    return {
        "membership_id": data.get("id", ""),
        "valid":         data.get("valid", False),
        "status":        data.get("status", "unknown"),
        "license_key":   data.get("license_key") or data.get("full_license_key", ""),
        "plan_id":       plan_obj.get("id", ""),
        "plan_name":     plan_obj.get("name", ""),
        "whop_user_id":  user_obj.get("id", ""),
        "whop_email":    user_obj.get("email") or data.get("email", ""),
        "whop_username": user_obj.get("username", ""),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/whop/verify", response_model=schemas.WhopVerifyResponse)
async def verify_whop_access_code(
    body: schemas.WhopVerifyRequest,
    db: Session = Depends(get_db),
):
    """
    1. Validate the access code with Whop's API.
    2. Confirm the membership is active.
    3. Upsert a local WhopMembership record.
    4. Ensure a local User record exists (always user id=1 for single-user mode).
    5. Return a signed JWT + membership info.
    """
    if not body.license_key or not body.license_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Access code cannot be empty.",
        )

    # ── 1. Call Whop API ──────────────────────────────────────────────────────
    raw = await _call_whop_api(body.license_key)
    mem = _extract_membership(raw)

    # ── 2. Validate status ────────────────────────────────────────────────────
    if not mem["valid"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access code is not valid. Please subscribe at whop.com.",
        )

    if mem["status"] not in ("active", "trialing"):
        status_msgs = {
            "expired":  "Your subscription has expired. Please renew on Whop.",
            "canceled": "Your subscription has been cancelled. Please subscribe again on Whop.",
            "banned":   "This access code has been suspended.",
        }
        detail = status_msgs.get(
            mem["status"],
            f"Subscription status is '{mem['status']}'. Please contact support.",
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

    # Optional: restrict to a specific Whop product
    if WHOP_PRODUCT_ID and mem["plan_id"] and WHOP_PRODUCT_ID not in mem["plan_id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This access code is for a different product.",
        )

    # ── 3. Upsert WhopMembership ──────────────────────────────────────────────
    existing_mem = (
        db.query(models.WhopMembership)
        .filter(or_(
            models.WhopMembership.license_key   == mem["license_key"],
            models.WhopMembership.membership_id == mem["membership_id"],
        ))
        .first()
    )

    # Ensure the default user exists
    user = db.query(models.User).filter(models.User.id == 1).first()
    if not user:
        user = models.User(
            username=mem["whop_username"] or "watchdog",
            email=mem["whop_email"] or "watchdog@local",
            hashed_password="",
            is_active=True,
        )
        db.add(user)
        db.flush()   # get id

    # Update email/username from Whop if provided
    if mem["whop_email"] and user.email == "watchdog@local":
        user.email = mem["whop_email"]
    if mem["whop_username"] and user.username == "watchdog":
        user.username = mem["whop_username"]

    if existing_mem:
        # Refresh fields on every re-login
        existing_mem.status       = mem["status"]
        existing_mem.plan_name    = mem["plan_name"]
        existing_mem.whop_email   = mem["whop_email"]
        existing_mem.whop_username= mem["whop_username"]
        existing_mem.verified_at  = datetime.now(timezone.utc)
        membership_record = existing_mem
    else:
        membership_record = models.WhopMembership(
            user_id       = user.id,
            membership_id = mem["membership_id"],
            whop_user_id  = mem["whop_user_id"],
            license_key   = mem["license_key"] or body.license_key.strip(),
            status        = mem["status"],
            plan_name     = mem["plan_name"],
            whop_email    = mem["whop_email"],
            whop_username = mem["whop_username"],
            verified_at   = datetime.now(timezone.utc),
        )
        db.add(membership_record)

    db.commit()
    db.refresh(user)
    db.refresh(membership_record)

    # ── 4. Issue JWT ──────────────────────────────────────────────────────────
    token = create_access_token({
        "sub":                user.username,
        "user_id":            user.id,
        "whop_membership_id": membership_record.membership_id,
    })

    return schemas.WhopVerifyResponse(
        access_token=token,
        token_type="bearer",
        user=schemas.UserOut.model_validate(user),
        membership=schemas.WhopMembershipInfo.model_validate(membership_record),
    )


@router.get("/me", response_model=schemas.UserOut)
def get_me(current_user: models.User = Depends(get_current_user_jwt)):
    """
    Return the authenticated user's profile.
    Used by the frontend on startup to validate a stored JWT token.
    """
    return current_user


# ── Dev-only admin bypass ─────────────────────────────────────────────────────

@router.post("/admin-login")
def admin_login(db: Session = Depends(get_db)):
    """
    One-click login for local development/testing only.
    Disabled automatically when APP_ENV != 'development'.
    """
    if os.getenv("APP_ENV", "development") != "development":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin login is only available in development mode.",
        )

    # Get or create the default user
    user = db.query(models.User).filter(models.User.id == 1).first()
    if not user:
        from ..auth import ensure_default_user
        ensure_default_user()
        user = db.query(models.User).filter(models.User.id == 1).first()

    token = create_access_token({
        "sub":     user.username,
        "user_id": user.id,
        "role":    "admin",
    })

    return {"access_token": token, "token_type": "bearer", "user": {"username": user.username}}
