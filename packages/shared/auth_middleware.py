"""Authentication middleware for Linka services"""
from typing import Optional
from enum import Enum
from pydantic import BaseModel


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
    role: UserRole
    
    class Config:
        from_attributes = True


async def get_current_user(token: str) -> AuthenticatedUser:
    """Get current authenticated user from token"""
    # Placeholder implementation
    return AuthenticatedUser(id="test-user", role=UserRole.CUSTOMER)


async def get_current_user_optional(token: Optional[str] = None) -> Optional[AuthenticatedUser]:
    """Get current user optionally (user might not be authenticated)"""
    if token:
        return await get_current_user(token)
    return None


def require_roles(roles: list):
    """Dependency to require specific roles"""
    def check_role(user: AuthenticatedUser) -> AuthenticatedUser:
        if user.role not in roles:
            raise PermissionError("User does not have required role")
        return user
    return check_role


def require_kyc_level(level: int = 1):
    """Dependency to require specific KYC level"""
    async def check_kyc(user: AuthenticatedUser) -> AuthenticatedUser:
        # Placeholder implementation
        return user
    return check_kyc


