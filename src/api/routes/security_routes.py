"""
Local storage security endpoints — Issue #66.

POST   /admin/api-keys            Create a DuckDB-backed API key (admin only)
GET    /admin/api-keys            List all API keys        (admin only)
GET    /admin/api-keys/{key_id}   Get a single key         (admin only)
DELETE /admin/api-keys/{key_id}   Revoke an API key        (admin only)

POST   /admin/mfa/setup           Create/replace TOTP secret (admin only)
POST   /admin/mfa/verify          Verify TOTP code & enable MFA (admin only)
GET    /admin/mfa/status          Check if MFA is enabled (admin only)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.auth.jwt_auth import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["security"])


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _require_admin(user: dict = Depends(require_auth)) -> dict:
    if user.get("role", "").lower() not in {"admin", "administrator"}:
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    role: str = Field("viewer", pattern="^(admin|editor|viewer)$")
    expires_in_days: Optional[int] = Field(365, ge=1, le=3650)
    permissions: List[str] = Field(
        default_factory=list,
        description="e.g. kb:read:<domain>, kb:read:*, documents:ingest[:<source_type>]",
    )


class KeyResponse(BaseModel):
    key_id: str
    key_prefix: str
    name: str
    role: str
    status: str
    created_at: Any
    expires_at: Optional[Any]
    last_used_at: Optional[Any]
    usage_count: int
    permissions: List[str] = Field(default_factory=list)


class CreateKeyResponse(KeyResponse):
    raw_key: str  # Only returned at creation; never stored in plaintext


class MFASetupRequest(BaseModel):
    email: str = Field(..., description="Email address for TOTP URI label")


class MFAVerifyRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=8, description="6-digit TOTP code")


# ---------------------------------------------------------------------------
# API key endpoints
# ---------------------------------------------------------------------------

@router.post("/api-keys", response_model=CreateKeyResponse, status_code=201,
             summary="Create a new local API key (bcrypt-hashed, DuckDB-stored)")
async def create_api_key(
    body: CreateKeyRequest,
    _admin: dict = Depends(_require_admin),
) -> Dict[str, Any]:
    from src.api.auth.local_api_keys import create_api_key as _create
    result = _create(name=body.name, role=body.role, expires_in_days=body.expires_in_days,
                     permissions=body.permissions)
    # Retrieve the stored record to fill remaining fields
    from src.api.auth.local_api_keys import get_api_key
    record = get_api_key(result["key_id"]) or {}
    return {**record, "raw_key": result["raw_key"]}


@router.get("/api-keys", response_model=List[KeyResponse],
            summary="List all local API keys (hashes not returned)")
async def list_api_keys(_admin: dict = Depends(_require_admin)) -> List[Dict[str, Any]]:
    from src.api.auth.local_api_keys import list_api_keys as _list
    return _list()


@router.get("/api-keys/{key_id}", response_model=KeyResponse,
            summary="Get a single API key by ID")
async def get_api_key(
    key_id: str,
    _admin: dict = Depends(_require_admin),
) -> Dict[str, Any]:
    from src.api.auth.local_api_keys import get_api_key as _get
    record = _get(key_id)
    if not record:
        raise HTTPException(status_code=404, detail="API key not found")
    return record


@router.delete("/api-keys/{key_id}", status_code=204,
               summary="Revoke an API key")
async def revoke_api_key(
    key_id: str,
    _admin: dict = Depends(_require_admin),
) -> None:
    from src.api.auth.local_api_keys import revoke_api_key as _revoke
    if not _revoke(key_id):
        raise HTTPException(status_code=404, detail="API key not found")


# ---------------------------------------------------------------------------
# MFA endpoints
# ---------------------------------------------------------------------------

@router.post("/mfa/setup", summary="Generate TOTP secret for admin MFA")
async def mfa_setup(
    body: MFASetupRequest,
    admin: dict = Depends(_require_admin),
) -> Dict[str, Any]:
    """
    Create a TOTP secret for the requesting admin user.

    Returns the `totp_uri` (otpauth:// URI) which can be rendered as a QR
    code by any authenticator app (Google Authenticator, Aegis, etc.) and
    the raw `secret` for manual entry.

    MFA is NOT enabled until `POST /admin/mfa/verify` succeeds.
    """
    from src.api.auth.totp_mfa import setup_totp
    user_id = admin.get("sub", "admin")
    try:
        return setup_totp(user_id=user_id, email=body.email)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/mfa/verify", summary="Verify TOTP code and activate MFA")
async def mfa_verify(
    body: MFAVerifyRequest,
    admin: dict = Depends(_require_admin),
) -> Dict[str, Any]:
    """
    Verify a 6-digit TOTP code against the stored secret.

    On success, MFA is marked as enabled for this user.
    """
    from src.api.auth.totp_mfa import verify_totp
    user_id = admin.get("sub", "admin")
    valid = verify_totp(user_id=user_id, code=body.code)
    if not valid:
        raise HTTPException(status_code=400, detail="Invalid or expired TOTP code")
    return {"mfa_enabled": True, "user_id": user_id}


@router.get("/mfa/status", summary="Check if MFA is enabled for requesting admin")
async def mfa_status(admin: dict = Depends(_require_admin)) -> Dict[str, Any]:
    from src.api.auth.totp_mfa import is_mfa_enabled
    user_id = admin.get("sub", "admin")
    return {"user_id": user_id, "mfa_enabled": is_mfa_enabled(user_id)}
