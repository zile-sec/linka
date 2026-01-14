from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import httpx
from pydantic import BaseModel
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Linka API Gateway")

limiter = Limiter(key_func=get_remote_address)

# Health Check Endpoints for K8s
@app.get("/health")
async def health():
    """Liveness probe - basic service health"""
    return {"status": "alive", "service": "api-gateway"}

@app.get("/ready")
async def readiness():
    """Readiness probe - check downstream service availability"""
    async with httpx.AsyncClient() as client:
        try:
            # Check critical downstream service (user-service)
            response = await client.get(f"{SERVICE_URLS['user']}/health", timeout=2.0)
            if response.status_code != 200:
                return {"status": "not ready", "detail": "user-service unhealthy"}, 503
        except Exception as e:
            return {"status": "not ready", "detail": str(e)}, 503
    return {"status": "ready", "service": "api-gateway"}
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Microservices URLs (env vars or service discovery in prod)
SERVICE_URLS = {
    "user": os.getenv("USER_SERVICE_URL", "http://user-service:8000"),
    "wallet": os.getenv("WALLET_SERVICE_URL", "http://wallet-service:8001"),
    # Add others...
}

# Supabase config
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Middleware for Auth Validation
security = HTTPBearer()

async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {credentials.credentials}", "apikey": SUPABASE_KEY}
        response = await client.get(f"{SUPABASE_URL}/auth/v1/user", headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid token")
        return response.json()

# Middleware for Compliance (e.g., KYC context)
async def compliance_check(request: Request):
    if "wallet" in request.url.path and request.method in ["POST", "PUT"]:
        if "kyc_context" not in request.headers:
            raise HTTPException(status_code=400, detail="Missing compliance header for BoZ KYC")
    return True

# Real-time Support (WebSocket for Supabase broadcasts)
@app.websocket("/realtime")
async def realtime_websocket(websocket):
    await websocket.accept()
    # Integrate Supabase Realtime client here (use supabase-py realtime)
    # Example: subscribe to channels for order/status updates
    # ...

# Auth Flow Endpoints (Login/Signup)
class AuthRequest(BaseModel):
    email: str
    password: str
    role: str = "customer"

@app.post("/auth/signup")
@limiter.limit("5/minute")
async def signup(request: Request, auth: AuthRequest):
    # Forward to User Service for Supabase signup
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{SERVICE_URLS['user']}/signup", json=auth.dict())
        if response.status_code == 200:
            # Hand-off to Wallet for KYC if retailer
            # Assume user type from profile; forward if retailer
            # user_data = response.json()
            # try:
            #     await client.post(f"{SERVICE_URLS['wallet']}/kyc/initiate", json={"user_id": user_data.get("user_id")})
            # except Exception as e:
            #     # KYC initiation failed but user was created, log and continue
            #     pass
            pass
    return response.json()

@app.post("/auth/login")
@limiter.limit("10/minute")
async def login(request: Request, auth: AuthRequest):
    """Login user and return access token with full profile"""
    # Forward to User Service
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{SERVICE_URLS['user']}/login", 
            json=auth.dict(),
            timeout=10.0
        )
        
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=response.json().get("detail", "Login failed")
            )
        
        return response.json()

# General Routing Proxy
@app.api_route("/{service}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
@limiter.limit("100/minute")  # Rate limit example
async def proxy_request(service: str, path: str, request: Request, user: dict = Depends(verify_token), _ = Depends(compliance_check)):
    if service not in SERVICE_URLS:
        raise HTTPException(status_code=404, detail="Service not found")
    
    url = f"{SERVICE_URLS[service]}/{path}"
    async with httpx.AsyncClient() as client:
        headers = dict(request.headers)
        headers.pop("host", None)  # Clean headers
        body = await request.body()
        
        response = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body
        )
        return Response(content=response.content, status_code=response.status_code, headers=dict(response.headers))

# Load Balancing Example (round-robin; use real LB in prod)
# For simplicity, assume single URLs; extend with list of replicas

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
