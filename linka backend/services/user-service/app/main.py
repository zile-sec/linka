"""
Linka User Service
==================
Handles authentication (signup / login / logout / token refresh),
profile CRUD, and role-specific onboarding for:
  - customer
  - retailer  (SME)
  - driver    (delivery-maker)
"""

from __future__ import annotations

import os
import sys
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, field_validator

# ---------------------------------------------------------------------------
# Path setup  (packages dir is available via PYTHONPATH in Docker;
# this fallback keeps `python main.py` working locally too)
# ---------------------------------------------------------------------------
_pkg_dir = os.path.join(os.path.dirname(__file__), "../../../packages")
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

from shared.supabase_client import get_supabase_client
from shared.auth_middleware import (
    get_current_user,
    AuthenticatedUser,
    UserRole,
    require_roles,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("user-service")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Linka User Service",
    version="1.0.0",
    description="Authentication, profile management, and role-specific onboarding",
)

VALID_ROLES = {"customer", "retailer", "driver"}
PASSWORD_MIN_LENGTH = 8


# ---------------------------------------------------------------------------
# Global error handler  (catch-all so nothing leaks to clients)
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def _global_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred. Please try again later."},
    )


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------
class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    role: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    # SME-specific (only required when role == retailer)
    business_name: Optional[str] = None
    business_type: Optional[str] = None
    # Driver-specific (only required when role == driver)
    vehicle_type: Optional[str] = None
    license_number: Optional[str] = None

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
        return v

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        if len(v) < PASSWORD_MIN_LENGTH:
            raise ValueError(f"password must be at least {PASSWORD_MIN_LENGTH} characters")
        if not any(c.isdigit() for c in v):
            raise ValueError("password must contain at least one digit")
        if not any(c.isupper() for c in v):
            raise ValueError("password must contain at least one uppercase letter")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    business_name: Optional[str] = None
    business_type: Optional[str] = None
    vehicle_type: Optional[str] = None
    license_number: Optional[str] = None


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordUpdateRequest(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        if len(v) < PASSWORD_MIN_LENGTH:
            raise ValueError(f"password must be at least {PASSWORD_MIN_LENGTH} characters")
        return v


# ===================================================================
#  Small, reusable helper functions
# ===================================================================

def _build_user_meta(payload: SignupRequest) -> dict:
    """Build the Supabase `raw_user_meta_data` dict from a signup payload."""
    meta = {
        "role": payload.role,
        "full_name": payload.full_name or "",
    }
    if payload.role == "retailer":
        meta["business_name"] = payload.business_name or ""
        meta["business_type"] = payload.business_type or ""
    if payload.role == "driver":
        meta["vehicle_type"] = payload.vehicle_type or ""
        meta["license_number"] = payload.license_number or ""
    return meta


def _format_profile(profile: dict) -> dict:
    """Return a consistent, safe subset of profile fields."""
    return {
        "id": profile.get("id"),
        "email": profile.get("email"),
        "role": profile.get("role"),
        "full_name": profile.get("full_name"),
        "phone": profile.get("phone"),
        "avatar_url": profile.get("avatar_url"),
        "kyc_status": profile.get("kyc_status", "unverified"),
        "kyc_level": profile.get("kyc_level", 0),
        "is_active": profile.get("is_active", True),
        "last_login_at": profile.get("last_login_at"),
        "created_at": profile.get("created_at"),
        "updated_at": profile.get("updated_at"),
    }


def _supabase_signup(email: str, password: str, meta: dict):
    """Call Supabase auth.sign_up and return the response object.

    Raises HTTPException on failure so callers stay clean.
    """
    sb = get_supabase_client()
    try:
        resp = sb.client.auth.sign_up(
            {"email": email, "password": password, "options": {"data": meta}}
        )
    except Exception as exc:
        logger.error("supabase signup call failed: %s", exc)
        raise HTTPException(status_code=502, detail="Auth provider unavailable") from exc

    if resp.user is None:
        logger.warning("signup returned no user for %s", email)
        raise HTTPException(status_code=400, detail="Signup failed. The email may already be registered.")
    return resp


def _supabase_login(email: str, password: str):
    """Call Supabase auth.sign_in_with_password.

    Returns (user, session) or raises HTTPException.
    """
    sb = get_supabase_client()
    try:
        resp = sb.client.auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except Exception as exc:
        logger.error("supabase login call failed: %s", exc)
        raise HTTPException(status_code=502, detail="Auth provider unavailable") from exc

    if not resp.session:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return resp.user, resp.session


def _fetch_profile(user_id: str) -> dict:
    """Fetch a user_profiles row by id. Raises 404 when missing."""
    sb = get_supabase_client()
    profile = sb.get_single("user_profiles", {"id": user_id})
    if not profile:
        logger.warning("profile missing for user_id=%s", user_id)
        raise HTTPException(status_code=404, detail="User profile not found")
    return profile


def _patch_profile(user_id: str, fields: dict) -> dict:
    """Update specific columns on a user_profiles row."""
    sb = get_supabase_client()
    try:
        updated = sb.update("user_profiles", {"id": user_id}, fields)
        return updated
    except Exception as exc:
        logger.error("profile update failed for %s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to update profile") from exc


def _strip_none(data: dict) -> dict:
    """Remove keys whose value is None so we never overwrite with NULL."""
    return {k: v for k, v in data.items() if v is not None}


# ===================================================================
#  Health-check endpoints
# ===================================================================

@app.get("/health", tags=["ops"])
async def health():
    """Liveness probe -- always returns 200 if the process is up."""
    return {"status": "alive", "service": "user-service"}


@app.get("/ready", tags=["ops"])
async def readiness():
    """Readiness probe -- verifies Supabase connectivity."""
    try:
        sb = get_supabase_client()
        ok = sb.test_connection()
        if not ok:
            raise RuntimeError("connection test returned False")
        return {"status": "ready", "service": "user-service"}
    except Exception as exc:
        logger.error("readiness check failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Not ready: {exc}")


# ===================================================================
#  Signup
# ===================================================================

@app.post("/signup", tags=["auth"])
def signup(payload: SignupRequest):
    """Register a new user.

    Creates an auth.users row via Supabase Auth and (via the DB trigger)
    a matching user_profiles row.  Role-specific metadata is stored on
    the auth user so the trigger can copy it into the profile.
    """
    logger.info("signup started | email=%s role=%s", payload.email, payload.role)

    # Role-specific validation
    if payload.role == "retailer" and not payload.business_name:
        raise HTTPException(status_code=400, detail="business_name is required for SME/retailer accounts")
    if payload.role == "driver" and not payload.license_number:
        raise HTTPException(status_code=400, detail="license_number is required for delivery-maker accounts")

    meta = _build_user_meta(payload)
    auth_resp = _supabase_signup(payload.email, payload.password, meta)
    user_id = str(auth_resp.user.id)

    # Patch phone + role-specific extras into profile (trigger only sets basics)
    extras = _strip_none({
        "phone": payload.phone,
    })
    if extras:
        try:
            _patch_profile(user_id, extras)
        except Exception:
            # Non-fatal: profile was created by the trigger; extras can be added later
            logger.warning("could not patch extras onto profile %s", user_id)

    logger.info("signup complete | user_id=%s role=%s", user_id, payload.role)
    return {
        "message": "Account created. Please check your email to confirm.",
        "user_id": user_id,
        "email": payload.email,
        "role": payload.role,
    }


# ===================================================================
#  Login
# ===================================================================

@app.post("/login", tags=["auth"])
def login(payload: LoginRequest):
    """Authenticate and return a JWT + full profile."""
    logger.info("login attempt | email=%s", payload.email)

    user, session = _supabase_login(payload.email, payload.password)
    user_id = str(user.id)

    profile = _fetch_profile(user_id)

    logger.info("login success | user_id=%s role=%s", user_id, profile.get("role"))
    return {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "token_type": "bearer",
        "expires_in": session.expires_in,
        "user": _format_profile(profile),
    }


# ===================================================================
#  Logout
# ===================================================================

@app.post("/logout", tags=["auth"])
async def logout(current_user: AuthenticatedUser = Depends(get_current_user)):
    """Invalidate the current session on the server side."""
    logger.info("logout | user_id=%s", current_user.id)
    try:
        sb = get_supabase_client()
        sb.client.auth.sign_out()
    except Exception as exc:
        # Best-effort; client should discard the token anyway
        logger.warning("server-side logout failed: %s", exc)
    return {"message": "Logged out"}


# ===================================================================
#  Token refresh
# ===================================================================

@app.post("/refresh", tags=["auth"])
def refresh_token(refresh_token: str):
    """Exchange a refresh token for a new access token."""
    logger.info("token refresh requested")
    sb = get_supabase_client()
    try:
        resp = sb.client.auth.refresh_session(refresh_token)
        if not resp.session:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        return {
            "access_token": resp.session.access_token,
            "refresh_token": resp.session.refresh_token,
            "token_type": "bearer",
            "expires_in": resp.session.expires_in,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("refresh failed: %s", exc)
        raise HTTPException(status_code=401, detail="Could not refresh session")


# ===================================================================
#  Password management
# ===================================================================

@app.post("/password/reset", tags=["auth"])
def request_password_reset(payload: PasswordResetRequest):
    """Send a password-reset email (Supabase handles the email)."""
    logger.info("password reset requested | email=%s", payload.email)
    sb = get_supabase_client()
    try:
        sb.client.auth.reset_password_email(payload.email)
    except Exception as exc:
        # Don't reveal whether the email exists
        logger.warning("reset email call failed: %s", exc)
    return {"message": "If that email is registered you will receive a reset link."}


@app.put("/password", tags=["auth"])
async def update_password(
    payload: PasswordUpdateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Update the authenticated user's password."""
    logger.info("password update | user_id=%s", current_user.id)
    sb = get_supabase_client()
    try:
        sb.client.auth.update_user({"password": payload.new_password})
        return {"message": "Password updated"}
    except Exception as exc:
        logger.error("password update failed: %s", exc)
        raise HTTPException(status_code=500, detail="Password update failed")


# ===================================================================
#  Profile CRUD
# ===================================================================

@app.get("/profile", tags=["profile"])
async def get_profile(current_user: AuthenticatedUser = Depends(get_current_user)):
    """Return the authenticated user's full profile."""
    logger.info("get profile | user_id=%s", current_user.id)
    profile = _fetch_profile(current_user.id)
    return _format_profile(profile)


@app.put("/profile", tags=["profile"])
async def update_profile(
    payload: UpdateProfileRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Partially update the authenticated user's profile.

    Only non-None fields in the request body will be written.
    """
    logger.info("update profile | user_id=%s", current_user.id)
    fields = _strip_none(payload.model_dump())
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    updated = _patch_profile(current_user.id, fields)
    logger.info("profile updated | user_id=%s fields=%s", current_user.id, list(fields.keys()))
    return {"message": "Profile updated", "profile": updated}


@app.delete("/profile", tags=["profile"])
async def deactivate_account(
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Soft-delete: sets is_active = false on the profile."""
    logger.info("deactivate account | user_id=%s", current_user.id)
    _patch_profile(current_user.id, {"is_active": False})
    return {"message": "Account deactivated"}


# ===================================================================
#  Admin-only: list / lookup users
# ===================================================================

@app.get("/users", tags=["admin"])
async def list_users(
    role: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: AuthenticatedUser = Depends(require_roles([UserRole.ADMIN])),
):
    """List user profiles (admin only). Optionally filter by role."""
    logger.info("list users | admin=%s role_filter=%s", current_user.id, role)
    sb = get_supabase_client()
    query = sb.client.table("user_profiles").select("*")
    if role:
        query = query.eq("role", role)
    query = query.order("created_at", desc=True).range(offset, offset + limit - 1)
    resp = query.execute()
    return {"users": [_format_profile(u) for u in (resp.data or [])], "count": len(resp.data or [])}


@app.get("/users/{user_id}", tags=["admin"])
async def get_user_by_id(
    user_id: str,
    current_user: AuthenticatedUser = Depends(require_roles([UserRole.ADMIN])),
):
    """Get any user's profile by ID (admin only)."""
    logger.info("admin get user | admin=%s target=%s", current_user.id, user_id)
    profile = _fetch_profile(user_id)
    return _format_profile(profile)


# ===================================================================
#  Entrypoint (local dev)
# ===================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
