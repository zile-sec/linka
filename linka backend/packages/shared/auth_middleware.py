"""Authentication middleware for Linka services"""
from typing import Optional
from enum import Enum
from pydantic import BaseModel
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import logging
from .supabase_client import get_supabase_client

logger = logging.getLogger(__name__)
security = HTTPBearer()


class UserRole(str, Enum):
    """User roles in the system"""
    ADMIN = "admin"
    RETAILER = "retailer"
    CUSTOMER = "customer"
    DRIVER = "driver"
    SUPPORT = "support"


class AuthenticatedUser(BaseModel):
    """Authenticated user model"""
    id: str
    email: str
    role: UserRole
    kyc_status: str = "unverified"
    kyc_level: int = 0
    full_name: Optional[str] = None
    phone: Optional[str] = None
    
    class Config:
        from_attributes = True


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> AuthenticatedUser:
    """Get current authenticated user from token"""
    try:
        token = credentials.credentials
        supabase = get_supabase_client()
        
        # Verify token and get user
        user_response = supabase.client.auth.get_user(token)
        
        if not user_response or not user_response.user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        
        user_id = user_response.user.id
        
        # Get user profile from database
        profile = supabase.get_single("user_profiles", {"id": user_id})
        
        if not profile:
            raise HTTPException(status_code=404, detail="User profile not found")
        
        return AuthenticatedUser(
            id=user_id,
            email=profile["email"],
            role=UserRole(profile["role"]),
            kyc_status=profile.get("kyc_status", "unverified"),
            kyc_level=profile.get("kyc_level", 0),
            full_name=profile.get("full_name"),
            phone=profile.get("phone")
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error authenticating user: {str(e)}")
        raise HTTPException(status_code=401, detail="Authentication failed")


async def get_current_user_optional(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> Optional[AuthenticatedUser]:
    """Get current user optionally (user might not be authenticated)"""
    if credentials:
        return await get_current_user(credentials)
    return None


def require_roles(allowed_roles: List[UserRole]):
    """Dependency to require specific roles"""
    async def check_role(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=403, 
                detail=f"Access denied. Required roles: {[r.value for r in allowed_roles]}"
            )
        return user
    return check_role


def require_kyc_level(min_level: int = 1):
    """Dependency to require specific KYC level"""
    async def check_kyc(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if user.kyc_level < min_level:
            raise HTTPException(
                status_code=403,
                detail=f"KYC verification level {min_level} required. Current level: {user.kyc_level}"
            )
        return user
    return check_kyc
