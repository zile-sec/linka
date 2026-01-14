from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
import requests
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from datetime import datetime

app = FastAPI(title="Linka Wallet Service")

# ============ HEALTH CHECK ============

@app.get("/health")
async def health():
    return {"status": "alive", "service": "wallet-service", "timestamp": datetime.utcnow().isoformat()}

# Configs (use env vars in prod)
SUPABASE_URL = "https://your-supabase-url.supabase.co"
SUPABASE_KEY = "your-supabase-anon-key"
SMILE_ID_API_KEY = "your-smile-id-api-key"
SMILE_ID_BASE_URL = "https://testapi.smileidentity.com/v1"  # Use prod: https://api.smileidentity.com/v1

# Supabase helper
def supabase_request(endpoint, method="GET", data=None, headers=None):
    url = f"{SUPABASE_URL}/{endpoint}"
    default_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    if headers:
        default_headers.update(headers)
    response = requests.request(method, url, json=data, headers=default_headers)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.json())
    return response.json()

class KYCRequest(BaseModel):
    user_id: str
    id_type: str = "NATIONAL_ID"  # BoZ: NRC common for Zambians
    id_number: str
    first_name: str
    last_name: str
    dob: str  # YYYY-MM-DD
    selfie_image_url: str | None = None  # For liveness/biometrics

class KYCStatus(BaseModel):
    status: str  # pending | verified | rejected | manual_review
    level: int  # 0=basic, 1=full (BoZ tiered)
    reference: str | None

def get_auth_token(credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer())):
    return credentials.credentials

# Initiate KYC (BoZ-compliant: NRC verify + optional biometrics)
@app.post("/kyc/initiate")
def initiate_kyc(request: KYCRequest, token: str = Depends(get_auth_token)):
    # Auth check via Supabase
    supabase_headers = {"Authorization": f"Bearer {token}"}
    user = supabase_request("auth/v1/user", method="GET", headers=supabase_headers)
    
    # Smile ID payload (Zambia-specific: country='ZM')
    payload = {
        "user_id": request.user_id,
        "job_id": f"linka-kyc-{request.user_id}",
        "partner_params": {
            "user_id": request.user_id,
            "job_type": 1,  # Document verification + optional biometrics
            "job_id": f"linka-kyc-{request.user_id}"
        },
        "source": "linka_app",
        "country": "ZM",  # Zambia
        "id_type": request.id_type,
        "id_number": request.id_number,
        "first_name": request.first_name,
        "surname": request.last_name,
        "dob": request.dob,
        # Selfie for enhanced KYC (BoZ tier 2+)
    }
    
    headers = {
        "Authorization": f"Bearer {SMILE_ID_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(f"{SMILE_ID_BASE_URL}/id-verification", json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()
        
        # Update Supabase profile (e.g., in 'users' table with RLS)
        update_data = {
            "kyc_status": "pending",
            "kyc_reference": result.get("job_id"),
            "kyc_level": 0  # Basic until verified
        }
        supabase_request(f"rest/v1/users?id=eq.{request.user_id}", method="PATCH", data=update_data, headers=supabase_headers)
        
        return {
            "status": "initiated",
            "job_id": result.get("job_id"),
            "message": "KYC started. Await verification (NRC/biometrics per BoZ)."
        }
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"KYC failed: {str(e)}")

# Webhook for Smile ID callback (async result handling)
@app.post("/kyc/webhook")
async def kyc_webhook(payload: dict):
    # Validate webhook signature (Smile ID docs)
    # Extract result
    job_id = payload.get("job_id")
    result_code = payload.get("result", {}).get("result_code")
    
    if result_code == "1012":  # Verified (per Smile ID codes)
        kyc_status = "verified"
        kyc_level = 1  # Full tier
    elif result_code in ["1013", "1014"]:  # Rejected/Failed
        kyc_status = "rejected"
        kyc_level = 0
    else:
        kyc_status = "manual_review"
        kyc_level = 0
    
    # Update Supabase (find user by job_id)
    user = supabase_request(f"rest/v1/users?kyc_reference=eq.{job_id}", method="GET")
    if user:
        user_id = user[0]["id"]
        update_data = {"kyc_status": kyc_status, "kyc_level": kyc_level}
        supabase_request(f"rest/v1/users?id=eq.{user_id}", method="PATCH", data=update_data)
    
    return {"status": "processed"}

# Check KYC before operations (middleware-style, callable in routes)
def require_kyc(level: int = 1):
    async def middleware(token: str = Depends(get_auth_token)):
        supabase_headers = {"Authorization": f"Bearer {token}"}
        user = supabase_request("auth/v1/user", method="GET", headers=supabase_headers)
        profile = supabase_request(f"rest/v1/users?id=eq.{user['id']}", method="GET", headers=supabase_headers)[0]
        
        if profile["kyc_level"] < level:
            raise HTTPException(status_code=403, detail=f"KYC level {profile['kyc_level']} insufficient (required: {level}). Complete verification.")
        return profile
    return middleware

# Example: Protect balance endpoint (require full KYC)
@app.get("/balance")
def get_balance(profile: dict = Depends(require_kyc(level=1))):
    # Proceed if KYC passed
    # ... (previous balance logic)
    return {"message": "Balance access granted (KYC verified)."}
