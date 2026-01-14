from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, EmailStr
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
import os
import logging
from typing import Optional
import sys

# Add packages directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../packages'))

from shared.supabase_client import get_supabase_client
from shared.auth_middleware import get_current_user, AuthenticatedUser

load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Linka User Service")
security = HTTPBearer()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

class UserSignup(BaseModel):
    email: EmailStr
    password: str
    role: str
    full_name: Optional[str] = None
    phone: Optional[str] = None

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UpdateProfile(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    avatar_url: Optional[str] = None


# ============ HEALTH CHECK ENDPOINTS ============
@app.get("/health")
async def health():
    """Liveness probe - basic service health"""
    logger.info("Health check requested")
    return {"status": "alive", "service": "user-service"}

@app.get("/ready")
async def readiness():
    """Readiness probe - verify Supabase connectivity"""
    try:
        logger.info("Readiness check - testing Supabase connection")
        supabase = get_supabase_client()
        # Test connection by querying a system table
        supabase.client.table("user_profiles").select("count", count="exact").limit(0).execute()
        logger.info("Readiness check passed")
        return {"status": "ready", "service": "user-service"}
    except Exception as e:
        logger.error(f"Readiness check failed: {str(e)}")
        raise HTTPException(status_code=503, detail=f"Service not ready: {str(e)}")

# ============ AUTH ENDPOINTS ============

@app.post("/signup")
def signup(user: UserSignup):
    """Register a new user with profile creation"""
    if user.role not in ["customer", "retailer", "driver"]:
        logger.warning(f"Invalid role attempted: {user.role} for email: {user.email}")
        raise HTTPException(
            status_code=400, 
            detail="Invalid role: must be 'customer', 'retailer', or 'driver'"
        )
    
    logger.info(f"User signup initiated - email: {user.email}, role: {user.role}")
    try:
        supabase = get_supabase_client()
        
        auth_response = supabase.client.auth.sign_up({
            "email": user.email,
            "password": user.password,
            "options": {
                "data": {
                    "role": user.role,
                    "full_name": user.full_name or ""
                }
            }
        })
        
        if auth_response.user is None:
            raise HTTPException(status_code=400, detail="Signup failed")
        
        user_id = auth_response.user.id
        
        if user.phone:
            supabase.update("user_profiles", {"id": user_id}, {"phone": user.phone})
        
        logger.info(f"User signup successful - email: {user.email}, user_id: {user_id}")
        
        return {
            "message": "User created successfully. Please check your email to confirm your account.",
            "user_id": user_id,
            "email": user.email,
            "role": user.role
        }
    except Exception as e:
        logger.error(f"Signup failed for {user.email}: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/login")
def login(user: UserLogin):
    """Login user and return token with profile data"""
    logger.info(f"Login attempt - email: {user.email}")
    try:
        supabase = get_supabase_client()
        
        auth_response = supabase.client.auth.sign_in_with_password({
            "email": user.email,
            "password": user.password
        })
        
        if not auth_response.session:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        user_id = auth_response.user.id
        access_token = auth_response.session.access_token
        
        profile = supabase.get_single("user_profiles", {"id": user_id})
        
        if not profile:
            raise HTTPException(status_code=404, detail="User profile not found")
        
        logger.info(f"Login successful - email: {user.email}, role: {profile['role']}")
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "id": user_id,
                "email": profile["email"],
                "role": profile["role"],
                "full_name": profile.get("full_name"),
                "phone": profile.get("phone"),
                "avatar_url": profile.get("avatar_url"),
                "kyc_status": profile.get("kyc_status", "unverified"),
                "kyc_level": profile.get("kyc_level", 0),
                "last_login_at": profile.get("last_login_at"),
                "created_at": profile.get("created_at")
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login failed for {user.email}: {str(e)}")
        raise HTTPException(status_code=401, detail="Login failed")

@app.get("/profile")
async def get_profile(current_user: AuthenticatedUser = Depends(get_current_user)):
    """Get current user's profile"""
    logger.info(f"Profile request for user: {current_user.id}")
    try:
        supabase = get_supabase_client()
        
        profile = supabase.get_single("user_profiles", {"id": current_user.id})
        
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")
        
        logger.info(f"Profile retrieved successfully for user: {current_user.id}")
        return {
            "id": profile["id"],
            "email": profile["email"],
            "role": profile["role"],
            "full_name": profile.get("full_name"),
            "phone": profile.get("phone"),
            "avatar_url": profile.get("avatar_url"),
            "kyc_status": profile.get("kyc_status"),
            "kyc_level": profile.get("kyc_level"),
            "is_active": profile.get("is_active"),
            "last_login_at": profile.get("last_login_at"),
            "created_at": profile.get("created_at"),
            "updated_at": profile.get("updated_at")
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Profile retrieval failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to retrieve profile")

@app.put("/profile")
async def update_profile(
    profile_data: UpdateProfile,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Update current user's profile"""
    logger.info(f"Profile update request for user: {current_user.id}")
    try:
        supabase = get_supabase_client()
        
        update_data = {}
        if profile_data.full_name is not None:
            update_data["full_name"] = profile_data.full_name
        if profile_data.phone is not None:
            update_data["phone"] = profile_data.phone
        if profile_data.avatar_url is not None:
            update_data["avatar_url"] = profile_data.avatar_url
        
        if not update_data:
            raise HTTPException(status_code=400, detail="No update data provided")
        
        updated_profile = supabase.update(
            "user_profiles",
            {"id": current_user.id},
            update_data
        )
        
        logger.info(f"Profile updated successfully for user: {current_user.id}")
        return {
            "message": "Profile updated successfully",
            "profile": updated_profile
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Profile update failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to update profile")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
