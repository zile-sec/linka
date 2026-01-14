from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, EmailStr
import requests
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
import os
import logging
from typing import Optional

load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Linka User Service")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

class UserSignup(BaseModel):
    email: EmailStr
    password: str
    role: str  # "customer" or "retailer" (validated)

class UserLogin(BaseModel):
    email: EmailStr
    password: str

def supabase_request(endpoint, method="POST", data=None, headers=None):
    url = f"{SUPABASE_URL}/auth/v1{endpoint}"
    default_headers = {
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json"
    }
    if headers:
        default_headers.update(headers)
    response = requests.request(method, url, json=data, headers=default_headers)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.json())
    return response.json()

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
        # Test Supabase connectivity with a simple request
        response = requests.get(
            f"{SUPABASE_URL}/auth/v1/settings",
            headers={"apikey": SUPABASE_KEY},
            timeout=5
        )
        if response.status_code >= 500:
            logger.error("Supabase service unavailable")
            return {"status": "not ready", "detail": "Supabase unavailable"}, 503
        logger.info("Readiness check passed")
        return {"status": "ready", "service": "user-service"}
    except Exception as e:
        logger.error(f"Readiness check failed: {str(e)}")
        return {"status": "not ready", "detail": str(e)}, 503

# ============ AUTH ENDPOINTS ============

@app.post("/signup")
def signup(user: UserSignup):
    if user.role not in ["customer", "retailer"]:
        logger.warning(f"Invalid role attempted: {user.role} for email: {user.email}")
        raise HTTPException(status_code=400, detail="Invalid role: must be 'customer' or 'retailer'")
    
    logger.info(f"User signup initiated - email: {user.email}, role: {user.role}")
    try:
        # Supabase signup
        # Pass role in user_metadata immediately
        data = {
            "email": user.email, 
            "password": user.password,
            "data": {"role": user.role}
        }
        auth_response = supabase_request("/signup", data=data)
        
        # Handle response (access_token might be missing if email confirmation is required)
        access_token = auth_response.get("access_token")
        user_obj = auth_response.get("user", auth_response)
        user_id = user_obj.get("id")
        
        # If retailer, trigger KYC hand-off (async)
        # KYC disabled temporarily
        # if user.role == "retailer":
        #     logger.info(f"Triggering KYC for retailer: {user.email}")
        #     requests.post("http://wallet-service:8000/kyc/initiate", json={"user_id": user_id})
        
        logger.info(f"User signup successful - email: {user.email}")
        return {
            "message": "User created", 
            "role": user.role, 
            "token": access_token,
            "user_id": user_id
        }
    except Exception as e:
        logger.error(f"Signup failed for {user.email}: {str(e)}")
        raise

@app.post("/login")
def login(user: UserLogin):
    logger.info(f"Login attempt - email: {user.email}")
    try:
        data = {"email": user.email, "password": user.password}
        auth_response = supabase_request("/token?grant_type=password", data=data)
        
        # Fetch role from profile
        headers = {"Authorization": f"Bearer {auth_response['access_token']}"}
        profile = supabase_request("/user", method="GET", headers=headers)
        role = profile.get("user_metadata", {}).get("role", "unknown")
        
        logger.info(f"Login successful - email: {user.email}, role: {role}")
        return {"token": auth_response["access_token"], "role": role}
    except Exception as e:
        logger.error(f"Login failed for {user.email}: {str(e)}")
        raise

@app.get("/profile")
def get_profile(credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer())):
    logger.info("Profile request received")
    try:
        headers = {"Authorization": f"Bearer {credentials.credentials}"}
        profile = supabase_request("/user", method="GET", headers=headers)
        logger.info(f"Profile retrieved successfully")
        return profile
    except Exception as e:
        logger.error(f"Profile retrieval failed: {str(e)}")
        raise

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)